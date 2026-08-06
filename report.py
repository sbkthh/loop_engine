"""ReportGenerator: build LOOP_REPORT.md from state.json."""

import os
import datetime

from constants import SYNCED
from spec_utils import derive_report_path


def generate(state, root_dir="."):
    lines = ["# LOOP_REPORT.md", ""]
    lines.append(f"> Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    modules = state.get("modules", {})
    if modules:
        lines.append("## Module Status")
        lines.append("")
        lines.append("| Module | Status | spec_hash | current_action | last_synced |")
        lines.append("|--------|--------|-----------|---------------|-------------|")
        current = state.get("current", {})
        for key, m in sorted(modules.items()):
            h = (m.get("spec_hash") or "")[:8]
            action = current.get("action") if current.get("module") == key else ""
            synced = (m.get("last_synced") or "-")[:10]
            lines.append(f"| {key} | {m['status']} | {h} | {action or '-'} | {synced} |")
        lines.append("")

    drafts = state.get("gray_drafts", [])
    if drafts:
        lines.append("## Gray-List Drafts")
        lines.append("")
        lines.append("| # | Module | Summary | Status |")
        lines.append("|---|--------|---------|--------|")
        for d in drafts:
            lines.append(f"| {d['id']} | {d.get('module', '-')} | {d.get('summary', '')} | {d.get('status', 'pending')} |")
        lines.append("")

    trail = state.get("audit_trail", [])
    if trail:
        lines.append("## Audit Trail")
        lines.append("")
        lines.append("| Date | Module | Change | Reason | Trigger |")
        lines.append("|------|--------|--------|--------|---------|")
        for a in trail[-30:]:
            lines.append(f"| {a.get('date', '-')} | {a.get('module', '-')} | {a.get('change', '-')} | {a.get('reason', '-')} | {a.get('trigger', '-')} |")
        lines.append("")

    trace = state.get("trace", [])
    if trace:
        lines.append("## Execution Trace")
        lines.append("")
        lines.append("| Time | Phase | Module | Output | Result |")
        lines.append("|------|-------|--------|--------|--------|")
        for t in trace[-20:]:
            out = (t.get("output", "") or "")[:60]
            lines.append(f"| {t.get('time', '-')} | {t.get('phase', '-')} | {t.get('module', '-')} | {out} | {t.get('result', '-')} |")
        lines.append("")

    return "\n".join(lines)


def write(state, root_dir="."):
    changes = set()
    for m in state.get("modules", {}).values():
        changes.add(m.get("change_id"))
    for change_id in changes:
        if not change_id:
            continue
        path = derive_report_path(change_id, root_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(generate(state, root_dir))
