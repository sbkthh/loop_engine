"""Router widening for aibot: conversation-scoped sessions + group read-only."""
import pytest


@pytest.fixture(autouse=True)
def _clean_recent(monkeypatch):
    from wecom_server import router
    monkeypatch.setattr(router, "_recent_activity", {})
    yield


def test_session_key_isolates_conversations(tmp_path, monkeypatch):
    from wecom_server import router
    monkeypatch.setattr(router, "_SESSION_DIR", str(tmp_path))

    a, a_new = router._get_session_id("alice", "req", "aibot:g:wr1")
    b, b_new = router._get_session_id("alice", "req", "aibot:g:wr2")
    assert a_new and b_new and a != b
    assert (tmp_path / "aibot_g_wr1__req.txt").exists()
    assert (tmp_path / "aibot_g_wr2__req.txt").exists()


def test_session_key_default_keeps_legacy_filename(tmp_path, monkeypatch):
    from wecom_server import router
    monkeypatch.setattr(router, "_SESSION_DIR", str(tmp_path))

    sid, is_new = router._get_session_id("alice", "global")
    assert is_new
    assert (tmp_path / "alice__global.txt").read_text() == sid


def test_recent_requirement_isolated_by_session_key():
    from wecom_server import router
    router._touch_recent("alice", "reqA", "aibot:g:wr1")

    assert router._recent_requirement("alice", "aibot:g:wr1") == "reqA"
    assert router._recent_requirement("alice", "aibot:g:wr2") is None
    assert router._recent_requirement("alice") is None


def test_group_readonly_blocks_approve(monkeypatch):
    from wecom_server import router
    called = []
    monkeypatch.setattr(router, "_execute_approve",
                        lambda *a, **k: called.append(a) or "approved")

    reply = router._dispatch_json_action(
        {"action": "approve", "requirement": "req"}, [], "/tmp", "u1",
        router._GROUP_READONLY_ACTIONS)

    assert "群聊只读" in reply
    assert called == []


def test_group_readonly_allows_history(monkeypatch):
    from wecom_server import router
    monkeypatch.setattr(router, "_execute_history",
                        lambda *a, **k: "hist ok")

    reply = router._dispatch_json_action(
        {"action": "history", "requirement": "ALL"}, [], "/tmp", "u1",
        router._GROUP_READONLY_ACTIONS)

    assert reply == "hist ok"


def test_default_allowlist_is_unrestricted(monkeypatch):
    from wecom_server import router
    called = []
    monkeypatch.setattr(router, "_approve_prefix_block", lambda *a: None)
    monkeypatch.setattr(router, "_execute_approve",
                        lambda *a, **k: called.append(a) or "approved")

    reply = router._dispatch_json_action(
        {"action": "approve", "requirement": "req"}, [], "/tmp", "u1")

    assert reply == "approved"
    assert called


def test_readonly_turn_adds_note_and_skips_correction(
        tmp_path, monkeypatch):
    from wecom_server import router
    prompts = []
    processed = {}

    monkeypatch.setattr(router, "_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr(router, "_detect_requirement", lambda m, r: "req")
    monkeypatch.setattr(router, "_system_state_snapshot", lambda r: "")
    monkeypatch.setattr(router, "_chat_audit_settings",
                        lambda readonly=False: processed.__setitem__(
                            "readonly", readonly))
    monkeypatch.setattr(router, "_run_llm_turn",
                        lambda sid, new, prompt, settings:
                        prompts.append(prompt) or "ok")
    monkeypatch.setattr(router, "_unregistered_edits",
                        lambda sid, reply: {"mod"})
    monkeypatch.setattr(router, "_process_llm_reply",
                        lambda *a, **k: processed.setdefault("reply", a))

    router._llm_dispatch("hi", [{"name": "req", "root": "/tmp"}], "/tmp",
                         "u1", "aibot:g:wr1",
                         router._GROUP_READONLY_ACTIONS)

    assert router._READONLY_NOTE in prompts[0]
    assert processed["readonly"] is True
    assert len(prompts) == 1, "correction loop must be skipped in readonly"
    assert processed["reply"][5] == "u1"
    assert processed["reply"][6] is router._GROUP_READONLY_ACTIONS


def test_readonly_audit_settings_include_guard():
    from wecom_server import router
    payload = router._audit_settings(readonly=True)
    assert "readonly_guard.sh" in payload
    assert "readonly_guard.sh" not in router._audit_settings(readonly=False)
