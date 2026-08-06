"""Tests for scheduler.py — poll/detect, pending, approve, lock, run, dispatch."""

import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler


def _make_state(root, modules, current=None):
    os.makedirs(os.path.join(root, ".loop"), exist_ok=True)
    state = {
        "version": 1,
        "root_dir": root,
        "current": current or {"module": None, "action": None, "attempt": 0},
        "modules": modules,
        "gray_drafts": [],
        "trace": [],
        "audit_trail": [],
    }
    with open(os.path.join(root, ".loop", "state.json"), "w") as f:
        json.dump(state, f)


def _make_spec(root, change_id, module_name, content="spec content"):
    p = os.path.join(root, "openspec", "changes", change_id, "specs",
                     module_name, "spec.md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)
    return hashlib.md5(content.encode()).hexdigest()


def _module(change_id, name, status, spec_hash=None):
    return {
        "change_id": change_id,
        "module_name": name,
        "project_root": ".",
        "status": status,
        "spec_hash": spec_hash,
        "maker_attempt": 0,
        "review_fix_attempt": 0,
        "files_created": [],
        "files_modified": [],
        "plan_path": None,
        "last_synced": None,
    }


class SchedulerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._paths = {
            "REGISTRY_PATH": scheduler.REGISTRY_PATH,
            "PENDING_PATH": scheduler.PENDING_PATH,
            "CONFIG_PATH": scheduler.CONFIG_PATH,
            "LOG_PATH": scheduler.LOG_PATH,
        }
        scheduler.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        scheduler.PENDING_PATH = os.path.join(self.tmp.name, "pending.json")
        scheduler.CONFIG_PATH = os.path.join(self.tmp.name, "schedule.json")
        scheduler.LOG_PATH = os.path.join(self.tmp.name, "schedule.log")

    def tearDown(self):
        for attr, value in self._paths.items():
            setattr(scheduler, attr, value)
        self.tmp.cleanup()

    def register(self, name, root):
        data = {"requirements": []}
        if os.path.exists(scheduler.REGISTRY_PATH):
            with open(scheduler.REGISTRY_PATH) as f:
                data = json.load(f)
        data["requirements"].append({"name": name, "root": root,
                                     "registered_at": "t"})
        with open(scheduler.REGISTRY_PATH, "w") as f:
            json.dump(data, f)
        return root


class TestPoll(SchedulerBase):
    def test_poll_synced_hash_changed(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        h = _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "SYNCED", spec_hash=h)})
        _make_spec(root, "c", "m", content="changed spec")

        entries = scheduler.poll()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["requirement"], "req")
        self.assertEqual(e["trigger"], "SPEC_CHANGED")
        self.assertEqual(e["modules"][0]["status"], "PARTIAL")
        self.assertTrue(e["modules"][0]["spec_hash_changed"])
        self.assertFalse(e["approved"])

    def test_poll_ready(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")})

        entries = scheduler.poll()
        self.assertEqual(entries[0]["trigger"], "READY_PENDING")

    def test_poll_needs_refinement_report_only(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "NEEDS_REFINEMENT")})

        entries = scheduler.poll()
        self.assertEqual(entries[0]["trigger"], "NEEDS_REFINEMENT")
        self.assertNotIn(entries[0]["trigger"], scheduler.AUTO_EXECUTABLE)

    def test_poll_skips_mid_progress(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")},
                    current={"module": "c/m", "action": "SCORE", "attempt": 0})

        self.assertEqual(scheduler.poll(), [])

    def test_poll_detects_new_draft_module(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {})
        _make_spec(root, "c", "brand-new-module")

        entries = scheduler.poll()
        self.assertEqual(entries[0]["trigger"], "DRAFT")
        self.assertEqual(entries[0]["modules"][0]["key"],
                         "c/brand-new-module")

    def test_poll_no_pending_when_synced_unchanged(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        h = _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "SYNCED", spec_hash=h)})

        self.assertEqual(scheduler.poll(), [])

    def test_poll_skips_uninitialized_requirement(self):
        self.register("req", os.path.join(self.tmp.name, "req"))
        self.assertEqual(scheduler.poll(), [])

    def test_poll_preserves_approval_and_detected_at(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        h = _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "SYNCED", spec_hash=h)})
        _make_spec(root, "c", "m", content="changed")

        scheduler.poll()
        scheduler.approve("req")
        first = scheduler.load_pending()["pending"][0]
        entries = scheduler.poll()
        self.assertTrue(entries[0]["approved"])
        self.assertEqual(entries[0]["detected_at"], first["detected_at"])


class TestApprove(SchedulerBase):
    def test_approve_auto_executable(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        scheduler.poll()

        self.assertEqual(scheduler.approve("req"), 1)
        entry = scheduler.load_pending()["pending"][0]
        self.assertTrue(entry["approved"])
        self.assertEqual(scheduler.approve("req"), 0)

    def test_approve_report_only_raises(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "NEEDS_REFINEMENT")})
        scheduler.poll()

        with self.assertRaises(ValueError):
            scheduler.approve("req")

    def test_approve_unknown_raises(self):
        with self.assertRaises(ValueError):
            scheduler.approve("ghost")

    def test_approve_all_only_auto(self):
        r1 = self.register("req-a", os.path.join(self.tmp.name, "req-a"))
        _make_spec(r1, "c", "m")
        _make_state(r1, {"c/m": _module("c", "m", "READY")})
        r2 = self.register("req-b", os.path.join(self.tmp.name, "req-b"))
        _make_spec(r2, "c", "m")
        _make_state(r2, {"c/m": _module("c", "m", "NEEDS_REFINEMENT")})
        scheduler.poll()

        self.assertEqual(scheduler.approve(all_=True), 1)
        pending = scheduler.load_pending()["pending"]
        by_name = {e["requirement"]: e for e in pending}
        self.assertTrue(by_name["req-a"]["approved"])
        self.assertFalse(by_name["req-b"]["approved"])


class TestLock(SchedulerBase):
    def test_acquire_release(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(root, exist_ok=True)
        self.assertTrue(scheduler.acquire_lock(root))
        self.assertTrue(scheduler.is_locked(root))
        scheduler.release_lock(root)
        self.assertFalse(scheduler.is_locked(root))

    def test_acquire_live_lock_fails(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(os.path.join(root, ".loop"), exist_ok=True)
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))
        self.assertFalse(scheduler.acquire_lock(root))

    def test_acquire_reclaims_stale_lock(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(os.path.join(root, ".loop"), exist_ok=True)
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write("99999999")
        self.assertTrue(scheduler.acquire_lock(root))
        self.assertTrue(scheduler.is_locked(root))

    def test_release_only_own_lock(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(os.path.join(root, ".loop"), exist_ok=True)
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write("99999999")
        scheduler.release_lock(root)
        self.assertTrue(os.path.exists(os.path.join(root, ".loop", "lock")))


class TestRun(SchedulerBase):
    def _fake_run(self, next_actions, commit_next="MAKER_STEP0",
                  commit_error=None):
        def fake_run(cmd, **kwargs):
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    action = next_actions.pop(0) if next_actions else "IDLE"
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": action,
                                           "module": "c/m"}), returncode=0)
                if sub == "commit":
                    if commit_error:
                        return types.SimpleNamespace(
                            stdout=json.dumps({"error": commit_error}),
                            returncode=0)
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": commit_next}),
                        returncode=0)
            return types.SimpleNamespace(stdout="", returncode=0)
        return fake_run

    def _register_pending(self, name):
        root = self.register(name, os.path.join(self.tmp.name, name))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        scheduler.poll()
        scheduler.approve(name)
        return root

    def test_run_to_idle(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"])
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "idle")
        self.assertEqual(result["steps"], 3)
        self.assertFalse(scheduler.is_locked(root))
        self.assertEqual(scheduler.load_pending()["pending"], [])
        with open(scheduler.LOG_PATH) as f:
            log = f.read()
        self.assertIn("run req: start", log)
        self.assertIn("finished (idle)", log)

    def test_run_commit_error_stops(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE"],
                              commit_error="No result file")
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "commit_error")
        self.assertFalse(scheduler.is_locked(root))

    def test_run_no_advance_stops(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"], commit_next=None)
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "no_advance")
        self.assertEqual(result["steps"], 1)
        self.assertFalse(scheduler.is_locked(root))

    def test_run_same_action_cap(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE"] * 10, commit_next="SCORE")
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "repeat_limit")
        self.assertEqual(result["steps"], 3)
        self.assertFalse(scheduler.is_locked(root))

    def test_run_not_registered(self):
        result = scheduler.run_requirement("ghost")
        self.assertIn("error", result)

    def test_run_locked_requirement_fails(self):
        root = self._register_pending("req")
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))
        result = scheduler.run_requirement("req")
        self.assertIn("error", result)


class TestDispatch(SchedulerBase):
    def _pending_entry(self, name, trigger, approved=True):
        return {
            "requirement": name,
            "root": os.path.join(self.tmp.name, name),
            "trigger": trigger,
            "modules": [{"key": "c/m", "status": "READY"}],
            "detected_at": "t",
            "approved": approved,
        }

    def test_dispatch_respects_concurrency(self):
        entries = [self._pending_entry("req-a", "READY_PENDING"),
                   self._pending_entry("req-b", "READY_PENDING")]
        with mock.patch.object(scheduler.subprocess, "Popen",
                               return_value=types.SimpleNamespace(pid=1)) as p:
            forked = scheduler.dispatch(entries, max_concurrency=1)

        self.assertEqual(forked, ["req-a"])
        self.assertEqual(p.call_count, 1)
        cmd = p.call_args[0][0]
        self.assertEqual(cmd[-2:], ["run", "req-a"])

    def test_dispatch_forks_all_up_to_limit(self):
        entries = [self._pending_entry("req-a", "READY_PENDING"),
                   self._pending_entry("req-b", "READY_PENDING")]
        with mock.patch.object(scheduler.subprocess, "Popen",
                               return_value=types.SimpleNamespace(pid=2)):
            forked = scheduler.dispatch(entries, max_concurrency=2)

        self.assertEqual(sorted(forked), ["req-a", "req-b"])

    def test_dispatch_skips_unapproved_and_report_only(self):
        entries = [
            self._pending_entry("req-a", "READY_PENDING", approved=False),
            self._pending_entry("req-b", "NEEDS_REFINEMENT"),
        ]
        with mock.patch.object(scheduler.subprocess, "Popen",
                               return_value=types.SimpleNamespace(pid=3)):
            forked = scheduler.dispatch(entries, max_concurrency=2)

        self.assertEqual(forked, [])

    def test_dispatch_skips_locked(self):
        root = os.path.join(self.tmp.name, "req-a")
        os.makedirs(os.path.join(root, ".loop"), exist_ok=True)
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))
        entries = [self._pending_entry("req-a", "READY_PENDING")]
        with mock.patch.object(scheduler.subprocess, "Popen",
                               return_value=types.SimpleNamespace(pid=4)):
            forked = scheduler.dispatch(entries, max_concurrency=2)

        self.assertEqual(forked, [])


class TestConfig(SchedulerBase):
    def test_default_config(self):
        cfg = scheduler.load_config()
        self.assertEqual(cfg["interval_minutes"], 5)
        self.assertEqual(cfg["max_concurrency"], 2)

    def test_set_interval_round_trip(self):
        scheduler.set_interval(10)
        self.assertEqual(scheduler.load_config()["interval_minutes"], 10)
        with self.assertRaises(ValueError):
            scheduler.set_interval(0)

    def test_set_max_concurrency_round_trip(self):
        scheduler.set_max_concurrency(3)
        self.assertEqual(scheduler.load_config()["max_concurrency"], 3)
        with self.assertRaises(ValueError):
            scheduler.set_max_concurrency(0)


if __name__ == "__main__":
    unittest.main()
