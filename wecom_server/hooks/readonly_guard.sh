#!/bin/bash
# PreToolUse hook injected only for read-only conversations (aibot group
# chats). Blocks any mutation of loop_engine state/repos and all file edits,
# so "group chat is read-only" is enforced at the tool boundary instead of
# relying on the model to follow the prompt.
input=$(cat)

DATA_DIR="${LOOP_ENGINE_DATA_DIR:-$HOME/.qoder/loop_engine}"
AUDIT_LOG="${AUDIT_LOG:-$DATA_DIR/audit.log}"

blocked=$(printf '%s' "$input" | /usr/bin/python3 -c '
import json, re, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = d.get("tool_name", "")
ti = d.get("tool_input", {}) or {}

if tool in ("Edit", "Write", "NotebookEdit"):
    target = ti.get("file_path") or ti.get("notebook_path") or ""
    print(tool + "\t" + target)
    sys.exit(0)

if tool != "Bash":
    sys.exit(0)

cmd = ti.get("command", "") or ""
patterns = [
    r"\bloop_engine\s+(close|prune-worktrees|approve|run|next|reset|set-status|"
    r"add-blocker|remove-blocker|resolve-draft|requirement-add|requirement-remove|"
    r"requirement-rename|scope-audit)\b",
    r"__main__\.py\s+(close|prune-worktrees|approve|run|next|reset|set-status|"
    r"add-blocker|remove-blocker|resolve-draft|requirement-add|requirement-remove|"
    r"requirement-rename)\b",
    r"\b(import|from)\s+(scheduler|machine|state)\b",
    r"\bscheduler\.(approve|dispatch|run|poll)\b",
    r"\bgit\s+(push|commit|merge|rebase|reset|checkout|restore|clean)\b",
    r"\bgh\s+pr\b",
    r"\bglab\s+mr\b",
    r"\b(sed\s+-i|perl\s+-i|tee\b|rm\s+-|mv\s+|cp\s+)",
    # file redirection, but allow 2>&1 and friends
    r"(?<![0-9])>{1,2}(?![&])",
]
for p in patterns:
    if re.search(p, cmd):
        print("Bash\t" + " ".join(cmd.split())[:300])
        sys.exit(0)

if "dgate exec" in cmd and re.search(
        r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE)\b", cmd, re.I):
    print("Bash\t" + " ".join(cmd.split())[:300])
    sys.exit(0)
')

[ -z "$blocked" ] && exit 0

ts=$(date '+%Y-%m-%dT%H:%M:%S')
flat=$(printf '%s' "$blocked" | tr '\n' ' ')
echo "[$ts] BLOCKED readonly session tool=$flat" >> "$AUDIT_LOG"
printf '已阻止：群聊只读模式不允许执行该操作（%s）。请到单聊中操作。' "${blocked//$'\t'/ }"
exit 1
