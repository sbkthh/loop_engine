"""Tests for the group-chat read-only hook (readonly_guard.sh)."""
import json
import os
import subprocess
import tempfile

HOOK = os.path.join(os.path.dirname(__file__), "..", "wecom_server",
                    "hooks", "readonly_guard.sh")


def _run(command):
    tmp = tempfile.mkdtemp()
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}})
    env = {**os.environ, "AUDIT_LOG": os.path.join(tmp, "audit.log")}
    return subprocess.run(["bash", HOOK], input=payload, env=env,
                          capture_output=True, text=True)


def test_sql_comparison_operators_are_not_redirection():
    for command in (
        "dgate exec --catalog-uuid c --qualified-name fluxwmsdb "
        "\"SELECT 1 FROM bsm_config WHERE addTime >= '2026-10-01'\"",
        "dgate exec --catalog-uuid c --qualified-name fluxwmsdb "
        "\"SELECT 1 FROM bsm_config WHERE addTime > '2026-10-01'\"",
        "awk 'NR>=1690 && NR<=1760' manager/Foo.java",
    ):
        r = _run(command)
        assert r.returncode == 0, r.stdout


def test_real_redirection_is_still_blocked():
    r = _run("echo hi > /tmp/x")
    assert r.returncode == 1
    assert "已阻止" in r.stdout


def test_fd_redirection_is_allowed():
    r = _run("dgate exec --catalog-uuid c --qualified-name fluxwmsdb "
             "\"SELECT 1\" 2>&1")
    assert r.returncode == 0, r.stdout


def test_mutations_are_still_blocked():
    for command in (
        "git push origin main",
        "bash -c \"git push origin main\"",
        "sed -i 's/a/b/' f.txt",
        "dgate exec --catalog-uuid c --qualified-name d "
        "\"UPDATE t SET a=1\"",
    ):
        r = _run(command)
        assert r.returncode == 1, command
        assert "已阻止" in r.stdout
