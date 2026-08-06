"""LLM intent classification and dispatch for WeCom messages."""
import json
import os
import shutil
import subprocess

from .handlers.approve import handle_approve
from .handlers.status import handle_status

_CLASSIFICATION_PROMPT = (
    "You are a WeCom message router. Classify the user's message into one of: "
    "approve, status, other. "
    "Respond with ONLY a single word: approve, status, or other.\n\n"
    "Message: {message}\n\n"
    "Classification:"
)


def classify_intent(message, qodercli_path=None):
    """Use LLM to classify user message intent."""
    if qodercli_path is None:
        qodercli_path = (
            shutil.which("qodercli") or
            os.path.expanduser("~/.local/bin/qodercli")
        )
    prompt = _CLASSIFICATION_PROMPT.format(message=message)
    r = subprocess.run(
        [qodercli_path, "--print", "--dangerously-skip-permissions"],
        input=prompt,
        capture_output=True, text=True, timeout=30,
    )
    result = (r.stdout or "").strip().lower()
    if "approve" in result:
        return "approve"
    if "status" in result:
        return "status"
    return "other"


def dispatch(message, registry, data_dir):
    """Classify and dispatch to the right handler."""
    intent = classify_intent(message)
    if intent == "approve":
        return "[调度] " + handle_approve(message, registry, data_dir)
    if intent == "status":
        return "[状态] " + handle_status(registry, data_dir)
    return (
        "我不太明白，试试说：\n"
        "- '查状态' — 查看所有需求状态\n"
        "- '批准执行' — 批准待执行的需求\n"
    )