"""Tests for cli.session_clean — old session file cleanup, per backend store."""

import io
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_cli
import cli
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
        return self._make_in(self.proj, name, age_days)

    def _make_in(self, base, name, age_days):
        jsonl = os.path.join(base, name + ".jsonl")
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

    def test_command_sweeps_every_backend_store(self):
        """P4: only ~/.qoder/projects was ever passed, so the weekly cron was a
        no-op for pi and its sessions grew without bound."""
        stores = []
        for name in ("qoder-store", "pi-store"):
            store = os.path.join(self.tmp.name, name)
            os.makedirs(os.path.join(store, "proj"))
            stores.append(store)
        aged = self._make_in(os.path.join(stores[0], "proj"), "old-a", 60)
        pi_aged = self._make_in(os.path.join(stores[1], "proj"), "old-b", 60)
        fresh = self._make_in(os.path.join(stores[1], "proj"), "new-c", 1)
        args = SimpleNamespace(older_than=self.days, dry_run=False)

        with mock.patch.object(agent_cli, "session_dirs",
                               return_value=stores), \
                mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            cli.cmd_session_clean(args)
        self.assertFalse(os.path.exists(aged))
        self.assertFalse(os.path.exists(pi_aged))
        self.assertTrue(os.path.exists(fresh))
