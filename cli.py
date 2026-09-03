"""CLI: argparse dispatch for loop_engine commands."""

import argparse
import datetime
import json
import shutil
import sys
import os
import time

from state import StateManager
from machine import StateMachine
from constants import DRAFT, ALL_STATUSES
from setup import setup_requirement, init_from_prd, add_project_to_requirement
from spec_utils import coerce_roots
import report
import registry
import scheduler
import scope_audit
from wecom_server.wecom_api import md_bold


def _require_lock(root, cmd):
    """next/commit must run under a lock holder (scheduler run or
    manual-begin); otherwise G could drive the loop unaccounted."""
    if scheduler.is_locked(root):
        return
    print(f"{cmd} 被拒绝：{root} 无持有锁。请先回复「批准执行」由调度器接管，"
          f"或 manual-begin 手动接管。")
    sys.exit(1)


def cmd_next(args):
    _require_lock(args.root, "next")
    machine = StateMachine(args.root)
    result = machine.next()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_commit(args):
    _require_lock(args.root, "commit")
    machine = StateMachine(args.root)
    result = machine.commit()
    # count this step in an active manual session (no-op for scheduler runs)
    scheduler.manual_step(args.root)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_status(args):
    sm = StateManager(args.root)
    state = sm.load()
    modules = state.get("modules", {})
    current = state.get("current", {})
    print(f"Root: {sm.root_dir}")
    print(f"Modules: {len(modules)}")
    print(f"Current: {current.get('module', '-')} / {current.get('action', '-')}")
    print()
    if modules:
        print(f"{'Module':<45} {'Status':<20} {'Hash':<10}")
        print("-" * 75)
        for key, m in sorted(modules.items()):
            h = (m.get("spec_hash") or "")[:8]
            print(f"{key:<45} {m['status']:<20} {h}")
    drafts = state.get("gray_drafts", [])
    pending = [d for d in drafts if d.get("status") == "pending"]
    if pending:
        print(f"\nGray-list drafts: {len(pending)} pending")


def cmd_init(args):
    sm = StateManager(args.root)
    state = sm.init_state()
    print(f"Initialized: {sm.state_path}")
    print(f"Root: {sm.root_dir}")


def cmd_reset(args):
    sm = StateManager(args.root)
    state = sm.load()
    key = args.module
    if key in state["modules"]:
        m = state["modules"][key]
        m["status"] = DRAFT
        m["maker_attempt"] = 0
        m["review_fix_attempt"] = 0
        m["files_created"] = []
        m["files_modified"] = []
        if state["current"].get("module") == key:
            StateManager.clear_current(state)
        sm.save(state)
        report.write(state, args.root)
        print(f"Reset: {key}")
    else:
        print(f"Module not found: {key}")
        sys.exit(1)


def cmd_add_blocker(args):
    print("add-blocker: not implemented yet")


def cmd_set_status(args):
    if args.status not in ALL_STATUSES:
        print(f"Invalid status: {args.status}. Valid: {', '.join(ALL_STATUSES)}")
        sys.exit(1)
    sm = StateManager(args.root)
    state = sm.load()
    key = args.module
    if key not in state["modules"]:
        print(f"Module not found: {key}")
        sys.exit(1)
    old = state["modules"][key]["status"]
    state["modules"][key]["status"] = args.status
    if state["current"].get("module") == key:
        StateManager.clear_current(state)
    sm.save(state)
    report.write(state, args.root)
    print(f"{key}: {old} -> {args.status}")


def cmd_set_project_root(args):
    sm = StateManager(args.root)
    state = sm.load()
    key = args.module
    if key not in state["modules"]:
        print(f"Module not found: {key}")
        sys.exit(1)
    paths = [os.path.abspath(p) for p in args.paths]
    for p in paths:
        if not os.path.isdir(p):
            print(f"Directory does not exist: {p}")
            sys.exit(1)
    roots = coerce_roots(paths)
    mod = state["modules"][key]
    old = mod.get("project_roots") or [mod.get("project_root", ".")]
    mod["project_roots"] = roots
    mod["project_root"] = roots[0]
    sm.save(state)
    _record_module_to_project(args.root, key, roots)
    report.write(state, args.root)
    print(f"{key}: project_roots {old} -> {roots}")


def _record_module_to_project(root, module_key, paths):
    """Append every bound path's project name to module_to_project[<module>]
    (list-valued in multi-repo mode; scalar values are promoted on read).
    Paths that don't match any registered project are silently skipped.
    """
    if "/" not in module_key:
        return
    module_name = module_key.split("/", 1)[1]
    root = os.path.abspath(root)
    paths = [os.path.abspath(p) for p in (
        paths if isinstance(paths, (list, tuple)) else [paths])]
    data = registry.load()
    for r in data.get("requirements", []):
        if os.path.abspath(r.get("root", "")) != root:
            continue
        mapping = r.setdefault("module_to_project", {})
        existing = coerce_roots(mapping.get(module_name))
        # Reuse coerce_roots for scalar→list promotion; project names live
        # in the same 1-D slot as paths here (both are strings, dedup order).
        appended = list(existing) if existing != ["."] else []
        for path in paths:
            for p in r.get("projects", []):
                worktree = os.path.abspath(os.path.join(root, p.get("name", "")))
                source = os.path.abspath(p.get("source", "")) if p.get("source") else None
                if path == worktree or (source and path == source):
                    if p["name"] not in appended:
                        appended.append(p["name"])
                    break
        if not appended:
            return
        mapping[module_name] = appended
        registry.save(data)
        print(f"module_to_project: {module_name} -> {appended} (auto-recorded)")
        return


def cmd_resolve_draft(args):
    from machine import resolve_gray_draft
    ok, msg = resolve_gray_draft(StateManager(args.root), args.id,
                                 args.decision)
    print(msg)
    if not ok:
        sys.exit(1)


def _load_context_file(path):
    """Load environment context JSON file. Returns dict or None."""
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"Error: cannot load context file {path}: {e}")
        sys.exit(1)


def cmd_setup(args):
    projects = []
    if args.projects:
        for pair in args.projects.split(","):
            name, _, path = pair.partition("=")
            name = name.strip()
            path = os.path.expanduser(path.strip())
            if not name or not path:
                print(f"Invalid project spec: {pair}")
                sys.exit(1)
            projects.append((name, path))
    context = _load_context_file(args.context)
    result = setup_requirement(args.requirement_name, args.root, args.change,
                               projects, context=context)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if "error" in result:
        sys.exit(1)


def cmd_requirement_add(args):
    if args.prd:
        projects = []
        if args.projects:
            for pair in args.projects.split(","):
                name, _, path = pair.partition("=")
                name = name.strip()
                path = os.path.expanduser(path.strip())
                if not name or not path:
                    print(f"Invalid project spec: {pair}")
                    sys.exit(1)
                projects.append((name, path))
        if not projects:
            print("Error: --projects required with --prd (comma-separated name=path pairs)")
            sys.exit(1)
        if not args.change:
            print("Error: --change required with --prd (change ID for feature branch)")
            sys.exit(1)
        if not os.path.exists(args.prd):
            print(f"Error: PRD file not found: {args.prd}")
            sys.exit(1)
        modules = args.modules.split(",") if args.modules else None
        context = _load_context_file(args.context)
        result = init_from_prd(args.name, args.root_path, args.change, projects,
                               args.prd, modules=modules, context=context)
        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)
        print(f"Registered: {result['entry']['name']} -> {result['root']}")
        print(f"  Change:   {result['change_id']}")
        print(f"  Modules:  {', '.join(result['modules'])}")
        print(f"  PRD:      {result['prd']}")
        print()
        print("Next steps:")
        print("  1. In qodercli, run '/prd-to-spec' to generate OpenSpec artifacts from the PRD")
        print("  2. Then run '@spec-session' to start SCORE round-trip")
        return
    try:
        entry = registry.add_requirement(args.name, args.root_path,
                                         description=args.description)
        print(f"Registered: {entry['name']} -> {entry['root']}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_requirement_list(args):
    requirements = registry.list_requirements()
    if not requirements:
        print("No requirements registered.")
        return
    print(f"{'Name':<32} {'Root':<48} {'Desc':<40} {'Projects':<8} Registered")
    print("-" * 120)
    for r in requirements:
        projects = len(r.get("projects", []))
        desc = (r.get("description") or "")[:38]
        print(f"{r['name']:<32} {r['root']:<48} {desc:<40} {projects:<8} {r.get('registered_at', '-')}")


def cmd_requirement_remove(args):
    if registry.remove_requirement(args.name):
        print(f"Removed: {args.name}")
    else:
        print(f"Requirement not found: {args.name}")
        sys.exit(1)


def cmd_requirement_add_project(args):
    result = add_project_to_requirement(args.requirement, args.name,
                                        args.source, branch=args.branch)
    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    print(f"Added project: {result['project']}")
    print(f"  Worktree: {result['worktree']} (branch {result['branch']})")
    print(f"  Requirement: {result['entry']['name']} "
          f"({len(result['entry'].get('projects', []))} projects)")


def cmd_requirement_rename(args):
    try:
        if registry.rename_requirement(args.old_name, args.new_name):
            print(f"Renamed: {args.old_name} -> {args.new_name}")
        else:
            print(f"Requirement not found: {args.old_name}")
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_context_set(args):
    context = _load_context_file(args.file)
    sm = StateManager(args.root)
    os.makedirs(sm.loop_dir, exist_ok=True)
    ctx_path = os.path.join(sm.loop_dir, "context.json")
    with open(ctx_path, "w") as f:
        json.dump(context, f, indent=2, ensure_ascii=False)
    print(f"Context written: {ctx_path}")


def cmd_context_show(args):
    sm = StateManager(args.root)
    ctx_path = os.path.join(sm.loop_dir, "context.json")
    if not os.path.exists(ctx_path):
        print("No context.json (environment context not configured).")
        return
    with open(ctx_path) as f:
        print(f.read())


def cmd_poll(args):
    try:
        merged = scheduler.poll()
    except Exception as e:
        scheduler.notify_text(f"[调度失败] poll 异常：{md_bold(str(e))}")
        print(f"Poll failed: {e}")
        sys.exit(1)
    config = scheduler.load_config()
    forked = scheduler.dispatch(merged, config.get("max_concurrency", 2))
    config["last_run"] = datetime.datetime.now().isoformat()
    scheduler.save_config(config)
    if not merged:
        print("No pending work.")
    else:
        for e in merged:
            state = "approved" if e.get("approved") else "pending"
            print(f"{e['requirement']:<28} {e['trigger']:<18} "
                  f"{len(e['modules'])} module(s) [{state}]")
    if forked:
        print(f"Forked: {', '.join(forked)}")


def cmd_pending(args):
    entries = scheduler.load_pending().get("pending", [])
    if not entries:
        print("No pending work.")
        return
    print(f"{'Requirement':<32} {'Trigger':<18} {'Mods':<6} "
          f"{'Approved':<9} Detected")
    print("-" * 100)
    for e in entries:
        print(f"{e['requirement']:<32} {e['trigger']:<18} "
              f"{len(e['modules']):<6} {str(e.get('approved')):<9} "
              f"{e.get('detected_at', '-')}")
    print()
    print("Auto-executable (loop_engine approve <name>): SPEC_CHANGED, READY_PENDING")
    print("Report-only (spec session work): NEEDS_REFINEMENT, BLOCKED, DRAFT")


def cmd_approve(args):
    if not args.requirement and not args.all:
        print("Error: specify a requirement name or --all")
        sys.exit(1)
    try:
        count = scheduler.approve(name=args.requirement, all_=args.all)
        if args.all:
            print(f"Approved {count} pending requirement(s).")
        elif count:
            print(f"Approved: {args.requirement}")
        else:
            print(f"{args.requirement} already approved or no pending work.")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_run(args):
    result = scheduler.run_requirement(args.requirement)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if "error" in result:
        sys.exit(1)


def cmd_manual_hold(args):
    """(internal) Detached lock holder spawned by manual-begin."""
    if not scheduler.manual_hold(args.root):
        sys.exit(1)


def cmd_manual_begin(args):
    if not scheduler.manual_begin(args.root):
        print(f"Lock held for {args.root} — manual session not started")
        sys.exit(1)
    print(f"Manual session started for {args.root} (lock acquired)")


def cmd_manual_end(args):
    if not scheduler.manual_end(args.root):
        print(f"No manual session or lock replaced for {args.root}")
        sys.exit(1)
    print(f"Manual session ended for {args.root} (lock released)")


def session_clean(projects_dir, older_than_days, dry_run=False):
    """Delete qodercli session files (<project>/<uuid>.jsonl + sibling dir)
    older than N days, for every project dir under ~/.qoder/projects."""
    cutoff = time.time() - older_than_days * 86400
    removed = 0
    if not os.path.isdir(projects_dir):
        return removed
    for project in os.listdir(projects_dir):
        pdir = os.path.join(projects_dir, project)
        if not os.path.isdir(pdir):
            continue
        for entry in os.listdir(pdir):
            jsonl = os.path.join(pdir, entry)
            if not jsonl.endswith(".jsonl"):
                continue
            if os.path.getmtime(jsonl) >= cutoff:
                continue
            if not dry_run:
                os.unlink(jsonl)
                sibling = jsonl[:-6]  # strip ".jsonl" → session dir (attachments)
                if os.path.isdir(sibling):
                    shutil.rmtree(sibling)
            removed += 1
    return removed


def cmd_session_clean(args):
    projects_dir = os.path.expanduser("~/.qoder/projects")
    removed = session_clean(projects_dir, args.older_than, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}Removed {removed} session(s) older than "
          f"{args.older_than}d from {projects_dir}")


def cmd_schedule_status(args):
    cfg = scheduler.load_config()
    print(f"Max concurrency: {cfg['max_concurrency']}")
    print(f"Last run: {cfg.get('last_run') or '-'}")
    print(f"Config: {scheduler.CONFIG_PATH}")
    print(f"Log: {scheduler.LOG_PATH}")


def cmd_schedule_max_concurrency(args):
    try:
        cfg = scheduler.set_max_concurrency(args.n)
        print(f"Max concurrency: {cfg['max_concurrency']}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_self_install(args):
    """Install skills, bin shim, and data directory."""
    from __init__ import __version__
    engine_dir = os.path.dirname(os.path.abspath(__file__))
    # 1. Install skills
    _SKILLS = ("spec-session", "prd-to-spec", "grill-me",
               "requirement-register", "manual-loop")
    for name in _SKILLS:
        src = os.path.join(engine_dir, "skills", name, "SKILL.md")
        if not os.path.exists(src):
            print(f"Skill source not found (skipping): {src}")
            continue
        dst_dir = os.path.expanduser(f"~/.qoder/skills/{name}")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "SKILL.md")
        with open(src) as f:
            content = f.read()
        with open(dst, "w") as f:
            f.write(content)
        print(f"Skill: {dst}")
    # 2. Install bin shim
    bin_dir = os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    shim_path = os.path.join(bin_dir, "loop_engine")
    if not os.path.exists(shim_path):
        with open(shim_path, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f'exec python3 "{engine_dir}/__main__.py" "$@"\n')
        os.chmod(shim_path, 0o755)
        print(f"Shim: {shim_path}")
    else:
        print(f"Shim: {shim_path} (already exists)")
    # 3. Data directory
    data_dir = os.path.expanduser("~/.qoder/loop_engine")
    os.makedirs(data_dir, exist_ok=True)
    print(f"Data:  {data_dir}")
    # 4. Recommend pip install
    if "loop_engine" not in sys.modules:
        print(f"\nTip: run 'pip install -e {engine_dir}' to register the"
              " loop_engine entry point (requires pip).")
    print("Done.")


def cmd_self_check(args):
    checks = []
    # 1. CLI (shim or pip entry point)
    import subprocess
    shim_path = os.path.expanduser("~/.local/bin/loop_engine")
    pip_installed = os.path.exists(shim_path) or "loop_engine" in getattr(sys, "argv", [""])[0] or ""
    if pip_installed:
        checks.append(("CLI", True, f"available"))
    else:
        r = subprocess.run([sys.executable, "-m", "loop_engine", "--help"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            checks.append(("CLI", True, "python3 -m loop_engine works"))
        else:
            checks.append(("CLI", False, "not found"))
    # 2. Skills
    _SKILL_NAMES = ("spec-session", "prd-to-spec", "grill-me",
                    "requirement-register", "manual-loop")
    all_skills_ok = True
    for name in _SKILL_NAMES:
        skill_path = os.path.expanduser(f"~/.qoder/skills/{name}/SKILL.md")
        if os.path.exists(skill_path):
            checks.append((f"Skill-{name}", True, skill_path))
        else:
            checks.append((f"Skill-{name}", False, "not found — run 'loop_engine self-install'"))
            all_skills_ok = False
    # 3. Data dir
    data_dir = os.path.expanduser("~/.qoder/loop_engine")
    os.makedirs(data_dir, exist_ok=True)
    checks.append(("Data dir", True, data_dir))
    # 4. Registry
    reg_path = os.path.join(data_dir, "requirements.json")
    if os.path.exists(reg_path):
        import json
        with open(reg_path) as f:
            data = json.load(f)
        n = len(data.get("requirements", []))
        checks.append(("Registry", True, f"{n} requirement(s) registered"))
    else:
        checks.append(("Registry", True, "empty — register with 'loop_engine requirement-add'"))
    # 5. Tests
    test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
    if os.path.exists(test_dir):
        r = subprocess.run([sys.executable, "-m", "pytest", test_dir, "-q", "--tb=no"],
                           capture_output=True, text=True)
        line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "?"
        checks.append(("Tests", r.returncode == 0, line))
    else:
        checks.append(("Tests", False, "no tests/ dir"))

    print(f"{'Check':<12} {'Status':<8} Detail")
    print("-" * 60)
    all_ok = True
    for name, ok, detail in checks:
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"{name:<12} {status:<8} {detail}")
    print()
    if all_ok:
        print("System ready.")
    else:
        print("Some checks failed. Run 'loop_engine self-install' to fix.")
        sys.exit(1)


def cmd_wecom_start(args):
    from wecom_server.server import start
    data_dir = os.path.expanduser("~/.qoder/loop_engine")
    config_path = os.path.join(data_dir, "wecom.json")
    if not os.path.exists(config_path):
        print("WeCom not configured. Run 'loop_engine wecom config' first.")
        sys.exit(1)
    print(f"Starting WeCom webhook on port {args.port}...")
    if args.ngrok:
        import subprocess
        ngrok = subprocess.Popen(
            ["ngrok", "http", str(args.port), "--log=stdout"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"ngrok started (pid {ngrok.pid}).")
        print("Callback URL: https://<ngrok-id>.ngrok.io/callback")
    start(port=args.port)


def cmd_wecom_stop(args):
    import requests
    port = 5000
    try:
        requests.post(f"http://127.0.0.1:{port}/shutdown", timeout=2)
        print("WeCom webhook stopped.")
    except Exception:
        print("WeCom webhook is not running.")


def cmd_wecom_status(args):
    import socket
    port = args.port or 5000
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = s.connect_ex(("127.0.0.1", port))
    s.close()
    if result == 0:
        print(f"WeCom webhook is running on port {port}")
    else:
        print("WeCom webhook is not running.")


def cmd_wecom_config(args):
    import json
    data_dir = os.path.expanduser("~/.qoder/loop_engine")
    config_path = os.path.join(data_dir, "wecom.json")
    if args.show:
        if os.path.exists(config_path):
            with open(config_path) as f:
                print(f.read())
        else:
            print("Not configured.")
        return
    if args.set:
        parts = args.set.split("=", 1)
        if len(parts) != 2:
            print("Usage: --set key=value")
            sys.exit(1)
        config = {}
        if os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)
        config[parts[0]] = parts[1]
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"Set {parts[0]} = {parts[1]}")
        return
    print("Enter WeCom configuration (press Enter to skip):")
    config = {}
    for key in ["corp_id", "agent_id", "secret", "token", "encoding_aes_key"]:
        val = input(f"  {key}: ").strip()
        if val:
            config[key] = val
    if config:
        os.makedirs(data_dir, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"Saved to {config_path}")
    else:
        print("No values entered, config not saved.")


def cmd_feishu_start(args):
    from feishu_server.server import start
    data_dir = os.path.expanduser("~/.qoder/loop_engine")
    config_path = os.path.join(data_dir, "feishu.json")
    if not os.path.exists(config_path):
        print("Feishu not configured. Run 'loop_engine feishu config' first.")
        sys.exit(1)
    print("Starting Feishu bot (long connection)...")
    start()


def cmd_feishu_stop(args):
    import signal
    import subprocess
    r = subprocess.run(["pgrep", "-f", "loop_engine.*feishu start"],
                       capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p.strip()]
    if not pids:
        print("Feishu bot is not running.")
        return
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    print(f"Feishu bot stopped ({len(pids)} process(es)).")


def cmd_feishu_status(args):
    import subprocess
    r = subprocess.run(["pgrep", "-f", "loop_engine.*feishu start"],
                       capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p.strip()]
    if pids:
        print(f"Feishu bot is running (pid {pids[0]}).")
    else:
        print("Feishu bot is not running.")


def cmd_feishu_config(args):
    import json
    data_dir = os.path.expanduser("~/.qoder/loop_engine")
    config_path = os.path.join(data_dir, "feishu.json")
    if args.show:
        if os.path.exists(config_path):
            with open(config_path) as f:
                print(f.read())
        else:
            print("Not configured.")
        return
    if args.set:
        parts = args.set.split("=", 1)
        if len(parts) != 2:
            print("Usage: --set key=value")
            sys.exit(1)
        config = {}
        if os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)
        config[parts[0]] = parts[1]
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"Set {parts[0]} = {parts[1]}")
        return
    print("Enter Feishu configuration (press Enter to skip):")
    config = {}
    for key in ["app_id", "app_secret", "encrypt_key", "verification_token"]:
        val = input(f"  {key}: ").strip()
        if val:
            config[key] = val
    if config:
        os.makedirs(data_dir, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print(f"Saved to {config_path}")
    else:
        print("No values entered, config not saved.")


def cmd_scope_audit(args):
    scope_audit.cmd_scope_audit(args)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", "-r", default=".", help="Root directory")

    parser = argparse.ArgumentParser(
        prog="loop_engine",
        description="Loop Engine Python Orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("next", parents=[common], help="Get next directives")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("commit", parents=[common], help="Commit result and advance")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("status", parents=[common], help="Print state summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("init", parents=[common], help="Initialize .loop/ and state.json")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("reset", parents=[common], help="Reset a module to DRAFT")
    p.add_argument("module", help="Module key (change_id/module_name)")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("set-status", parents=[common],
                       help="Set module status (e.g. DRAFT to READY)")
    p.add_argument("module", help="Module key (change_id/module_name)")
    p.add_argument("status", choices=ALL_STATUSES,
                   help=f"One of: {', '.join(ALL_STATUSES)}")
    p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("set-project-root", parents=[common],
                       help="Bind module to one or more project worktrees")
    p.add_argument("module", help="Module key (change_id/module_name)")
    p.add_argument("paths", nargs="+",
                   help="Absolute path(s) of the project working copies")
    p.set_defaults(func=cmd_set_project_root)

    p = sub.add_parser("add-blocker", parents=[common])
    p.add_argument("module")
    p.add_argument("description")
    p.set_defaults(func=cmd_add_blocker)

    p = sub.add_parser("resolve-draft", parents=[common])
    p.add_argument("id", type=int)
    p.add_argument("decision", choices=["accept", "reject"])
    p.set_defaults(func=cmd_resolve_draft)

    p = sub.add_parser("setup", parents=[common],
                       help="Create requirement: worktrees, init, register")
    p.add_argument("--requirement-name", required=True,
                   help="Requirement name for registry")
    p.add_argument("--change", required=True,
                   help="Change ID for feature branch")
    p.add_argument("--projects", required=True,
                   help="Comma-separated name=path pairs")
    p.add_argument("--context", default=None,
                   help="Path to environment context JSON (databases/nacos, written to .loop/context.json)")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("requirement-add", parents=[common],
                       help="Register an existing requirement")
    p.add_argument("name", help="Requirement business name (e.g. cross-dock-v2, NOT the repo name)")
    p.add_argument("root_path", help="Requirement root directory")
    p.add_argument("--description", "-d", default=None,
                   help="Natural-language description of the requirement (for semantic matching)")
    p.add_argument("--prd", default=None,
                   help="Path to PRD markdown document — enables full setup (worktrees + spec init)")
    p.add_argument("--change", default=None,
                   help="Change ID for feature branch (required with --prd)")
    p.add_argument("--projects", default=None,
                   help="Comma-separated name=path pairs (required with --prd)")
    p.add_argument("--modules", default=None,
                   help="Comma-separated module names matching PRD ## headings (default: all sections)")
    p.add_argument("--context", default=None,
                   help="Path to environment context JSON (databases/nacos, written to .loop/context.json)")
    p.set_defaults(func=cmd_requirement_add)

    p = sub.add_parser("requirement-add-project", parents=[common],
                       help="Add a project (worktree) to an existing requirement")
    p.add_argument("requirement", help="Requirement name")
    p.add_argument("--name", required=True,
                   help="Project directory name (worktree name)")
    p.add_argument("--source", required=True,
                   help="Source git repo path")
    p.add_argument("--branch", default=None,
                   help="Branch name (default: first project's branch, "
                        "else feature/<name>)")
    p.set_defaults(func=cmd_requirement_add_project)

    p = sub.add_parser("requirement-rename", parents=[common],
                       help="Rename a registered requirement")
    p.add_argument("old_name", help="Current requirement name")
    p.add_argument("new_name", help="New requirement business name (NOT the repo name)")
    p.set_defaults(func=cmd_requirement_rename)

    p = sub.add_parser("requirement-list", parents=[common],
                       help="List all registered requirements")
    p.set_defaults(func=cmd_requirement_list)

    p = sub.add_parser("requirement-remove", parents=[common],
                       help="Unregister a requirement")
    p.add_argument("name", help="Requirement name to remove")
    p.set_defaults(func=cmd_requirement_remove)

    p = sub.add_parser("context", parents=[common],
                       help="Manage .loop/context.json (databases, nacos, gateways)")
    csub = p.add_subparsers(dest="context_command", required=True)
    pc_set = csub.add_parser("set", help="Write context from a JSON file")
    pc_set.add_argument("--file", required=True,
                        help="Path to environment context JSON file")
    pc_set.set_defaults(func=cmd_context_set)
    pc_show = csub.add_parser("show", help="Show current context")
    pc_show.set_defaults(func=cmd_context_show)

    p = sub.add_parser("poll", parents=[common],
                       help="Run one poll cycle (detect pending + dispatch approved)")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("pending", parents=[common],
                       help="View pending work list")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("approve", parents=[common],
                       help="Approve a pending requirement for auto-execution")
    p.add_argument("requirement", nargs="?", help="Requirement name")
    p.add_argument("--all", action="store_true",
                   help="Approve all auto-executable pending items")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("run", parents=[common],
                       help="(internal) Execute a requirement to completion")
    p.add_argument("requirement", help="Requirement name")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("manual-hold", parents=[common],
                       help="(internal) Hold the requirement lock (spawned by manual-begin)")
    p.set_defaults(func=cmd_manual_hold)

    p = sub.add_parser("manual-begin", parents=[common],
                       help="(internal) Acquire lock for a manual (G-driven) loop")
    p.add_argument("root", help="Requirement root dir")
    p.set_defaults(func=cmd_manual_begin)

    p = sub.add_parser("manual-end", parents=[common],
                       help="(internal) Release a manual-session lock")
    p.add_argument("root", help="Requirement root dir")
    p.set_defaults(func=cmd_manual_end)

    p = sub.add_parser("scope-audit", parents=[common],
                       help="Compare declared file changes against actual git status")
    p.set_defaults(func=cmd_scope_audit)

    p = sub.add_parser("session-clean", parents=[common],
                       help="Delete old qodercli session files (~/.qoder/projects)")
    p.add_argument("--older-than", type=int, default=30,
                   help="Delete sessions older than N days (default: 30)")
    p.add_argument("--dry-run", action="store_true",
                   help="Report without deleting")
    p.set_defaults(func=cmd_session_clean)

    p = sub.add_parser("schedule", parents=[common],
                       help="Scheduler config (max concurrency)")
    ssub = p.add_subparsers(dest="schedule_command", required=True)
    p_status = ssub.add_parser("status", help="Show schedule config")
    p_status.set_defaults(func=cmd_schedule_status)
    p_maxc = ssub.add_parser("max-concurrency", help="Set max parallel runs")
    p_maxc.add_argument("n", type=int)
    p_maxc.set_defaults(func=cmd_schedule_max_concurrency)

    p = sub.add_parser("wecom", help="WeCom integration (webhook server)")
    wsub = p.add_subparsers(dest="wecom_command", required=True)

    ws = wsub.add_parser("start", help="Start webhook server")
    ws.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    ws.add_argument("--ngrok", action="store_true", help="Auto-start ngrok")
    ws.set_defaults(func=cmd_wecom_start)

    ws = wsub.add_parser("stop", help="Stop webhook server")
    ws.set_defaults(func=cmd_wecom_stop)

    ws = wsub.add_parser("status", help="Check webhook server status")
    ws.add_argument("--port", type=int, default=5000)
    ws.set_defaults(func=cmd_wecom_status)

    ws = wsub.add_parser("config", help="Configure WeCom settings")
    ws.add_argument("--show", action="store_true", help="Show current config")
    ws.add_argument("--set", help="Set a config value (key=value)")
    ws.set_defaults(func=cmd_wecom_config)

    p = sub.add_parser("feishu", help="Feishu integration (long-connection bot)")
    fsub = p.add_subparsers(dest="feishu_command", required=True)

    fs = fsub.add_parser("start", help="Start bot (WebSocket long connection)")
    fs.set_defaults(func=cmd_feishu_start)

    fs = fsub.add_parser("stop", help="Stop bot")
    fs.set_defaults(func=cmd_feishu_stop)

    fs = fsub.add_parser("status", help="Check bot status")
    fs.set_defaults(func=cmd_feishu_status)

    fs = fsub.add_parser("config", help="Configure Feishu settings")
    fs.add_argument("--show", action="store_true", help="Show current config")
    fs.add_argument("--set", help="Set a config value (key=value)")
    fs.set_defaults(func=cmd_feishu_config)

    p = sub.add_parser("self-install", help="Install skill and verify setup")
    p.set_defaults(func=cmd_self_install)

    p = sub.add_parser("self-check", help="Verify system integrity")
    p.set_defaults(func=cmd_self_check)

    args = parser.parse_args()
    args.func(args)
