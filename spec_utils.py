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


def compute_spec_hash(spec_path):
    if not os.path.exists(spec_path):
        return None
    with open(spec_path, "rb") as f:
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


def resolve_project_root(root_dir, module_name):
    """Map a discovered module to its working copy via the registry.

    projects[].name is the real project/repo name; module_to_project maps
    spec module names to those project names. Prefers the worktree
    (root/<project name>) when it exists, else the source repo path.
    Returns None when no registry project matches the module name.
    """
    root_dir = os.path.abspath(root_dir)
    for r in registry.list_requirements():
        if os.path.abspath(r.get("root", "")) != root_dir:
            continue
        project_name = r.get("module_to_project", {}).get(module_name) or module_name
        for p in r.get("projects", []):
            if p.get("name") != project_name:
                continue
            worktree = os.path.join(root_dir, project_name)
            if os.path.isdir(worktree):
                return worktree
            source = p.get("source")
            if source and os.path.isdir(source):
                return os.path.abspath(source)
    return None


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


def read_checker_test_command(cmd, root, files=()):
    """CHECKER-only incremental test command.

    CHECKER never modifies code and the previous step (GREEN) just ran a
    full 'mvn clean test', so target/ artifacts are fresh — a clean rebuild
    is pure waste (20-30 min on zkh projects). The clean-first rule exists
    because incremental builds after code edits are unreliable; no code
    changes happen between GREEN and CHECKER, so incremental is safe here.
    """
    cmd = re.sub(r"\bclean\s+", "", cmd)
    root = os.path.abspath(root).replace(os.sep, "/") + "/"
    modules = set()
    for path in files:
        path = path.replace(os.sep, "/")
        if path.startswith(root):
            first = path[len(root):].split("/", 1)[0]
            if first:
                modules.add(first)
    if modules:
        cmd = f"{cmd} -pl {','.join(sorted(modules))} -am"
    return cmd
