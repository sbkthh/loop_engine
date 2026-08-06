"""Approve handler: approve pending requirements for auto-execution."""
import json
import os


def handle_approve(message, registry, pending_dir=None):
    if pending_dir is None:
        pending_dir = os.path.expanduser("~/.qoder/loop_engine")
    pending_path = os.path.join(pending_dir, "pending.json")
    if not os.path.exists(pending_path):
        return "当前没有待审批的需求。"
    with open(pending_path) as f:
        data = json.load(f)
    entries = data.get("pending", [])
    if not entries:
        return "当前没有待审批的需求。"
    msg_lower = message.lower().strip()
    # Try to match a specific requirement name
    matched = None
    for e in entries:
        if e["requirement"].lower() in msg_lower or msg_lower in e["requirement"].lower():
            matched = e
            break
    if not matched:
        # Approve all auto-executable
        count = 0
        names = []
        for e in entries:
            if e.get("trigger") in ("SPEC_CHANGED", "READY_PENDING") and not e.get("approved"):
                e["approved"] = True
                count += 1
                names.append(e["requirement"])
        if count > 0:
            with open(pending_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return f"已批准 {count} 个需求：{', '.join(names)}，调度器将在下个周期执行。"
        return "没有待审批的自动执行项。"
    if matched.get("approved"):
        return f"{matched['requirement']} 已在批准状态。"
    if matched.get("trigger") not in ("SPEC_CHANGED", "READY_PENDING"):
        return f"{matched['requirement']}（{matched['trigger']}）需要人工处理，无法自动执行。"
    matched["approved"] = True
    with open(pending_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return f"已批准 {matched['requirement']}，调度器将在下个周期执行。"