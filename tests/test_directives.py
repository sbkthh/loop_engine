"""Tests for directives.py — CHECKER incremental test command."""

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import (CHECKER, CLASSIFY_CHANGE, MAKER_STEP0, ALIGN_DOCS,
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

    def _module(self, prev="oldhash"):
        return {
            "change_id": "chg1",
            "module_name": "m1",
            "project_root": ".",
            "spec_hash": "newhash",
            "prev_spec_hash": prev,
            "maker_attempt": 0,
        }

    def _place_baseline(self, text):
        path = os.path.join(self.root, ".loop", "backup", "spec-m1-1.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return hashlib.md5(text.encode("utf-8")).hexdigest(), path

    def test_resolved_baseline_becomes_the_diff_target(self):
        prev, path = self._place_baseline("导出 19 列")
        out = build(MAKER_STEP0, "chg1/m1", self._module(prev), self.root)
        ins = out["directives"]["instructions"]
        self.assertIn("ONLY the current spec change", ins)
        self.assertIn("OUT OF SCOPE", ins)
        self.assertIn("do NOT list them as tasks", ins)
        self.assertIn("context.prev_spec_path", ins)
        self.assertEqual(out["directives"]["context"]["prev_spec_path"], path)

    def test_unresolvable_baseline_plans_the_whole_spec(self):
        # The draft-33 failure mode: no recoverable previous version, yet the
        # plan still claimed "this increment produces no implementation tasks".
        out = build(MAKER_STEP0, "chg1/m1", self._module(), self.root)
        ins = out["directives"]["instructions"]
        self.assertEqual(out["directives"]["context"]["prev_spec_path"], "")
        self.assertIn("Scope: the WHOLE spec", ins)
        self.assertNotIn("OUT OF SCOPE", ins)

    def test_maker_step0_prev_hash_empty_when_absent(self):
        m = self._module()
        del m["prev_spec_hash"]
        out = build(MAKER_STEP0, "chg1/m1", m, self.root)
        self.assertEqual(
            out["directives"]["context"]["prev_spec_hash"], "")
        self.assertEqual(out["directives"]["context"]["prev_spec_path"], "")

    def test_classify_change_gets_the_baseline_file_to_diff(self):
        prev, path = self._place_baseline("导出 19 列")
        out = build(CLASSIFY_CHANGE, "chg1/m1", self._module(prev), self.root)
        self.assertEqual(out["directives"]["context"]["prev_spec_path"], path)
        self.assertIn("context.prev_spec_path",
                      out["directives"]["instructions"])


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
        self.assertIn("For each rejected warning, determine which document it belongs to", ins)
        self.assertIn('"alignment_report"', out["directives"]["output_format"])
        self.assertEqual(out["directives"]["context"]["rejected_drafts"],
                         self._rejected())

    def test_align_docs_empty_rejected_default(self):
        out = build(ALIGN_DOCS, "chg1/m1", self._module(), self.root)
        self.assertIn("For each rejected warning, determine which document it belongs to", out["directives"]["instructions"])
        self.assertEqual(out["directives"]["context"]["rejected_drafts"], [])


if __name__ == '__main__':
    unittest.main()


class TestMakerScopedCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "mod-a/src/main/java"))
        self.plan = os.path.join(
            self.root, "plans/chg1/m1-plan.md")
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
        ins = out["directives"]["instructions"]
        self.assertIn(
            "Run ONLY the new tests: 'mvn clean test -pl mod-a -am "
            "-Dtest=<the test classes you wrote> "
            "-Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false'.",
            ins)
        self.assertIn(
            "run the full 'mvn clean test -pl mod-a -am' WITHOUT the -Dtest filter",
            ins)

    def test_green_uses_scoped_command_plus_full_compile(self):
        out = build(MAKER_STEP2_GREEN, "chg1/m1", self._module(), self.root)
        ins = out["directives"]["instructions"]
        self.assertIn("Run 'mvn clean test -pl mod-a -am'. All tests must pass.", ins)
        self.assertIn("mvn clean compile", ins)
        self.assertIn("-Dsurefire.failIfNoSpecifiedTests=false", ins)
        self.assertIn("evidence MUST come from the full "
                      "'mvn clean test -pl mod-a -am' run", ins)

    def test_green_without_plan_falls_back_to_full(self):
        os.remove(self.plan)
        m = self._module()
        m["plan_path"] = None
        out = build(MAKER_STEP2_GREEN, "chg1/m1", m, self.root)
        self.assertIn("Run 'mvn clean test'. All tests must pass.",
                      out["directives"]["instructions"])


class TestMultiRepoDirectiveWire(unittest.TestCase):
    """Commit 4: directives.build emits project_roots (canonical) +
    project_root (derived), and per-repo test_commands_by_repo on the
    CHECKER/MAKER context."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)
        self.repo_a = os.path.join(self.root, "kunhe-wms")
        self.repo_b = os.path.join(self.root, "opc-sna")
        for repo, mod in ((self.repo_a, "inventory"), (self.repo_b, "consumer")):
            os.makedirs(os.path.join(repo, mod, "src/main/java"))
            with open(os.path.join(repo, mod, "pom.xml"), "w"):
                pass

    def tearDown(self):
        self.tmp.cleanup()

    def _module(self):
        return {
            "change_id": "chg", "module_name": "m",
            "project_roots": [self.repo_a, self.repo_b],
            "project_root": self.repo_a,
            "spec_hash": "abc", "maker_attempt": 1,
            "files_created": [
                os.path.join(self.repo_a, "inventory/src/main/java/Foo.java"),
                os.path.join(self.repo_b, "consumer/src/main/java/Bar.java"),
            ],
            "files_modified": [],
        }

    def test_plural_roots_wired_on_base_and_context(self):
        out = build(CHECKER, "chg/m", self._module(), self.root)
        self.assertEqual(out["project_roots"], [self.repo_a, self.repo_b])
        self.assertEqual(out["project_root"], self.repo_a)
        self.assertEqual(out["directives"]["context"]["project_roots"],
                         [self.repo_a, self.repo_b])
        self.assertEqual(out["directives"]["context"]["project_root"],
                         self.repo_a)

    def test_checker_per_repo_scoped_command(self):
        out = build(CHECKER, "chg/m", self._module(), self.root)
        by_repo = out["directives"]["context"]["test_commands_by_repo"]
        self.assertEqual(by_repo[self.repo_a], "mvn test -pl inventory -am")
        self.assertEqual(by_repo[self.repo_b], "mvn test -pl consumer -am")
        # legacy singular still points at first repo's scoped command
        self.assertEqual(out["directives"]["context"]["test_command"],
                         by_repo[self.repo_a])
        # Bug α guard: -pl values are maven modules, never repo names
        self.assertNotIn("kunhe-wms", by_repo[self.repo_a])
        self.assertNotIn("opc-sna", by_repo[self.repo_b])

    def test_instructions_list_each_repo_when_multi(self):
        out = build(CHECKER, "chg/m", self._module(), self.root)
        ins = out["directives"]["instructions"]
        self.assertIn(self.repo_a, ins)
        self.assertIn(self.repo_b, ins)
        self.assertIn("Per-repo scoped commands:", ins)


class TestFixStepDeclarationContract(unittest.TestCase):
    """①：两个修复步的输出契约必须索要文件清单——越界修复今天不留痕。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.abspath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _fmt(self, action):
        m = {"change_id": "chg", "module_name": "m", "project_root": ".",
             "spec_hash": "abc", "maker_attempt": 1,
             "hard_errors": [], "review_issues": [],
             "files_created": [], "files_modified": []}
        return build(action, "chg/m", m, self.root)["directives"]["output_format"]

    def test_both_fix_steps_ask_for_the_files_they_edited(self):
        from constants import CODE_REVIEW_FIX, MAKER_FIX
        for action in (MAKER_FIX, CODE_REVIEW_FIX):
            fmt = self._fmt(action)
            self.assertIn('"files_created"', fmt)
            self.assertIn('"files_modified"', fmt)
            self.assertIn("every file this fix actually edited", fmt)

    def test_declaration_is_prompted_beyond_the_plan(self):
        from constants import CODE_REVIEW_FIX, MAKER_FIX
        self.assertIn("does not list them", self._fmt(MAKER_FIX))
        self.assertIn("outside the plan", self._fmt(CODE_REVIEW_FIX))
