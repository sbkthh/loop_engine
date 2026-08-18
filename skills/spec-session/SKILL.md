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
- Every spec change — first creation OR later modification — starts with the `grilling`/`grill-me` skill: interview the user one question at a time until shared understanding, then edit spec.md
- `openspec-new-change`/`openspec-propose` create a NEW change proposal only (proposal/design/specs/tasks in one go); they do NOT support appending to or modifying an existing change/spec. Modify an existing spec by editing its spec.md in place
- Editing a SYNCED module's spec → warn it will transition to PARTIAL; score first; grep sibling specs for cross-module contract impact
- Pending triggers: SPEC_CHANGED / READY_PENDING = auto-executable (scheduler forks after user approval); NEEDS_REFINEMENT / BLOCKED / DRAFT = report-only, your job to resolve here
