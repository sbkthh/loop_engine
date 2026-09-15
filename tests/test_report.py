"""LOOP_REPORT.md is one file per requirement, not per change."""

import os

import report


def _state():
    return {
        "current": {"module": None, "action": None},
        "modules": {
            "cd-001/dashboard": {"status": "SYNCED", "change_id": "cd-001"},
            "cd-002/picking": {"status": "READY", "change_id": "cd-002"},
        },
        "gray_drafts": [], "audit_trail": [], "trace": [],
    }


def test_report_lands_at_the_root_once(tmp_path):
    """It renders the whole state's module table, so writing it per change_id
    used to duplicate identical bytes into every package — and put them where
    `openspec archive` would carry them off."""
    report.write(_state(), str(tmp_path))

    assert (tmp_path / "LOOP_REPORT.md").is_file()
    assert not list(tmp_path.glob("openspec/**/LOOP_REPORT.md"))
    assert "cd-001/dashboard" in (tmp_path / "LOOP_REPORT.md").read_text()


def test_report_path_follows_the_template(tmp_path):
    from constants import REPORT_PATH_TEMPLATE

    assert report.derive_report_path(str(tmp_path)) == os.path.join(
        str(tmp_path), REPORT_PATH_TEMPLATE)
