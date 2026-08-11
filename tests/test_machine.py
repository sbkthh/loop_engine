"""Tests for machine.py — full next/commit round-trips."""

import sys
import os
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import StateManager
from machine import StateMachine
from constants import READY, SYNCED, NEEDS_REFINEMENT, DRAFT, RESULT_FILE


class TestMachineFullRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        spec_dir = os.path.join(self.root, "openspec/changes/test-change/specs/test-module")
        os.makedirs(spec_dir, exist_ok=True)
        with open(os.path.join(spec_dir, "spec.md"), "w") as f:
            f.write("# Test Spec\n\n## Scenarios\n\n1. Create item\n")
        self.key = StateManager.module_key("test-change", "test-module")

    def tearDown(self):
        self.tmp.cleanup()

    def _init_module_ready(self):
        sm = StateManager(self.root)
        state = sm.init_state()
        import hashlib
        spec_path = os.path.join(self.root,
            "openspec/changes/test-change/specs/test-module/spec.md")
        with open(spec_path, "rb") as f:
            spec_hash = hashlib.md5(f.read()).hexdigest()
        StateManager.add_module(state, self.key, "test-change", "test-module",
                               spec_hash=spec_hash)
        state["modules"][self.key]["status"] = READY
        sm.save(state)

    def _write_result(self, content):
        result_path = os.path.join(self.root, RESULT_FILE)
        os.makedirs(os.path.dirname(result_path), exist_ok=True)
        with open(result_path, "w") as f:
            f.write(content)

    def _result_path(self):
        return os.path.join(self.root, RESULT_FILE)

    def test_full_round_trip(self):
        self._init_module_ready()
        machine = StateMachine(self.root)

        r = machine.next()
        self.assertEqual(r["action"], "SCORE")
        self.assertEqual(r["module"], self.key)

        self._write_result("SCORE: 95/100\nCROSS_CONSISTENCY: PASS")
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP0")

        r = machine.next()
        self.assertEqual(r["action"], "MAKER_STEP0")

        plan_path = f"openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        with open(plan_full, "w") as f:
            f.write("# Plan\n")
        self._write_result(
            f"---MAKER_OUTPUT---\nSTATUS: SUCCESS\nPLAN_PATH: {plan_path}\n---END_MAKER_OUTPUT---"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP1_RED")

        r = machine.next()
        self.assertEqual(r["action"], "MAKER_STEP1_RED")

        self._write_result(
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS\n"
            "TDD_RED_EVIDENCE:\n"
            "  test_files_written:\n"
            "    - /src/test/FooTest.java\n"
            "  red_test_output: |\n"
            "    Tests run: 1, Failures: 1\n"
            "  red_confirmed: true\n"
            "---END_MAKER_OUTPUT---"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP2_GREEN")

        r = machine.next()
        self.assertEqual(r["action"], "MAKER_STEP2_GREEN")

        self._write_result(
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS\n"
            "FILES_CREATED:\n  - /src/main/Foo.java\n"
            "FILES_MODIFIED:\n"
            f"PLAN_PATH: /abs/plan.md\n"
            "TEST_RESULTS:\n"
            "  class: FooTest\n"
            "  total: 5\n  passed: 5  failed: 0\n"
            "BLOCKERS: none\n"
            "HUMAN_DECISIONS: 0\n"
            "---END_MAKER_OUTPUT---"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "CHECKER")

        r = machine.next()
        self.assertEqual(r["action"], "CHECKER")

        self._write_result(
            "---CHECKER_OUTPUT---\n"
            "STATUS: CONSISTENT\n"
            "DISCREPANCY_COUNT: 0\n"
            "HARD_ERROR_COUNT: 0\n"
            "SOFT_WARNING_COUNT: 0\n"
            "INFO_COUNT: 0\n"
            "DISCREPANCIES:\n"
            "TEST_RESULTS:\n"
            "  class: FooTest\n  total: 5  passed: 5  failed: 0  errors: 0\n"
            "COVERAGE: 1/1 Scenarios have test methods\n"
            "---END_CHECKER_OUTPUT---"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "CODE_REVIEW")

        r = machine.next()
        self.assertEqual(r["action"], "CODE_REVIEW")

        self._write_result("No Critical or Important issues found.")
        r = machine.commit()
        self.assertEqual(r["next_action"], "_SYNCED_")

        r = machine.next()
        self.assertEqual(r["action"], "IDLE")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["status"], SYNCED)

    def test_code_review_issues_route_to_fix_and_are_stored(self):
        self._init_module_ready()
        machine = StateMachine(self.root)

        machine.next()
        self._write_result("SCORE: 95/100\nCROSS_CONSISTENCY: PASS")
        machine.commit()

        machine.next()
        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        with open(plan_full, "w") as f:
            f.write("# Plan\n")
        self._write_result(
            f"---MAKER_OUTPUT---\nSTATUS: SUCCESS\nPLAN_PATH: {plan_path}\n---END_MAKER_OUTPUT---"
        )
        machine.commit()

        machine.next()
        self._write_result(
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS\n"
            "TDD_RED_EVIDENCE:\n"
            "  test_files_written:\n"
            "    - /src/test/FooTest.java\n"
            "  red_test_output: |\n"
            "    Tests run: 1, Failures: 1\n"
            "  red_confirmed: true\n"
            "---END_MAKER_OUTPUT---"
        )
        machine.commit()

        machine.next()
        self._write_result(
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS\n"
            "FILES_CREATED:\n  - /src/main/Foo.java\n"
            "FILES_MODIFIED:\n  - /src/main/Foo.java\n"
            "PLAN_PATH: /abs/plan.md\n"
            "TEST_RESULTS:\n"
            "  class: FooTest\n  total: 5\n  passed: 5  failed: 0\n"
            "BLOCKERS: none\n"
            "HUMAN_DECISIONS: 0\n"
            "---END_MAKER_OUTPUT---"
        )
        machine.commit()

        machine.next()
        self._write_result(
            "---CHECKER_OUTPUT---\n"
            "STATUS: CONSISTENT\n"
            "DISCREPANCY_COUNT: 0\n"
            "HARD_ERROR_COUNT: 0\n"
            "SOFT_WARNING_COUNT: 0\n"
            "INFO_COUNT: 0\n"
            "DISCREPANCIES:\n"
            "TEST_RESULTS:\n"
            "  class: FooTest\n  total: 5  passed: 5  failed: 0  errors: 0\n"
            "COVERAGE: 1/1 Scenarios have test methods\n"
            "---END_CHECKER_OUTPUT---"
        )
        machine.commit()

        machine.next()
        self._write_result(
            "**Important** — Foo.java:12 — exception swallowed\n"
            "**Important** — Foo.java:20 — tx still commits\n"
            "**Minor** — Bar.java:5 — POST vs GET\n"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "CODE_REVIEW_FIX")

        sm = StateManager(self.root)
        state = sm.load()
        issues = state["modules"][self.key].get("review_issues", [])
        self.assertEqual(len(issues), 3)
        self.assertEqual(issues[0]["severity"], "important")

    def test_score_below_threshold(self):
        self._init_module_ready()
        machine = StateMachine(self.root)

        machine.next()
        self._write_result("SCORE: 75/100\nCROSS_CONSISTENCY: PASS")
        r = machine.commit()
        self.assertIsNone(r["next_action"])

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["status"], NEEDS_REFINEMENT)
        self.assertEqual(state["modules"][self.key]["last_score"], 75)
        self.assertTrue(any(
            t.get("output") == "committed -> IDLE (SCORE 75/100)"
            for t in state["trace"]))

    def test_score_cross_consistency_fail(self):
        self._init_module_ready()
        machine = StateMachine(self.root)

        machine.next()
        self._write_result("SCORE: 95/100\nCROSS_CONSISTENCY: FAIL: orphan field")
        r = machine.commit()
        self.assertIsNone(r["next_action"])

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["status"], NEEDS_REFINEMENT)
        self.assertEqual(state["modules"][self.key]["last_score"], 85)

    def test_checker_hard_error_retry(self):
        self._init_module_ready()
        machine = StateMachine(self.root)
        machine.next()
        self._write_result("SCORE: 95/100\nCROSS_CONSISTENCY: PASS")
        machine.commit()
        machine.next()

        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        open(plan_full, "w").close()
        self._write_result(
            f"---MAKER_OUTPUT---\nSTATUS: SUCCESS\nPLAN_PATH: {plan_path}\n---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        self._write_result(
            "---MAKER_OUTPUT---\nSTATUS: SUCCESS\n"
            "TDD_RED_EVIDENCE:\n  test_files_written:\n    - /t/Foo.java\n"
            "  red_test_output: |\n    Tests run: 1, Failures: 1\n  red_confirmed: true\n"
            "---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        self._write_result(
            "---MAKER_OUTPUT---\nSTATUS: SUCCESS\nFILES_CREATED:\n  - /m/Foo.java\n"
            "FILES_MODIFIED:\nPLAN_PATH: /p.md\nTEST_RESULTS:\n"
            "  class: FooTest\n  total: 5  passed: 5  failed: 0\n"
            "BLOCKERS: none\nHUMAN_DECISIONS: 0\n---END_MAKER_OUTPUT---"
        )
        machine.commit()

        r = machine.next()
        self.assertEqual(r["action"], "CHECKER")

        self._write_result(
            "---CHECKER_OUTPUT---\nSTATUS: INCONSISTENT\n"
            "DISCREPANCY_COUNT: 1\nHARD_ERROR_COUNT: 1\n"
            "SOFT_WARNING_COUNT: 0\nINFO_COUNT: 0\n"
            "DISCREPANCIES:\n  1. [HARD_ERROR] [A] missing field\n"
            "TEST_RESULTS:\n  class: FooTest\n  total: 5  passed: 4  failed: 1  errors: 0\n"
            "COVERAGE: 1/1 Scenarios have test methods\n---END_CHECKER_OUTPUT---"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_FIX")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["maker_attempt"], 2)

    def test_checker_soft_warning_gray_list(self):
        self._init_module_ready()
        machine = StateMachine(self.root)

        machine.next()
        self._write_result("SCORE: 95/100\nCROSS_CONSISTENCY: PASS")
        machine.commit()
        machine.next()

        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        open(plan_full, "w").close()
        self._write_result(
            f"---MAKER_OUTPUT---\nSTATUS: SUCCESS\nPLAN_PATH: {plan_path}\n---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        self._write_result(
            "---MAKER_OUTPUT---\nSTATUS: SUCCESS\n"
            "TDD_RED_EVIDENCE:\n  test_files_written:\n    - /t/Foo.java\n"
            "  red_test_output: |\n    Tests run: 1, Failures: 1\n  red_confirmed: true\n"
            "---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        self._write_result(
            "---MAKER_OUTPUT---\nSTATUS: SUCCESS\nFILES_CREATED:\n  - /m/Foo.java\n"
            "FILES_MODIFIED:\nPLAN_PATH: /p.md\nTEST_RESULTS:\n"
            "  class: FooTest\n  total: 5  passed: 5  failed: 0\n"
            "BLOCKERS: none\nHUMAN_DECISIONS: 0\n---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        self._write_result(
            "---CHECKER_OUTPUT---\nSTATUS: INCONSISTENT\n"
            "DISCREPANCY_COUNT: 1\nHARD_ERROR_COUNT: 0\n"
            "SOFT_WARNING_COUNT: 1\nINFO_COUNT: 0\n"
            "DISCREPANCIES:\n  1. [SOFT_WARNING] [B] method mismatch\n"
            "TEST_RESULTS:\n  class: FooTest\n  total: 5  passed: 5  failed: 0  errors: 0\n"
            "COVERAGE: 1/1 Scenarios have test methods\n---END_CHECKER_OUTPUT---"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertTrue(len(state["gray_drafts"]) > 0)

    def test_checker_gray_list_fallback_when_warnings_unparsed(self):
        self._init_module_ready()
        machine = StateMachine(self.root)

        machine.next()
        self._write_result("SCORE: 95/100\nCROSS_CONSISTENCY: PASS")
        machine.commit()
        machine.next()

        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        open(plan_full, "w").close()
        self._write_result(
            f"---MAKER_OUTPUT---\nSTATUS: SUCCESS\nPLAN_PATH: {plan_path}\n---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        self._write_result(
            "---MAKER_OUTPUT---\nSTATUS: SUCCESS\n"
            "TDD_RED_EVIDENCE:\n  test_files_written:\n    - /t/Foo.java\n"
            "  red_test_output: |\n    Tests run: 1, Failures: 1\n  red_confirmed: true\n"
            "---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        self._write_result(
            "---MAKER_OUTPUT---\nSTATUS: SUCCESS\nFILES_CREATED:\n  - /m/Foo.java\n"
            "FILES_MODIFIED:\nPLAN_PATH: /p.md\nTEST_RESULTS:\n"
            "  class: FooTest\n  total: 5  passed: 5  failed: 0\n"
            "BLOCKERS: none\nHUMAN_DECISIONS: 0\n---END_MAKER_OUTPUT---"
        )
        machine.commit()
        machine.next()

        # SOFT_WARNING_COUNT > 0 but the entry lacks the [TYPE] bracket, so
        # the discrepancies regex parses nothing — draft must still be created
        self._write_result(
            "---CHECKER_OUTPUT---\nSTATUS: INCONSISTENT\n"
            "DISCREPANCY_COUNT: 1\nHARD_ERROR_COUNT: 0\n"
            "SOFT_WARNING_COUNT: 1\nINFO_COUNT: 0\n"
            "DISCREPANCIES:\n  1. [SOFT_WARNING] method mismatch\n"
            "TEST_RESULTS:\n  class: FooTest\n  total: 5  passed: 5  failed: 0  errors: 0\n"
            "COVERAGE: 1/1 Scenarios have test methods\n---END_CHECKER_OUTPUT---"
        )
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(len(state["gray_drafts"]), 1)
        self.assertIn("未按格式解析", state["gray_drafts"][0]["summary"])

    def test_commit_no_mid_progress(self):
        machine = StateMachine(self.root)
        r = machine.commit()
        self.assertIn("error", r)

    def test_commit_missing_result(self):
        self._init_module_ready()
        machine = StateMachine(self.root)
        machine.next()
        r = machine.commit()
        self.assertIn("error", r)

    def test_result_cleared_after_commit(self):
        self._init_module_ready()
        machine = StateMachine(self.root)
        machine.next()
        self._write_result("SCORE: 95/100\nCROSS_CONSISTENCY: PASS")
        machine.commit()
        with open(self._result_path()) as f:
            self.assertEqual(f.read(), "")

    def test_synced_hash_change_detection(self):
        self._init_module_ready()
        sm = StateManager(self.root)
        state = sm.load()
        state["modules"][self.key]["status"] = SYNCED
        sm.save(state)

        spec_path = os.path.join(self.root,
            "openspec/changes/test-change/specs/test-module/spec.md")
        with open(spec_path, "a") as f:
            f.write("\n## New Scenario\n")

        machine = StateMachine(self.root)
        r = machine.next()
        self.assertEqual(r["action"], "CLASSIFY_CHANGE")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["status"], "PARTIAL")

    def test_trace_trim(self):
        self._init_module_ready()
        machine = StateMachine(self.root)
        sm = StateManager(self.root)

        for i in range(25):
            state = sm.load()
            machine._trace(state, "TEST", self.key, f"trace row {i}")
            sm.save(state)

        state = sm.load()
        self.assertEqual(len(state["trace"]), 20)
        self.assertEqual(state["trace"][-1]["output"], "trace row 24")


class TestCliSetStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        spec_dir = os.path.join(self.root, "openspec/changes/test-change/specs/test-module")
        os.makedirs(spec_dir, exist_ok=True)
        with open(os.path.join(spec_dir, "spec.md"), "w") as f:
            f.write("# Test Spec\n")
        self.key = StateManager.module_key("test-change", "test-module")

    def tearDown(self):
        self.tmp.cleanup()

    def test_set_status_draft_to_ready(self):
        machine = StateMachine(self.root)
        machine.next()
        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["status"], "DRAFT")

        from cli import cmd_set_status
        import argparse
        args = argparse.Namespace(
            root=self.root, module=self.key, status="READY")
        cmd_set_status(args)

        state = sm.load()
        self.assertEqual(state["modules"][self.key]["status"], "READY")

    def test_set_status_invalid_status(self):
        from cli import cmd_set_status
        import argparse
        args = argparse.Namespace(
            root=self.root, module=self.key, status="BOGUS")
        with self.assertRaises(SystemExit):
            cmd_set_status(args)

    def test_set_status_clears_mid_progress(self):
        machine = StateMachine(self.root)
        machine.next()
        sm = StateManager(self.root)
        state = sm.load()
        state["modules"][self.key]["status"] = "READY"
        StateManager.set_current(state, self.key, "SCORE")
        sm.save(state)
        machine.next()

        from cli import cmd_set_status
        import argparse
        args = argparse.Namespace(
            root=self.root, module=self.key, status="DRAFT")
        cmd_set_status(args)

        state = sm.load()
        self.assertIsNone(state["current"]["module"])
        self.assertIsNone(state["current"]["action"])


if __name__ == '__main__':
    unittest.main()
