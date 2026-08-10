"""Tests for router async dispatch (all messages go through LLM)."""
import sys
import os
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server.router import dispatch


def test_dispatch_always_returns_callable():
    """All messages return callable (async LLM path)."""
    assert callable(dispatch("查状态", [], "/tmp"))
    assert callable(dispatch("批准", [], "/tmp"))
    assert callable(dispatch("随便说点什么", [], "/tmp"))
    assert callable(dispatch("", [], "/tmp"))


def _fake_llm_reply(stdout):
    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(stdout=stdout, returncode=0)
    return fake_run


def test_approve_prefix_executes(monkeypatch):
    """__APPROVE__ prefix triggers real scheduler.approve + dispatch."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req\n好的，开始执行"))
    calls = {}
    monkeypatch.setattr(scheduler, "approve",
                        lambda name: calls.__setitem__("approve", name) or 1)
    monkeypatch.setattr(scheduler, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(scheduler, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(scheduler, "dispatch",
                        lambda entries, max_concurrency=2:
                        calls.__setitem__("entries", entries) or ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "已批准并开始执行" in reply
    assert calls["approve"] == "req"
    assert calls["entries"][0]["requirement"] == "req"


def test_approve_unknown_requirement(monkeypatch):
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ ghost"))

    fn = dispatch("批准 ghost", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_approve_report_only_returns_error(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req"))
    monkeypatch.setattr(scheduler, "approve",
                        lambda name: (_ for _ in ()).throw(
                            ValueError("report-only")))

    fn = dispatch("批准 req", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "无法批准" in reply


def _fake_runs():
    return {"runs": [
        {"requirement": "req", "end": "idle", "steps": 3,
         "duration_seconds": 10, "finished_at": "2026-08-10T12:00:00.000000"},
        {"requirement": "other", "end": "commit_error", "steps": 2,
         "duration_seconds": 5, "finished_at": "2026-08-10T13:00:00.000000"},
    ]}


def test_history_prefix_lists_runs(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__HISTORY__ req\n最近记录如下"))
    monkeypatch.setattr(scheduler, "load_runs", _fake_runs)

    fn = dispatch("查一下 req 的执行历史", [], "/tmp", "u1")
    reply = fn()

    assert "req" in reply and "idle" in reply
    assert "commit_error" not in reply  # other's runs filtered out


def test_history_prefix_all(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__HISTORY__ ALL\n总体情况"))
    monkeypatch.setattr(scheduler, "load_runs", _fake_runs)

    fn = dispatch("最近执行情况怎么样", [], "/tmp", "u1")
    reply = fn()

    assert "req" in reply and "other" in reply
    assert "最近 2 次执行" in reply


def test_history_empty(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__HISTORY__ ALL"))
    monkeypatch.setattr(scheduler, "load_runs", lambda: {"runs": []})

    fn = dispatch("最近执行情况", [], "/tmp", "u1")
    reply = fn()

    assert "暂无执行历史" in reply


def test_normal_reply_untouched(monkeypatch):
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("两个需求的当前状态如下…"))

    fn = dispatch("查状态", [], "/tmp", "u1")
    reply = fn()

    assert reply.startswith("两个需求")