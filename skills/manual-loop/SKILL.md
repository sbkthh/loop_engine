# Manual Loop Execution

When driving the loop manually (user says "主动执行" or asks to run next/commit step by step):

1. ALWAYS run `loop_engine manual-begin --root <path>` BEFORE the first `loop_engine next`
   - This acquires the same lock the scheduler uses, preventing concurrent access
   - If manual-begin fails (lock held), tell the user and do NOT proceed

2. After the loop finishes (machine reports IDLE/SYNCED) or user stops it:
   - Run `loop_engine manual-end --root <path>` IMMEDIATELY
   - Never leave without manual-end: it writes the run record and releases the lock