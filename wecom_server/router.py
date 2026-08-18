"""Intent classification and dispatch for WeCom messages.

All messages go through async LLM path (qodercli subprocess, result pushed
via WeCom API). No keyword matching — LLM handles everything.
"""
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

logger = logging.getLogger("wecom")

# qodercli startup noise that leaks into stdout before the actual LLM reply
_LLM_STDOUT_NOISE = (
    "MCP issues detected",
    "All dependencies are up to date",
    "qodercli ",
)

_LLM_SYSTEM_PROMPT = (
    "You are a WeCom bot assistant for the loop_engine project.\n\n"
    "Answer concisely in Chinese. "
    "Every reply MUST start with '【<requirement name>】' (use 【通用】 when unknown). "
    "Keep reply under 500 chars. "
    "Use only WeCom markdown: **bold**, > quote, "
    "<font color=\"info|comment|warning\">text</font>. "
    "No tables, code blocks, lists, or links.\n\n"
    "System actions: to trigger a backend action, append on its own line:\n"
    "__JSON_ACTION__ {\"action\": \"<action>\", \"requirement\": \"<name>\", ...}\n"
    "Actions: approve | spec_result(requirement+module) | history | "
    "gray_list | adjudicate(requirement+target+decision)\n"
    "Do NOT add __JSON_ACTION__ when no action is needed.\n\n"
    "Workflow skills (read when relevant):\n"
    "- Requirement registration/PRD bootstrap → ~/.qoder/skills/requirement-register/SKILL.md\n"
    "- Spec editing workflow → ~/.qoder/skills/spec-session/SKILL.md\n"
    "- Manual loop execution → ~/.qoder/skills/manual-loop/SKILL.md\n\n"
    "User: __MESSAGE__\n"
)


_AUDIT_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hooks", "audit_hook.sh")


# Per-requirement module index: reload only when state.json changes.
# WeCom server is a long-running process, so in-process cache is effective;
# every message otherwise re-reads every requirement's full state.json just
# to learn its module keys for routing.
# ponytail: 复用 StateManager(root).load() 需要每消息全量重读，缓存键用
# (mtime_ns, size)，stat 是纳秒级，失效由 state.json 写入自然触发
_module_index_cache = {}  # root -> {"mtime_ns": int, "size": int, "modules": dict}


def _cached_modules(root):
    """Return the modules dict of a requirement's state.json, cached by
    (mtime_ns, size). Returns {} on any error (missing/corrupt)."""
    state_path = os.path.join(root, ".loop", "state.json")
    try:
        st = os.stat(state_path)
    except OSError:
        _module_index_cache.pop(root, None)
        return {}
    entry = _module_index_cache.get(root)
    if entry and entry["mtime_ns"] == st.st_mtime_ns and entry["size"] == st.st_size:
        return entry["modules"]
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from state import StateManager
        modules = StateManager(root).load().get("modules", {})
    except Exception:
        modules = {}
    _module_index_cache[root] = {
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "modules": modules,
    }
    return modules


def _audit_settings():
    """Per-invocation qodercli settings auditing sensitive tool calls.

    Injected via --settings so only WeCom-spawned sessions carry the hook;
    the user's own qodercli sessions are untouched.
    """
    return json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command", "command": _AUDIT_HOOK}]}
            ]
        }
    })


def _get_model():
    """Read persisted model from qodercli settings, fallback to DeepSeek-V4-Flash."""
    settings_path = os.path.expanduser("~/.qoder/settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
            return settings.get("model", {}).get("name", "DeepSeek-V4-Flash")
    except Exception:
        return "DeepSeek-V4-Flash"


_SESSION_DIR = os.path.expanduser("~/.qoder/loop_engine/sessions")


def _get_session_id(user_id, requirement="global"):
    """Get or create a stable qodercli session ID per WeCom user and
    requirement. Sessions are split per requirement so conversations
    about different requirements never share context.
    Returns (session_id, is_new) where is_new=True means first-time use.
    """
    os.makedirs(_SESSION_DIR, exist_ok=True)
    safe = requirement.replace("/", "_").replace(":", "_").replace(" ", "_")
    path = os.path.join(_SESSION_DIR, f"{user_id}__{safe}.txt")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip(), False
    sid = str(uuid.uuid4())
    with open(path, "w") as f:
        f.write(sid)
    return sid, True


def _detect_requirement(message, registry):
    """Deterministically find which requirement a message belongs to.
    Matches requirement names and their module keys/names; returns the
    first (leftmost) hit, or None to fall back to the global session.
    """
    hits = []
    for req in registry:
        name = req.get("name", "")
        if name and name in message:
            hits.append((message.index(name), name))
    for req in registry:
        for key in _cached_modules(req.get("root", "")):
            if key in message:
                hits.append((message.index(key), req["name"]))
                continue
            mod = key.rsplit("/", 1)[-1]
            if mod and mod in message:
                hits.append((message.index(mod), req["name"]))
    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], -len(h[1])))
    return hits[0][1]


def _execute_history(name, registry, data_dir):
    """Read runs.json and format the last executions. Returns reply text."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import scheduler
    runs = scheduler.load_runs()["runs"]
    if name != "ALL":
        runs = [r for r in runs if r["requirement"] == name]
    if not runs:
        return "暂无执行历史记录。"
    lines = [f"最近 {len(runs[-5:])} 次执行："]
    for r in runs[-5:]:
        lines.append(
            f"• {r['requirement']}：{r['end']}，{r['steps']} 步，"
            f"{r['duration_seconds']} 秒（{r['finished_at'][:16]}）")
    return "\n".join(lines)


def _pending_gray_drafts(registry, name="ALL"):
    """Collect pending gray-list drafts across requirements. Returns
    list of (req_name, draft) tuples."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from state import StateManager
    found = []
    for req in registry:
        if name != "ALL" and req.get("name") != name:
            continue
        try:
            st = StateManager(req.get("root", "")).load()
        except Exception:
            continue
        for d in st.get("gray_drafts", []):
            if d.get("status") == "pending":
                found.append((req.get("name", "?"), d))
    return found


def _execute_gray_list_view(name, registry, data_dir):
    """List pending gray-list drafts with adjudication instructions."""
    found = _pending_gray_drafts(registry, name)
    if not found:
        req = next((r for r in registry if r.get("name") == name), None)
        if name != "ALL" and not req:
            available = ", ".join(r.get("name", "?") for r in registry) or "无"
            return f"没有找到需求：{name}（可用：{available}）"
        return "当前没有待裁决的灰名单草稿。"
    lines = []
    for req_name, d in found:
        label = d.get("type_label", "")
        summary = d.get("summary", "")
        lines.append(f"「{req_name}」草稿 {d['id']}："
                     f"[{label}] {summary}")
        lines.append(f"→ 回复「接受 {d['id']}」或「拒绝 {d['id']}」裁决该条")
    lines.append("多条可一起处理：「全部接受」/「全部拒绝」，"
                 "或混合：「接受 1，拒绝 2 3」")
    return "\n".join(lines)


def _parse_decision_pairs(text):
    """Parse mixed adjudication '1=accept, 2=reject' into {id: decision}.

    Returns None when any token is malformed, so callers can report a
    format error instead of silently dropping decisions.
    """
    pairs = {}
    for token in re.split(r"[\s,，]+", text.strip()):
        if not token:
            continue
        m = re.fullmatch(r"(\d+)=(accept|reject)", token)
        if not m:
            return None
        pairs[int(m.group(1))] = m.group(2)
    return pairs


def _execute_adjudicate(name, target, decision, registry, data_dir):
    """Adjudicate gray-list drafts: target is 'all' or one/more draft ids."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from state import StateManager
    from machine import resolve_gray_draft

    if name == "ALL":
        return _execute_adjudicate_all(target, decision, registry, data_dir)

    req = next((r for r in registry if r.get("name") == name), None)
    if not req:
        available = ", ".join(r.get("name", "?") for r in registry) or "无"
        return f"没有找到需求：{name}（可用：{available}）"
    root = req["root"]
    import scheduler as _sched
    if _sched.is_locked(root):
        return f"需求「{name}」正在执行中，请等待完成后再裁决草稿"
    sm = StateManager(root)
    st = sm.load()
    drafts = st.get("gray_drafts", [])
    pending = [d for d in drafts if d.get("status") == "pending"]
    if decision in ("accept", "reject"):
        if target == "all":
            ids = [d["id"] for d in pending]
        else:
            try:
                ids = [int(t) for t in str(target).split(",") if t.strip()]
            except ValueError:
                return f"无法识别的草稿编号：{target}"
        pairs = {i: decision for i in ids}
    elif target == "mixed":
        pairs = _parse_decision_pairs(decision)
        if not pairs:
            return (f"无法识别的混合裁决格式：{decision}"
                    f"（应为 1=accept,2=reject 形式）")
    else:
        return f"无法识别的草稿编号：{target}"
    if not pairs:
        return "当前没有待裁决的草稿。"
    messages = []
    for draft_id, draft_decision in pairs.items():
        ok, msg = resolve_gray_draft(sm, draft_id, draft_decision)
        messages.append(msg)
    st = sm.load()
    remaining = [d for d in st.get("gray_drafts", [])
                 if d.get("status") == "pending"]
    lines = ["\n".join(messages)]
    if not remaining:
        import scheduler
        try:
            scheduler.approve(name)
        except ValueError:
            scheduler.poll()
            scheduler.approve(name)
        cfg = scheduler.load_config()
        scheduler.dispatch(scheduler.load_pending()["pending"],
                           max_concurrency=cfg.get("max_concurrency", 2))
        lines.append(f"灰名单已全部裁决完毕，继续执行 {name}")
    else:
        lines.append(f"还有 {len(remaining)} 条待裁决"
                     f"（回复「查看灰名单」查看）")
    return "\n".join(lines)


def _execute_adjudicate_all(target, decision, registry, data_dir):
    """Adjudicate drafts across all requirements with pending items."""
    if decision not in ("accept", "reject"):
        return "跨需求裁决仅支持单一决策（accept/reject）"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from state import StateManager
    from machine import resolve_gray_draft
    all_lines = []
    for req in registry:
        name = req.get("name", "?")
        root = req.get("root", "")
        if not root:
            continue
        sm = StateManager(root)
        try:
            st = sm.load()
        except Exception:
            continue
        pending = [d for d in st.get("gray_drafts", [])
                   if d.get("status") == "pending"]
        if not pending:
            continue
        if target == "all":
            ids = [d["id"] for d in pending]
        else:
            ids = [int(t) for t in str(target).split(",") if t.strip()]
        messages = []
        for draft_id in ids:
            ok, msg = resolve_gray_draft(sm, draft_id, decision)
            messages.append(msg)
        st = sm.load()
        remaining = [d for d in st.get("gray_drafts", [])
                     if d.get("status") == "pending"]
        all_lines.append(f"【{name}】 " + "; ".join(messages))
        if not remaining:
            import scheduler
            try:
                scheduler.approve(name)
            except ValueError:
                scheduler.poll()
                scheduler.approve(name)
            cfg = scheduler.load_config()
            scheduler.dispatch(scheduler.load_pending()["pending"],
                               max_concurrency=cfg.get("max_concurrency", 2))
            all_lines.append(f"  → 灰名单已全部裁决完毕，继续执行 {name}")
        else:
            all_lines.append(f"  → 还有 {len(remaining)} 条待裁决（回复「查看灰名单」查看）")
    if not all_lines:
        return "当前没有待裁决的草稿。"
    return "\n".join(all_lines)


def _approve_prefix_block(name, registry):
    """Fast reject __APPROVE__ when no module status in the requirement's
    state permits APPROVE (per STATUS_TABLE prefixes), avoiding a full
    scheduler.approve round-trip. Returns None when approval may proceed."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from constants import STATUS_TABLE
    from state import StateManager
    req = next((r for r in registry if r.get("name") == name), None)
    if not req:
        return None
    try:
        st = StateManager(req.get("root", "")).load()
    except Exception:
        return None
    modules = st.get("modules", {})
    if not modules:
        return None
    for m in modules.values():
        if "APPROVE" in STATUS_TABLE.get(m.get("status"), {}).get(
                "prefixes", ()):
            return None
    return (f"无法批准：{name} 当前没有可批准执行的工作"
            f"（模块状态不支持自动执行）")


def _execute_approve(name, registry, data_dir, user_id=None):
    """Approve + dispatch a requirement for real execution. Returns reply text."""
    req = next((r for r in registry if r.get("name") == name), None)
    if not req:
        available = ", ".join(r.get("name", "?") for r in registry) or "无"
        return f"没有找到需求：{name}（可用：{available}）"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import scheduler
    try:
        count = scheduler.approve(name, approved_by=user_id)
    except ValueError as e:
        return f"无法批准：{e}"
    if count == 0:
        return f"{name} 没有待批准的自动执行项（可能已批准）"
    cfg = scheduler.load_config()
    forked = scheduler.dispatch(scheduler.load_pending()["pending"],
                                max_concurrency=cfg.get("max_concurrency", 2))
    if name in forked:
        return f"已批准并开始执行：{name}"
    return f"已批准 {name}，等待调度（并发上限或正在运行）"


def _audit_line(text):
    """Append a line to the shared audit log (same file as audit_hook.sh)."""
    try:
        log_path = os.path.expanduser("~/.qoder/loop_engine/audit.log")
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with open(log_path, "a") as f:
            f.write(f"[{ts}] {text}\n")
    except Exception:
        logger.exception("[wecom] audit log write failed")


def _resolve_module_key(st, key):
    """Resolve a user-supplied module key, auto-completing bare names."""
    modules = st.get("modules", {})
    if key in modules:
        return key
    if "/" in key:
        raise ValueError(
            f"模块 {key} 不在状态机中（可用：{', '.join(modules) or '无'}）")
    matches = [k for k in modules if k.rsplit("/", 1)[-1] == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"模块名 {key} 对应多个模块（{'、'.join(sorted(matches))}），"
            "请在回复末尾追加 __JSON_ACTION__ {\"action\":\"spec_result\",\"requirement\":\"<需求名>\",\"module\":\"<change_id>/<module_name>\"}")
    raise ValueError(f"找不到模块 {key}（可用：{', '.join(modules) or '无'}）")


def _execute_spec_result_by_key(module_key, registry, data_dir):
    """Locate the requirement owning module_key and register the change."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from state import StateManager
    owners = []
    for req in registry:
        try:
            st = StateManager(req.get("root", "")).load()
        except Exception:
            continue
        if module_key in st.get("modules", {}):
            owners.append(req.get("name", "?"))
    if len(owners) == 1:
        return _execute_spec_result(owners[0], module_key, registry, data_dir)
    if len(owners) > 1:
        return (f"模块 {module_key} 对应多个需求（{'、'.join(owners)}），"
                "请在回复末尾追加 __JSON_ACTION__ {\"action\":\"spec_result\",\"requirement\":\"<需求名>\",\"module\":\"<change_id>/<module_name>\"}")
    return (f"找不到模块 {module_key}：没有需求的状态机包含该模块，"
            "请在回复末尾追加 __JSON_ACTION__ {\"action\":\"spec_result\",\"requirement\":\"<需求名>\",\"module\":\"<change_id>/<module_name>\"}")


def _execute_spec_result(name, module_key, registry, data_dir):
    """Register a G-edited spec change: verify hash changed, backup, PARTIAL.

    The spec file itself is edited by the assistant (audited by the hook);
    this function only controls the registration gate so the scheduler picks
    the change up only after the user approves execution. New modules not
    yet in state.json are registered here (full change_id/module_name key
    with an existing spec file).
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from constants import SPEC_PATH_TEMPLATE, PARTIAL, DRAFT
    import spec_utils
    from state import StateManager

    req = next((r for r in registry if r.get("name") == name), None)
    if not req:
        available = ", ".join(r.get("name", "?") for r in registry) or "无"
        return f"没有找到需求：{name}（可用：{available}）"
    root = req["root"]
    import scheduler as _sched
    if _sched.is_locked(root):
        return f"需求「{name}」正在执行中，请等待完成后再注册 spec 变更"
    sm = StateManager(root)
    st = sm.load()
    try:
        module_key = _resolve_module_key(st, module_key)
    except ValueError as e:
        # New module: auto-complete can't know it, but a full
        # change_id/module_name key with an existing spec file is valid.
        if "/" not in module_key:
            return str(e)
        change_id, module_name = module_key.split("/", 1)
        probe = os.path.join(root, SPEC_PATH_TEMPLATE.format(
            change_id=change_id, module_name=module_name))
        if not os.path.exists(probe):
            return str(e)
    else:
        change_id, module_name = module_key.split("/", 1)
    spec_path = os.path.join(root, SPEC_PATH_TEMPLATE.format(
        change_id=change_id, module_name=module_name))
    if not os.path.exists(spec_path):
        return (f"找不到 spec 文件：{spec_path}。"
                "请先编辑 spec，再在回复末尾追加 __JSON_ACTION__ {\"action\":\"spec_result\",...}")
    new_hash = spec_utils.compute_spec_hash(spec_path)
    new_norm_hash = spec_utils.compute_spec_norm_hash(spec_path)
    if module_key not in st["modules"]:
        project_root = spec_utils.resolve_project_root(root, module_name)
        StateManager.add_module(
            st, module_key, change_id, module_name,
            project_root=project_root or ".",
            spec_hash=new_hash, spec_norm_hash=new_norm_hash)
        old_hash = None
    else:
        old_hash = st["modules"][module_key].get("spec_hash")
        if old_hash == new_hash and \
                st["modules"][module_key].get("status") != DRAFT:
            return f"{module_key} 的 spec 没有变化（hash 未变），请先修改 spec.md"
    backup_dir = os.path.join(root, ".loop", "backup")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(
        backup_dir, f"spec-{module_name}-{int(time.time())}.md")
    rel = os.path.relpath(spec_path, root)
    old = subprocess.run(["git", "-C", root, "show", f"HEAD:{rel}"],
                         capture_output=True, text=True)
    if old.returncode == 0:
        with open(backup_path, "w") as f:
            f.write(old.stdout)
        backup_note = backup_path + " (HEAD)"
    else:
        # no git HEAD for this spec — snapshot current content so there is
        # at least a registration-time rollback point
        shutil.copy2(spec_path, backup_path)
        backup_note = backup_path + " (pre-edit snapshot, no git HEAD)"
    sm.set_module_field(st, module_key, "spec_hash", new_hash)
    if new_norm_hash:
        sm.set_module_field(st, module_key, "spec_norm_hash", new_norm_hash)
    sm.set_module_field(st, module_key, "status", PARTIAL)
    sm.set_module_field(st, module_key, "maker_attempt", 0)
    sm.set_module_field(st, module_key, "review_fix_attempt", 0)
    sm.save(st)
    _audit_line(f"SPEC {name} {module_key} {old_hash or 'new'}->{new_hash} "
                f"backup={backup_note}")
    # Refresh pending.json so approve works immediately
    import scheduler as _sched
    _sched.poll()
    bind_hint = ""
    if module_key in st["modules"] and st["modules"][module_key].get("project_root") == ".":
        bind_hint = "新模块尚未绑定项目，请先确认所属项目（set-project-root）\n"
    return (f"spec 变更已登记：{module_key}\n"
            f"旧 hash: {(old_hash or 'new')[:8]}  新 hash: {new_hash[:8]}\n"
            f"备份: {backup_note}\n"
            f"{bind_hint}请回复『批准执行 {name}』开始实现")


def _classify_requirement(message, registry):
    """Semantic fallback: ask the LLM which requirement a message is about.
    Returns a requirement name, or None when nothing matches. One-off
    call with no session context, so it never pollutes any conversation.
    """
    names = [r.get("name") for r in registry if r.get("name")]
    if not names:
        return None
    qodercli_path = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")
    prompt = (
        "你只做需求归属分类，不回答其他问题。可选需求："
        + "、".join(names)
        + "\n判断下面这条用户消息在聊哪个需求。消息没有明确指向任何需求，"
          "就回答「无」。只输出一个需求名或「无」，不要输出任何其他内容。\n\n"
        + f"消息：{message}"
    )
    try:
        r = subprocess.run(
            [qodercli_path, "--print", "--session-id", str(uuid.uuid4()),
             "--model", _get_model(), "--dangerously-skip-permissions"],
            input=prompt, capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.warning("[wecom] requirement classify failed: %s", e)
        return None
    reply = (r.stdout or "").strip()
    for name in names:
        if name in reply:
            return name
    return None


def _dispatch_json_action(payload, registry, data_dir, user_id):
    """Dispatch a __JSON_ACTION__ payload. Returns reply text, or an
    error message for unknown actions / missing parameters."""
    action = payload.get("action")
    if action == "approve":
        name = payload.get("requirement")
        if not name:
            return "缺少参数：requirement"
        blocked = _approve_prefix_block(name, registry)
        if blocked:
            return blocked
        return _execute_approve(name, registry, data_dir, user_id)
    if action == "spec_result":
        name = payload.get("requirement")
        module = payload.get("module")
        if not name or not module:
            return "缺少参数：requirement/module"
        return _execute_spec_result(name, module, registry, data_dir)
    if action == "adjudicate":
        name = payload.get("requirement") or "ALL"
        target = payload.get("target")
        decision = payload.get("decision")
        if not target or not decision:
            return "缺少参数：target/decision"
        return _execute_adjudicate(name, target, decision, registry, data_dir)
    if action == "gray_list":
        return _execute_gray_list_view(payload.get("requirement") or "ALL",
                                       registry, data_dir)
    if action == "history":
        return _execute_history(payload.get("requirement") or "ALL",
                                registry, data_dir)
    return f"未知 action：{action or '(空)'}"


_JSON_ACTION_RE = re.compile(r"__JSON_ACTION__\s*(\{[^{}]*\})", re.DOTALL)

_LEGACY_PREFIXES = ("__APPROVE__", "__HISTORY__", "__GRAY_LIST__",
                    "__ADJUDICATE__", "__SPEC_RESULT__")

# Per-requirement LLM lock: same requirement shares one session, so its
# qodercli calls must never overlap. Server-side queues already serialize
# detected requirements; this covers messages that only classify (LLM) to a
# requirement and would otherwise run on the "global" queue in parallel.
# ponytail: 队列按 detect 结果分流，classify 结果只有执行时才知道，
# 锁是防同 session 并发写的兜底，key 与 session 维度一致
_llm_locks = {}
_llm_locks_guard = threading.Lock()


def _llm_lock(requirement):
    with _llm_locks_guard:
        return _llm_locks.setdefault(requirement, threading.Lock())


def _llm_dispatch(message, registry, data_dir, user_id):
    """Background LLM direct response with per-user/per-requirement session."""
    qodercli_path = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")
    model = _get_model()
    requirement = _detect_requirement(message, registry)
    if not requirement:
        # exact match failed — ask the LLM which requirement this is about
        requirement = _classify_requirement(message, registry)
    session_id, is_new = _get_session_id(user_id, requirement or "global")
    prompt = _LLM_SYSTEM_PROMPT.replace("__MESSAGE__", message, 1)
    # first message creates session, subsequent messages resume it
    session_flag = "--session-id" if is_new else "--resume"
    settings = _audit_settings()
    with _llm_lock(requirement or "global"):
        try:
            r = subprocess.run(
                [qodercli_path, "--print", session_flag, session_id, "--model", model,
                 "--dangerously-skip-permissions", "--settings", settings],
                input=prompt, capture_output=True, text=True, timeout=900,
            )
            lines = (r.stdout or "").splitlines()
            while lines and lines[0].strip().startswith(_LLM_STDOUT_NOISE):
                lines.pop(0)
            reply = "\n".join(lines).strip()
            # resume failed (session lost, e.g. after server restart) → create fresh
            if not reply and not is_new:
                logger.info("[wecom] session %s not found, creating new", session_id)
                r = subprocess.run(
                    [qodercli_path, "--print", "--session-id", session_id, "--model", model,
                     "--dangerously-skip-permissions", "--settings", settings],
                    input=prompt, capture_output=True, text=True, timeout=900,
                )
                lines = (r.stdout or "").splitlines()
                while lines and lines[0].strip().startswith(_LLM_STDOUT_NOISE):
                    lines.pop(0)
                reply = "\n".join(lines).strip()
        except Exception as e:
            logger.error("[wecom] LLM dispatch error: %s", e)
            return f"处理失败：{e}"
    if not reply:
        return "无响应，请稍后再试。"
    if requirement and not reply.startswith(("__", "【")):
        reply = f"【{requirement}】\n{reply}"
    json_match = _JSON_ACTION_RE.search(reply)
    if json_match:
        try:
            payload = json.loads(json_match.group(1))
        except ValueError:
            logger.warning("[wecom] __JSON_ACTION__ invalid JSON, "
                           "falling back to legacy prefixes")
            payload = None
        if payload is not None:
            return _dispatch_json_action(payload, registry, data_dir,
                                         user_id)
    if reply.startswith(_LEGACY_PREFIXES):
        logger.info("[wecom] legacy prefix reply (deprecated, prefer "
                    "__JSON_ACTION__)")
    if reply.startswith("__APPROVE__"):
        name = reply[len("__APPROVE__"):].strip().splitlines()[0].strip()
        blocked = _approve_prefix_block(name, registry)
        if blocked:
            return blocked
        return _execute_approve(name, registry, data_dir, user_id)
    if reply.startswith("__HISTORY__"):
        name = reply[len("__HISTORY__"):].strip().splitlines()[0].strip() or "ALL"
        return _execute_history(name, registry, data_dir)
    if reply.startswith("__GRAY_LIST__"):
        name = reply[len("__GRAY_LIST__"):].strip().splitlines()[0].strip() or "ALL"
        return _execute_gray_list_view(name, registry, data_dir)
    if reply.startswith("__ADJUDICATE__"):
        rest = reply[len("__ADJUDICATE__"):].strip().splitlines()[0].strip()
        parts = rest.split()
        if len(parts) < 2:
            return ("格式错误：__ADJUDICATE__ <需求名> "
                    "<编号|all|mixed> <accept|reject>")
        name = parts[0]
        if parts[1] == "mixed":
            if len(parts) < 3:
                return "格式错误：mixed 需要 id=accept,id=reject 决策对"
            target, decision = "mixed", " ".join(parts[2:])
        else:
            if len(parts) != 3:
                return ("格式错误：__ADJUDICATE__ <需求名> "
                        "<编号|all|mixed> <accept|reject>")
            target, decision = parts[1], parts[2]
        return _execute_adjudicate(name, target, decision,
                                   registry, data_dir)
    if reply.startswith("__SPEC_RESULT__"):
        rest = reply[len("__SPEC_RESULT__"):].strip().splitlines()[0].strip()
        parts = rest.split(None, 1)
        if len(parts) == 1:
            # single-token full key (assistant merged name+key) — locate owner
            return _execute_spec_result_by_key(parts[0], registry, data_dir)
        if len(parts) != 2:
            return ("格式错误：__SPEC_RESULT__ <需求名> "
                    "<change_id>/<module_name>")
        return _execute_spec_result(parts[0], parts[1], registry, data_dir)
    return reply


def dispatch(message, registry, data_dir, user_id="default"):
    """Classify and dispatch to the right handler.

    Returns Callable[[], str] for async (return "success" immediately,
    push result via WeCom API in background). The callable carries the
    deterministically detected requirement so the server can queue messages
    per requirement (serial within, parallel across).
    """
    fn = lambda: _llm_dispatch(message, registry, data_dir, user_id)
    fn.requirement = _detect_requirement(message, registry)
    return fn
