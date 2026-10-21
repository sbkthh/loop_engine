"""Tests for `loop_engine prune-worktrees` — CLOSED gate, clean + merged checks."""

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import registry


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


class TestPruneWorktrees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_registry_path = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        self.root = os.path.join(self.tmp.name, "req")
        os.makedirs(self.root)
        self.remote = os.path.join(self.tmp.name, "remote.git")
        _git("init", "-q", "--bare", self.remote)
        self.src = os.path.join(self.tmp.name, "src")
        _git("init", "-q", "-b", "master", self.src)
        _git("config", "user.email", "t@t", cwd=self.src)
        _git("config", "user.name", "t", cwd=self.src)
        _git("config", "commit.gpgsign", "false", cwd=self.src)
        with open(os.path.join(self.src, "a.txt"), "w") as f:
            f.write("a\n")
        _git("add", ".", cwd=self.src)
        _git("commit", "-qm", "init", cwd=self.src)
        _git("remote", "add", "origin", self.remote, cwd=self.src)
        _git("push", "-q", "-u", "origin", "master", cwd=self.src)
        _git("remote", "set-head", "origin", "master", cwd=self.src)
        self.wt = os.path.join(self.root, "src")
        _git("worktree", "add", "-q", "-b", "feature/x", self.wt, cwd=self.src)
        registry.add_requirement(
            "req", self.root,
            projects=[{"name": "src", "source": self.src}])

    def tearDown(self):
        registry.REGISTRY_PATH = self._orig_registry_path
        self.tmp.cleanup()

    def _commit_in_wt(self):
        with open(os.path.join(self.wt, "b.txt"), "w") as f:
            f.write("b\n")
        _git("add", "b.txt", cwd=self.wt)
        _git("commit", "-qm", "change", cwd=self.wt)

    def _merge_to_mainline(self):
        _git("merge", "--ff-only", "feature/x", cwd=self.src)
        _git("push", "-q", "origin", "master", cwd=self.src)

    def _run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_prune_worktrees(argparse.Namespace(name="req"))
        return out.getvalue()

    def test_removes_clean_merged_worktree_and_keeps_branch(self):
        registry.close_requirement("req")
        self._commit_in_wt()
        self._merge_to_mainline()
        out = self._run()
        self.assertIn("已删除 worktree: src", out)
        self.assertFalse(os.path.isdir(self.wt))
        branches = _git("branch", "--list", "feature/x", cwd=self.src).stdout
        self.assertIn("feature/x", branches)

    def test_skips_dirty_worktree(self):
        registry.close_requirement("req")
        self._commit_in_wt()
        self._merge_to_mainline()
        with open(os.path.join(self.wt, "dirty.txt"), "w") as f:
            f.write("x\n")
        out = self._run()
        self.assertIn("工作区有未提交改动", out)
        self.assertTrue(os.path.isdir(self.wt))

    def test_skips_unmerged_worktree(self):
        registry.close_requirement("req")
        self._commit_in_wt()
        out = self._run()
        self.assertIn("未合入", out)
        self.assertTrue(os.path.isdir(self.wt))

    def test_refuses_active_requirement(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            cli.cmd_prune_worktrees(argparse.Namespace(name="req"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("需求未结项", out.getvalue())
        self.assertTrue(os.path.isdir(self.wt))

    def test_idempotent_after_removal(self):
        registry.close_requirement("req")
        self._commit_in_wt()
        self._merge_to_mainline()
        self._run()
        self.assertIn("没有可清理的 worktree", self._run())


if __name__ == "__main__":
    unittest.main()
