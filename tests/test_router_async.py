"""Tests for router async dispatch (all messages go through LLM)."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server.router import dispatch


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
    assert "manual-loop" in router._LLM_SYSTEM_PROMPT
    assert "__JSON_ACTION__" in router._LLM_SYSTEM_PROMPT
    assert "spec_result" in router._LLM_SYSTEM_PROMPT


def _fake_llm_reply(stdout):
    def fake_run(cmd, **kwargs):
        if any("qodercli" in part for part in cmd):
            return types.SimpleNamespace(stdout=stdout, returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)
    return fake_run


def test_approve_prefix_executes(monkeypatch):
    """__APPROVE__ prefix triggers real scheduler.approve + dispatch."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req\n好的，开始执行"))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ ghost"))

    fn = dispatch("批准 ghost", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_approve_prefix_passes_user_id(monkeypatch):
    """Approval records who initiated it so scheduler notifies that user."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req"))
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
    """DRAFT-only requirement: __APPROVE__ rejected via STATUS_TABLE
    prefixes before scheduler.approve is ever reached."""
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
                        lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req"))
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
                        lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req"))
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


def test_approve_report_only_returns_error(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req"))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__HISTORY__ ALL"))
    monkeypatch.setattr(scheduler, "load_runs", lambda: {"runs": []})

    fn = dispatch("最近执行情况", [], "/tmp", "u1")
    reply = fn()

    assert "暂无执行历史" in reply


def test_normal_reply_untouched(monkeypatch):
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
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
    """__SPEC_RESULT__ verifies + backs up + updates hash/status to PARTIAL."""
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
                stdout="__SPEC_RESULT__ req chg1/m1\n已修改", returncode=0)
        return real_run(cmd, **kwargs)  # git show must run for real

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req chg1/m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] == "PARTIAL"
    assert len(os.listdir(os.path.join(root, ".loop", "backup"))) == 1


def test_spec_result_unknown_requirement(monkeypatch, tmp_path):
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ ghost chg1/m1"))
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
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req chg1/m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "没有变化" in reply


def test_spec_result_missing_spec_file(monkeypatch, tmp_path):
    from wecom_server import router

    root, spec_path = _make_spec_root(tmp_path)
    os.remove(spec_path)  # module registered but spec file gone
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req chg1/m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "找不到 spec" in reply


def test_spec_result_unknown_module_name(monkeypatch, tmp_path):
    """No matching module → helpful error, no registration."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req onlyname"))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req chg1/m2"))
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
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req chg1/m2"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("新增 m2 的 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "不在状态机" in reply
    assert "chg1/m2" not in StateManager(root).load()["modules"]


def test_spec_result_promotes_draft_module(monkeypatch, tmp_path):
    """DRAFT module (auto-discovered, never gated) with unchanged hash is
    promoted to PARTIAL by __SPEC_RESULT__ — the confirmation gate."""
    from wecom_server import router
    from state import StateManager
    import spec_utils

    root, spec_path = _make_spec_root(tmp_path)
    sm = StateManager(root)
    st = sm.load()
    st["modules"]["chg1/m1"]["status"] = "DRAFT"
    sm.save(st)

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req chg1/m1"))
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
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply(llm_reply))
    return dispatch("灰名单", [{"name": "req", "root": registry_root}],
                    "/tmp", "u1")


def test_gray_list_view_lists_pending_drafts(monkeypatch, tmp_path):
    """__GRAY_LIST__ lists only pending drafts + adjudication instructions."""
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

    fn = _gray_test(monkeypatch, tmp_path, "__GRAY_LIST__ ALL", root)
    reply = fn()

    assert "草稿 1" in reply and "warn A" in reply
    assert "草稿 2" in reply and "warn B" in reply
    assert "warn C" not in reply
    assert "接受 1" in reply and "拒绝 2" in reply
    assert StateManager(root).load()["gray_drafts"][0]["status"] == "pending"


def test_gray_list_view_empty(monkeypatch, tmp_path):
    """No pending drafts → clear message."""
    root, _ = _make_spec_root(tmp_path)

    fn = _gray_test(monkeypatch, tmp_path, "__GRAY_LIST__ ALL", root)
    reply = fn()

    assert "没有待裁决" in reply


def test_adjudicate_accept_marks_draft(monkeypatch, tmp_path):
    """__ADJUDICATE__ accept marks the draft and reports remaining."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
        {"id": 2, "module": "chg1/m1", "summary": "warn B",
         "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    "__ADJUDICATE__ req 1 accept\n已接受", root)
    reply = fn()

    st = StateManager(root).load()
    assert st["gray_drafts"][0]["status"] == "accepted"
    assert st["gray_drafts"][1]["status"] == "pending"
    assert "已接受草稿 1" in reply
    assert "还有 1 条待裁决" in reply


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
                    "__ADJUDICATE__ req all reject\n已拒绝", root)
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
                    "__ADJUDICATE__ req 99 accept", root)
    reply = fn()

    assert "找不到草稿 99" in reply
    assert StateManager(root).load()["gray_drafts"][0]["status"] == "pending"


def test_adjudicate_mixed_decisions(monkeypatch, tmp_path):
    """__ADJUDICATE__ mixed applies per-draft decisions in one message."""
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
                    "__ADJUDICATE__ req mixed 1=accept, 2=reject,3=reject\n"
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
                    "__ADJUDICATE__ req mixed 1=accept,2=reject", root)
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
                    "__ADJUDICATE__ req mixed 1=maybe", root)
    reply = fn()

    assert "混合裁决格式" in reply
    assert StateManager(root).load()["gray_drafts"][0]["status"] == "pending"


def test_adjudicate_all_requires_single_decision(monkeypatch, tmp_path):
    """Cross-requirement ALL adjudication rejects mixed mode."""
    root, _ = _make_spec_root(tmp_path)

    fn = _gray_test(monkeypatch, tmp_path,
                    "__ADJUDICATE__ ALL mixed 1=accept,2=reject", root)
    reply = fn()

    assert "单一决策" in reply



def test_adjudicate_unknown_requirement(monkeypatch, tmp_path):
    """Unknown requirement → helpful error."""
    root, _ = _make_spec_root(tmp_path)

    fn = _gray_test(monkeypatch, tmp_path,
                    "__ADJUDICATE__ ghost 1 accept", root)
    reply = fn()

    assert "没有找到需求" in reply


def test_spec_result_autocompletes_module_name(monkeypatch, tmp_path):
    """Bare module name resolves to the unique change_id/module_name."""
    from wecom_server import router
    from state import StateManager

    root, spec_path = _make_spec_root(tmp_path)
    with open(spec_path, "w") as f:
        f.write("# v2 changed")

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req m1"))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "对应多个模块" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] != "PARTIAL"


def test_spec_result_single_token_full_key(monkeypatch, tmp_path):
    """One-token full key (name merged with key) → owner located by key."""
    from wecom_server import router
    from state import StateManager

    root, spec_path = _make_spec_root(tmp_path)
    with open(spec_path, "w") as f:
        f.write("# v2 changed")

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ chg1/m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "已登记" in reply and "chg1/m1" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] == "PARTIAL"


def test_spec_result_single_token_unknown_key(monkeypatch, tmp_path):
    """One-token key that no requirement owns → helpful error, no change."""
    from wecom_server import router
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ ghost/m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "找不到模块" in reply
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
                        lambda uid, req="global": ("sid", True))

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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"approve"}'))

    fn = dispatch("批准", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert reply == "缺少参数：requirement"


def test_dispatch_json_unknown_action(monkeypatch):
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"fly"}'))

    fn = dispatch("随便", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert reply == "未知 action：fly"


def test_dispatch_json_prefix_priority(monkeypatch):
    """JSON action block wins even when a legacy prefix is present."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply("__HISTORY__ ghost\n"
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


def test_dispatch_json_invalid_falls_back_to_legacy(monkeypatch):
    """Malformed JSON block degrades to the legacy prefix path."""
    from wecom_server import router

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply("__APPROVE__ ghost"))

    fn = dispatch("批准 ghost", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_dispatch_old_prefix_compat(monkeypatch):
    """Legacy prefixes still work after the JSON protocol lands."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ req\n好的，开始执行"))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid, req="global": ("sid", True))
    monkeypatch.setattr(
        router.subprocess, "run",
        _fake_llm_reply('__JSON_ACTION__ {"action":"spec_result","requirement":"req","module":"chg1/m1"}'))

    fn = dispatch("已修改 spec", registry, "/tmp", "u1")
    reply = fn()

    assert "spec" in reply or "已登记" in reply or "PARTIAL" in reply

    from state import StateManager
    st = StateManager(root).load()
    assert st["modules"]["chg1/m1"]["status"] == "PARTIAL"
