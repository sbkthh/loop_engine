"""Tests for `loop_engine close` — closure gates, main-spec sync, archiving."""

import argparse
import datetime
import glob
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import registry
from state import StateManager
from spec_utils import compute_spec_hash

DELTA_A = """## ADDED Requirements

### Requirement: Order creation
The system SHALL create orders.

## MODIFIED Requirements

### Requirement: Order listing
**Existing Requirement**: legacy listing exists.
The system SHALL paginate orders.
"""

DELTA_B = """## External Dependencies

Uses internal HTTP clients only.

## ADDED Requirements

### Requirement: SKU query
The system SHALL query SKUs.
"""

MAIN_A = """# m-a

## Purpose

Baseline purpose for orders.

## Requirements

### Requirement: Order listing
The system SHALL list orders.

### Requirement: Keep me
The system SHALL keep this untouched.

### Requirement: Old thing
The system SHALL do old things.

## External Dependencies

Legacy HTTP client.
"""

MERGE_DELTA_A = """## External Dependencies

Uses internal HTTP clients only.

## ADDED Requirements

### Requirement: Order creation
The system SHALL create orders.

## MODIFIED Requirements

### Requirement: Order listing
The system SHALL paginate orders.

## REMOVED Requirements

### Requirement: Old thing
"""


class TestClose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_registry_path = registry.REGISTRY_PATH
        self._orig_data_dir = cli.DATA_DIR
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        cli.DATA_DIR = os.path.join(self.tmp.name, "data")
        self.domain = os.path.join(self.tmp.name, "boss")
        self.root = os.path.join(self.domain, "req-a")
        self._build_requirement()

    def tearDown(self):
        registry.REGISTRY_PATH = self._orig_registry_path
        cli.DATA_DIR = self._orig_data_dir
        self.tmp.cleanup()

    def _build_requirement(self, overrides=None):
        root = self.root
        for module, delta in (("m-a", DELTA_A), ("m-b", DELTA_B)):
            spec_dir = os.path.join(
                root, "openspec", "changes", "chg", "specs", module)
            os.makedirs(spec_dir, exist_ok=True)
            with open(os.path.join(spec_dir, "spec.md"), "w") as f:
                f.write(delta)
        sm = StateManager(root)
        sm.init_state()
        st = sm.load()
        overrides = overrides or {}
        for key, module in (("chg/m-a", "m-a"), ("chg/m-b", "m-b")):
            spec_path = os.path.join(
                root, "openspec", "changes", "chg", "specs", module, "spec.md")
            sm.add_module(st, key, "chg", module,
                          spec_hash=compute_spec_hash(spec_path))
            st["modules"][key]["status"] = overrides.get(key, "SYNCED")
        sm.save(st)
        registry.add_requirement("req-a", root)

    def _close(self, name="req-a", merge_existing=False):
        cli.cmd_close(argparse.Namespace(
            name=name, merge_existing=merge_existing))

    def _set_delta(self, module, text):
        path = os.path.join(self.root, "openspec", "changes", "chg",
                            "specs", module, "spec.md")
        with open(path, "w") as f:
            f.write(text)
        sm = StateManager(self.root)
        st = sm.load()
        st["modules"][f"chg/{module}"]["spec_hash"] = compute_spec_hash(path)
        sm.save(st)

    def _seed_main_spec(self, module, text):
        d = os.path.join(self.domain, "openspec", "specs", module)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "spec.md")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_close_syncs_main_specs_archives_and_closes(self):
        state_path = os.path.join(self.root, ".loop", "state.json")
        state_before = open(state_path, "rb").read()

        self._close()

        main_a = os.path.join(self.domain, "openspec", "specs", "m-a", "spec.md")
        text_a = open(main_a).read()
        self.assertIn("# m-a", text_a)
        self.assertIn("## Purpose", text_a)
        self.assertEqual(text_a.count("## Requirements"), 1)
        self.assertNotIn("## ADDED Requirements", text_a)
        self.assertNotIn("## MODIFIED Requirements", text_a)
        self.assertEqual(text_a.count("### Requirement:"), 2)
        self.assertIn("req-a", text_a)

        main_b = os.path.join(self.domain, "openspec", "specs", "m-b", "spec.md")
        text_b = open(main_b).read()
        # preamble stays ahead of ## Requirements (same layout as the
        # strategic-stockup manual closure)
        self.assertIn("## External Dependencies", text_b)
        self.assertLess(text_b.index("## External Dependencies"),
                        text_b.index("## Requirements"))
        self.assertEqual(text_b.count("### Requirement:"), 1)

        today = datetime.date.today().isoformat()
        self.assertTrue(os.path.isdir(os.path.join(
            self.root, "openspec", "changes", "archive", f"{today}-chg")))
        self.assertEqual(
            os.listdir(os.path.join(self.root, "openspec", "changes")),
            ["archive"])

        entry = registry.find_requirement("req-a")
        self.assertEqual(entry["status"], "CLOSED")
        self.assertEqual(entry["closed_at"], today)

        # frozen audit trail: state.json byte-identical
        self.assertEqual(open(state_path, "rb").read(), state_before)

    def test_close_refuses_non_synced_module(self):
        sm = StateManager(self.root)
        st = sm.load()
        st["modules"]["chg/m-b"]["status"] = "PARTIAL"
        sm.save(st)

        with self.assertRaises(SystemExit):
            self._close()

        self.assertNotIn("status", registry.find_requirement("req-a"))
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "openspec", "changes", "chg")))
        self.assertFalse(os.path.exists(
            os.path.join(self.domain, "openspec", "specs")))

    def test_close_refuses_pending_gray_drafts(self):
        sm = StateManager(self.root)
        st = sm.load()
        st["gray_drafts"] = [{"id": 1, "module": "chg/m-a",
                              "status": "pending"}]
        sm.save(st)

        with self.assertRaises(SystemExit):
            self._close()

        self.assertNotIn("status", registry.find_requirement("req-a"))

    def test_close_refuses_hash_mismatch(self):
        spec = os.path.join(self.root, "openspec", "changes", "chg",
                            "specs", "m-a", "spec.md")
        with open(spec, "a") as f:
            f.write("\n<!-- tampered after registration -->\n")

        with self.assertRaises(SystemExit):
            self._close()

        self.assertNotIn("status", registry.find_requirement("req-a"))
        self.assertFalse(os.path.exists(
            os.path.join(self.domain, "openspec", "specs", "m-a")))

    def test_close_refuses_existing_main_spec_target(self):
        existing_dir = os.path.join(self.domain, "openspec", "specs", "m-a")
        os.makedirs(existing_dir)
        with open(os.path.join(existing_dir, "spec.md"), "w") as f:
            f.write("# baseline from an earlier requirement\n")

        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            self._close()

        self.assertIn("--merge-existing", out.getvalue())
        self.assertNotIn("status", registry.find_requirement("req-a"))
        # nothing archived, no partial sync (two-phase: all-or-nothing)
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "openspec", "changes", "chg")))
        self.assertFalse(os.path.exists(
            os.path.join(self.domain, "openspec", "specs", "m-b")))

    def test_close_merge_existing_applies_delta_and_backs_up(self):
        main_a = self._seed_main_spec("m-a", MAIN_A)
        old_a = open(main_a).read()
        self._set_delta("m-a", MERGE_DELTA_A)

        out = io.StringIO()
        with redirect_stdout(out):
            self._close(merge_existing=True)

        text = open(main_a).read()
        self.assertIn("Baseline purpose for orders.", text)
        self.assertNotIn("req-a", text)
        self.assertIn("The system SHALL paginate orders.", text)
        self.assertNotIn("The system SHALL list orders.", text)
        self.assertNotIn("### Requirement: Old thing", text)
        self.assertIn("The system SHALL keep this untouched.", text)
        self.assertIn("### Requirement: Order creation", text)
        self.assertLess(text.index("Keep me"), text.index("Order creation"))
        self.assertIn("Uses internal HTTP clients only.", text)
        self.assertNotIn("Legacy HTTP client.", text)
        self.assertEqual(text.count("## Requirements"), 1)
        self.assertIn("合并 +1 ~1 -1", out.getvalue())

        main_b = os.path.join(self.domain, "openspec", "specs", "m-b", "spec.md")
        self.assertTrue(os.path.exists(main_b))

        backups = glob.glob(os.path.join(
            cli.DATA_DIR, "spec-backups", "m-a", "*.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(open(backups[0]).read(), old_a)
        self.assertEqual(
            registry.find_requirement("req-a")["status"], "CLOSED")

    def test_close_merge_conflict_writes_nothing(self):
        main_a = self._seed_main_spec("m-a", MAIN_A)
        before = open(main_a, "rb").read()
        self._set_delta("m-a", """## MODIFIED Requirements

### Requirement: Does not exist
changed
""")

        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            self._close(merge_existing=True)

        self.assertIn("Does not exist", out.getvalue())
        self.assertEqual(open(main_a, "rb").read(), before)
        self.assertFalse(os.path.exists(
            os.path.join(self.domain, "openspec", "specs", "m-b")))
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "openspec", "changes", "chg")))
        self.assertNotIn("status", registry.find_requirement("req-a"))
        self.assertEqual(glob.glob(os.path.join(
            cli.DATA_DIR, "spec-backups", "**", "*.md"), recursive=True), [])

    def test_close_unknown_requirement(self):
        with self.assertRaises(SystemExit):
            self._close("ghost")
        self.assertIsNone(registry.find_requirement("ghost"))
        self.assertIsNotNone(registry.find_requirement("req-a"))


class TestMergeDeltaSpec(unittest.TestCase):
    MAIN = ("# m\n\n## Purpose\n\nKeep this purpose.\n\n"
            "## Requirements\n\n### Requirement: A\nbody a\n")

    def _merge(self, delta, main=None):
        return cli._merge_delta_spec(main or self.MAIN, delta, "m")

    def test_rejects_renamed_and_plain_requirements_sections(self):
        with self.assertRaises(ValueError):
            self._merge("## RENAMED Requirements\n\n### Requirement: A\n")
        with self.assertRaises(ValueError):
            self._merge("## Requirements\n\n### Requirement: A\nbody\n")

    def test_rejects_duplicate_names_inside_delta(self):
        with self.assertRaises(ValueError):
            self._merge("## ADDED Requirements\n\n"
                        "### Requirement: X\none\n\n"
                        "### Requirement: X\ntwo\n")

    def test_rejects_unsafe_operations(self):
        for delta in (
            "## ADDED Requirements\n\n### Requirement: A\nnew\n",
            "## MODIFIED Requirements\n\n### Requirement: B\nnew\n",
            "## REMOVED Requirements\n\n- `### Requirement: B`\n",
            "## ADDED Requirements\n\n### Requirement: X\nnew\n\n"
            "## REMOVED Requirements\n\n### Requirement: X\n",
        ):
            with self.assertRaises(ValueError):
                self._merge(delta)

    def test_appends_new_extra_section(self):
        text, counts = self._merge(
            "## ADDED Requirements\n\n### Requirement: B\nbody b\n\n"
            "## Notes\n\nhello\n")
        self.assertEqual(counts, {"added": 1, "modified": 0, "removed": 0})
        self.assertEqual(text.count("### Requirement:"), 2)
        self.assertIn("Keep this purpose.", text)
        self.assertLess(text.index("## Requirements"), text.index("## Notes"))
        self.assertTrue(text.rstrip().endswith("hello"))

    def test_removed_bullet_and_modified_replace(self):
        main = self.MAIN + "\n### Requirement: B\nold b\n"
        text, counts = self._merge(
            "## MODIFIED Requirements\n\n### Requirement: A\nnew a\n\n"
            "## REMOVED Requirements\n\n- `### Requirement: B`\n", main)
        self.assertEqual(counts, {"added": 0, "modified": 1, "removed": 1})
        self.assertIn("new a", text)
        self.assertNotIn("old b", text)
        self.assertNotIn("### Requirement: B", text)


if __name__ == "__main__":
    unittest.main()
