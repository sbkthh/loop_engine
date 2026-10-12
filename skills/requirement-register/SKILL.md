# Requirement Registration & Spec Management

When the user asks to register a new requirement from a PRD:

1. Collect required args (ask one at a time if missing):
   - requirement name (business name)
   - root directory (absolute path, created automatically)
   - change id (kebab-case)
   - at least one project: `name=<git_repo_path>` (comma-separated for multiple)

2. Run: `loop_engine requirement-add <name> <root> --prd <prd_path> --change <change_id> --projects name=path[,name=path]`
   - PRD path comes from the user's message — verify it exists locally
   - The command creates git worktrees and writes `.loop/prd_summary.json`

3. Report the result (root, change id, modules) and tell the user the next step is to say "按 PRD 生成 spec".

## Adding a Project

When the user says "给 XX 加项目" / "新增项目" / "补充项目":
1. Ask for project name and source git repo path if not provided
2. Run: `loop_engine requirement-add-project <name> --name <project> --source <path>`
3. Report the worktree path and branch

## Spec Management

When creating or modifying spec files:

1. Read `../spec-session/SKILL.md` relative to this SKILL.md's directory (not the process cwd) and follow its workflow
2. PRD bootstrap (no OpenSpec artifacts yet, user says "生成 spec" / "按 PRD 初始化"):
   - Run `loop_engine requirement-list` to get the root
   - Run `openspec new change <change_id>` in the root
   - Run `openspec status --change <change_id> --json` and `openspec instructions <id> --change <change_id> --json` for each artifact
   - Write proposal/design/specs/tasks from `.loop/prd_summary.json`
3. After editing a spec, append __JSON_ACTION__ `{"action":"spec_result","requirement":"<name>","module":"<change_id>/<module_name>"}` with BOTH requirement and module fields
4. Always run the grilling/grill-me skill first (every spec change): interview the user one question at a time
5. `openspec-new-change` / `openspec-propose` create a NEW change only — edit existing specs by modifying spec.md in place

## Module–Repo Binding

A module whose `project_roots` is `["."]` in `<root>/.loop/state.json` is
unbound — it cannot be implemented (no repo to edit, no test gate to run).
Do not let the user approve it.

After generating or registering specs (PRD bootstrap, new module, spec_result):
1. Read every module's `project_roots` from `<root>/.loop/state.json`
2. For each unbound module: confirm its owning project(s) with the user,
   one at a time, citing evidence (PRD + grep candidate worktrees for the
   interfaces/classes the module touches)
3. Bind: `loop_engine set-project-root --root <root> <change_id>/<module_name> <abs_path>...`
   - paths = project worktree directories under the requirement root
     (e.g. `<root>/kunhe-wms`); two or more paths bind a multi-repo module
   - the command auto-records `module_to_project` in the registry
4. Verify: re-read state.json — `project_roots` must no longer be `["."]`