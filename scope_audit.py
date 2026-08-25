"""scope-audit: compare declared file changes against actual git status.

Report-only, non-blocking. Designed to be additive — zero changes to the
main loop flow (machine.py / scheduler.py / directives.py / state.py).
"""

import json
import os
import subprocess
import sys

from constants import STATE_FILE

try:
    from scheduler import notify_text as _wecom_notify
except ImportError:
    _wecom_notify = None

# Engine-internal artifacts filtered from git status output
_FILTER_DIRS = frozenset({".loop", ".codegraph", ".git"})


def load_state(root):
    """Read state.json from the requirement root directory."""
    path = os.path.join(root, STATE_FILE)
    if not os.path.exists(path):
        raise ValueError(f"state.json not found: {path}")
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid state.json: {e}")


def get_git_status(project_root):
    """Return (set of changed files relative to project_root, error string).

    Uses ``git status --porcelain`` for uncommitted changes. Error is None
    on success.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True, text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return set(), "git status timed out"
    except FileNotFoundError:
        return set(), "git not found (is this a git repository?)"

    if result.returncode != 0:
        return set(), (result.stderr or "").strip() or f"git exited {result.returncode}"

    changed = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            # rename: use the destination path
            path = path.split(" -> ", 1)[1]
        changed.add(os.path.normpath(path))
    return changed, None


def _should_filter(path):
    """True for engine-internal files that should not appear as 'unexplained'."""
    parts = path.split(os.sep)
    return any(p in _FILTER_DIRS for p in parts)


def audit_module(module, project_root):
    """Compare one module's declared changes against git status.

    Returns a dict with keys: declared, actual, unexplained, phantom, error.
    """
    declared = set()
    for f in module.get("files_created", []):
        declared.add(os.path.normpath(os.path.relpath(f, project_root)))
    for f in module.get("files_modified", []):
        declared.add(os.path.normpath(os.path.relpath(f, project_root)))

    actual, err = get_git_status(project_root)
    if err:
        return {"error": err}

    actual_filtered = {p for p in actual if not _should_filter(p)}
    unexplained = sorted(actual_filtered - declared)
    phantom = sorted(declared - actual_filtered)

    return {
        "declared": sorted(declared),
        "actual": sorted(actual_filtered),
        "unexplained": unexplained,
        "phantom": phantom,
        "clean": not unexplained and not phantom,
    }


def audit(state, root_dir):
    """Run scope audit for every module in state. Returns {module_key: result}."""
    results = {}
    for key, module in state.get("modules", {}).items():
        project_root = module.get("project_root") or root_dir
        if not project_root or not os.path.isdir(project_root):
            results[key] = {"error": f"project_root not found or not a directory: {project_root}"}
            continue
        result = audit_module(module, project_root)
        result["status"] = module.get("status", "?")
        results[key] = result
    return results


def format_report(results):
    """Human-readable multi-module audit report."""
    lines = []
    total_unexplained = 0
    modules_with_gaps = 0

    for key in sorted(results):
        r = results[key]
        header = f"Module: {key} ({r.get('status', '?')})"
        lines.append("")
        lines.append(header)
        lines.append("-" * len(header))

        if "error" in r:
            lines.append(f"  ERROR: {r['error']}")
            continue

        lines.append(f"  申报: {len(r['declared'])} files")
        for f in r["declared"]:
            lines.append(f"    {f}")

        done = "clean" if not r["actual"] else f"{len(r['actual'])} files"
        lines.append(f"  实际 git status: {done}")
        for f in r["actual"]:
            lines.append(f"     {f}")

        if r["unexplained"]:
            lines.append(f"  {chr(9888)} 未申报改动: {len(r['unexplained'])} files")
            for f in r["unexplained"]:
                lines.append(f"    ! {f}")
            total_unexplained += len(r["unexplained"])
            modules_with_gaps += 1

        if r["phantom"]:
            lines.append(f"  ~ 申报未改动: {len(r['phantom'])} files")
            for f in r["phantom"]:
                lines.append(f"    ~ {f}")

        if r.get("clean"):
            lines.append("  状态: {0} {1}".format(chr(10003), "一致"))

    lines.append("")
    lines.append("=" * 60)
    clean_count = len(results) - modules_with_gaps
    lines.append(f"Summary: {len(results)} modules, {clean_count} clean, "
                 f"{modules_with_gaps} with gaps")
    lines.append(f"Total unexplained changes: {total_unexplained}")
    return "\n".join(lines)


def format_wecom_summary(results):
    """Compact one-line-per-module summary for WeChat notification (fits <2048 bytes).

    Only called when there are unexplained changes — clean modules are not
    included; the goal is to surface what needs attention, not confirm what's fine.
    """
    lines = ["[scope-audit] \u26a0 发现未申报改动"]
    for key in sorted(results):
        r = results[key]
        if "error" in r:
            lines.append(f"  \u274c {key} \u2014 {r['error']}")
            continue
        if not r["unexplained"]:
            continue
        short = key.split("/", 1)[1] if "/" in key else key
        lines.append(f"  \u26a0 {short} \u2014 {len(r['unexplained'])} 未申报")
        for f in r["unexplained"]:
            candidate = f"    ! {f}"
            test_text = "\n".join(lines + [candidate, "", "运行 `loop_engine scope-audit --root .` 查看完整报告"])
            if len(test_text.encode("utf-8")) > 2000:
                lines.append("    \u2026 (截断, 运行 CLI 查看完整报告)")
                break
            lines.append(candidate)
    if len(lines) == 1:
        lines.append("  (无详细信息)")
    lines.append("")
    lines.append("运行 `loop_engine scope-audit --root .` 查看完整报告")
    return "\n".join(lines)


def notify_wecom(results, root):
    """Push audit summary via WeCom when there are gaps. Silently skipped otherwise."""
    has_gaps = any(r.get("unexplained") for r in results.values())
    if not has_gaps:
        return False
    if not _wecom_notify:
        return False
    try:
        text = format_wecom_summary(results)
        return _wecom_notify(text)
    except Exception:
        return False


def cmd_scope_audit(args):
    """CLI handler: load state, run audit, print report."""
    root = os.path.abspath(args.root)
    try:
        state = load_state(root)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    results = audit(state, root)
    print(format_report(results))
    notify_wecom(results, root)