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

import requests  # noqa: E402 — used by notify_pending()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.expanduser("~/.qoder/loop_engine")
REGISTRY_PATH = os.path.join(DATA_DIR, "requirements.json")
PENDING_PATH = os.path.join(DATA_DIR, "pending.json")
CONFIG_PATH = os.path.join(DATA_DIR, "schedule.json")
LOG_PATH = os.path.join(DATA_DIR, "schedule.log")

STATE_FILE = ".loop/state.json"
LOCK_FILE = ".loop/lock"
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

DEFAULT_CONFIG = {"interval_minutes": 5, "max_concurrency": 2, "last_run": None}

LOOP_AGENT_PROMPT = (
    "You are a loop engine agent. You will receive directives JSON. "
    "Read the spec/plan files it references, follow the instructions, "
    "and write your output to .loop/result.md in the specified output format."
)

MAX_SAME_ACTION = 3
MAX_TOTAL_STEPS = 200

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


def approve(name=None, all_=False):
    data = load_pending()
    entries = data.get("pending", [])
    if all_:
        count = 0
        for e in entries:
            if e.get("trigger") in AUTO_EXECUTABLE and not e.get("approved"):
                e["approved"] = True
                count += 1
        _save_pending(data)
        return count
    if not name:
        raise ValueError("Specify a requirement name or --all")
    entry = _find_entry(data, name)
    if not entry:
        raise ValueError(f"No pending work for requirement: {name}")
    if entry.get("trigger") not in AUTO_EXECUTABLE:
        raise ValueError(
            f"{name} ({entry.get('trigger')}) is report-only — "
            "needs spec session work, not auto-execution")
    if entry.get("approved"):
        return 0
    entry["approved"] = True
    _save_pending(data)
    return 1


def _clear_approval(name):
    data = load_pending()
    before = len(data.get("pending", []))
    data["pending"] = [e for e in data.get("pending", [])
                       if e.get("requirement") != name]
    if len(data["pending"]) < before:
        _save_pending(data)


def notify_pending(fresh_entries):
    """Push WeCom notification for newly detected pending items.

    Uses group-bot webhook when configured (no domain/token needed);
    falls back to the self-built app API for setups with a verified domain.
    """
    if not fresh_entries:
        return
    wecom_config_path = os.path.join(DATA_DIR, "wecom.json")
    if not os.path.exists(wecom_config_path):
        return  # WeCom not configured, skip notification
    with open(wecom_config_path) as f:
        config = json.load(f)
    # Build message
    lines = ["[调度] 检测到待处理项："]
    for entry in fresh_entries:
        trigger = entry.get("trigger", "UNKNOWN")
        modules = entry.get("modules", [])
        names = ", ".join(m.get("key", "?") for m in modules)
        lines.append(f"• {entry['requirement']} ({trigger}): {names}")
    lines.append("终端执行 'loop_engine approve <name>' 确认后调度器开始执行。")
    msg_content = "\n".join(lines)
    # Group bot webhook path (preferred)
    webhook_url = config.get("webhook_url")
    if webhook_url:
        r = requests.post(
            webhook_url,
            json={"msgtype": "text", "text": {"content": msg_content}},
            timeout=10)
        result = r.json()
        if result.get("errcode", -1) != 0:
            _log(f"notify: webhook send failed: {result.get('errmsg')}")
        else:
            _log(f"notify: sent {len(fresh_entries)} pending item(s) to WeCom group")
        return
    # Self-built app API path (requires verified domain in WeCom console)
    if not config.get("corp_id") or not config.get("secret") or not config.get("agent_id"):
        return
    r = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": config["corp_id"], "corpsecret": config["secret"]},
        timeout=10)
    token_data = r.json()
    if token_data.get("errcode", -1) != 0:
        _log(f"notify: gettoken failed: {token_data.get('errmsg')}")
        return
    access_token = token_data["access_token"]
    r2 = requests.post(
        f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}",
        json={
            "touser": "@all",
            "msgtype": "text",
            "agentid": int(config["agent_id"]),
            "text": {"content": msg_content},
            "safe": 0,
        },
        timeout=10)
    result = r2.json()
    if result.get("errcode", -1) != 0:
        _log(f"notify: send failed: {result.get('errmsg')}")
    else:
        _log(f"notify: sent {len(fresh_entries)} pending item(s) to WeCom")


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


def set_interval(minutes):
    if minutes < 1:
        raise ValueError("interval must be >= 1 minute")
    cfg = load_config()
    cfg["interval_minutes"] = int(minutes)
    save_config(cfg)
    return cfg


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
    """Returns True if the lock is acquired (reclaims stale locks)."""
    path = _lock_path(root)
    if os.path.exists(path):
        try:
            with open(path) as f:
                pid = int(f.read().strip() or "0")
        except (ValueError, OSError):
            pid = None
        if pid and _pid_alive(pid):
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
    _log(f"run {name}: start ({root})")
    try:
        steps = 0
        same_action = 0
        last_action = None
        end = "max_steps"
        while steps < MAX_TOTAL_STEPS:
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
            q = subprocess.run(
                [_QODERCLI, "--print", "--dangerously-skip-permissions",
                 "--cwd", root, "--append-system-prompt", LOOP_AGENT_PROMPT,
                 json.dumps(directives)],
                capture_output=True, text=True)
            if q.returncode != 0:
                _log(f"run {name}: qodercli exited {q.returncode}")
                end = "qodercli_failed"
                break
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
        _log(f"run {name}: finished ({end}) after {steps} step(s)")
        return {"requirement": name, "steps": steps, "end": end}
    finally:
        release_lock(root)
        _clear_approval(name)


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
