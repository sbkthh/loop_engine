"""Tests for cli.session_clean — old qodercli session files cleanup."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import session_clean


class TestSessionClean(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.days = 30
        self.proj = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.proj)

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, name, age_days):
        jsonl = os.path.join(self.proj, name + ".jsonl")
        with open(jsonl, "w") as f:
            f.write("{}")
        old = time.time() - age_days * 86400
        os.utime(jsonl, (old, old))
        return jsonl

    def test_removes_old_session_files_and_sibling_dirs(self):
        self._make("aaa", age_days=60)
        os.mkdir(os.path.join(self.proj, "aaa"))
        removed = session_clean(self.tmp.name, self.days)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(os.path.join(self.proj, "aaa.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "aaa")))

    def test_keeps_fresh_sessions(self):
        self._make("bbb", age_days=5)
        removed = session_clean(self.tmp.name, self.days)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(os.path.join(self.proj, "bbb.jsonl")))

    def test_ignores_non_session_files_and_dirs(self):
        with open(os.path.join(self.proj, "readme.txt"), "w") as f:
            f.write("keep")
        os.mkdir(os.path.join(self.tmp.name, "some-dir"))
        removed = session_clean(self.tmp.name, self.days)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(os.path.join(self.proj, "readme.txt")))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "some-dir")))

    def test_dry_run_reports_without_deleting(self):
        self._make("ccc", age_days=60)
        removed = session_clean(self.tmp.name, self.days, dry_run=True)
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.exists(os.path.join(self.proj, "ccc.jsonl")))

    def test_missing_projects_dir_is_noop(self):
        removed = session_clean(os.path.join(self.tmp.name, "nope"), self.days)
        self.assertEqual(removed, 0)
