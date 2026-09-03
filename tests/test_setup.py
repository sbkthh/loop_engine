"""Tests for setup.py — worktree creation, requirement init, full setup flow."""

import sys
import os
import json
import tempfile
import subprocess
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setup import (create_worktree, init_requirement, setup_requirement,
                   add_project_to_requirement, init_from_prd)
from state import StateManager
import registry


class TestCreateWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = os.path.join(self.tmp.name, "source")
        os.makedirs(self.source)
        subprocess.run(["git", "init", "-q"], cwd=self.source, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.source)
        subprocess.run(["git", "config", "user.name", "T"], cwd=self.source)
        with open(os.path.join(self.source, "README.md"), "w") as f:
            f.write("# Test\n")
        subprocess.run(["git", "add", "."], cwd=self.source)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=self.source, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_worktree_success(self):
        target = os.path.join(self.tmp.name, "wt")
        success, msg = create_worktree(self.source, target, "test-change")
        self.assertTrue(success)
        self.assertTrue(os.path.exists(target))
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=target, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "feature/test-change")

    def test_worktree_not_git_repo(self):
        non_git = os.path.join(self.tmp.name, "notgit")
        os.makedirs(non_git)
        target = os.path.join(self.tmp.name, "wt2")
        success, msg = create_worktree(non_git, target, "test-change")
        self.assertFalse(success)
        self.assertTrue(len(msg) > 0)


class TestInitRequirement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_state_and_openspec(self):
        root = self.tmp.name
        result = init_requirement(root)
        self.assertTrue(os.path.exists(result["state_path"]))
        self.assertTrue(os.path.exists(result["openspec_dir"]))
        self.assertIsNone(result["context_path"])

    def test_init_with_context(self):
        root = self.tmp.name
        context = {"databases": {"uat": {"host": "10.0.0.1"}}}
        result = init_requirement(root, context=context)
        self.assertIsNotNone(result["context_path"])
        self.assertTrue(os.path.exists(result["context_path"]))
        with open(result["context_path"]) as f:
            loaded = json.load(f)
        self.assertEqual(loaded["databases"]["uat"]["host"], "10.0.0.1")


class TestSetupRequirement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._original_path = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")

        self.src_a = os.path.join(self.tmp.name, "proj-a")
        self.src_b = os.path.join(self.tmp.name, "proj-b")
        for src in (self.src_a, self.src_b):
            os.makedirs(src)
            subprocess.run(["git", "init", "-q"], cwd=src, check=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=src)
            subprocess.run(["git", "config", "user.name", "T"], cwd=src)
            with open(os.path.join(src, "README.md"), "w") as f:
                f.write("# Test\n")
            subprocess.run(["git", "add", "."], cwd=src)
            subprocess.run(["git", "commit", "-q", "-m", "init"],
                           cwd=src, capture_output=True)

    def tearDown(self):
        registry.REGISTRY_PATH = self._original_path
        self.tmp.cleanup()

    def test_multi_project_flow(self):
        root = os.path.join(self.tmp.name, "requirement-root")
        result = setup_requirement("test-req", root, "chg-1",
                                  [("proj-a", self.src_a), ("proj-b", self.src_b)])
        self.assertNotIn("error", result)
        self.assertTrue(os.path.exists(os.path.join(root, "proj-a")))
        self.assertTrue(os.path.exists(os.path.join(root, "proj-b")))
        self.assertTrue(os.path.exists(os.path.join(root, ".loop", "state.json")))
        entry = registry.find_requirement("test-req")
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry["projects"]), 2)

    def test_single_project(self):
        root = os.path.join(self.tmp.name, "single-root")
        result = setup_requirement("single-req", root, "chg-2",
                                  [("proj-a", self.src_a)])
        self.assertNotIn("error", result)
        self.assertTrue(os.path.exists(os.path.join(root, "proj-a")))
        self.assertTrue(os.path.exists(os.path.join(root, ".loop", "state.json")))
        self.assertTrue(os.path.exists(os.path.join(root, "openspec")))
        entry = registry.find_requirement("single-req")
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry["projects"]), 1)

    def test_duplicate_requirement_name(self):
        root = os.path.join(self.tmp.name, "dup-root")
        setup_requirement("dup-req", root, "chg-3", [("proj-a", self.src_a)])
        result = setup_requirement("dup-req", root, "chg-4", [("proj-a", self.src_a)])
        self.assertIn("error", result)

    def test_add_project_full_flow(self):
        root = os.path.join(self.tmp.name, "add-root")
        result = setup_requirement("add-req", root, "chg-5",
                                   [("proj-a", self.src_a)])
        self.assertNotIn("error", result)
        added = add_project_to_requirement("add-req", "proj-c", self.src_b)
        self.assertNotIn("error", added)
        self.assertEqual(added["project"], "proj-c")
        self.assertTrue(os.path.exists(added["worktree"]))
        self.assertEqual(added["branch"], "feature/chg-5")  # inherited from proj-a
        entry = registry.find_requirement("add-req")
        self.assertEqual(len(entry["projects"]), 2)
        self.assertEqual(entry["projects"][1]["name"], "proj-c")
        self.assertEqual(entry["projects"][1]["branch"], "feature/chg-5")
        branch = subprocess.run(["git", "branch", "--show-current"],
                                cwd=added["worktree"],
                                capture_output=True, text=True)
        self.assertEqual(branch.stdout.strip(), "feature/chg-5")

    def test_add_project_explicit_branch(self):
        root = os.path.join(self.tmp.name, "add-root2")
        setup_requirement("add-req2", root, "chg-6", [("proj-a", self.src_a)])
        added = add_project_to_requirement("add-req2", "proj-c", self.src_b,
                                           branch="feature/custom")
        self.assertNotIn("error", added)
        self.assertEqual(added["branch"], "feature/custom")
        entry = registry.find_requirement("add-req2")
        self.assertEqual(entry["projects"][1]["branch"], "feature/custom")

    def test_add_project_unknown_requirement(self):
        result = add_project_to_requirement("ghost", "proj-c", self.src_b)
        self.assertIn("error", result)

    def test_add_project_duplicate(self):
        root = os.path.join(self.tmp.name, "add-root3")
        setup_requirement("add-req3", root, "chg-7",
                          [("proj-a", self.src_a), ("proj-b", self.src_b)])
        result = add_project_to_requirement("add-req3", "proj-b", self.src_a)
        self.assertIn("error", result)

    def test_add_project_existing_target_dir(self):
        root = os.path.join(self.tmp.name, "add-root4")
        setup_requirement("add-req4", root, "chg-8", [("proj-a", self.src_a)])
        os.makedirs(os.path.join(root, "occupied"))
        result = add_project_to_requirement("add-req4", "occupied", self.src_b)
        self.assertIn("error", result)


class TestInitFromPrdProjectRoot(unittest.TestCase):
    """Bug β regression: PRD-registered modules must not stay at "." —
    resolve_project_root in discovery is gated on `key not in state`, so
    an unbound default sticks forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_registry = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        self.src_a = os.path.join(self.tmp.name, "proj-a")
        self.src_b = os.path.join(self.tmp.name, "proj-b")
        for src in (self.src_a, self.src_b):
            os.makedirs(src)
            subprocess.run(["git", "init", "-q"], cwd=src, check=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=src)
            subprocess.run(["git", "config", "user.name", "T"], cwd=src)
            with open(os.path.join(src, "README.md"), "w") as f:
                f.write("# Test\n")
            subprocess.run(["git", "add", "."], cwd=src)
            subprocess.run(["git", "commit", "-q", "-m", "init"],
                           cwd=src, capture_output=True)
        self.prd = os.path.join(self.tmp.name, "prd.md")
        with open(self.prd, "w") as f:
            f.write("# PRD\n\n## Inventory\n\ncreate item\n\n## Pricing\n\ncalc\n")

    def tearDown(self):
        registry.REGISTRY_PATH = self._orig_registry
        self.tmp.cleanup()

    def _state(self, root):
        return StateManager(root).load()

    def test_binds_first_worktree_path(self):
        root = os.path.join(self.tmp.name, "req-root")
        result = init_from_prd("req-1", root, "chg-a",
                               [("proj-a", self.src_a),
                                ("proj-b", self.src_b)], self.prd)
        self.assertNotIn("error", result)
        state = self._state(root)
        for key, mod in state["modules"].items():
            self.assertEqual(mod["project_root"], os.path.join(root, "proj-a"),
                             f"module {key} stuck at default '.'")
            self.assertNotEqual(mod["project_root"], ".")

    def test_no_projects_keeps_dot_sentinel(self):
        root = os.path.join(self.tmp.name, "req-root-nop")
        # empty projects list → worktree loop skipped, modules default to "."
        result = init_from_prd("req-nop", root, "chg-nop", [], self.prd)
        self.assertNotIn("error", result)
        state = self._state(root)
        self.assertTrue(state["modules"], "expected modules to be registered")
        for mod in state["modules"].values():
            self.assertEqual(mod["project_root"], ".")


if __name__ == '__main__':
    unittest.main()
