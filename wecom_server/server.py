"""Flask webhook server for WeCom callbacks."""
import base64
import json
import os
import random
import string
import time
import xml.etree.ElementTree as ET

from flask import Flask, request, Response

from .crypto import verify_signature, decrypt_callback, encrypt_callback

# Note: WeCom callback XML is from authenticated WeCom servers (verified by
# msg_signature), so we use stdlib ElementTree. If extending to untrusted
# sources, switch to defusedxml.

app = Flask(__name__)

# Runtime config, set by start()
CONFIG = {}
DATA_DIR = os.path.expanduser("~/.qoder/loop_engine")


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
        return f"verify failed: {e}", 400


@app.route("/callback", methods=["POST"])
def callback_message():
    """Receive WeCom message callback (POST)."""
    token = CONFIG.get("token", "")
    aes_key_b64 = CONFIG.get("encoding_aes_key", "") + "="
    aes_key = base64.b64decode(aes_key_b64)
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
        return f"decrypt failed: {e}", 400
    # Parse inner XML
    inner = ET.fromstring(plain)
    content = inner.findtext("Content", "")
    # Dispatch
    from .router import dispatch
    import registry as reg_mod
    reg = reg_mod.list_requirements()
    reply = dispatch(content, reg, DATA_DIR)
    # Encrypt reply
    result = encrypt_callback(reply, token, aes_key)
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
    import signal
    os.kill(os.getpid(), signal.SIGINT)
    return "shutting down"


def start(port=5000, debug=False):
    """Start the Flask server. Blocks until shutdown."""
    global CONFIG
    config_path = os.path.join(DATA_DIR, "wecom.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            CONFIG = json.load(f)
    CONFIG["port"] = port
    if debug:
        app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
    else:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port)