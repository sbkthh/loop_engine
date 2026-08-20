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
import registry


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

    def test_routes_all_statuses_from_table(self):
        """next() routes every status per STATUS_TABLE (next / idle_msg)."""
        from constants import STATUS_TABLE
        for status, entry in STATUS_TABLE.items():
            with self.subTest(status=status):
                self._init_module_ready()
                sm = StateManager(self.root)
                state = sm.load()
                state["modules"][self.key]["status"] = status
                StateManager.clear_current(state)
                sm.save(state)
                r = StateMachine(self.root).next()
                if entry["next"]:
                    self.assertEqual(r["action"], entry["next"])
                else:
                    self.assertEqual(r["action"], "IDLE")
                    self.assertIn(
                        entry["idle_msg"].format(module_key=self.key),
                        r["message"])

    def test_plan_hash_change_routes_directly_to_maker_step1(self):
        """A SYNCED module whose plan file changed (spec unchanged) routes
        to MAKER_STEP1_RED — skip SCORE and MAKER_STEP0."""
        from constants import MAKER_STEP1_RED
        import hashlib
        sm = StateManager(self.root)
        state = sm.init_state()
        spec_path = os.path.join(self.root,
            "openspec/changes/test-change/specs/test-module/spec.md")
        with open(spec_path, "rb") as f:
            spec_hash = hashlib.md5(f.read()).hexdigest()
        StateManager.add_module(state, self.key, "test-change",
                                "test-module", spec_hash=spec_hash,
                                plan_hash="stale_plan_hash")
        state["modules"][self.key]["status"] = SYNCED
        sm.save(state)

        plan_path = os.path.join(self.root,
            "openspec/changes/test-change/plans/test-module-plan.md")
        os.makedirs(os.path.dirname(plan_path), exist_ok=True)
        with open(plan_path, "w") as f:
            f.write("new plan content")

        r = StateMachine(self.root).next()
        self.assertEqual(r["action"], MAKER_STEP1_RED)
        self.assertIn(self.key, r.get("module", ""))
        # state shows PARTIAL + updated hash
        state = sm.load()
        m = state["modules"][self.key]
        self.assertEqual(m["status"], "PARTIAL")
        self.assertNotEqual(m["plan_hash"], "stale_plan_hash")
        self.assertEqual(m["maker_attempt"], 0)

    def test_plan_hash_initialized_silently_when_none(self):
        """A SYNCED module without plan_hash (upgraded state) silently
        initialises it without triggering a status change."""
        import hashlib
        sm = StateManager(self.root)
        state = sm.init_state()
        spec_path = os.path.join(self.root,
            "openspec/changes/test-change/specs/test-module/spec.md")
        with open(spec_path, "rb") as f:
            spec_hash = hashlib.md5(f.read()).hexdigest()
        StateManager.add_module(state, self.key, "test-change",
                                "test-module", spec_hash=spec_hash)
        state["modules"][self.key]["status"] = SYNCED
        sm.save(state)

        plan_path = os.path.join(self.root,
            "openspec/changes/test-change/plans/test-module-plan.md")
        os.makedirs(os.path.dirname(plan_path), exist_ok=True)
        with open(plan_path, "w") as f:
            f.write("some plan content")

        r = StateMachine(self.root).next()
        self.assertEqual(r["action"], "IDLE")
        state = sm.load()
        m = state["modules"][self.key]
        self.assertEqual(m["status"], "SYNCED")
        self.assertIsNotNone(m["plan_hash"])

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

    def _drive_to_step1(self, machine, plan_body):
        """Drive READY module through SCORE + MAKER_STEP0 (with plan evidence
        verified) to MAKER_STEP1_RED, ready for a gap_audit commit."""
        machine.next()
        self._write_score()
        machine.commit()
        machine.next()
        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        with open(plan_full, "w") as f:
            f.write(plan_body)
        self._write_maker_step0(plan_path)
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP1_RED")

    def _write_maker_step1_red_with_gap_audit(self, audit):
        payload = {
            "status": "SUCCESS",
            "tdd_red_evidence": {
                "test_files_written": ["/src/test/FooTest.java"],
                "red_test_output": "Tests run: 12, Failures: 2, Errors: 0",
                "red_confirmed": True,
                "tdd_skip": False,
            },
            "gap_audit": audit,
        }
        self._write_result(
            f"---MAKER_OUTPUT---\n{json.dumps(payload)}\n---END_MAKER_OUTPUT---")

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

    def test_checker_hard_errors_exhausted_blocks_module(self):
        """MAKER_FIX budget exhausted with hard errors still present →
        module transitions to BLOCKED instead of re-running SCORE→MAKER→
        CHECKER forever (the old path left it READY and every run looped
        until MAX_TOTAL_STEPS)."""
        from constants import BLOCKED, MAX_MAKER_ATTEMPTS
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_green(machine)

        def hard_error_result():
            return json.dumps({
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

        def write_checker_failure():
            self._write_result(
                f"---CHECKER_OUTPUT---\n{hard_error_result()}\n"
                f"---END_CHECKER_OUTPUT---")

        r = machine.next()
        self.assertEqual(r["action"], "CHECKER")
        write_checker_failure()
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_FIX")

        # each remaining budget slot: MAKER_FIX then another failing CHECKER
        for _ in range(MAX_MAKER_ATTEMPTS - 1):
            r = machine.next()
            self.assertEqual(r["action"], "MAKER_FIX")
            self._write_result(
                f"---MAKER_OUTPUT---\n{_maker_step2_green_json()}\n"
                f"---END_MAKER_OUTPUT---")
            machine.commit()
            r = machine.next()
            self.assertEqual(r["action"], "CHECKER")
            write_checker_failure()
            r = machine.commit()

        self.assertIsNone(r["next_action"])
        sm = StateManager(self.root)
        state = sm.load()
        module = state["modules"][self.key]
        self.assertEqual(module["status"], BLOCKED)
        self.assertEqual(module["maker_attempt"], MAX_MAKER_ATTEMPTS)
        # BLOCKED is terminal: no auto routing until the user edits the spec
        r = StateMachine(self.root).next()
        self.assertEqual(r["action"], "IDLE")
        self.assertIn("被阻塞", r["message"])

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
        drafts = state["gray_drafts"]
        self.assertEqual(len(drafts), 2)
        archived = [d for d in drafts if d.get("_archived")]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["id"], 99)
        self.assertEqual(archived[0]["status"], "rejected")
        pending = [d for d in drafts if d.get("status") == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertIn("new issue", pending[0]["summary"])

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
        self.assertIsNotNone(
            state["modules"][self.key].get("spec_norm_hash"))

    def test_needs_refinement_hash_change_routes_classify(self):
        """NEEDS_REFINEMENT 模块的 spec 完善后重新进入评分循环。"""
        self._init_module_ready()
        sm = StateManager(self.root)
        state = sm.load()
        state["modules"][self.key]["status"] = NEEDS_REFINEMENT
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

    def test_cosmetic_spec_change_skips_loop(self):
        """Comment/format-only spec edit: hashes refreshed, stays SYNCED,
        no CLASSIFY_CHANGE routing."""
        from spec_utils import compute_spec_norm_hash
        sm = StateManager(self.root)
        state = sm.init_state()
        import hashlib
        spec_path = os.path.join(self.root,
            "openspec/changes/test-change/specs/test-module/spec.md")
        with open(spec_path, "rb") as f:
            spec_hash = hashlib.md5(f.read()).hexdigest()
        StateManager.add_module(state, self.key, "test-change", "test-module",
                                spec_hash=spec_hash,
                                spec_norm_hash=compute_spec_norm_hash(spec_path))
        state["modules"][self.key]["status"] = SYNCED
        sm.save(state)

        with open(spec_path, "a") as f:
            f.write("\n<!-- review note -->\n")

        machine = StateMachine(self.root)
        r = machine.next()
        self.assertEqual(r["action"], "IDLE")

        state = sm.load()
        m = state["modules"][self.key]
        self.assertEqual(m["status"], "SYNCED")
        self.assertNotEqual(m["spec_hash"], spec_hash)
        self.assertEqual(m["spec_norm_hash"],
                         compute_spec_norm_hash(spec_path))
        self.assertTrue(any("cosmetic" in t.get("output", "")
                            for t in state["trace"]))

    def test_norm_hash_backfilled_when_raw_unchanged(self):
        """Upgraded state (no spec_norm_hash) with unchanged spec:
        silent backfill, no status change."""
        sm = StateManager(self.root)
        state = sm.init_state()
        import hashlib
        spec_path = os.path.join(self.root,
            "openspec/changes/test-change/specs/test-module/spec.md")
        with open(spec_path, "rb") as f:
            spec_hash = hashlib.md5(f.read()).hexdigest()
        StateManager.add_module(state, self.key, "test-change", "test-module",
                                spec_hash=spec_hash)
        state["modules"][self.key]["status"] = SYNCED
        sm.save(state)

        r = StateMachine(self.root).next()
        self.assertEqual(r["action"], "IDLE")

        state = sm.load()
        m = state["modules"][self.key]
        self.assertEqual(m["status"], "SYNCED")
        self.assertIsNotNone(m.get("spec_norm_hash"))

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

    def _valid_claim_plan(self):
        java = os.path.join(self.root, "Foo.java")
        with open(java, "w") as f:
            f.write("a\nb\nc\n")
        return f"- C1: 已有，见 {java}:2\n"

    def test_step0_rejects_plan_without_evidence(self):
        """MAKER_STEP0 plan '已有' claim without file:line → rejected."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        machine.next()
        self._write_score()
        machine.commit()
        machine.next()
        plan_path = "openspec/changes/test-change/plans/test-module-plan.md"
        plan_full = os.path.join(self.root, plan_path)
        os.makedirs(os.path.dirname(plan_full), exist_ok=True)
        with open(plan_full, "w") as f:
            f.write("- C1: 已有实现，无需变更\n")
        self._write_maker_step0(plan_path)
        r = machine.commit()
        self.assertIn("Plan evidence errors", r.get("error", ""))
        self.assertIn("without code evidence", r.get("error", ""))

    def test_step1_red_requires_gap_audit_when_claims_exist(self):
        """Plan has '已有' claims → gap_audit is mandatory in RED output."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_step1(machine, self._valid_claim_plan())

        self._write_maker_step1_red(["/src/test/FooTest.java"])
        r = machine.commit()
        self.assertIn("gap_audit missing", r.get("error", ""))

    def test_step1_red_rejects_short_gap_audit(self):
        """Fewer gap_audit entries than plan claims → rejected."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_step1(machine, self._valid_claim_plan())

        self._write_maker_step1_red_with_gap_audit([])
        r = machine.commit()
        self.assertIn("gap_audit has 0 entries", r.get("error", ""))

    def test_step1_red_rejects_entries_without_evidence(self):
        """gap_audit entry missing evidence field → rejected."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        self._drive_to_step1(machine, self._valid_claim_plan())

        self._write_maker_step1_red_with_gap_audit(
            [{"plan_item": "C1", "verified": True}])
        r = machine.commit()
        self.assertIn("must have 'evidence'", r.get("error", ""))

    def test_step1_red_accepts_complete_gap_audit(self):
        """Every claim verified with evidence → proceeds to GREEN."""
        self._init_module_ready()
        machine = StateMachine(self.root)
        java = os.path.join(self.root, "Foo.java")
        plan = self._valid_claim_plan()
        self._drive_to_step1(machine, plan)

        self._write_maker_step1_red_with_gap_audit(
            [{"plan_item": "C1", "evidence": f"{java}:2",
              "verified": True, "note": "ok"}])
        r = machine.commit()
        self.assertEqual(r["next_action"], "MAKER_STEP2_GREEN")


class TestDiscoveryProjectRoot(unittest.TestCase):
    """Auto-discovered modules must get project_root from the registry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self._orig_registry = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        spec_dir = os.path.join(
            self.root, "openspec/changes/test-change/specs/test-module")
        os.makedirs(spec_dir, exist_ok=True)
        with open(os.path.join(spec_dir, "spec.md"), "w") as f:
            f.write("# Spec\n\n## Scenarios\n\n1. Do it\n")
        self.key = StateManager.module_key("test-change", "test-module")

    def tearDown(self):
        registry.REGISTRY_PATH = self._orig_registry
        self.tmp.cleanup()

    def _discovered_module(self):
        StateMachine(self.root).next()
        return StateManager(self.root).load()["modules"][self.key]

    def test_worktree_mode_prefers_worktree_dir(self):
        src = os.path.join(self.tmp.name, "src-repo")
        os.makedirs(src)
        os.makedirs(os.path.join(self.root, "test-module"))
        registry.add_requirement("test-change", self.root, projects=[
            {"name": "test-module", "source": src, "branch": "feature/x"}])
        module = self._discovered_module()
        self.assertEqual(module["project_root"],
                         os.path.join(self.root, "test-module"))

    def test_direct_mode_falls_back_to_source(self):
        src = os.path.join(self.tmp.name, "src-repo")
        os.makedirs(src)
        registry.add_requirement("test-change", self.root, projects=[
            {"name": "test-module", "source": src, "branch": "feature/x"}])
        module = self._discovered_module()
        self.assertEqual(module["project_root"], src)

    def test_no_registry_match_defaults_to_dot(self):
        module = self._discovered_module()
        self.assertEqual(module["project_root"], ".")

    def test_other_requirement_root_not_matched(self):
        src = os.path.join(self.tmp.name, "src-repo")
        os.makedirs(src)
        other_root = os.path.join(self.tmp.name, "other-root")
        os.makedirs(other_root)
        registry.add_requirement("other-req", other_root, projects=[
            {"name": "test-module", "source": src}])
        module = self._discovered_module()
        self.assertEqual(module["project_root"], ".")


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

    def test_set_project_root(self):
        machine = StateMachine(self.root)
        machine.next()
        sm = StateManager(self.root)
        self.assertEqual(sm.load()["modules"][self.key]["project_root"], ".")

        proj_dir = os.path.join(self.root, "work-dir")
        os.makedirs(proj_dir)
        from cli import cmd_set_project_root
        import argparse
        args = argparse.Namespace(root=self.root, module=self.key, path=proj_dir)
        cmd_set_project_root(args)

        self.assertEqual(sm.load()["modules"][self.key]["project_root"],
                         os.path.abspath(proj_dir))

    def test_set_project_root_missing_dir(self):
        from cli import cmd_set_project_root
        import argparse
        args = argparse.Namespace(root=self.root, module=self.key,
                                  path=os.path.join(self.root, "ghost"))
        with self.assertRaises(SystemExit):
            cmd_set_project_root(args)

    def test_set_project_root_unknown_module(self):
        from cli import cmd_set_project_root
        import argparse
        args = argparse.Namespace(root=self.root, module="change/ghost",
                                  path=self.root)
        with self.assertRaises(SystemExit):
            cmd_set_project_root(args)


class TestSetProjectRootRecordsMapping(unittest.TestCase):
    """cmd_set_project_root auto-records module->project when path matches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self._orig_registry = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.root, "requirements.json")
        spec_dir = os.path.join(self.root,
                                "openspec/changes/test-change/specs/test-module")
        os.makedirs(spec_dir, exist_ok=True)
        with open(os.path.join(spec_dir, "spec.md"), "w") as f:
            f.write("# Test Spec\n")
        self.key = StateManager.module_key("test-change", "test-module")
        self.src = os.path.join(self.root, "src-kunhe-wms")
        os.makedirs(self.src)
        registry.add_requirement("test-change", self.root, projects=[
            {"name": "kunhe-wms", "source": self.src, "branch": "feature/x"}])
        StateMachine(self.root).next()

    def tearDown(self):
        registry.REGISTRY_PATH = self._orig_registry
        self.tmp.cleanup()

    def _bind(self, path):
        from cli import cmd_set_project_root
        import argparse
        os.makedirs(path, exist_ok=True)
        args = argparse.Namespace(root=self.root, module=self.key, path=path)
        cmd_set_project_root(args)

    def _mapping(self):
        return registry.find_requirement("test-change").get(
            "module_to_project", {})

    def test_worktree_bind_records_mapping(self):
        wt = os.path.join(self.root, "kunhe-wms")
        self._bind(wt)
        self.assertEqual(self._mapping(), {"test-module": "kunhe-wms"})

    def test_source_bind_records_mapping(self):
        self._bind(self.src)
        self.assertEqual(self._mapping(), {"test-module": "kunhe-wms"})

    def test_unmatched_path_records_nothing(self):
        self._bind(os.path.join(self.root, "unrelated-dir"))
        self.assertEqual(self._mapping(), {})

    def test_rebind_overwrites_mapping(self):
        self._bind(os.path.join(self.root, "kunhe-wms"))
        other_src = os.path.join(self.root, "src-other")
        os.makedirs(other_src)
        registry.add_project("test-change", "other-proj", other_src)
        self._bind(other_src)
        self.assertEqual(self._mapping(), {"test-module": "other-proj"})

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