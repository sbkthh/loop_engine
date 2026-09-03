"""Intent classification and dispatch for WeCom messages.

All messages go through async LLM path (qodercli subprocess, result pushed
via WeCom API). No keyword matching — LLM handles everything.
"""
import datetime
import difflib
import glob
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
    "Every reply MUST start with '【<requirement name>】'; use 【通用】 "
    "for global/cross-requirement questions (总览、所有需求状态) and when "
    "unknown. "
    "Keep reply under 500 chars. "
    "Use only WeCom markdown: **bold**, > quote, "
    "<font color=\"info|comment|warning\">text</font>. "
    "No tables, code blocks, lists, or links.\n\n"
    "System actions: to trigger a backend action, append on its own line:\n"
    "__JSON_ACTION__ {\"action\": \"<action>\", \"requirement\": \"<name>\", ...}\n"
    "Actions: approve | spec_result(requirement+module) | history | "
    "gray_list | adjudicate(requirement+target+decision, "
    "decision=accept|reject, target=all or draft ids e.g. \"28 29\")\n"
    "Do NOT add __JSON_ACTION__ when no action is needed — EXCEPT: "
    "editing spec.md in this turn makes spec_result MANDATORY in this "
    "same reply.\n\n"
    "Gray list rule: when the user asks to view the gray list "
    "(e.g. '查看灰名单'), you MUST output __JSON_ACTION__ "
    "{\"action\": \"gray_list\", \"requirement\": \"<name>\"}. "
    "Do NOT read state.json and answer directly — "
    "the backend has the correct display logic. "
    "gray_drafts = the real gray list; "
    "review_issues = CODE_REVIEW historical findings, not gray list items.\n\n"
    "Workflow skills (read when relevant):\n"
    "- Requirement registration/PRD bootstrap → ~/.qoder/skills/requirement-register/SKILL.md\n"
    "- Spec editing workflow → ~/.qoder/skills/spec-session/SKILL.md\n"
    "\n"
    "Runtime mode: each WeCom message is one --print turn; the session "
    "resumes on the user's next reply, so a multi-turn interview works "
    "across messages. Never launch background agents (their results can't "
    "reach you in this one-shot turn) and never promise to continue or "
    "implement later. Do synchronous work now (Edit, Grep, ask ONE "
    "clarifying question); the user's reply carries the session forward.\n"
    "\n"
    "Change boundary: ANY code change — bugfix, refactor, feature, or "
    "requirement-level — goes through the spec-session flow: read "
    "~/.qoder/skills/spec-session/SKILL.md, run the grill-me interview "
    "one question at a time to sharpen the spec, edit spec.md yourself, "
    "then SCORE. Do NOT edit code directly, do NOT just tell the user to "
    "do it, do NOT promise to implement later.\n"
    "\n"
    "Spec edit guardrails (hard rules):\n"
    "- Before editing spec.md, list the concrete change points (which "
    "Requirement/Scenario/fields) and get the user's explicit confirmation "
    "for each one via grill-me. Never add whole new Requirement/Scenario "
    "blocks, copy content from other modules, or append anything the user "
    "did not confirm.\n"
    "- Never edit .loop/state.json, pending.json, or other loop runtime "
    "state files directly — they are write-protected.\n"
    "- After editing any spec.md you MUST append __JSON_ACTION__ "
    "spec_result (requirement + module) at the end of that SAME reply, "
    "before any other action. Never reply about a spec edit without the "
    "registration.\n"
    "\n"
    "User: __MESSAGE__\n"
)


_AUDIT_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hooks", "audit_hook.sh")

# change_id/module_name must be single path segments: blocks ".." and "/"
# from escaping the requirement root when building spec paths.
_MODULE_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")

# Audit-hook SPEC_SNAPSHOT files (<YYYYMMDDTHHMMSS>-<session-id>-<module>.md)
# mark that G edited a spec.md this session; used to re-drive G when it
# replies without a spec_result registration.
_SPEC_SNAP_DIR = os.path.expanduser("~/.qoder/loop_engine/spec-snapshots")
_SPEC_SNAP_RE = re.compile(
    r"^(\d{8}T\d{6})-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})-(.+)\.md$")
_SPEC_EDIT_WINDOW = 300  # seconds: covers one grill-me edit + reply round
_CORRECTION_MAX_ROUNDS = 2


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
    the user's own qodercli sessions are untouched. Edit/Write are logged
    (never blocked) so direct code edits by G leave an audit trail.
    """
    return json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash|Edit|Write",
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


# Global/cross-requirement intent: when no requirement name or module key
# is mentioned, these words mean the message asks about ALL requirements
# (总览/所有需求状态…) — stay in the global session instead of falling
# back to the most recently active requirement session.
_GLOBAL_INTENT_RE = re.compile(
    r"总览|汇总|概览|一览|全局|整体|总体|所有需求|全部需求|每个需求|各需求|"
    r"需求状态|模块状态|所有模块|全部模块"
)


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
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import scheduler
    lines = []
    for req_name, d in found:
        lines.append(scheduler._format_gray_draft(
            d, summary_max=None, prefix=f"「{req_name}」"))
        lines.append(f"→ 回复「接受 {d['id']}」或「拒绝 {d['id']}」裁决该条")
        lines.append("")
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


_ACCEPT_SYNONYMS = {"accept", "接受", "通过", "同意", "批准", "approve",
                    "agree", "agreed", "yes", "y", "ok"}
_REJECT_SYNONYMS = {"reject", "拒绝", "驳回", "否决", "不同意", "deny",
                    "refuse", "no", "n"}
_ALL_SYNONYMS = {"all", "全部", "全", "所有", "所有草稿"}


def _normalize_decision(value):
    """Canonicalize a single adjudication decision to 'accept'/'reject'.

    Returns None when the value is a mixed-spec string (contains '=',
    handled by the caller) or an unrecognized keyword, so callers can
    report the *decision* as the problem instead of the draft number.
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v or "=" in v:
        return None
    if v in _ACCEPT_SYNONYMS:
        return "accept"
    if v in _REJECT_SYNONYMS:
        return "reject"
    return None


def _parse_target_ids(target):
    """Split a target into int draft ids, tolerating comma/space/顿号.

    Returns None when any token is non-numeric so the caller can report
    'unrecognized draft number' accurately.
    """
    parts = [p for p in re.split(r"[,，、\s]+", str(target).strip()) if p]
    if not parts:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


def _execute_adjudicate(name, target, decision, registry, data_dir):
    """Adjudicate gray-list drafts: target is 'all' or one/more draft ids."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from state import StateManager
    from machine import resolve_gray_draft

    canonical = _normalize_decision(decision)
    if canonical:
        decision = canonical

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
        if str(target).strip().lower() in _ALL_SYNONYMS:
            ids = [d["id"] for d in pending]
        else:
            ids = _parse_target_ids(target)
            if ids is None:
                return f"无法识别的草稿编号：{target}"
        pairs = {i: decision for i in ids}
    elif target == "mixed":
        pairs = _parse_decision_pairs(decision)
        if not pairs:
            return (f"无法识别的混合裁决格式：{decision}"
                    f"（应为 1=accept,2=reject 形式）")
    else:
        return (f"无法识别的裁决指令：{decision}"
                f"（请用「接受/拒绝 <编号>」或「全部接受/全部拒绝」）")
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
    canonical = _normalize_decision(decision)
    if canonical:
        decision = canonical
    if decision not in ("accept", "reject"):
        # Mixed or non-uniform decision — check if all pending drafts
        # belong to a single requirement. If so, delegate to the per-req
        # handler which supports mixed (accept some, reject others).
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from state import StateManager
        pending_reqs = []
        for req in registry:
            root = req.get("root", "")
            if not root:
                continue
            sm = StateManager(root)
            try:
                st = sm.load()
            except Exception:
                continue
            if any(d.get("status") == "pending" for d in st.get("gray_drafts", [])):
                pending_reqs.append(req.get("name", "?"))
        if len(pending_reqs) == 1:
            return _execute_adjudicate(
                pending_reqs[0], "mixed", decision, registry, data_dir)
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
        if str(target).strip().lower() in _ALL_SYNONYMS:
            ids = [d["id"] for d in pending]
        else:
            ids = _parse_target_ids(target) or []
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
    """Fast reject approve when no module status in the requirement's
    state permits APPROVE (per STATUS_TABLE entries), avoiding a full
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
    # poll 已检测到自动执行条目（如 NEEDS_REFINEMENT 模块 spec 完善后
    # 触发 SPEC_CHANGED）时放行：state.json 的状态要等 run 持锁后才转换
    import scheduler as _sched
    entry = _sched._find_entry(_sched.load_pending(), name)
    if entry and _sched._entry_auto_exec(entry):
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
        if not _MODULE_SEGMENT_RE.fullmatch(change_id) or \
                not _MODULE_SEGMENT_RE.fullmatch(module_name):
            return (f"非法模块 key：{module_key}（change_id/module_name "
                    "只允许字母、数字、点、横线、下划线）")
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
            project_roots=[project_root] if project_root else ["."],
            spec_hash=new_hash, spec_norm_hash=new_norm_hash)
        old_hash = None
    else:
        old_hash = st["modules"][module_key].get("spec_hash")
        if old_hash == new_hash:
            if st["modules"][module_key].get("status") == PARTIAL:
                return (f"{module_key} 已登记（spec hash {new_hash[:8]}），"
                        f"等待『批准执行 {name}』触发实现")
            if st["modules"][module_key].get("status") != DRAFT:
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
    # Change-size summary from the latest pre-edit snapshot (taken by the
    # audit hook before each Edit) so the user sees how big the change is
    # before approving execution.
    summary = ""
    snap_dir = os.path.join(data_dir, "spec-snapshots")
    try:
        snaps = sorted(
            glob.glob(os.path.join(snap_dir, f"*-{module_name}.md")),
            key=os.path.getmtime)
    except OSError:
        snaps = []
    if snaps:
        try:
            old_text = open(snaps[-1], encoding="utf-8").read()
            new_text = open(spec_path, encoding="utf-8").read()
            lines = difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(), lineterm="")
            added = sum(1 for l in lines
                        if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in lines
                          if l.startswith("-") and not l.startswith("---"))
            summary = f"变更规模: +{added} 行 / -{removed} 行\n"
        except OSError:
            pass
    # Refresh pending.json so approve works immediately
    import scheduler as _sched
    _sched.poll()
    bind_hint = ""
    if module_key in st["modules"]:
        _roots = spec_utils.coerce_roots(
            st["modules"][module_key].get("project_roots",
                                          st["modules"][module_key].get("project_root")))
        if _roots == ["."]:
            bind_hint = ("新模块尚未绑定项目，请先确认所属项目"
                         "（set-project-root 支持一次绑定多个仓库）\n")
    return (f"spec 变更已登记：{module_key}\n"
            f"{summary}"
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


_APPROVE_INTENT_RE = re.compile(r"批准|同意执行|可以执行|开始实现|执行吧|批了")
_APPROVE_NEGATION_RE = re.compile(r"不批准|不要批准|别批准|不同意|取消批准")


def _user_intends_approve(message):
    """approve 必须由用户本人的批准意图触发，防止 G 自行批准自己的登记。

    G 的 LLM 回复可以携带 __JSON_ACTION__ approve；若最近一条用户消息
    没有『批准』等确认词（或明确否定），说明是 G 自作主张，拒绝并请
    用户本人确认。
    """
    if not message:
        return False
    if _APPROVE_NEGATION_RE.search(message):
        return False
    return bool(_APPROVE_INTENT_RE.search(message))


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


# Recent-activity routing fallback: when a reply lacks any requirement
# keyword (typical for short grill-me answers), route to the requirement
# this user most recently talked to. In-memory only — resets on server
# restart, but the first message after restart carries the requirement
# name (users are told to), so deterministic routing re-seeds it.
# ponytail: 全进程内存 dict，不做磁盘持久化；重启后靠首条带名消息重建
_RECENT_WINDOW = 1800  # 30 min covers a grill-me round-trip
_recent_activity = {}  # (user_id, requirement) -> last-active timestamp
_recent_guard = threading.Lock()


def _touch_recent(user_id, requirement):
    if not requirement:
        return
    with _recent_guard:
        _recent_activity[(user_id, requirement)] = time.time()


def _recent_requirement(user_id):
    now = time.time()
    with _recent_guard:
        candidates = [
            (ts, req) for (uid, req), ts in _recent_activity.items()
            if uid == user_id and now - ts < _RECENT_WINDOW
        ]
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _system_state_snapshot(registry):
    """Compact real-time state across requirements: PARTIAL modules and
    pending approvals. Injected into every G prompt so answers about the
    current state come from a shared fact layer, not session memory — a
    spec edit made in one session is visible to every other session."""
    lines = []
    for req in registry:
        root = req.get("root", "")
        name = req.get("name")
        if not root or not name:
            continue
        try:
            with open(os.path.join(root, ".loop", "state.json")) as f:
                st = json.load(f)
        except (OSError, ValueError):
            continue
        mods = sorted(f"{key}:{m.get('status')}"
                      for key, m in st.get("modules", {}).items()
                      if m.get("status") == "PARTIAL")
        if mods:
            lines.append(f"- {name}：{', '.join(mods)}")
    import scheduler as _sched
    try:
        with open(_sched.PENDING_PATH) as f:
            pend = json.load(f)
        for p in pend.get("pending", []):
            state = "已批准" if p.get("approved") else "待批准"
            keys = ", ".join(m.get("key", "?") for m in p.get("modules", []))
            lines.append(f"- 待办 {p.get('requirement')}：{state}（触发 "
                         f"{p.get('trigger')}，模块 {keys}）")
    except (OSError, ValueError):
        pass
    if not lines:
        return ("\n\n【当前系统状态】所有需求模块已同步，无待办变更。回答"
                "『当前变更/状态』类问题时直接据此作答，不要用 git 提交历史推测。")
    return ("\n\n【当前系统状态】（server 从 .loop/state.json 与 pending.json "
            "实时生成，所有会话一致；回答『当前变更/状态』类问题以此为准，"
            "不要用 git 提交历史推测）\n" + "\n".join(lines))


def _recent_spec_snapshots(session_id, now=None):
    """Modules this session edited within the window, per audit-hook
    SPEC_SNAPSHOT files. Returns {module_name: mtime}; empty when none."""
    if not session_id:
        return {}
    now = time.time() if now is None else now
    edited = {}
    try:
        names = os.listdir(_SPEC_SNAP_DIR)
    except OSError:
        return edited
    for name in names:
        m = _SPEC_SNAP_RE.match(name)
        if not m or m.group(2) != session_id:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y%m%dT%H%M%S"))
        except ValueError:
            continue
        if now - ts <= _SPEC_EDIT_WINDOW:
            edited.setdefault(m.group(3), ts)
    return edited


def _json_action_payloads(reply):
    """All __JSON_ACTION__ payloads in a reply, in order; malformed blocks
    are skipped. G may emit one block per module, so dispatch must not
    stop at the first match."""
    payloads = []
    for m in _JSON_ACTION_RE.finditer(reply):
        try:
            payloads.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return payloads


def _unregistered_edits(session_id, reply):
    """Modules this session edited within the window but not covered by a
    spec_result block in reply. Drives the correction loop."""
    edited = set(_recent_spec_snapshots(session_id))
    registered = {p.get("module", "").rsplit("/", 1)[-1]
                  for p in _json_action_payloads(reply)
                  if p.get("action") == "spec_result"}
    return edited - registered


def _spec_correction_prompt(session_id):
    """Prompt re-driving G to append a spec_result registration when it
    edited spec.md but replied without one. None when nothing to register."""
    modules = sorted(_recent_spec_snapshots(session_id))
    if not modules:
        return None
    return (
        "[系统纠正] 你刚才编辑了 spec：" + "、".join(modules) +
        "，但回复中没有 __JSON_ACTION__ spec_result 登记。硬性规则："
        "编辑 spec.md 后必须在回复末尾追加 "
        "__JSON_ACTION__ {\"action\":\"spec_result\",\"requirement\":"
        "\"<需求名>\",\"module\":\"<change_id>/<module_name>\"} 完成登记"
        "（服务器会备份旧 spec、置 PARTIAL、刷新 pending），登记后由用户"
        "『批准执行』触发实现，不要直接 approve。请补充 spec_result 登记"
        "动作后回复。"
    )


def _run_llm_turn(qodercli_path, session_flag, session_id, model, settings,
                  prompt):
    """One qodercli --print turn with startup-noise cleanup. Returns the
    LLM reply text ('' when qodercli produced no usable stdout)."""
    r = subprocess.run(
        [qodercli_path, "--print", session_flag, session_id, "--model", model,
         "--dangerously-skip-permissions", "--settings", settings],
        input=prompt, capture_output=True, text=True, timeout=900)
    lines = (r.stdout or "").splitlines()
    while lines and lines[0].strip().startswith(_LLM_STDOUT_NOISE):
        lines.pop(0)
    return "\n".join(lines).strip()


def _llm_dispatch(message, registry, data_dir, user_id):
    """Background LLM direct response with per-user/per-requirement session."""
    qodercli_path = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")
    model = _get_model()
    requirement = _detect_requirement(message, registry)
    if not requirement and not _GLOBAL_INTENT_RE.search(message):
        # no keyword hit — try the most recently active requirement session
        # first (covers short grill-me answers), then the LLM classifier.
        # Global-intent questions (总览/所有需求状态…) skip the fallback and
        # stay in the global session so the reply is prefixed 【通用】.
        requirement = _recent_requirement(user_id) \
            or _classify_requirement(message, registry)
    _touch_recent(user_id, requirement)
    session_id, is_new = _get_session_id(user_id, requirement or "global")
    prompt = _LLM_SYSTEM_PROMPT.replace("__MESSAGE__", message, 1)
    # Shared fact layer: real-time state injected into every session so
    # answers never depend on which session the message landed in.
    prompt += _system_state_snapshot(registry)
    # Tell G about background files so it can read them when needed, instead
    # of relying on static injection that would waste tokens on every message.
    if requirement:
        req_entry = next((r for r in registry if r.get("name") == requirement), None)
        if req_entry:
            root = req_entry.get("root", "")
            if root:
                prompt += (
                    f"\n\n本需求的相关背景文件路径，需要时可自行读取：\n"
                    f"- {root}/.loop/state.json —— 模块状态、spec 哈希、灰名单草稿、SCORE 评分明细（评分不足时含具体缺口原因）\n"
                    f"- {root}/.loop/context.json —— 执行上下文（仅在运行中时存在）\n"
                    f"- ~/.qoder/loop_engine/requirements.json —— 全局需求注册表"
                )
    # first message creates session, subsequent messages resume it
    session_flag = "--session-id" if is_new else "--resume"
    settings = _audit_settings()
    with _llm_lock(requirement or "global"):
        try:
            reply = _run_llm_turn(qodercli_path, session_flag, session_id,
                                  model, settings, prompt)
            # resume failed (session lost, e.g. after server restart) → create fresh
            if not reply and not is_new:
                logger.info("[wecom] session %s not found, creating new",
                            session_id)
                reply = _run_llm_turn(qodercli_path, "--session-id", session_id,
                                      model, settings, prompt)
        except Exception as e:
            logger.error("[wecom] LLM dispatch error: %s", e)
            return f"处理失败：{e}"
    if not reply:
        return "无响应，请稍后再试。"
    # Correction loop: G edited spec.md this round (audit-hook snapshot
    # within the window) but its reply did not register every edited module.
    # Re-drive the same session so it appends __JSON_ACTION__ spec_result
    # before we act on its reply — approve must not run on unregistered
    # spec changes. Give up after a few rounds so a stubborn G can't hang
    # the reply; its last action then goes through the normal dispatch.
    rounds = 0
    missing = _unregistered_edits(session_id, reply)
    while rounds < _CORRECTION_MAX_ROUNDS and missing:
        correction = _spec_correction_prompt(session_id)
        if not correction:
            break
        logger.info("[wecom] correction round %d for session %s",
                    rounds + 1, session_id)
        approved_before = [p for p in _json_action_payloads(reply)
                           if p.get("action") == "approve"]
        corrected = _run_llm_turn(qodercli_path, "--resume", session_id,
                                  model, settings, correction)
        if not corrected:
            break
        # G re-focusing on registration may drop the approve it already
        # declared — carry it back so "register first, approve after"
        # completes in this same dispatch and the user's approval is not
        # swallowed by the correction round.
        if approved_before and not any(
                p.get("action") == "approve"
                for p in _json_action_payloads(corrected)):
            keep = "\n\n".join(
                f"__JSON_ACTION__ {json.dumps(p, ensure_ascii=False)}"
                for p in approved_before)
            corrected = corrected.rstrip() + "\n\n" + keep
        reply = corrected
        rounds += 1
        missing = _unregistered_edits(session_id, reply)
    return _process_llm_reply(reply, message, requirement, registry,
                              data_dir, user_id)


def _process_llm_reply(reply, message, requirement, registry, data_dir,
                       user_id):
    """Parse and execute a G reply: __JSON_ACTION__ block wins over legacy
    prefixes; otherwise plain text passes through. Returns user-facing text."""
    if requirement and not reply.startswith(("__", "【")):
        reply = f"【{requirement}】\n{reply}"
    payloads = _json_action_payloads(reply)
    if payloads:
        body = _JSON_ACTION_RE.sub("", reply).strip()
        results = []
        for payload in payloads:
            if payload.get("action") == "approve" and not _user_intends_approve(message):
                name = payload.get("requirement") or ""
                results.append(
                    f"无法自动批准：本条对话中未检测到你的『批准执行』确认。"
                    f"如需执行，请本人回复：批准执行 {name}")
            else:
                results.append(_dispatch_json_action(payload, registry,
                                                     data_dir, user_id))
        result = "\n\n".join(r for r in results if r)
        if not body:
            return result
        # G 正文（含 spec 变更披露）与动作结果拼接，避免动作执行吞掉正文
        return f"{body}\n\n{result}"
    # All actions now go through __JSON_ACTION__ only
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
