"""Agent CLI backend for the loop path.

scheduler spawns one non-interactive agent session per step. Binary location, MCP
whitelist flags, where the model name is read from, and how session continuation
is detected are all CLI-specific — they live here so a second backend lands as
one builder function instead of a cross-file patch.

Everything above this seam stays backend-agnostic: session identity (uuid5 of
root + module key) is the caller's choice, and the `.loop/result.md` /
`__JSON_ACTION__` contracts are prompt-level, not flag-level.

Chosen by env LOOP_ENGINE_AGENT_CLI; unset means qodercli (this machine's
behaviour is unchanged by importing this module).
"""

import json
import os
import shutil

_MCP_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "minimal_mcp.json")


def backend():
    return os.environ.get("LOOP_ENGINE_AGENT_CLI", "qodercli")


def _qodercli_binary():
    return (shutil.which("qodercli")
            or os.path.expanduser("~/.local/bin/qodercli"))


def _qodercli_model():
    """Model for loop-agent sessions, from the same settings the WeCom G path
    reads (~/.qoder/settings.json model.name). Empty string means pass no
    --model and keep the CLI's own default."""
    try:
        with open(os.path.expanduser("~/.qoder/settings.json")) as f:
            return json.load(f).get("model", {}).get("name") or ""
    except (OSError, ValueError):
        return ""


def _session_file_exists(sid, cwd):
    """True when a persisted session jsonl already exists for (sid, cwd).
    qodercli stores sessions under
    ~/.qoder/projects/<cwd-with-slashes-and-dots-as-dashes>/<sid>.jsonl."""
    processed = cwd.replace("/", "-").replace(".", "-")
    path = os.path.expanduser(f"~/.qoder/projects/{processed}/{sid}.jsonl")
    return os.path.isfile(path)


def _qodercli_cmd(root, sid, system_prompt, user_text):
    # --resume when the session is already on disk, --session-id otherwise: the
    # first step of a module-run creates it, every later step continues it.
    #
    # Only codegraph in the MCP whitelist — loop steps never touch
    # playwright/postgres/redis/mysql, and skipping them shaves minutes off each
    # cold start.
    cmd = [_qodercli_binary(), "--print",
           "--resume" if _session_file_exists(sid, root) else "--session-id",
           sid,
           "--strict-mcp-config", "--mcp-config", _MCP_CONFIG,
           "--dangerously-skip-permissions",
           "--cwd", root, "--append-system-prompt", system_prompt]
    model = _qodercli_model()
    if model:
        cmd += ["--model", model]
    cmd.append(user_text)
    return cmd


# One row per agent CLI: how to spawn a step, and where its assets live. A None
# cmd means "known backend, argv builder not written yet", never "fall back to
# qodercli" — a silently-falling-back spawn would report pi results that are
# really qodercli's.
#
# Both pi directories are measured on this machine (pi 0.85.1 + pi-subagents
# 0.66.0, 2026-09-08), not read out of docs. pi scans ~/.pi/agent/skills/ *and*
# the shared ~/.agents/skills/; we install to the former so our 5 skills stay out
# of the user's general skill set. Subagent profiles are a pi-subagents concept
# (pi's core has none) and it reads ~/.pi/agent/agents/**/*.md recursively.
#
# For whoever writes _pi_cmd: pi has no per-tool permission prompt at all — bash
# and edit execute immediately in -p, so there is no --dangerously-skip-permissions
# analogue to find (and no confirmation stall to worry about). That also means the
# only throttle is --tools, so a step should get a narrowed tool set rather than
# pi's default "everything". MCP whitelist: pi has no --strict-mcp-config; use
# pi-mcp-adapter's --mcp-config <path> plus env PI_MCP_CONFIG_MODE=exclusive,
# which stops the six ambient MCP config layers from merging in.
_BACKENDS = {
    "qodercli": {"cmd": _qodercli_cmd,
                 "skills": "~/.qoder/skills",
                 "agents": "~/.qoder/agents"},
    "pi": {"cmd": None,
           "skills": "~/.pi/agent/skills",
           "agents": "~/.pi/agent/agents"},
}


def _profile(name=None):
    name = name or backend()
    if name not in _BACKENDS:
        raise RuntimeError(
            f"未实现的 agent 后端：{name}"
            f"（可选：{', '.join(sorted(_BACKENDS))}）")
    return _BACKENDS[name]


def asset_dirs():
    """(skills_dir, agents_dir) for the selected backend."""
    p = _profile()
    return (os.path.expanduser(p["skills"]),
            os.path.expanduser(p["agents"]))


def build_cmd(root, sid, system_prompt, user_text):
    """Spawn-ready argv list for one loop step. Caller runs it with cwd=root —
    the MCP child inherits the OS cwd, so pinning it here is what makes
    codegraph index the target repo instead of the daemon's own directory."""
    name = backend()
    builder = _profile(name)["cmd"]
    if builder is None:
        implemented = sorted(k for k, v in _BACKENDS.items() if v["cmd"])
        raise RuntimeError(
            f"agent 后端 {name} 尚未实现 argv 构造"
            f"（已实现：{', '.join(implemented)}）")
    return builder(root, sid, system_prompt, user_text)
