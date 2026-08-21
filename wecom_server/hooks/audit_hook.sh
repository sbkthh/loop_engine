#!/bin/bash
# PreToolUse hook for WeCom-spawned qodercli sessions: log sensitive
# external-side-effect commands (git push, MR creation, destructive ops)
# and every Edit/Write file path (G's direct code edits stay traceable)
# to the loop_engine audit log. Never blocks — exit 0 always.
input=$(cat)

AUDIT_LOG="${AUDIT_LOG:-$HOME/.qoder/loop_engine/audit.log}"

info=$(printf '%s' "$input" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get("tool_input", {})
    if d.get("tool_name") in ("Edit", "Write"):
        print(d["tool_name"] + "\t" + ti.get("file_path", ""))
    else:
        print("Bash\t" + ti.get("command", ""))
except Exception:
    print("Bash\t")
')

tool=${info%%$'\t'*}
detail=${info#*$'\t'}

should_log=0
if [ "$tool" = "Bash" ]; then
    if printf '%s' "$detail" | grep -qEi 'git push|--force|gh pr|glab mr|rm -rf|rm -fr|DROP TABLE|TRUNCATE TABLE'; then
        should_log=1
    fi
else
    [ -n "$detail" ] && should_log=1
fi

if [ "$should_log" -eq 1 ]; then
    ts=$(date '+%Y-%m-%dT%H:%M:%S')
    sid=$(printf '%s' "$input" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("session_id", ""))
except Exception:
    print("")
')
    flat=$(printf '%s' "$detail" | tr '\n' ' ')
    mkdir -p "$(dirname "$AUDIT_LOG")"
    echo "[$ts] session=$sid tool=$tool target=$flat" >> "$AUDIT_LOG"
fi

exit 0
