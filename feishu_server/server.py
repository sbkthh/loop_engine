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

from .feishu_api import send_text

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
    if message.get("message_type") != "text":
        logger.info("[feishu] ignored non-text message: %s",
                    message.get("message_type"))
        return
    open_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    try:
        content = json.loads(message.get("content", "{}")).get("text", "")
    except ValueError:
        content = ""
    logger.info("[feishu] msg from %s: %s", open_id, content)
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


def _process(content, open_id):
    """Background: dispatch through the shared router, push the reply."""
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
