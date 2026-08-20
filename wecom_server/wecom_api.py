"""WeCom REST API: access_token caching + message push."""
import logging
import re
import time

import requests

logger = logging.getLogger("wecom")

_token_cache = {"token": None, "expires_at": 0.0}

_MAX_BYTES = 1800  # keep well under WeCom's 2048-byte markdown limit


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


def sanitize_markdown(content):
    """Convert standard markdown to WeCom's supported subset (#, **, >, <font>).

    Tables collapse to pipe-joined text rows; fenced code blocks become indented text.
    """
    lines = []
    in_code = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines.append("    " + line)
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # table separator row
            lines.append(" | ".join(cells))
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def split_segments(content, max_bytes=_MAX_BYTES):
    """Split content into <=max_bytes segments on character boundaries (never mid-CJK)."""
    segments = []
    current = ""
    for ch in content:
        trial = current + ch
        if len(trial.encode("utf-8")) > max_bytes:
            segments.append(current)
            current = ch
        else:
            current = trial
    if current:
        segments.append(current)
    return segments


def md_bold(text):
    """WeCom markdown bold."""
    return f"**{text}**"


def md_color(text, color="comment"):
    """WeCom markdown font color: info (green), comment (gray), warning (orange-red)."""
    return f'<font color="{color}">{text}</font>'


def send_text(user_id, content, config):
    """Push content as WeCom markdown, segmented if long. Returns True if all sent."""
    token = get_access_token(config)
    content = sanitize_markdown(content)
    for seg in split_segments(content):
        r = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
            json={
                "touser": user_id,
                "msgtype": "markdown",
                "agentid": int(config["agent_id"]),
                "markdown": {"content": seg},
            },
            timeout=10,
        )
        data = r.json()
        if data.get("errcode", -1) != 0:
            logger.error("[wecom] send failed: %s", data.get("errmsg"))
            return False
        logger.info("[wecom] pushed segment (%d bytes) to %s", len(seg.encode("utf-8")), user_id)
    return True
