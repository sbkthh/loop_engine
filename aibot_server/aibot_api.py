"""WeCom 智能机器人 long-connection protocol: pure frame/message helpers.

Transport lives in server.py; keeping the wire format here makes the
protocol testable without a socket.
"""
import re
import time
from urllib.parse import parse_qs, urlparse

from wecom_server.wecom_api import sanitize_markdown, split_segments

WS_URL = "wss://openws.work.weixin.qq.com"
MAX_BYTES = 1800  # keep under WeCom's 2048-byte markdown limit
STREAM_MAX_BYTES = 20000  # stream replies allow up to 20480 bytes

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


_FONT_RE = re.compile(r'<font color="[^"]*">(.*?)</font>', flags=re.DOTALL)


def split(content, max_bytes=MAX_BYTES):
    """Flatten WeCom <font> tags (unsupported in aibot markdown), then segment."""
    text = _FONT_RE.sub(r"\1", content or "")
    return split_segments(sanitize_markdown(text), max_bytes=max_bytes)


def strip_mentions(text):
    return _MENTION_RE.sub("", text or "").strip()


def _mixed_text(mixed):
    mixed = mixed or {}
    items = mixed.get("msg_item") or mixed.get("items") or []
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Official callbacks use `msgtype`; older/third-party SDKs use `type`.
        if (item.get("msgtype") or item.get("type")) != "text":
            continue
        content = ((item.get("text") or {}).get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n".join(parts)


def extract_text(body):
    """Text content from text/mixed messages; "" for anything else."""
    if not isinstance(body, dict):
        return ""
    msgtype = body.get("msgtype")
    if msgtype == "text":
        return ((body.get("text") or {}).get("content") or "").strip()
    if msgtype == "mixed":
        return _mixed_text(body.get("mixed"))
    return ""


def extract_quote(body):
    """Quoted message text embedded in the callback, or "".

    The quote is part of the inbound frame; there is no API to fetch the
    original message by id. Non-text quotes become a placeholder so G knows
    an attachment was referenced.
    """
    quote = body.get("quote") if isinstance(body, dict) else None
    if not isinstance(quote, dict):
        return ""
    msgtype = quote.get("msgtype")
    if msgtype == "text":
        return ((quote.get("text") or {}).get("content") or "").strip()
    if msgtype == "mixed":
        return _mixed_text(quote.get("mixed"))
    if msgtype == "voice":
        return ((quote.get("voice") or {}).get("content") or "").strip()
    if msgtype == "image":
        return "[引用图片]"
    if msgtype == "file":
        name = (quote.get("file") or {}).get("name") or ""
        return f"[引用文件: {name}]" if name else "[引用文件]"
    return ""


def _media_item(item):
    if not isinstance(item, dict):
        return None
    kind = item.get("msgtype") or item.get("type")
    if kind not in ("image", "file"):
        return None
    content = item.get(kind) or {}
    url = (content.get("url") or "").strip()
    if not url:
        return None
    aeskey = (content.get("aeskey") or content.get("aes_key") or "").strip()
    if not aeskey:
        # Some frames put the key on the download URL instead of the body.
        query = parse_qs(urlparse(url).query)
        aeskey = (query.get("aeskey") or query.get("aes_key") or [""])[0]
    return {"kind": kind, "url": url, "aeskey": aeskey}


def extract_media(body):
    """Downloadable attachments in this message (images/files), or [].

    Quoted attachments are deliberately excluded: v1 only handles media the
    user sent in the current message.
    """
    if not isinstance(body, dict):
        return []
    msgtype = body.get("msgtype")
    if msgtype in ("image", "file"):
        item = _media_item({"msgtype": msgtype, msgtype: body.get(msgtype)})
        return [item] if item else []
    if msgtype == "mixed":
        mixed = body.get("mixed") or {}
        items = mixed.get("msg_item") or mixed.get("items") or []
        found = []
        for item in items:
            media = _media_item(item)
            if media:
                found.append(media)
        return found
    return []
