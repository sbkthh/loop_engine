#!/bin/bash
# PreToolUse hook for WeCom-spawned qodercli sessions: log sensitive
# external-side-effect commands (git push, MR creation, destructive ops)
# and every Edit/Write file path (G's direct code edits stay traceable)
# to the loop_engine audit log.
# Guards:
#  - BLOCK edits/writes to loop engine runtime state files (state.json,
#    pending.json, ...): those are written only by server handlers
#    (spec_result / approve / scheduler). G must use the __JSON_ACTION__
#    spec_result flow instead.
#  - SNAPSHOT spec.md content before every Edit so spec_result can report
#    change size (+X/-Y lines) at registration time.
#  - BLOCK Bash commands that write to protected state files (sed/python/tee
#    redirection bypassing Edit/Write) and commands that drive the loop
#    engine directly (import scheduler/machine/state, loop_engine next/run).
#    Read-only Bash (cat/grep/ls/git status) stays allowed.
input=$(cat)

AUDIT_LOG="${AUDIT_LOG:-$HOME/.qoder/loop_engine/audit.log}"
SNAP_DIR="${SNAP_DIR:-$HOME/.qoder/loop_engine/spec-snapshots}"

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
ts=$(date '+%Y-%m-%dT%H:%M:%S')
sid=$(printf '%s' "$input" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("session_id", ""))
except Exception:
    print("")
')

mkdir -p "$(dirname "$AUDIT_LOG")" "$SNAP_DIR"

# Guard 1: block direct edits to runtime state files.
# state.json/lock are protected only under a .loop/ dir; the flat JSON
# files (pending/runs/schedule/requirements/wecom) are loop engine data.
if printf '%s' "$detail" | grep -qE '(^|/)\.loop/(state\.json|lock)$|/(pending|runs|schedule|requirements|wecom)\.json$'; then
    flat=$(printf '%s' "$detail" | tr '\n' ' ')
    echo "[$ts] BLOCKED session=$sid tool=$tool target=$flat (runtime state file)" >> "$AUDIT_LOG"
    printf '已阻止：%s 是 loop engine 运行时状态文件，禁止直接编辑。请走 __JSON_ACTION__ spec_result 流程登记 spec 变更。' "$flat"
    exit 1
fi

# Guard 2: snapshot spec.md before each Edit (pre-edit content) so the
# server can diff change size when spec_result registers the change.
if [ "$tool" = "Edit" ] && printf '%s' "$detail" | grep -qE '/specs/[^/]+/spec\.md$'; then
    if [ -f "$detail" ]; then
        module=$(printf '%s' "$detail" | sed -E 's#.*/specs/([^/]+)/spec\.md$#\1#')
        snap="$SNAP_DIR/$(date +%Y%m%dT%H%M%S)-$sid-$module.md"
        cp "$detail" "$snap" 2>/dev/null
        pre_lines=$(wc -l < "$detail" | tr -d ' ')
        echo "[$ts] SPEC_SNAPSHOT session=$sid target=$detail lines=$pre_lines snapshot=$snap" >> "$AUDIT_LOG"
    fi
fi

# Guard 3: block Bash that writes to protected state files — closes the
# sed/python/tee redirection hole where Edit/Write are the only guarded path.
# Read-only commands (cat/grep/ls/git status) referencing the same paths pass.
if [ "$tool" = "Bash" ]; then
    if printf '%s' "$detail" | grep -qE '\.loop/|(^|/)(pending|runs|schedule|requirements|wecom)\.json|state\.json'; then
        if printf '%s' "$detail" | grep -qE '>>?|sed[[:space:]]+-i|perl[[:space:]]+-i|tee([[:space:]]|$)|cp[[:space:]]+|mv[[:space:]]+|rm[[:space:]]+|git[[:space:]]+(checkout|restore)|open\([^)]*[[:punct:]]w'; then
            flat=$(printf '%s' "$detail" | tr '\n' ' ')
            echo "[$ts] BLOCKED session=$sid tool=Bash target=$flat (state file write via Bash)" >> "$AUDIT_LOG"
            printf '已阻止：该 Bash 命令试图写入 loop engine 状态文件。请走 __JSON_ACTION__ spec_result 流程，或使用只读命令查看状态。' 
            exit 1
        fi
    fi
fi

# Guard 4: block Bash that drives the loop engine directly (importing
# scheduler/machine/state modules or invoking next/run/dispatch/approve),
# so G cannot approve or execute work outside the __JSON_ACTION__ gate.
if [ "$tool" = "Bash" ]; then
    if printf '%s' "$detail" | grep -qE '(import|from)[[:space:]]+(scheduler|machine|state)\b|scheduler\.(approve|dispatch|run|poll|next|is_locked)|loop_engine[[:space:]]+(next|run|dispatch|approve)|-m[[:space:]]+loop_engine[[:space:]]+(next|run|dispatch|approve)'; then
        flat=$(printf '%s' "$detail" | tr '\n' ' ')
        echo "[$ts] BLOCKED session=$sid tool=Bash target=$flat (loop engine direct drive)" >> "$AUDIT_LOG"
        printf '已阻止：该 Bash 命令直接驱动 loop engine。请通过 __JSON_ACTION__ approve / spec_result 流程操作。' 
        exit 1
    fi
fi

should_log=0
if [ "$tool" = "Bash" ]; then
    if printf '%s' "$detail" | grep -qEi 'git push|--force|gh pr|glab mr|rm -rf|rm -fr|DROP TABLE|TRUNCATE TABLE'; then
        should_log=1
    fi
else
    [ -n "$detail" ] && should_log=1
fi

if [ "$should_log" -eq 1 ]; then
    flat=$(printf '%s' "$detail" | tr '\n' ' ')
    echo "[$ts] session=$sid tool=$tool target=$flat" >> "$AUDIT_LOG"
fi

exit 0
