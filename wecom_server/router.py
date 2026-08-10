"""Intent classification and dispatch for WeCom messages.

All messages go through async LLM path (qodercli subprocess, result pushed
via WeCom API). No keyword matching — LLM handles everything.
"""
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid

logger = logging.getLogger("wecom")

# qodercli startup noise that leaks into stdout before the actual LLM reply
_LLM_STDOUT_NOISE = (
    "MCP issues detected",
    "All dependencies are up to date",
    "qodercli ",
)

_LLM_SYSTEM_PROMPT = (
    "You are a WeCom bot assistant for the loop_engine project.\n\n"
    "loop_engine is a spec-driven development loop management system. "
    "It manages requirements through a state machine: "
    "SCORE → CLASSIFY_CHANGE → MAKER_STEP0 → STEP1_RED → STEP2_GREEN → CHECKER → "
    "MAKER_FIX(optional) → CODE_REVIEW → CODE_REVIEW_FIX(optional) → SYNCED.\n\n"
    "Available CLI commands:\n"
    "- loop_engine requirement-list — list all registered requirements\n"
    "- loop_engine status --root <path> — module state summary\n"
    "- loop_engine next --root <path> — route next step, output directives\n"
    "- loop_engine commit --root <path> — submit result, advance state machine\n"
    "- loop_engine init --root <path> — initialize .loop/state.json\n"
    "- loop_engine poll — detect pending changes\n"
    "- loop_engine pending — view pending work list\n"
    "- loop_engine approve <name> — approve a requirement for auto-execution\n"
    "- loop_engine reset --root <path> <module> — reset module to DRAFT\n"
    "- loop_engine set-status --root <path> <module> <status> — manual status\n"
    "- loop_engine requirement-add <name> <root> --prd <doc> — register from PRD\n"
    "- loop_engine self-check — verify system integrity\n\n"
    "Core concepts:\n"
    "- Requirement: a business goal (e.g. 'strategic stockup upgrade')\n"
    "- Module: one spec file + corresponding code, smallest orchestration unit\n"
    "- State machine: DRAFT → SCORE → MAKER → CHECKER → CODE_REVIEW → SYNCED\n"
    "- Each requirement has its own .loop/state.json, isolated by --root\n\n"
    "WeCom bot commands:\n"
    "- '查状态' — check all requirement statuses (instant, sync reply)\n"
    "- '批准执行' — approve pending auto-executions (instant, sync reply)\n"
    "- Any other question — LLM processes and pushes result via API (async)\n\n"
    "When the user is clearly approving/confirming execution of a requirement "
    "(e.g. '批准执行', '同意执行 cross-dock', 'approve'), your reply MUST start "
    "with exactly '__APPROVE__ <requirement name>' on the first line, then you "
    "may add a short confirmation. Do NOT run any commands — the prefix "
    "triggers the real approval automatically.\n\n"
    "When the user asks about execution history (e.g. '最近执行情况', "
    "'执行历史'), your reply MUST start with '__HISTORY__ <requirement name>' "
    "(or '__HISTORY__ ALL' when no requirement is mentioned), then you may add "
    "a short intro. Do NOT run any commands — the prefix reads the history "
    "automatically.\n\n"
    "Spec management rules (creating or modifying any spec.md):\n"
    "- First read ~/.qoder/skills/spec-session/SKILL.md and follow its workflow\n"
    "- ALWAYS run the grilling/grill-me skill first (every spec change, new or "
    "modification): interview the user one question at a time until shared "
    "understanding, then edit the spec\n"
    "- openspec-new-change/openspec-propose create a NEW change proposal only; "
    "they do NOT support appending to or modifying an existing change/spec — "
    "modify an existing spec by editing its spec.md in place\n"
    "- After editing a spec, your reply MUST start with exactly "
    "'__SPEC_RESULT__ <requirement name> <module key>' on the first line "
    "(module key is change_id/module_name, e.g. "
    "cross-dock-v2-backend/cross-dock-persistence), then a short summary of "
    "what changed. Do NOT run 'loop_engine next' or 'commit' — the prefix "
    "registers the change (hash update + backup) and the user then approves "
    "execution\n\n"
    "Manual execution rules (when driving a next/commit loop manually, e.g. "
    "user says '主动执行' or asks you to run the loop step by step):\n"
    "- ALWAYS run 'loop_engine manual-begin --root <path>' BEFORE the first "
    "'loop_engine next' — it acquires the same lock the scheduler uses, so a "
    "manual loop and a scheduled run never touch the same requirement "
    "concurrently. If manual-begin fails (lock held), do NOT proceed — tell "
    "the user the requirement is locked\n"
    "- Run 'loop_engine manual-end --root <path>' IMMEDIATELY when the loop "
    "finishes (machine reports IDLE/SYNCED) or the user stops it — never "
    "leave a manual loop without manual-end: it writes the run record and "
    "releases the lock\n\n"
    "Answer the user's question concisely in Chinese. "
    "If asked about specific project status, run the command. "
    "If you don't know, say so.\n\n"
    "Output format (mandatory):\n"
    "- Keep the reply under 500 Chinese characters\n"
    "- Use ONLY WeCom-supported markdown: # heading, **bold**, > quote, "
    "<font color=\"info|comment|warning\">text</font>\n"
    "- NO tables, NO code blocks (```), NO lists with - or *, NO links — "
    "plain text lines with line breaks instead\n"
    "- For multiple items, write them as separate lines like: '模块A: 状态'\n\n"
    "User: {message}\n"
)


_AUDIT_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hooks", "audit_hook.sh")


def _audit_settings():
    """Per-invocation qodercli settings auditing sensitive tool calls.

    Injected via --settings so only WeCom-spawned sessions carry the hook;
    the user's own qodercli sessions are untouched.
    """
    return json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command", "command": _AUDIT_HOOK}]}
            ]
        }
    })


def _get_model():
    """Read persisted model from qodercli settings, fallback to DeepSeek-V4-Flash."""
    settings_path = os.path.expanduser("~/.qoder/settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
            return settings.get("model", {}).get("name", "DeepSeek-V4-Flash")
    except Exception:
        return "DeepSeek-V4-Flash"


_SESSION_DIR = os.path.expanduser("~/.qoder/loop_engine/sessions")


def _get_session_id(user_id):
    """Get or create a stable qodercli session ID per WeCom user.
    Returns (session_id, is_new) where is_new=True means first-time use.
    """
    os.makedirs(_SESSION_DIR, exist_ok=True)
    path = os.path.join(_SESSION_DIR, f"{user_id}.txt")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip(), False
    sid = str(uuid.uuid4())
    with open(path, "w") as f:
        f.write(sid)
    return sid, True


def _execute_history(name, registry, data_dir):
    """Read runs.json and format the last executions. Returns reply text."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import scheduler
    runs = scheduler.load_runs()["runs"]
    if name != "ALL":
        runs = [r for r in runs if r["requirement"] == name]
    if not runs:
        return "暂无执行历史记录。"
    lines = [f"最近 {len(runs[-5:])} 次执行："]
    for r in runs[-5:]:
        lines.append(
            f"• {r['requirement']}：{r['end']}，{r['steps']} 步，"
            f"{r['duration_seconds']} 秒（{r['finished_at'][:16]}）")
    return "\n".join(lines)


def _execute_approve(name, registry, data_dir):
    """Approve + dispatch a requirement for real execution. Returns reply text."""
    req = next((r for r in registry if r.get("name") == name), None)
    if not req:
        available = ", ".join(r.get("name", "?") for r in registry) or "无"
        return f"没有找到需求：{name}（可用：{available}）"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import scheduler
    try:
        count = scheduler.approve(name)
    except ValueError as e:
        return f"无法批准：{e}"
    if count == 0:
        return f"{name} 没有待批准的自动执行项（可能已批准）"
    cfg = scheduler.load_config()
    forked = scheduler.dispatch(scheduler.load_pending()["pending"],
                                max_concurrency=cfg.get("max_concurrency", 2))
    if name in forked:
        return f"已批准并开始执行：{name}"
    return f"已批准 {name}，等待调度（并发上限或正在运行）"


def _audit_line(text):
    """Append a line to the shared audit log (same file as audit_hook.sh)."""
    try:
        log_path = os.path.expanduser("~/.qoder/loop_engine/audit.log")
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(log_path, "a") as f:
            f.write(f"[{ts}] {text}\n")
    except Exception:
        logger.exception("[wecom] audit log write failed")


def _resolve_module_key(st, key):
    """Resolve a user-supplied module key, auto-completing bare names."""
    modules = st.get("modules", {})
    if key in modules:
        return key
    if "/" in key:
        raise ValueError(
            f"模块 {key} 不在状态机中（可用：{', '.join(modules) or '无'}）")
    matches = [k for k in modules if k.rsplit("/", 1)[-1] == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"模块名 {key} 对应多个模块（{'、'.join(sorted(matches))}），"
            f"请回复 __SPEC_RESULT__ <需求名> <change_id>/<module_name>")
    raise ValueError(f"找不到模块 {key}（可用：{', '.join(modules) or '无'}）")


def _execute_spec_result(name, module_key, registry, data_dir):
    """Register a G-edited spec change: verify hash changed, backup, PARTIAL.

    The spec file itself is edited by the assistant (audited by the hook);
    this function only controls the registration gate so the scheduler picks
    the change up only after the user approves execution.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from constants import SPEC_PATH_TEMPLATE, PARTIAL
    import spec_utils
    from state import StateManager

    req = next((r for r in registry if r.get("name") == name), None)
    if not req:
        available = ", ".join(r.get("name", "?") for r in registry) or "无"
        return f"没有找到需求：{name}（可用：{available}）"
    root = req["root"]
    sm = StateManager(root)
    st = sm.load()
    try:
        module_key = _resolve_module_key(st, module_key)
    except ValueError as e:
        return str(e)
    change_id, module_name = module_key.split("/", 1)
    spec_path = os.path.join(root, SPEC_PATH_TEMPLATE.format(
        change_id=change_id, module_name=module_name))
    if not os.path.exists(spec_path):
        return (f"找不到 spec 文件：{spec_path}。"
                f"请先编辑 spec 再输出 __SPEC_RESULT__。")
    new_hash = spec_utils.compute_spec_hash(spec_path)
    module = st["modules"][module_key]
    old_hash = module.get("spec_hash")
    if old_hash == new_hash:
        return f"{module_key} 的 spec 没有变化（hash 未变），请先修改 spec.md"
    backup_dir = os.path.join(root, ".loop", "backup")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(
        backup_dir, f"spec-{module_name}-{int(time.time())}.md")
    rel = os.path.relpath(spec_path, root)
    old = subprocess.run(["git", "-C", root, "show", f"HEAD:{rel}"],
                         capture_output=True, text=True)
    if old.returncode == 0:
        with open(backup_path, "w") as f:
            f.write(old.stdout)
        backup_note = backup_path + " (HEAD)"
    else:
        # no git HEAD for this spec — snapshot current content so there is
        # at least a registration-time rollback point
        shutil.copy2(spec_path, backup_path)
        backup_note = backup_path + " (pre-edit snapshot, no git HEAD)"
    sm.set_module_field(st, module_key, "spec_hash", new_hash)
    sm.set_module_field(st, module_key, "status", PARTIAL)
    sm.save(st)
    _audit_line(f"SPEC {name} {module_key} {old_hash}->{new_hash} "
                f"backup={backup_note}")
    return (f"spec 变更已登记：{module_key}\n"
            f"旧 hash: {old_hash[:8]}  新 hash: {new_hash[:8]}\n"
            f"备份: {backup_note}\n"
            f"请回复『批准执行 {name}』开始实现")


def _llm_dispatch(message, registry, data_dir, user_id):
    """Background LLM direct response with per-user session context."""
    qodercli_path = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")
    model = _get_model()
    session_id, is_new = _get_session_id(user_id)
    prompt = _LLM_SYSTEM_PROMPT.format(message=message)
    # first message creates session, subsequent messages resume it
    session_flag = "--session-id" if is_new else "--resume"
    settings = _audit_settings()
    try:
        r = subprocess.run(
            [qodercli_path, "--print", session_flag, session_id, "--model", model,
             "--dangerously-skip-permissions", "--settings", settings],
            input=prompt, capture_output=True, text=True,
        )
        lines = (r.stdout or "").splitlines()
        while lines and lines[0].strip().startswith(_LLM_STDOUT_NOISE):
            lines.pop(0)
        reply = "\n".join(lines).strip()
        # resume failed (session lost, e.g. after server restart) → create fresh
        if not reply and not is_new:
            logger.info("[wecom] session %s not found, creating new", session_id)
            r = subprocess.run(
                [qodercli_path, "--print", "--session-id", session_id, "--model", model,
                 "--dangerously-skip-permissions", "--settings", settings],
                input=prompt, capture_output=True, text=True,
            )
            lines = (r.stdout or "").splitlines()
            while lines and lines[0].strip().startswith(_LLM_STDOUT_NOISE):
                lines.pop(0)
            reply = "\n".join(lines).strip()
    except Exception as e:
        logger.error("[wecom] LLM dispatch error: %s", e)
        return f"处理失败：{e}"
    if not reply:
        return "无响应，请稍后再试。"
    if reply.startswith("__APPROVE__"):
        name = reply[len("__APPROVE__"):].strip().splitlines()[0].strip()
        return _execute_approve(name, registry, data_dir)
    if reply.startswith("__HISTORY__"):
        name = reply[len("__HISTORY__"):].strip().splitlines()[0].strip() or "ALL"
        return _execute_history(name, registry, data_dir)
    if reply.startswith("__SPEC_RESULT__"):
        rest = reply[len("__SPEC_RESULT__"):].strip().splitlines()[0].strip()
        parts = rest.split(None, 1)
        if len(parts) != 2:
            return ("格式错误：__SPEC_RESULT__ <需求名> "
                    "<change_id>/<module_name>")
        return _execute_spec_result(parts[0], parts[1], registry, data_dir)
    return reply


def dispatch(message, registry, data_dir, user_id="default"):
    """Classify and dispatch to the right handler.

    Returns Callable[[], str] for async (return "success" immediately,
    push result via WeCom API in background).
    """
    return lambda: _llm_dispatch(message, registry, data_dir, user_id)
