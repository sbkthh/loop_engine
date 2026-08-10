#!/bin/bash
# PreToolUse hook for WeCom-spawned qodercli sessions: log sensitive
# external-side-effect commands (git push, MR creation, destructive ops)
# to the loop_engine audit log. Never blocks — exit 0 always.
input=$(cat)

cmd=$(printf '%s' "$input" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("command", ""))
except Exception:
    print("")
')

if [ -z "$cmd" ]; then
    exit 0
fi

if printf '%s' "$cmd" | grep -qEi 'git push|--force|gh pr|glab mr|rm -rf|rm -fr|DROP TABLE|TRUNCATE TABLE'; then
    ts=$(date '+%Y-%m-%dT%H:%M:%S')
    sid=$(printf '%s' "$input" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("session_id", ""))
except Exception:
    print("")
')
    flat=$(printf '%s' "$cmd" | tr '\n' ' ')
    echo "[$ts] session=$sid cmd=$flat" >> /Users/chuan.li/.qoder/loop_engine/audit.log
fi

exit 0
