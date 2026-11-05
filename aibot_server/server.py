"""WeCom 智能机器人 long-connection transport.

Mirrors feishu_server's shape: one outbound WebSocket, per-conversation
serial queues, the platform-agnostic wecom_server.router.dispatch, and a
loopback HTTP control port for scheduler notifications and stop/status.

Group chats are read-only: the router receives a read-only action
allowlist and a tool-level guard hook, so no approve/close/edit can run
from a group. Single chats: `allowed_users` gets full actions and owns
`last_user.json` (scheduler notifications); every other single chat is
read-only. Missing/empty `allowed_users` means all DMs are read-only.
"""
import asyncio
import json
import logging
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from constants import DATA_DIR as _DEFAULT_DATA_DIR

from .aibot_api import (
    STREAM_MAX_BYTES,
    WS_URL,
    extract_media,
    extract_quote,
    extract_text,
    ping_frame,
    respond_frame,
    send_msg_frame,
    split,
    strip_mentions,
    subscribe_frame,
)
from . import media as media_mod
from .media import prepare_media

logger = logging.getLogger("aibot")

CONFIG = {}
DATA_DIR = _DEFAULT_DATA_DIR

_ws = None
_connected = False
_loop = None
_stop_event = None
_last_inbound = 0.0

_MAX_PENDING = 3
_DUP_CACHE_TTL = 900
_DUP_CACHE_MAX = 200
_LAST_USER_TTL = 5

_seen = {}
_seen_guard = threading.Lock()
_last_user_write = {}

_queues = {}
_workers = {}

# Same set as router._GROUP_READONLY_ACTIONS, kept in sync by
# test_group_readonly_set_matches_router; importing the router here would
# pull its module-level dependencies into every aibot import.
GROUP_READONLY_ACTIONS = frozenset({"history", "gray_list", "score_view"})


def load_config(data_dir=None):
    path = os.path.join(data_dir or DATA_DIR, "aibot.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _dedup(msgid):
    """True when msgid was already seen (WeCom may redeliver on reconnect)."""
    now = time.time()
    with _seen_guard:
        if _seen.get(msgid, 0) > now:
            return True
        if len(_seen) > _DUP_CACHE_MAX:
            expired = [k for k, v in _seen.items() if v <= now]
            for k in expired:
                _seen.pop(k, None)
        _seen[msgid] = now + _DUP_CACHE_TTL
    return False


def _is_subscribe_frame(frame):
    """True for the subscribe ack/error. Error frames may omit `cmd` and
    carry only the aibot_subscribe_* req_id, so match both."""
    req_id = (frame.get("headers") or {}).get("req_id", "")
    return (frame.get("cmd") == "aibot_subscribe"
            or req_id.startswith("aibot_subscribe_"))


def _conversation(body):
    """(kind, chatid, userid) or None. Single-chat chatid is the userid."""
    kind = body.get("chattype") or "single"
    userid = ((body.get("from") or {}).get("userid") or "").strip()
    chatid = (body.get("chatid") or "").strip()
    if kind == "group":
        return ("group", chatid, userid) if chatid and userid else None
    if kind != "single" or not userid:
        return None
    return ("single", userid, userid)


def _is_self_message(body):
    bot_id = CONFIG.get("bot_id") or ""
    if not bot_id:
        return False
    aibotid = body.get("aibotid") or ""
    if aibotid and aibotid != bot_id:
        return True
    return ((body.get("from") or {}).get("userid") or "") == bot_id


def _remember_user(chatid, chattype, userid):
    """Record the last interaction target for scheduler pushes."""
    now = time.time()
    if now - _last_user_write.get(chatid, 0) < _LAST_USER_TTL:
        return
    _last_user_write[chatid] = now
    try:
        with open(os.path.join(DATA_DIR, "last_user.json"), "w") as f:
            json.dump({"user": userid, "platform": "aibot",
                       "chatid": chatid, "chattype": chattype}, f,
                      ensure_ascii=False)
    except OSError:
        pass


def _session_key(kind, chatid):
    return f"aibot:{'u' if kind == 'single' else 'g'}:{chatid}"


def _is_allowed_user(userid):
    """Single-chat allowlist. Missing/empty config means deny all DMs."""
    allowed = CONFIG.get("allowed_users") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    return bool(userid) and userid in {
        u.strip() for u in allowed if isinstance(u, str) and u.strip()}


async def _send(frame):
    ws = _ws
    if ws is None:
        raise RuntimeError("aibot not connected")
    await ws.send(json.dumps(frame, ensure_ascii=False))


async def _reply(req_id, stream_id, content, finish=True):
    if not req_id:
        return
    await _send(respond_frame(req_id, stream_id, content, finish=finish))


async def _send_markdown(chatid, content):
    for segment in split(content):
        await _send(send_msg_frame(chatid, segment))


async def _send_reply(req_id, msgid, chatid, content):
    """Finish the reply stream opened by the ack on the original callback.

    Ack and final must share one stream id: WeCom then updates the same
    message bubble, so the final result stays quoted to the user's question.
    Two ids would render as a second, unquoted message. Only content beyond
    the stream limit spills into proactive pushes.
    """
    segments = split(content, max_bytes=STREAM_MAX_BYTES)
    if not segments:
        return
    await _send(respond_frame(req_id, f"reply_{msgid}", segments[0],
                              finish=True))
    for segment in split("\n".join(segments[1:])):
        await _send(send_msg_frame(chatid, segment))


def _build_job(text, userid, session_key, allowed_actions, detect_text=None):
    from wecom_server.router import dispatch
    import registry as reg_mod
    return dispatch(text, reg_mod.list_requirements(), DATA_DIR, userid,
                    session_key=session_key,
                    allowed_actions=allowed_actions,
                    detect_text=detect_text)


def _queue_for(key):
    q = _queues.get(key)
    if q is None:
        q = asyncio.Queue(maxsize=_MAX_PENDING)
        _queues[key] = q
        _workers[key] = asyncio.create_task(_worker(q))
    return q


async def _worker(q):
    while True:
        fn, chatid = await q.get()
        try:
            reply = await asyncio.get_running_loop().run_in_executor(None, fn)
        except Exception as e:
            logger.error("[aibot] handler failed: %s", e)
            reply = f"处理失败：{e}"
        try:
            req_id = getattr(fn, "reply_req_id", "")
            msgid = getattr(fn, "reply_msgid", "")
            if req_id:
                try:
                    await _send_reply(req_id, msgid, chatid, reply)
                except Exception as e:
                    logger.error("[aibot] reply push failed, falling back: %s", e)
                    await _send_markdown(chatid, reply)
            else:
                await _send_markdown(chatid, reply)
        except Exception as e:
            logger.error("[aibot] push failed: %s", e)


async def _enqueue(kind, chatid, fn):
    key = f"{kind}:{chatid}"
    try:
        _queue_for(key).put_nowait((fn, chatid))
    except asyncio.QueueFull:
        note = "上一条还在处理中，完成后我会推送结果，请勿重复发送。"
        req_id = getattr(fn, "reply_req_id", "")
        msgid = getattr(fn, "reply_msgid", "")
        if req_id and msgid:
            await _reply(req_id, f"reply_{msgid}", note, finish=True)
        else:
            await _send_markdown(chatid, note)


async def _handle_frame(frame):
    """Handle one inbound frame. Exposed for tests (fake _ws)."""
    cmd = frame.get("cmd")
    if cmd != "aibot_msg_callback":
        if frame.get("errcode") not in (None, 0):
            logger.error("[aibot] server error: %s", frame)
        return
    body = frame.get("body") or {}
    req_id = (frame.get("headers") or {}).get("req_id", "")
    msgid = body.get("msgid") or ""
    if not msgid or _dedup(msgid) or _is_self_message(body):
        return
    conv = _conversation(body)
    if not conv:
        return
    kind, chatid, userid = conv
    msgtype = body.get("msgtype")
    if msgtype == "event":
        return
    # Only the owner's single chat is writable and may redirect scheduler
    # notifications. Everyone else (and every group) is read-only.
    is_owner = kind == "single" and _is_allowed_user(userid)
    if kind == "single" and not is_owner:
        logger.info("[aibot] read-only single chat: %s", userid)
    if msgtype not in ("text", "mixed", "image", "file"):
        try:
            await _reply(req_id, f"unsup_{msgid}",
                         "暂不支持该消息类型，请发送文字。")
        except Exception as e:
            logger.error("[aibot] unsupported reply failed: %s", e)
        return
    text = strip_mentions(extract_text(body))
    media_items = extract_media(body)
    quote = extract_quote(body)
    if not text and not media_items and not quote:
        if kind == "single":
            try:
                await _reply(req_id, f"empty_{msgid}",
                             "未识别到文本内容，请用纯文本重发。")
            except Exception as e:
                logger.error("[aibot] empty reply failed: %s", e)
        return
    if is_owner:
        _remember_user(chatid, kind, userid)
    allowed = None if is_owner else GROUP_READONLY_ACTIONS
    receipt = CONFIG.get("receipt_enabled", True)
    if isinstance(receipt, str):
        receipt = receipt.strip().lower() not in ("0", "false", "no", "off")
    if receipt:
        try:
            await _reply(req_id, f"reply_{msgid}", "已收到，正在处理…",
                         finish=False)
        except Exception as e:
            logger.error("[aibot] ack failed: %s", e)
    fragments = []
    if media_items:
        try:
            loop = asyncio.get_running_loop()
            fragments = await loop.run_in_executor(
                None,
                lambda: [prepare_media(item, msgid) for item in media_items])
        except Exception as e:
            first = media_items[0] if media_items else {}
            logger.error(
                "[aibot] media failed: %s (kind=%s host=%s aeskey_len=%d)",
                e, first.get("kind"),
                urlparse(first.get("url") or "").hostname,
                len(first.get("aeskey") or ""))
            try:
                if req_id:
                    await _reply(req_id, f"reply_{msgid}",
                                 "附件处理失败，请重发或改发文字。", finish=True)
                else:
                    await _send_markdown(chatid, "附件处理失败，请重发或改发文字。")
            except Exception as send_err:
                logger.error("[aibot] media error push failed: %s", send_err)
            return
    parts = ([text] if text else []) + fragments
    current = "\n\n".join(parts)
    if quote:
        current = (f"[引用消息]\n{quote}\n\n[当前消息]\n{current}"
                   if current else f"[引用消息]\n{quote}")
    fn = _build_job(current, userid, _session_key(kind, chatid), allowed,
                    detect_text=text)
    fn.reply_req_id = req_id
    fn.reply_msgid = msgid
    await _enqueue(kind, chatid, fn)


async def _heartbeat(ws, stop):
    while not stop.is_set():
        await asyncio.sleep(25)
        if stop.is_set():
            return
        if time.time() - _last_inbound > 90:
            logger.warning("[aibot] no inbound for 90s, reconnecting")
            await ws.close()
            return
        try:
            await ws.send(json.dumps(ping_frame()))
        except Exception as e:
            logger.warning("[aibot] heartbeat failed: %s", e)
            return


async def _session(config):
    global _ws, _connected, _last_inbound
    from websockets.asyncio.client import connect

    async with connect(WS_URL, ping_interval=None, ping_timeout=None,
                       max_size=4 * 1024 * 1024) as ws:
        _ws = ws
        _connected = True
        _last_inbound = time.time()
        await ws.send(json.dumps(
            subscribe_frame(config["bot_id"], config["secret"]),
            ensure_ascii=False))
        logger.info("[aibot] connected, subscribe sent")
        stop = asyncio.Event()
        hb = asyncio.create_task(_heartbeat(ws, stop))
        try:
            async for raw in ws:
                _last_inbound = time.time()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if frame.get("errcode") not in (None, 0):
                    logger.error("[aibot] server error frame: %s", frame)
                    if _is_subscribe_frame(frame):
                        raise RuntimeError(
                            frame.get("errmsg") or "subscribe failed")
                    continue
                await _handle_frame(frame)
        finally:
            stop.set()
            hb.cancel()
            _connected = False
            _ws = None


async def _run(config):
    delay = 1.0
    while not _stop_event.is_set():
        started = time.time()
        try:
            await _session(config)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[aibot] connection error: %s", e)
        if _stop_event.is_set():
            break
        elapsed = time.time() - started
        if elapsed < 5:
            # Kicked by a newer connection or auth failure: do not hammer
            # aibot_subscribe (the server rate-limits subscriptions).
            delay = 60.0
        elif elapsed > 60:
            delay = 1.0
        wait = min(60.0, delay) * random.uniform(0.8, 1.2)
        logger.info("[aibot] reconnecting in %.0fs", wait)
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=wait)
            break
        except asyncio.TimeoutError:
            pass
        delay = min(60.0, max(1.0, delay * 2))


async def _shutdown():
    if _stop_event is not None:
        _stop_event.set()
    ws = _ws
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass


class _ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "connected": _connected,
                             "bot_id": CONFIG.get("bot_id", "")})
        else:
            self._json(404, {"ok": False})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            payload = {}
        if self.path == "/notify":
            chatid = (payload.get("chatid") or "").strip()
            content = payload.get("content") or ""
            if not chatid or not content:
                self._json(400, {"ok": False, "error": "chatid/content required"})
                return
            if not _connected or _loop is None:
                self._json(503, {"ok": False, "error": "aibot not connected"})
                return
            fut = asyncio.run_coroutine_threadsafe(
                _send_markdown(chatid, content), _loop)
            try:
                fut.result(timeout=5)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/shutdown":
            self._json(200, {"ok": True})
            if _loop is not None:
                asyncio.run_coroutine_threadsafe(_shutdown(), _loop)
            return
        self._json(404, {"ok": False})


def _start_control(port):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _ControlHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def _main(config):
    global _loop, _stop_event
    _loop = asyncio.get_running_loop()
    _stop_event = asyncio.Event()
    await _run(config)


def start(data_dir=None):
    """Blocking entry point: control port + WebSocket long connection."""
    global CONFIG, DATA_DIR
    if data_dir:
        DATA_DIR = data_dir
        media_mod.MEDIA_DIR = os.path.join(DATA_DIR, "files", "aibot")
    CONFIG = load_config(DATA_DIR)
    if not CONFIG.get("bot_id") or not CONFIG.get("secret"):
        print("aibot.json missing bot_id/secret. "
              "Run 'loop_engine aibot config' first.")
        raise SystemExit(1)
    from wecom_server.router import configure_recent
    configure_recent(os.path.join(DATA_DIR, "recent.json"))
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("websockets not installed. Run 'pip install websockets'.")
        raise SystemExit(1)
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("cryptography not installed. Run 'pip install cryptography'.")
        raise SystemExit(1)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass
    port = int(CONFIG.get("port", 5099))
    try:
        httpd = _start_control(port)
    except OSError:
        print(f"aibot already running (port {port})")
        raise SystemExit(1)
    print(f"Starting aibot long connection (control port {port})...", flush=True)
    try:
        asyncio.run(_main(CONFIG))
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
