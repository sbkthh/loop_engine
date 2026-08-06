"""StateMachine: deterministic routing, retry, and state transitions."""

import os
import json
import datetime

from constants import (
    SYNCED, PARTIAL, READY, NEEDS_REFINEMENT, BLOCKED, DRAFT,
    SCORE, CLASSIFY_CHANGE, MAKER_STEP0, MAKER_STEP1_RED,
    MAKER_STEP2_GREEN, CHECKER, MAKER_FIX, CODE_REVIEW, CODE_REVIEW_FIX,
    SCORE_THRESHOLD, MAX_MAKER_ATTEMPTS, MAX_REVIEW_FIX_CYCLES,
    TRACE_RETENTION, AUDIT_RETENTION, RESULT_FILE,
)
from state import StateManager
from spec_utils import discover_modules, compute_spec_hash, derive_spec_path
from parser import (
    parse_maker_output, parse_checker_output,
    parse_score, parse_classify_change, parse_code_review,
)
import directives
import report

_SYNCED = "_SYNCED_"
_GRAY_LIST = "_GRAY_LIST_"


class StateMachine:
    def __init__(self, root_dir="."):
        self.root_dir = os.path.abspath(root_dir)
        self.sm = StateManager(self.root_dir)
        self._handlers = {
            SCORE: self._commit_score,
            CLASSIFY_CHANGE: self._commit_classify_change,
            MAKER_STEP0: self._commit_maker_step0,
            MAKER_STEP1_RED: self._commit_maker_step1_red,
            MAKER_STEP2_GREEN: self._commit_maker_step2_green,
            CHECKER: self._commit_checker,
            MAKER_FIX: self._commit_maker_fix,
            CODE_REVIEW: self._commit_code_review,
            CODE_REVIEW_FIX: self._commit_code_review_fix,
        }

    def next(self):
        state = self.sm.load()

        discovered = discover_modules(self.root_dir)
        for change_id, module_name, spec_path in discovered:
            key = StateManager.module_key(change_id, module_name)
            if key not in state["modules"]:
                spec_hash = compute_spec_hash(spec_path)
                StateManager.add_module(
                    state, key, change_id, module_name, spec_hash=spec_hash
                )

        mid = StateManager.find_mid_progress(state)
        if mid:
            module_key, module, action = mid
            self.sm.save(state)
            return directives.build(action, module_key, module, self.root_dir)

        for key, module in list(state["modules"].items()):
            if module["status"] == SYNCED:
                spec_path = derive_spec_path(
                    module["change_id"], module["module_name"], self.root_dir
                )
                current_hash = compute_spec_hash(spec_path)
                if current_hash and current_hash != module.get("spec_hash"):
                    module["prev_spec_hash"] = module.get("spec_hash")
                    module["spec_hash"] = current_hash
                    module["status"] = PARTIAL
                    StateManager.set_current(state, key, CLASSIFY_CHANGE)
                    self._trace(state, "SCAN", key,
                                f"spec hash changed -> PARTIAL")
                    self.sm.save(state)
                    return directives.build(
                        CLASSIFY_CHANGE, key, module, self.root_dir
                    )

        selected = StateManager.select_next_module(state)
        if not selected:
            self.sm.save(state)
            return {"action": "IDLE", "message": "所有模块同步，无待处理项"}

        module_key, module = selected
        action = None
        if module["status"] == PARTIAL:
            action = CLASSIFY_CHANGE
        elif module["status"] == READY:
            action = SCORE
        elif module["status"] == NEEDS_REFINEMENT:
            action = SCORE
        elif module["status"] == BLOCKED:
            self.sm.save(state)
            return {"action": "IDLE", "message": f"模块 {module_key} 被阻塞"}
        elif module["status"] == DRAFT:
            self.sm.save(state)
            return {"action": "IDLE",
                    "message": f"模块 {module_key} 评分不足，建议使用 openspec-explore 补全"}
        elif module["status"] == SYNCED:
            self.sm.save(state)
            return {"action": "IDLE", "message": "所有模块同步，无待处理项"}

        StateManager.set_current(state, module_key, action)
        self._trace(state, "SCAN", module_key, f"routed to {action}")
        self.sm.save(state)
        module = state["modules"][module_key]
        return directives.build(action, module_key, module, self.root_dir)

    def commit(self):
        state = self.sm.load()

        mid = StateManager.find_mid_progress(state)
        if not mid:
            return {"error": "No module mid-progress. Run 'loop_engine next' first."}

        module_key, module, action = mid

        result_path = os.path.join(self.root_dir, RESULT_FILE)
        if not os.path.exists(result_path):
            return {"error": f"No result file at {result_path}"}
        with open(result_path) as f:
            result_text = f.read()

        handler = self._handlers.get(action)
        if not handler:
            return {"error": f"No handler for action {action}"}

        try:
            next_action = handler(state, module_key, module, result_text)
        except ValueError as e:
            self._trace(state, action, module_key, f"ERROR: {e}", "❌")
            self.sm.save(state)
            return {"error": str(e)}

        if next_action == _SYNCED:
            self._execute_synced(state, module_key, module)
            StateManager.clear_current(state)
        elif next_action == _GRAY_LIST:
            self._execute_gray_list(state, module_key, module)
            StateManager.clear_current(state)
        elif next_action:
            attempt = module.get("maker_attempt", 0)
            StateManager.set_current(state, module_key, next_action, attempt)
        else:
            StateManager.clear_current(state)

        display = (next_action or 'IDLE').replace('_', '')
        self._trace(state, action, module_key,
                     f"committed -> {display}")
        self.sm.save(state)
        report.write(state, self.root_dir)
        self._clear_result()

        return {
            "action": action,
            "module": module_key,
            "result": "committed",
            "next_action": next_action,
        }

    def _commit_score(self, state, key, module, text):
        parsed = parse_score(text)
        score = parsed.get("score")
        cross = parsed.get("cross_consistency")
        if score is None:
            raise ValueError("SCORE field missing from result")
        if cross == "FAIL":
            score = min(score, 85)
        if score >= SCORE_THRESHOLD and cross != "FAIL":
            module["status"] = READY
            return MAKER_STEP0
        module["status"] = NEEDS_REFINEMENT
        return None

    def _commit_classify_change(self, state, key, module, text):
        parsed = parse_classify_change(text)
        magnitude = parsed.get("magnitude")
        if magnitude == "轻量":
            return CHECKER
        if magnitude == "重量":
            return SCORE
        raise ValueError(f"Unknown change magnitude: {magnitude}")

    def _commit_maker_step0(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("No MAKER_OUTPUT block found")
        if parsed.get("status") != "SUCCESS":
            return MAKER_STEP0
        plan_path = parsed.get("plan_path")
        if not plan_path:
            raise ValueError("PLAN_PATH missing")
        full = plan_path if os.path.isabs(plan_path) else os.path.join(
            self.root_dir, plan_path)
        if not os.path.exists(full):
            raise ValueError(f"Plan file not found: {plan_path}")
        module["plan_path"] = plan_path
        return MAKER_STEP1_RED

    def _commit_maker_step1_red(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("No MAKER_OUTPUT block found")
        evidence = parsed.get("tdd_red_evidence")
        if not evidence:
            raise ValueError("TDD_RED_EVIDENCE missing")
        if not evidence.get("red_confirmed"):
            raise ValueError("RED not confirmed")
        if not evidence.get("test_files_written"):
            raise ValueError("No test files written")
        return MAKER_STEP2_GREEN

    def _commit_maker_step2_green(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("No MAKER_OUTPUT block found")
        if parsed.get("status") not in ("SUCCESS", "PARTIAL"):
            raise ValueError(f"Maker failed: {parsed.get('status')}")
        tr = parsed.get("test_results", {})
        if (tr.get("passed") or 0) <= 0:
            raise ValueError("No tests passed")
        module["files_created"] = parsed.get("files_created", [])
        module["files_modified"] = parsed.get("files_modified", [])
        module["maker_attempt"] = 1
        return CHECKER

    def _commit_checker(self, state, key, module, text):
        parsed = parse_checker_output(text)
        if not parsed:
            raise ValueError("No CHECKER_OUTPUT block found")
        hard = parsed.get("hard_error_count") or 0
        soft = parsed.get("soft_warning_count") or 0
        module["hard_errors"] = [
            d for d in parsed.get("discrepancies", [])
            if d.get("severity") == "HARD_ERROR"
        ]
        module["soft_warnings"] = [
            d for d in parsed.get("discrepancies", [])
            if d.get("severity") == "SOFT_WARNING"
        ]
        if hard > 0 and module.get("maker_attempt", 0) < MAX_MAKER_ATTEMPTS:
            module["maker_attempt"] = module.get("maker_attempt", 0) + 1
            return MAKER_FIX
        if hard > 0:
            return None
        if soft > 0:
            return _GRAY_LIST
        return CODE_REVIEW

    def _commit_maker_fix(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("No MAKER_OUTPUT block found")
        if parsed.get("status") != "SUCCESS":
            raise ValueError(f"Fix failed: {parsed.get('status')}")
        tr = parsed.get("test_results", {})
        if (tr.get("passed") or 0) <= 0:
            raise ValueError("No tests passed after fix")
        return CHECKER

    def _commit_code_review(self, state, key, module, text):
        parsed = parse_code_review(text)
        critical = parsed.get("critical", 0)
        important = parsed.get("important", 0)
        if critical > 0 or important > 0:
            module["review_issues"] = parsed.get("issues", [])
        if (critical > 0 or important > 0) and \
                module.get("review_fix_attempt", 0) < MAX_REVIEW_FIX_CYCLES:
            module["review_fix_attempt"] = \
                module.get("review_fix_attempt", 0) + 1
            return CODE_REVIEW_FIX
        return _SYNCED

    def _commit_code_review_fix(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("No MAKER_OUTPUT block found")
        if parsed.get("status") != "SUCCESS":
            raise ValueError(f"Review fix failed: {parsed.get('status')}")
        if module.get("review_fix_attempt", 0) > MAX_REVIEW_FIX_CYCLES:
            return _SYNCED
        return CHECKER

    def _execute_synced(self, state, key, module):
        spec_path = derive_spec_path(
            module["change_id"], module["module_name"], self.root_dir)
        current_hash = compute_spec_hash(spec_path)
        if current_hash:
            module["spec_hash"] = current_hash
        prev_status = module["status"]
        module["status"] = SYNCED
        module["last_synced"] = datetime.datetime.now().isoformat()
        self._audit(state, key, f"{prev_status}->{SYNCED}")
        trail = state.get("audit_trail", [])
        if len(trail) > AUDIT_RETENTION:
            state["audit_trail"] = trail[-AUDIT_RETENTION:]

    def _execute_gray_list(self, state, key, module):
        for warning in module.get("soft_warnings", []):
            state.setdefault("gray_drafts", []).append({
                "id": len(state.get("gray_drafts", [])) + 1,
                "module": key,
                "summary": warning.get("description", ""),
                "status": "pending",
            })

    def _trace(self, state, phase, module_key, output, result="✅"):
        now = datetime.datetime.now()
        state.setdefault("trace", []).append({
            "time": now.strftime("%H:%M"),
            "phase": phase,
            "module": module_key,
            "output": output,
            "result": result,
        })
        if len(state["trace"]) > TRACE_RETENTION:
            state["trace"] = state["trace"][-TRACE_RETENTION:]

    def _audit(self, state, module_key, change, reason="", trigger="自动"):
        now = datetime.datetime.now()
        state.setdefault("audit_trail", []).append({
            "date": now.strftime("%Y-%m-%d %H:%M"),
            "module": module_key,
            "change": change,
            "reason": reason,
            "trigger": trigger,
        })

    def _clear_result(self):
        path = os.path.join(self.root_dir, RESULT_FILE)
        if os.path.exists(path):
            with open(path, "w") as f:
                f.write("")
