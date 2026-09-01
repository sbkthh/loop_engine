"""Tests for router async dispatch (all messages go through LLM)."""
import datetime
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server.router import dispatch


@pytest.fixture(autouse=True)
def _isolate_recent_activity():
    """_recent_activity is a module global; clear it between tests so one
    test's touched requirement can't steer another's keyword-less reply."""
    from wecom_server import router
    router._recent_activity.clear()
    yield
    router._recent_activity.clear()


def test_dispatch_always_returns_callable():
    """All messages return callable (async LLM path)."""
    assert callable(dispatch("查状态", [], "/tmp"))
    assert callable(dispatch("批准", [], "/tmp"))
    assert callable(dispatch("随便说点什么", [], "/tmp"))
    assert callable(dispatch("", [], "/tmp"))


def test_system_prompt_has_prd_bootstrap_rules():
    """G prompt must reference skill files for PRD registration + bootstrap."""
    from wecom_server import router

    assert "requirement-register" in router._LLM_SYSTEM_PROMPT
    assert "spec-session" in router._LLM_SYSTEM_PROMPT
    assert "manual-loop" not in router._LLM_SYSTEM_PROMPT, \
        "manual-loop skill deprecated, prompt should not reference it"
    assert "__JSON_ACTION__" in router._LLM_SYSTEM_PROMPT
    assert "spec_result" in router._LLM_SYSTEM_PROMPT
    assert "MUST append __JSON_ACTION__" in router._LLM_SYSTEM_PROMPT, \
        "G must be required to emit spec_result in the SAME reply after editing spec.md"
    assert "spec_result MANDATORY" in router._LLM_SYSTEM_PROMPT, \
        "Actions list must mark spec_result as mandatory exception after spec edits"
    assert "Change boundary" in router._LLM_SYSTEM_PROMPT, \
        "G must know all code changes go through spec-session, no bugfix exception"
    assert "grill-me" in router._LLM_SYSTEM_PROMPT, \
        "G must drive the spec-session + grill-me flow itself, not bounce back"
    assert "one --print turn" in router._LLM_SYSTEM_PROMPT, \
        "G must know each message is one-shot; never launch background agents"
    assert "Bash|Edit|Write" in router._audit_settings(), \
        "G sessions must audit-trail every Edit/Write"


def _fake_llm_reply(stdout):
    def fake_run(cmd, **kwargs):
        if any("qodercli" in part for part in cmd):
            return types.SimpleNamespace(stdout=stdout, returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)
    return fake_run


def _fake_llm_sequence(replies):
    """fake_run returning replies in call order; records prompts."""
    state = {"inputs": [], "calls": 0}

    def fake_run(cmd, **kwargs):
        if any("qodercli" in part for part in cmd):
            state["inputs"].append(kwargs.get("input", ""))
            idx = state["calls"]
            state["calls"] += 1
            return types.SimpleNamespace(
                stdout=replies[min(idx, len(replies) - 1)], returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)
    return fake_run, state


def _seed_spec_snapshot(snap_dir, session_id, module, ts=None):
    snap_dir.mkdir(parents=True, exist_ok=True)
    if ts is None:
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = snap_dir / f"{ts}-{session_id}-{module}.md"
    path.write_text("# pre-edit")
    return str(path)


def test_recent_requirement_returns_most_recent(monkeypatch):
    """Most recently touched requirement wins for a user."""
    from wecom_server import router
    t = [1000.0]
    monkeypatch.setattr(router.time, "time", lambda: t[0])
    router._recent_activity.clear()
    router._touch_recent("u1", "reqA")
    t[0] = 1001.0
    router._touch_recent("u1", "reqB")
    t[0] = 1002.0
    assert router._recent_requirement("u1") == "reqB"


def test_recent_requirement_expires(monkeypatch):
    """Stale activity (older than _RECENT_WINDOW) is ignored."""
    from wecom_server import router
    t = [1000.0]
    monkeypatch.setattr(router.time, "time", lambda: t[0])
    router._recent_activity.clear()
    router._touch_recent("u1", "reqA")
    t[0] = 1000.0 + router._RECENT_WINDOW + 1
    assert router._recent_requirement("u1") is None


def test_recent_requirement_scoped_per_user(monkeypatch):
    """u1's recent activity does not leak to u2."""
    from wecom_server import router
    monkeypatch.setattr(router.time, "time", lambda: 1000.0)
    router._recent_activity.clear()
    router._touch_recent("u1", "reqA")
    assert router._recent_requirement("u2") is None


def test_system_state_snapshot_lists_partial_modules(tmp_path):
    """PARTIAL modules appear in the shared snapshot; SYNCED ones do not."""
    from wecom_server import router
    import json
    root = tmp_path / "proj"
    (root / ".loop").mkdir(parents=True)
    (root / ".loop" / "state.json").write_text(json.dumps({
        "modules": {
            "c/a": {"status": "PARTIAL"},
            "c/b": {"status": "SYNCED"},
        }
    }))
    snap = router._system_state_snapshot([{"name": "reqA", "root": str(root)}])
    assert "reqA" in snap
    assert "c/a:PARTIAL" in snap
    assert "c/b" not in snap


def test_system_state_snapshot_includes_pending(tmp_path, monkeypatch):
    """Pending entries carry requirement, trigger, approval state, modules."""
    from wecom_server import router
    import json
    import scheduler
    monkeypatch.setattr(scheduler, "PENDING_PATH", str(tmp_path / "pending.json"))
    (tmp_path / "pending.json").write_text(json.dumps({
        "pending": [{
            "requirement": "reqA",
            "trigger": "SPEC_CHANGED",
            "approved": False,
            "modules": [{"key": "c/a"}],
        }]
    }))
    snap = router._system_state_snapshot([])
    assert "待办 reqA" in snap
    assert "待批准" in snap
    assert "SPEC_CHANGED" in snap
    assert "c/a" in snap


def test_system_state_snapshot_all_synced(tmp_path, monkeypatch):
    """No pending work yields an explicit 'nothing pending' statement."""
    from wecom_server import router
    import scheduler
    monkeypatch.setattr(scheduler, "PENDING_PATH", str(tmp_path / "missing.json"))
    snap = router._system_state_snapshot([])
    assert "无待办变更" in snap
    assert "git" in snap, "must steer G away from git-history answers"


def test_dispatch_prompt_injects_state_snapshot(monkeypatch):
    """Every LLM dispatch prompt carries the shared system-state snapshot."""
    from wecom_server import router
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global":
                        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router, "_system_state_snapshot",
                        lambda registry: "\n\n【当前系统状态】FAKE-STATE")
    fake, state = _fake_llm_sequence(["普通回复"])
    monkeypatch.setattr(router.subprocess, "run", fake)
    fn = dispatch("查看 req 状态", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()
    assert "FAKE-STATE" in state["inputs"][0]


def test_unregistered_edits_missing_detection(monkeypatch, tmp_path):
    """Correction gap = edited modules minus registered spec_result modules;
    full registration yields an empty gap."""
    from wecom_server import router

    snap_dir = tmp_path / "snaps"
    _seed_spec_snapshot(snap_dir, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "m1")
    _seed_spec_snapshot(snap_dir, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "m2")
    monkeypatch.setattr(router, "_SPEC_SNAP_DIR", str(snap_dir))
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    missing = router._unregistered_edits(
        sid, '__JSON_ACTION__ {"action":"spec_result","requirement":"req",'
             '"module":"chg1/m1"}')
    assert missing == {"m2"}

    missing = router._unregistered_edits(
        sid, '__JSON_ACTION__ {"action":"spec_result","requirement":"req",'
             '"module":"chg1/m1"}\n'
             '__JSON_ACTION__ {"action":"spec_result","requirement":"req",'
             '"module":"m2"}')
    assert missing == set()


def test_keywordless_reply_routes_to_recent_requirement(monkeypatch):
    """A short grill-me answer with no keyword routes to the recently
    active requirement session; the LLM classifier is never invoked."""
    from wecom_server import router

    routed = {}
    monkeypatch.setattr(router, "_detect_requirement",
                        lambda msg, reg: None)
    monkeypatch.setattr(router, "_recent_requirement",
                        lambda uid: "reqA")
    monkeypatch.setattr(router, "_classify_requirement",
                        lambda msg, reg: routed.__setitem__("classified", True) or "x")
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global":
                        routed.__setitem__("req", req) or ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("【reqA】grill-me: 改动1 要加哪几列？"))

    fn = dispatch("加三列：planNo、confirmStatus",
                  [{"name": "reqA", "root": "/tmp/x"}], "/tmp", "u1")
    fn()
    assert routed.get("req") == "reqA"
    assert "classified" not in routed


def test_global_intent_message_stays_global(monkeypatch):
    """'看一下需求状态' is a cross-requirement question: it must NOT fall
    back to the recent requirement session (or the LLM classifier), so the
    reply prefix is 【通用】 instead of the last-touched requirement."""
    from wecom_server import router

    routed = {}
    monkeypatch.setattr(router, "_detect_requirement",
                        lambda msg, reg: None)
    monkeypatch.setattr(router, "_recent_requirement",
                        lambda uid: routed.__setitem__("recent", True) or "reqA")
    monkeypatch.setattr(router, "_classify_requirement",
                        lambda msg, reg: routed.__setitem__("classified", True) or "reqA")
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global":
                        routed.__setitem__("req", req) or ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("【通用】当前所有需求均已同步。"))

    fn = dispatch("看一下需求状态",
                  [{"name": "reqA", "root": "/tmp/x"}], "/tmp", "u1")
    fn()
    assert routed.get("req") == "global"
    assert "recent" not in routed
    assert "classified" not in routed


def test_global_intent_regex_does_not_catch_short_answers(monkeypatch):
    """Short grill-me answers and bare status questions (no requirement
    name) must keep routing to the recent requirement session."""
    from wecom_server import router

    for msg in ("可以", "改吧", "继续", "确认", "现在什么状态", "状态怎么样",
                "随便聊聊", "好"):
        assert not router._GLOBAL_INTENT_RE.search(msg), f"{msg!r} 不应是全局意图"
    for msg in ("看一下需求状态", "所有需求状态", "总览一下", "全部需求汇总",
                "各需求进度", "整体情况", "所有模块状态"):
        assert router._GLOBAL_INTENT_RE.search(msg), f"{msg!r} 应为全局意图"


def test_approve_prefix_executes(monkeypatch):
    """JSON approve action triggers real scheduler.approve + dispatch."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"req"}\n好的，开始执行'))
    calls = {}
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        calls.__setitem__("approve", name) or 1)
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"ghost"}'))

    fn = dispatch("批准 ghost", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_approve_prefix_passes_user_id(monkeypatch):
    """Approval records who initiated it so scheduler notifies that user."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"req"}'))
    calls = {}
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        calls.__setitem__("approved_by", approved_by) or 1)
    monkeypatch.setattr(scheduler, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(scheduler, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(scheduler, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert "已批准并开始执行" in reply
    assert calls["approved_by"] == "u1"


def test_approve_rejected_fast_via_status_table(monkeypatch, tmp_path):
    """DRAFT-only requirement: approve rejected via STATUS_TABLE
    before scheduler.approve is ever reached."""
    import json
    from wecom_server import router
    import scheduler

    root = tmp_path / "req"
    (root / ".loop").mkdir(parents=True)
    (root / ".loop" / "state.json").write_text(json.dumps({
        "modules": {"c/m": {"status": "DRAFT"}},
        "current": None,
    }))
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"req"}'))
    called = []
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        called.append(name) or 1)

    fn = dispatch("批准执行 req", [{"name": "req", "root": str(root)}],
                  "/tmp", "u1")
    reply = fn()

    assert "无法批准" in reply
    assert "DRAFT" not in reply
    assert not called


def test_approve_allows_ready_via_status_table(monkeypatch, tmp_path):
    """READY module passes the STATUS_TABLE prefix check and reaches
    scheduler.approve."""
    import json
    from wecom_server import router
    import scheduler

    root = tmp_path / "req"
    (root / ".loop").mkdir(parents=True)
    (root / ".loop" / "state.json").write_text(json.dumps({
        "modules": {"c/m": {"status": "READY"}},
        "current": None,
    }))
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"req"}'))
    called = []
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        called.append(name) or 1)
    monkeypatch.setattr(scheduler, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(scheduler, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(scheduler, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": str(root)}],
                  "/tmp", "u1")
    reply = fn()

    assert called == ["req"]
    assert "已批准并开始执行" in reply


def test_approve_allows_pending_spec_changed_via_status_table(monkeypatch, tmp_path):
    """NEEDS_REFINEMENT state.json + poll 已检测到 SPEC_CHANGED 条目：
    快速拦截放行，达到 scheduler.approve。"""
    import json
    from wecom_server import router
    import scheduler

    root = tmp_path / "req"
    (root / ".loop").mkdir(parents=True)
    (root / ".loop" / "state.json").write_text(json.dumps({
        "modules": {"c/m": {"status": "NEEDS_REFINEMENT"}},
        "current": None,
    }))
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"req"}'))
    called = []
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        called.append(name) or 1)
    monkeypatch.setattr(scheduler, "load_pending", lambda: {"pending": [
        {"requirement": "req", "trigger": "SPEC_CHANGED",
         "modules": [{"status": "PARTIAL"}]}]})
    monkeypatch.setattr(scheduler, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(scheduler, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": str(root)}],
                  "/tmp", "u1")
    reply = fn()

    assert called == ["req"]
    assert "已批准并开始执行" in reply


def test_approve_report_only_returns_error(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"req"}'))
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None: (_ for _ in ()).throw(
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"history","requirement":"req"}\n最近记录如下'))
    monkeypatch.setattr(scheduler, "load_runs", _fake_runs)

    fn = dispatch("查一下 req 的执行历史", [], "/tmp", "u1")
    reply = fn()

    assert "req" in reply and "idle" in reply
    assert "commit_error" not in reply  # other's runs filtered out


def test_history_prefix_all(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"history","requirement":"ALL"}\n总体情况'))
    monkeypatch.setattr(scheduler, "load_runs", _fake_runs)

    fn = dispatch("最近执行情况怎么样", [], "/tmp", "u1")
    reply = fn()

    assert "req" in reply and "other" in reply
    assert "最近 2 次执行" in reply


def test_history_empty(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"history","requirement":"ALL"}'))
    monkeypatch.setattr(scheduler, "load_runs", lambda: {"runs": []})

    fn = dispatch("最近执行情况", [], "/tmp", "u1")
    reply = fn()

    assert "暂无执行历史" in reply


def test_normal_reply_untouched(monkeypatch):
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("两个需求的当前状态如下…"))

    fn = dispatch("查状态", [], "/tmp", "u1")
    reply = fn()

    assert reply.startswith("两个需求")


def _make_spec_root(tmp_path, git=True):
    """Build a requirement root: one module in state.json + a spec.md.
    With git=True (default) the spec is committed so backup can fetch HEAD."""
    import subprocess
    from state import StateManager
    import spec_utils
    root = str(tmp_path)
    spec_dir = os.path.join(root, "openspec", "changes", "chg1", "specs", "m1")
    os.makedirs(spec_dir, exist_ok=True)
    spec_path = os.path.join(spec_dir, "spec.md")
    with open(spec_path, "w") as f:
        f.write("# v1")
    sm = StateManager(root)
    sm.init_state()
    st = sm.load()
    sm.add_module(st, "chg1/m1", "chg1", "m1",
                  spec_hash=spec_utils.compute_spec_hash(spec_path))
    sm.save(st)
    if git:
        # -c core.excludesfile=/dev/null: the user's global gitignore
        # excludes openspec/, which would otherwise untrack the spec
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(["git", "-C", root, "-c", "core.excludesfile=/dev/null",
                        "add", "-A"], check=True)
        subprocess.run(["git", "-C", root, "-c", "core.excludesfile=/dev/null",
                        "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "v1"],
                       check=True)
    return root, spec_path


def test_spec_result_registers_change(monkeypatch, tmp_path):
    """spec_result action verifies + backs up + updates hash/status to PARTIAL."""
    import subprocess as sp
    from wecom_server import router
    from state import StateManager
    import spec_utils

    root, spec_path = _make_spec_root(tmp_path)
    with open(spec_path, "w") as f:
        f.write("# v2 changed")

    real_run = sp.run

    def fake_run(cmd, **kwargs):
        if any("qodercli" in part for part in cmd):
            return types.SimpleNamespace(
                stdout='__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}\n已修改', returncode=0)
        return real_run(cmd, **kwargs)  # git show must run for real

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run", fake_run)
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("修改一下 chg1/m1 的 spec", [{"name": "req", "root": root}],
                  "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply and "批准执行" in reply
    st = StateManager(root).load()
    mod = st["modules"]["chg1/m1"]
    assert mod["status"] == "PARTIAL"
    assert mod["spec_hash"] == spec_utils.compute_spec_hash(spec_path)
    backups = os.listdir(os.path.join(root, ".loop", "backup"))
    assert len(backups) == 1
    with open(os.path.join(root, ".loop", "backup", backups[0])) as f:
        assert f.read() == "# v1"  # HEAD version, not the edited one


def test_spec_result_backup_fallback_without_git(monkeypatch, tmp_path):
    """No git HEAD → still registers with a snapshot backup."""
    from wecom_server import router
    from state import StateManager

    root, spec_path = _make_spec_root(tmp_path, git=False)
    with open(spec_path, "w") as f:
        f.write("# v2 changed")

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] == "PARTIAL"
    assert len(os.listdir(os.path.join(root, ".loop", "backup"))) == 1


def test_spec_result_unknown_requirement(monkeypatch, tmp_path):
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"ghost","module":"chg1/m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_spec_result_unchanged_spec(monkeypatch, tmp_path):
    """Same hash on a registered (non-DRAFT) module → no registration."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    st = StateManager(root).load()
    st["modules"]["chg1/m1"]["status"] = "SYNCED"
    StateManager(root).save(st)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "没有变化" in reply


def test_spec_result_duplicate_registration_returns_registered(monkeypatch, tmp_path):
    """PARTIAL module with matching hash = already registered; a repeat
    spec_result says '已登记' instead of a misleading '没有变化' error."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    st = StateManager(root).load()
    st["modules"]["chg1/m1"]["status"] = "PARTIAL"
    StateManager(root).save(st)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply
    assert "没有变化" not in reply


def test_spec_result_missing_spec_file(monkeypatch, tmp_path):
    from wecom_server import router

    root, spec_path = _make_spec_root(tmp_path)
    os.remove(spec_path)  # module registered but spec file gone
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "找不到 spec" in reply


def test_spec_result_unknown_module_name(monkeypatch, tmp_path):
    """No matching module → helpful error, no registration."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"onlyname"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "找不到模块" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] != "PARTIAL"


def test_spec_result_registers_new_module(monkeypatch, tmp_path):
    """New module (absent from state.json) with full key + spec file registers."""
    from wecom_server import router
    from state import StateManager
    import spec_utils

    root, _ = _make_spec_root(tmp_path)
    new_dir = os.path.join(root, "openspec", "changes", "chg1", "specs", "m2")
    os.makedirs(new_dir, exist_ok=True)
    new_spec = os.path.join(new_dir, "spec.md")
    with open(new_spec, "w") as f:
        f.write("# m2 complete spec")

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m2"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("新增 m2 的 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply and "chg1/m2" in reply
    st = StateManager(root).load()
    mod = st["modules"]["chg1/m2"]
    assert mod["status"] == "PARTIAL"
    assert mod["spec_hash"] == spec_utils.compute_spec_hash(new_spec)
    # existing module untouched
    assert st["modules"]["chg1/m1"]["spec_hash"] is not None


def test_spec_result_new_module_no_spec_file(monkeypatch, tmp_path):
    """New module key without a spec file → still rejected, nothing registered."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m2"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("新增 m2 的 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "不在状态机" in reply
    assert "chg1/m2" not in StateManager(root).load()["modules"]


def test_spec_result_rejects_path_traversal(tmp_path):
    """'..' segments in a new-module key must be rejected, not resolved
    outside the requirement root (no read/copy/registration)."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    # a spec file OUTSIDE the requirement root that must never be touched
    outside = os.path.join(tmp_path, "outside", "specs", "victim", "spec.md")
    os.makedirs(os.path.dirname(outside), exist_ok=True)
    with open(outside, "w") as f:
        f.write("# victim spec outside root")

    reply = router._execute_spec_result(
        "req", "chg1/../../../../outside/specs/victim", [{"name": "req", "root": root}],
        "/tmp")

    assert "非法模块 key" in reply
    st = StateManager(root).load()
    assert "chg1/../../../../outside/specs/victim" not in st["modules"]
    # nothing escaped into the requirement's backup dir
    backup_dir = os.path.join(root, ".loop", "backup")
    if os.path.exists(backup_dir):
        assert all("victim" not in n for n in os.listdir(backup_dir))


def test_spec_result_rejects_slash_in_module_name(tmp_path):
    """Extra '/' inside a new-module key must be rejected (single segment)."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    reply = router._execute_spec_result(
        "req", "chg1/m2/extra", [{"name": "req", "root": root}], "/tmp")

    assert "非法模块 key" in reply
    assert "chg1/m2/extra" not in StateManager(root).load()["modules"]


def test_spec_result_promotes_draft_module(monkeypatch, tmp_path):
    """DRAFT module (auto-discovered, never gated) with unchanged hash is
    promoted to PARTIAL by spec_result — the confirmation gate."""
    from wecom_server import router
    from state import StateManager
    import spec_utils

    root, spec_path = _make_spec_root(tmp_path)
    sm = StateManager(root)
    st = sm.load()
    st["modules"]["chg1/m1"]["status"] = "DRAFT"
    sm.save(st)

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("确认 chg1/m1 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply
    st = StateManager(root).load()
    assert st["modules"]["chg1/m1"]["status"] == "PARTIAL"
    assert st["modules"]["chg1/m1"]["spec_hash"] == \
        spec_utils.compute_spec_hash(spec_path)


def _seed_gray_drafts(root, drafts):
    from state import StateManager
    sm = StateManager(root)
    st = sm.load()
    st["gray_drafts"] = drafts
    sm.save(st)


def _gray_test(monkeypatch, tmp_path, llm_reply, registry_root):
    from wecom_server import router
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply(llm_reply))
    return dispatch("灰名单", [{"name": "req", "root": registry_root}],
                    "/tmp", "u1")


def test_gray_list_view_lists_pending_drafts(monkeypatch, tmp_path):
    """gray_list action lists only pending drafts + adjudication instructions."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
        {"id": 2, "module": "chg1/m1", "summary": "warn B",
         "status": "pending"},
        {"id": 3, "module": "chg1/m1", "summary": "warn C",
         "status": "accepted"},
    ])

    fn = _gray_test(monkeypatch, tmp_path, '__JSON_ACTION__ {"action":"gray_list","requirement":"ALL"}', root)
    reply = fn()

    assert "草稿 1" in reply and "warn A" in reply
    assert "草稿 2" in reply and "warn B" in reply
    assert "warn C" not in reply
    assert "接受 1" in reply and "拒绝 2" in reply
    assert StateManager(root).load()["gray_drafts"][0]["status"] == "pending"


def test_gray_list_view_empty(monkeypatch, tmp_path):
    """No pending drafts → clear message."""
    root, _ = _make_spec_root(tmp_path)

    fn = _gray_test(monkeypatch, tmp_path, '__JSON_ACTION__ {"action":"gray_list","requirement":"ALL"}', root)
    reply = fn()

    assert "没有待裁决" in reply


def test_adjudicate_accept_marks_draft(monkeypatch, tmp_path):
    """adjudicate accept marks the draft and reports remaining."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
        {"id": 2, "module": "chg1/m1", "summary": "warn B",
         "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"1","decision":"accept"}\n已接受', root)
    reply = fn()

    st = StateManager(root).load()
    assert st["gray_drafts"][0]["status"] == "accepted"
    assert st["gray_drafts"][1]["status"] == "pending"
    assert "已接受草稿 1" in reply
    assert "还有 1 条待裁决" in reply


def test_adjudicate_accepts_chinese_decision_synonym(monkeypatch, tmp_path):
    """LLM emitting decision='接受' must resolve the draft, not error.

    Regression: '接受28 29' failed with '无法识别的草稿编号：28' because
    the decision was never canonicalized to {accept,reject}.
    """
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 28, "module": "chg1/m1", "summary": "warn A", "status": "pending"},
        {"id": 99, "module": "chg1/m1", "summary": "warn B", "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"28","decision":"接受"}', root)
    reply = fn()

    st = StateManager(root).load()
    statuses = {d["id"]: d["status"] for d in st["gray_drafts"]}
    assert statuses[28] == "accepted"
    assert "已接受草稿 28" in reply


def test_adjudicate_space_separated_ids(monkeypatch, tmp_path):
    """target='28 29' with a single decision adjudicates both drafts."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 28, "module": "chg1/m1", "summary": "warn A", "status": "pending"},
        {"id": 29, "module": "chg1/m1", "summary": "warn B", "status": "pending"},
        {"id": 30, "module": "chg1/m1", "summary": "warn C", "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"28 29","decision":"接受"}', root)
    reply = fn()

    st = StateManager(root).load()
    statuses = {d["id"]: d["status"] for d in st["gray_drafts"]}
    assert statuses == {28: "accepted", 29: "accepted", 30: "pending"}


def test_adjudicate_unknown_decision_blames_decision_not_number(monkeypatch, tmp_path):
    """A non-decision value must report the decision as the problem, and
    must NOT tell the user the (valid) draft number is unrecognized."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 28, "module": "chg1/m1", "summary": "warn A", "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"28","decision":"maybe"}', root)
    reply = fn()

    assert "无法识别的草稿编号：28" not in reply
    assert "裁决指令" in reply
    assert StateManager(root).load()["gray_drafts"][0]["status"] == "pending"


def test_adjudicate_all_done_auto_dispatches(monkeypatch, tmp_path):
    """Last draft adjudicated → auto-approves and dispatches."""
    import scheduler as sched_mod
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
    ])

    approve_called = False
    dispatch_called = False
    def _fake_approve(name, **kw):
        nonlocal approve_called
        approve_called = True
        return 1
    def _fake_dispatch(pending, **kw):
        nonlocal dispatch_called
        dispatch_called = True
        return {"req": None}
    monkeypatch.setattr(sched_mod, "approve", _fake_approve)
    monkeypatch.setattr(sched_mod, "dispatch", _fake_dispatch)

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"all","decision":"reject"}\n已拒绝', root)
    reply = fn()

    st = StateManager(root).load()
    assert st["gray_drafts"][0]["status"] == "rejected"
    assert "已拒绝草稿 1" in reply
    assert "全部裁决完毕" in reply
    assert "继续执行 req" in reply
    assert approve_called
    assert dispatch_called


def test_adjudicate_unknown_id(monkeypatch, tmp_path):
    """Unknown draft id → helpful error, state untouched."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"99","decision":"accept"}', root)
    reply = fn()

    assert "找不到草稿 99" in reply
    assert StateManager(root).load()["gray_drafts"][0]["status"] == "pending"


def test_adjudicate_mixed_decisions(monkeypatch, tmp_path):
    """adjudicate mixed applies per-draft decisions in one message."""
    import scheduler as sched_mod
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
        {"id": 2, "module": "chg1/m1", "summary": "warn B",
         "status": "pending"},
        {"id": 3, "module": "chg1/m1", "summary": "warn C",
         "status": "pending"},
    ])

    approve_called = False
    dispatch_called = False
    def _fake_approve(name, **kw):
        nonlocal approve_called
        approve_called = True
        return 1
    def _fake_dispatch(pending, **kw):
        nonlocal dispatch_called
        dispatch_called = True
        return {"req": None}
    monkeypatch.setattr(sched_mod, "approve", _fake_approve)
    monkeypatch.setattr(sched_mod, "dispatch", _fake_dispatch)

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"mixed","decision":"1=accept, 2=reject,3=reject"}\n'
                    "已处理", root)
    reply = fn()

    st = StateManager(root).load()
    statuses = {d["id"]: d["status"] for d in st["gray_drafts"]}
    assert statuses == {1: "accepted", 2: "rejected", 3: "rejected"}
    assert "已接受草稿 1" in reply
    assert "已拒绝草稿 2" in reply
    assert "已拒绝草稿 3" in reply
    assert "全部裁决完毕" in reply
    assert approve_called
    assert dispatch_called


def test_adjudicate_mixed_last_remaining_reports(monkeypatch, tmp_path):
    """Mixed adjudication leaving drafts pending reports the remainder."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
        {"id": 2, "module": "chg1/m1", "summary": "warn B",
         "status": "pending"},
        {"id": 3, "module": "chg1/m1", "summary": "warn C",
         "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"mixed","decision":"1=accept,2=reject"}', root)
    reply = fn()

    st = StateManager(root).load()
    statuses = {d["id"]: d["status"] for d in st["gray_drafts"]}
    assert statuses == {1: "accepted", 2: "rejected", 3: "pending"}
    assert "还有 1 条待裁决" in reply


def test_adjudicate_mixed_bad_format(monkeypatch, tmp_path):
    """Malformed mixed spec → helpful error, state untouched."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"req","target":"mixed","decision":"1=maybe"}', root)
    reply = fn()

    assert "混合裁决格式" in reply
    assert StateManager(root).load()["gray_drafts"][0]["status"] == "pending"


def test_adjudicate_all_requires_single_decision(monkeypatch, tmp_path):
    """Cross-requirement ALL adjudication rejects mixed mode."""
    root, _ = _make_spec_root(tmp_path)

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"ALL","target":"mixed","decision":"1=accept,2=reject"}', root)
    reply = fn()

    assert "单一决策" in reply



def test_adjudicate_unknown_requirement(monkeypatch, tmp_path):
    """Unknown requirement → helpful error."""
    root, _ = _make_spec_root(tmp_path)

    fn = _gray_test(monkeypatch, tmp_path,
                    '__JSON_ACTION__ {"action":"adjudicate","requirement":"ghost","target":"1","decision":"accept"}', root)
    reply = fn()

    assert "没有找到需求" in reply


def test_spec_result_autocompletes_module_name(monkeypatch, tmp_path):
    """Bare module name resolves to the unique change_id/module_name."""
    from wecom_server import router
    from state import StateManager

    root, spec_path = _make_spec_root(tmp_path)
    with open(spec_path, "w") as f:
        f.write("# v2 changed")

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply and "chg1/m1" in reply
    st = StateManager(root).load()
    assert st["modules"]["chg1/m1"]["status"] == "PARTIAL"


def test_spec_result_ambiguous_module_name(monkeypatch, tmp_path):
    """Module name matching multiple keys → asks for full key."""
    from wecom_server import router
    from state import StateManager
    import spec_utils

    root, spec_path = _make_spec_root(tmp_path)
    spec2_dir = os.path.join(root, "openspec", "changes", "chg2", "specs", "m1")
    os.makedirs(spec2_dir, exist_ok=True)
    spec2_path = os.path.join(spec2_dir, "spec.md")
    with open(spec2_path, "w") as f:
        f.write("# v1")
    sm = StateManager(root)
    st = sm.load()
    sm.add_module(st, "chg2/m1", "chg2", "m1",
                  spec_hash=spec_utils.compute_spec_hash(spec2_path))
    sm.save(st)
    with open(spec_path, "w") as f:
        f.write("# v2 changed")

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"m1"}'))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "对应多个模块" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] != "PARTIAL"





def test_get_session_id_split_per_requirement(monkeypatch, tmp_path):
    """Same user talking about different requirements gets different
    sessions; same (user, requirement) pair stays stable."""
    from wecom_server import router

    monkeypatch.setattr(router, "_SESSION_DIR", str(tmp_path))

    sid_a1, new1 = router._get_session_id("u1", "reqA")
    sid_b1, new2 = router._get_session_id("u1", "reqB")
    sid_a2, new3 = router._get_session_id("u1", "reqA")
    sid_global1, new4 = router._get_session_id("u1")
    sid_global2, new5 = router._get_session_id("u1", "global")

    assert sid_a1 != sid_b1 != sid_global1
    assert new1 and new2 and new4
    assert sid_a2 == sid_a1 and not new3
    assert sid_global2 == sid_global1 and not new5


def test_detect_requirement_matches_name_and_module(tmp_path):
    """Messages mentioning requirement name or module name resolve to the
    owning requirement; unrelated messages fall back to None (global)."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    registry = [{"name": "req", "root": root}]

    assert router._detect_requirement("改一下 req 的 spec", registry) == "req"
    assert router._detect_requirement("改一下 m1 的 spec", registry) == "req"
    assert router._detect_requirement("改一下 chg1/m1", registry) == "req"
    assert router._detect_requirement("随便聊聊", registry) is None


def test_detect_requirement_index_cached(monkeypatch, tmp_path):
    """Module index is read from state.json once; repeat messages hit the
    (mtime, size) cache instead of re-reading."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    router._module_index_cache.clear()
    registry = [{"name": "req", "root": root}]

    calls = []
    orig_load = StateManager.load
    monkeypatch.setattr(StateManager, "load",
                        lambda self: calls.append(1) or orig_load(self))

    for _ in range(3):
        assert router._detect_requirement("改一下 m1 的 spec", registry) == "req"
    assert len(calls) == 1


def test_detect_requirement_index_reloads_on_state_change(tmp_path):
    """Registering a new module in state.json (mtime bump) invalidates the
    cache; the new module is routed on the next message."""
    import time
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    router._module_index_cache.clear()
    registry = [{"name": "req", "root": root}]

    assert router._detect_requirement("改一下 m1 的 spec", registry) == "req"

    sm = StateManager(root)
    st = sm.load()
    sm.add_module(st, "chg1/m2", "chg1", "m2", spec_hash="h")
    sm.save(st)
    state_path = os.path.join(root, ".loop", "state.json")
    os.utime(state_path, ns=(time.time_ns() + 10**9,) * 2)

    assert router._detect_requirement("改一下 m2 的 spec", registry) == "req"


def test_llm_lock_serializes_same_requirement(monkeypatch, tmp_path):
    """Two LLM calls resolving to the same requirement never overlap, even
    when not queued by the server (e.g. classify fallback)."""
    import threading
    import time
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    router._module_index_cache.clear()
    router._llm_locks.clear()
    registry = [{"name": "req", "root": root}]
    order = []
    gate = threading.Event()

    def fake_run(cmd, **kwargs):
        order.append("enter")
        gate.wait(timeout=2)
        order.append("exit")
        return types.SimpleNamespace(stdout="好的", returncode=0)

    monkeypatch.setattr(router.subprocess, "run", fake_run)
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))

    def worker():
        router._llm_dispatch("改一下 m1 的 spec", registry, "/tmp", "u1")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    time.sleep(0.2)
    t2.start()
    time.sleep(0.2)
    assert order == ["enter"]  # second call blocked on the requirement lock

    gate.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert order == ["enter", "exit", "enter", "exit"]


def test_llm_dispatch_tags_reply_with_requirement(monkeypatch, tmp_path):
    """LLM chat replies get a 【requirement】 header and use the
    requirement-scoped session when the message identifies one."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    used = {}
    monkeypatch.setattr(
        router, "_get_session_id",
        lambda uid, req="global": used.__setitem__("req", req) or ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("好的，我来看一下"))

    fn = dispatch("改一下 m1 的 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert used["req"] == "req"
    assert reply == "【req】\n好的，我来看一下"


def test_llm_dispatch_global_session_no_tag_when_unknown(monkeypatch, tmp_path):
    """Messages with no identifiable requirement use the global session
    and get no 【...】 header."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    used = {}
    monkeypatch.setattr(
        router, "_get_session_id",
        lambda uid, req="global": used.__setitem__("req", req) or ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("有什么可以帮你"))

    fn = dispatch("随便聊聊", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert used["req"] == "global"
    assert reply == "有什么可以帮你"



def test_classify_requirement_semantic_match(monkeypatch, tmp_path):
    """Messages with no exact name still route via semantic classification."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply("我判断这条消息在聊 req"))

    name = router._classify_requirement(
        "把那个时间范围校验的报错改成中文提示",
        [{"name": "req", "root": root}])

    assert name == "req"


def test_classify_requirement_none_when_no_match(monkeypatch, tmp_path):
    """Classifier saying 无 falls back to global."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router.subprocess, "run", _fake_llm_reply("无"))

    name = router._classify_requirement("你好", [{"name": "req", "root": root}])

    assert name is None


def test_llm_dispatch_semantic_classify_routes_session(monkeypatch, tmp_path):
    """Exact match miss + semantic hit → requirement session + tagged reply."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)

    def fake_run(cmd, **kwargs):
        if any("qodercli" in part for part in cmd):
            if "只做需求归属分类" in kwargs.get("input", ""):
                return types.SimpleNamespace(stdout="req", returncode=0)
            return types.SimpleNamespace(stdout="好的，我来看一下", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)

    used = {}
    monkeypatch.setattr(
        router, "_get_session_id",
        lambda uid, req="global": used.__setitem__("req", req) or ("sid", True))
    monkeypatch.setattr(router.subprocess, "run", fake_run)

    fn = dispatch("把那个时间范围校验的报错改成中文提示",
                  [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert used["req"] == "req"
    assert reply == "【req】\n好的，我来看一下"


def test_dispatch_json_approve(monkeypatch):
    """__JSON_ACTION__ approve dispatches like the old prefix."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('好的\n__JSON_ACTION__ {"action":"approve","requirement":"req"}'))
    calls = {}
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        calls.__setitem__("approve", name) or 1)
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


def test_dispatch_json_missing_params(monkeypatch):
    """JSON action with missing required fields returns 缺少参数."""
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"approve"}'))

    fn = dispatch("批准", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert reply == "缺少参数：requirement"


def test_dispatch_json_unknown_action(monkeypatch):
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"fly"}'))

    fn = dispatch("随便", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert reply == "未知 action：fly"


def test_dispatch_json_keeps_assistant_body(monkeypatch):
    """JSON action 动作结果与 G 正文拼接返回：spec_result 的变更披露不再被吞。"""
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global":
                        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply(
                            "【req】刚修正一处 spec 笔误：SKU → SKU销售单位。\n\n"
                            '__JSON_ACTION__ {"action":"spec_result",'
                            '"requirement":"req","module":"c/m"}'))
    monkeypatch.setattr(router, "_dispatch_json_action",
                        lambda payload, registry, data_dir, user_id:
                        "spec 变更已登记：req/c/m")

    fn = dispatch("查看 req 需求情况", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert "刚修正一处 spec 笔误" in reply
    assert "spec 变更已登记：req/c/m" in reply
    assert "__JSON_ACTION__" not in reply


def test_dispatch_json_without_body_returns_result_only(monkeypatch):
    """无正文的纯动作回复保持原行为：只返回动作结果。"""
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global":
                        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply(
                            '__JSON_ACTION__ {"action":"history"}'))
    monkeypatch.setattr(router, "_dispatch_json_action",
                        lambda payload, registry, data_dir, user_id:
                        "最近 0 次执行")

    fn = dispatch("查 req 历史", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert reply == "最近 0 次执行"


def test_dispatch_json_multiple_blocks_all_executed(monkeypatch):
    """G emits one spec_result block per edited module → every block
    executes, the body is preserved, and no __JSON_ACTION__ residue stays."""
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global":
                        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply(
            "【req】branch_stock_amount 类型已写入 3 处 spec。\n\n"
            '__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"c/a"}\n'
            '__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"c/b"}\n'
            '__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"c/c"}'))
    calls = []
    monkeypatch.setattr(router, "_execute_spec_result",
                        lambda name, module, registry, data_dir:
                        calls.append(module) or f"spec 变更已登记：{module}")

    fn = dispatch("战略备货 确认改类型", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert calls == ["c/a", "c/b", "c/c"], "every JSON action block must run"
    assert "branch_stock_amount 类型已写入" in reply
    assert "spec 变更已登记：c/a" in reply
    assert "spec 变更已登记：c/b" in reply
    assert "spec 变更已登记：c/c" in reply
    assert "__JSON_ACTION__" not in reply


def test_dispatch_json_prefix_priority(monkeypatch):
    """JSON action block wins even when a legacy prefix is present."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"history","requirement":"ghost"}\n'
                        '__JSON_ACTION__ {"action":"approve","requirement":"req"}'))
    calls = {}
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        calls.__setitem__("approve", name) or 1)
    monkeypatch.setattr(scheduler, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(scheduler, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(scheduler, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert calls["approve"] == "req"
    assert "已批准并开始执行" in reply


def test_dispatch_json_unknown_requirement_error(monkeypatch):
    """JSON action for an unknown requirement returns a helpful error."""
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"ghost"}'))

    fn = dispatch("批准 ghost", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_dispatch_json_approve_executes(monkeypatch):
    """JSON approve action triggers real scheduler.approve + dispatch."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply('__JSON_ACTION__ {"action":"approve","requirement":"req"}\n好的，开始执行'))
    calls = {}
    monkeypatch.setattr(scheduler, "approve",
                        lambda name, approved_by=None:
                        calls.__setitem__("approve", name) or 1)
    monkeypatch.setattr(scheduler, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(scheduler, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(scheduler, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "已批准并开始执行" in reply
    assert calls["approve"] == "req"


def test_dispatch_json_spec_result(monkeypatch, tmp_path):
    """JSON spec_result action registers a spec change (hash update)."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    registry = [{"name": "req", "root": root}]

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}'))

    fn = dispatch("已修改 spec", registry, "/tmp", "u1")
    reply = fn()

    assert "spec" in reply or "已登记" in reply or "PARTIAL" in reply

    from state import StateManager
    st = StateManager(root).load()
    assert st["modules"]["chg1/m1"]["status"] == "PARTIAL"


def test_correction_loop_registers_spec_after_edit(monkeypatch, tmp_path):
    """G edited spec.md (snapshot exists) but replied approve without
    registration → the server re-drives the session; G appends spec_result;
    registration runs AND the original approve is carried back, so the
    user's approval takes effect in the same dispatch."""
    import scheduler as sched_mod
    from wecom_server import router
    from state import StateManager

    root, spec_path = _make_spec_root(tmp_path)
    with open(spec_path, "w") as f:
        f.write("# v2 changed")
    snap_dir = tmp_path / "snaps"
    _seed_spec_snapshot(snap_dir, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "m1")
    monkeypatch.setattr(router, "_SPEC_SNAP_DIR", str(snap_dir))

    fake_run, state = _fake_llm_sequence([
        '__JSON_ACTION__ {"action":"approve","requirement":"req"}',
        '__JSON_ACTION__ {"action":"spec_result","requirement":"req",'
        '"module":"chg1/m1"}',
    ])
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run", fake_run)
    monkeypatch.setattr(router, "_audit_line", lambda text: None)
    approved = []
    monkeypatch.setattr(sched_mod, "approve",
                        lambda name, approved_by=None:
                        approved.append(name) or 1)
    monkeypatch.setattr(sched_mod, "poll", lambda: None)
    monkeypatch.setattr(sched_mod, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(sched_mod, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": root}],
                  "/tmp", "u1")
    reply = fn()

    assert state["calls"] == 2
    assert "spec_result" in state["inputs"][1]
    assert "已登记" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] == "PARTIAL"
    assert approved == ["req"], "approve declared before correction must still run"
    assert "已批准并开始执行" in reply


def test_correction_loop_skipped_without_spec_edit(monkeypatch, tmp_path):
    """No spec snapshot for this session → approve executes on the first
    reply, exactly one LLM turn."""
    import scheduler as sched_mod
    from wecom_server import router

    snap_dir = tmp_path / "snaps"  # never created
    monkeypatch.setattr(router, "_SPEC_SNAP_DIR", str(snap_dir))
    fake_run, state = _fake_llm_sequence([
        '__JSON_ACTION__ {"action":"approve","requirement":"req"}',
    ])
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run", fake_run)
    approved = []
    monkeypatch.setattr(sched_mod, "approve",
                        lambda name, approved_by=None:
                        approved.append(name) or 1)
    monkeypatch.setattr(sched_mod, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(sched_mod, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(sched_mod, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert state["calls"] == 1
    assert approved == ["req"]
    assert "已批准并开始执行" in reply


def test_correction_loop_exhausts_then_dispatch(monkeypatch, tmp_path):
    """G ignores two corrections and keeps replying approve → the last
    approve goes through the normal path (no infinite loop)."""
    import scheduler as sched_mod
    from wecom_server import router

    snap_dir = tmp_path / "snaps"
    _seed_spec_snapshot(snap_dir, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "m1")
    monkeypatch.setattr(router, "_SPEC_SNAP_DIR", str(snap_dir))
    fake_run, state = _fake_llm_sequence([
        '__JSON_ACTION__ {"action":"approve","requirement":"req"}',
        '__JSON_ACTION__ {"action":"approve","requirement":"req"}',
        '__JSON_ACTION__ {"action":"approve","requirement":"req"}',
    ])
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run", fake_run)
    approved = []
    monkeypatch.setattr(sched_mod, "approve",
                        lambda name, approved_by=None:
                        approved.append(name) or 1)
    monkeypatch.setattr(sched_mod, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(sched_mod, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(sched_mod, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert state["calls"] == 3
    assert approved == ["req"]
    assert "已批准并开始执行" in reply


def test_correction_loop_ignores_stale_snapshots(monkeypatch, tmp_path):
    """Snapshots outside the edit window (e.g. a previous grill-me round)
    do not trigger the correction loop."""
    import scheduler as sched_mod
    from wecom_server import router

    snap_dir = tmp_path / "snaps"
    _seed_spec_snapshot(snap_dir, "sid", "m1", ts="20260819T181858")
    monkeypatch.setattr(router, "_SPEC_SNAP_DIR", str(snap_dir))
    fake_run, state = _fake_llm_sequence([
        '__JSON_ACTION__ {"action":"approve","requirement":"req"}',
    ])
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run", fake_run)
    approved = []
    monkeypatch.setattr(sched_mod, "approve",
                        lambda name, approved_by=None:
                        approved.append(name) or 1)
    monkeypatch.setattr(sched_mod, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(sched_mod, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(sched_mod, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert state["calls"] == 1
    assert approved == ["req"]
    assert "已批准并开始执行" in reply


def test_correction_loop_other_sessions_snapshots_ignored(monkeypatch,
                                                         tmp_path):
    """Snapshots from a different session never trigger correction."""
    import scheduler as sched_mod
    from wecom_server import router

    snap_dir = tmp_path / "snaps"
    _seed_spec_snapshot(snap_dir, "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb", "m1")
    monkeypatch.setattr(router, "_SPEC_SNAP_DIR", str(snap_dir))
    fake_run, state = _fake_llm_sequence([
        '__JSON_ACTION__ {"action":"approve","requirement":"req"}',
    ])
    monkeypatch.setattr(router, "_get_session_id",
                        lambda uid, req="global": ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", True))
    monkeypatch.setattr(router.subprocess, "run", fake_run)
    approved = []
    monkeypatch.setattr(sched_mod, "approve",
                        lambda name, approved_by=None:
                        approved.append(name) or 1)
    monkeypatch.setattr(sched_mod, "load_pending",
                        lambda: {"pending": [{"requirement": "req"}]})
    monkeypatch.setattr(sched_mod, "load_config",
                        lambda: {"max_concurrency": 2})
    monkeypatch.setattr(sched_mod, "dispatch",
                        lambda entries, max_concurrency=2: ["req"])

    fn = dispatch("批准执行 req", [{"name": "req", "root": "/tmp/x"}],
                  "/tmp", "u1")
    reply = fn()

    assert state["calls"] == 1
    assert approved == ["req"]
    assert "已批准并开始执行" in reply

