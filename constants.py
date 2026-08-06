"""Constants: state names, thresholds, delimiters, path templates."""

# Module statuses (macro phase of a module's lifecycle)
SYNCED = "SYNCED"
PARTIAL = "PARTIAL"
READY = "READY"
NEEDS_REFINEMENT = "NEEDS_REFINEMENT"
BLOCKED = "BLOCKED"
DRAFT = "DRAFT"

ALL_STATUSES = (SYNCED, PARTIAL, READY, NEEDS_REFINEMENT, BLOCKED, DRAFT)

# Priority order for module selection (highest first)
PRIORITY_ORDER = (PARTIAL, READY, NEEDS_REFINEMENT, BLOCKED, DRAFT, SYNCED)

# Agent actions (specific steps the LLM executes)
SCORE = "SCORE"
CLASSIFY_CHANGE = "CLASSIFY_CHANGE"
MAKER_STEP0 = "MAKER_STEP0"
MAKER_STEP1_RED = "MAKER_STEP1_RED"
MAKER_STEP2_GREEN = "MAKER_STEP2_GREEN"
CHECKER = "CHECKER"
MAKER_FIX = "MAKER_FIX"
CODE_REVIEW = "CODE_REVIEW"
CODE_REVIEW_FIX = "CODE_REVIEW_FIX"

ALL_ACTIONS = (
    SCORE, CLASSIFY_CHANGE, MAKER_STEP0, MAKER_STEP1_RED,
    MAKER_STEP2_GREEN, CHECKER, MAKER_FIX, CODE_REVIEW, CODE_REVIEW_FIX,
)

# Thresholds
SCORE_THRESHOLD = 90
MAX_MAKER_ATTEMPTS = 3
MAX_REVIEW_FIX_CYCLES = 1

# Retention limits
TRACE_RETENTION = 20
AUDIT_RETENTION = 30

# Output block delimiters
MAKER_OUTPUT_START = "---MAKER_OUTPUT---"
MAKER_OUTPUT_END = "---END_MAKER_OUTPUT---"
CHECKER_OUTPUT_START = "---CHECKER_OUTPUT---"
CHECKER_OUTPUT_END = "---END_CHECKER_OUTPUT---"

# File names (relative to root dir)
STATE_FILE = ".loop/state.json"
RESULT_FILE = ".loop/result.md"
CONTEXT_FILE = ".loop/context.json"

# Path templates (relative to root dir)
SPEC_PATH_TEMPLATE = "openspec/changes/{change_id}/specs/{module_name}/spec.md"
PLAN_PATH_TEMPLATE = "openspec/changes/{change_id}/plans/{module_name}-plan.md"
REPORT_PATH_TEMPLATE = "openspec/changes/{change_id}/LOOP_REPORT.md"

# Glob pattern for spec discovery
SPEC_GLOB = "openspec/changes/*/specs/*/spec.md"
