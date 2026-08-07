"""Intent classification and dispatch for WeCom messages.

All messages go through async LLM path (qodercli subprocess, result pushed
via WeCom API). No keyword matching — LLM handles everything.
"""
import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger("wecom")

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
    "Answer the user's question concisely in Chinese. "
    "If asked about specific project status, run the command. "
    "If you don't know, say so.\n\n"
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


def _llm_dispatch(message, registry, data_dir):
    """Background LLM direct response. Returns reply string."""
    qodercli_path = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")
    model = _get_model()
    prompt = _LLM_SYSTEM_PROMPT.format(message=message)
    try:
        r = subprocess.run(
            [qodercli_path, "--print", "--model", model, "--dangerously-skip-permissions"],
            input=prompt, capture_output=True, text=True,
        )
        reply = (r.stdout or "").strip()
    except Exception as e:
        logger.error("[wecom] LLM dispatch error: %s", e)
        return f"处理失败：{e}"
    if not reply:
        return "无响应，请稍后再试。"
    return reply


def dispatch(message, registry, data_dir):
    """Classify and dispatch to the right handler.

    Returns Callable[[], str] for async (return "success" immediately,
    push result via WeCom API in background).
    """
    return lambda: _llm_dispatch(message, registry, data_dir)
