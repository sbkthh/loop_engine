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
        "bot_id": "bot1", "secret": "s", "receipt_enabled": False})
    monkeypatch.setattr(server, "_seen", {})
    monkeypatch.setattr(server, "_last_user_write", {})
    monkeypatch.setattr(server, "_queues", {})
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_ws", FakeWS())
    monkeypatch.setattr(server, "_connected", True)

    calls = {"count": 0}
    result = {"reply": "RESULT"}

    def fake_dispatch(message, registry, data_dir, user_id,
                      session_key=None, allowed_actions=None):
        calls.update(message=message, user_id=user_id,
                     session_key=session_key, allowed=allowed_actions)
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
    last = json.loads((tmp_path / "last_user.json").read_text())
    assert last["chatid"] == "wr123"
    assert last["chattype"] == "group"
    assert last["user"] == "bob"


def test_duplicate_msgid_dispatched_once(env):
    server, calls, _, _ = env
    frame = _callback(msgid="dup")
    _run(server, frame)
    _run(server, frame)
    assert calls["count"] == 1


def test_unsupported_message_type_replies_without_dispatch(env):
    server, calls, _, _ = env
    _run(server, _callback(msgtype="image"))

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


def test_ack_then_final_push(env, monkeypatch):
    server, calls, _, _ = env
    server.CONFIG["receipt_enabled"] = True
    _run(server, _callback(msgid="m7"), wait=0.3)

    frames = server._ws.sent
    assert frames[0]["cmd"] == "aibot_respond_msg"
    assert frames[0]["body"]["stream"]["id"] == "ack_m7"
    assert frames[-1]["cmd"] == "aibot_send_msg"
    assert frames[-1]["body"]["chatid"] == "alice"
    assert frames[-1]["body"]["markdown"]["content"] == "RESULT"


def test_worker_exception_is_reported_and_loop_survives(env, monkeypatch):
    server, calls, result, _ = env
    result["reply"] = RuntimeError("boom")
    _run(server, _callback(msgid="m8"), wait=0.3)

    pushed = [f for f in server._ws.sent if f["cmd"] == "aibot_send_msg"]
    assert pushed and "处理失败" in pushed[-1]["body"]["markdown"]["content"]


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
