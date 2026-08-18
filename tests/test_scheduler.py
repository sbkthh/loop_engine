"""Tests for scheduler.py — poll/detect, pending, approve, lock, run, dispatch."""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler


def _make_state(root, modules, current=None, drafts=None):
    os.makedirs(os.path.join(root, ".loop"), exist_ok=True)
    state = {
        "version": 1,
        "root_dir": root,
        "current": current or {"module": None, "action": None, "attempt": 0},
        "modules": modules,
        "gray_drafts": drafts if drafts is not None else [],
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


class TestStatusTableSemantics(unittest.TestCase):
    def test_auto_exec_matches_old_trigger_semantics(self):
        """STATUS_TABLE auto_exec preserves old AUTO_EXECUTABLE coverage:
        only PARTIAL/READY (SPEC_CHANGED/READY_PENDING) are executable."""
        from constants import STATUS_TABLE

        auto = {s for s, v in STATUS_TABLE.items() if v["auto_exec"]}
        self.assertEqual(auto, {scheduler.PARTIAL, scheduler.READY})
        self.assertEqual(STATUS_TABLE[scheduler.PARTIAL]["trigger"],
                         scheduler.SPEC_CHANGED)
        self.assertEqual(STATUS_TABLE[scheduler.READY]["trigger"],
                         scheduler.READY_PENDING)


class SchedulerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._paths = {
            "REGISTRY_PATH": scheduler.REGISTRY_PATH,
            "PENDING_PATH": scheduler.PENDING_PATH,
            "CONFIG_PATH": scheduler.CONFIG_PATH,
            "LOG_PATH": scheduler.LOG_PATH,
            "RUNS_PATH": scheduler.RUNS_PATH,
        }
        scheduler.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        scheduler.PENDING_PATH = os.path.join(self.tmp.name, "pending.json")
        scheduler.CONFIG_PATH = os.path.join(self.tmp.name, "schedule.json")
        scheduler.LOG_PATH = os.path.join(self.tmp.name, "schedule.log")
        scheduler.RUNS_PATH = os.path.join(self.tmp.name, "runs.json")
        # poll()/run_requirement() would otherwise send real WeCom pushes
        self._notify_pending_orig = scheduler.notify_pending
        scheduler.notify_pending = mock.MagicMock()
        self._notify_text_orig = scheduler.notify_text
        scheduler.notify_text = mock.MagicMock()

    def tearDown(self):
        scheduler.notify_text = self._notify_text_orig
        scheduler.notify_pending = self._notify_pending_orig
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

    def _fake_run(self, next_actions, commit_next="MAKER_STEP0",
                  commit_error=None, qodercli_failures=0, commit_failures=0):
        q_left = [qodercli_failures]
        c_left = [commit_failures]

        def fake_run(cmd, **kwargs):
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    action = next_actions.pop(0) if next_actions else "IDLE"
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": action,
                                           "module": "c/m"}), stderr="", returncode=0)
                if sub == "commit":
                    if commit_error:
                        return types.SimpleNamespace(
                            stdout=json.dumps({"error": commit_error}),
                            stderr="", returncode=0)
                    if c_left[0] > 0:
                        c_left[0] -= 1
                        return types.SimpleNamespace(
                            stdout=json.dumps({"error": "transient"}),
                            stderr="", returncode=0)
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": commit_next}),
                        stderr="", returncode=0)
            if q_left[0] > 0 and any("qodercli" in part for part in cmd):
                q_left[0] -= 1
                return types.SimpleNamespace(stdout="", stderr="boom", returncode=1)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)
        return fake_run

    def _register_pending(self, name):
        root = self.register(name, os.path.join(self.tmp.name, name))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        scheduler.poll()
        scheduler.approve(name)
        return root


class TestPoll(SchedulerBase):
    def _state_with_gray_drafts(self, root, drafts):
        state_path = os.path.join(root, ".loop", "state.json")
        with open(state_path) as f:
            state = json.load(f)
        state["gray_drafts"] = drafts
        with open(state_path, "w") as f:
            json.dump(state, f)

    def test_poll_gray_list_trigger_when_drafts_pending(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        self._state_with_gray_drafts(root, [
            {"id": 1, "module": "c/m", "summary": "warn",
             "status": "pending"}])

        entries = scheduler.poll()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["trigger"], "GRAY_LIST")

    def test_poll_ready_trigger_after_drafts_resolved(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        self._state_with_gray_drafts(root, [
            {"id": 1, "module": "c/m", "summary": "warn",
             "status": "accepted"}])

        entries = scheduler.poll()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["trigger"], "READY_PENDING")

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
        self.assertFalse(scheduler._entry_auto_exec(entries[0]))

    def test_poll_blocked_report_only(self):
        """A BLOCKED module (checker hard errors exhausted) is report-only:
        poll notifies, approve refuses — no re-approval into the loop."""
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "BLOCKED")})

        entries = scheduler.poll()
        self.assertEqual(entries[0]["trigger"], "BLOCKED")
        self.assertFalse(scheduler._entry_auto_exec(entries[0]))
        with self.assertRaises(ValueError) as ctx:
            scheduler.approve("req")
        self.assertIn("报告状态", str(ctx.exception))

    def test_poll_skips_mid_progress(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")},
                    current={"module": "c/m", "action": "SCORE", "attempt": 0})
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))

        self.assertEqual(scheduler.poll(), [])

    def test_poll_detects_stale_current_without_lock(self):
        """A stale current.action with no lock must not hide pending work."""
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")},
                    current={"module": "c/m", "action": "SCORE", "attempt": 0})

        entries = scheduler.poll()
        self.assertEqual(entries[0]["trigger"], "READY_PENDING")

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

    def test_poll_carries_approved_entry_while_locked(self):
        """Mid-execution polls must not drop the approved entry — a
        gray_list pause stays resumable only if the entry survives the run."""
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        scheduler.poll()
        scheduler.approve("req")
        # run in progress: lock held + current.action set → _poll_requirement
        # returns None, but the approved entry must be carried over
        _make_state(root, {"c/m": _module("c", "m", "READY")},
                    current={"module": "c/m", "action": "CHECKER", "attempt": 0})
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))

        self.assertEqual(scheduler.poll(), [])
        entries = scheduler.load_pending()["pending"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["requirement"], "req")
        self.assertTrue(entries[0]["approved"])

        # run finished (unlocked): re-detected, approval preserved
        os.remove(os.path.join(root, ".loop", "lock"))
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        entries = scheduler.poll()
        self.assertTrue(entries[0]["approved"])

    def test_poll_drops_unapproved_entry_while_locked(self):
        """Carry-over applies only to approved entries — an unapproved
        entry must not be revived by a mid-execution poll."""
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        scheduler.poll()
        _make_state(root, {"c/m": _module("c", "m", "READY")},
                    current={"module": "c/m", "action": "CHECKER", "attempt": 0})
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))

        self.assertEqual(scheduler.poll(), [])
        self.assertEqual(scheduler.load_pending()["pending"], [])



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

    def test_approve_blocked_by_pending_gray_drafts(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")},
                    drafts=[{"id": 1, "module": "c/m", "status": "pending",
                             "summary": "s", "type_label": "SOFT_WARNING"}])
        scheduler.poll()

        with self.assertRaises(ValueError) as ctx:
            scheduler.approve("req")
        self.assertIn("灰名单", str(ctx.exception))
        entry = scheduler.load_pending()["pending"][0]
        self.assertFalse(entry["approved"])

    def test_approve_all_skips_pending_gray_drafts(self):
        r1 = self.register("req-a", os.path.join(self.tmp.name, "req-a"))
        _make_spec(r1, "c", "m")
        _make_state(r1, {"c/m": _module("c", "m", "READY")})
        r2 = self.register("req-b", os.path.join(self.tmp.name, "req-b"))
        _make_spec(r2, "c", "m")
        _make_state(r2, {"c/m": _module("c", "m", "READY")},
                    drafts=[{"id": 1, "module": "c/m", "status": "pending",
                             "summary": "s", "type_label": "SOFT_WARNING"}])
        scheduler.poll()

        self.assertEqual(scheduler.approve(all_=True), 1)
        by_name = {e["requirement"]: e
                   for e in scheduler.load_pending()["pending"]}
        self.assertTrue(by_name["req-a"]["approved"])
        self.assertFalse(by_name["req-b"]["approved"])

    def test_approve_no_pending_synced_friendly(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "SYNCED")})

        with self.assertRaises(ValueError) as ctx:
            scheduler.approve("req")
        self.assertIn("已执行完成", str(ctx.exception))

    def test_approve_no_pending_mid_progress_friendly(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "READY")},
                    current={"module": "c/m", "action": "MAKER_STEP0",
                             "attempt": 0})
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))

        with self.assertRaises(ValueError) as ctx:
            scheduler.approve("req")
        self.assertIn("正在执行中", str(ctx.exception))

    def test_approve_stale_current_reports_waiting(self):
        """Stale current.action without lock → not 'executing', just no work."""
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "PARTIAL")},
                    current={"module": "c/m", "action": "SCORE", "attempt": 0})

        with self.assertRaises(ValueError) as ctx:
            scheduler.approve("req")
        self.assertIn("等待下次 poll", str(ctx.exception))

    def test_approve_records_approved_by(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_spec(root, "c", "m")
        _make_state(root, {"c/m": _module("c", "m", "READY")})
        scheduler.poll()

        self.assertEqual(scheduler.approve("req", approved_by="LiChuan"), 1)
        entry = scheduler.load_pending()["pending"][0]
        self.assertEqual(entry["approved_by"], "LiChuan")


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

    def test_acquire_reclaims_stale_live_lock(self):
        root = os.path.join(self.tmp.name, "req")
        lock = os.path.join(root, ".loop", "lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        old = time.time() - scheduler.LOCK_MAX_AGE_SECONDS - 60
        os.utime(lock, (old, old))
        self.assertTrue(scheduler.acquire_lock(root))
        self.assertTrue(scheduler.is_locked(root))

    def test_acquire_fresh_live_lock_kept(self):
        root = os.path.join(self.tmp.name, "req")
        lock = os.path.join(root, ".loop", "lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        os.utime(lock, None)  # fresh mtime
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

    def test_lock_heartbeat_refreshes_mtime(self):
        """A run's heartbeat thread keeps the lock fresh even when no step
        boundary touches it — a hung subprocess must never let the 24h
        staleness check reclaim the lock mid-run."""
        root = os.path.join(self.tmp.name, "req")
        lock = os.path.join(root, ".loop", "lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        old = time.time() - 3600
        os.utime(lock, (old, old))
        stop = threading.Event()
        with mock.patch.object(scheduler, "LOCK_HEARTBEAT_SECONDS", 0.05):
            th = threading.Thread(target=scheduler._lock_heartbeat,
                                  args=(root, stop), daemon=True)
            th.start()
            time.sleep(0.3)
            stop.set()
            th.join(1)
        self.assertGreater(os.path.getmtime(lock), old)

    def test_lock_heartbeat_stops_when_lock_released(self):
        root = os.path.join(self.tmp.name, "req")
        lock = os.path.join(root, ".loop", "lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        stop = threading.Event()
        with mock.patch.object(scheduler, "LOCK_HEARTBEAT_SECONDS", 0.05):
            th = threading.Thread(target=scheduler._lock_heartbeat,
                                  args=(root, stop), daemon=True)
            th.start()
            time.sleep(0.2)
            os.unlink(lock)
            th.join(1)
        self.assertFalse(th.is_alive())  # exits on OSError, not forever

    def test_manual_begin_locks(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(root, exist_ok=True)
        self.assertTrue(scheduler.manual_begin(root))
        self.assertTrue(scheduler.is_locked(root))
        self.assertTrue(os.path.exists(os.path.join(root, scheduler.MANUAL_FILE)))

    def test_manual_begin_fails_when_scheduler_runs(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(root, exist_ok=True)
        scheduler.acquire_lock(root)
        self.assertFalse(scheduler.manual_begin(root))

    def test_manual_end_releases_own_lock(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(root, exist_ok=True)
        scheduler.manual_begin(root)
        self.assertTrue(scheduler.manual_end(root))
        self.assertFalse(scheduler.is_locked(root))
        self.assertFalse(os.path.exists(os.path.join(root, scheduler.MANUAL_FILE)))

    def test_manual_end_refuses_replaced_lock(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(root, exist_ok=True)
        scheduler.manual_begin(root)
        # manual session died and a scheduler run took over the lock
        import subprocess as sp
        proc = sp.Popen(["sleep", "60"])
        try:
            with open(os.path.join(root, ".loop", "lock"), "w") as f:
                f.write(str(proc.pid))
            self.assertFalse(scheduler.manual_end(root))
            self.assertTrue(scheduler.is_locked(root))
        finally:
            proc.kill()

    def test_manual_begin_blocks_scheduler_run(self):
        root = self._register_pending("req")
        self.assertTrue(scheduler.manual_begin(root))
        result = scheduler.run_requirement("req")
        self.assertIn("error", result)
        self.assertIn("lock held", result["error"])

    def test_manual_run_records_history(self):
        root = self._register_pending("req")
        scheduler.manual_begin(root)
        scheduler.manual_step(root)
        scheduler.manual_step(root)
        self.assertTrue(scheduler.manual_end(root))

        runs = scheduler.load_runs()["runs"]
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r["requirement"], "req")
        self.assertEqual(r["end"], "idle")
        self.assertEqual(r["steps"], 2)

    def test_manual_end_records_manual_stop_when_mid_progress(self):
        root = self._register_pending("req")
        with open(os.path.join(root, ".loop", "state.json")) as f:
            state = json.load(f)
        state["current"] = {"module": "c/m", "action": "MAKER_STEP0",
                            "attempt": 0}
        with open(os.path.join(root, ".loop", "state.json"), "w") as f:
            json.dump(state, f)
        scheduler.manual_begin(root)
        self.assertTrue(scheduler.manual_end(root))

        runs = scheduler.load_runs()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["end"], "manual_stop")

    def test_manual_step_noop_without_session(self):
        root = os.path.join(self.tmp.name, "req")
        os.makedirs(root, exist_ok=True)
        scheduler.manual_step(root)  # must not raise
        self.assertEqual(scheduler.load_runs()["runs"], [])

    def test_poll_auto_ends_dead_manual_session(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "SYNCED")})
        scheduler.manual_begin(root)
        # simulate the manual session dying
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write("999999999")
        with open(os.path.join(root, scheduler.MANUAL_FILE), "w") as f:
            json.dump({"pid": 999999999, "started_at": time.time(),
                       "steps": 4}, f)

        scheduler.poll()

        self.assertFalse(os.path.exists(os.path.join(root, ".loop", "lock")))
        self.assertFalse(
            os.path.exists(os.path.join(root, scheduler.MANUAL_FILE)))
        runs = scheduler.load_runs()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["requirement"], "req")
        self.assertEqual(runs[0]["steps"], 4)

    def test_poll_removes_stale_manual_but_keeps_replaced_lock(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "SYNCED")})
        import subprocess as sp
        proc = sp.Popen(["sleep", "60"])
        try:
            with open(os.path.join(root, ".loop", "lock"), "w") as f:
                f.write(str(proc.pid))
            with open(os.path.join(root, scheduler.MANUAL_FILE), "w") as f:
                json.dump({"pid": 999999999, "started_at": time.time(),
                           "steps": 2}, f)

            scheduler.poll()

            self.assertTrue(scheduler.is_locked(root))
            self.assertFalse(
                os.path.exists(os.path.join(root, scheduler.MANUAL_FILE)))
            self.assertEqual(scheduler.load_runs()["runs"], [])
        finally:
            proc.kill()

    def test_poll_keeps_live_manual_session(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "SYNCED")})
        scheduler.manual_begin(root)

        scheduler.poll()

        self.assertTrue(
            os.path.exists(os.path.join(root, scheduler.MANUAL_FILE)))
        self.assertEqual(scheduler.load_runs()["runs"], [])


class TestRun(SchedulerBase):
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

    def test_run_archives_step_result(self):
        root = self._register_pending("req")
        next_actions = ["SCORE", "IDLE"]

        def fake_run(cmd, **kwargs):
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    action = next_actions.pop(0) if next_actions else "IDLE"
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": action, "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": "MAKER_STEP0"}),
                        stderr="", returncode=0)
            if any("qodercli" in part for part in cmd):
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write("SCORE: 95/100\nCROSS_CONSISTENCY: PASS")
                return types.SimpleNamespace(stdout="", stderr="", returncode=0)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(scheduler.subprocess, "run",
                               side_effect=fake_run):
            scheduler.run_requirement("req")

        archive = os.path.join(root, ".loop", "result-SCORE.md")
        self.assertTrue(os.path.exists(archive))
        with open(archive) as f:
            self.assertEqual(f.read(), "SCORE: 95/100\nCROSS_CONSISTENCY: PASS")

    def test_run_commit_error_stops(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"],
                              commit_error="No result file")
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "commit_error")
        self.assertEqual(result["steps"], 2)
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

    def test_run_passes_previous_result_to_next_step(self):
        root = self._register_pending("req")
        next_actions = ["SCORE", "MAKER_STEP0"]
        captured = []
        result_body = ("---MAKER_OUTPUT---\nSTATUS: SUCCESS\n"
                       "FILES_CREATED:\n  - A.java\n---END_MAKER_OUTPUT---")

        def fake_run(cmd, **kwargs):
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    action = next_actions.pop(0) if next_actions else "IDLE"
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": action, "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": "MAKER_STEP0"}),
                        stderr="", returncode=0)
            if any("qodercli" in part for part in cmd):
                captured.append(json.loads(cmd[-1]))
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write(result_body)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake_run):
            scheduler.run_requirement("req")

        self.assertEqual(len(captured), 2)
        self.assertIsNone(
            captured[0].get("directives", {}).get("context", {})
            .get("previous_result"))
        self.assertEqual(
            captured[1]["directives"]["context"]["previous_result"], result_body)

    def test_run_derives_session_id_from_root_action(self):
        """qodercli calls carry a deterministic session ID: same root+action
        +attempt reuses it, so audit/session files map back to the step."""
        root = self._register_pending("req")
        captured = []

        def fake_run(cmd, **kwargs):
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE", "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": "MAKER_STEP0"}),
                        stderr="", returncode=0)
            if any("qodercli" in part for part in cmd):
                captured.append(cmd)
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write("ok")
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake_run):
            scheduler.run_requirement("req")

        self.assertEqual(len(captured), 2)
        sids = []
        for cmd in captured:
            i = cmd.index("--session-id")
            sids.append(cmd[i + 1])
        self.assertEqual(sids[0], sids[1])  # same step, same attempt → same ID
        self.assertEqual(
            sids[0],
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"{root}:SCORE:0")))

    def test_run_retry_gets_new_session_id(self):
        """A retried step (same action, retries incremented) gets a fresh ID,
        keeping the replay session clean of the failed attempt's context."""
        root = self._register_pending("req")
        captured = []
        fail = [1]

        def fake_run(cmd, **kwargs):
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE", "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": "MAKER_STEP0"}),
                        stderr="", returncode=0)
            if any("qodercli" in part for part in cmd):
                captured.append(cmd)
                if fail[0] > 0:
                    fail[0] -= 1
                    return types.SimpleNamespace(stdout="", stderr="boom",
                                                 returncode=1)
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write("ok")
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake_run):
            scheduler.run_requirement("req")

        self.assertEqual(len(captured), 2)
        sids = [cmd[cmd.index("--session-id") + 1] for cmd in captured]
        self.assertNotEqual(sids[0], sids[1])
        self.assertEqual(sids[0], str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                 f"{root}:SCORE:0")))
        self.assertEqual(sids[1], str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                 f"{root}:SCORE:1")))

    def test_run_retries_qodercli_failure_once(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"],
                              qodercli_failures=1)
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "idle")
        self.assertEqual(result["steps"], 3)
        self.assertFalse(scheduler.is_locked(root))

    def test_run_retries_commit_error_once(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"],
                              commit_failures=1)
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "idle")
        self.assertEqual(result["steps"], 3)

    def test_run_qodercli_failure_after_retry(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"],
                              qodercli_failures=2)
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "qodercli_failed")
        self.assertEqual(result["steps"], 2)

    def test_run_commit_error_after_retry(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"],
                              commit_failures=2)
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "commit_error")
        self.assertEqual(result["steps"], 2)

    def test_run_qodercli_timeout_retries_then_fails(self):
        """A hung LLM call (TimeoutExpired) is killed and treated like an
        exit-code failure: one retry, then a clean stop with the lock
        released — never a crashed run or a leaked lock."""
        root = self._register_pending("req")
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            if any("qodercli" in part for part in cmd):
                calls["n"] += 1
                raise scheduler.subprocess.TimeoutExpired(cmd, 1)
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE", "module": "c/m"}),
                        stderr="", returncode=0)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake_run):
            result = scheduler.run_requirement("req")

        self.assertEqual(calls["n"], 2)  # initial + one retry
        self.assertEqual(result["end"], "qodercli_failed")
        self.assertFalse(scheduler.is_locked(root))

    def test_run_commit_timeout_retries_then_commit_error(self):
        root = self._register_pending("req")
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            if any("qodercli" in part for part in cmd):
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write("ok")
                return types.SimpleNamespace(stdout="", stderr="",
                                             returncode=0)
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE", "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    calls["n"] += 1
                    raise scheduler.subprocess.TimeoutExpired(cmd, 1)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake_run):
            result = scheduler.run_requirement("req")

        self.assertEqual(calls["n"], 2)
        self.assertEqual(result["end"], "commit_error")
        self.assertFalse(scheduler.is_locked(root))

    def test_run_repairs_format_error_in_place(self):
        """Format errors resume the same LLM session to rewrite result.md,
        then re-commit — no step replay."""
        from parser import parse
        root = self._register_pending("req")
        next_actions = ["SCORE", "SCORE"]
        sids = []
        repaired = {"sids": [], "details": []}

        def fake_run(cmd, **kwargs):
            if any("qodercli" in part for part in cmd):
                sids.append(cmd[cmd.index("--session-id") + 1])
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write('{"cross_consistency": "PASS"}')  # missing score
                return types.SimpleNamespace(stdout="", stderr="",
                                             returncode=0)
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    action = next_actions.pop(0) if next_actions else "IDLE"
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": action, "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    with open(os.path.join(root, ".loop", "result.md")) as f:
                        text = f.read()
                    try:
                        parse(text, "SCORE")
                    except ValueError as e:
                        return types.SimpleNamespace(
                            stdout=json.dumps({"error": str(e)}),
                            stderr="", returncode=0)
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": "MAKER_STEP0"}),
                        stderr="", returncode=0)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        def fake_repair(root_, sid, detail):
            repaired["sids"].append(sid)
            repaired["details"].append(detail)
            with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                f.write('{"score": 95, "cross_consistency": "PASS"}')
            return True

        with mock.patch.object(scheduler.subprocess, "run",
                               side_effect=fake_run), \
             mock.patch.object(scheduler, "_repair_result",
                               side_effect=fake_repair):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "idle")
        # each repair resumes the session that produced the broken output
        self.assertEqual(repaired["sids"], sids)
        self.assertIn("Output format error: missing field(s): score",
                      repaired["details"][0])

    def test_run_format_repair_exhausted_falls_back_to_retry(self):
        """Repair budget exhausted (2 per attempt) → full step replay with a
        fresh session id, then commit_error when the second attempt also fails."""
        from parser import parse
        root = self._register_pending("req")
        sids = []
        repair_calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            if any("qodercli" in part for part in cmd):
                sids.append(cmd[cmd.index("--session-id") + 1])
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write('{"cross_consistency": "PASS"}')
                return types.SimpleNamespace(stdout="", stderr="",
                                             returncode=0)
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE", "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    with open(os.path.join(root, ".loop", "result.md")) as f:
                        text = f.read()
                    try:
                        parse(text, "SCORE")
                    except ValueError as e:
                        return types.SimpleNamespace(
                            stdout=json.dumps({"error": str(e)}),
                            stderr="", returncode=0)
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": "MAKER_STEP0"}),
                        stderr="", returncode=0)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        def fake_repair(root_, sid, detail):
            repair_calls["n"] += 1
            with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                f.write('{"cross_consistency": "PASS"}')  # still broken
            return True

        with mock.patch.object(scheduler.subprocess, "run",
                               side_effect=fake_run), \
             mock.patch.object(scheduler, "_repair_result",
                               side_effect=fake_repair):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "commit_error")
        self.assertEqual(len(sids), 2)  # initial + replayed attempt
        self.assertNotEqual(sids[0], sids[1])
        self.assertEqual(repair_calls["n"], 4)  # 2 per attempt

    def test_run_semantic_commit_error_skips_repair(self):
        """Errors without the 'Output format error: ' prefix are semantic —
        they go straight to the existing retry path."""
        from parser import parse
        root = self._register_pending("req")
        calls = {"repair": 0}

        def fake_run(cmd, **kwargs):
            if any("qodercli" in part for part in cmd):
                with open(os.path.join(root, ".loop", "result.md"), "w") as f:
                    f.write("not json, not legacy text")
                return types.SimpleNamespace(stdout="", stderr="",
                                             returncode=0)
            if any("__main__.py" in part for part in cmd):
                sub = cmd[cmd.index(next(p for p in cmd if "__main__.py" in p)) + 1]
                if sub == "next":
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE", "module": "c/m"}),
                        stderr="", returncode=0)
                if sub == "commit":
                    with open(os.path.join(root, ".loop", "result.md")) as f:
                        text = f.read()
                    parsed = parse(text, "SCORE")
                    if not parsed or parsed.get("score") is None:
                        # machine-level semantic error, no format prefix
                        return types.SimpleNamespace(
                            stdout=json.dumps(
                                {"error": "SCORE field missing from result"}),
                            stderr="", returncode=0)
                    return types.SimpleNamespace(
                        stdout=json.dumps({"action": "SCORE",
                                           "next_action": "MAKER_STEP0"}),
                        stderr="", returncode=0)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

        def fake_repair(root_, sid, detail):
            calls["repair"] += 1
            return True

        with mock.patch.object(scheduler.subprocess, "run",
                               side_effect=fake_run), \
             mock.patch.object(scheduler, "_repair_result",
                               side_effect=fake_repair):
            result = scheduler.run_requirement("req")

        self.assertEqual(result["end"], "commit_error")
        self.assertEqual(calls["repair"], 0)

    def test_run_records_history(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"])
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            scheduler.run_requirement("req")

        runs = scheduler.load_runs()["runs"]
        self.assertEqual(len(runs), 1)
        r = runs[0]
        self.assertEqual(r["requirement"], "req")
        self.assertEqual(r["end"], "idle")
        self.assertEqual(r["steps"], 3)
        self.assertGreaterEqual(r["duration_seconds"], 0)
        self.assertIn("started_at", r)
        self.assertIn("finished_at", r)

    def test_run_history_appends(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"])
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake):
            scheduler.run_requirement("req")
            scheduler.run_requirement("req")
        self.assertEqual(len(scheduler.load_runs()["runs"]), 2)

    def test_run_locked_requirement_fails(self):
        root = self._register_pending("req")
        with open(os.path.join(root, ".loop", "lock"), "w") as f:
            f.write(str(os.getpid()))
        result = scheduler.run_requirement("req")
        self.assertIn("error", result)


class TestDispatch(SchedulerBase):
    def _pending_entry(self, name, trigger, approved=True, status="READY"):
        return {
            "requirement": name,
            "root": os.path.join(self.tmp.name, name),
            "trigger": trigger,
            "modules": [{"key": "c/m", "status": status}],
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
            self._pending_entry("req-b", "NEEDS_REFINEMENT",
                                status="NEEDS_REFINEMENT"),
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
        self.assertEqual(cfg["max_concurrency"], 2)
        self.assertNotIn("interval_minutes", cfg)

    def test_set_max_concurrency_round_trip(self):
        scheduler.set_max_concurrency(3)
        self.assertEqual(scheduler.load_config()["max_concurrency"], 3)
        with self.assertRaises(ValueError):
            scheduler.set_max_concurrency(0)


class TestNotify(SchedulerBase):
    def test_notify_pending_advice_per_trigger(self):
        with mock.patch.object(scheduler, "notify_pending",
                               self._notify_pending_orig), \
             mock.patch.object(scheduler, "notify_text") as nt:
            scheduler.notify_pending([
                {"requirement": "req-a", "trigger": "SPEC_CHANGED",
                 "modules": [{"key": "c/m", "status": "PARTIAL"}]},
                {"requirement": "req-b", "trigger": "NEEDS_REFINEMENT",
                 "modules": [{"key": "c/m", "status": "NEEDS_REFINEMENT"}]},
            ])
        text = nt.call_args.args[0]
        self.assertIn("微信回复「批准执行 req-a」即可开始执行", text)
        self.assertIn("请回复「完善spec」进一步完善 spec", text)
        self.assertNotIn("终端执行", text)

    def test_notify_pending_gray_list_guides_adjudication(self):
        with mock.patch.object(scheduler, "notify_pending",
                               self._notify_pending_orig), \
             mock.patch.object(scheduler, "notify_text") as nt:
            scheduler.notify_pending([
                {"requirement": "req-a", "trigger": "GRAY_LIST",
                 "modules": [{"key": "c/m", "status": "READY"}]},
            ])
        text = nt.call_args.args[0]
        self.assertIn("灰名单问题待裁决", text)
        self.assertIn("「查看灰名单」", text)
        self.assertNotIn("批准执行", text)
        self.assertNotIn("即可开始执行", text)

    def test_notify_text_skips_without_config(self):
        scheduler.notify_text = self._notify_text_orig
        with mock.patch.object(scheduler, "DATA_DIR", self.tmp.name):
            self.assertFalse(scheduler.notify_text("hello"))

    def test_notify_text_skips_without_recipient(self):
        scheduler.notify_text = self._notify_text_orig
        wecom = os.path.join(self.tmp.name, "wecom.json")
        with open(wecom, "w") as f:
            json.dump({"corp_id": "c", "secret": "s", "agent_id": 1}, f)
        with mock.patch.object(scheduler, "DATA_DIR", self.tmp.name), \
             mock.patch("wecom_server.wecom_api.send_text") as mock_send:
            self.assertFalse(scheduler.notify_text("hello"))
        mock_send.assert_not_called()

    def test_notify_text_sends_to_last_user(self):
        scheduler.notify_text = self._notify_text_orig
        wecom = os.path.join(self.tmp.name, "wecom.json")
        with open(wecom, "w") as f:
            json.dump({"corp_id": "c", "secret": "s", "agent_id": 1}, f)
        with open(os.path.join(self.tmp.name, "last_user.json"), "w") as f:
            json.dump({"user": "LiChuan"}, f)
        with mock.patch.object(scheduler, "DATA_DIR", self.tmp.name), \
             mock.patch("wecom_server.wecom_api.send_text",
                        return_value=True) as mock_send:
            self.assertTrue(scheduler.notify_text("hello"))
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], "LiChuan")
        self.assertEqual(mock_send.call_args[0][1], "hello")

    def test_notify_text_explicit_user_overrides_last_user(self):
        scheduler.notify_text = self._notify_text_orig
        wecom = os.path.join(self.tmp.name, "wecom.json")
        with open(wecom, "w") as f:
            json.dump({"corp_id": "c", "secret": "s", "agent_id": 1}, f)
        with open(os.path.join(self.tmp.name, "last_user.json"), "w") as f:
            json.dump({"user": "SomeoneElse"}, f)
        with mock.patch.object(scheduler, "DATA_DIR", self.tmp.name), \
             mock.patch("wecom_server.wecom_api.send_text",
                        return_value=True) as mock_send:
            self.assertTrue(scheduler.notify_text("hello", user_id="LiChuan"))
        self.assertEqual(mock_send.call_args[0][0], "LiChuan")

    def test_run_sends_start_and_finish(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE"])
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake), \
             mock.patch.object(scheduler, "notify_text") as mock_notify:
            scheduler.run_requirement("req")
        messages = [c.args[0] for c in mock_notify.call_args_list]
        self.assertTrue(any(m.startswith("[调度] 开始执行 req") for m in messages))
        self.assertTrue(any("执行完成（成功）" in m for m in messages))

    def test_end_message_gray_list_guides_adjudication(self):
        msg = scheduler._end_message(
            "req", "gray_list", 5, 11, None,
            os.path.join(self.tmp.name, "req"))
        self.assertIn("执行暂停", msg)
        self.assertIn("灰名单", msg)
        self.assertIn("查看灰名单", msg)
        self.assertNotIn("批准执行 req", msg)
        self.assertNotIn("gray_list", msg)

    def test_end_message_no_advance_reports_reason(self):
        root = self.register("req", os.path.join(self.tmp.name, "req"))
        _make_state(root, {"c/m": _module("c", "m", "NEEDS_REFINEMENT")})
        msg = scheduler._end_message(
            "req", "no_advance", 2, 5, None, root)
        self.assertIn("SCORE 评分不足", msg)
        self.assertIn("完善 spec", msg)
        self.assertNotIn("no_advance", msg)

    def test_run_notifies_approved_user(self):
        root = self._register_pending("req")
        pend = scheduler.load_pending()
        pend["pending"][0]["approved_by"] = "LiChuan"
        scheduler._save_pending(pend)
        fake = self._fake_run(next_actions=["SCORE"])
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake), \
             mock.patch.object(scheduler, "notify_text") as mock_notify:
            scheduler.run_requirement("req")
        users = {c.args[1] for c in mock_notify.call_args_list
                 if len(c.args) > 1}
        self.assertIn("LiChuan", users)

    def test_run_sends_heartbeat_for_long_run(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"])
        t0 = 1000.0
        times = [t0, t0, t0 + 301, t0 + 301, t0 + 301, t0 + 301, t0 + 302]
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake), \
             mock.patch.object(scheduler, "notify_text") as mock_notify, \
             mock.patch.object(scheduler.time, "time", side_effect=times):
            scheduler.run_requirement("req")
        messages = [c.args[0] for c in mock_notify.call_args_list]
        self.assertTrue(any("仍在执行" in m and "5 分钟" in m for m in messages))

    def test_run_heartbeat_backs_off(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "PLAN"] * 4)
        t0 = 1000.0
        # 5min heartbeat at iter1, 15min heartbeat at iter3; after that
        # 30min ceiling means iter5 (t0+1501) must NOT fire a third beat.
        # Each beat consumes 3 time calls (check, elapsed, last_beat).
        times = [t0, t0 + 301, t0 + 301, t0 + 301, t0 + 302,
                 t0 + 1201, t0 + 1201, t0 + 1201, t0 + 1301,
                 t0 + 1501, t0 + 1800, t0 + 2100,
                 t0 + 2400, t0 + 2700, t0 + 2701]
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake), \
             mock.patch.object(scheduler, "notify_text") as mock_notify, \
             mock.patch.object(scheduler.time, "time", side_effect=times):
            scheduler.run_requirement("req")
        messages = [c.args[0] for c in mock_notify.call_args_list]
        beats = [m for m in messages if "仍在执行" in m]
        self.assertEqual(len(beats), 2)

    def test_heartbeat_includes_position(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "PLAN"] * 4)
        t0 = 1000.0
        times = [t0, t0 + 301, t0 + 301, t0 + 301, t0 + 302,
                 t0 + 1201, t0 + 1201, t0 + 1201, t0 + 1301,
                 t0 + 1501, t0 + 1800, t0 + 2100,
                 t0 + 2400, t0 + 2700, t0 + 2701]
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake), \
             mock.patch.object(scheduler, "notify_text") as mock_notify, \
             mock.patch.object(scheduler.time, "time", side_effect=times):
            scheduler.run_requirement("req")
        messages = [c.args[0] for c in mock_notify.call_args_list]
        beats = [m for m in messages if "仍在执行" in m]
        # second beat (after iter2 finished) shows the last activity
        self.assertIn("PLAN c/m", beats[1])

    def test_failure_notify_includes_detail(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"],
                              commit_failures=2)
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake), \
             mock.patch.object(scheduler, "notify_text") as mock_notify:
            scheduler.run_requirement("req")
        messages = [c.args[0] for c in mock_notify.call_args_list]
        done = [m for m in messages if "执行失败" in m]
        self.assertEqual(len(done), 1)
        self.assertIn("transient", done[0])  # commit error detail
        self.assertIn("重试", done[0])

    def test_idle_notify_keeps_success_wording(self):
        root = self._register_pending("req")
        fake = self._fake_run(next_actions=["SCORE", "SCORE"])
        with mock.patch.object(scheduler.subprocess, "run", side_effect=fake), \
             mock.patch.object(scheduler, "notify_text") as mock_notify:
            scheduler.run_requirement("req")
        messages = [c.args[0] for c in mock_notify.call_args_list]
        done = [m for m in messages if "执行完成（成功）" in m]
        self.assertEqual(len(done), 1)
        self.assertIn("SYNCED", done[0])


if __name__ == "__main__":
    unittest.main()
