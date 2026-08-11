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

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__APPROVE__ ghost"))

    fn = dispatch("批准 ghost", [{"name": "req", "root": "/tmp/x"}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_approve_prefix_passes_user_id(monkeypatch):
    """Approval records who initiated it so scheduler notifies that user."""
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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


def test_approve_report_only_returns_error(monkeypatch):
    from wecom_server import router
    import scheduler

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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
    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ ghost chg1/m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "没有找到需求" in reply


def test_spec_result_unchanged_spec(monkeypatch, tmp_path):
    """Same hash → no registration, tells G to actually edit the spec."""
    from wecom_server import router

    root, _ = _make_spec_root(tmp_path)
    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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
    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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
    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ req onlyname"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "找不到模块" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] != "PARTIAL"


def _seed_gray_drafts(root, drafts):
    from state import StateManager
    sm = StateManager(root)
    st = sm.load()
    st["gray_drafts"] = drafts
    sm.save(st)


def _gray_test(monkeypatch, tmp_path, llm_reply, registry_root):
    from wecom_server import router
    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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
    assert "接受" in reply and "拒绝" in reply
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


def test_adjudicate_all_done_hints_approve(monkeypatch, tmp_path):
    """Last draft adjudicated → tells user to approve to continue."""
    from state import StateManager

    root, _ = _make_spec_root(tmp_path)
    _seed_gray_drafts(root, [
        {"id": 1, "module": "chg1/m1", "summary": "warn A",
         "status": "pending"},
    ])

    fn = _gray_test(monkeypatch, tmp_path,
                    "__ADJUDICATE__ req all reject\n已拒绝", root)
    reply = fn()

    st = StateManager(root).load()
    assert st["gray_drafts"][0]["status"] == "rejected"
    assert "已拒绝草稿 1" in reply
    assert "全部裁决完毕" in reply
    assert "批准执行 req" in reply


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

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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

    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
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
    monkeypatch.setattr(router, "_get_session_id", lambda uid: ("sid", True))
    monkeypatch.setattr(router.subprocess, "run",
                        _fake_llm_reply("__SPEC_RESULT__ ghost/m1"))
    monkeypatch.setattr(router, "_audit_line", lambda text: None)

    fn = dispatch("改 spec", [{"name": "req", "root": root}], "/tmp", "u1")
    reply = fn()

    assert "找不到模块" in reply
    assert StateManager(root).load()["modules"]["chg1/m1"]["status"] != "PARTIAL"
