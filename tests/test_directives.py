"""Tests for directives.py — CHECKER incremental test command."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import (CHECKER, MAKER_STEP0, ALIGN_DOCS,
                       MAKER_STEP1_RED, MAKER_STEP2_GREEN)
from directives import build


class TestCheckerDirective(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _module(self):
        return {
            "change_id": "chg1",
            "module_name": "m1",
            "project_root": ".",
            "spec_hash": "abc",
            "maker_attempt": 1,
            "files_created": [
                os.path.join(self.root, "mod-a/src/test/java/A.java")],
            "files_modified": [
                os.path.join(self.root, "mod-b/src/main/java/B.java")],
        }

    def test_checker_uses_incremental_scoped_command(self):
        out = build(CHECKER, "chg1/m1", self._module(), self.root)
        ins = out["directives"]["instructions"]
        self.assertIn("mvn test -pl mod-a,mod-b -am", ins)
        self.assertNotIn("clean", ins)
        self.assertNotIn("all code files", ins)
        self.assertEqual(
            out["directives"]["context"]["test_command"],
            "mvn test -pl mod-a,mod-b -am")

    def test_checker_falls_back_when_no_files(self):
        m = self._module()
        m["files_created"] = []
        m["files_modified"] = []
        out = build(CHECKER, "chg1/m1", m, self.root)
        self.assertIn("Run 'mvn test'", out["directives"]["instructions"])


class TestMakerStep0Scope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _module(self):
        return {
            "change_id": "chg1",
            "module_name": "m1",
            "project_root": ".",
            "spec_hash": "newhash",
            "prev_spec_hash": "oldhash",
            "maker_attempt": 0,
        }

    def test_maker_step0_plan_scoped_to_current_change_only(self):
        out = build(MAKER_STEP0, "chg1/m1", self._module(), self.root)
        ins = out["directives"]["instructions"]
        self.assertIn("ONLY the current spec change", ins)
        self.assertIn("OUT OF SCOPE", ins)
        self.assertIn("do NOT list them as tasks", ins)
        self.assertEqual(
            out["directives"]["context"]["prev_spec_hash"], "oldhash")

    def test_maker_step0_prev_hash_empty_when_absent(self):
        m = self._module()
        del m["prev_spec_hash"]
        out = build(MAKER_STEP0, "chg1/m1", m, self.root)
        self.assertEqual(
            out["directives"]["context"]["prev_spec_hash"], "")


class TestAlignDocsDirective(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _module(self):
        return {
            "change_id": "chg1",
            "module_name": "m1",
            "project_root": ".",
            "spec_hash": "abc",
            "files_created": ["src/main/java/A.java"],
            "files_modified": ["src/main/java/B.java"],
        }

    def _rejected(self):
        return [
            {"id": 8, "summary": "Rule.java:236 weight 2000 vs spec 15000"},
            {"id": 9, "summary": "Rule.java:236 weight threshold mismatch"},
        ]

    def test_align_docs_injects_rejected_drafts_per_item(self):
        out = build(ALIGN_DOCS, "chg1/m1", self._module(), self.root,
                    rejected_drafts=self._rejected())
        ins = out["directives"]["instructions"]
        self.assertIn("[8] Rule.java:236 weight 2000 vs spec 15000", ins)
        self.assertIn("[9] Rule.java:236 weight threshold mismatch", ins)
        self.assertIn("For EACH rejected warning above", ins)
        self.assertIn("spec must become 2000", ins)
        self.assertIn('"alignment_report"', out["directives"]["output_format"])
        self.assertEqual(out["directives"]["context"]["rejected_drafts"],
                         self._rejected())

    def test_align_docs_empty_rejected_default(self):
        out = build(ALIGN_DOCS, "chg1/m1", self._module(), self.root)
        self.assertIn("For EACH rejected warning above", out["directives"]["instructions"])
        self.assertEqual(out["directives"]["context"]["rejected_drafts"], [])


if __name__ == '__main__':
    unittest.main()


class TestMakerScopedCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "mod-a/src/main/java"))
        self.plan = os.path.join(
            self.root, "openspec/changes/chg1/plans/m1-plan.md")
        os.makedirs(os.path.dirname(self.plan), exist_ok=True)
        with open(self.plan, "w", encoding="utf-8") as f:
            f.write("- 改 mod-a/src/main/java/Foo.java\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _module(self):
        return {
            "change_id": "chg1", "module_name": "m1",
            "project_root": self.root, "spec_hash": "abc",
            "maker_attempt": 1, "plan_path": self.plan,
        }

    def test_red_uses_plan_scoped_command(self):
        out = build(MAKER_STEP1_RED, "chg1/m1", self._module(), self.root)
        self.assertIn("Run 'mvn clean test -pl mod-a -am'.",
                      out["directives"]["instructions"])

    def test_green_uses_scoped_command_plus_full_compile(self):
        out = build(MAKER_STEP2_GREEN, "chg1/m1", self._module(), self.root)
        ins = out["directives"]["instructions"]
        self.assertIn("Run 'mvn clean test -pl mod-a -am'.", ins)
        self.assertIn("mvn clean compile", ins)

    def test_green_without_plan_falls_back_to_full(self):
        os.remove(self.plan)
        m = self._module()
        m["plan_path"] = None
        out = build(MAKER_STEP2_GREEN, "chg1/m1", m, self.root)
        self.assertIn("Run 'mvn clean test'. All tests must pass.",
                      out["directives"]["instructions"])
