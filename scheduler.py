"""Scheduler (Layer 2): poll requirements, track pending work, dispatch runs.

Pure Python, no LLM. Communicates with the loop engine core only through
files (.loop/state.json, spec.md) and CLI subprocesses — never imports core
modules, per the orchestration design spec.
"""

import datetime
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import requests  # noqa: E402 — used by notify_pending()

from constants import MAX_MAKER_ATTEMPTS, STATUS_TABLE
from spec_utils import (compute_spec_hash, compute_spec_norm_hash,
                        discover_modules)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser("~/.qoder/loop_engine")
REGISTRY_PATH = os.path.join(DATA_DIR, "requirements.json")
PENDING_PATH = os.path.join(DATA_DIR, "pending.json")
CONFIG_PATH = os.path.join(DATA_DIR, "schedule.json")
LOG_PATH = os.path.join(DATA_DIR, "schedule.log")

STATE_FILE = ".loop/state.json"
LOCK_FILE = ".loop/lock"
MANUAL_FILE = ".loop/manual.json"
RUNS_PATH = os.path.join(DATA_DIR, "runs.json")

SYNCED = "SYNCED"
PARTIAL = "PARTIAL"
READY = "READY"
NEEDS_REFINEMENT = "NEEDS_REFINEMENT"
BLOCKED = "BLOCKED"
DRAFT = "DRAFT"

SPEC_CHANGED = "SPEC_CHANGED"
READY_PENDING = "READY_PENDING"
GRAY_LIST = "GRAY_LIST"

_TRIGGER_FOR_STATUS = {s: v["trigger"] for s, v in STATUS_TABLE.items()
                       if v["trigger"]}
_TRIGGER_PRIORITY = tuple(s for s in STATUS_TABLE
                          if STATUS_TABLE[s]["trigger"])

_TRIGGER_LABELS = {v["trigger"]: v["label"]
                   for v in STATUS_TABLE.values() if v["trigger"]}
_TRIGGER_LABELS[GRAY_LIST] = "灰名单"  # non-status exception: draft adjudication

DEFAULT_CONFIG = {"max_concurrency": 2, "last_run": None}

LOOP_AGENT_PROMPT = (
    "You are a loop engine agent. You will receive directives JSON. "
    "Read the spec/plan files it references, follow the instructions, "
    "and write your output to .loop/result.md in the specified output format. "
    "The directives may carry 'context.previous_result' — the previous step's "
    "output; use it as context for continuity."
)

MAX_SAME_ACTION = 3
MAX_TOTAL_STEPS = 200
HEARTBEAT_SECONDS = 300
HEARTBEAT_MAX_SECONDS = 1800  # backoff ceiling: 5min -> 15min -> 30min
MAX_FAILURE_RETRIES = 1  # transient qodercli/commit failures get one retry
MAX_RUNS = 100  # runs.json history cap — oldest entries trimmed on write
MAX_FORMAT_REPAIRS = 2  # format errors resume the same LLM session to rewrite result.md
# Per-step caps are hung-call backstops, not task budgets.
STEP_TIMEOUT_SECONDS = 6 * 3600  # one qodercli/LLM step
QUICK_TIMEOUT_SECONDS = 30       # local next/commit CLI calls (pure Python, <1s)

_QODERCLI = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")


# ---------- file helpers ----------

def _atomic_write_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def _log(message):
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.datetime.now().isoformat()} {message}\n")


def _engine_cmd(*args):
    return [sys.executable, os.path.join(BASE_DIR, "__main__.py")] + list(args)


# ---------- registry / pending ----------

def _read_registry():
    if not os.path.exists(REGISTRY_PATH):
        return []
    with open(REGISTRY_PATH) as f:
        data = json.load(f)
    return data.get("requirements", [])


def load_pending():
    if not os.path.exists(PENDING_PATH):
        return {"pending": []}
    with open(PENDING_PATH) as f:
        return json.load(f)


def _save_pending(data):
    _atomic_write_json(PENDING_PATH, data)


def _find_entry(data, name):
    for e in data.get("pending", []):
        if e.get("requirement") == name:
            return e
    return None


# ---------- detection ----------

def _poll_requirement(root, name):
    """Detect pending work for one requirement. Never writes state.json."""
    state_path = os.path.join(root, STATE_FILE)
    if not os.path.exists(state_path):
        return None
    with open(state_path) as f:
        state = json.load(f)
    if state.get("current", {}).get("action") and is_locked(root):
        return None  # mid-progress — lock held, being executed, skip
    modules = state.get("modules", {})
    detected = []
    for key, module in modules.items():
        status = module.get("status")
        hash_changed = False
        if status == SYNCED:
            spec_path = os.path.join(
                root, "openspec", "changes", module.get("change_id", ""),
                "specs", module.get("module_name", ""), "spec.md")
            current_hash = compute_spec_hash(spec_path)
            if not current_hash or current_hash == module.get("spec_hash"):
                continue
            norm_hash = compute_spec_norm_hash(spec_path)
            old_norm = module.get("spec_norm_hash")
            if old_norm is not None and norm_hash and \
                    norm_hash == old_norm:
                continue  # comment/format-only edit, machine skips the loop
            hash_changed = True
            status = PARTIAL
        if status not in _TRIGGER_FOR_STATUS:
            continue
        detected.append({
            "key": key,
            "status": status,
            "spec_hash_changed": hash_changed,
            "cross_project": bool(module.get("project_root"))
            and module.get("project_root") != ".",
        })
    for change_id, module_name, _spec_path in discover_modules(root):
        key = f"{change_id}/{module_name}"
        if key not in modules:
            detected.append({
                "key": key, "status": DRAFT,
                "spec_hash_changed": False, "cross_project": False,
            })
    if not detected:
        return None
    drafts = state.get("gray_drafts", [])
    if any(d.get("status") == "pending" for d in drafts):
        # gray-list drafts await human adjudication — this overrides the
        # READY_PENDING trigger so the notification guides adjudication
        trigger = GRAY_LIST
    else:
        trigger = next(_TRIGGER_FOR_STATUS[s] for s in _TRIGGER_PRIORITY
                       if any(m["status"] == s for m in detected))
    return {
        "requirement": name,
        "root": root,
        "trigger": trigger,
        "modules": detected,
        "detected_at": datetime.datetime.now().isoformat(),
        "approved": False,
    }


def poll():
    """Run one detection cycle and merge into pending.json."""
    for req in _read_registry():
        _cleanup_stale_manual(req.get("root", ""))
        # Clear stuck current field when the manual session died but left
        # state.json mid-progress — the flock is already released by the OS
        # when the process exited, so another run can't conflict.
        root = req.get("root", "")
        state_path = os.path.join(root, STATE_FILE)
        if os.path.exists(state_path) and not is_locked(root):
            try:
                with open(state_path) as f:
                    state = json.load(f)
            except (OSError, ValueError):
                continue
            if state.get("current", {}).get("action"):
                state["current"] = {}
                with open(state_path, "w") as f:
                    json.dump(state, f, indent=2)
                _log(f"poll: cleared stuck current in {req.get('name')} "
                     f"(manual session ended)")
    fresh = []
    for req in _read_registry():
        entry = _poll_requirement(req.get("root"), req.get("name"))
        if entry:
            fresh.append(entry)
    prev = load_pending()
    fresh_names = {e["requirement"] for e in fresh}
    # Carry over approved entries whose requirement is mid-execution:
    # _poll_requirement returns None while the lock is held, but the approved
    # entry must survive the run so a gray_list pause stays resumable — the
    # run's finally block decides whether to clear it afterwards.
    carried = [e for e in prev.get("pending", [])
               if e.get("approved")
               and is_locked(e.get("root", ""))
               and e["requirement"] not in fresh_names]
    newly_detected = []
    for entry in fresh:
        old = _find_entry(prev, entry["requirement"])
        if old and old.get("approved"):
            entry["approved"] = True
            entry["detected_at"] = old.get("detected_at", entry["detected_at"])
        elif not old:
            newly_detected.append(entry)
        elif old.get("trigger") != entry.get("trigger"):
            # trigger changed (e.g., GRAY_LIST → SPEC_CHANGED) — re-notify
            newly_detected.append(entry)
    # Auto-archive stale gray drafts when a module goes through a new spec
    # change cycle: the old findings are from a previous spec version and
    # the new loop will regenerate them if still relevant.
    for entry in fresh:
        if not any(m.get("spec_hash_changed") for m in entry.get("modules", [])):
            continue
        root = entry.get("root", "")
        state_path = os.path.join(root, STATE_FILE)
        if not os.path.exists(state_path):
            continue
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        changed_keys = {m["key"] for m in entry["modules"] if m.get("spec_hash_changed")}
        archived = False
        for d in state.get("gray_drafts", []):
            if d.get("status") == "pending" and d.get("module") in changed_keys:
                d["status"] = "rejected"
                d["_archived"] = True
                archived = True
        if archived:
            _log(f"poll: archived stale gray drafts for spec change in {entry['requirement']}")
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2)
    _save_pending({"pending": fresh + carried})
    if newly_detected:
        notify_pending(newly_detected)
    return fresh


def _no_pending_message(name):
    """Friendly reason for a requirement that has no pending work."""
    req = next((r for r in _read_registry() if r.get("name") == name), None)
    if not req:
        return f"没有找到需求：{name}"
    try:
        with open(os.path.join(req["root"], STATE_FILE)) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return f"{name} 当前没有待执行工作（等待下次 poll 检测）"
    if is_locked(req["root"]):
        return f"{name} 正在执行中，无需重复批准"
    if all(m.get("status") == SYNCED
           for m in state.get("modules", {}).values()):
        return f"{name} 已执行完成（SYNCED），无需批准"
    return f"{name} 当前没有待执行工作（等待下次 poll 检测）"


def _has_pending_gray_drafts(root):
    """True if the project at *root* has any uncleared gray-list draft."""
    state_path = os.path.join(root, ".loop", "state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return False
    return any(
        d.get("status") == "pending"
        for d in state.get("gray_drafts", [])
    )


def _entry_auto_exec(entry):
    """True when any detected module status is auto-executable."""
    return any(STATUS_TABLE.get(m.get("status"), {}).get("auto_exec")
               for m in entry.get("modules", []))


def approve(name=None, all_=False, approved_by=None):
    data = load_pending()
    entries = data.get("pending", [])
    if all_:
        count = 0
        for e in entries:
            if (_entry_auto_exec(e)
                    and not e.get("approved")
                    and not _has_pending_gray_drafts(e.get("root", ""))):
                e["approved"] = True
                if approved_by:
                    e["approved_by"] = approved_by
                count += 1
        _save_pending(data)
        return count
    if not name:
        raise ValueError("Specify a requirement name or --all")
    entry = _find_entry(data, name)
    if not entry:
        raise ValueError(_no_pending_message(name))
    if not _entry_auto_exec(entry):
        statuses = [m.get("status") for m in entry.get("modules", [])]
        labels = [STATUS_TABLE.get(s, {}).get("label", s) for s in statuses]
        tlabel = labels[0] if labels else entry.get("trigger", "UNKNOWN")
        raise ValueError(
            f"{name}（{tlabel}）为报告状态，需先完成 spec 相关工作，不支持自动执行")
    if entry.get("approved"):
        return 0
    if _has_pending_gray_drafts(entry.get("root", "")):
        raise ValueError(
            f"{name} 有灰名单草稿待裁决，请先处理后再批准执行")
    entry["approved"] = True
    if approved_by:
        entry["approved_by"] = approved_by
    _save_pending(data)
    return 1


def _clear_approval(name):
    data = load_pending()
    before = len(data.get("pending", []))
    data["pending"] = [e for e in data.get("pending", [])
                       if e.get("requirement") != name]
    if len(data["pending"]) < before:
        _save_pending(data)


def _last_user():
    """Most recently active WeCom user (written by the wecom server)."""
    try:
        with open(os.path.join(DATA_DIR, "last_user.json")) as f:
            return json.load(f).get("user") or None
    except (OSError, ValueError):
        return None


def notify_text(message, user_id=None):
    """Push a plain text notification to the WeCom self-built app chat.

    Recipient is the user who approved the run (or the most recently active
    WeCom user). Returns True if sent. Silently skips when WeCom is not
    configured or no recipient is known.
    """
    wecom_config_path = os.path.join(DATA_DIR, "wecom.json")
    if not os.path.exists(wecom_config_path):
        return False
    with open(wecom_config_path) as f:
        config = json.load(f)
    if not user_id:
        user_id = _last_user()
    if not user_id:
        _log("notify: no recipient user, skipped")
        return False
    if not config.get("corp_id") or not config.get("secret") or not config.get("agent_id"):
        return False
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from wecom_server.wecom_api import send_text
        sent = send_text(user_id, message, config)
        _log(f"notify: sent to {user_id}" if sent
             else f"notify: send failed for {user_id}")
        return sent
    except Exception as e:
        _log(f"notify: send failed: {e}")
        return False


def _pending_gray_evidence(root):
    """Load pending gray-list drafts from state.json, return formatted lines."""
    state_path = os.path.join(root, ".loop", "state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return []
    lines = []
    for d in state.get("gray_drafts", []):
        if d.get("status") == "pending":
            lines.append(
                f"  #{d['id']} [{d.get('type_label', '?')}] "
                f"{d.get('summary', '')}")
    return lines


def notify_pending(fresh_entries):
    """Push WeCom notification for newly detected pending items."""
    if not fresh_entries:
        return
    lines = ["[调度] 检测到待处理项："]
    advice = []
    for entry in fresh_entries:
        trigger = entry.get("trigger", "UNKNOWN")
        modules = entry.get("modules", [])
        names = ", ".join(m.get("key", "?") for m in modules)
        label = _TRIGGER_LABELS.get(trigger, trigger)
        lines.append(f"• {entry['requirement']}（{label}）：{names}")
        if _entry_auto_exec(entry):
            if trigger == GRAY_LIST:
                evidence = _pending_gray_evidence(entry.get("root", ""))
                if evidence:
                    advice.append(f"「{entry['requirement']}」灰名单待裁决：")
                    advice.extend(evidence)
                    advice.append("回复「接受/拒绝 <编号>」裁决，如「全部接受」")
                else:
                    advice.append(
                        f"「{entry['requirement']}」有灰名单问题待裁决，"
                        f"请回复「查看灰名单」了解详情")
            else:
                advice.append(
                    f"微信回复「批准执行 {entry['requirement']}」即可开始执行")
        elif trigger == NEEDS_REFINEMENT:
            advice.append("请回复「完善spec」进一步完善 spec")
        elif trigger == BLOCKED:
            advice.append("请处理阻塞问题后回复「完善spec」")
        elif trigger == DRAFT:
            advice.append("请回复「完善spec」完成新模块 spec")
    lines.extend(advice)
    notify_text("\n".join(lines))


# ---------- schedule config ----------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        data = json.load(f)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(data or {})
    return cfg


def save_config(cfg):
    _atomic_write_json(CONFIG_PATH, cfg)


def load_runs():
    """Read execution history (runs.json). Returns {"runs": [...]}."""
    if not os.path.exists(RUNS_PATH):
        return {"runs": []}
    with open(RUNS_PATH) as f:
        return json.load(f)


def _record_run(name, end, steps, started_at, finished_at):
    data = load_runs()
    data.setdefault("runs", []).append({
        "requirement": name,
        "end": end,
        "steps": steps,
        "started_at": datetime.datetime.fromtimestamp(started_at).isoformat(),
        "finished_at": datetime.datetime.fromtimestamp(finished_at).isoformat(),
        "duration_seconds": int(finished_at - started_at),
    })
    if len(data["runs"]) > MAX_RUNS:
        data["runs"] = data["runs"][-MAX_RUNS:]
    _atomic_write_json(RUNS_PATH, data)


def set_max_concurrency(n):
    if n < 1:
        raise ValueError("max concurrency must be >= 1")
    cfg = load_config()
    cfg["max_concurrency"] = int(n)
    save_config(cfg)
    return cfg


# ---------- per-requirement lock ----------

def _lock_path(root):
    return os.path.join(root, LOCK_FILE)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


_held_locks = {}
_held_locks_guard = threading.Lock()


def acquire_lock(root):
    """Returns True if the exclusive flock on .loop/lock is acquired.

    The lock file is persistent and never unlinked: unlinking lets two
    processes lock different inodes and both win. flock is released by
    the kernel on process death, so dead-owner reclaim is automatic.
    """
    path = _lock_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    with _held_locks_guard:
        _held_locks[path] = fd
    try:
        os.lseek(fd, 0, 0)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return True


def release_lock(root):
    path = _lock_path(root)
    with _held_locks_guard:
        fd = _held_locks.pop(path, None)
    if fd is None:
        return
    try:
        os.lseek(fd, 0, 0)
        os.ftruncate(fd, 0)
        os.write(fd, b"0")
    except OSError:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


def is_locked(root):
    """True if the exclusive flock on .loop/lock is held by anyone.

    Probes the kernel lock directly — pid content and mtime are advisory
    only and can lie (recycled pid, stale age); the flock never can.
    """
    path = _lock_path(root)
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return False


def manual_begin(root):
    """Acquire the requirement lock for a manual (G-driven) loop.

    The flock is held by a detached holder process (`manual-hold`), so it
    survives the manual-begin CLI exiting; manual_end terminates it. This
    makes the manual lock real across processes — next/commit now refuse
    to run without a held lock.
    """
    holder = subprocess.Popen(
        _engine_cmd("manual-hold", "--root", root),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    for _ in range(200):  # up to ~10s for the holder to acquire the lock
        if holder.poll() is not None:
            return False
        try:
            with open(_lock_path(root)) as f:
                if f.read().strip() == str(holder.pid):
                    break
        except OSError:
            pass
        time.sleep(0.05)
    else:
        holder.terminate()
        return False
    try:
        with open(os.path.join(root, MANUAL_FILE), "w") as f:
            json.dump({"pid": holder.pid, "started_at": time.time(),
                       "steps": 0}, f)
    except OSError:
        holder.terminate()
        return False
    return True


def manual_hold(root):
    """Detached lock holder: acquire the flock and keep it until the
    session file disappears (manual_end) or we are terminated.

    Grace period covers the window before manual_begin writes the session
    file; a crashed manual_begin leaves no lock behind.
    """
    if not acquire_lock(root):
        return False
    session_path = os.path.join(root, MANUAL_FILE)
    deadline = time.time() + 10
    idle_timeout = 3600  # ponytail: a crashed G leaves the flock forever
                         # (its holder pid stays alive, so poll cleanup
                         # can't tell); expire after an hour without commits
    try:
        while True:
            if time.time() >= deadline:
                try:
                    st = os.stat(session_path)
                except OSError:
                    break
                if time.time() - st.st_mtime > idle_timeout:
                    break
            time.sleep(5)
    finally:
        release_lock(root)
    return True


def manual_step(root):
    """Count one committed step of an active manual session (no-op otherwise)."""
    session_path = os.path.join(root, MANUAL_FILE)
    if not os.path.exists(session_path):
        return
    try:
        with open(session_path) as f:
            session = json.load(f)
        session["steps"] = int(session.get("steps", 0)) + 1
        with open(session_path, "w") as f:
            json.dump(session, f)
    except (ValueError, OSError):
        pass


def manual_end(root):
    """Release a manual-session lock and record the run in runs.json.

    Terminates the detached holder process; the kernel releases the flock
    on its death. Returns False (without touching the lock) when the lock
    was replaced by another process — e.g. a scheduler run after the
    manual session died.
    """
    session_path = os.path.join(root, MANUAL_FILE)
    if not os.path.exists(session_path):
        return False
    try:
        with open(session_path) as f:
            session = json.load(f)
        with open(_lock_path(root)) as f:
            lock_pid = int(f.read().strip() or "0")
    except (ValueError, OSError):
        return False
    if lock_pid != session.get("pid"):
        return False
    pid = int(session.get("pid", 0) or 0)
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # holder already dead; kernel released the flock
    for _ in range(50):  # wait for the flock to actually clear
        if not is_locked(root):
            break
        time.sleep(0.1)
    try:
        os.unlink(session_path)
    except OSError:
        return False
    _record_manual_run(root, session)
    return True


def _record_manual_run(root, session):
    started_at = float(session.get("started_at", time.time()))
    req = next((r for r in _read_registry() if r.get("root") == root), None)
    name = req.get("name") if req else os.path.basename(root.rstrip("/")) or root
    # loop completed when the machine has no module mid-progress
    end = "idle"
    try:
        with open(os.path.join(root, STATE_FILE)) as f:
            state = json.load(f)
        if state.get("current", {}).get("module"):
            end = "manual_stop"
    except (OSError, ValueError):
        pass
    _record_run(name, end, int(session.get("steps", 0)),
                started_at, time.time())


def _cleanup_stale_manual(root):
    """Auto-end manual sessions whose owner process died."""
    session_path = os.path.join(root, MANUAL_FILE)
    if not os.path.exists(session_path):
        return
    try:
        with open(session_path) as f:
            session = json.load(f)
    except (ValueError, OSError):
        return
    try:
        pid = int(session.get("pid", 0) or 0)
    except ValueError:
        return
    if _pid_alive(pid):
        return
    if manual_end(root):
        return
    # lock was replaced by a scheduler run — session file is stale
    try:
        os.unlink(session_path)
    except OSError:
        pass


_SCORE_DIM_LABELS = {
    "scenario_coverage": "场景覆盖",
    "field_completeness": "字段定义",
    "api_contract": "API 契约",
    "exception_coverage": "异常场景",
    "ambiguity_markers": "未决标记",
}


def _score_gap_text(module):
    """SCORE 评分不足的明细：分数、一致性门禁、各维度具体缺口。"""
    parts = []
    score = module.get("last_score")
    if score is not None:
        parts.append(f"{score}/100")
    if module.get("score_cross") == "FAIL":
        parts.append("跨模块一致性 FAIL")
    dims = module.get("score_dimensions") or {}
    gaps = []
    for k, v in dims.items():
        if not isinstance(v, str) or not v.strip():
            continue
        if v.strip().lower() in ("ok", "strong", "pass", "good", "fine", "none"):
            continue
        label = _SCORE_DIM_LABELS.get(k, k)
        gaps.append(f"{label}: {v.strip()}")
    if gaps:
        parts.append("；".join(gaps))
    return f"SCORE 评分不足（{', '.join(parts)}）" if parts else "SCORE 评分不足（<90）"


def _no_advance_reason(root):
    """Human-readable reason for a no_advance stop."""
    try:
        with open(os.path.join(root, STATE_FILE)) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return "状态机未推进"
    statuses = [m.get("status") for m in state.get("modules", {}).values()]
    for key, m in state.get("modules", {}).items():
        if m.get("hard_errors") and m.get("maker_attempt", 0) >= MAX_MAKER_ATTEMPTS:
            return (f"Checker 硬性偏差未解决且 MAKER_FIX 已用尽 "
                    f"（{MAX_MAKER_ATTEMPTS}/{MAX_MAKER_ATTEMPTS}），"
                    f"需人工处理：修改代码对齐 spec，或调整 spec 后重新登记")
    if NEEDS_REFINEMENT in statuses:
        refined = [(k, m) for k, m in state.get("modules", {}).items()
                   if m.get("status") == NEEDS_REFINEMENT]
        detail = "；".join(f"{k}: {_score_gap_text(m)}" for k, m in refined)
        return f"需要完善 spec：{detail}"
    if BLOCKED in statuses:
        return "存在阻塞模块，需要人工处理"
    if DRAFT in statuses:
        return "存在未完成 spec 的新模块"
    if all(s == SYNCED for s in statuses):
        return "所有模块已同步"
    return "状态机未推进（无下一步可执行）"


def _end_message(name, end, steps, elapsed_min, failure_detail, root):
    """User-facing run-end notification: outcome + what to do next."""
    base = f"[调度] {name} "
    if end == "idle":
        return (f"{base}执行完成（成功）：所有模块已同步 SYNCED，"
                f"共 {steps} 步，耗时 {elapsed_min} 分钟")
    if end == "gray_list":
        return (f"{base}执行暂停：Checker 发现待人工裁决的问题（灰名单）。"
                f"微信回复「查看灰名单」了解详情")
    if end == "no_advance":
        reason = _no_advance_reason(root)
        next_hint = ("处理完成后回复「批准执行 {name}」继续"
                     if reason != "所有模块已同步"
                     else "无需进一步操作")
        return (f"{base}执行暂停：{reason}，共 {steps} 步，耗时 {elapsed_min} 分钟。"
                f"{next_hint}")
    if end in ("qodercli_failed", "commit_error",
               "bad_next_output", "bad_commit_output"):
        detail = failure_detail or end
        return (f"{base}执行失败：{detail}，共 {steps} 步，"
                f"耗时 {elapsed_min} 分钟。修复后回复「批准执行 {name}」重试")
    if end == "repeat_limit":
        return (f"{base}执行停止：同一步骤重复多次未推进，需人工检查，"
                f"共 {steps} 步，耗时 {elapsed_min} 分钟")
    if end == "max_steps":
        return (f"{base}执行停止：达到最大步数上限，需人工检查，"
                f"共 {steps} 步，耗时 {elapsed_min} 分钟")
    return f"{base}执行结束（{end}），共 {steps} 步，耗时 {elapsed_min} 分钟"


def running_count(registry_entries=None):
    if registry_entries is None:
        registry_entries = _read_registry()
    return sum(1 for r in registry_entries if is_locked(r.get("root", "")))


# ---------- execution ----------

_REPAIR_PROMPT = (
    "Your previous output could not be parsed. Rewrite .loop/result.md with "
    "ONLY the required JSON object — no markdown, no code fences, no "
    "commentary. Do NOT re-run tests, do NOT recompile, do NOT modify any "
    "other files. Parse error: {detail}"
)


def _repair_result(root, sid, detail):
    """Resume the same LLM session to rewrite result.md in valid format.

    No tests/compilation rerun: the step's work is already done, only the
    output envelope was malformed. Returns False when the repair call fails.
    """
    try:
        q = subprocess.run(
            [_QODERCLI, "--print", "--session-id", sid,
             "--no-session-persistence",
             "--dangerously-skip-permissions",
             "--cwd", root, "--append-system-prompt",
             _REPAIR_PROMPT.format(detail=detail),
             "Rewrite .loop/result.md with the required JSON object"],
            capture_output=True, text=True, timeout=STEP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _log("repair: qodercli timed out")
        return False
    if q.returncode != 0:
        _log(f"repair: qodercli exited {q.returncode}: "
             f"{(q.stderr or '').strip()}")
        return False
    return True


def run_requirement(name):
    """Execute one requirement to completion: next → qodercli → commit."""
    req = next((r for r in _read_registry() if r.get("name") == name), None)
    if not req:
        return {"error": f"Requirement not registered: {name}"}
    root = req["root"]
    if not acquire_lock(root):
        return {"error": f"Requirement already running (lock held): {root}"}
    user_id = None
    try:
        entry = _find_entry(load_pending(), name)
        user_id = entry.get("approved_by") if entry else None
    except (OSError, ValueError):
        pass
    start = time.time()
    last_beat = start
    beat_interval = HEARTBEAT_SECONDS
    notify_text(f"[调度] 开始执行 {name} ({root})", user_id)
    _log(f"run {name}: start ({root})")
    try:
        steps = 0
        same_action = 0
        last_action = None
        retries = 0
        active_action = None
        active_module = None
        failure_detail = None
        prev_result = None  # previous step's result.md content, fed to next step
        end = "max_steps"
        while steps < MAX_TOTAL_STEPS:
            # heartbeat: ping WeCom with backoff (5min -> 15min -> 30min)
            # so long runs stay visibly alive without spamming
            if time.time() - last_beat >= beat_interval:
                elapsed_min = int((time.time() - start) // 60)
                pos = f"（{active_action} {active_module}）" if active_action else ""
                notify_text(f"[调度] {name} 仍在执行{pos}，已 {elapsed_min} 分钟",
                            user_id)
                last_beat = time.time()
                beat_interval = min(beat_interval * 3, HEARTBEAT_MAX_SECONDS)
            steps += 1
            try:
                r = subprocess.run(_engine_cmd("next", "--root", root),
                                   capture_output=True, text=True,
                                   timeout=QUICK_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                _log(f"run {name}: next timed out")
                end = "qodercli_failed"
                failure_detail = "next 命令超时"
                break
            try:
                directives = json.loads(r.stdout or "{}")
            except json.JSONDecodeError:
                _log(f"run {name}: invalid next output")
                end = "bad_next_output"
                break
            action = directives.get("action")
            active_action = action
            active_module = directives.get("module")
            if action == "IDLE":
                end = "idle"
                break
            if action == last_action:
                same_action += 1
                if same_action >= MAX_SAME_ACTION:
                    _log(f"run {name}: action {action} repeated "
                         f"{same_action} times, stopping")
                    end = "repeat_limit"
                    break
            else:
                same_action = 1
                last_action = action
            _log(f"run {name}: step {steps} action={action} "
                 f"module={directives.get('module', '-')}")
            payload = directives
            if prev_result is not None:
                payload = dict(directives)
                inner = dict(payload.get("directives", {}))
                inner = {**inner, "context": {
                    **inner.get("context", {}),
                    "previous_result": prev_result}}
                payload["directives"] = inner
            # deterministic session id: same root+action+attempt maps to the
            # same qodercli session (audit/trace back to the step); a retry
            # increments retries and gets a fresh id, keeping replay clean
            sid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                 f"{root}:{action}:{retries}"))
            try:
                q = subprocess.run(
                    [_QODERCLI, "--print", "--session-id", sid,
                     "--no-session-persistence",
                     "--dangerously-skip-permissions",
                     "--cwd", root, "--append-system-prompt", LOOP_AGENT_PROMPT,
                     json.dumps(payload)],
                    capture_output=True, text=True,
                    timeout=STEP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # subprocess.run already killed the hung child; the step
                # replays idempotently through the same retry path
                _log(f"run {name}: qodercli timed out after "
                     f"{STEP_TIMEOUT_SECONDS // 3600}h")
                failure_detail = f"步骤超时（>{STEP_TIMEOUT_SECONDS // 3600} 小时）"
                if retries < MAX_FAILURE_RETRIES:
                    retries += 1
                    _log(f"run {name}: retrying step (retry {retries})")
                    continue
                end = "qodercli_failed"
                break
            if q.returncode != 0:
                _log(f"run {name}: qodercli exited {q.returncode}")
                failure_detail = (q.stderr or "").strip() or f"exit {q.returncode}"
                if retries < MAX_FAILURE_RETRIES:
                    retries += 1
                    _log(f"run {name}: retrying step (retry {retries})")
                    continue  # state unchanged — same step replays idempotently
                end = "qodercli_failed"
                break
            # capture result.md before commit consumes and clears it
            result_text = ""
            result_path = os.path.join(root, ".loop", "result.md")
            if os.path.exists(result_path):
                with open(result_path) as f:
                    result_text = f.read()
                archive_path = os.path.join(root, ".loop",
                                            f"result-{action}.md")
                try:
                    with open(archive_path, "w") as f:
                        f.write(result_text)
                except OSError:
                    pass
            # format errors are repaired in place (resume the same LLM
            # session to rewrite result.md) before falling back to a full
            # step replay; semantic errors skip straight to the retry path
            format_repairs = 0
            bad_commit_output = False
            while True:
                try:
                    c = subprocess.run(_engine_cmd("commit", "--root", root),
                                       capture_output=True, text=True,
                                       timeout=QUICK_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    _log(f"run {name}: commit timed out")
                    commit = {"error": "commit 超时"}
                    break
                try:
                    commit = json.loads(c.stdout or "{}")
                except json.JSONDecodeError:
                    _log(f"run {name}: invalid commit output")
                    end = "bad_commit_output"
                    bad_commit_output = True
                    break
                if "error" not in commit:
                    break
                _log(f"run {name}: commit error: {commit['error']}")
                failure_detail = commit["error"]
                if not failure_detail.startswith("Output format error: "):
                    break
                if format_repairs >= MAX_FORMAT_REPAIRS:
                    _log(f"run {name}: format repair exhausted "
                         f"({format_repairs})")
                    break
                format_repairs += 1
                _log(f"run {name}: repairing result.md (repair "
                     f"{format_repairs})")
                if not _repair_result(root, sid, failure_detail):
                    break
            if bad_commit_output:
                break
            if "error" not in commit and format_repairs > 0:
                # repair rewrote result.md — feed the fixed text forward
                with open(result_path) as f:
                    result_text = f.read()
            if "error" in commit:
                _log(f"run {name}: commit error: {commit['error']}")
                failure_detail = commit["error"]
                if retries < MAX_FAILURE_RETRIES:
                    retries += 1
                    _log(f"run {name}: retrying step (retry {retries})")
                    continue
                end = "commit_error"
                break
            if not commit.get("next_action"):
                _log(f"run {name}: no state advance, stopping")
                end = "no_advance"
                break
            if commit.get("next_action") == "_GRAY_LIST_":
                _log(f"run {name}: gray-listed, stopping for human review")
                end = "gray_list"
                break
            prev_result = result_text
        _log(f"run {name}: finished ({end}) after {steps} step(s)")
        return {"requirement": name, "steps": steps, "end": end}
    finally:
        release_lock(root)
        if end != "gray_list":
            _clear_approval(name)
        finished_at = time.time()
        elapsed_min = int((finished_at - start) // 60)
        _record_run(name, end, steps, start, finished_at)
        notify_text(
            _end_message(name, end, steps, elapsed_min, failure_detail, root),
            user_id)


def dispatch(entries, max_concurrency=2):
    """Fork 'run' subprocesses for approved auto-executable entries."""
    forked = []
    running = running_count()
    for entry in entries:
        if not entry.get("approved") or not _entry_auto_exec(entry):
            continue
        root = entry.get("root")
        if running >= max_concurrency:
            _log(f"dispatch: skip {entry['requirement']} — "
                 f"concurrency limit {max_concurrency}")
            continue
        if is_locked(root):
            _log(f"dispatch: skip {entry['requirement']} — lock held")
            continue
        if _has_pending_gray_drafts(root):
            _log(f"dispatch: skip {entry['requirement']} — pending gray drafts")
            continue
        with open(LOG_PATH, "a") as logf:
            proc = subprocess.Popen(
                _engine_cmd("run", entry["requirement"]),
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        _log(f"dispatch: forked run for {entry['requirement']} (pid {proc.pid})")
        forked.append(entry["requirement"])
        running += 1
    return forked
