"""DirectiveBuilder: generate self-contained directives JSON per action."""

import json
import os

from constants import (
    SCORE, CLASSIFY_CHANGE, MAKER_STEP0, MAKER_STEP1_RED,
    MAKER_STEP2_GREEN, CHECKER, MAKER_FIX, CODE_REVIEW, CODE_REVIEW_FIX,
    ALIGN_DOCS,
)
from spec_utils import (
    derive_spec_path, derive_plan_path, read_test_command,
    read_checker_test_command,
)


def build(action, module_key, module, root_dir=".", rejected_drafts=None,
          accepted_drafts=None):
    change_id = module["change_id"]
    module_name = module["module_name"]
    spec_path = derive_spec_path(change_id, module_name, root_dir)
    plan_path = derive_plan_path(change_id, module_name, root_dir)
    project_root = module.get("project_root", ".")
    test_cmd = read_test_command(project_root)
    spec_hash = module.get("spec_hash", "")
    attempt = module.get("maker_attempt", 0)

    base = {
        "action": action,
        "module": module_key,
        "attempt": attempt,
        "project_root": project_root,
        "directives": {
            "spec_path": spec_path,
            "context": {"spec_hash": spec_hash, "project_root": project_root},
        },
    }
    d = base["directives"]

    if action == SCORE:
        d["instructions"] = (
            "Read the spec file. Score it on 5 dimensions:\n"
            "1. Scenario coverage (>=5 well-formed Scenarios = strong; 0 = disqualifying)\n"
            "2. Field definition completeness (field tables with non-empty type/source/constraint)\n"
            "3. API contract completeness (HTTP method, request body, response defined)\n"
            "4. Exception scenario coverage (~20%+ of Scenarios are failures/errors)\n"
            "5. Unambiguous markers (count TBD/待定/TODO/待确认 markers; @HumanDecision does NOT count)\n"
            "Hard floor: 0 Scenarios, no field table, or API missing HTTP method = cap <90.\n"
            "Then run cross-consistency gate:\n"
            "- Scenario steps → field table (orphan references = FAIL)\n"
            "- API request/response → field table (mismatched = FAIL)\n"
            "- Exception triggers → main flow preconditions (contradiction = FAIL)\n"
            "Any cross-ref failure caps score at 85."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"score": 87, "cross_consistency": "PASS|FAIL", '
            '"dimensions": {"scenario_coverage": "只 3 个场景，缺支付失败场景", '
            '"field_completeness": "ok", "api_contract": "ok", '
            '"exception_coverage": "ok", "ambiguity_markers": 3}}\n'
            "score: 0-100 integer. cross_consistency: PASS or FAIL "
            "(FAIL when any cross-ref check fails; score is then capped at 85).\n"
            "dimensions: for every dimension below strong, state the CONCRETE "
            "gap in Chinese (exactly which scenario/field/API/edge case is "
            "missing or contradicts); write \"ok\" when strong. "
            "ambiguity_markers: count of TBD/TODO/待定/待确认 markers."
        )

    elif action == CLASSIFY_CHANGE:
        old_hash = module.get("prev_spec_hash", "")
        d["instructions"] = (
            "Read the spec file. Compare with the previous version (hash changed).\n"
            "Classify the change magnitude:\n"
            "- 轻量 (lightweight): typo, constraint tweak, field rename, enum value add, comment/format\n"
            "- 重量 (heavy): new Scenario/field/API, business logic change, interface call-mode change"
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"magnitude": "轻量", "reason": "brief explanation"}\n'
            "magnitude must be exactly 轻量 or 重量."
        )
        d["context"]["old_hash"] = old_hash

    elif action == MAKER_STEP0:
        d["plan_path"] = plan_path
        d["instructions"] = (
            "Planning Mode. Read the spec file and AGENTS.md.\n"
            "Scope: ONLY the current spec change. Compare the spec with the "
            "previous version to identify what THIS change requires "
            "(find the newest backup file .loop/backup/spec-<module>-*.md, "
            "or use prev_spec_hash for reference). The plan must include "
            "ONLY tasks required by this change.\n"
            "Pre-existing spec↔code deviations NOT introduced by this change "
            "are OUT OF SCOPE: do NOT list them as tasks, do NOT plan fixes "
            "for them — the CHECKER detects them independently.\n"
            "CRITICAL — project root: all file paths in the plan MUST use "
            "the project_root from the context field below as the base "
            "directory. Do NOT guess or hardcode paths from your training "
            "data. If a worktree directory exists at "
            "<root_dir>/<project_name> (where root_dir is the requirement "
            "root, e.g. /Users/chuan.li/loop-work/cross-dock), use the "
            "worktree path; otherwise use project_root as-is.\n"
            "Generate an implementation plan at the plan_path. The plan must include:\n"
            "- File list: each file to create/modify with its absolute path under project_root\n"
            "- Class responsibilities: one sentence per class\n"
            "- Data flow: Controller → Service → DAO → DB\n"
            "- TDD classification table: each method marked 'must TDD' or 'skip' with reason\n"
            "- File ownership matrix: which files belong to which task, no overlap\n"
            "- Test strategy: what each test class covers\n"
            "EVIDENCE RULE — every '已有/无需变更' claim (existing behavior "
            "you are NOT changing) SHOULD cite code evidence as "
            "`FilePath.java:line` (e.g. `PurchasePlanServiceImpl.java:810`) "
            "on the SAME line, so the STEP1 audit can verify it. Verify each "
            "claim against the real code with grep/read BEFORE writing it. "
            "Spec line citations (spec.md:NN) do NOT count as evidence.\n"
            "EVIDENCE PATH FORM — the cited FilePath is resolved against the "
            "project_root in the context below. Bare file names like "
            "`Foo.java:10` FAIL when the file is not directly under "
            "project_root (multi-module repos). Use an absolute path "
            "(`/Users/.../module/src/main/java/x/Foo.java:10`) or a path "
            "relative to project_root that includes the module directory "
            "(`zkh-opc-sna-manager/src/main/java/x/Foo.java:10`). Never cite "
            "a file that does not exist on disk.\n"
            "EVIDENCE WORDING — do NOT use '已有' or '无需变更' as section "
            "headers or lead-in phrases (e.g. a bare line "
            "'> 已有/无需变更（附证据）：' is ambiguous). Only use the words "
            "on concrete item lines, each carrying its file:line reference.\n"
            "Do NOT write any test or implementation code."
        )
        d["context"]["prev_spec_hash"] = module.get("prev_spec_hash", "")
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"status": "SUCCESS", "plan_path": "' + plan_path + '"}\n'
            "status: SUCCESS or FAILED. plan_path: absolute path of the plan file written."
        )

    elif action == MAKER_STEP1_RED:
        d["plan_path"] = module.get("plan_path") or plan_path
        d["instructions"] = (
            "TDD RED Mode. Read the spec file and the plan file.\n"
            "AUDIT STEP — before writing tests, find every '已有/无需变更' "
            "claim in the plan, read the cited file:line evidence if "
            "present, and verify the code actually implements the claimed "
            "behavior. If a claim has no citation or its evidence does NOT "
            "hold (file/line wrong or behavior absent), locate the relevant "
            "code yourself with grep/read and treat the claim as a GAP: "
            "include it in this change's test coverage and list it in "
            "gap_audit with verified: false. Do not silently trust the "
            "plan's claims.\n"
            "Write test classes for all 'must TDD' methods from the plan's classification table.\n"
            f"Run '{test_cmd}'. Tests MUST fail with assertion failures (not compile errors).\n"
            "Do NOT write any implementation code (src/main).\n"
            "If the plan's classification table has NO 'must TDD' methods (all entries are "
            "'skip' because tests already exist), do NOT write new tests: run the existing "
            "tests, confirm they pass, and declare 'tdd_skip: true' in TDD_RED_EVIDENCE."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"status": "SUCCESS", "plan_path": "' + (module.get('plan_path') or plan_path) + '",\n'
            ' "tdd_red_evidence": {"test_files_written": ["/abs/path/Test.java"],\n'
            '  "red_test_output": "Tests run: 5, Failures: 5 ...",\n'
            '  "red_confirmed": true, "tdd_skip": false},\n'
            ' "gap_audit": [{"plan_item": "C1", "evidence": "PurchasePlanServiceImpl.java:810",\n'
            '   "verified": true, "note": "brief"}]}\n'
            "gap_audit: one entry per '已有/无需变更' claim in the plan "
            "(same count as claims; empty array only when the plan has no "
            "claims). evidence: the plan's file:line citation. verified: "
            "true when the code really implements it, false when the claim "
            "does NOT hold and you are converting it into a GAP.\n"
            "tdd_skip: true ONLY when the plan's classification table has no "
            "'must TDD' methods (then red_confirmed: true, test_files_written: [])."
        )

    elif action == MAKER_STEP2_GREEN:
        d["plan_path"] = module.get("plan_path") or plan_path
        d["instructions"] = (
            "TDD GREEN Mode. Read the spec, plan, and test files from Step 1.\n"
            "Write implementation code. Do NOT modify test assertions.\n"
            f"Run '{test_cmd}'. All tests must pass.\n"
            "Then run 'mvn clean compile' at the project root to verify the full project compiles.\n"
            "Never run 'mvn compile' or 'mvn test' without clean first.\n"
            "Max 3 retries on failure (fix impl, not tests)."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"status": "SUCCESS", "plan_path": "' + (module.get('plan_path') or plan_path) + '",\n'
            ' "files_created": ["/abs/path/Foo.java"],\n'
            ' "files_modified": ["/abs/path/Bar.java"],\n'
            ' "test_results": {"class_name": "FooTest", "total": 7, '
            '"passed": 7, "failed": 0, "errors": 0},\n'
            ' "blockers": "none", "human_decisions": 0}\n'
            "status: SUCCESS, PARTIAL, or FAILED. files_created/files_modified: "
            "absolute paths. blockers: 'none' or a description. "
            "human_decisions: count of decisions that need human input."
        )

    elif action == CHECKER:
        d["plan_path"] = module.get("plan_path") or plan_path
        changed_files = (module.get("files_created", [])
                         + module.get("files_modified", []))
        checker_cmd = read_checker_test_command(test_cmd, root_dir, changed_files)
        d["instructions"] = (
            "Verify spec-plan-code three-way consistency.\n"
            "Read the spec file, plan file, and the changed files listed in "
            "context (files_created / files_modified).\n"
            f"Run '{checker_cmd}' to get baseline test results.\n"
            "Check: field existence, method signatures, line references, module dependencies,\n"
            "type consistency, import completeness, task status, cross-plan dependencies.\n"
            "Classify each discrepancy as HARD_ERROR (compile/logic failure),\n"
            "SOFT_WARNING (logic error/description inaccurate), or INFO (precision suggestion).\n"
            "Do NOT modify any files.\n"
            "IMPORTANT: All descriptions MUST be written in Chinese."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"status": "INCONSISTENT",\n'
            ' "discrepancy_count": 3, "hard_error_count": 1, '
            '"soft_warning_count": 1, "info_count": 1,\n'
            ' "discrepancies": [\n'
            '  {"severity": "HARD_ERROR", "type": "test-coverage", '
            '"description": "src/Foo.java:42 ..."},\n'
            '  {"severity": "SOFT_WARNING", "type": "plan deviation", '
            '"description": "..."},\n'
            '  {"severity": "INFO", "type": "precision", '
            '"description": "..."}\n'
            ' ],\n'
            ' "test_results": {"class_name": "FooTest", "total": 7, '
            '"passed": 7, "failed": 0, "errors": 0},\n'
            ' "coverage": {"tested": 5, "total": 6}}\n'
            "Counts must exactly match the discrepancies array: "
            "discrepancy_count = array length, hard_error_count = number of "
            "HARD_ERROR items, etc. severity must be exactly one of "
            "HARD_ERROR, SOFT_WARNING, INFO. description must include file:line."
        )
        d["context"]["files_created"] = module.get("files_created", [])
        d["context"]["files_modified"] = module.get("files_modified", [])
        d["context"]["test_command"] = checker_cmd

    elif action == MAKER_FIX:
        d["plan_path"] = module.get("plan_path") or plan_path
        hard_errors = module.get("hard_errors", [])
        accepted_lines = "\n".join(
            f"- [{a.get('id')}] {a.get('summary', '')}" for a in (accepted_drafts or [])
        ) if accepted_drafts else ""
        if hard_errors and accepted_lines:
            fix_items = (
                "Fix these discrepancies:\n"
                "HARD_ERROR (spec says code is wrong):\n"
                + "\n".join(f"  [{e.get('type','?')}] {e.get('description','')}"
                           for e in hard_errors)
                + "\nSOFT_WARNING (user accepted these gray-list items):\n"
                + accepted_lines
            )
        elif hard_errors:
            fix_items = (
                "Fix these HARD_ERROR discrepancies:\n"
                + "\n".join(f"  [{e.get('type','?')}] {e.get('description','')}"
                           for e in hard_errors)
            )
        elif accepted_lines:
            fix_items = (
                "Fix these gray-list items the user accepted:\n" + accepted_lines
            )
        else:
            fix_items = "No specific discrepancies reported."
        d["instructions"] = (
            "Fix Mode.\n"
            "Do NOT redesign, add features, or modify spec files.\n"
            "If fix requires test changes: mini TDD cycle (RED first, then GREEN).\n"
            f"{fix_items}\n"
            "Run 'mvn compile -DskipTests' to verify compilation."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"status": "SUCCESS",\n'
            ' "fixed_items": ["fixed item description"],\n'
            ' "remaining_items": ["unfixed item description"],\n'
            ' "build_result": "BUILD SUCCESS"}\n'
            "status: SUCCESS or FAILED."
        )
        d["context"]["hard_errors"] = hard_errors
        d["context"]["accepted_warnings"] = (accepted_drafts or [])
        d["context"]["test_command"] = test_cmd

    elif action == CODE_REVIEW:
        d["plan_path"] = module.get("plan_path") or plan_path
        d["instructions"] = (
            "Review the code changes for production readiness.\n"
            "Check: logic bugs, security, architecture, test soundness, spec-Scenario coverage.\n"
            "Classify each issue as Critical, Important, or Minor.\n"
            "Do NOT modify any files."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"issues": [\n'
            '  {"severity": "critical", '
            '"text": "src/Foo.java:42 — description"},\n'
            '  {"severity": "important", "text": "..."}\n'
            ' ]}\n'
            "severity must be exactly one of critical, important, minor. "
            "Empty array (\"issues\": []) when no issues found."
        )
        d["context"]["files_created"] = module.get("files_created", [])
        d["context"]["files_modified"] = module.get("files_modified", [])

    elif action == CODE_REVIEW_FIX:
        d["plan_path"] = module.get("plan_path") or plan_path
        review_issues = module.get("review_issues", [])
        d["instructions"] = (
            "Fix Mode. Fix the reported Critical and Important review issues.\n"
            "Do NOT add new features or modify spec files.\n"
            "Run 'mvn compile -DskipTests' to verify compilation."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"status": "SUCCESS",\n'
            ' "fixed_items": ["fixed item description"],\n'
            ' "remaining_items": ["unfixed item description"],\n'
            ' "build_result": "BUILD SUCCESS"}\n'
            "status: SUCCESS or FAILED."
        )
        d["context"]["review_issues"] = review_issues
        d["context"]["test_command"] = test_cmd

    elif action == ALIGN_DOCS:
        d["plan_path"] = module.get("plan_path") or plan_path
        changed_files = (module.get("files_created", [])
                         + module.get("files_modified", []))
        rejected = rejected_drafts or []
        rejected_lines = "\n".join(
            f"- [{r.get('id')}] {r.get('summary', '')}" for r in rejected
        )
        d["instructions"] = (
            "The user rejected checker warnings — the code is correct, "
            "but spec/plan documents are outdated.\n"
            "Read the code files listed below (files_created / files_modified). "
            "Update spec.md and plan.md to accurately reflect the actual "
            "implementation.\n"
            "Rejected warnings you MUST resolve by aligning spec.md to the code "
            "(every rejected warning = the spec text disagrees with the code, "
            "and the code wins):\n"
            f"{rejected_lines or '(none listed)'}\n"
            "Rules:\n"
            "- Do NOT modify any code files.\n"
            "- For EACH rejected warning above, change the spec.md text it "
            "points at so it matches the code EXACTLY (e.g. if the warning "
            "says spec requires 15000 but code uses 2000, spec must become "
            "2000). Do NOT keep spec text that still contradicts the code.\n"
            "- Update spec Scenarios, API contracts, field tables to match code.\n"
            "- Update plan TDD table, file ownership, data flow to match code.\n"
            "- IMPORTANT: All descriptions MUST be written in Chinese.\n"
            "- Keep the original structure and level of detail."
        )
        d["output_format"] = (
            "Write to .loop/result.md ONLY a single JSON object — no markdown, "
            "no code fences, no commentary:\n"
            '{"status": "SUCCESS",\n'
            ' "updated_files": ["openspec/changes/.../spec.md", '
            '"openspec/changes/.../plan.md"],\n'
            ' "alignment_report": [{"id": 8, "aligned": true, '
            '"note": "spec L111 changed 15000 -> 2000"}]}\n'
            "status: SUCCESS or FAILED. "
            "updated_files: relative paths of spec and plan files updated. "
            "alignment_report: one entry per rejected warning id; "
            "aligned=false only when the warning was not applicable "
            "and explain why in note."
        )
        d["context"]["files_created"] = module.get("files_created", [])
        d["context"]["files_modified"] = module.get("files_modified", [])
        d["context"]["rejected_drafts"] = rejected

    # Machine-local environment context (databases, nacos, gateways), gitignored.
    ctx_path = os.path.join(root_dir, ".loop", "context.json")
    if os.path.exists(ctx_path):
        try:
            with open(ctx_path) as f:
                d["context"]["environment"] = json.load(f)
        except (OSError, ValueError) as e:
            d["context"]["environment_error"] = f"invalid context.json: {e}"

    return base
