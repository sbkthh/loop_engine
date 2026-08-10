"""Tests for the WeCom audit hook script (wecom_server/hooks/audit_hook.sh)."""
import json
import os
import subprocess
import tempfile

HOOK = os.path.join(os.path.dirname(__file__), "..", "wecom_server",
                    "hooks", "audit_hook.sh")


def _run_hook(tool_input, session_id="sid-1"):
    tmp = tempfile.mkdtemp()
    audit_log = os.path.join(tmp, "audit.log")
    script = open(HOOK).read().replace(
        "/Users/chuan.li/.qoder/loop_engine/audit.log", audit_log)
    tmp_script = os.path.join(tmp, "hook.sh")
    with open(tmp_script, "w") as f:
        f.write(script)
    payload = json.dumps({
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_input": tool_input,
    })
    r = subprocess.run(["bash", tmp_script], input=payload,
                       capture_output=True, text=True)
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
