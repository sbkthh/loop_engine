"""Tests for scope_audit.py"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scope_audit import (
    _should_filter,
    audit,
    audit_module,
    cmd_scope_audit,
    format_report,
    get_git_status,
    load_state,
)


# ---------- load_state ----------

def test_load_state_file_not_found(tmp_path):
    d = tmp_path / "nope"
    d.mkdir()
    with pytest.raises(ValueError, match="state.json not found"):
        load_state(str(d))


def test_load_state_corrupt(tmp_path):
    state_file = tmp_path / ".loop" / "state.json"
    state_file.parent.mkdir()
    state_file.write_text("{bad json")
    with pytest.raises(ValueError, match="Invalid state.json"):
        load_state(str(tmp_path))


def test_load_state_ok(tmp_path):
    state_file = tmp_path / ".loop" / "state.json"
    state_file.parent.mkdir()
    data = {"modules": {"m1": {"status": "SYNCED"}}}
    state_file.write_text(json.dumps(data))
    state = load_state(str(tmp_path))
    assert state["modules"]["m1"]["status"] == "SYNCED"


# ---------- get_git_status ----------

def test_get_git_status_clean(tmp_path):
    """git status with no output = clean."""
    import subprocess as sp
    old_run = sp.run
    def _fake_run(cmd, **kw):
        assert cmd[0] == "git"
        return sp.CompletedProcess(cmd, 0, "", "")

    try:
        subprocess.run = _fake_run
        changed, err = get_git_status(str(tmp_path))
        assert err is None
        assert changed == set()
    finally:
        subprocess.run = old_run


def test_get_git_status_not_git(tmp_path):
    """project_root not a git repo has stderr → error returned."""
    changed, err = get_git_status(str(tmp_path))
    assert err is not None
    assert changed == set()


# ---------- _should_filter ----------

@pytest.mark.parametrize("path,expected", [
    (".loop/state.json", True),
    (".codegraph/nodes.db", True),
    (".git/HEAD", True),
    ("src/main/Foo.java", False),
    ("zkh-opc-sna-stock-strategy/src/test/Test.java", False),
    ("openspec/changes/x/specs/y/spec.md", False),
])
def test_should_filter(path, expected):
    assert _should_filter(path) == expected


# ---------- audit_module ----------

def _make_module(files_created=None, files_modified=None, status="SYNCED"):
    return {
        "files_created": files_created or [],
        "files_modified": files_modified or [],
        "status": status,
    }


def test_audit_module_clean(monkeypatch, tmp_path):
    """Declared matches actual = clean."""
    monkeypatch.setattr(
        "scope_audit.get_git_status",
        lambda _: ({"Foo.java", "Bar.java"}, None),
    )
    mod = _make_module(files_modified=[
        str(tmp_path / "Foo.java"),
        str(tmp_path / "Bar.java"),
    ])
    result = audit_module(mod, str(tmp_path))
    assert result["clean"] is True
    assert result["unexplained"] == []
    assert result["phantom"] == []


def test_audit_module_unexplained(monkeypatch, tmp_path):
    """Actual has extra file = unexplained."""
    monkeypatch.setattr(
        "scope_audit.get_git_status",
        lambda _: ({"Foo.java", "Unexplained.java"}, None),
    )
    mod = _make_module(files_modified=[str(tmp_path / "Foo.java")])
    result = audit_module(mod, str(tmp_path))
    assert result["clean"] is False
    assert "Unexplained.java" in result["unexplained"]


def test_audit_module_phantom(monkeypatch, tmp_path):
    """Declared file not in actual = phantom."""
    monkeypatch.setattr(
        "scope_audit.get_git_status",
        lambda _: ({"Foo.java"}, None),
    )
    mod = _make_module(files_modified=[
        str(tmp_path / "Foo.java"),
        str(tmp_path / "Phantom.java"),
    ])
    result = audit_module(mod, str(tmp_path))
    assert result["clean"] is False
    assert "Phantom.java" in result["phantom"]


def test_audit_module_filters_loop(monkeypatch, tmp_path):
    """.loop/ and .codegraph/ files are filtered from actual."""
    monkeypatch.setattr(
        "scope_audit.get_git_status",
        lambda _: ({"Foo.java", ".loop/state.json"}, None),
    )
    mod = _make_module(files_modified=[str(tmp_path / "Foo.java")])
    result = audit_module(mod, str(tmp_path))
    assert result["clean"] is True
    # .loop/state.json must not appear in unexplained
    assert ".loop/state.json" not in result["unexplained"]


# ---------- audit (multi-module) ----------

def test_audit_empty_modules():
    state = {"modules": {}}
    assert audit(state, "/tmp") == {}


def test_audit_missing_project_root(monkeypatch, tmp_path):
    """Module with missing project_root uses root_dir; nonexistent dirs get error."""
    monkeypatch.setattr("scope_audit.get_git_status", lambda _: (set(), None))
    state = {"modules": {"m/m": _make_module()}}
    result = audit(state, str(tmp_path))
    assert "m/m" in result
    assert "error" not in result.get("m/m", {})


# ---------- audit_module (multi-repo, Commit 4) ----------

def test_audit_module_multi_repo_prefixes_paths(monkeypatch, tmp_path):
    """Declared in repo A, actually changed in repo B: A shows phantom,
    B shows unexplained; both paths carry the repo prefix (relpath to root)."""
    repo_a = tmp_path / "kunhe-wms"
    repo_b = tmp_path / "opc-sna"
    repo_a.mkdir()
    repo_b.mkdir()
    declared = str(repo_a / "inventory/src/main/java/Foo.java")

    def fake_git_status(root):
        if str(root).endswith("kunhe-wms"):
            return (set(), None)                # A: nothing changed
        return ({"consumer/src/main/java/Bar.java"}, None)  # B: unrelated change

    monkeypatch.setattr("scope_audit.get_git_status", fake_git_status)
    mod = _make_module(files_modified=[declared])
    result = audit_module(mod, [str(repo_a), str(repo_b)], str(tmp_path))
    assert os.path.join("kunhe-wms",
                        "inventory/src/main/java/Foo.java") in result["phantom"]
    assert os.path.join("opc-sna",
                        "consumer/src/main/java/Bar.java") in result["unexplained"]
    assert result["clean"] is False


def test_audit_module_multi_repo_all_green_when_each_side_matches(monkeypatch, tmp_path):
    """Both repos report the corresponding declared file => clean."""
    repo_a = tmp_path / "kunhe-wms"
    repo_b = tmp_path / "opc-sna"
    repo_a.mkdir()
    repo_b.mkdir()
    declared_a = str(repo_a / "inventory/src/main/java/Foo.java")
    declared_b = str(repo_b / "consumer/src/main/java/Bar.java")

    def fake_git_status(root):
        if str(root).endswith("kunhe-wms"):
            return ({"inventory/src/main/java/Foo.java"}, None)
        return ({"consumer/src/main/java/Bar.java"}, None)

    monkeypatch.setattr("scope_audit.get_git_status", fake_git_status)
    mod = _make_module(files_modified=[declared_a, declared_b])
    result = audit_module(mod, [str(repo_a), str(repo_b)], str(tmp_path))
    assert result["clean"] is True
    assert result["phantom"] == []
    assert result["unexplained"] == []


def test_audit_module_scalar_root_still_works(monkeypatch, tmp_path):
    """Legacy scalar project_root argument remains accepted."""
    monkeypatch.setattr("scope_audit.get_git_status",
                        lambda _: ({"Foo.java"}, None))
    mod = _make_module(files_modified=[str(tmp_path / "Foo.java")])
    result = audit_module(mod, str(tmp_path))  # str, not list
    assert result["clean"] is True


def test_audit_module_error_from_any_repo_bubbles_up(monkeypatch, tmp_path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    def fake_git_status(root):
        if str(root).endswith("/b"):
            return (set(), "boom")
        return (set(), None)

    monkeypatch.setattr("scope_audit.get_git_status", fake_git_status)
    mod = _make_module()
    result = audit_module(mod, [str(repo_a), str(repo_b)], str(tmp_path))
    assert "error" in result
    assert "boom" in result["error"]


# ---------- format_report ----------

def test_format_report_clean():
    results = {
        "change/mod1": {
            "status": "SYNCED",
            "declared": ["Foo.java"],
            "actual": ["Foo.java"],
            "unexplained": [],
            "phantom": [],
            "clean": True,
        },
    }
    text = format_report(results)
    assert "一致" in text
    assert "3 modules" not in text  # only 1, won't say 3


def test_format_report_gap():
    results = {
        "change/mod1": {
            "status": "SYNCED",
            "declared": [],
            "actual": ["Unexplained.java"],
            "unexplained": ["Unexplained.java"],
            "phantom": [],
            "clean": False,
        },
    }
    text = format_report(results)
    assert "未申报改动" in text
    assert "1 with gaps" in text


def test_format_report_error():
    results = {"change/mod1": {"error": "project_root not found: /nope"}}
    text = format_report(results)
    assert "ERROR" in text


# ---------- integration smoke test ----------

def test_cmd_scope_audit_integration(monkeypatch, tmp_path, capsys):
    """Smoke test: create state.json, mock git status, run cmd."""
    loop_dir = tmp_path / ".loop"
    loop_dir.mkdir()
    state = {
        "modules": {
            "change/m1": {
                "status": "SYNCED",
                "project_root": str(tmp_path),
                "files_created": [],
                "files_modified": [str(tmp_path / "Foo.java")],
            },
        },
    }
    (loop_dir / "state.json").write_text(json.dumps(state))

    monkeypatch.setattr(
        "scope_audit.get_git_status",
        lambda _: ({"Foo.java"}, None),
    )

    class Args:
        root = str(tmp_path)

    cmd_scope_audit(Args())
    captured = capsys.readouterr()
    assert "一致" in captured.out or "✓" in captured.out