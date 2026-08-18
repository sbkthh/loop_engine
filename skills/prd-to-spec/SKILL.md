---
name: prd-to-spec
description: Bootstrap OpenSpec artifacts from a PRD summary (.loop/prd_summary.json). Bridge between loop_engine's `requirement-add --prd` and openspec-propose — generates proposal, design, specs, and tasks from PRD content without asking the user what to build.
---

# PRD-to-Spec Bridge

Read `.loop/prd_summary.json` (written by `loop_engine requirement-add --prd`) and create all OpenSpec artifacts (proposal → design → specs → tasks) by injecting the PRD content instead of asking the user.

**Prerequisite**: `requirement-add --prd` must have been run first. The PRD summary at `.loop/prd_summary.json` contains the change_id, module breakdown, and PRD section content.

---

**Input**: Optional root directory (default: current working directory). The `.loop/prd_summary.json` must exist at the root.

**Steps**

1. **Locate and parse PRD summary**

   Look for `.loop/prd_summary.json`:
   - If a root argument is provided, check `<root>/.loop/prd_summary.json`
   - Otherwise, check current directory and parent directories
   - If not found, error with: "PRD summary not found. Run `loop_engine requirement-add --prd <path> --change <id> --projects ...` first."

   Parse the JSON — extract these fields:
   - `change_id`: used as the openspec change name
   - `requirement_name`: business name
   - `root`: absolute path to requirement root
   - `prd_path`: original PRD markdown path
   - `modules`: array of `{name, heading, prd_content, spec_path}`
   - `projects`: array of `{name, source}`

2. **Create the change directory**

   ```bash
   cd "<root>"
   openspec new change "<change_id>"
   ```

   This scaffolds `openspec/changes/<change_id>/` with `.openspec.yaml`.
   If the change already exists, show a message and continue.

3. **Get the artifact build order**

   ```bash
   openspec status --change "<change_id>" --json
   ```

   Parse the JSON to get:
   - `applyRequires`: artifact IDs needed before implementation
   - `artifacts`: list with `id`, `status`, `dependencies`

4. **Build a PRD context document for artifact generation**

   Before looping through artifacts, compose a concise context document from the PRD summary. This captures the high-level "why" and "what" that every artifact needs:

   - Requirement name and description (from `prd_path` and `requirement_name`)
   - List of modules with their headings and a 1-2 sentence summary of each section's content
   - Project information (which repos are involved)
   - The original PRD path for reference
   - Any cross-module dependencies evident from the PRD

   Use this context when generating each artifact. But do NOT write a separate context file — keep it in your working memory.

5. **Pre-read PRD for artifact content**

   Read the original PRD markdown file at `prd_path`. Its content is authoritative — the sections in `prd_summary.json` are parsed extracts, but the full PRD has richer context (introduction, background, goals, etc.).

6. **Create artifacts in sequence until apply-ready**

   Loop through artifacts in dependency order (artifacts with no pending dependencies first):

   a. **For each artifact that is `ready`**:
      - Get instructions:
        ```bash
        openspec instructions <artifact-id> --change "<change_id>" --json
        ```
      - The instructions include:
        - `context`: Project background (constraints — do NOT copy into output)
        - `rules`: Artifact-specific rules (constraints — do NOT copy into output)
        - `template`: The structure to use for your output file
        - `instruction`: Schema-specific guidance
        - `outputPath`: Where to write
        - `dependencies`: Completed artifacts to read for context
      - Read any completed dependency files for context
      - Create the artifact file using the PRD content as the source material:

        **Artifact-specific content strategy:**

        - **proposal** (`proposal.md`): Use the PRD's introduction/background/goals sections (content before the first `##` heading, plus high-level context from the full PRD). Focus on "what & why". List the modules from the PRD summary as the scope.

        - **design** (`design.md`): Use the PRD's architectural content, module descriptions, and any technical details. If the PRD doesn't have enough design detail, make reasonable default choices and note them. Focus on "how".

        - **specs** (`specs/**/*.md`): For each module in the PRD summary, use its `prd_content` (the section body) as the specification source material. Map each PRD section to a spec file. If the PRD mentions specific scenarios, behaviors, or acceptance criteria, translate them into Scenario format. If not, create basic scenarios from the module description.

        - **tasks** (`tasks.md`): Derive implementation tasks from the design and specs. Break into logical steps per module. Reference the project structure from `projects` in the summary.

      - Apply `context` and `rules` as constraints
      - Show brief progress: "Created <artifact-id>"

   b. **Continue until all `applyRequires` artifacts are complete**
      - After creating each artifact, re-run `openspec status --change "<change_id>" --json`
      - Check if every artifact ID in `applyRequires` has `status: "done"`

   c. **If PRD content is insufficient for an artifact**:
      - Use AskUserQuestion to fill the gap
      - Then continue

7. **Show final status**

   ```bash
   openspec status --change "<change_id>"
   ```

**Output**

After completing all artifacts, summarize:
- Change name: `<change_id>`
- Location: `<root>/openspec/changes/<change_id>/`
- Number of artifacts created
- Number of modules spec'd out (from PRD summary)
- What's ready: "All artifacts created from PRD! Ready for spec review and scoring."
- Prompt: "Run `@spec-session` to start the SCORE round-trip and refine the specs."

**Guidelines**

- The PRD content is the authoritative source — use it to fill artifact content, not the template's placeholder text
- For the `specs` artifact, create one spec file per module. The `outputPath` in instructions may include a glob like `specs/**/*.md` — that means you create individual files for each module
- Read dependency artifacts before creating dependent ones (e.g., read proposal.md before writing design.md)
- If a change with this ID already exists, the user may want to continue it — show status and ask
- Do NOT modify `.loop/prd_summary.json` or `.loop/state.json` — this skill only creates OpenSpec artifacts

**Guardrails**
- All artifact content must be derived from the PRD, not hallucinated
- If the PRD has only high-level goals (no technical detail), the design artifact will necessarily be high-level — acknowledge this in the summary
- Verify each artifact file exists after writing before proceeding
- Never overwrite an existing artifact without the user's consent