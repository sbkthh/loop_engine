"""Intent classification and dispatch for WeCom messages.

All messages go through async LLM path (qodercli subprocess, result pushed
via WeCom API). No keyword matching — LLM handles everything.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
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
    "- After editing a spec, run 'loop_engine next --root <path>' to start the "
    "loop (SCORE/CLASSIFY_CHANGE) — never implement code directly\n\n"
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


def _llm_dispatch(message, registry, data_dir, user_id):
    """Background LLM direct response with per-user session context."""
    qodercli_path = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")
    model = _get_model()
    session_id, is_new = _get_session_id(user_id)
    prompt = _LLM_SYSTEM_PROMPT.format(message=message)
    # first message creates session, subsequent messages resume it
    session_flag = "--session-id" if is_new else "--resume"
    try:
        r = subprocess.run(
            [qodercli_path, "--print", session_flag, session_id, "--model", model,
             "--dangerously-skip-permissions"],
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
                 "--dangerously-skip-permissions"],
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
    return reply


def dispatch(message, registry, data_dir, user_id="default"):
    """Classify and dispatch to the right handler.

    Returns Callable[[], str] for async (return "success" immediately,
    push result via WeCom API in background).
    """
    return lambda: _llm_dispatch(message, registry, data_dir, user_id)
