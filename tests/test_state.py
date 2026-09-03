"""Tests for state.py — load/save, module selection."""

import sys
import os
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import StateManager
from constants import DRAFT, READY, PARTIAL, SYNCED


class TestStateManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sm = StateManager(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_state(self):
        state = self.sm.init_state()
        self.assertTrue(os.path.exists(self.sm.state_path))
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["modules"], {})

    def test_load_creates_if_missing(self):
        state = self.sm.load()
        self.assertTrue(os.path.exists(self.sm.state_path))
        self.assertEqual(state["modules"], {})

    def test_save_keeps_rolling_backup(self):
        state = self.sm.init_state()
        key = StateManager.module_key("chg", "mod")
        StateManager.add_module(state, key, "chg", "mod")
        state["modules"][key]["status"] = READY
        self.sm.save(state)
        self.assertTrue(os.path.exists(self.sm.bak_path))

        loaded = self.sm.load()
        self.assertEqual(loaded["modules"][key]["status"], READY)
        # backup holds the previous good state
        with open(self.sm.bak_path) as f:
            bak = json.load(f)
        self.assertNotIn(key, bak["modules"])

    def test_load_restores_missing_from_backup(self):
        state = self.sm.init_state()
        key = StateManager.module_key("chg", "mod")
        StateManager.add_module(state, key, "chg", "mod")
        state["modules"][key]["status"] = PARTIAL
        self.sm.save(state)  # bak now holds the initial empty state
        self.sm.save(state)  # current = PARTIAL, bak = previous good

        os.unlink(self.sm.state_path)
        loaded = self.sm.load()

        self.assertTrue(os.path.exists(self.sm.state_path))  # restored
        self.assertEqual(loaded["modules"][key]["status"], PARTIAL)

    def test_load_quarantines_corrupt_and_restores(self):
        state = self.sm.init_state()
        key = StateManager.module_key("chg", "mod")
        StateManager.add_module(state, key, "chg", "mod")
        self.sm.save(state)
        self.sm.save(state)  # bak exists

        with open(self.sm.state_path, "w") as f:
            f.write("{not valid json")
        loaded = self.sm.load()

        self.assertEqual(loaded["modules"][key]["status"], DRAFT)
        with open(self.sm.state_path) as f:
            json.load(f)  # restored file parses
        corrupts = [n for n in os.listdir(self.sm.loop_dir)
                    if n.startswith("state.json.corrupt-")]
        self.assertEqual(len(corrupts), 1)

    def test_load_corrupt_no_backup_rebuilds(self):
        state = self.sm.init_state()  # first save → no bak (no previous state)
        self.assertFalse(os.path.exists(self.sm.bak_path))

        with open(self.sm.state_path, "w") as f:
            f.write("{broken")
        loaded = self.sm.load()

        self.assertEqual(loaded["modules"], {})
        corrupts = [n for n in os.listdir(self.sm.loop_dir)
                    if n.startswith("state.json.corrupt-")]
        self.assertEqual(len(corrupts), 1)

    def test_save_load_roundtrip(self):
        state = self.sm.init_state()
        key = StateManager.module_key("chg", "mod")
        StateManager.add_module(state, key, "chg", "mod",
                                project_root="./proj", spec_hash="abc123")
        state["modules"][key]["status"] = READY
        self.sm.save(state)

        loaded = self.sm.load()
        self.assertEqual(loaded["modules"][key]["status"], READY)
        self.assertEqual(loaded["modules"][key]["spec_hash"], "abc123")

    def test_atomic_save_no_temp_left(self):
        state = self.sm.init_state()
        self.sm.save(state)
        tmps = [f for f in os.listdir(self.sm.loop_dir) if f.endswith(".tmp")]
        self.assertEqual(len(tmps), 0)

    def test_find_mid_progress(self):
        state = self.sm.init_state()
        key = StateManager.module_key("chg", "mod")
        StateManager.add_module(state, key, "chg", "mod")
        StateManager.set_current(state, key, "SCORE")
        self.sm.save(state)

        loaded = self.sm.load()
        result = StateManager.find_mid_progress(loaded)
        self.assertIsNotNone(result)
        mkey, module, action = result
        self.assertEqual(mkey, key)
        self.assertEqual(action, "SCORE")

    def test_find_mid_progress_none(self):
        state = self.sm.init_state()
        result = StateManager.find_mid_progress(state)
        self.assertIsNone(result)

    def test_select_next_module_partial_over_ready(self):
        state = self.sm.init_state()
        k1 = StateManager.module_key("c", "ready_mod")
        k2 = StateManager.module_key("c", "partial_mod")
        StateManager.add_module(state, k1, "c", "ready_mod")
        StateManager.add_module(state, k2, "c", "partial_mod")
        state["modules"][k1]["status"] = READY
        state["modules"][k2]["status"] = PARTIAL
        key, mod = StateManager.select_next_module(state)
        self.assertEqual(key, k2)

    def test_select_next_module_ready(self):
        state = self.sm.init_state()
        key = StateManager.module_key("c", "mod")
        StateManager.add_module(state, key, "c", "mod")
        state["modules"][key]["status"] = READY
        k, m = StateManager.select_next_module(state)
        self.assertEqual(k, key)

    def test_select_next_module_all_synced(self):
        state = self.sm.init_state()
        key = StateManager.module_key("c", "mod")
        StateManager.add_module(state, key, "c", "mod")
        state["modules"][key]["status"] = SYNCED
        k, m = StateManager.select_next_module(state)
        self.assertEqual(k, key)

    def test_add_module_defaults(self):
        state = self.sm.init_state()
        key = StateManager.module_key("c", "mod")
        module = StateManager.add_module(state, key, "c", "mod")
        self.assertEqual(module["status"], DRAFT)
        self.assertEqual(module["maker_attempt"], 0)
        self.assertEqual(module["files_created"], [])


class TestProjectRootsMigration(unittest.TestCase):
    """Commit 1 loader shim: promote legacy scalar project_root to canonical
    project_roots list in memory; disk files are rewritten only on save()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sm = StateManager(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_raw(self, mod_fields):
        raw = {
            "version": 1,
            "root_dir": self.sm.root_dir,
            "current": {"module": None, "action": None, "attempt": 0},
            "modules": {"chg/mod": dict(mod_fields)},
            "gray_drafts": [], "trace": [], "audit_trail": [],
        }
        os.makedirs(os.path.dirname(self.sm.state_path), exist_ok=True)
        with open(self.sm.state_path, "w") as f:
            json.dump(raw, f)

    def test_scalar_root_promoted_to_list(self):
        self._write_raw({
            "change_id": "chg", "module_name": "mod",
            "project_root": "./kunhe-wms", "status": DRAFT,
        })
        mod = self.sm.load()["modules"]["chg/mod"]
        self.assertEqual(mod["project_roots"], ["./kunhe-wms"])
        # derived scalar view kept in sync for un-migrated readers
        self.assertEqual(mod["project_root"], "./kunhe-wms")

    def test_missing_both_fields_defaults_to_unbound(self):
        self._write_raw({
            "change_id": "chg", "module_name": "mod", "status": DRAFT,
        })
        mod = self.sm.load()["modules"]["chg/mod"]
        self.assertEqual(mod["project_roots"], ["."])
        self.assertEqual(mod["project_root"], ".")

    def test_migration_prefers_existing_list_and_is_idempotent(self):
        self._write_raw({
            "change_id": "chg", "module_name": "mod",
            "project_root": "./stale", "project_roots": ["./a", "./b"],
            "status": DRAFT,
        })
        first = self.sm.load()["modules"]["chg/mod"]
        self.assertEqual(first["project_roots"], ["./a", "./b"])
        # derived field refreshed from list, not from the stale scalar
        self.assertEqual(first["project_root"], "./a")
        second = self.sm.load()["modules"]["chg/mod"]
        self.assertEqual(second["project_roots"], ["./a", "./b"])
        self.assertEqual(second["project_root"], "./a")


class TestAddModuleProjectRoots(unittest.TestCase):
    """Commit 3 writer: add_module accepts both kwargs; project_roots wins,
    scalar is derived from the first list element."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sm = StateManager(self.tmp.name)
        self.state = self.sm.init_state()
        self.key = StateManager.module_key("chg", "mod")

    def tearDown(self):
        self.tmp.cleanup()

    def test_plural_kw_wins_over_singular(self):
        module = StateManager.add_module(
            self.state, self.key, "chg", "mod",
            project_root="./ignored", project_roots=["./a", "./b"])
        self.assertEqual(module["project_roots"], ["./a", "./b"])
        self.assertEqual(module["project_root"], "./a")

    def test_singular_kw_still_accepted_and_promoted(self):
        module = StateManager.add_module(
            self.state, self.key, "chg", "mod", project_root="./legacy")
        self.assertEqual(module["project_roots"], ["./legacy"])
        self.assertEqual(module["project_root"], "./legacy")

    def test_defaults_write_both_fields_as_dot(self):
        module = StateManager.add_module(self.state, self.key, "chg", "mod")
        self.assertEqual(module["project_roots"], ["."])
        self.assertEqual(module["project_root"], ".")

    def test_list_is_deduped_and_order_preserved(self):
        module = StateManager.add_module(
            self.state, self.key, "chg", "mod",
            project_roots=["./b", "./a", "./b"])
        self.assertEqual(module["project_roots"], ["./b", "./a"])
        self.assertEqual(module["project_root"], "./b")


if __name__ == '__main__':
    unittest.main()
