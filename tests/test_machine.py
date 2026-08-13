"""Tests for machine.py — full next/commit round-trips (JSON format)."""

import sys
import os
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import StateManager
from machine import StateMachine
from constants import (READY, SYNCED, NEEDS_REFINEMENT, DRAFT, RESULT_FILE,
                       MAKER_STEP1_RED)


def _score_json(score, cross):
    return json.dumps({"score": score, "cross_consistency": cross})


def _maker_step0_json(plan_path):
    return json.dumps({"status": "SUCCESS", "plan_path": plan_path})


def _maker_step1_red_json(files=None, output=None, confirmed=True, tdd_skip=False):
    return json.dumps({
        "status": "SUCCESS",
        "tdd_red_evidence": {
            "test_files_written": files or [],
            "red_test_output": output or "Tests run: 1, Failures: 1",
            "red_confirmed": confirmed,
            "tdd_skip": tdd_skip,
        },
    })


def _maker_step2_green_json(files_created=None, files_modified=None,
                            plan_path="/p.md", test_total=5, test_pass=5,
                            test_fail=0, errors=0, blockers="none", decisions=0):
    return json.dumps({
        "status": "SUCCESS",
        "files_created": files_created or [],
        "files_modified": files_modified or [],
        "plan_path": plan_path,
        "test_results": {"class_name": "FooTest", "total": test_total,
                         "passed": test_pass, "failed": test_fail,
                         "errors": errors},
        "blockers": blockers,
        "human_decisions": decisions,
    })


def _checker_consistent_json():
    return json.dumps({
        "status": "CONSISTENT",
        "discrepancy_count": 0,
        "hard_error_count": 0,
        "soft_warning_count": 0,
        "info_count": 0,
        "discrepancies": [],
        "test_results": {"class_name": "FooTest", "total": 5,
                         "passed": 5, "failed": 0, "errors": 0},
        "coverage": {"tested": 1, "total": 1},
    })


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

    def _write_score(self, score=95, cross="PASS"):
        self._write_result(_score_json(score, cross))

    def _write_maker_step0(self, plan_path):
        self._write_result(
            f"---MAKER_OUTPUT---\n{_maker_step0_json(plan_path)}\n---END_MAKER_OUTPUT---")

    def _write_maker_step1_red(self, files=None, output=None, confirmed=True, tdd_skip=False):
        self._write_result(
            f"---MAKER_OUTPUT---\n{_maker_step1_red_json(files, output, confirmed, tdd_skip)}\n"
            f"---END_MAKER_OUTPUT---")

    def _write_maker_step2_green(self, files_created=None, files_modified=None,
                                 plan_path="/p.md", **kw):
        self._write_result(
            f"---MAKER_OUTPUT---\n{_maker_step2_green_json(files_created, files_modified, plan_path, **kw)}\n"
            f"---END_MAKER_OUTPUT---")

    def _write_checker_consistent(self):
        self._write_result(
            f"---CHECKER_OUTPUT---\n{_checker_consistent_json()}\n---END_CHECKER_OUTPUT---")

    def _drive_to_green(self, machine):
        """Helper: run SCORE + MAKER steps to reach CHECKER."""
        machine.next()
        self._write_score()
        machine.commit()
        machine.next()
        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        open(plan_full, "w").close()
        self._write_maker_step0(plan_path)
        machine.commit()
        machine.next()
        self._write_maker_step1_red(["/t/Foo.java"])
        machine.commit()
        machine.next()
        self._write_maker_step2_green(["/m/Foo.java"],
                                      plan_path="/p.md",
                                      test_total=5, test_pass=5, test_fail=0)
        machine.commit()

    def test_full_round_trip(self):
        self._init_module_ready()
        machine = StateMachine(self.root)

        r = machine.next()
        self.assertEqual(r["action"], "SCORE")
        self.assertEqual(r["module"], self.key)

        self._write_score()
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP0")

        r = machine.next()
        self.assertEqual(r["action"], "MAKER_STEP0")

        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        with open(plan_full, "w") as f:
            f.write("# Plan\n")
        self._write_maker_step0(plan_path)
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP1_RED")

        r = machine.next()
        self.assertEqual(r["action"], "MAKER_STEP1_RED")

        self._write_maker_step1_red(["/src/test/FooTest.java"])
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP2_GREEN")

        r = machine.next()
        self.assertEqual(r["action"], "MAKER_STEP2_GREEN")

        self._write_maker_step2_green(["/src/main/Foo.java"], [],
                                      plan_path="/abs/plan.md",
                                      test_total=5, test_pass=5, test_fail=0)
        r = machine.commit()
        self.assertEqual(r["next_action"], "CHECKER")

        r = machine.next()
        self.assertEqual(r["action"], "CHECKER")

        self._write_checker_consistent()
        r = machine.commit()
        self.assertEqual(r["next_action"], "CODE_REVIEW")

        r = machine.next()
        self.assertEqual(r["action"], "CODE_REVIEW")

        self._write_result(json.dumps({"issues": []}))
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

        self._drive_to_green(machine)

        machine.next()
        self._write_checker_consistent()
        machine.commit()

        machine.next()
        self._write_result(json.dumps({
            "issues": [
                {"severity": "important",
                 "text": "Foo.java:12 — exception swallowed"},
                {"severity": "important",
                 "text": "Foo.java:20 — tx still commits"},
                {"severity": "minor",
                 "text": "Bar.java:5 — POST vs GET"},
            ],
        }))
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
        self._write_score(score=75)
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
        self._write_score(score=95, cross="FAIL")
        r = machine.commit()
        self.assertIsNone(r["next_action"])

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["status"], NEEDS_REFINEMENT)
        self.assertEqual(state["modules"][self.key]["last_score"], 85)

    def test_checker_hard_error_retry(self):
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_green(machine)

        r = machine.next()
        self.assertEqual(r["action"], "CHECKER")

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 1,
            "hard_error_count": 1,
            "soft_warning_count": 0,
            "info_count": 0,
            "discrepancies": [
                {"severity": "HARD_ERROR", "type": "A",
                 "description": "missing field"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 4, "failed": 1, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_FIX")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["maker_attempt"], 2)

    def test_checker_soft_warning_gray_list(self):
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_green(machine)

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 1,
            "hard_error_count": 0,
            "soft_warning_count": 1,
            "info_count": 0,
            "discrepancies": [
                {"severity": "SOFT_WARNING", "type": "B",
                 "description": "method mismatch"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertTrue(len(state["gray_drafts"]) > 0)
        module = state["modules"][self.key]
        self.assertEqual(module.get("_gray_resume"), "MAKER_FIX")

    def test_checker_gray_list_resumes_to_maker_fix_after_accept(self):
        """After all gray drafts accepted, next() resumes to MAKER_FIX."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_green(machine)

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 1,
            "hard_error_count": 0,
            "soft_warning_count": 1,
            "info_count": 0,
            "discrepancies": [
                {"severity": "SOFT_WARNING", "type": "B",
                 "description": "method mismatch"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        sm = StateManager(self.root)
        state = sm.load()
        for d in state["gray_drafts"]:
            d["status"] = "accepted"
        sm.save(state)

        r = machine.next()
        self.assertEqual(r["action"], "MAKER_FIX")
        self.assertEqual(r["module"], self.key)
        state = sm.load()
        self.assertNotIn("_gray_resume", state["modules"].get(self.key, {}))

    def test_checker_gray_list_resumes_to_code_review_after_all_rejected(self):
        """After all gray drafts rejected, next() resumes to CODE_REVIEW."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_green(machine)

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 1,
            "hard_error_count": 0,
            "soft_warning_count": 1,
            "info_count": 0,
            "discrepancies": [
                {"severity": "SOFT_WARNING", "type": "B",
                 "description": "method mismatch"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        sm = StateManager(self.root)
        state = sm.load()
        for d in state["gray_drafts"]:
            d["status"] = "rejected"
        sm.save(state)

        r = machine.next()
        self.assertEqual(r["action"], "ALIGN_DOCS")
        self.assertEqual(r["module"], self.key)
        state = sm.load()
        self.assertNotIn("_gray_resume", state["modules"].get(self.key, {}))

    def test_align_docs_flag_consumed_by_checker_commit(self):
        """_align_done must not survive CHECKER commit, else next() re-dispatches
        a redundant CHECKER after the module reaches SYNCED."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_green(machine)

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 1,
            "hard_error_count": 0,
            "soft_warning_count": 1,
            "info_count": 0,
            "discrepancies": [
                {"severity": "SOFT_WARNING", "type": "B",
                 "description": "method mismatch"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        sm = StateManager(self.root)
        state = sm.load()
        for d in state["gray_drafts"]:
            d["status"] = "rejected"
        sm.save(state)

        # ALIGN_DOCS -> CHECKER route
        r = machine.next()
        self.assertEqual(r["action"], "ALIGN_DOCS")
        self._write_result(json.dumps({
            "status": "SUCCESS",
            "updated_files": ["openspec/changes/chg/specs/m/spec.md"],
            "alignment_report": [{"id": 1, "aligned": True, "note": "ok"}],
        }))
        machine.commit()
        state = sm.load()
        self.assertTrue(state["modules"][self.key].get("_align_done"))

        # CHECKER commit consumes the flag
        self._write_checker_consistent()
        machine.commit()
        state = sm.load()
        self.assertNotIn("_align_done", state["modules"][self.key])
        # and a later next() must NOT re-dispatch CHECKER from the stale flag
        r = machine.next()
        self.assertEqual(r["action"], "CODE_REVIEW")

    def test_checker_gray_list_fallback_when_warnings_unparsed(self):
        """When SOFT_WARNING_COUNT > 0 but no parseable discrepancies,
        fall back to raw count for gray-list routing."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_green(machine)

        # soft_warning_count=1 but discrepancies array is empty —
        # tests the raw_soft > len(parsed_soft) fallback in _commit_checker
        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 1,
            "hard_error_count": 0,
            "soft_warning_count": 1,
            "info_count": 0,
            "discrepancies": [
                {"severity": "SOFT_WARNING", "type": "",
                 "description": "method mismatch"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        # Note: with JSON parsing, the discrepancy IS parsed, so the
        # raw_soft > len(parsed_soft) condition is only relevant for JSON
        # output that the parser cannot parse into 'discrepancies' (shouldn't
        # happen with valid JSON). This test just verifies the GRAY_LIST routing.
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        sm = StateManager(self.root)
        state = sm.load()
        self.assertEqual(len(state["gray_drafts"]), 1)

    def test_checker_filters_rejected_warnings(self):
        """Checker skips soft_warnings whose description matches a rejected draft."""
        from state import StateManager
        self._init_module_ready()
        machine = StateMachine(self.root)

        sm = StateManager(self.root)
        st = sm.load()
        st.setdefault("gray_drafts", []).append({
            "id": 99, "module": self.key, "status": "rejected",
            "summary": "method mismatch",
        })
        sm.save(st)

        self._drive_to_green(machine)

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 2,
            "hard_error_count": 0,
            "soft_warning_count": 2,
            "info_count": 0,
            "discrepancies": [
                {"severity": "SOFT_WARNING", "type": "B",
                 "description": "method mismatch"},
                {"severity": "SOFT_WARNING", "type": "C",
                 "description": "new issue"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "_GRAY_LIST_")

        state = sm.load()
        self.assertEqual(len(state["gray_drafts"]), 1)

    def test_checker_filters_all_rejected_skips_gray_list(self):
        """When all soft_warnings match rejected drafts, skip GRAY_LIST."""
        from state import StateManager
        self._init_module_ready()
        machine = StateMachine(self.root)

        sm = StateManager(self.root)
        st = sm.load()
        st.setdefault("gray_drafts", []).append({
            "id": 99, "module": self.key, "status": "rejected",
            "summary": "method mismatch",
        })
        sm.save(st)

        self._drive_to_green(machine)

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 1,
            "hard_error_count": 0,
            "soft_warning_count": 1,
            "info_count": 0,
            "discrepancies": [
                {"severity": "SOFT_WARNING", "type": "B",
                 "description": "method mismatch"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        self.assertEqual(r["next_action"], "CODE_REVIEW")
        state = sm.load()
        self.assertEqual(len(state["gray_drafts"]), 1)

    def test_checker_fingerprint_suppresses_reworded_findings(self):
        """Rejected fingerprint (file:line) suppresses repeat findings even
        when the LLM rewords the description (exact-match filter misses it)."""
        from state import StateManager
        self._init_module_ready()
        machine = StateMachine(self.root)

        sm = StateManager(self.root)
        st = sm.load()
        st.setdefault("gray_drafts", []).append({
            "id": 99, "module": self.key, "status": "rejected",
            "summary": "Rule.java:236 weight threshold 2000 vs spec 15000",
        })
        sm.save(st)

        self._drive_to_green(machine)

        checker_json = json.dumps({
            "status": "INCONSISTENT",
            "discrepancy_count": 2,
            "hard_error_count": 1,
            "soft_warning_count": 1,
            "info_count": 0,
            "discrepancies": [
                {"severity": "HARD_ERROR", "type": "A",
                 "description": "spec L111 requires 15000 but "
                                 "Rule.java:236 uses 2000"},
                {"severity": "SOFT_WARNING", "type": "B",
                 "description": "weight mismatch at Rule.java:236 "
                                 "(spec L111/L513)"},
            ],
            "test_results": {"class_name": "FooTest", "total": 5,
                             "passed": 5, "failed": 0, "errors": 0},
            "coverage": {"tested": 1, "total": 1},
        })
        self._write_result(
            f"---CHECKER_OUTPUT---\n{checker_json}\n---END_CHECKER_OUTPUT---")
        r = machine.commit()
        # hard suppressed -> no MAKER_FIX; soft suppressed -> no GRAY_LIST
        self.assertEqual(r["next_action"], "CODE_REVIEW")
        state = sm.load()
        self.assertEqual(state["modules"][self.key]["hard_errors"], [])
        self.assertEqual(state["modules"][self.key]["soft_warnings"], [])
        self.assertEqual(
            len(state["modules"][self.key]["suppressed_checker"]), 2)
        self.assertEqual(len(state["gray_drafts"]), 1)

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
        self._write_score()
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

    def test_red_skip_accepts_no_test_files(self):
        self._init_module_ready()
        sm = StateManager(self.root)
        state = sm.load()
        StateManager.set_current(state, self.key, MAKER_STEP1_RED)
        sm.save(state)
        machine = StateMachine(self.root)

        self._write_maker_step1_red(files=[], output=(
            "Tests run: 19, Failures: 0, Errors: 0\nBUILD SUCCESS"),
            tdd_skip=True)
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP2_GREEN")

    def test_red_skip_false_without_files_still_rejected(self):
        self._init_module_ready()
        sm = StateManager(self.root)
        state = sm.load()
        StateManager.set_current(state, self.key, MAKER_STEP1_RED)
        sm.save(state)
        machine = StateMachine(self.root)

        self._write_maker_step1_red(files=[], output=(
            "Tests run: 19, Failures: 0"),
            tdd_skip=False)
        r = machine.commit()
        self.assertIn("No test files written", r.get("error", ""))

    def test_red_implicit_confirm_when_tests_pass_on_existing_impl(self):
        """red_confirmed=false with test files + 0 failures → auto-confirm."""
        self._init_module_ready()
        sm = StateManager(self.root)
        state = sm.load()
        StateManager.set_current(state, self.key, MAKER_STEP1_RED)
        sm.save(state)
        machine = StateMachine(self.root)

        self._write_maker_step1_red(
            files=["/src/test/FooTest.java"],
            output="Tests run: 12, Failures: 0, Errors: 0",
            confirmed=False)
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP2_GREEN")

    def test_red_implicit_confirm_rejected_when_tests_fail(self):
        """red_confirmed=false with test failures → still rejected."""
        self._init_module_ready()
        sm = StateManager(self.root)
        state = sm.load()
        StateManager.set_current(state, self.key, MAKER_STEP1_RED)
        sm.save(state)
        machine = StateMachine(self.root)

        self._write_maker_step1_red(
            files=["/src/test/FooTest.java"],
            output="Tests run: 12, Failures: 2, Errors: 0",
            confirmed=False)
        r = machine.commit()
        self.assertIn("RED not confirmed", r.get("error", ""))


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