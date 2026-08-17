"""Tests for spec_utils.py — test command normalization (clean-first 铁律)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spec_utils import read_test_command, read_checker_test_command


class TestReadTestCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, ".qoder"), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_agents(self, content):
        with open(os.path.join(self.root, ".qoder", "AGENTS.md"), "w") as f:
            f.write(content)

    def test_fallback_includes_clean(self):
        self.assertEqual(read_test_command(self.root), "mvn clean test")

    def test_plain_test_command_gets_clean(self):
        self._write_agents("Run all tests: mvn test")
        self.assertEqual(read_test_command(self.root), "mvn clean test")

    def test_clean_test_command_kept(self):
        self._write_agents("Run all tests: mvn clean test")
        self.assertEqual(read_test_command(self.root), "mvn clean test")

    def test_custom_flags_preserved(self):
        self._write_agents("Run all tests: mvn test -DskipITs")
        self.assertEqual(read_test_command(self.root), "mvn clean test -DskipITs")


class TestReadCheckerTestCommand(unittest.TestCase):
    """CHECKER reuses GREEN's fresh build artifacts: no clean, scoped -pl."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_clean_when_no_files(self):
        self.assertEqual(
            read_checker_test_command("mvn clean test", self.root, []),
            "mvn test")

    def test_scopes_to_changed_modules(self):
        files = [
            os.path.join(self.root, "zkh-opc-sna-manager",
                         "src/main/java/com/X.java"),
            os.path.join(self.root, "zkh-opc-sna-stock-strategy",
                         "src/test/java/com/Y.java"),
        ]
        cmd = read_checker_test_command("mvn clean test", self.root, files)
        self.assertEqual(
            cmd,
            "mvn test -pl zkh-opc-sna-manager,zkh-opc-sna-stock-strategy -am")

    def test_outside_files_ignored(self):
        cmd = read_checker_test_command(
            "mvn clean test", self.root,
            ["/elsewhere/project/src/main/java/X.java"])
        self.assertEqual(cmd, "mvn test")


class TestResolveProjectRoot(unittest.TestCase):
    """projects[].name = real project name; module_to_project bridges module names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)
        import registry as reg
        self._orig_path = reg.REGISTRY_PATH
        reg.REGISTRY_PATH = os.path.join(self.root, "requirements.json")
        self.reg = reg

    def tearDown(self):
        self.reg.REGISTRY_PATH = self._orig_path
        self.tmp.cleanup()

    def _register(self, projects, module_to_project=None):
        entry = {"name": "test-req", "root": self.root, "projects": projects}
        if module_to_project:
            entry["module_to_project"] = module_to_project
        self.reg.save({"requirements": [entry]})

    def test_mapping_prefers_worktree(self):
        src = os.path.join(self.root, "src-repo")
        wt = os.path.join(self.root, "kunhe-wms")
        os.makedirs(src)
        os.makedirs(wt)
        self._register([{"name": "kunhe-wms", "source": src}],
                       module_to_project={"seed-label-print": "kunhe-wms"})
        from spec_utils import resolve_project_root
        self.assertEqual(
            resolve_project_root(self.root, "seed-label-print"), wt)

    def test_mapping_falls_back_to_source(self):
        src = os.path.join(self.root, "kunhe-order")
        os.makedirs(src)
        self._register([{"name": "kunhe-order", "source": src}],
                       module_to_project={"cross-dock-persistence": "kunhe-order"})
        from spec_utils import resolve_project_root
        self.assertEqual(
            resolve_project_root(self.root, "cross-dock-persistence"), src)

    def test_legacy_name_match_without_mapping(self):
        src = os.path.join(self.root, "legacy-proj")
        os.makedirs(src)
        self._register([{"name": "legacy-proj", "source": src}])
        from spec_utils import resolve_project_root
        self.assertEqual(resolve_project_root(self.root, "legacy-proj"), src)

    def test_no_match_returns_none(self):
        src = os.path.join(self.root, "other-proj")
        os.makedirs(src)
        self._register([{"name": "other-proj", "source": src}])
        from spec_utils import resolve_project_root
        self.assertIsNone(resolve_project_root(self.root, "unknown-module"))


if __name__ == '__main__':
    unittest.main()
