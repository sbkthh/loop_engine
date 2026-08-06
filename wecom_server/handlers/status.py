"""Status handler: read all requirements and summarize state."""
import json
import os


def handle_status(registry, data_dir=None):
    if not registry:
        return "没有注册的需求。"
    lines = []
    for req in registry:
        name = req.get("name", "?")
        root = req.get("root", "")
        state_path = os.path.join(root, ".loop", "state.json")
        if not os.path.exists(state_path):
            lines.append(f"{name}: 未初始化")
            continue
        with open(state_path) as f:
            state = json.load(f)
        modules = state.get("modules", {})
        current = state.get("current", {})
        if not modules:
            lines.append(f"{name}: 无模块")
            continue
        status_counts = {}
        for m in modules.values():
            s = m.get("status", "UNKNOWN")
            status_counts[s] = status_counts.get(s, 0) + 1
        parts = [f"{v} {k}" for k, v in sorted(status_counts.items())]
        active = ""
        if current.get("action"):
            active = f" [执行中: {current['module']} / {current['action']}]"
        lines.append(f"{name}: {', '.join(parts)}{active}")
    return "状态汇总：\n" + "\n".join(lines)