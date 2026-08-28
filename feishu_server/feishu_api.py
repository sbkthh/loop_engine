"""Feishu (Lark) REST API: app_access_token caching + message push."""
import json
import logging
import re
import time

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
    content = re.sub(r'<font color="[^"]*">(.*?)</font>', r"\1", content, flags=re.DOTALL)
    return content.replace("**", "").strip()


def send_text(open_id, content, config):
    """Push content as a Feishu text message. Returns True if sent."""
    token = get_app_access_token(config)
    r = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "open_id"},
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": sanitize_text(content)}),
        },
        timeout=10,
    )
    data = r.json()
    if data.get("code", -1) != 0:
        logger.error("[feishu] send failed: %s", data.get("msg"))
        return False
    logger.info("[feishu] pushed text to %s", open_id)
    return True
