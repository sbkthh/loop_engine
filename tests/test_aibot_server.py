"""aibot transport: frame handling, queues, replies, control port."""
import asyncio
import json
import time

import pytest


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True


def _callback(msgid="m1", kind="single", chatid=None, userid="alice",
              msgtype="text", content="hi", req_id="r1", aibotid="bot1"):
    body = {
        "msgid": msgid,
        "aibotid": aibotid,
        "chattype": kind,
        "from": {"userid": userid},
        "msgtype": msgtype,
    }
    if chatid:
        body["chatid"] = chatid
    if msgtype == "text":
        body["text"] = {"content": content}
    return {"cmd": "aibot_msg_callback",
            "headers": {"req_id": req_id}, "body": body}


@pytest.fixture
def env(monkeypatch, tmp_path):
    from aibot_server import server

    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "CONFIG", {
        "bot_id": "bot1", "secret": "s", "receipt_enabled": False,
        "allowed_users": ["alice"]})
    monkeypatch.setattr(server, "_seen", {})
    monkeypatch.setattr(server, "_last_user_write", {})
    monkeypatch.setattr(server, "_queues", {})
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_ws", FakeWS())
    monkeypatch.setattr(server, "_connected", True)
    monkeypatch.setattr(server, "_fallback_replies", {})

    calls = {"count": 0}
    result = {"reply": "RESULT"}

    def fake_dispatch(message, registry, data_dir, user_id,
                      session_key=None, allowed_actions=None,
                      detect_text=None):
        calls.update(message=message, user_id=user_id,
                     session_key=session_key, allowed=allowed_actions,
                     detect_text=detect_text)
        calls["count"] += 1
        reply = result["reply"]
        if isinstance(reply, Exception):
            def boom():
                raise reply
            return boom
        return lambda: reply

    monkeypatch.setattr("wecom_server.router.dispatch", fake_dispatch)
    monkeypatch.setattr("registry.list_requirements", lambda: [])
    return server, calls, result, tmp_path


def _run(server, frame, wait=0.2):
    async def scenario():
        await server._handle_frame(frame)
        await asyncio.sleep(wait)
    asyncio.run(scenario())


def test_single_chat_uses_user_session_and_records_last_user(env):
    server, calls, _, tmp_path = env
    _run(server, _callback(kind="single", userid="alice"))

    assert calls["session_key"] == "aibot:u:alice"
    assert calls["allowed"] is None
    assert calls["user_id"] == "alice"
    last = json.loads((tmp_path / "last_user.json").read_text())
    assert last == {"user": "alice", "platform": "aibot",
                    "chatid": "alice", "chattype": "single"}


def test_group_chat_uses_group_session_and_readonly_actions(env):
    server, calls, _, tmp_path = env
    _run(server, _callback(kind="group", chatid="wr123", userid="bob"))

    assert calls["session_key"] == "aibot:g:wr123"
    assert calls["allowed"] == server.GROUP_READONLY_ACTIONS
    assert calls["user_id"] == "bob"
    assert not (tmp_path / "last_user.json").exists()


def test_duplicate_msgid_dispatched_once(env):
    server, calls, _, _ = env
    frame = _callback(msgid="dup")
    _run(server, frame)
    _run(server, frame)
    assert calls["count"] == 1


def test_unsupported_message_type_replies_without_dispatch(env):
    server, calls, _, _ = env
    _run(server, _callback(msgtype="voice"))

    assert calls["count"] == 0
    assert server._ws.sent
    frame = server._ws.sent[0]
    assert frame["cmd"] == "aibot_respond_msg"
    assert "暂不支持" in frame["body"]["stream"]["content"]


def test_group_mention_only_is_ignored(env):
    server, calls, _, _ = env
    _run(server, _callback(kind="group", chatid="wr1",
                           content="@机器人\u2005"))
    assert calls["count"] == 0
    assert server._ws.sent == []


def test_single_empty_text_asks_to_resend(env):
    server, calls, _, _ = env
    _run(server, _callback(kind="single", content=""))

    assert calls["count"] == 0
    assert "未识别到文本" in server._ws.sent[0]["body"]["stream"]["content"]


def test_self_and_foreign_bot_messages_ignored(env):
    server, calls, _, _ = env
    _run(server, _callback(userid="bot1"))
    _run(server, _callback(msgid="m2", aibotid="other-bot"))
    assert calls["count"] == 0


def test_ack_then_final_update_same_stream(env, monkeypatch):
    server, calls, _, _ = env
    server.CONFIG["receipt_enabled"] = True
    _run(server, _callback(msgid="m7"), wait=0.3)

    frames = server._ws.sent
    ack = frames[0]
    assert ack["cmd"] == "aibot_respond_msg"
    assert ack["body"]["stream"]["id"] == "reply_m7"
    assert ack["body"]["stream"]["finish"] is False

    final = frames[-1]
    assert final["cmd"] == "aibot_respond_msg"
    assert final["headers"]["req_id"] == "r1"
    # same stream id: WeCom updates the quoted reply bubble, not a new message
    assert final["body"]["stream"]["id"] == ack["body"]["stream"]["id"]
    assert final["body"]["stream"]["finish"] is True
    assert final["body"]["stream"]["content"] == "RESULT"


def test_reply_fits_in_one_quoted_stream(env):
    server, calls, result, _ = env
    result["reply"] = "中" * 2000
    _run(server, _callback(msgid="m9"), wait=0.3)

    quoted = [f for f in server._ws.sent
              if f["cmd"] == "aibot_respond_msg"
              and f["headers"]["req_id"] == "r1"]
    assert len(quoted) == 1
    assert quoted[0]["body"]["stream"]["id"] == "reply_m9"
    assert quoted[0]["body"]["stream"]["finish"] is True
    assert quoted[0]["body"]["stream"]["content"] == "中" * 2000
    assert not [f for f in server._ws.sent if f["cmd"] == "aibot_send_msg"]


def test_oversized_final_reply_quotes_first_segment_and_pushes_rest(env):
    server, calls, result, _ = env
    result["reply"] = "中" * 8000  # 24000 bytes > 20000 stream cap
    _run(server, _callback(msgid="m10"), wait=0.3)

    quoted = [f for f in server._ws.sent
              if f["cmd"] == "aibot_respond_msg"
              and f["headers"]["req_id"] == "r1"]
    assert len(quoted) == 1
    assert quoted[0]["body"]["stream"]["id"] == "reply_m10"
    assert len(quoted[0]["body"]["stream"]["content"].encode("utf-8")) <= 20000

    pushed = [f for f in server._ws.sent if f["cmd"] == "aibot_send_msg"]
    assert pushed
    assert all(len(f["body"]["markdown"]["content"].encode("utf-8")) <= 1800
               for f in pushed)


def test_stale_stream_pushes_proactively(env, monkeypatch):
    """A reply that outlives the stream window must not update the expired
    bubble (WeCom 846608); it is pushed proactively instead."""
    server, calls, _, _ = env
    server.CONFIG["receipt_enabled"] = True
    monkeypatch.setattr(server, "STREAM_MAX_AGE", 0)
    _run(server, _callback(msgid="m11"), wait=0.3)

    finished = [f for f in server._ws.sent
                if f["cmd"] == "aibot_respond_msg"
                and f["body"]["stream"]["finish"]]
    assert not finished, "expired stream must not receive the final reply"
    pushed = [f for f in server._ws.sent if f["cmd"] == "aibot_send_msg"]
    assert pushed and pushed[-1]["body"]["markdown"]["content"] == "RESULT"


def test_rejected_stream_update_falls_back_to_push(env):
    """A late server error frame (stream expired) pushes the remembered
    reply proactively instead of losing it."""
    server, calls, _, _ = env

    async def scenario():
        server._fallback_replies["r9"] = ("alice", "回退内容", time.time())
        await server._handle_error_frame({
            "headers": {"req_id": "r9"}, "errcode": 846608,
            "errmsg": "stream message update expired (>10 minutes)"})

    asyncio.run(scenario())
    pushed = [f for f in server._ws.sent if f["cmd"] == "aibot_send_msg"]
    assert pushed and pushed[-1]["body"]["markdown"]["content"] == "回退内容"
    assert "r9" not in server._fallback_replies


def test_error_frame_without_stream_does_not_push(env):
    server, calls, _, _ = env

    asyncio.run(server._handle_error_frame({
        "headers": {"req_id": "unknown"}, "errcode": 846608,
        "errmsg": "expired"}))
    assert not [f for f in server._ws.sent if f["cmd"] == "aibot_send_msg"]


def test_worker_exception_is_reported_and_loop_survives(env, monkeypatch):
    server, calls, result, _ = env
    result["reply"] = RuntimeError("boom")
    _run(server, _callback(msgid="m8"), wait=0.3)

    pushed = [f for f in server._ws.sent
              if f["cmd"] == "aibot_respond_msg"
              and f["headers"]["req_id"] == "r1"]
    assert pushed and "处理失败" in pushed[-1]["body"]["stream"]["content"]


def test_queue_full_sends_busy_note(env):
    server, calls, _, _ = env

    async def scenario():
        q = asyncio.Queue(maxsize=3)
        for _ in range(3):
            q.put_nowait((lambda: "x", "alice"))
        server._queues["single:alice"] = q
        await server._enqueue("single", "alice", lambda: "x")

    asyncio.run(scenario())
    assert calls["count"] == 0
    pushed = [f for f in server._ws.sent if f["cmd"] == "aibot_send_msg"]
    assert pushed and "上一条还在处理中" in pushed[-1]["body"]["markdown"]["content"]


def test_quote_is_prepended_to_current_message(env):
    server, calls, _, _ = env
    frame = _callback(msgid="q1", content="继续")
    frame["body"]["quote"] = {
        "msgtype": "text", "text": {"content": "原问题"}}
    _run(server, frame)

    message = calls["message"]
    assert "[引用消息]" in message
    assert message.index("原问题") < message.index("继续")
    assert calls["detect_text"] == "继续"


def test_group_quote_with_only_mention_still_dispatches(env):
    server, calls, _, _ = env
    frame = _callback(msgid="q2", kind="group", chatid="wr1",
                      content="@机器人\u2005")
    frame["body"]["quote"] = {
        "msgtype": "text", "text": {"content": "被引用的内容"}}
    _run(server, frame)

    assert "被引用的内容" in calls["message"]


def test_image_message_dispatches_with_media_fragment(env, monkeypatch):
    server, calls, _, _ = env
    server.CONFIG["receipt_enabled"] = True
    monkeypatch.setattr(
        server, "prepare_media",
        lambda item, msgid: f"[图片] {item['kind']} {msgid}")
    frame = _callback(msgid="img1", msgtype="image")
    frame["body"]["image"] = {"url": "https://x/a.png", "aeskey": "k"}
    _run(server, frame, wait=0.3)

    assert "[图片] image img1" in calls["message"]
    frames = server._ws.sent
    assert frames[0]["body"]["stream"]["id"] == "reply_img1"
    assert frames[-1]["cmd"] == "aibot_respond_msg"
    assert frames[-1]["headers"]["req_id"] == "r1"
    assert frames[-1]["body"]["stream"]["id"] == "reply_img1"


def test_file_message_dispatches_with_media_fragment(env, monkeypatch):
    server, calls, _, _ = env
    monkeypatch.setattr(
        server, "prepare_media",
        lambda item, msgid: f"[文件] {item['kind']} {msgid}")
    frame = _callback(msgid="file1", msgtype="file")
    frame["body"]["file"] = {"url": "https://x/a.txt", "aeskey": "k"}
    _run(server, frame)

    assert "[文件] file file1" in calls["message"]


def test_mixed_text_and_image_are_combined(env, monkeypatch):
    server, calls, _, _ = env
    monkeypatch.setattr(
        server, "prepare_media",
        lambda item, msgid: f"[图片] {msgid}")
    frame = _callback(msgid="mix1", msgtype="mixed")
    frame["body"]["mixed"] = {"msg_item": [
        {"msgtype": "text", "text": {"content": "看看这个"}},
        {"msgtype": "image", "image": {"url": "https://x/a.png"}},
    ]}
    _run(server, frame)

    assert "看看这个" in calls["message"]
    assert "[图片] mix1" in calls["message"]


def test_group_image_without_text_is_dispatched_readonly(env, monkeypatch):
    server, calls, _, _ = env
    monkeypatch.setattr(
        server, "prepare_media",
        lambda item, msgid: f"[图片] {msgid}")
    frame = _callback(msgid="img2", kind="group", chatid="wr1",
                      userid="bob", msgtype="image",
                      content="@机器人\u2005")
    frame["body"]["image"] = {"url": "https://x/a.png"}
    _run(server, frame)

    assert calls["count"] == 1
    assert calls["allowed"] == server.GROUP_READONLY_ACTIONS
    assert "[图片] img2" in calls["message"]


def test_media_failure_finishes_reply_and_skips_dispatch(env, monkeypatch):
    server, calls, _, _ = env

    def boom(item, msgid):
        raise ValueError("附件解密失败")

    monkeypatch.setattr(server, "prepare_media", boom)
    frame = _callback(msgid="img3", msgtype="image")
    frame["body"]["image"] = {"url": "https://x/a.png"}
    _run(server, frame)

    assert calls["count"] == 0
    replies = [f for f in server._ws.sent if f["cmd"] == "aibot_respond_msg"]
    assert replies and "附件处理失败" in replies[-1]["body"]["stream"]["content"]
    assert replies[-1]["body"]["stream"]["finish"] is True


def test_image_message_uses_real_media_pipeline(env, monkeypatch):
    """Server -> media -> filesystem integration (download faked only)."""
    import base64
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from aibot_server import media

    server, calls, _, tmp_path = env
    key = bytes(range(32))
    plain = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    pad = 32 - len(plain) % 32
    padded = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = enc.update(padded) + enc.finalize()

    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(
        media, "download",
        lambda url, max_bytes=media.MAX_BYTES:
        (encrypted, {"Content-Disposition": 'filename="shot.png"'}))
    frame = _callback(msgid="imgreal", msgtype="image")
    frame["body"]["image"] = {
        "url": "https://x/a", "aeskey": base64.b64encode(key).decode()}
    _run(server, frame, wait=0.3)

    assert "图片已保存到：" in calls["message"]
    path = calls["message"].split("图片已保存到：", 1)[1].splitlines()[0]
    assert path.startswith(str(tmp_path))
    assert open(path, "rb").read() == plain


def test_subscribe_error_detected_without_cmd():
    from aibot_server import server
    frame = {"headers": {"req_id": "aibot_subscribe_123"},
             "errcode": 853000, "errmsg": "invalid bot_id or secret"}
    assert server._is_subscribe_frame(frame) is True
    assert server._is_subscribe_frame(
        {"headers": {"req_id": "msg_1"}, "cmd": "aibot_msg_callback"}) is False


def test_group_readonly_set_matches_router():
    from aibot_server import server
    from wecom_server import router
    assert server.GROUP_READONLY_ACTIONS == router._GROUP_READONLY_ACTIONS


def test_control_health_and_validation(env):
    import http.client
    server, _, _, _ = env
    httpd = server._start_control(0)
    port = httpd.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read())["connected"] is True

        conn.request("POST", "/notify", body=b"{}",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

        with pytest.raises(OSError):
            server._start_control(port)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_non_owner_single_chat_is_readonly(env):
    server, calls, _, tmp_path = env
    _run(server, _callback(msgid="ro1", userid="mallory"))

    assert calls["count"] == 1
    assert calls["allowed"] == server.GROUP_READONLY_ACTIONS
    assert not (tmp_path / "last_user.json").exists()


def test_missing_allowlist_makes_every_single_chat_readonly(env, monkeypatch):
    server, calls, _, _ = env
    monkeypatch.setitem(server.CONFIG, "allowed_users", [])
    _run(server, _callback(msgid="ro2", userid="alice"))
    assert calls["allowed"] == server.GROUP_READONLY_ACTIONS


def test_owner_single_chat_is_writable_and_records_last_user(env):
    server, calls, _, tmp_path = env
    _run(server, _callback(msgid="ok1", userid="alice"))

    assert calls["allowed"] is None
    data = json.loads((tmp_path / "last_user.json").read_text())
    assert data["user"] == "alice"
    assert data["chattype"] == "single"


def test_group_chat_never_records_last_user(env):
    server, calls, _, tmp_path = env
    _run(server, _callback(msgid="grp1", kind="group", chatid="wr1",
                           userid="bob"))

    assert calls["allowed"] == server.GROUP_READONLY_ACTIONS
    assert not (tmp_path / "last_user.json").exists()


def test_subscribe_error_frame_raises(env):
    server, _, _, _ = env
    with pytest.raises(RuntimeError):
        asyncio.run(server._handle_error_frame({
            "headers": {"req_id": "aibot_subscribe_123"},
            "errcode": 853000, "errmsg": "invalid bot_id or secret"}))
