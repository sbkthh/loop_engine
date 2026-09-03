"""Spec utilities: discovery, hashing, path derivation."""

import glob
import hashlib
import json
import os
import re

from constants import (
    SPEC_PATH_TEMPLATE,
    PLAN_PATH_TEMPLATE,
    REPORT_PATH_TEMPLATE,
    SPEC_GLOB,
)
import registry


def parse_prd_sections(prd_path):
    """Parse a PRD markdown file into (heading, content) sections by ## headings."""
    with open(prd_path, "r") as f:
        content = f.read()
    sections = []
    lines = content.split("\n")
    current_heading = None
    current_content = []
    for line in lines:
        if line.startswith("## "):
            if current_heading:
                sections.append((current_heading, "\n".join(current_content).strip()))
            current_heading = line[3:].strip()
            current_content = []
        else:
            current_content.append(line)
    if current_heading:
        sections.append((current_heading, "\n".join(current_content).strip()))
    return sections


def write_prd_summary(root, change_id, requirement_name, sections, prd_path, projects=None):
    """Write .loop/prd_summary.json from parsed PRD sections.

    The /prd-to-spec skill reads this file to bootstrap openspec-propose
    artifacts (proposal → design → specs → tasks) with the actual PRD
    content, instead of asking the user what to build.
    """
    modules = []
    for heading, content in sections:
        module_name = heading.lower().replace(" ", "-").replace("/", "-")
        spec_path = derive_spec_path(change_id, module_name, root)
        modules.append({
            "name": module_name,
            "heading": heading,
            "prd_content": content,
            "spec_path": spec_path,
        })
    summary = {
        "prd_path": os.path.abspath(prd_path),
        "change_id": change_id,
        "requirement_name": requirement_name,
        "root": os.path.abspath(root),
        "projects": projects or [],
        "modules": modules,
    }
    summary_path = os.path.join(root, ".loop", "prd_summary.json")
    os.makedirs(os.path.dirname(summary_path), exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return summary_path


def derive_spec_path(change_id, module_name, root="."):
    return os.path.join(root, SPEC_PATH_TEMPLATE.format(
        change_id=change_id, module_name=module_name))


def derive_plan_path(change_id, module_name, root="."):
    return os.path.join(root, PLAN_PATH_TEMPLATE.format(
        change_id=change_id, module_name=module_name))


def derive_report_path(change_id, root="."):
    return os.path.join(root, REPORT_PATH_TEMPLATE.format(change_id=change_id))


def coerce_roots(value):
    """Normalize a project_root(s) value to an ordered, deduped, non-empty list.

    Accepts None, a scalar string (legacy shape), or a list/tuple of strings.
    Blank / None / non-string entries collapse to '.'. Empty result becomes
    ['.'] so unbound state stays comparable via sentinel.
    """
    if value is None:
        return ["."]
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    seen = set()
    for v in items:
        v = v.strip() if isinstance(v, str) and v.strip() else "."
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out or ["."]


def compute_spec_hash(spec_path):
    if not os.path.exists(spec_path):
        return None
    with open(spec_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def normalize_spec(text):
    """Normalize spec text so comment/format-only edits hash identically.

    Strips HTML comments, normalizes newlines, trims trailing whitespace
    per line and surrounding blank lines. Conservative on purpose: prose
    reflow, heading reorder, table edits still change the normalized hash
    and keep the full loop.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]*<!--.*?-->[ \t]*\n?", "", text, flags=re.DOTALL)
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def compute_spec_norm_hash(spec_path):
    if not os.path.exists(spec_path):
        return None
    with open(spec_path, "r", encoding="utf-8") as f:
        return hashlib.md5(normalize_spec(f.read()).encode("utf-8")).hexdigest()


def compute_plan_hash(plan_path):
    if not os.path.exists(plan_path):
        return None
    with open(plan_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def discover_modules(root="."):
    pattern = os.path.join(root, SPEC_GLOB)
    results = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        parts = path.replace("\\", "/").split("/")
        try:
            ci = parts.index("changes") + 1
            si = parts.index("specs", ci) + 1
            change_id = parts[ci]
            module_name = parts[si]
        except (ValueError, IndexError):
            continue
        results.append((change_id, module_name, path))
    return results


def resolve_project_roots(root_dir, module_name):
    """Map a discovered module to every bound working copy (multi-repo M1).

    Reads registry module_to_project[<module>] which may be scalar (legacy)
    or list (multi-repo). Falls back to the module name itself when unmapped.
    Returns an ordered, deduped list of existing worktree/source paths; empty
    when no registry match.
    """
    root_dir = os.path.abspath(root_dir)
    for r in registry.list_requirements():
        if os.path.abspath(r.get("root", "")) != root_dir:
            continue
        raw = r.get("module_to_project", {}).get(module_name)
        names = coerce_roots(raw) if raw is not None else [module_name]
        if names == ["."]:
            names = [module_name]
        by_name = {p.get("name"): p for p in r.get("projects", [])}
        out, seen = [], set()
        for project_name in names:
            p = by_name.get(project_name)
            if p is None:
                continue
            worktree = os.path.join(root_dir, project_name)
            candidate = worktree if os.path.isdir(worktree) else (
                os.path.abspath(p["source"])
                if p.get("source") and os.path.isdir(p["source"]) else None)
            if candidate and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
        return out
    return []


def resolve_project_root(root_dir, module_name):
    """Thin scalar wrapper around resolve_project_roots (returns first path
    or None). Kept for callers not yet migrated to multi-repo; Commit 4
    runtime paths use the plural resolver directly.
    """
    roots = resolve_project_roots(root_dir, module_name)
    return roots[0] if roots else None


def read_test_command(project_root):
    agents_path = os.path.join(project_root, ".qoder", "AGENTS.md")
    cmd = "mvn test"
    if os.path.exists(agents_path):
        with open(agents_path) as f:
            content = f.read()
        m = re.search(r"mvn\s+(?:clean\s+)?test\b[^\n]*", content)
        if m:
            cmd = m.group(0).strip()
    # 铁律 (strict-development-workflow): 编译/测试前必须先 mvn clean。
    # 命令本身带上 clean，防止执行层直接照用裸 test_cmd 跳过清理。
    if not re.search(r"\bclean\b", cmd):
        cmd = cmd.replace("mvn", "mvn clean", 1)
    return cmd


def _with_module_scope(cmd, modules):
    if modules:
        cmd = f"{cmd} -pl {','.join(sorted(modules))} -am"
    return cmd


def read_checker_test_command(cmd, repo_root, files=()):
    """CHECKER-only incremental test command scoped to the modules of `files`.

    CHECKER never modifies code and GREEN already ran 'mvn clean test', so
    target/ is fresh — drop clean to save 20-30 min on zkh projects.
    `repo_root` must be the OWNING REPO path (not the requirement root), else
    the stripped first segment is the repo name and `-pl` is wrong.
    """
    cmd = re.sub(r"\bclean\s+", "", cmd)
    repo_root = os.path.abspath(repo_root).replace(os.sep, "/") + "/"
    modules = set()
    for path in files:
        path = path.replace(os.sep, "/")
        if path.startswith(repo_root):
            first = path[len(repo_root):].split("/", 1)[0]
            if first:
                modules.add(first)
    return _with_module_scope(cmd, modules)


_PLAN_SRC_RE = re.compile(r"([\w.-]+)/src/(?:main|test)/")


def read_maker_test_command(cmd, root, plan_path):
    """Scope the maker (RED/GREEN) test run to the modules the plan
    touches — the plan cites its sources as <module>/src/... paths and
    usually edits 1-2 modules, while a full-reactor 'mvn clean test'
    costs 20-30 min on zkh projects. Keeps clean (edits invalidate
    incremental builds). Falls back to the full command when the plan
    is missing or cites no recognizable module paths."""
    if not plan_path or " -pl" in cmd:
        return cmd
    full = plan_path if os.path.isabs(plan_path) else os.path.join(
        root, plan_path)
    if not os.path.exists(full):
        return cmd
    with open(full, encoding="utf-8") as f:
        text = f.read()
    modules = {m.group(1) for m in _PLAN_SRC_RE.finditer(text)}
    modules = {m for m in modules if os.path.isdir(os.path.join(root, m))}
    return _with_module_scope(cmd, modules)


def read_test_commands(project_roots):
    """Per-repo base test command (mvn clean test / read from .qoder/AGENTS.md).

    Returns {abs_repo_path: base_cmd}. Requirement root stays out — each
    worktree runs its own reactor; single-repo callers get a 1-element dict.
    """
    out = {}
    for repo in project_roots:
        abs_repo = os.path.abspath(repo)
        if abs_repo in out:
            continue
        out[abs_repo] = read_test_command(repo)
    return out


def _files_by_repo(repo_roots, files):
    """Bucket files under the longest-matching repo prefix. Files that don't
    match any repo are dropped (they can't be tested by any reactor)."""
    abs_roots = [os.path.abspath(r).replace(os.sep, "/").rstrip("/")
                 for r in repo_roots]
    buckets = {r: [] for r in abs_roots}
    for path in files:
        norm = os.path.abspath(path).replace(os.sep, "/")
        best = None
        for r in abs_roots:
            if norm.startswith(r + "/") and (best is None or len(r) > len(best)):
                best = r
        if best is not None:
            buckets[best].append(norm)
    return buckets


def read_checker_test_commands(cmd_by_repo, repo_roots, files):
    """Per-repo CHECKER scoped command. Bug α-aware: each repo strips paths
    relative to ITSELF so `-pl` yields maven module names, not repo names."""
    buckets = _files_by_repo(repo_roots, files)
    return {repo: read_checker_test_command(
        cmd_by_repo.get(repo, "mvn test"), repo, buckets[repo])
        for repo in buckets}


def read_maker_test_commands(cmd_by_repo, repo_roots, plan_path):
    """Per-repo MAKER scoped command. Plan paths are prefixed with the repo
    name (e.g. `kunhe-wms/inventory/src/...`); we scope to a repo's own
    sub-paths and skip modules the plan never cites for that repo."""
    out = {}
    for repo in repo_roots:
        abs_repo = os.path.abspath(repo)
        cmd = cmd_by_repo.get(abs_repo) or cmd_by_repo.get(repo, "mvn clean test")
        out[abs_repo] = read_maker_test_command(cmd, abs_repo, plan_path)
    return out


PLAN_EXISTING_MARKERS = ("已有", "无需变更")


def count_plan_existing_claims(plan_path):
    """Count plan lines carrying an '已有/无需变更' claim."""
    if not os.path.exists(plan_path):
        return 0
    count = 0
    with open(plan_path, "r", encoding="utf-8") as f:
        for line in f:
            if any(m in line for m in PLAN_EXISTING_MARKERS):
                count += 1
    return count
