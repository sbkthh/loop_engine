"""Feishu (Lark) REST API: app_access_token caching + message push."""
import json
import logging
import os
import re
import time
from urllib.parse import unquote

import requests

logger = logging.getLogger("feishu")

_token_cache = {"token": None, "expires_at": 0.0}


def get_app_access_token(config):
    """Get cached app_access_token, refresh if expired or within 5 min."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["token"]
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": config["app_id"], "app_secret": config["app_secret"]},
        timeout=10,
    )
    data = r.json()
    if data.get("code", -1) != 0:
        raise RuntimeError(f"app_access_token failed: {data.get('msg')}")
    _token_cache["token"] = data["app_access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expire", 7200)
    logger.info("[feishu] app_access_token refreshed, expire=%s", data.get("expire"))
    return _token_cache["token"]


def sanitize_text(content):
    """Flatten WeCom-flavoured markdown into Feishu plain text."""
    return _strip_font(content).replace("**", "").strip()


def _strip_font(content):
    """Drop <font> tags (unsupported in card markdown), keep the text."""
    content = re.sub(r'<font color="[^"]*">(.*?)</font>', r"\1", content, flags=re.DOTALL)
    return content.strip()


_MARKDOWN_HINT_RE = re.compile(r"\*\*|\]\(|<font")


def send_text(open_id, content, config):
    """Push content to Feishu. Markdown-flavoured content goes out as an
    interactive card (renders bold/links/lists); plain text stays a text
    message. Returns True if sent."""
    if _MARKDOWN_HINT_RE.search(content):
        return _send(open_id, "interactive", {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": _strip_font(content)}],
        }, config, "card")
    return _send(open_id, "text", {"text": sanitize_text(content)}, config, "text")


def _send(open_id, msg_type, payload, config, label):
    token = get_app_access_token(config)
    r = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "open_id"},
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": open_id,
            "msg_type": msg_type,
            "content": json.dumps(payload),
        },
        timeout=10,
    )
    data = r.json()
    if data.get("code", -1) != 0:
        logger.error("[feishu] send failed: %s", data.get("msg"))
        return False
    logger.info("[feishu] pushed %s to %s", label, open_id)
    return True


_FILES_DIR = os.path.expanduser("~/.qoder/loop_engine/files")


def download_file(message_id, file_key, config, save_dir=_FILES_DIR):
    """Download a file attached to a received message. Returns the saved
    path, or None on failure. Resource follows the message itself — no
    separate media library (unlike WeCom's media/get)."""
    token = get_app_access_token(config)
    r = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/messages/"
        f"{message_id}/resources/{file_key}",
        params={"type": "file"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code != 200 or "application/json" in r.headers.get("Content-Type", ""):
        logger.error("[feishu] download failed: %s %s",
                     r.status_code, r.text[:200])
        return None
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)",
                  r.headers.get("Content-Disposition", ""))
    name = unquote(m.group(1)) if m else f"feishu-{file_key[:12]}"
    # urllib3 decodes headers as latin-1; non-ASCII filenames arrive as
    # mojibake (执行结果 → æ§è¡ç»æ) unless re-decoded as UTF-8.
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, name)
    with open(path, "wb") as f:
        f.write(r.content)
    logger.info("[feishu] downloaded %s (%d bytes)", path, len(r.content))
    return path
