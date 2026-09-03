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
    monkeypatch.setattr(server, "CONFIG",
                        {"app_id": "cli_a", "app_secret": "s",
                         "receipt_enabled": False, "file_wait_seconds": 0.3})
    monkeypatch.setattr(server, "_seen_events", {})
    monkeypatch.setattr(server, "_pending", {})
    monkeypatch.setattr(server, "_last_user_write", {})
    for entry in getattr(server, "_file_buffer", {}).values():
        if entry.get("timer"):
            entry["timer"].cancel()
    monkeypatch.setattr(server, "_file_buffer", {})
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


def _file_event(file_name="prd.pdf", open_id="ou_abc", event_id="evt-file"):
    return {
        "header": {"event_id": event_id,
                   "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {"message_type": "file", "message_id": "om_1",
                        "content": json.dumps({"file_key": "fk",
                                               "file_name": file_name})},
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


def test_receipt_pushed_before_reply_by_default(monkeypatch):
    """Missing receipt_enabled key defaults to on; receipt precedes reply."""
    monkeypatch.setattr(server, "CONFIG", {"app_id": "a", "app_secret": "s"})
    pushed, pushes = _wire_dispatch_and_push(
        monkeypatch, lambda *a, **k: "reply")

    server._handle_event(_msg_event("hi"))
    assert pushed.wait(timeout=2)
    for _ in range(20):
        if len(pushes) >= 2:
            break
        time.sleep(0.1)
    assert pushes[0] == ("ou_abc", "已收到，正在处理…")
    assert pushes[1] == ("ou_abc", "reply")


def test_receipt_disabled_by_config(monkeypatch):
    monkeypatch.setattr(server, "CONFIG",
                        {"app_id": "a", "app_secret": "s",
                         "receipt_enabled": False})
    pushed, pushes = _wire_dispatch_and_push(
        monkeypatch, lambda *a, **k: "reply")

    server._handle_event(_msg_event("hi"))
    assert pushed.wait(timeout=2)
    assert pushes == [("ou_abc", "reply")]


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


def test_file_message_downloaded_and_dispatched(monkeypatch, tmp_path):
    saved = str(tmp_path / "prd.pdf")
    monkeypatch.setattr(server, "download_file",
                        lambda mid, fk, cfg: saved)
    seen = {}

    def dispatch(content, reg, data_dir, user):
        seen["content"] = content
        return "file reply"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    payload = {
        "header": {"event_id": "evt-file",
                   "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {"message_type": "file", "message_id": "om_1",
                        "content": json.dumps({"file_key": "fk",
                                               "file_name": "prd.pdf"})},
        },
    }
    server._handle_event(payload)
    assert pushed.wait(timeout=2)
    assert saved in seen["content"] and "prd.pdf" in seen["content"]
    assert pushes == [("ou_abc", "file reply")]


def test_post_message_text_plus_file(monkeypatch, tmp_path):
    saved = str(tmp_path / "prd.md")
    monkeypatch.setattr(server, "download_file",
                        lambda mid, fk, cfg: saved)
    seen = {}

    def dispatch(content, reg, data_dir, user):
        seen["content"] = content
        return "post reply"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    payload = {
        "header": {"event_id": "evt-post",
                   "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {"message_type": "post", "message_id": "om_9",
                        "content": json.dumps({"post": {"zh_cn": {
                            "content": [[
                                {"tag": "text", "text": "这是PRD，注册新需求"},
                                {"tag": "file", "file_key": "fk",
                                 "file_name": "prd.md"},
                            ]]}}})},
        },
    }
    server._handle_event(payload)
    assert pushed.wait(timeout=2)
    assert "用户说：这是PRD，注册新需求" in seen["content"]
    assert saved in seen["content"] and "prd.md" in seen["content"]
    assert pushes == [("ou_abc", "post reply")]


def test_post_message_text_only(monkeypatch):
    seen = {}

    def dispatch(content, reg, data_dir, user):
        seen["content"] = content
        return "ok"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    payload = {
        "header": {"event_id": "evt-post2",
                   "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {"message_type": "post", "message_id": "om_9",
                        "content": json.dumps({"post": {"zh_cn": {
                            "content": [[
                                {"tag": "text", "text": "纯富文本"},
                                {"tag": "a", "text": "链接",
                                 "href": "https://x"},
                            ]]}}})},
        },
    }
    server._handle_event(payload)
    assert pushed.wait(timeout=2)
    assert seen["content"] == "用户说：纯富文本 链接(https://x)"


def test_parse_post_extracts_text_and_files():
    text, files = server._parse_post({"post": {"zh_cn": {"content": [
        [{"tag": "text", "text": "a"}, {"tag": "at", "user_id": "u"}],
        [{"tag": "file", "file_key": "k1", "file_name": "f1"},
         {"tag": "file", "file_key": "k2", "file_name": "f2"}],
    ]}}})
    assert text == "a"
    assert files == [("k1", "f1"), ("k2", "f2")]


def test_file_download_failure_pushes_error(monkeypatch):
    monkeypatch.setattr(server, "download_file", lambda *a: None)
    pushed, pushes = _wire_dispatch_and_push(
        monkeypatch,
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not dispatch")))

    payload = {
        "header": {"event_id": "evt-file2",
                   "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {"message_type": "file", "message_id": "om_1",
                        "content": json.dumps({"file_key": "fk",
                                               "file_name": "x.pdf"})},
        },
    }
    server._handle_event(payload)
    assert pushed.wait(timeout=2)
    assert "下载失败" in pushes[0][1]


def test_on_message_translates_sdk_event(monkeypatch):
    """SDK typed object → dict payload → handler gets text + open_id."""
    class _NS:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    data = _NS(
        header=_NS(event_id="evt-sdk", event_type="im.message.receive_v1"),
        event=_NS(
            sender=_NS(sender_id=_NS(open_id="ou_sdk")),
            message=_NS(message_type="text", message_id="om_sdk",
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


def test_file_then_text_merges_into_one_prompt(monkeypatch, tmp_path):
    """Long window: text arriving in-window merges with the buffered file
    into a single dispatch; the file is never processed on its own."""
    monkeypatch.setattr(server, "CONFIG",
                        {"app_id": "a", "app_secret": "s",
                         "receipt_enabled": False, "file_wait_seconds": 5})
    saved = str(tmp_path / "prd.pdf")
    monkeypatch.setattr(server, "download_file", lambda mid, fk, cfg: saved)
    seen = []

    def dispatch(content, reg, data_dir, user):
        seen.append(content)
        return "reply"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    server._handle_event(_file_event(file_name="prd.pdf"))
    time.sleep(0.3)  # let the download+buffer thread populate
    server._handle_event(_msg_event("注册成需求", event_id="evt-txt"))

    assert pushed.wait(timeout=3)
    assert len(seen) == 1
    assert "用户说：注册成需求" in seen[0]
    assert saved in seen[0] and "prd.pdf" in seen[0]
    assert pushes == [("ou_abc", "reply")]


def test_file_without_text_flushes_alone_after_window(monkeypatch, tmp_path):
    """No text in-window: the buffered file dispatches alone with the
    fallback instruction and no '用户说' line."""
    saved = str(tmp_path / "prd.pdf")
    monkeypatch.setattr(server, "download_file", lambda mid, fk, cfg: saved)
    seen = []

    def dispatch(content, reg, data_dir, user):
        seen.append(content)
        return "reply"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    server._handle_event(_file_event(file_name="prd.pdf"))
    assert pushed.wait(timeout=3)
    assert len(seen) == 1
    assert "用户说" not in seen[0]
    assert saved in seen[0] and "prd.pdf" in seen[0]
    assert "请读取文件内容" in seen[0]
    assert pushes == [("ou_abc", "reply")]


def test_file_wait_zero_processes_immediately(monkeypatch, tmp_path):
    """file_wait_seconds<=0 disables buffering: the file is processed alone
    right away instead of waiting out a (here absurd) 30s window."""
    monkeypatch.setattr(server, "CONFIG",
                        {"app_id": "a", "app_secret": "s",
                         "receipt_enabled": False, "file_wait_seconds": 0})
    saved = str(tmp_path / "prd.pdf")
    monkeypatch.setattr(server, "download_file", lambda mid, fk, cfg: saved)
    seen = []

    def dispatch(content, reg, data_dir, user):
        seen.append(content)
        return "reply"
    pushed, pushes = _wire_dispatch_and_push(monkeypatch, dispatch)

    server._handle_event(_file_event(file_name="prd.pdf"))
    assert pushed.wait(timeout=3)
    assert len(seen) == 1
    assert saved in seen[0] and "请读取文件内容" in seen[0]
    assert pushes == [("ou_abc", "reply")]
