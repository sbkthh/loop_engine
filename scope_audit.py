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


def audit_module(module, project_roots, root_dir=None):
    """Compare declared file changes against git status of every bound repo.

    `project_roots` may be a scalar (legacy) or list. Paths in the result
    are relative to `root_dir` so multi-repo output disambiguates by repo
    prefix; when `root_dir` is omitted it falls back to the single repo
    path (matching legacy single-repo behavior). Declared-but-not-changed
    becomes 'phantom'; changed-but-not-declared becomes 'unexplained'.
    """
    if isinstance(project_roots, str):
        project_roots = [project_roots]
    abs_roots = [os.path.abspath(r) for r in (project_roots or [])
                 if r and os.path.isdir(r)]
    if not abs_roots:
        return {"error": "no bound project_roots exist"}
    if root_dir is None:
        root_dir = abs_roots[0]
    root_dir = os.path.abspath(root_dir)
    declared = set()
    for f in (list(module.get("files_created", []))
              + list(module.get("files_modified", []))):
        declared.add(os.path.relpath(os.path.abspath(f), root_dir))

    actual = set()
    for repo in abs_roots:
        raw, err = get_git_status(repo)
        if err:
            return {"error": f"{repo}: {err}"}
        for p in raw:
            if _should_filter(p):
                continue
            actual.add(os.path.relpath(os.path.join(repo, p), root_dir))

    return {
        "declared": sorted(declared),
        "actual": sorted(actual),
        "unexplained": sorted(actual - declared),
        "phantom": sorted(declared - actual),
        "clean": not (actual - declared) and not (declared - actual),
    }


def audit(state, root_dir):
    """Run scope audit for every module in state. Returns {module_key: result}."""
    results = {}
    for key, module in state.get("modules", {}).items():
        roots = module.get("project_roots") or module.get("project_root") or root_dir
        roots = roots if isinstance(roots, list) else [roots]
        roots = [r if os.path.isabs(r) else
                 os.path.normpath(os.path.join(root_dir, r)) for r in roots]
        existing = [r for r in roots if r and os.path.isdir(r)]
        if not existing:
            results[key] = {"error": f"project_roots not found or not a directory: {roots}"}
            continue
        result = audit_module(module, existing, root_dir)
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