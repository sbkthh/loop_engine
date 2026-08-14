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
    "loop_engine is a spec-driven development loop management system. "
    "It manages requirements through a state machine: "
    "SCORE → CLASSIFY_CHANGE → MAKER_STEP0 → STEP1_RED → STEP2_GREEN → CHECKER → "
    "MAKER_FIX(optional) → CODE_REVIEW → CODE_REVIEW_FIX(optional) → SYNCED.\n\n"
    "Available CLI commands:\n"
    "- loop_engine requirement-list — list all registered requirements\n"
    "- loop_engine status --root <path> — module state summary\n"
    "- loop_engine next --root <path> — route next step, output directives\n"
    "- loop_engine commit --root <path> — submit result, advance state machine\n"
    "- loop_engine init --root <path> — initialize .loop/state.json\n"
    "- loop_engine poll — detect pending changes\n"
    "- loop_engine pending — view pending work list\n"
    "- loop_engine approve <name> — approve a requirement for auto-execution\n"
    "- loop_engine reset --root <path> <module> — reset module to DRAFT\n"
    "- loop_engine set-status --root <path> <module> <status> — manual status\n"
    "- loop_engine requirement-add <name> <root> --prd <doc> — register from PRD\n"
    "- loop_engine self-check — verify system integrity\n\n"
    "Core concepts:\n"
    "- Requirement: a business goal (e.g. 'strategic stockup upgrade')\n"
    "- Module: one spec file + corresponding code, smallest orchestration unit\n"
    "- State machine: DRAFT → SCORE → MAKER → CHECKER → CODE_REVIEW → SYNCED\n"
    "- Each requirement has its own .loop/state.json, isolated by --root\n\n"
    "WeCom bot commands:\n"
    "- '查状态' — check all requirement statuses (instant, sync reply)\n"
    "- '批准执行' — approve pending auto-executions (instant, sync reply)\n"
    "- Any other question — LLM processes and pushes result via API (async)\n\n"
    "When the user is clearly approving/confirming execution of a requirement "
    "(e.g. '批准执行', '同意执行 cross-dock', 'approve'), your reply MUST start "
    "with exactly '__APPROVE__ <requirement name>' on the first line, then you "
    "may add a short confirmation. Do NOT run any commands — the prefix "
    "triggers the real approval automatically.\n\n"
    "When the user asks about execution history (e.g. '最近执行情况', "
    "'执行历史'), your reply MUST start with '__HISTORY__ <requirement name>' "
    "(or '__HISTORY__ ALL' when no requirement is mentioned), then you may add "
    "a short intro. Do NOT run any commands — the prefix reads the history "
    "automatically.\n\n"
    "When the user asks to view gray-list drafts (e.g. '查看灰名单'), your "
    "reply MUST start with '__GRAY_LIST__ <requirement name>' (or "
    "'__GRAY_LIST__ ALL' when no requirement is mentioned), then you may add "
    "a short intro. Do NOT run any commands.\n\n"
    "When the user wants to adjudicate gray-list drafts (e.g. '接受 1', "
    "'拒绝 2 3', '接受 1，拒绝 2 3', '全部接受', '全部拒绝'), your reply MUST "
    "start with exactly '__ADJUDICATE__ <requirement name> <target> <decision>' "
    "(or '__ADJUDICATE__ ALL <target> <decision>' when no "
    "requirement is mentioned) on the first line, then you may add a short "
    "confirmation.\n"
    "<target> is 'all' for all pending, a comma-separated list of ids for "
    "a single decision, or 'mixed' for different decisions per draft. "
    "<decision> is 'accept'/'reject' for uniform decisions, or a "
    "comma-separated 'id=decision,...' pairs for mixed mode "
    "(e.g. '1=accept,2=reject,3=reject').\n"
    "Examples:\n"
    "  accept 1 2 → __ADJUDICATE__ req 1,2 accept\n"
    "  接受 1，拒绝 2 3 → __ADJUDICATE__ req mixed 1=accept,2=reject,3=reject\n"
    "  全部接受 → __ADJUDICATE__ req all accept\n"
    "Do NOT omit the requirement name — use ALL as the name when the "
    "user didn't say which requirement. If the user "
    "mentions only draft numbers, infer the requirement from the last "
    "gray-list context. Do NOT run any commands — the prefix adjudicates "
    "automatically.\n\n"
    "Requirement registration rules (user asks to register a new requirement "
    "from a PRD, e.g. '注册需求', '按 PRD 注册 xx', '初始化 xx 需求'):\n"
    "- Collect the required arguments first; ask the user one by one for "
    "anything missing: requirement name (business name), root directory "
    "(absolute path, created automatically), change id (kebab-case), and at "
    "least one project as 'name=<git repo path>' (comma-separated for "
    "multiple projects). The PRD path comes from the user's message — it is "
    "a local path on this machine, verify it exists\n"
    "- Then run 'loop_engine requirement-add <name> <root> --prd <prd_path> "
    "--change <change_id> --projects name=path[,name=path]'. The command "
    "creates git worktrees and writes .loop/prd_summary.json; report the "
    "result (root, change id, modules) and tell the user the next step is to "
    "say '按 PRD 生成 spec' to create the OpenSpec artifacts\n\n"
    "Spec management rules (creating or modifying any spec.md):\n"
    "- First read ~/.qoder/skills/spec-session/SKILL.md and follow its workflow\n"
    "- PRD bootstrap: when a requirement's root has .loop/prd_summary.json "
    "but the OpenSpec artifacts are missing (no "
    "openspec/changes/<change_id>/.openspec.yaml) and the user asks to "
    "generate or initialize specs from the PRD (e.g. '生成 spec', '按 PRD "
    "初始化 xx'), follow ~/.qoder/skills/prd-to-spec/SKILL.md: get the root "
    "via 'loop_engine requirement-list', then in the root run 'openspec new "
    "change <change_id>', then 'openspec status --change <change_id> --json' "
    "and 'openspec instructions <id> --change <change_id> --json' for each "
    "artifact, writing proposal/design/specs/tasks from the PRD content in "
    ".loop/prd_summary.json. Do NOT skip this even if the user just says "
    "'按 PRD 生成 spec'\n"
    "- ALWAYS run the grilling/grill-me skill first (every spec change, new "
    "or modification, PRD bootstrap included): interview the user one "
    "question at a time until shared understanding, then edit the spec\n"
    "- openspec-new-change/openspec-propose create a NEW change proposal only; "
    "they do NOT support appending to or modifying an existing change/spec — "
    "modify an existing spec by editing its spec.md in place\n"
    "- After editing a spec, your reply MUST start with exactly "
    "'__SPEC_RESULT__ <requirement name> <module key>' on the first line. "
    "IMPORTANT: <requirement name> and <module key> are TWO space-separated "
    "arguments — even when the requirement name equals the change_id prefix, "
    "write both, e.g. '__SPEC_RESULT__ cross-dock-v2-backend "
    "cross-dock-v2-backend/cross-dock-persistence'. Do NOT merge them into "
    "one token. Then add a short summary of what changed. Do NOT run "
    "'loop_engine next' or 'commit' — the prefix registers the change (hash "
    "update + backup) and the user then approves execution\n\n"
    "Manual execution rules (when driving a next/commit loop manually, e.g. "
    "user says '主动执行' or asks you to run the loop step by step):\n"
    "- ALWAYS run 'loop_engine manual-begin --root <path>' BEFORE the first "
    "'loop_engine next' — it acquires the same lock the scheduler uses, so a "
    "manual loop and a scheduled run never touch the same requirement "
    "concurrently. If manual-begin fails (lock held), do NOT proceed — tell "
    "the user the requirement is locked\n"
    "- Run 'loop_engine manual-end --root <path>' IMMEDIATELY when the loop "
    "finishes (machine reports IDLE/SYNCED) or the user stops it — never "
    "leave a manual loop without manual-end: it writes the run record and "
    "releases the lock\n\n"
    "Answer the user's question concisely in Chinese. "
    "Every reply MUST start with a line '【<requirement name>】' "
    "identifying the requirement being discussed (e.g. 【cross-dock-v2-backend】); "
    "when no requirement is identifiable, use 【通用】. "
    "If asked about specific project status, run the command. "
    "If you don't know, say so.\n\n"
    "Output format (mandatory):\n"
    "- Keep the reply under 500 Chinese characters\n"
    "- Use ONLY WeCom-supported markdown: # heading, **bold**, > quote, "
    "<font color=\"info|comment|warning\">text</font>\n"
    "- NO tables, NO code blocks (```), NO lists with - or *, NO links — "
    "plain text lines with line breaks instead\n"
    "- For multiple items, write them as separate lines like: '模块A: 状态'\n\n"
    "User: {message}\n"
)


_AUDIT_HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "hooks", "audit_hook.sh")


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
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from state import StateManager
    for req in registry:
        try:
            st = StateManager(req.get("root", "")).load()
        except Exception:
            continue
        for key in st.get("modules", {}):
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
            f"请回复 __SPEC_RESULT__ <需求名> <change_id>/<module_name>")
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
                f"请回复 __SPEC_RESULT__ <需求名> <change_id>/<module_name>")
    return (f"找不到模块 {module_key}：没有需求的状态机包含该模块，"
            f"请回复 __SPEC_RESULT__ <需求名> <change_id>/<module_name>")


def _execute_spec_result(name, module_key, registry, data_dir):
    """Register a G-edited spec change: verify hash changed, backup, PARTIAL.

    The spec file itself is edited by the assistant (audited by the hook);
    this function only controls the registration gate so the scheduler picks
    the change up only after the user approves execution.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from constants import SPEC_PATH_TEMPLATE, PARTIAL
    import spec_utils
    from state import StateManager

    req = next((r for r in registry if r.get("name") == name), None)
    if not req:
        available = ", ".join(r.get("name", "?") for r in registry) or "无"
        return f"没有找到需求：{name}（可用：{available}）"
    root = req["root"]
    sm = StateManager(root)
    st = sm.load()
    try:
        module_key = _resolve_module_key(st, module_key)
    except ValueError as e:
        return str(e)
    change_id, module_name = module_key.split("/", 1)
    spec_path = os.path.join(root, SPEC_PATH_TEMPLATE.format(
        change_id=change_id, module_name=module_name))
    if not os.path.exists(spec_path):
        return (f"找不到 spec 文件：{spec_path}。"
                f"请先编辑 spec 再输出 __SPEC_RESULT__。")
    new_hash = spec_utils.compute_spec_hash(spec_path)
    module = st["modules"][module_key]
    old_hash = module.get("spec_hash")
    if old_hash == new_hash:
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
    sm.set_module_field(st, module_key, "status", PARTIAL)
    sm.set_module_field(st, module_key, "maker_attempt", 0)
    sm.set_module_field(st, module_key, "review_fix_attempt", 0)
    sm.save(st)
    _audit_line(f"SPEC {name} {module_key} {old_hash}->{new_hash} "
                f"backup={backup_note}")
    # Refresh pending.json so approve works immediately
    import scheduler as _sched
    _sched.poll()
    return (f"spec 变更已登记：{module_key}\n"
            f"旧 hash: {old_hash[:8]}  新 hash: {new_hash[:8]}\n"
            f"备份: {backup_note}\n"
            f"请回复『批准执行 {name}』开始实现")


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


def _llm_dispatch(message, registry, data_dir, user_id):
    """Background LLM direct response with per-user/per-requirement session."""
    qodercli_path = shutil.which("qodercli") or os.path.expanduser("~/.local/bin/qodercli")
    model = _get_model()
    requirement = _detect_requirement(message, registry)
    if not requirement:
        # exact match failed — ask the LLM which requirement this is about
        requirement = _classify_requirement(message, registry)
    session_id, is_new = _get_session_id(user_id, requirement or "global")
    prompt = _LLM_SYSTEM_PROMPT.format(message=message)
    # first message creates session, subsequent messages resume it
    session_flag = "--session-id" if is_new else "--resume"
    settings = _audit_settings()
    try:
        r = subprocess.run(
            [qodercli_path, "--print", session_flag, session_id, "--model", model,
             "--dangerously-skip-permissions", "--settings", settings],
            input=prompt, capture_output=True, text=True,
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
                input=prompt, capture_output=True, text=True,
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
    if reply.startswith("__APPROVE__"):
        name = reply[len("__APPROVE__"):].strip().splitlines()[0].strip()
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
    push result via WeCom API in background).
    """
    return lambda: _llm_dispatch(message, registry, data_dir, user_id)
