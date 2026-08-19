"""Flask webhook server for WeCom callbacks."""
import base64
import json
import logging
import os
import queue
import random
import string
import threading
import time
import xml.etree.ElementTree as ET

from flask import Flask, request, Response

from .crypto import verify_signature, decrypt_callback, encrypt_callback

logger = logging.getLogger("wecom")

# Note: WeCom callback XML is from authenticated WeCom servers (verified by
# msg_signature), so we use stdlib ElementTree. If extending to untrusted
# sources, switch to defusedxml.

app = Flask(__name__)

# Runtime config, set by start()
CONFIG = {}
DATA_DIR = os.path.expanduser("~/.qoder/loop_engine")

# Per-requirement serial queues: messages of the same requirement execute
# strictly in arrival order (they share one qodercli session), different
# requirements run in parallel. Bounded so a burst during a long task gets
# an immediate answer instead of an unbounded backlog.
# ponytail: ThreadPoolExecutor 会并行执行同一 session 的 qodercli 造成
# 上下文写竞态和回复乱序，必须按需求串行；queue.Full 是天然的有界反馈。
# 消费者线程按需求懒创建，单用户场景需求数量有限，不回收
_MAX_PENDING = 3
_pending = {}  # requirement -> queue.Queue
_pending_guard = threading.Lock()

# WeCom duplicate-callback dedup: the server sometimes POSTs the same message
# twice (identical msg_signature/timestamp/nonce). We short-circuit the second
# POST to avoid double-processing (duplicate G calls, double approve, etc.).
_DUP_CACHE_TTL = 30  # seconds
_seen_callbacks = {}  # msg_signature -> expiry_time
_seen_guard = threading.Lock()


def _queue_for(requirement):
    with _pending_guard:
        q = _pending.get(requirement)
        if q is None:
            q = queue.Queue(maxsize=_MAX_PENDING)
            _pending[requirement] = q
            threading.Thread(target=_drain_pending, args=(q,), daemon=True).start()
        return q

_LAST_USER_TTL = 5  # seconds; skip rewriting last_user.json within this window
_last_user_write = {}  # user -> last write timestamp


@app.route("/callback", methods=["GET"])
def callback_verify():
    """WeCom URL verification (GET)."""
    token = CONFIG.get("token", "")
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")
    aes_key_b64 = CONFIG.get("encoding_aes_key", "") + "="
    try:
        aes_key = base64.b64decode(aes_key_b64)
        plain = decrypt_callback(echostr, msg_signature, timestamp, nonce, token, aes_key)
        return plain
    except Exception as e:
        logger.error("callback verify failed: %s", e)
        return f"verify failed: {e}", 400


@app.route("/callback", methods=["POST"])
def callback_message():
    """Receive WeCom message callback (POST)."""
    global _seen_callbacks  # reassigned below (cleanup dict comp), needs global
    token = CONFIG.get("token", "")
    aes_key_b64 = CONFIG.get("encoding_aes_key", "") + "="
    aes_key = base64.b64decode(aes_key_b64)
    corpid = CONFIG.get("corp_id", "corpid")
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    # Duplicate-callback guard: identical msg_signature within TTL → skip
    with _seen_guard:
        if _seen_callbacks.get(msg_signature, 0) > time.time():
            logger.info("[wecom] duplicate callback skipped: %s", msg_signature)
            reply_xml_body = (
                f"<xml>"
                f"<ToUserName><![CDATA[{CONFIG.get('corp_id', 'corpid')}]]></ToUserName>"
                f"<FromUserName><![CDATA[{CONFIG.get('corp_id', 'corpid')}]]></FromUserName>"
                f"<CreateTime>{int(time.time())}</CreateTime>"
                f"<MsgType><![CDATA[text]]></MsgType>"
                f"<Content><![CDATA[OK]]></Content>"
                f"</xml>"
            )
            result = encrypt_callback(reply_xml_body, token, aes_key, corpid=CONFIG.get("corp_id", "corpid"))
            reply_xml = (
                f"<xml>"
                f"<Encrypt><![CDATA[{result['encrypted']}]]></Encrypt>"
                f"<MsgSignature><![CDATA[{result['signature']}]]></MsgSignature>"
                f"<TimeStamp>{result['timestamp']}</TimeStamp>"
                f"<Nonce><![CDATA[{result['nonce']}]]></Nonce>"
                f"</xml>"
            )
            return Response(reply_xml, mimetype="text/xml")
        if len(_seen_callbacks) > 100:
            _seen_callbacks = {k: v for k, v in _seen_callbacks.items()
                               if v > time.time()}
        _seen_callbacks[msg_signature] = time.time() + _DUP_CACHE_TTL
    body = request.get_data(as_text=True)
    # Parse XML
    root = ET.fromstring(body)
    encrypted = root.findtext("Encrypt")
    # Decrypt
    try:
        plain = decrypt_callback(encrypted, msg_signature, timestamp, nonce, token, aes_key)
    except ValueError as e:
        logger.warning("decrypt failed: %s", e)
        return f"decrypt failed: {e}", 400
    # Parse inner XML
    inner = ET.fromstring(plain)
    content = inner.findtext("Content", "")
    from_user = inner.findtext("FromUserName", "")
    msg_type = inner.findtext("MsgType", "")
    logger.info("[wecom] msg from %s (%s): %s", from_user, msg_type, content)
    # Remember the most recent active user so scheduler notifications
    # (pending detection etc.) can target the self-built app chat.
    now = time.time()
    if now - _last_user_write.get(from_user, 0) >= _LAST_USER_TTL:
        _last_user_write[from_user] = now
        try:
            with open(os.path.join(DATA_DIR, "last_user.json"), "w") as f:
                json.dump({"user": from_user}, f)
        except OSError:
            pass
    # Dispatch
    try:
        from .router import dispatch
        import registry as reg_mod
        reg = reg_mod.list_requirements()
        result = dispatch(content, reg, DATA_DIR, from_user)
        if callable(result):
            queued = _submit_async(result, from_user, CONFIG)
            if queued:
                reply = queued
            else:
                preview = content if len(content) <= 20 else content[:20] + "…"
                reply = f"已收到：「{preview}」正在处理中，完成后推送结果。"
            logger.info("[wecom] ack sent (async processing)")
        else:
            reply = result
            logger.info("[wecom] reply: %s", reply[:200])
    except Exception as e:
        import traceback
        logger.error("[wecom] dispatch error: %s", e)
        traceback.print_exc()
        reply = "暂时无法处理，请稍后再试。"
    # Encrypt reply with actual corpid
    reply_xml_body = (
        f"<xml>"
        f"<ToUserName><![CDATA[{from_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{corpid}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{reply}]]></Content>"
        f"</xml>"
    )
    result = encrypt_callback(reply_xml_body, token, aes_key, corpid=corpid)
    reply_xml = (
        f"<xml>"
        f"<Encrypt><![CDATA[{result['encrypted']}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{result['signature']}]]></MsgSignature>"
        f"<TimeStamp>{result['timestamp']}</TimeStamp>"
        f"<Nonce><![CDATA[{result['nonce']}]]></Nonce>"
        f"</xml>"
    )
    return Response(reply_xml, mimetype="text/xml")


@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/shutdown", methods=["POST"])
def shutdown():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return "forbidden", 403
    import signal
    os.kill(os.getpid(), signal.SIGINT)
    return "shutting down"


def _submit_async(fn, user_id, config):
    """Queue a message on its requirement's serial queue. Returns None when
    accepted, or an immediate reply string when that queue is full."""
    requirement = getattr(fn, "requirement", None) or "global"
    try:
        _queue_for(requirement).put_nowait((fn, user_id, config))
    except queue.Full:
        return "上一条还在处理中，完成后我会推送结果，请勿重复发送。"
    return None


def _drain_pending(q):
    """Serial consumer per requirement: one qodercli at a time, in order."""
    while True:
        _async_worker(*q.get())


def _async_worker(fn, user_id, config):
    """Background thread: run fn(), push result via WeCom API."""
    try:
        reply = fn()
        logger.info("[wecom] async reply: %s", reply[:200])
    except Exception as e:
        logger.error("[wecom] async handler error: %s", e)
        reply = f"处理失败：{e}"
    try:
        from .wecom_api import send_text
        send_text(user_id, reply, config)
    except Exception as e:
        logger.error("[wecom] push failed: %s", e)


def start(port=5000, debug=False):
    """Start the Flask server. Blocks until shutdown."""
    global CONFIG
    log_path = os.path.expanduser("~/wecom_server.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(),
        ],
    )
    config_path = os.path.join(DATA_DIR, "wecom.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            CONFIG = json.load(f)
    CONFIG["port"] = port
    print(f"Starting WeCom webhook on port {port}...", flush=True)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)