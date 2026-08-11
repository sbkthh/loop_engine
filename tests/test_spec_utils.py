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


if __name__ == '__main__':
    unittest.main()
