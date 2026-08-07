"""WeCom REST API: access_token caching + message push."""
import logging
import time

import requests

logger = logging.getLogger("wecom")

_token_cache = {"token": None, "expires_at": 0.0}


def get_access_token(config):
    """Get cached access_token, refresh if expired or within 5 min of expiry."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["token"]
    r = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": config["corp_id"], "corpsecret": config["secret"]},
        timeout=10,
    )
    data = r.json()
    if data.get("errcode", -1) != 0:
        raise RuntimeError(f"gettoken failed: {data.get('errmsg')}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 7200)
    logger.info("[wecom] access_token refreshed, expires_in=%s", data.get("expires_in"))
    return _token_cache["token"]


def send_text(user_id, content, config):
    """Push a text message to a specific user. Returns True on success."""
    token = get_access_token(config)
    r = requests.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
        json={
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(config["agent_id"]),
            "text": {"content": content},
        },
        timeout=10,
    )
    data = r.json()
    if data.get("errcode", -1) != 0:
        logger.error("[wecom] send failed: %s", data.get("errmsg"))
        return False
    logger.info("[wecom] pushed message to %s (%d chars)", user_id, len(content))
    return True
