"""Flask webhook server for WeCom callbacks."""
import base64
import json
import logging
import os
import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
MAX_ASYNC_WORKERS = 8
_async_executor = ThreadPoolExecutor(max_workers=MAX_ASYNC_WORKERS)

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
    token = CONFIG.get("token", "")
    aes_key_b64 = CONFIG.get("encoding_aes_key", "") + "="
    aes_key = base64.b64decode(aes_key_b64)
    corpid = CONFIG.get("corp_id", "corpid")
    msg_signature = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
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
            _async_executor.submit(_async_worker, result, from_user, CONFIG)
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