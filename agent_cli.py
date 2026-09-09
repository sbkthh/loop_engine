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
import logging
import os
import shutil
import subprocess
from functools import lru_cache

from constants import ALL_STEP_KEYS, CHAT_STEP, CLASSIFY_STEP

_MCP_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "minimal_mcp.json")

# Per-step model names live in a file next to this module, not in code: the two
# backends spell models differently and a vendor name committed here would be
# pushed into every mirror checkout.
_MODEL_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "agent_models.json")

_LOG = logging.getLogger(__name__)
_WARNED = set()


def _warn_once(key, msg):
    """Config problems are human-made and non-fatal, but a per-spawn warning
    would bury the audit log. First one only; the state is visible in argv."""
    if key not in _WARNED:
        _WARNED.add(key)
        _LOG.warning(msg)


def backend():
    return os.environ.get("LOOP_ENGINE_AGENT_CLI", "qodercli")


def _model_config_path():
    # Cache keys take the path, so pointing this env at a fixture re-reads.
    return os.environ.get("LOOP_ENGINE_MODEL_CONFIG") or _MODEL_CONFIG


@lru_cache(maxsize=None)
def _known_models(name):
    """Model names the CLI accepts, or None when we can't ask.

    Validation exists because an unknown --model is not an error: measured
    2026-09-09, qodercli answers rc=0 with `Model "X" is not available right
    now; using "auto" instead` and keeps serving the rest of the session from
    auto. Cost is one 3s call per process, paid only once a section actually
    names something.
    """
    binary = _qodercli_binary() if name == "qodercli" else _pi_binary()
    try:
        r = subprocess.run([binary, "--list-models"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    names = {ln.strip().lower() for ln in r.stdout.splitlines() if ln.strip()}
    names.discard("model")  # column header
    return names or None


def _qodercli_binary():
    return (shutil.which("qodercli")
            or os.path.expanduser("~/.local/bin/qodercli"))


def _qodercli_settings_model():
    """Model for loop-agent sessions, from the same settings the WeCom G path
    reads (~/.qoder/settings.json model.name). Empty string means pass no
    --model and keep the CLI's own default."""
    try:
        with open(os.path.expanduser("~/.qoder/settings.json")) as f:
            return json.load(f).get("model", {}).get("name") or ""
    except (OSError, ValueError):
        return ""


def _qodercli_model(step=None):
    return _configured_model("qodercli", step) or _qodercli_settings_model()


@lru_cache(maxsize=None)
def _step_models(name, path):
    """step → model for backend `name`; {} means "no usable config, behave as
    before this file existed".

    All-or-nothing on purpose. Omitting --model on a resumed session does not
    re-read settings.json — measured 2026-09-09, qodercli keeps serving that
    session with whatever model the previous turn used. A half-filled file
    would therefore make a step's model depend on which step ran last, which
    is exactly the silent drift the engine exists to prevent. So a section is
    live only when `default` resolves to a name every unnamed step can fall
    back to.

    A name the CLI does not list drops the section rather than being passed
    through: an unknown --model exits 0 (see _known_models), so the typo would
    surface as a differently-priced run, not as a failure.
    """
    try:
        with open(path) as f:
            section = json.load(f).get(name)
    except OSError:
        return {}
    except ValueError as e:
        _warn_once(f"parse:{path}", f"agent_models.json 解析失败，按未配置处理：{e}")
        return {}
    if not isinstance(section, dict):
        return {}
    wanted = {str(k): str(v).strip() for k, v in section.items() if str(v).strip()}
    if not wanted:
        return {}
    known = _known_models(name)

    def usable(model):
        if known is None or model.lower() in known:
            return True
        _warn_once(f"model:{name}:{model}",
                   f"型号 {model!r} 不在 {name} 的 --list-models 结果里，"
                   f"整份每步模型配置不生效（写错不会报错，只会静默跑在 auto 上）")
        return False

    default = wanted.get("default", "")
    if not default or not usable(default):
        if "default" not in wanted:
            _warn_once(f"default:{path}",
                       f"{name} 段配了步骤级模型但没有 default：整段不生效。"
                       "省略 --model 会沿用上一轮的模型，未配的步骤不能留空")
        return {}
    resolved = {"default": default}
    for step in ALL_STEP_KEYS:
        model = wanted.get(step) or default
        resolved[step] = model if usable(model) else default
    return resolved


def _configured_model(name, step):
    models = _step_models(name, _model_config_path())
    if not models:
        return ""
    return models.get(step) or models["default"]


def _session_file_exists(sid, cwd):
    """True when a persisted session jsonl already exists for (sid, cwd).
    qodercli stores sessions under
    ~/.qoder/projects/<cwd-with-slashes-and-dots-as-dashes>/<sid>.jsonl."""
    processed = cwd.replace("/", "-").replace(".", "-")
    path = os.path.expanduser(f"~/.qoder/projects/{processed}/{sid}.jsonl")
    return os.path.isfile(path)


def _qodercli_cmd(root, sid, system_prompt, user_text, action=None):
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
    model = _qodercli_model(action)
    if model:
        cmd += ["--model", model]
    cmd.append(user_text)
    return cmd


def _pi_binary():
    return (os.environ.get("LOOP_ENGINE_PI_BIN")
            or shutil.which("pi")
            or os.path.expanduser("~/.nvm/versions/node/v22.22.0/bin/pi"))


def _pi_settings_model():
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


def _pi_model(step=None):
    return _configured_model("pi", step) or _pi_settings_model()


# pi's builtin tools are read/grep/find/ls/bash/edit/write (+powershell); MCP
# arrives through the adapter's single `mcp` meta-tool, so an allowlist naming
# codegraph_* matches nothing and `mcp` is what has to be listed. Everything the
# other packages add (subagent, web_search, fetch_content, …) is dropped here —
# with no permission prompt and no sandbox, --tools is the only throttle pi has.
_PI_TOOLS = "read,grep,find,ls,bash,edit,write,mcp"


def _pi_cmd(root, sid, system_prompt, user_text, action=None):
    # root is deliberately unused: pi has no --cwd, and the child's OS cwd (which
    # scheduler.run pins to root) is both pi's working dir and the cwd its MCP
    # children inherit. --session-id creates the session when missing, so the
    # --session-id/--resume two-step and its disk probe have no counterpart.
    cmd = [_pi_binary(), "--print", "--session-id", sid,
           "--mcp-config", _MCP_CONFIG,
           "--tools", os.environ.get("LOOP_ENGINE_PI_TOOLS", _PI_TOOLS),
           "--append-system-prompt", system_prompt]
    model = _pi_model(action)
    if model:
        cmd += ["--model", model]
    cmd.append(user_text)
    return cmd


# The WeCom G path spawns the same CLIs under a different contract: the whole
# composed prompt arrives on stdin and the reply is whatever lands on stdout, so
# there is no system-prompt flag and no trailing user-text argument. Session
# identity is the user's conversation (create on first message, resume after),
# and the spec-edit audit hook rides in per backend: --settings JSON or a loaded
# extension. See chat_audit_mode().


def _qodercli_chat_cmd(session_id, is_new, audit_settings, step=CHAT_STEP):
    cmd = [_qodercli_binary(), "--print",
           "--session-id" if is_new else "--resume", session_id]
    model = _qodercli_model(step)
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

# pi's counterpart to a per-call PreToolUse JSON is a tool_call hook that can
# only come from a loaded extension, so the audit chain ships as a thin .ts
# adapter sitting next to audit_hook.sh rather than a second copy of its guards.
_PI_CHAT_BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "wecom_server", "hooks", "pi_audit_bridge.ts")


def _pi_chat_bridge():
    return os.environ.get("LOOP_ENGINE_PI_CHAT_EXTENSION") or _PI_CHAT_BRIDGE


def _pi_chat_cmd(session_id, is_new, audit_settings, step=CHAT_STEP):
    # is_new is unused: --session-id creates the session when it is missing.
    # audit_settings is unused: pi gets the hook from the -e extension instead.
    # The flag is conditional because a missing extension path is a hard pi
    # failure — shipping without the .ts would take down every G reply.
    cmd = [_pi_binary(), "--print", "--session-id", session_id,
           "--tools", os.environ.get("LOOP_ENGINE_PI_CHAT_TOOLS", _PI_CHAT_TOOLS)]
    if chat_audit_mode() == "extension":
        cmd += ["-e", _pi_chat_bridge()]
    model = _pi_model(step)
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
                 "audit": "settings",
                 "skills": "~/.qoder/skills",
                 "agents": "~/.qoder/agents"},
    "pi": {"cmd": _pi_cmd,
           "chat": _pi_chat_cmd,
           "clean": _pi_clean_reply,
           "audit": "extension",
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


def build_cmd(root, sid, system_prompt, user_text, action=None):
    """Spawn-ready argv list for one loop step. Caller runs it with cwd=root —
    the MCP child inherits the OS cwd, so pinning it here is what makes
    codegraph index the target repo instead of the daemon's own directory.

    `action` names the step so agent_models.json can give each one its own
    model; None or an unconfigured name means "whatever this backend defaults
    to"."""
    name = backend()
    builder = _profile(name)["cmd"]
    if builder is None:
        implemented = sorted(k for k, v in _BACKENDS.items() if v["cmd"])
        raise RuntimeError(
            f"agent 后端 {name} 尚未实现 argv 构造"
            f"（已实现：{', '.join(implemented)}）")
    return builder(root, sid, system_prompt, user_text, action)


def build_chat_cmd(session_id, is_new=True, audit_settings=None,
                   step=CHAT_STEP):
    """Spawn-ready argv for one WeCom turn. The prompt goes on stdin and the
    reply is read back with clean_reply(), so nothing here carries text.

    `step` separates the G conversation from the requirement-classification
    turn, which is a one-shot session rather than a resumed conversation."""
    chat = _profile()["chat"]
    return chat(session_id, is_new, audit_settings, step)


def clean_reply(stdout):
    """LLM reply text from process stdout, minus the backend's startup noise."""
    return _profile()["clean"](stdout)


def chat_audit_mode():
    """How the audit hook reaches one chat turn: "settings" (per-call PreToolUse
    JSON), "extension" (a loaded .ts adapter next to the hook), or "" — no hook
    in play, so the spec-edit audit trail and the correction loop built on it
    are absent. A missing extension file reports "" rather than "extension":
    pi treats an unloadable -e path as a hard startup failure, and a packaging
    gap must not cost a human the whole reply."""
    mode = _profile()["audit"]
    if mode != "extension":
        return mode
    return mode if os.path.isfile(_pi_chat_bridge()) else ""
