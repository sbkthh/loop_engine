"""scheduler.notify_text routing to the aibot loopback control port."""
import json

import pytest


class _Resp:
    def __init__(self, status):
        self.status = status

    def read(self):
        return b"{}"


class FakeConn:
    instances = []
    status = 200
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = []
        FakeConn.instances.append(self)
        if FakeConn.fail:
            raise OSError("connection refused")

    def request(self, method, path, body=None, headers=None):
        self.requests.append({
            "method": method, "path": path,
            "body": json.loads(body) if body else None,
            "headers": headers,
        })

    def getresponse(self):
        return _Resp(FakeConn.status)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake(monkeypatch, tmp_path):
    import scheduler
    FakeConn.instances = []
    FakeConn.status = 200
    FakeConn.fail = False
    monkeypatch.setattr("http.client.HTTPConnection", FakeConn)
    monkeypatch.setattr(scheduler, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "_log", lambda message: None)
    (tmp_path / "aibot.json").write_text(json.dumps({
        "bot_id": "bot1", "secret": "s", "port": 5099}))
    return tmp_path, scheduler


def _last_user(tmp_path, **extra):
    data = {"user": "alice", "platform": "aibot"}
    data.update(extra)
    (tmp_path / "last_user.json").write_text(
        json.dumps(data, ensure_ascii=False))


def test_aibot_notify_posts_to_control_port(tmp_path, _fake):
    _last_user(tmp_path, chatid="wr123", chattype="group")
    scheduler = _fake[1]

    assert scheduler.notify_text("执行完成") is True

    conn = FakeConn.instances[0]
    assert (conn.host, conn.port) == ("127.0.0.1", 5099)
    req = conn.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/notify"
    assert req["body"] == {"chatid": "wr123", "content": "执行完成"}


def test_aibot_notify_falls_back_to_user_as_chatid(tmp_path, _fake):
    _last_user(tmp_path)
    scheduler = _fake[1]

    assert scheduler.notify_text("hello") is True
    assert FakeConn.instances[0].requests[0]["body"]["chatid"] == "alice"


def test_aibot_notify_without_config_skips(tmp_path, _fake):
    (tmp_path / "aibot.json").unlink()
    _last_user(tmp_path)
    scheduler = _fake[1]

    assert scheduler.notify_text("hello") is False
    assert FakeConn.instances == []


def test_aibot_notify_connection_failure_is_false(tmp_path, _fake):
    _last_user(tmp_path)
    FakeConn.fail = True
    scheduler = _fake[1]

    assert scheduler.notify_text("hello") is False


def test_aibot_notify_http_error_is_false(tmp_path, _fake):
    _last_user(tmp_path)
    FakeConn.status = 503
    scheduler = _fake[1]

    assert scheduler.notify_text("hello") is False
