"""DirectiveBuilder: generate self-contained directives JSON per action."""

import json
import os

from constants import (
    SCORE, CLASSIFY_CHANGE, MAKER_STEP0, MAKER_STEP1_RED,
    MAKER_STEP2_GREEN, CHECKER, MAKER_FIX, CODE_REVIEW, CODE_REVIEW_FIX,
)
from spec_utils import (
    derive_spec_path, derive_plan_path, read_test_command,
)


def build(action, module_key, module, root_dir="."):
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
        "directives": {
            "spec_path": spec_path,
            "context": {"spec_hash": spec_hash},
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
            "Write to .loop/result.md:\n"
            "SCORE: {n}/100\n"
            "DIMENSIONS:\n"
            "  scenario_coverage: {assessment}\n"
            "  field_completeness: {assessment}\n"
            "  api_contract: {assessment}\n"
            "  exception_coverage: {assessment}\n"
            "  ambiguity_markers: {count}\n"
            "CROSS_CONSISTENCY: PASS|FAIL: {details if FAIL}"
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
            "Write to .loop/result.md:\n"
            "CHANGE_MAGNITUDE: 轻量|重量\n"
            "REASON: {brief explanation}"
        )
        d["context"]["old_hash"] = old_hash

    elif action == MAKER_STEP0:
        d["plan_path"] = plan_path
        d["instructions"] = (
            "Planning Mode. Read the spec file and AGENTS.md.\n"
            "Generate an implementation plan at the plan_path. The plan must include:\n"
            "- File list: each file to create/modify with its package path\n"
            "- Class responsibilities: one sentence per class\n"
            "- Data flow: Controller → Service → DAO → DB\n"
            "- TDD classification table: each method marked 'must TDD' or 'skip' with reason\n"
            "- File ownership matrix: which files belong to which task, no overlap\n"
            "- Test strategy: what each test class covers\n"
            "Do NOT write any test or implementation code."
        )
        d["output_format"] = (
            "Write MAKER_OUTPUT block to .loop/result.md:\n"
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS|FAILED\n"
            f"PLAN_PATH: {plan_path}\n"
            "---END_MAKER_OUTPUT---"
        )

    elif action == MAKER_STEP1_RED:
        d["plan_path"] = module.get("plan_path") or plan_path
        d["instructions"] = (
            "TDD RED Mode. Read the spec file and the plan file.\n"
            "Write test classes for all 'must TDD' methods from the plan's classification table.\n"
            f"Run '{test_cmd}'. Tests MUST fail with assertion failures (not compile errors).\n"
            "Do NOT write any implementation code (src/main).\n"
            "If the plan's classification table has NO 'must TDD' methods (all entries are "
            "'skip' because tests already exist), do NOT write new tests: run the existing "
            "tests, confirm they pass, and declare 'tdd_skip: true' in TDD_RED_EVIDENCE."
        )
        d["output_format"] = (
            "Write MAKER_OUTPUT block to .loop/result.md:\n"
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS|FAILED\n"
            "TDD_RED_EVIDENCE:\n"
            "  test_files_written:\n"
            "    - {absolute_path}\n"
            "  red_test_output: |\n"
            "    {mvn test output summary}\n"
            "  red_confirmed: true|false\n"
            "  tdd_skip: true|false  (true only when the plan has no 'must TDD' methods)\n"
            "---END_MAKER_OUTPUT---"
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
            "Write MAKER_OUTPUT block to .loop/result.md:\n"
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS|PARTIAL|FAILED\n"
            "FILES_CREATED:\n"
            "  - {absolute_path}\n"
            "FILES_MODIFIED:\n"
            "  - {absolute_path}\n"
            f"PLAN_PATH: {module.get('plan_path', plan_path)}\n"
            "TEST_RESULTS:\n"
            "  class: {test_class}\n"
            "  total: {n}\n"
            "  passed: {n}\n"
            "  failed: {n}\n"
            "BLOCKERS: none|{description}\n"
            "HUMAN_DECISIONS: {n}|none\n"
            "---END_MAKER_OUTPUT---"
        )

    elif action == CHECKER:
        d["plan_path"] = module.get("plan_path") or plan_path
        d["instructions"] = (
            "Verify spec-plan-code three-way consistency.\n"
            f"Read the spec file, plan file, and all code files.\n"
            f"Run '{test_cmd}' to get baseline test results.\n"
            "Check: field existence, method signatures, line references, module dependencies,\n"
            "type consistency, import completeness, task status, cross-plan dependencies.\n"
            "Classify each discrepancy as HARD_ERROR (compile/logic failure),\n"
            "SOFT_WARNING (logic error/description inaccurate), or INFO (precision suggestion).\n"
            "Do NOT modify any files."
        )
        d["output_format"] = (
            "Write CHECKER_OUTPUT block to .loop/result.md:\n"
            "---CHECKER_OUTPUT---\n"
            "STATUS: CONSISTENT|INCONSISTENT\n"
            "DISCREPANCY_COUNT: {n}\n"
            "HARD_ERROR_COUNT: {n}\n"
            "SOFT_WARNING_COUNT: {n}\n"
            "INFO_COUNT: {n}\n"
            "DISCREPANCIES:\n"
            "  1. [HARD_ERROR] [{type}] {description with file:line}\n"
            "  2. [SOFT_WARNING] [{type}] {description}\n"
            "  3. [INFO] [{type}] {description}\n"
            "TEST_RESULTS:\n"
            "  class: {test_class}\n"
            "  total: {n}\n"
            "  passed: {n}\n"
            "  failed: {n}\n"
            "  errors: {n}\n"
            "COVERAGE: {n}/{m} Scenarios have test methods\n"
            "---END_CHECKER_OUTPUT---"
        )
        d["context"]["files_created"] = module.get("files_created", [])
        d["context"]["files_modified"] = module.get("files_modified", [])
        d["context"]["test_command"] = test_cmd

    elif action == MAKER_FIX:
        d["plan_path"] = module.get("plan_path") or plan_path
        hard_errors = module.get("hard_errors", [])
        d["instructions"] = (
            "Fix Mode. Fix ONLY the reported HARD_ERROR discrepancies.\n"
            "Do NOT redesign, add features, or modify spec files.\n"
            "If fix requires test changes: mini TDD cycle (RED first, then GREEN).\n"
            f"Run '{test_cmd}'."
        )
        d["output_format"] = (
            "Write MAKER_OUTPUT block to .loop/result.md:\n"
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS|FAILED\n"
            "FIXED_ITEMS:\n"
            "  - {description of fixed item}\n"
            "REMAINING_ITEMS:\n"
            "  - {description of unfixed item}\n"
            "TEST_RESULTS:\n"
            "  class: {test_class}\n"
            "  total: {n}\n"
            "  passed: {n}\n"
            "  failed: {n}\n"
            "---END_MAKER_OUTPUT---"
        )
        d["context"]["hard_errors"] = hard_errors
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
            "Write your review to .loop/result.md.\n"
            "Format each issue as:\n"
            "[Critical|Important|Minor] {file}:{line} — {description}\n"
            "If no issues found, write: 'No Critical or Important issues found.'"
        )
        d["context"]["files_created"] = module.get("files_created", [])
        d["context"]["files_modified"] = module.get("files_modified", [])

    elif action == CODE_REVIEW_FIX:
        d["plan_path"] = module.get("plan_path") or plan_path
        review_issues = module.get("review_issues", [])
        d["instructions"] = (
            "Fix Mode. Fix the reported Critical and Important review issues.\n"
            "Do NOT add new features or modify spec files.\n"
            f"Run '{test_cmd}'."
        )
        d["output_format"] = (
            "Write MAKER_OUTPUT block to .loop/result.md:\n"
            "---MAKER_OUTPUT---\n"
            "STATUS: SUCCESS|FAILED\n"
            "FIXED_ITEMS:\n"
            "  - {description}\n"
            "REMAINING_ITEMS:\n"
            "  - {description}\n"
            "TEST_RESULTS:\n"
            "  class: {test_class}\n"
            "  total: {n}\n"
            "  passed: {n}\n"
            "  failed: {n}\n"
            "---END_MAKER_OUTPUT---"
        )
        d["context"]["review_issues"] = review_issues
        d["context"]["test_command"] = test_cmd

    # Machine-local environment context (databases, nacos, gateways), gitignored.
    ctx_path = os.path.join(root_dir, ".loop", "context.json")
    if os.path.exists(ctx_path):
        try:
            with open(ctx_path) as f:
                d["context"]["environment"] = json.load(f)
        except (OSError, ValueError) as e:
            d["context"]["environment_error"] = f"invalid context.json: {e}"

    return base
