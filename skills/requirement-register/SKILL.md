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

1. Read ~/.qoder/skills/spec-session/SKILL.md and follow its workflow
2. PRD bootstrap (no OpenSpec artifacts yet, user says "生成 spec" / "按 PRD 初始化"):
   - Run `loop_engine requirement-list` to get the root
   - Run `openspec new change <change_id>` in the root
   - Run `openspec status --change <change_id> --json` and `openspec instructions <id> --change <change_id> --json` for each artifact
   - Write proposal/design/specs/tasks from `.loop/prd_summary.json`
3. After editing a spec, append __JSON_ACTION__ `{"action":"spec_result","requirement":"<name>","module":"<change_id>/<module_name>"}` with BOTH requirement and module fields
4. Always run the grilling/grill-me skill first (every spec change): interview the user one question at a time
5. `openspec-new-change` / `openspec-propose` create a NEW change only — edit existing specs by modifying spec.md in place