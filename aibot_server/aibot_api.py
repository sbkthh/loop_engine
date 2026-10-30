"""WeCom 智能机器人 long-connection protocol: pure frame/message helpers.

Transport lives in server.py; keeping the wire format here makes the
protocol testable without a socket.
"""
import re
import time

from wecom_server.wecom_api import sanitize_markdown, split_segments

WS_URL = "wss://openws.work.weixin.qq.com"
MAX_BYTES = 1800  # keep under WeCom's 2048-byte markdown limit

# Mentions arrive as "@机器人\u2005" (U+2005 four-per-em space) and may repeat.
_MENTION_RE = re.compile(r"^(?:\s*@[^\s\u2005]+\u2005?\s*)+")


def _req_id(prefix):
    return f"{prefix}_{int(time.time() * 1000)}"


def subscribe_frame(bot_id, secret):
    return {
        "cmd": "aibot_subscribe",
        "headers": {"req_id": _req_id("aibot_subscribe")},
        "body": {"bot_id": bot_id, "secret": secret},
    }


def ping_frame():
    return {"cmd": "ping", "headers": {"req_id": _req_id("ping")}}


def respond_frame(req_id, stream_id, content, finish=True):
    return {
        "cmd": "aibot_respond_msg",
        "headers": {"req_id": req_id},
        "body": {
            "msgtype": "stream",
            "stream": {"id": stream_id, "content": content, "finish": finish},
        },
    }


def send_msg_frame(chatid, content):
    return {
        "cmd": "aibot_send_msg",
        "headers": {"req_id": _req_id("aibot_send_msg")},
        "body": {
            "chatid": chatid,
            "msgtype": "markdown",
            "markdown": {"content": content},
        },
    }


def split(content):
    """Sanitize WeCom markdown and segment by UTF-8 byte length."""
    return split_segments(sanitize_markdown(content or ""), max_bytes=MAX_BYTES)


def strip_mentions(text):
    return _MENTION_RE.sub("", text or "").strip()


def extract_text(body):
    """Text content from text/mixed messages; "" for anything else."""
    if not isinstance(body, dict):
        return ""
    msgtype = body.get("msgtype")
    if msgtype == "text":
        return ((body.get("text") or {}).get("content") or "").strip()
    if msgtype == "mixed":
        mixed = body.get("mixed") or {}
        items = mixed.get("msg_item") or mixed.get("items") or []
        parts = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            content = ((item.get("text") or {}).get("content") or "").strip()
            if content:
                parts.append(content)
        return "\n".join(parts)
    return ""
