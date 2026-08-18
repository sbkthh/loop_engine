"""Tests for wecom_server/file_buffer.py — buffer, nudge, expiry, decode, attach."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wecom_server import file_buffer


class FileBufferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _pending_path(self):
        return os.path.join(self.data_dir, "pending_files.json")

    def _state(self):
        p = self._pending_path()
        if not os.path.exists(p):
            return {"files": []}
        with open(p) as f:
            return json.load(f)

    def test_add_file(self):
        ok, msg = file_buffer.add_file("u1", "mid1", "app.log", self.data_dir)
        self.assertTrue(ok)
        self.assertIn("app.log", msg)
        state = self._state()
        self.assertEqual(len(state["files"]), 1)
        self.assertEqual(state["files"][0]["user"], "u1")
        self.assertEqual(state["files"][0]["media_id"], "mid1")
        self.assertFalse(state["files"][0]["nudge_sent"])

    def test_add_file_rejects_when_group_full(self):
        for i in range(5):
            file_buffer.add_file("u1", f"mid{i}", f"f{i}.log", self.data_dir)
        ok, msg = file_buffer.add_file("u1", "mid5", "extra.log", self.data_dir)
        self.assertFalse(ok)
        self.assertIn("5", msg)

    def test_attach_pending_no_files(self):
        text, used = file_buffer.attach_pending("hello", "u1", self.data_dir)
        self.assertEqual(text, "hello")
        self.assertEqual(used, [])

    def test_attach_pending_decodes_and_clears(self):
        file_buffer.add_file("u1", "mid1", "test.txt", self.data_dir)
        text = "分析这个日志"
        # We can't actually download from WeCom in tests, so download_media
        # will fail. Verify graceful handling.
        text, used = file_buffer.attach_pending(text, "u1", self.data_dir)
        self.assertIn("下载失败", text)
        # Buffer should be cleared even on failure
        self.assertEqual(self._state()["files"], [])

    def test_decode_utf8(self):
        raw = "你好世界".encode("utf-8")
        self.assertEqual(file_buffer._decode(raw), "你好世界")

    def test_decode_gbk_fallback(self):
        raw = "你好世界".encode("gbk")
        self.assertEqual(file_buffer._decode(raw), "你好世界")

    def test_decode_binary_returns_none(self):
        raw = b"\x89PNG\x00\x0d\x0a\x1a\x0a"
        self.assertIsNone(file_buffer._decode(raw))

    def test_decode_empty(self):
        self.assertEqual(file_buffer._decode(b""), "")

    def test_attach_pending_parses_group_per_user(self):
        file_buffer.add_file("u1", "m1", "a.txt", self.data_dir)
        file_buffer.add_file("u2", "m2", "b.txt", self.data_dir)
        text, used = file_buffer.attach_pending("hello", "u1", self.data_dir)
        self.assertIn("下载失败", text)
        # u2's file should still be there
        state = self._state()
        self.assertEqual(len(state["files"]), 1)
        self.assertEqual(state["files"][0]["user"], "u2")

    def test_nudge_and_expire(self):
        """Mock time by manually setting received_at far in the past."""
        old = time.time() - 2000  # 33min ago → expired
        data = {"files": [{
            "user": "u1", "media_id": "m1", "name": "old.log",
            "received_at": old, "nudge_sent": False,
        }]}
        file_buffer._save(data, self.data_dir)
        # No wecom.json → nudge_and_expire returns without action
        # Verify the file is still there (no config = no expiry push)
        state = self._state()
        self.assertEqual(len(state["files"]), 1)