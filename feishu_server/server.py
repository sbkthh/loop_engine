"""Feishu bot via official SDK WebSocket long connection.

No public URL, tunnel, or signature verification needed — the SDK keeps an
outbound connection to Feishu and auto-reconnects. Reuses the platform-
agnostic wecom_server.router.dispatch; only transport is Feishu-specific.
"""
import json
import logging
import os
import queue
import threading
import time

from .feishu_api import download_file, send_text

logger = logging.getLogger("feishu")

# Runtime config, set by start()
CONFIG = {}
DATA_DIR = os.path.expanduser("~/.qoder/loop_engine")

# Per-requirement serial queues, same design as the WeCom server (kept as a
# copy so the production WeCom path stays untouched).
# ponytail: duplicated queue glue from wecom_server/server.py; extract a
# shared module only when a third platform shows up.
_MAX_PENDING = 3
_pending = {}  # requirement -> queue.Queue
_pending_guard = threading.Lock()

# The SDK may redeliver events on reconnect; dedup by event_id as a second
# line of defense.
_DUP_CACHE_TTL = 900  # seconds
_seen_events = {}
_seen_guard = threading.Lock()

_LAST_USER_TTL = 5  # seconds; skip rewriting last_user.json within this window
_last_user_write = {}  # user -> last write timestamp

# Feishu can't put a file and free text in one message, so a bare file is
# buffered (not dispatched) for `file_wait_seconds` (feishu.json, default 60)
# hoping the user's next text is the instruction for it. open_id ->
# {"files": [(name, path)], "timer": Timer}. The timer flushes the file alone
# (fallback prompt) if no text arrives in time.
_DEFAULT_FILE_WAIT = 60  # seconds
_file_buffer = {}
_file_buffer_guard = threading.Lock()


def _handle_event(payload):
    """payload: dict shape of im.message.receive_v1. Spawns processing."""
    global _seen_events  # reassigned below (cleanup dict comp), needs global
    header = payload.get("header", {})
    event_id = header.get("event_id", "")
    if not event_id:
        return
    with _seen_guard:
        if _seen_events.get(event_id, 0) > time.time():
            logger.info("[feishu] duplicate event skipped: %s", event_id)
            return
        if len(_seen_events) > 100:
            _seen_events = {k: v for k, v in _seen_events.items()
                            if v > time.time()}
        _seen_events[event_id] = time.time() + _DUP_CACHE_TTL
    if header.get("event_type") != "im.message.receive_v1":
        return
    event = payload.get("event", {})
    message = event.get("message", {})
    open_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    mtype = message.get("message_type")
    if mtype == "file":
        try:
            fc = json.loads(message.get("content", "{}"))
        except ValueError:
            fc = {}
        logger.info("[feishu] file from %s: %s", open_id, fc.get("file_name", ""))
        threading.Thread(
            target=_buffer_file, daemon=True,
            args=(message.get("message_id", ""),
                  [(fc.get("file_key", ""), fc.get("file_name", ""))],
                  open_id)).start()
        return
    if mtype == "post":
        try:
            pc = json.loads(message.get("content", "{}"))
        except ValueError:
            pc = {}
        text, files = _parse_post(pc)
        logger.info("[feishu] post from %s: %s (%d file(s))",
                    open_id, text[:100], len(files))
        threading.Thread(target=_process_post, daemon=True,
                         args=(message.get("message_id", ""), text,
                               files, open_id)).start()
        return
    if mtype != "text":
        logger.info("[feishu] ignored non-text message: %s", mtype)
        return
    try:
        content = json.loads(message.get("content", "{}")).get("text", "")
    except ValueError:
        content = ""
    logger.info("[feishu] msg from %s: %s", open_id, content)
    pending = _take_file_buffer(open_id)
    if pending:
        # Text following a buffered file: merge into one prompt. The
        # file-arrival receipt already fired, so skip the duplicate.
        threading.Thread(target=_process, daemon=True,
                         kwargs={"content": _build_file_prompt(content, pending),
                                 "open_id": open_id,
                                 "send_receipt": False}).start()
        return
    threading.Thread(target=_process, args=(content, open_id), daemon=True).start()


def _remember_user(user_id):
    """Record the most recent active user + platform for scheduler pushes."""
    now = time.time()
    if now - _last_user_write.get(user_id, 0) >= _LAST_USER_TTL:
        _last_user_write[user_id] = now
        try:
            with open(os.path.join(DATA_DIR, "last_user.json"), "w") as f:
                json.dump({"user": user_id, "platform": "feishu"}, f)
        except OSError:
            pass


def _push(user_id, reply):
    try:
        send_text(user_id, reply, CONFIG)
    except Exception as e:
        logger.error("[feishu] push failed: %s", e)


def _receipt(open_id):
    """Lightweight ack so silence can't be confused with a dead service.
    Long connections have no inline reply channel (unlike WeCom callbacks);
    feishu.json receipt_enabled=false turns this off."""
    if not CONFIG.get("receipt_enabled", True):
        return
    try:
        send_text(open_id, "已收到，正在处理…", CONFIG)
    except Exception as e:
        logger.error("[feishu] receipt failed: %s", e)


def _process(content, open_id, send_receipt=True):
    """Background: dispatch through the shared router, push the reply."""
    if send_receipt:
        _receipt(open_id)
    _remember_user(open_id)
    try:
        from wecom_server.router import dispatch
        import registry as reg_mod
        reg = reg_mod.list_requirements()
        result = dispatch(content, reg, DATA_DIR, open_id)
        if callable(result):
            _submit_async(result, open_id, CONFIG)
            logger.info("[feishu] ack: async processing")
        else:
            _push(open_id, result)
            logger.info("[feishu] reply: %s", result[:200])
    except Exception as e:
        import traceback
        logger.error("[feishu] dispatch error: %s", e)
        traceback.print_exc()
        _push(open_id, "暂时无法处理，请稍后再试。")


def _parse_post(content_json):
    """Extract (text, [(file_key, file_name)]) from a post message payload.
    Paragraphs live under a locale key (zh_cn/en_us/...); take the first."""
    post = content_json.get("post") or {}
    body = next((v for v in post.values() if isinstance(v, dict)), None)
    texts, files = [], []
    for para in (body or {}).get("content") or []:
        for node in para or []:
            tag = node.get("tag")
            if tag == "text" and node.get("text"):
                texts.append(node["text"])
            elif tag == "a" and node.get("text"):
                texts.append(f'{node["text"]}({node.get("href", "")})')
            elif tag == "file":
                files.append((node.get("file_key", ""),
                              node.get("file_name", "")))
    return " ".join(texts).strip(), files


def _download_files(message_id, files):
    """Download each (file_key, file_name); return [(name, path)] that saved."""
    saved = []
    for file_key, file_name in files:
        try:
            path = download_file(message_id, file_key, CONFIG)
        except Exception as e:
            logger.error("[feishu] file download error: %s", e)
            path = None
        if path:
            saved.append((file_name, path))
    return saved


def _build_file_prompt(text, saved):
    """Merged prompt: optional user note + one line per attachment + the
    read-and-act fallback when files are present. Empty string if nothing."""
    parts = []
    if text:
        parts.append(f"用户说：{text}")
    parts += [f"附件「{n}」已保存到 {p}" for n, p in saved]
    if saved:
        parts.append("请读取文件内容并按其中的诉求处理"
                     "（如为 PRD 文档，按需求注册流程引导）。")
    return "\n".join(parts)


def _process_post(message_id, text, files, open_id):
    """Download attached files, then dispatch a prompt with text + paths."""
    saved = _download_files(message_id, files)
    if files and not saved:
        _push(open_id, "文件下载失败，请稍后再试。")
        return
    prompt = _build_file_prompt(text, saved)
    if not prompt:
        return
    _process(prompt, open_id)


def _buffer_file(message_id, files, open_id):
    """Worker for a bare file message: ack, download, then hold for text."""
    _receipt(open_id)
    saved = _download_files(message_id, files)
    if not saved:
        _push(open_id, "文件下载失败，请稍后再试。")
        return
    _add_to_file_buffer(open_id, saved)


def _add_to_file_buffer(open_id, saved):
    wait = CONFIG.get("file_wait_seconds", _DEFAULT_FILE_WAIT)
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        wait = _DEFAULT_FILE_WAIT
    if wait <= 0:
        # Buffering disabled: process the file alone now (receipt already sent).
        _process(_build_file_prompt("", saved), open_id, send_receipt=False)
        return
    with _file_buffer_guard:
        entry = _file_buffer.get(open_id)
        if entry and entry["timer"]:
            entry["timer"].cancel()
        files = (entry["files"] if entry else []) + saved
        timer = threading.Timer(wait, _flush_file_buffer, args=(open_id,))
        timer.daemon = True
        _file_buffer[open_id] = {"files": files, "timer": timer}
    timer.start()


def _take_file_buffer(open_id):
    """Atomically claim a user's pending buffer; None if empty. Single owner of
    the text-merge vs timer-flush race, so a file dispatches exactly once."""
    with _file_buffer_guard:
        entry = _file_buffer.pop(open_id, None)
    if not entry:
        return None
    if entry["timer"]:
        entry["timer"].cancel()
    return entry["files"]


def _flush_file_buffer(open_id):
    """Timer fired with no text: dispatch the buffered file alone."""
    files = _take_file_buffer(open_id)
    if not files:
        return
    _process(_build_file_prompt("", files), open_id, send_receipt=False)


def _queue_for(requirement):
    with _pending_guard:
        q = _pending.get(requirement)
        if q is None:
            q = queue.Queue(maxsize=_MAX_PENDING)
            _pending[requirement] = q
            threading.Thread(target=_drain_pending, args=(q,), daemon=True).start()
        return q


def _submit_async(fn, user_id, config):
    """Queue on its requirement's serial queue; push busy-note when full."""
    requirement = getattr(fn, "requirement", None) or "global"
    try:
        _queue_for(requirement).put_nowait((fn, user_id, config))
    except queue.Full:
        _push(user_id, "上一条还在处理中，完成后我会推送结果，请勿重复发送。")


def _drain_pending(q):
    while True:
        _async_worker(*q.get())


def _async_worker(fn, user_id, config):
    try:
        reply = fn()
        logger.info("[feishu] async reply: %s", reply[:200])
    except Exception as e:
        logger.error("[feishu] async handler error: %s", e)
        reply = f"处理失败：{e}"
    _push(user_id, reply)


def _on_message(data):
    """SDK callback for im.message.receive_v1 (long connection)."""
    try:
        payload = {
            "header": {
                "event_id": data.header.event_id if data.header else "",
                "event_type": data.header.event_type if data.header else "",
            },
            "event": {
                "sender": {"sender_id": {
                    "open_id": data.event.sender.sender_id.open_id
                    if data.event and data.event.sender else ""}},
                "message": {
                    "message_type": data.event.message.message_type
                    if data.event and data.event.message else "",
                    "message_id": data.event.message.message_id
                    if data.event and data.event.message else "",
                    "content": data.event.message.content
                    if data.event and data.event.message else "{}",
                },
            },
        }
        _handle_event(payload)
    except Exception as e:
        import traceback
        logger.error("[feishu] event handling error: %s", e)
        traceback.print_exc()


def start():
    """Connect to Feishu via WebSocket long connection. Blocks until stopped."""
    global CONFIG
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config_path = os.path.join(DATA_DIR, "feishu.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            CONFIG = json.load(f)
    if not CONFIG.get("app_id") or not CONFIG.get("app_secret"):
        print("feishu.json missing app_id/app_secret. "
              "Run 'loop_engine feishu config' first.")
        raise SystemExit(1)
    import lark_oapi as lark
    # Corporate TLS interception serves a self-signed root that is in
    # certifi's bundle but not in OpenSSL's default paths; without this the
    # WebSocket handshake fails with CERTIFICATE_VERIFY_FAILED.
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_on_message)
               .build())
    client = lark.ws.Client(CONFIG["app_id"], CONFIG["app_secret"],
                            event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    print("Starting Feishu long-connection bot (WebSocket)...", flush=True)
    client.start()  # blocks; auto_reconnect enabled by default
