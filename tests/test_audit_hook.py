"""Tests for the WeCom audit hook script (wecom_server/hooks/audit_hook.sh)."""
import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(__file__), "..", "wecom_server",
                    "hooks", "audit_hook.sh")


def _run(tool_input, session_id="sid-1", tool_name="Bash"):
    """Run the hook with both write targets isolated to a tempdir.

    SNAP_DIR must be overridden too: audit_hook.sh mkdir's it on every call,
    so a test run would otherwise litter the real spec-snapshots dir.
    """
    tmp = tempfile.mkdtemp()
    audit_log = os.path.join(tmp, "audit.log")
    snap_dir = os.path.join(tmp, "spec-snapshots")
    payload = json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    })
    env = {**os.environ, "AUDIT_LOG": audit_log, "SNAP_DIR": snap_dir}
    r = subprocess.run(["bash", HOOK], input=payload, env=env,
                       capture_output=True, text=True)
    return r, audit_log, snap_dir


def _run_hook(tool_input, session_id="sid-1", tool_name="Bash"):
    r, audit_log, _ = _run(tool_input, session_id, tool_name)
    assert r.returncode == 0, r.stderr
    return audit_log


def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def test_hook_logs_git_push():
    log = _run_hook({"command": "git push origin main"})
    assert "git push origin main" in _read(log)
    assert "session=sid-1" in _read(log)


def test_hook_logs_force_push_and_rm_rf():
    log = _run_hook({"command": "git push --force && rm -rf target/"})
    content = _read(log)
    assert "git push --force && rm -rf target/" in content


def test_hook_logs_mr_creation():
    log = _run_hook({"command": "gh pr create --title x --body y"})
    assert "gh pr create" in _read(log)


def test_hook_ignores_harmless_commands():
    log = _run_hook({"command": "ls -la && mvn test"})
    assert _read(log) == ""


def test_hook_ignores_missing_command():
    log = _run_hook({})
    assert _read(log) == ""


def test_hook_logs_edit_file_path():
    path = "/proj/src/main/java/Foo.java"
    log = _run_hook({"file_path": path}, tool_name="Edit")
    content = _read(log)
    assert path in content
    assert "tool=Edit" in content
    assert "session=sid-1" in content


def test_hook_logs_write_file_path():
    path = "/proj/src/main/java/Bar.java"
    log = _run_hook({"file_path": path}, tool_name="Write")
    content = _read(log)
    assert path in content
    assert "tool=Write" in content


def test_hook_ignores_edit_without_path():
    log = _run_hook({}, tool_name="Edit")
    assert _read(log) == ""


SESSION_UUID = "0f9d8a1b-2c3d-4e5f-6a7b-8c9d0e1f2a3b"


def test_hook_blocks_edit_of_loop_state_json():
    r, log, _ = _run({"file_path": "/req/.loop/state.json"},
                                 tool_name="Edit")
    assert r.returncode == 1
    assert "已阻止" in r.stdout
    assert "BLOCKED" in _read(log)


def test_hook_blocks_write_of_pending_json():
    r, log, _ = _run({"file_path": "/req/pending.json"},
                                 tool_name="Write")
    assert r.returncode == 1
    assert "已阻止" in r.stdout


def test_hook_blocks_bash_writing_state_file():
    r, _, _ = _run({"command": "echo x >> .loop/state.json"})
    assert r.returncode == 1
    assert "已阻止" in r.stdout


def test_hook_blocks_bash_driving_engine():
    r, _, _ = _run(
        {"command": 'python3 -c "import scheduler"'})
    assert r.returncode == 1
    assert "已阻止" in r.stdout


def test_hook_snapshots_spec_md_edit():
    """spec_result reports +X/-Y lines from these files, and the router only
    sees them if the name matches _SPEC_SNAP_RE (session id must be a UUID)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from wecom_server.router import _SPEC_SNAP_RE

    spec = os.path.join(tempfile.mkdtemp(), "specs", "wms-out", "spec.md")
    os.makedirs(os.path.dirname(spec))
    with open(spec, "w") as f:
        f.write("# Spec\n\nScenario: 出库\n")

    r, log, snap_dir = _run({"file_path": spec},
                                        tool_name="Edit",
                                        session_id=SESSION_UUID)
    assert r.returncode == 0, r.stderr
    names = os.listdir(snap_dir)
    assert len(names) == 1, names
    assert _SPEC_SNAP_RE.match(names[0]), names[0]
    assert names[0].endswith("-wms-out.md")
    content = _read(os.path.join(snap_dir, names[0]))
    assert "Scenario: 出库" in content
    assert "SPEC_SNAPSHOT" in _read(log)
