"""Agent CLI backend seam: per-CLI argv and stdout quirks, in one place.

Two spawn paths share the profile table:

- loop step (scheduler): one non-interactive session, prompt in argv
  (--append-system-prompt + user text), reply read from .loop/result.md, so
  stdout is never parsed → build_cmd().
- WeCom chat (router): one turn per message, the whole composed prompt on stdin,
  reply taken from stdout → build_chat_cmd() + clean_reply().

Binary location, MCP whitelist flags, where the model name is read from, session
continuation, and which startup lines pollute stdout are all CLI-specific.
Everything above this seam stays backend-agnostic: session identity (uuid5 of
root + module key for the loop, per-user/per-requirement for chat) is the
caller's choice, and the `.loop/result.md` / `__JSON_ACTION__` contracts are
prompt-level, not flag-level.

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


def _pi_binary():
    return (os.environ.get("LOOP_ENGINE_PI_BIN")
            or shutil.which("pi")
            or os.path.expanduser("~/.nvm/versions/node/v22.22.0/bin/pi"))


def _pi_model():
    """pi's own default, spelled provider/model for its --model flag.

    ~/.pi/agent/settings.json only carries defaultProvider/defaultModel once a
    human picks one in the TUI — this machine has neither, so pi falls back to
    "first model with configured auth" (deepseek-v4-pro). Empty string means
    pass no --model and keep pi's resolution order.
    """
    try:
        with open(os.path.expanduser("~/.pi/agent/settings.json")) as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return ""
    model = settings.get("defaultModel")
    if not model:
        return ""
    provider = settings.get("defaultProvider")
    return f"{provider}/{model}" if provider else model


# pi's builtin tools are read/grep/find/ls/bash/edit/write (+powershell); MCP
# arrives through the adapter's single `mcp` meta-tool, so an allowlist naming
# codegraph_* matches nothing and `mcp` is what has to be listed. Everything the
# other packages add (subagent, web_search, fetch_content, …) is dropped here —
# with no permission prompt and no sandbox, --tools is the only throttle pi has.
_PI_TOOLS = "read,grep,find,ls,bash,edit,write,mcp"


def _pi_cmd(root, sid, system_prompt, user_text):
    # root is deliberately unused: pi has no --cwd, and the child's OS cwd (which
    # scheduler.run pins to root) is both pi's working dir and the cwd its MCP
    # children inherit. --session-id creates the session when missing, so the
    # --session-id/--resume two-step and its disk probe have no counterpart.
    cmd = [_pi_binary(), "--print", "--session-id", sid,
           "--mcp-config", _MCP_CONFIG,
           "--tools", os.environ.get("LOOP_ENGINE_PI_TOOLS", _PI_TOOLS),
           "--append-system-prompt", system_prompt]
    model = _pi_model()
    if model:
        cmd += ["--model", model]
    cmd.append(user_text)
    return cmd


# The WeCom G path spawns the same CLIs under a different contract: the whole
# composed prompt arrives on stdin and the reply is whatever lands on stdout, so
# there is no system-prompt flag and no trailing user-text argument. Session
# identity is the user's conversation (create on first message, resume after),
# and the spec-edit audit hook rides in through --settings.


def _qodercli_chat_cmd(session_id, is_new, audit_settings):
    cmd = [_qodercli_binary(), "--print",
           "--session-id" if is_new else "--resume", session_id]
    model = _qodercli_model()
    if model:
        cmd += ["--model", model]
    cmd.append("--dangerously-skip-permissions")
    if audit_settings:
        cmd += ["--settings", audit_settings]
    return cmd


# qodercli startup noise that leaks into stdout before the actual LLM reply
_QODERCLI_STDOUT_NOISE = (
    "MCP issues detected",
    "All dependencies are up to date",
    "qodercli ",
)


def _qodercli_clean_reply(stdout):
    lines = (stdout or "").splitlines()
    while lines and lines[0].strip().startswith(_QODERCLI_STDOUT_NOISE):
        lines.pop(0)
    return "\n".join(lines).strip()


# G answers from the requirement's own .loop/state.json and edits spec.md itself;
# codegraph would only add MCP cold-start latency to a reply a human is waiting
# on. edit/write stay in — the spec-session flow has G writing spec.md, which is
# exactly what the audit hook and the correction loop guard.
_PI_CHAT_TOOLS = "read,grep,find,ls,bash,edit,write"


def _pi_chat_cmd(session_id, is_new, audit_settings):
    # is_new is unused: --session-id creates the session when it is missing.
    # audit_settings is unused: qodercli takes a PreToolUse hook as a per-call
    # --settings JSON; pi's equivalent is a before_tool hook that has to come
    # from a loaded extension (--extension/-e or a discovered extensions dir),
    # and nothing here ships one. So the spec-edit audit trail — and the
    # correction loop that reads it — is unwired under pi, which is what
    # chat_supports_audit_hook() reports and the caller warns about.
    cmd = [_pi_binary(), "--print", "--session-id", session_id,
           "--tools", os.environ.get("LOOP_ENGINE_PI_CHAT_TOOLS", _PI_CHAT_TOOLS)]
    model = _pi_model()
    if model:
        cmd += ["--model", model]
    return cmd


def _pi_clean_reply(stdout):
    # measured 2026-09-08: pi -p puts the reply on stdout and its version/npm
    # warnings on stderr, so there is no leading noise to strip.
    return (stdout or "").strip()


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
# For whoever extends pi: pi has no per-tool permission prompt at all — bash and
# edit execute immediately in -p, so there is no --dangerously-skip-permissions
# analogue to find, and no confirmation stall to worry about. The price is that
# pi has no sandbox either, so _PI_TOOLS is the whole containment story.
#
# MCP whitelist: pi has no --strict-mcp-config. --mcp-config <path> replaces only
# the pi-global layer; ~/.config/mcp/mcp.json, ~/.agents/mcp.json, <cwd>/.mcp.json
# and <cwd>/.pi/mcp.json still merge in. PI_MCP_CONFIG_MODE=exclusive does reduce
# it to that one layer, but measured against the adapter it drops the override path
# on the floor too (config.js:374/347 — exclusive discards overridePath), i.e. our
# own minimal_mcp.json stops loading: 1/1 servers without it, 0/0 with it.
_BACKENDS = {
    "qodercli": {"cmd": _qodercli_cmd,
                 "chat": _qodercli_chat_cmd,
                 "clean": _qodercli_clean_reply,
                 "audit": True,
                 "skills": "~/.qoder/skills",
                 "agents": "~/.qoder/agents"},
    "pi": {"cmd": _pi_cmd,
           "chat": _pi_chat_cmd,
           "clean": _pi_clean_reply,
           "audit": False,
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


def build_chat_cmd(session_id, is_new=True, audit_settings=None):
    """Spawn-ready argv for one WeCom turn. The prompt goes on stdin and the
    reply is read back with clean_reply(), so nothing here carries text."""
    chat = _profile()["chat"]
    return chat(session_id, is_new, audit_settings)


def clean_reply(stdout):
    """LLM reply text from process stdout, minus the backend's startup noise."""
    return _profile()["clean"](stdout)


def chat_supports_audit_hook():
    """True when a chat turn can be given the audit hook by value (qodercli's
    --settings JSON). A backend that needs its own plugin file instead (pi:
    before_tool in an extension) reports False, so the caller states that the
    spec-edit trail — and the correction loop built on it — is not in play."""
    return bool(_profile()["audit"])
