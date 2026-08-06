"""Phase 0: create worktrees, init requirement, register in registry."""

import json
import os
import subprocess

from constants import STATE_FILE
from state import StateManager
from spec_utils import parse_prd_sections, generate_spec_from_prd, compute_spec_hash
import registry
import report


def init_from_prd(name, root, change_id, projects, prd_path, modules=None):
    """Full PRD-driven setup: parse PRD → worktrees → init → generate specs → register."""
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True, mode=0o755)
    sections = parse_prd_sections(prd_path)
    if modules:
        section_map = dict(sections)
        sections = [(m, section_map.get(m, "")) for m in modules if m in section_map]

    for proj_name, proj_source in projects:
        target = os.path.join(root, proj_name)
        if os.path.exists(target):
            continue
        success, msg = create_worktree(proj_source, target, change_id)
        if not success:
            return {"error": f"Worktree failed for {proj_name}: {msg}"}

    init_requirement(root)
    sm = StateManager(root)
    state = sm.load()

    created = []
    for heading, content in sections:
        module_name = heading.lower().replace(" ", "-").replace("/", "-")
        spec_dir = os.path.join(root, "openspec", "changes", change_id, "specs", module_name)
        os.makedirs(spec_dir, exist_ok=True, mode=0o755)
        spec_path = os.path.join(spec_dir, "spec.md")
        if not os.path.exists(spec_path):
            spec_content = generate_spec_from_prd(heading, content)
            with open(spec_path, "w") as f:
                f.write(spec_content)
        key = StateManager.module_key(change_id, module_name)
        if key not in state["modules"]:
            spec_hash = compute_spec_hash(spec_path)
            StateManager.add_module(state, key, change_id, module_name, spec_hash=spec_hash)
        created.append(module_name)

    sm.save(state)
    projects_data = [{"name": n, "source": s} for n, s in projects]
    entry = registry.add_requirement(name, root, projects=projects_data,
                                     description=f"PRD: {prd_path}")
    report.write(state, root)
    return {
        "entry": entry,
        "root": root,
        "change_id": change_id,
        "modules": created,
        "prd": prd_path,
    }


def create_worktree(source, target, change_id):
    """Create a git worktree with a feature branch. Returns (success, msg)."""
    if not os.path.exists(os.path.join(source, ".git")):
        return False, "Not a git repository"
    branch = f"feature/{change_id}"
    try:
        subprocess.run(["git", "worktree", "add", "-b", branch, target, "HEAD"],
                       cwd=source, capture_output=True, text=True, check=True)
        return True, f"Worktree created at {target} on branch {branch}"
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def init_requirement(root, context=None):
    """Initialize .loop/state.json and openspec/ directory."""
    os.makedirs(os.path.join(root, "openspec", "changes"), exist_ok=True)
    sm = StateManager(root)
    state = sm.init_state()
    result = {
        "state_path": sm.state_path,
        "openspec_dir": os.path.join(root, "openspec"),
        "context_path": None,
    }
    if context:
        context_path = os.path.join(sm.loop_dir, "context.json")
        with open(context_path, "w") as f:
            json.dump(context, f, indent=2, ensure_ascii=False)
        result["context_path"] = context_path
    return result


def setup_requirement(name, root, change_id, projects):
    """Full setup: worktrees → init → register. Returns result dict."""
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    for proj_name, proj_source in projects:
        target = os.path.join(root, proj_name)
        if os.path.exists(target):
            continue
        success, msg = create_worktree(proj_source, target, change_id)
        if not success:
            return {"error": f"Worktree failed for {proj_name}: {msg}"}
    init_requirement(root)
    projects_data = [{"name": n, "source": s} for n, s in projects]
    try:
        entry = registry.add_requirement(name, root, projects=projects_data)
        return {"entry": entry, "root": root, "change_id": change_id}
    except ValueError as e:
        return {"error": str(e)}