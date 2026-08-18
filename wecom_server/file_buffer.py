"""File message buffer: persist pending files, pair with next text message.

Design: docs/superpowers/specs/2026-08-14-wecom-file-pairing-design.md
"""
import json
import logging
import os
import threading
import time

from .wecom_api import download_media

logger = logging.getLogger("wecom")

_MAX_GROUP_SIZE = 5
_MAX_CHARS = 50000
_NUDGE_AFTER = 300  # 5 min
_EXPIRE_AFTER = 1800  # 30 min
_POLL_INTERVAL = 30  # nudge/expiry check interval


def _path(data_dir):
    return os.path.join(data_dir, "pending_files.json")


def _load(data_dir):
    p = _path(data_dir)
    if not os.path.exists(p):
        return {"files": []}
    with open(p) as f:
        return json.load(f)


def _save(data, data_dir):
    p = _path(data_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def add_file(user, media_id, filename, data_dir):
    """Record a file pending text pairing. Returns (ok, msg)."""
    data = _load(data_dir)
    user_files = [e for e in data["files"] if e["user"] == user]
    if len(user_files) >= _MAX_GROUP_SIZE:
        return False, f"已收到 {_MAX_GROUP_SIZE} 个待配对文件，请先发文字说明处理"
    now = time.time()
    data["files"].append({
        "user": user,
        "media_id": media_id,
        "name": filename,
        "received_at": now,
        "nudge_sent": False,
    })
    _save(data, data_dir)
    n = len(user_files) + 1
    return True, f"已收到文件 {filename}（第 {n}/{_MAX_GROUP_SIZE} 个），可继续发文字说明用途"


def _pending_group(data, user):
    """Return list of pending files for user, oldest first."""
    return [e for e in data["files"] if e["user"] == user]


def clear_group(data, user, data_dir):
    """Remove all files for a user."""
    data["files"] = [e for e in data["files"] if e["user"] != user]
    _save(data, data_dir)


def _load_config(data_dir):
    config_path = os.path.join(data_dir, "wecom.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return json.load(f)
    return {}


def attach_pending(text, user, data_dir):
    """Pair pending files with text. Returns (paired_text, files_used).

    Downloads, decodes, and prepends file contents to the text message.
    Failed files are removed from the group but do not block the rest.
    """
    data = _load(data_dir)
    group = _pending_group(data, user)
    if not group:
        return text, []

    config = _load_config(data_dir)
    lines = [text, ""]
    used = []
    failed = []
    for entry in group:
        try:
            content, filename = download_media(entry["media_id"], config)
            decoded = _decode(content)
            if decoded is None:
                failed.append(entry["name"])
                continue
            if len(decoded) > _MAX_CHARS:
                decoded = decoded[:_MAX_CHARS]
                decoded += f"\n（内容已截断，共 {len(content)} 字符）"
            lines.append(f"【文件: {filename}】")
            lines.append(decoded)
            lines.append("")
            used.append(entry["name"])
        except Exception as e:
            logger.warning("[file_buffer] download failed for %s: %s",
                          entry.get("media_id"), e)
            failed.append(entry.get("name", "?"))

    clear_group(data, user, data_dir)
    if failed:
        lines.append(f"（以下文件下载失败，请重发：{', '.join(failed)}）")
    return "\n".join(lines), used


def _decode(raw):
    """Try UTF-8 then GBK; return None for binary content."""
    if b"\x00" in raw[:4096]:
        return None
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def _nudge_and_expire(data_dir):
    """One pass: send nudge for 5-min-olds, expire 30-min-olds.

    Returns list of nudge messages and expiry messages to push.
    """
    from .wecom_api import send_text
    # Lazy import to avoid circular dependency; config is resolved at call time
    config_path = os.path.join(data_dir, "wecom.json")
    if not os.path.exists(config_path):
        return
    with open(config_path) as f:
        config = json.load(f)
    data = _load(data_dir)
    now = time.time()
    changed = False
    for entry in list(data["files"]):
        age = now - entry["received_at"]
        if age >= _EXPIRE_AFTER:
            send_text(entry["user"],
                      f"您之前发送的文件 {entry['name']} 已过期（超过 30 分钟未配对），"
                      "如需处理请重新发送",
                      config)
            data["files"].remove(entry)
            changed = True
        elif age >= _NUDGE_AFTER and not entry["nudge_sent"]:
            send_text(entry["user"],
                      f"已收到文件 {entry['name']}，请发送文字说明用途",
                      config)
            entry["nudge_sent"] = True
            changed = True
    if changed:
        _save(data, data_dir)


def start_nudge_thread(data_dir):
    """Start a daemon thread that periodically checks for nudge/expiry."""
    stop = threading.Event()

    def _loop():
        while not stop.wait(_POLL_INTERVAL):
            try:
                _nudge_and_expire(data_dir)
            except Exception:
                logger.exception("[file_buffer] nudge/expiry pass failed")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return stop