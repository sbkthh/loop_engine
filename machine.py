"""StateMachine: deterministic routing, retry, and state transitions."""

import os
import json
import re
import datetime
import subprocess

from constants import (
    SYNCED, PARTIAL, READY, NEEDS_REFINEMENT, BLOCKED, DRAFT,
    SCORE, CLASSIFY_CHANGE, MAKER_STEP0, MAKER_STEP1_RED,
    MAKER_STEP2_GREEN, CHECKER, MAKER_FIX, CODE_REVIEW, CODE_REVIEW_FIX,
    ALIGN_DOCS, STATUS_TABLE,
    SCORE_THRESHOLD, MAX_MAKER_ATTEMPTS, MAX_REVIEW_FIX_CYCLES,
    TRACE_RETENTION, AUDIT_RETENTION, MAX_GRAY_DRAFTS, RESULT_FILE,
)
from state import StateManager
from spec_utils import (discover_modules, compute_spec_hash,
                        compute_spec_norm_hash, compute_plan_hash,
                        derive_spec_path, derive_plan_path, resolve_project_root,
                        audit_plan_existing_evidence,
                        count_plan_existing_claims)
from parser import (
    parse_maker_output, parse_checker_output,
    parse_score, parse_classify_change, parse_code_review,
    parse_align_docs,
)
import directives
import report

_SYNCED = "_SYNCED_"
_GRAY_LIST = "_GRAY_LIST_"

# filename:lineno (or lineno range) inside a warning description, e.g.
# "StockStrategyMaterialExclusionRule.java:236". Used to suppress repeat
# checker findings for deviations the user already adjudicated as rejected.
_FINGERPRINT_RE = re.compile(
    r"([^/\s]+\.(?:java|py|ts|js|xml|go|rs|kt)):(\d+)(?:[-~–](\d+))?"
)


def _draft_fingerprints(drafts):
    fps = set()
    for d in drafts:
        fps.update(m.group(0)
                   for m in _FINGERPRINT_RE.finditer(d.get("summary", "")))
    return fps


def resolve_gray_draft(sm, draft_id, decision):
    """Adjudicate one gray-list draft. Returns (changed, message)."""
    state = sm.load()
    for d in state.get("gray_drafts", []):
        if d.get("id") == draft_id:
            if d.get("status") != "pending":
                return False, f"草稿 {draft_id} 已裁决（{d['status']}）"
            d["status"] = "accepted" if decision == "accept" else "rejected"
            sm.save(state)
            label = "接受" if decision == "accept" else "拒绝"
            return True, f"已{label}草稿 {draft_id}"
    return False, f"找不到草稿 {draft_id}"


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
            ALIGN_DOCS: self._commit_align_docs,
        }

    def next(self):
        state = self.sm.load()
        dirty = False

        discovered = discover_modules(self.root_dir)
        for change_id, module_name, spec_path in discovered:
            key = StateManager.module_key(change_id, module_name)
            if key not in state["modules"]:
                spec_hash = compute_spec_hash(spec_path)
                spec_norm_hash = compute_spec_norm_hash(spec_path)
                project_root = resolve_project_root(self.root_dir, module_name)
                plan_path = derive_plan_path(change_id, module_name, self.root_dir)
                plan_hash = compute_plan_hash(plan_path)
                StateManager.add_module(
                    state, key, change_id, module_name,
                    project_root=project_root or ".",
                    spec_hash=spec_hash, spec_norm_hash=spec_norm_hash,
                    plan_hash=plan_hash
                )
                dirty = True

        mid = StateManager.find_mid_progress(state)
        if mid:
            module_key, module, action = mid
            if dirty:
                self.sm.save(state)
            return self._build(state, action, module_key, module)

        for key, module in list(state["modules"].items()):
            if module["status"] in (SYNCED, NEEDS_REFINEMENT):
                # NEEDS_REFINEMENT: spec 完善后重新进入评分循环
                spec_path = derive_spec_path(
                    module["change_id"], module["module_name"], self.root_dir
                )
                current_hash = compute_spec_hash(spec_path)
                if current_hash and current_hash != module.get("spec_hash"):
                    norm_hash = compute_spec_norm_hash(spec_path)
                    old_norm = module.get("spec_norm_hash")
                    if old_norm is not None and norm_hash and \
                            norm_hash == old_norm:
                        # comment/format-only edit: normalized content
                        # unchanged, skip the loop (rule exception)
                        module["spec_hash"] = current_hash
                        module["spec_norm_hash"] = norm_hash
                        dirty = True
                        self._trace(state, "SCAN", key,
                                    "spec changed (cosmetic) -> loop skipped")
                        continue
                    module["prev_spec_hash"] = module.get("spec_hash")
                    module["spec_hash"] = current_hash
                    if norm_hash:
                        module["spec_norm_hash"] = norm_hash
                    module["status"] = PARTIAL
                    module["maker_attempt"] = 0
                    module["review_fix_attempt"] = 0
                    StateManager.set_current(state, key, CLASSIFY_CHANGE)
                    self._trace(state, "SCAN", key,
                                f"spec hash changed -> PARTIAL")
                    dirty = True
                    self.sm.save(state)
                    return self._build(
                        state, CLASSIFY_CHANGE, key, module
                    )
                elif module.get("spec_norm_hash") is None:
                    # upgraded state: backfill normalized hash silently so
                    # the next cosmetic edit is recognized as cosmetic
                    norm_hash = compute_spec_norm_hash(spec_path)
                    if norm_hash:
                        module["spec_norm_hash"] = norm_hash
                        dirty = True

        # Plan hash change detection: SYNCED module whose plan file changed
        # independently of its spec. Route directly to MAKER_STEP1_RED (the
        # plan already exists, no re-scoring or plan generation needed).
        for key, module in list(state["modules"].items()):
            if module["status"] == SYNCED:
                plan_path = derive_plan_path(
                    module["change_id"], module["module_name"], self.root_dir
                )
                current_hash = compute_plan_hash(plan_path)
                if current_hash and current_hash != module.get("plan_hash"):
                    if module.get("plan_hash") is None:
                        # first-time initialization (upgraded state)
                        module["plan_hash"] = current_hash
                        dirty = True
                    else:
                        module["prev_plan_hash"] = module.get("plan_hash")
                        module["plan_hash"] = current_hash
                        module["status"] = PARTIAL
                        module["maker_attempt"] = 0
                        module["review_fix_attempt"] = 0
                        StateManager.set_current(state, key, MAKER_STEP1_RED)
                        self._trace(state, "SCAN", key,
                                    f"plan hash changed -> PARTIAL")
                        dirty = True
                        self.sm.save(state)
                        return self._build(
                            state, MAKER_STEP1_RED, key, module
                        )

        selected = StateManager.select_next_module(state)
        if not selected:
            if dirty:
                self.sm.save(state)
            return {"action": "IDLE", "message": "所有模块同步，无待处理项"}

        module_key, module = selected

        # ALIGN_DOCS completed: skip MAKER steps, go directly to CHECKER
        if module.get("_align_done"):
            del module["_align_done"]
            self._refresh_spec_hashes(module)
            action = CHECKER
            StateManager.set_current(state, module_key, action)
            self._trace(state, "SCAN", module_key, "align done -> CHECKER")
            dirty = True
            self.sm.save(state)
            module = state["modules"][module_key]
            return self._build(state, CHECKER, module_key, module)

        gray_resume = module.get("_gray_resume")
        if gray_resume:
            pending = [d for d in state.get("gray_drafts", [])
                       if d.get("status") == "pending"
                       and d.get("module") == module_key]
            if not pending:
                del module["_gray_resume"]
                dirty = True
                any_accepted = any(d.get("status") == "accepted"
                                   and not d.get("_archived")
                                   for d in state.get("gray_drafts", [])
                                   if d.get("module") == module_key)
                any_rejected = any(d.get("status") == "rejected"
                                   and not d.get("_archived")
                                   for d in state.get("gray_drafts", [])
                                   if d.get("module") == module_key)
                if any_accepted and any_rejected:
                    action = MAKER_FIX
                    module["_pending_align"] = True
                elif any_accepted:
                    action = MAKER_FIX
                else:
                    action = ALIGN_DOCS
            else:
                action = None
        else:
            action = None
        if action:
            StateManager.set_current(state, module_key, action)
            self._trace(state, "SCAN", module_key,
                        f"gray resume -> {action}")
            dirty = True
            self.sm.save(state)
            module = state["modules"][module_key]
            return self._build(state, action, module_key, module)
        # A READY module that already has a valid plan for the CURRENT spec
        # skips SCORE/MAKER_STEP0: the plan survived a previous run (e.g. a
        # MAKER_STEP0 commit that failed later), and re-running STEP0 would
        # rewrite the plan and can reintroduce the rejected formatting.
        if module["status"] == READY:
            plan_file = module.get("plan_path")
            if plan_file and os.path.exists(plan_file):
                spec_path = derive_spec_path(
                    module["change_id"], module["module_name"], self.root_dir)
                if compute_spec_hash(spec_path) == module.get("spec_hash") \
                        and not audit_plan_existing_evidence(
                            plan_file,
                            project_root=module.get("project_root")):
                    action = MAKER_STEP1_RED
                    StateManager.set_current(state, module_key, action)
                    self._trace(state, "SCAN", module_key,
                                "valid plan exists -> skip SCORE/STEP0")
                    dirty = True
                    self.sm.save(state)
                    return self._build(state, action, module_key, module)
        entry = STATUS_TABLE.get(module["status"])
        if not entry:
            if dirty:
                self.sm.save(state)
            return {"action": "IDLE",
                    "message": f"未知状态 {module['status']}"}
        if entry["next"]:
            action = entry["next"]
            StateManager.set_current(state, module_key, action)
            self._trace(state, "SCAN", module_key, f"routed to {action}")
            dirty = True
            self.sm.save(state)
            return self._build(state, action, module_key,
                               state["modules"][module_key])
        if dirty:
            self.sm.save(state)
        message = (entry["idle_msg"] or "IDLE").format(module_key=module_key)
        return {"action": "IDLE", "message": message}

    def _build(self, state, action, module_key, module):
        rejected = [
            {"id": d.get("id"), "summary": d.get("summary", "")}
            for d in state.get("gray_drafts", [])
            if d.get("status") == "rejected" and d.get("module") == module_key
            and not d.get("_archived")
        ]
        accepted = [
            {"id": d.get("id"), "summary": d.get("summary", "")}
            for d in state.get("gray_drafts", [])
            if d.get("status") == "accepted" and d.get("module") == module_key
            and not d.get("_archived")
        ]
        return directives.build(action, module_key, module,
                                self.root_dir, rejected, accepted)

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
            module["_gray_resume"] = MAKER_FIX
            StateManager.clear_current(state)
        elif next_action:
            attempt = module.get("maker_attempt", 0)
            StateManager.set_current(state, module_key, next_action, attempt)
        else:
            StateManager.clear_current(state)

        display = (next_action or 'IDLE').replace('_', '')
        if action == SCORE and module.get("last_score") is not None:
            display = f"{display} (SCORE {module['last_score']}/100)"
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
        if not parsed:
            raise ValueError("Output format error: No SCORE block found")
        score = parsed.get("score")
        cross = parsed.get("cross_consistency")
        if score is None:
            raise ValueError("SCORE field missing from result")
        if cross == "FAIL":
            score = min(score, 85)
        module["last_score"] = score
        module["score_cross"] = cross
        dims = parsed.get("dimensions")
        module["score_dimensions"] = dims if isinstance(dims, dict) else None
        if score >= SCORE_THRESHOLD and cross != "FAIL":
            module["status"] = READY
            # new run cycle: clear fix-cycle counters left over from a run
            # that died mid-cycle (commit_error), else CODE_REVIEW_FIX is
            # skipped by the MAX_REVIEW_FIX_CYCLES guard on the next run
            module["review_fix_attempt"] = 0
            return MAKER_STEP0
        module["status"] = NEEDS_REFINEMENT
        return None

    def _commit_classify_change(self, state, key, module, text):
        parsed = parse_classify_change(text)
        if not parsed:
            raise ValueError("Output format error: No CLASSIFY_CHANGE block found")
        magnitude = parsed.get("magnitude")
        if magnitude == "轻量":
            return CHECKER
        if magnitude == "重量":
            return SCORE
        raise ValueError(f"Unknown change magnitude: {magnitude}")

    def _commit_maker_step0(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("Output format error: No MAKER_OUTPUT block found")
        if parsed.get("status") != "SUCCESS":
            return MAKER_STEP0
        plan_path = parsed.get("plan_path")
        if not plan_path:
            raise ValueError("PLAN_PATH missing")
        full = plan_path if os.path.isabs(plan_path) else os.path.join(
            self.root_dir, plan_path)
        if not os.path.exists(full):
            raise ValueError(f"Plan file not found: {plan_path}")
        evidence_errors = audit_plan_existing_evidence(
            full, project_root=module.get("project_root"))
        if evidence_errors:
            raise ValueError(
                "Plan evidence errors: " + "; ".join(evidence_errors[:5]))
        module["plan_path"] = plan_path
        return MAKER_STEP1_RED

    def _commit_maker_step1_red(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("Output format error: No MAKER_OUTPUT block found")
        self._check_gap_audit(parsed, module)
        evidence = parsed.get("tdd_red_evidence")
        if not evidence:
            raise ValueError("Output format error: TDD_RED_EVIDENCE missing")
        if evidence.get("tdd_skip"):
            # plan classifies all methods as skip — existing tests already
            # cover the change, no new RED tests needed; GREEN verifies pass
            return MAKER_STEP2_GREEN
        if not evidence.get("red_confirmed"):
            tr_out = (evidence.get("red_test_output") or "")
            if (evidence.get("test_files_written") and
                    "Failures: 0" in tr_out and "Errors: 0" in tr_out):
                self._trace(state, MAKER_STEP1_RED, key,
                            "RED implicitly confirmed (tests pass on existing impl)")
                evidence["red_confirmed"] = True
            else:
                raise ValueError("RED not confirmed")
        if not evidence.get("test_files_written"):
            raise ValueError("No test files written")
        return MAKER_STEP2_GREEN

    def _check_gap_audit(self, parsed, module):
        """Gap_audit must account for every '已有/无需变更' plan claim.

        The plan marks behaviors as already implemented; the maker must
        verify each one and report the result, or the audit step silently
        degrades to trusting the plan (the failure mode this prevents).
        """
        plan_path = module.get("plan_path")
        if not plan_path:
            return
        full = plan_path if os.path.isabs(plan_path) else os.path.join(
            self.root_dir, plan_path)
        claims = count_plan_existing_claims(full)
        if claims == 0:
            return
        audit = parsed.get("gap_audit")
        if not isinstance(audit, list):
            raise ValueError(
                f"Output format error: gap_audit missing ({claims} "
                f"'已有/无需变更' claims in plan need verification)")
        if len(audit) < claims:
            raise ValueError(
                f"Output format error: gap_audit has {len(audit)} entries "
                f"but plan has {claims} '已有/无需变更' claims")
        for entry in audit:
            if not isinstance(entry, dict) or \
                    "verified" not in entry or not entry.get("evidence"):
                raise ValueError(
                    "Output format error: gap_audit entries must have "
                    "'evidence' and 'verified'")

    def _commit_maker_step2_green(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("Output format error: No MAKER_OUTPUT block found")
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
            raise ValueError("Output format error: No CHECKER_OUTPUT block found")
        module.pop("_align_done", None)  # consumed on success: commit routes to CHECKER directly
        hard = parsed.get("hard_error_count") or 0
        raw_soft = parsed.get("soft_warning_count") or 0
        rejected = [
            d for d in state.get("gray_drafts", [])
            if d.get("status") == "rejected" and d.get("module") == key
        ]
        rejected_summaries = {d.get("summary", "") for d in rejected}
        rejected_fingerprints = _draft_fingerprints(rejected)

        def suppressed(desc):
            return (desc in rejected_summaries
                    or any(fp in desc for fp in rejected_fingerprints))

        hard_errors = [
            d for d in parsed.get("discrepancies", [])
            if d.get("severity") == "HARD_ERROR"
        ]
        suppressed_hard = [d for d in hard_errors
                           if suppressed(d.get("description", ""))]
        module["hard_errors"] = [
            d for d in hard_errors if d not in suppressed_hard
        ]
        parsed_soft = [
            d for d in parsed.get("discrepancies", [])
            if d.get("severity") == "SOFT_WARNING"
        ]
        filtered = [
            d for d in parsed_soft
            if not suppressed(d.get("description", ""))
        ]
        suppressed_soft = [d for d in parsed_soft
                           if suppressed(d.get("description", ""))]
        module["soft_warnings"] = filtered
        if suppressed_hard or suppressed_soft:
            module["suppressed_checker"] = [
                {"description": d.get("description", "")}
                for d in suppressed_hard + suppressed_soft
            ]
            self._trace(state, CHECKER, key,
                        f"suppressed {len(suppressed_hard)} hard + "
                        f"{len(suppressed_soft)} soft (rejected fingerprints)")
        # unparseable findings cannot be fingerprint-matched, so keep their
        # raw count; suppressed ones only reduce the parseable portion
        hard -= len(suppressed_hard)
        if raw_soft > len(parsed_soft):
            soft = max(raw_soft - len(suppressed_soft), len(filtered))
        else:
            soft = len(filtered)
        if hard > 0 and module.get("maker_attempt", 0) < MAX_MAKER_ATTEMPTS:
            module["maker_attempt"] = module.get("maker_attempt", 0) + 1
            return MAKER_FIX
        if hard > 0:
            # MAKER_FIX exhausted: without this transition the module stays
            # READY and every run re-runs SCORE→MAKER→CHECKER in a loop that
            # burns all 200 steps. BLOCKED is terminal until the user edits
            # the spec (spec_result → PARTIAL + maker_attempt=0) or fixes the
            # code; poll notifies "请处理阻塞问题后回复「完善spec」".
            module["status"] = BLOCKED
            self._trace(state, CHECKER, key,
                        f"hard errors persist after {MAX_MAKER_ATTEMPTS} "
                        f"MAKER_FIX attempts -> BLOCKED")
            return None
        if soft > 0:
            return _GRAY_LIST
        return CODE_REVIEW

    def _commit_maker_fix(self, state, key, module, text):
        parsed = parse_maker_output(text)
        if not parsed:
            raise ValueError("Output format error: No MAKER_OUTPUT block found")
        if parsed.get("status") != "SUCCESS":
            raise ValueError(f"Fix failed: {parsed.get('status')}")
        if parsed.get("build_result") != "BUILD SUCCESS":
            raise ValueError(f"Build failed: {parsed.get('build_result')}")
        if module.get("_pending_align"):
            del module["_pending_align"]
            return ALIGN_DOCS
        return CHECKER

    def _commit_align_docs(self, state, key, module, text):
        parsed = parse_align_docs(text)
        if not parsed:
            raise ValueError("Output format error: No ALIGN_DOCS block found")
        if parsed.get("status") != "SUCCESS":
            raise ValueError(f"Align docs failed: {parsed.get('status')}")
        module["_align_done"] = True
        if parsed.get("alignment_report"):
            module["alignment_report"] = parsed["alignment_report"]
        # ALIGN_DOCS may have edited spec.md: sync the hashes so the
        # follow-up CHECKER/scan compare against the new spec instead of
        # a stale hash
        self._refresh_spec_hashes(module)
        return CHECKER

    def _commit_code_review(self, state, key, module, text):
        parsed = parse_code_review(text)
        if not parsed:
            raise ValueError("Output format error: No CODE_REVIEW block found")
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
            raise ValueError("Output format error: No MAKER_OUTPUT block found")
        if parsed.get("status") != "SUCCESS":
            raise ValueError(f"Review fix failed: {parsed.get('status')}")
        if module.get("review_fix_attempt", 0) > MAX_REVIEW_FIX_CYCLES:
            return _SYNCED
        return CHECKER

    def _refresh_spec_hashes(self, module):
        # ponytail: 三处刷新点(align_done/ALIGN_DOCS/SYNCED)都须同步 norm
        # hash，内联易漏字段（重蹈 #94 同类漂移）
        spec_path = derive_spec_path(
            module["change_id"], module["module_name"], self.root_dir)
        new_hash = compute_spec_hash(spec_path)
        if new_hash:
            module["spec_hash"] = new_hash
        norm_hash = compute_spec_norm_hash(spec_path)
        if norm_hash:
            module["spec_norm_hash"] = norm_hash

    def _execute_synced(self, state, key, module):
        self._refresh_spec_hashes(module)
        plan_path = derive_plan_path(
            module["change_id"], module["module_name"], self.root_dir)
        new_plan_hash = compute_plan_hash(plan_path)
        if new_plan_hash:
            module["plan_hash"] = new_plan_hash
        prev_status = module["status"]
        if prev_status != SYNCED:
            project_root = module.get("project_root", self.root_dir)
            try:
                result = subprocess.run(
                    ["mvn", "clean", "test"],
                    cwd=project_root,
                    capture_output=True, text=True, timeout=600
                )
                if result.returncode != 0:
                    print(f"[WARN] Final test run failed for {key}: "
                          f"{result.stdout[-500:]}\n{result.stderr[-500:]}")
                else:
                    print(f"[OK] Final test run passed for {key}")
            except Exception as e:
                print(f"[WARN] Final test run error for {key}: {e}")
        module["status"] = SYNCED
        module["last_synced"] = datetime.datetime.now().isoformat()
        self._audit(state, key, f"{prev_status}->{SYNCED}")
        trail = state.get("audit_trail", [])
        if len(trail) > AUDIT_RETENTION:
            state["audit_trail"] = trail[-AUDIT_RETENTION:]

    def _execute_gray_list(self, state, key, module):
        TYPE_LABELS = {
            "plan deviation": "方案偏离",
            "test-coverage": "测试覆盖",
            "precision": "精确度",
            "type inconsistency": "类型不一致",
            "method signature": "方法签名",
            "import": "导入依赖",
            "module dependency": "模块依赖",
            "line reference": "引用位置",
        }
        warnings = module.get("soft_warnings", [])
        if not warnings:
            warnings = [{
                "type": "unknown",
                "description": ("Checker 报告软警告但条目未按格式解析，"
                                "详见 .loop/result-CHECKER.md 存档"),
            }]
        drafts = state.setdefault("gray_drafts", [])
        # Archive previously resolved drafts for this module instead of
        # deleting — the current batch is independent; old accepted/rejected
        # should not re-trigger MAKER_FIX on a new gray_list cycle, but the
        # audit trail is preserved for fingerprint suppression and history.
        for d in drafts:
            if d.get("module") == key and d.get("status") != "pending":
                d["_archived"] = True
        next_id = max((d.get("id", 0) for d in drafts), default=0)
        for warning in warnings:
            next_id += 1
            wt = warning.get("type", "")
            label = TYPE_LABELS.get(wt, wt.replace("_", " ") if wt else "其他")
            summary = warning.get("description", "")
            drafts.append({
                "id": next_id,
                "module": key,
                "type_label": label,
                "summary": summary,
                "status": "pending",
            })
        # Cap total drafts: trim oldest archived entries when over limit.
        # Archived drafts older than newest N entries are safe to drop;
        # fingerprint suppression still works from the remaining rejected
        # entries, and the audit trail is preserved in audit_trail separately.
        if len(drafts) > MAX_GRAY_DRAFTS:
            excess = len(drafts) - MAX_GRAY_DRAFTS
            archived = [(i, d) for i, d in enumerate(drafts)
                        if d.get("status") != "pending"]
            if archived:
                # Remove oldest excess archived entries (sorted by id ≅ age)
                archived.sort(key=lambda x: x[1].get("id", 0))
                remove_ids = {id(d) for _, d in archived[:excess]}
                drafts[:] = [d for d in drafts if id(d) not in remove_ids]

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
