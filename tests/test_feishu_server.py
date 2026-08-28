"""Tests for Feishu long-connection server: event handling, dedup, dispatch."""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from feishu_server import server


@pytest.fixture(autouse=True)
def _server_env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "CONFIG", {"app_id": "cli_a", "app_secret": "s"})
    monkeypatch.setattr(server, "_seen_events", {})
    monkeypatch.setattr(server, "_pending", {})
    yield


def _msg_event(text, open_id="ou_abc", event_id="evt-1"):
    return {
        "header": {"event_id": event_id,
                   "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {"message_type": "text",
                        "content": json.dumps({"text": text})},
        },
    }


def _wire_dispatch_and_push(monkeypatch, dispatch):
    pushed = threading.Event()
    pushes = []
    monkeypatch.setattr("wecom_server.router.dispatch", dispatch)
    monkeypatch.setattr("registry.list_requirements", lambda: [])

    def send_text(user, content, config):
        pushes.append((user, content))
        pushed.set()
        return True
    monkeypatch.setattr(server, "send_text", send_text)
    return pushed, pushes


def test_text_event_dispatch_and_push(monkeypatch, tmp_path):
    def dispatch(content, reg, data_dir, user):
        return "hello reply"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    server._handle_event(_msg_event("hi"))
    assert pushed.wait(timeout=2)
    assert pushes == [("ou_abc", "hello reply")]
    with open(os.path.join(str(tmp_path), "last_user.json")) as f:
        assert json.load(f) == {"user": "ou_abc", "platform": "feishu"}


def test_async_callable_queued_and_pushed(monkeypatch):
    def dispatch(content, reg, data_dir, user):
        def fn():
            return "async result"
        fn.requirement = "req1"
        return fn
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    server._handle_event(_msg_event("hi"))
    assert pushed.wait(timeout=2)
    assert pushes == [("ou_abc", "async result")]


def test_duplicate_event_processed_once(monkeypatch):
    count = {"n": 0}

    def dispatch(content, reg, data_dir, user):
        count["n"] += 1
        return "ok"
    pushed, _ = _wire_dispatch_and_push(monkeypatch, dispatch)

    server._handle_event(_msg_event("hi"))
    server._handle_event(_msg_event("hi"))
    assert pushed.wait(timeout=2)
    time.sleep(0.2)
    assert count["n"] == 1


def test_non_text_message_ignored(monkeypatch):
    def dispatch(*a, **k):
        raise AssertionError("should not dispatch")
    _wire_dispatch_and_push(monkeypatch, dispatch)
    payload = {
        "header": {"event_id": "evt-img",
                   "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {"message_type": "image", "content": "{}"},
        },
    }
    server._handle_event(payload)


def test_on_message_translates_sdk_event(monkeypatch):
    """SDK typed object → dict payload → handler gets text + open_id."""
    class _NS:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    data = _NS(
        header=_NS(event_id="evt-sdk", event_type="im.message.receive_v1"),
        event=_NS(
            sender=_NS(sender_id=_NS(open_id="ou_sdk")),
            message=_NS(message_type="text",
                        content=json.dumps({"text": "via sdk"})),
        ),
    )

    def dispatch(content, reg, data_dir, user):
        return "sdk reply"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    server._on_message(data)
    assert pushed.wait(timeout=2)
    assert pushes == [("ou_sdk", "sdk reply")]


def test_on_message_survives_malformed_event(monkeypatch):
    class _NS:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    data = _NS(header=None, event=None)
    monkeypatch.setattr(server, "send_text",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("should not push")))
    server._on_message(data)  # must not raise


def test_submit_async_full_queue_pushes_busy_note(monkeypatch):
    import queue as queue_mod
    q = queue_mod.Queue(maxsize=1)
    monkeypatch.setattr(server, "_queue_for", lambda req: q)
    pushes = []
    monkeypatch.setattr(server, "send_text",
                        lambda u, c, cfg: pushes.append(c) or True)

    def fn():
        return "ok"
    fn.requirement = "req1"
    assert server._submit_async(fn, "ou_1", {}) is None
    server._submit_async(fn, "ou_1", {})
    assert "上一条还在处理中" in pushes[0]
