"""Tests for WeCom handlers."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server.handlers.approve import handle_approve


def test_handle_approve_no_pending():
    with tempfile.TemporaryDirectory() as tmp:
        result = handle_approve("批准", [], tmp)
        assert "没有待审批" in result


def test_handle_approve_with_pending():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        with open(pending_path, "w") as f:
            json.dump({"pending": [
                {"requirement": "test-req", "root": tmp, "trigger": "SPEC_CHANGED",
                 "modules": [], "detected_at": "now", "approved": False}
            ]}, f)
        result = handle_approve("test-req", [], tmp)
        assert "test-req" in result
        assert "已批准" in result
        with open(pending_path) as f:
            data = json.load(f)
        assert data["pending"][0]["approved"] is True


def test_handle_approve_approve_all():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        with open(pending_path, "w") as f:
            json.dump({"pending": [
                {"requirement": "req-a", "root": tmp, "trigger": "SPEC_CHANGED",
                 "modules": [], "detected_at": "now", "approved": False},
                {"requirement": "req-b", "root": tmp, "trigger": "READY_PENDING",
                 "modules": [], "detected_at": "now", "approved": False},
            ]}, f)
        result = handle_approve("全部批准", [], tmp)
        assert "已批准 2 个需求" in result
        with open(pending_path) as f:
            data = json.load(f)
        assert data["pending"][0]["approved"] is True
        assert data["pending"][1]["approved"] is True


def test_handle_approve_already_approved():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        with open(pending_path, "w") as f:
            json.dump({"pending": [
                {"requirement": "test-req", "root": tmp, "trigger": "SPEC_CHANGED",
                 "modules": [], "detected_at": "now", "approved": True}
            ]}, f)
        result = handle_approve("test-req", [], tmp)
        assert "已在批准状态" in result


def test_handle_approve_report_only():
    with tempfile.TemporaryDirectory() as tmp:
        pending_path = os.path.join(tmp, "pending.json")
        with open(pending_path, "w") as f:
            json.dump({"pending": [
                {"requirement": "test-req", "root": tmp, "trigger": "NEEDS_REFINEMENT",
                 "modules": [], "detected_at": "now", "approved": False}
            ]}, f)
        result = handle_approve("test-req", [], tmp)
        assert "需要人工处理" in result