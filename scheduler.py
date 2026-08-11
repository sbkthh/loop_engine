"""Scheduler (Layer 2): poll requirements, track pending work, dispatch runs.

Pure Python, no LLM. Communicates with the loop engine core only through
files (.loop/state.json, spec.md) and CLI subprocesses — never imports core
modules, per the orchestration design spec.
"""

import datetime
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import requests  # noqa: E402 — used by notify_pending()

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
SPEC_GLOB = "openspec/changes/*/specs/*/spec.md"

SYNCED = "SYNCED"
PARTIAL = "PARTIAL"
READY = "READY"
NEEDS_REFINEMENT = "NEEDS_REFINEMENT"
BLOCKED = "BLOCKED"
DRAFT = "DRAFT"

SPEC_CHANGED = "SPEC_CHANGED"
READY_PENDING = "READY_PENDING"

AUTO_EXECUTABLE = (SPEC_CHANGED, READY_PENDING)

_TRIGGER_FOR_STATUS = {
    PARTIAL: SPEC_CHANGED,
    READY: READY_PENDING,
    NEEDS_REFINEMENT: NEEDS_REFINEMENT,
    BLOCKED: BLOCKED,
    DRAFT: DRAFT,
}
_TRIGGER_PRIORITY = (PARTIAL, READY, NEEDS_REFINEMENT, BLOCKED, DRAFT)

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
LOCK_MAX_AGE_SECONDS = 24 * 3600  # live-PID lock older than this is reclaimed

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

def _compute_spec_hash(spec_path):
    if not os.path.exists(spec_path):
        return None
    with open(spec_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _discover_specs(root):
    found = []
    for path in sorted(glob.glob(os.path.join(root, SPEC_GLOB), recursive=True)):
        parts = path.replace("\\", "/").split("/")
        try:
            ci = parts.index("changes") + 1
            si = parts.index("specs", ci) + 1
        except (ValueError, IndexError):
            continue
        found.append((parts[ci], parts[si]))
    return found


def _poll_requirement(root, name):
    """Detect pending work for one requirement. Never writes state.json."""
    state_path = os.path.join(root, STATE_FILE)
    if not os.path.exists(state_path):
        return None
    with open(state_path) as f:
        state = json.load(f)
    if state.get("current", {}).get("action"):
        return None  # mid-progress — being executed, skip
    modules = state.get("modules", {})
    detected = []
    for key, module in modules.items():
        status = module.get("status")
        hash_changed = False
        if status == SYNCED:
            spec_path = os.path.join(
                root, "openspec", "changes", module.get("change_id", ""),
                "specs", module.get("module_name", ""), "spec.md")
            current_hash = _compute_spec_hash(spec_path)
            if not current_hash or current_hash == module.get("spec_hash"):
                continue
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
    for change_id, module_name in _discover_specs(root):
        key = f"{change_id}/{module_name}"
        if key not in modules:
            detected.append({
                "key": key, "status": DRAFT,
                "spec_hash_changed": False, "cross_project": False,
            })
    if not detected:
        return None
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
    fresh = []
    for req in _read_registry():
        entry = _poll_requirement(req.get("root"), req.get("name"))
        if entry:
            fresh.append(entry)
    prev = load_pending()
    newly_detected = []
    for entry in fresh:
        old = _find_entry(prev, entry["requirement"])
        if old and old.get("approved"):
            entry["approved"] = True
            entry["detected_at"] = old.get("detected_at", entry["detected_at"])
        elif not old:
            newly_detected.append(entry)
    _save_pending({"pending": fresh})
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
    if state.get("current", {}).get("action"):
        return f"{name} 正在执行中，无需重复批准"
    if all(m.get("status") == SYNCED
           for m in state.get("modules", {}).values()):
        return f"{name} 已执行完成（SYNCED），无需批准"
    return f"{name} 当前没有待执行工作（等待下次 poll 检测）"


def approve(name=None, all_=False, approved_by=None):
    data = load_pending()
    entries = data.get("pending", [])
    if all_:
        count = 0
        for e in entries:
            if e.get("trigger") in AUTO_EXECUTABLE and not e.get("approved"):
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
    if entry.get("trigger") not in AUTO_EXECUTABLE:
        raise ValueError(
            f"{name} ({entry.get('trigger')}) is report-only — "
            "needs spec session work, not auto-execution")
    if entry.get("approved"):
        return 0
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


def notify_pending(fresh_entries):
    """Push WeCom notification for newly detected pending items."""
    if not fresh_entries:
        return
    lines = ["[调度] 检测到待处理项："]
    for entry in fresh_entries:
        trigger = entry.get("trigger", "UNKNOWN")
        modules = entry.get("modules", [])
        names = ", ".join(m.get("key", "?") for m in modules)
        lines.append(f"• {entry['requirement']} ({trigger}): {names}")
    lines.append("终端执行 'loop_engine approve <name>' 确认后调度器开始执行。")
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


def acquire_lock(root):
    """Returns True if the lock is acquired (reclaims stale locks).

    A lock is stale when its PID is dead, or when a live PID has not
    touched the lock for LOCK_MAX_AGE_SECONDS (run_requirement refreshes
    it every step, so an untouched lock means the runner is wedged).
    """
    path = _lock_path(root)
    if os.path.exists(path):
        try:
            with open(path) as f:
                pid = int(f.read().strip() or "0")
        except (ValueError, OSError):
            pid = None
        if pid and _pid_alive(pid):
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                age = 0
            if age <= LOCK_MAX_AGE_SECONDS:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock(root):
    path = _lock_path(root)
    try:
        with open(path) as f:
            pid = int(f.read().strip() or "0")
    except (ValueError, OSError, FileNotFoundError):
        return
    if pid == os.getpid():
        try:
            os.unlink(path)
        except OSError:
            pass


def is_locked(root):
    path = _lock_path(root)
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            pid = int(f.read().strip() or "0")
    except (ValueError, OSError):
        return False
    return _pid_alive(pid)


def manual_begin(root):
    """Acquire the requirement lock for a manual (G-driven) loop.

    Uses the same lock as run_requirement, so a manual loop and the
    scheduler can never run the same requirement concurrently. The session
    owner is recorded in .loop/manual.json so manual_end can tell whether
    the lock still belongs to this session before releasing it.
    """
    if not acquire_lock(root):
        return False
    try:
        with open(os.path.join(root, MANUAL_FILE), "w") as f:
            json.dump({"pid": os.getpid(), "started_at": time.time(),
                       "steps": 0}, f)
    except OSError:
        release_lock(root)
        return False
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

    Returns False (without touching the lock) when the lock was replaced
    by another process — e.g. a scheduler run after the manual session died.
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
    try:
        os.unlink(_lock_path(root))
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


def running_count(registry_entries=None):
    if registry_entries is None:
        registry_entries = _read_registry()
    return sum(1 for r in registry_entries if is_locked(r.get("root", "")))


# ---------- execution ----------

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
            os.utime(_lock_path(root), None)  # keep lock fresh for staleness check
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
            r = subprocess.run(_engine_cmd("next", "--root", root),
                               capture_output=True, text=True)
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
            q = subprocess.run(
                [_QODERCLI, "--print", "--session-id", sid,
                 "--no-session-persistence",
                 "--dangerously-skip-permissions",
                 "--cwd", root, "--append-system-prompt", LOOP_AGENT_PROMPT,
                 json.dumps(payload)],
                capture_output=True, text=True)
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
            c = subprocess.run(_engine_cmd("commit", "--root", root),
                               capture_output=True, text=True)
            try:
                commit = json.loads(c.stdout or "{}")
            except json.JSONDecodeError:
                _log(f"run {name}: invalid commit output")
                end = "bad_commit_output"
                break
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
        _clear_approval(name)
        finished_at = time.time()
        elapsed_min = int((finished_at - start) // 60)
        _record_run(name, end, steps, start, finished_at)
        if end == "idle":
            notify_text(f"[调度] {name} 执行完成：idle，共 {steps} 步，耗时 {elapsed_min} 分钟",
                        user_id)
        else:
            detail = f"：{failure_detail}" if failure_detail else ""
            notify_text(f"[调度] {name} 执行结束：{end}{detail}，共 {steps} 步，耗时 {elapsed_min} 分钟",
                        user_id)


def dispatch(entries, max_concurrency=2):
    """Fork 'run' subprocesses for approved auto-executable entries."""
    forked = []
    running = running_count()
    for entry in entries:
        if not entry.get("approved") or entry.get("trigger") not in AUTO_EXECUTABLE:
            continue
        root = entry.get("root")
        if running >= max_concurrency:
            _log(f"dispatch: skip {entry['requirement']} — "
                 f"concurrency limit {max_concurrency}")
            continue
        if is_locked(root):
            _log(f"dispatch: skip {entry['requirement']} — lock held")
            continue
        with open(LOG_PATH, "a") as logf:
            proc = subprocess.Popen(
                _engine_cmd("run", entry["requirement"]),
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        _log(f"dispatch: forked run for {entry['requirement']} (pid {proc.pid})")
        forked.append(entry["requirement"])
        running += 1
    return forked
