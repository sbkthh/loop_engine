"""Tests for `loop_engine close` — closure gates, main-spec sync, archiving."""

import argparse
import datetime
import os
import sys
import tempfile
import unittest

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


class TestClose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_registry_path = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        self.domain = os.path.join(self.tmp.name, "boss")
        self.root = os.path.join(self.domain, "req-a")
        self._build_requirement()

    def tearDown(self):
        registry.REGISTRY_PATH = self._orig_registry_path
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

    def _close(self, name="req-a"):
        cli.cmd_close(argparse.Namespace(name=name))

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

        with self.assertRaises(SystemExit):
            self._close()

        self.assertNotIn("status", registry.find_requirement("req-a"))
        # nothing archived, no partial sync (two-phase: all-or-nothing)
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "openspec", "changes", "chg")))
        self.assertFalse(os.path.exists(
            os.path.join(self.domain, "openspec", "specs", "m-b")))

    def test_close_unknown_requirement(self):
        with self.assertRaises(SystemExit):
            self._close("ghost")
        self.assertIsNone(registry.find_requirement("ghost"))
        self.assertIsNotNone(registry.find_requirement("req-a"))


if __name__ == "__main__":
    unittest.main()
