"""CLI: argparse dispatch for loop_engine commands."""

import argparse
import datetime
import json
import sys
import os

from state import StateManager
from machine import StateMachine
from constants import DRAFT, ALL_STATUSES
from setup import setup_requirement, init_from_prd
import report
import registry
import scheduler


def cmd_next(args):
    machine = StateMachine(args.root)
    result = machine.next()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_commit(args):
    machine = StateMachine(args.root)
    result = machine.commit()
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
    if drafts:
        print(f"\nGray-list drafts: {len(drafts)} pending")


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


def cmd_resolve_draft(args):
    print("resolve-draft: not implemented yet")


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
    result = setup_requirement(args.requirement_name, args.root, args.change, projects)
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
        result = init_from_prd(args.name, args.root_path, args.change, projects,
                               args.prd, modules=modules)
        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)
        print(f"Registered: {result['entry']['name']} -> {result['root']}")
        print(f"  Change:   {result['change_id']}")
        print(f"  Modules:  {', '.join(result['modules'])}")
        print(f"  PRD:      {result['prd']}")
        print()
        print("Next steps:")
        print("  1. In qodercli, run '@grilling' to refine requirements from the PRD")
        print("  2. Run '@openspec-propose' to formalize specs with proper artifacts")
        print("  3. Run 'loop_engine next --root <path>' to start SCORE round-trip")
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


def cmd_poll(args):
    merged = scheduler.poll()
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


def cmd_schedule_status(args):
    cfg = scheduler.load_config()
    print(f"Interval: {cfg['interval_minutes']} min")
    print(f"Max concurrency: {cfg['max_concurrency']}")
    print(f"Last run: {cfg.get('last_run') or '-'}")
    print(f"Config: {scheduler.CONFIG_PATH}")
    print(f"Log: {scheduler.LOG_PATH}")


def cmd_schedule_interval(args):
    try:
        cfg = scheduler.set_interval(args.minutes)
        print(f"Interval: {cfg['interval_minutes']} min")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_schedule_max_concurrency(args):
    try:
        cfg = scheduler.set_max_concurrency(args.n)
        print(f"Max concurrency: {cfg['max_concurrency']}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


_SPEC_SESSION_SKILL = """\
---
name: spec-session
description: Multi-requirement spec management session (Layer 1). Use when managing specs across all registered requirements — editing spec.md, SCORE round-trips, cross-module consistency, scheduler visibility. NOT for code implementation.
---

# Spec Session (Layer 1)

Role: multi-requirement spec manager. You manage specs for every requirement in `~/.qoder/loop_engine/requirements.json`. You do NOT implement code — execution is the scheduler's job (Layer 2).

## Session start

1. Read `~/.qoder/loop_engine/requirements.json` — all registered requirements
2. For each, read `<root>/.loop/state.json` — per-module status
3. Present a dashboard: requirement → modules/status, needs-attention items
4. Optionally `loop_engine pending` for the scheduler's pending list

## Commands (absolute --root, never cd)

- `loop_engine requirement-list` — all registered requirements
- `loop_engine status --root <req_root>` — module states
- `loop_engine next --root <req_root>` / `commit` — SCORE round-trip
- `loop_engine poll` / `pending` / `approve <name>` — scheduler visibility

## SCORE round-trip

1. `next` → SCORE directives (spec_path + criteria)
2. Read spec.md, score it, write `.loop/result.md` in `directives.output_format`
3. `commit` → ≥90 READY, <90 NEEDS_REFINEMENT
4. Refine spec.md, re-score until READY

## Rules

- Navigate with absolute paths from requirements.json roots; never change cwd
- Never edit state.json directly — state changes only via `next`/`commit`
- New requirement: grilling skill (requirements) → openspec-propose (scaffold) → SCORE round-trip
- Editing a SYNCED module's spec → warn it will transition to PARTIAL; score first; grep sibling specs for cross-module contract impact
- Pending triggers: SPEC_CHANGED / READY_PENDING = auto-executable (scheduler forks after user approval); NEEDS_REFINEMENT / BLOCKED / DRAFT = report-only, your job to resolve here
"""


def cmd_self_install(args):
    """Install the spec-session skill, bin shim, and data directory."""
    from __init__ import __version__
    engine_dir = os.path.dirname(os.path.abspath(__file__))
    # 1. Install the spec-session skill
    skill_dir = os.path.expanduser("~/.qoder/skills/spec-session")
    os.makedirs(skill_dir, exist_ok=True)
    skill_path = os.path.join(skill_dir, "SKILL.md")
    with open(skill_path, "w") as f:
        f.write(_SPEC_SESSION_SKILL)
    print(f"Skill: {skill_path}")
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
    # 2. Skill
    skill_path = os.path.expanduser("~/.qoder/skills/spec-session/SKILL.md")
    if os.path.exists(skill_path):
        checks.append(("Skill", True, skill_path))
    else:
        checks.append(("Skill", False, "not found — run 'loop_engine self-install'"))
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
    p.set_defaults(func=cmd_requirement_add)

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

    p = sub.add_parser("schedule", parents=[common],
                       help="Scheduler config (interval, max concurrency)")
    ssub = p.add_subparsers(dest="schedule_command", required=True)
    p_status = ssub.add_parser("status", help="Show schedule config")
    p_status.set_defaults(func=cmd_schedule_status)
    p_interval = ssub.add_parser("interval", help="Set polling interval")
    p_interval.add_argument("minutes", type=int)
    p_interval.set_defaults(func=cmd_schedule_interval)
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

    p = sub.add_parser("self-install", help="Install skill and verify setup")
    p.set_defaults(func=cmd_self_install)

    p = sub.add_parser("self-check", help="Verify system integrity")
    p.set_defaults(func=cmd_self_check)

    args = parser.parse_args()
    args.func(args)
