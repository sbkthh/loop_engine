"""Tests for agent_cli.py — the spawn seam both paths go through: the loop step
(build_cmd) and the WeCom chat turn (build_chat_cmd + clean_reply)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_cli


def _default_env():
    """Env without any LOOP_ENGINE_* override but with everything else intact —
    clear=True would drop HOME too, and expanduser('~') then falls back to pwd,
    which differs whenever the suite runs under a substituted HOME."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LOOP_ENGINE_")}
    return mock.patch.dict(os.environ, env, clear=True)


class BuildCmdTest(unittest.TestCase):
    def setUp(self):
        # this class describes qodercli's argv contract; an ambient
        # LOOP_ENGINE_AGENT_CLI would retarget every case here
        env = _default_env()
        env.start()
        self.addCleanup(env.stop)

    def test_qodercli_argv_matches_loop_contract(self):
        """Argument order is what the running loop depends on; a reorder here
        silently changes how every step spawns."""
        with mock.patch.object(agent_cli, "_session_file_exists",
                               return_value=False), \
                mock.patch.object(agent_cli, "_qodercli_model",
                                  return_value="gmodel"):
            cmd = agent_cli.build_cmd("/root/x", "sid-1", "SYS PROMPT",
                                      '{"action": "SCORE"}')
        self.assertEqual(cmd[1:5], ["--print", "--session-id", "sid-1",
                                   "--strict-mcp-config"])
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1],
                         os.path.join(os.path.dirname(
                             os.path.abspath(agent_cli.__file__)),
                             "minimal_mcp.json"))
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1],
                         "SYS PROMPT")
        self.assertEqual(cmd[cmd.index("--model") + 1], "gmodel")
        self.assertEqual(cmd[-1], '{"action": "SCORE"}')
        self.assertTrue(cmd[0].endswith("qodercli"))

    def test_no_model_configured_omits_flag(self):
        with mock.patch.object(agent_cli, "_session_file_exists",
                               return_value=True), \
                mock.patch.object(agent_cli, "_qodercli_model",
                                  return_value=""):
            cmd = agent_cli.build_cmd("/root/x", "sid-1", "S", "U")
        self.assertEqual(cmd[2], "--resume")
        self.assertNotIn("--model", cmd)

    def test_unknown_backend_fails_fast(self):
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "nope"}):
            with self.assertRaises(RuntimeError) as ctx:
                agent_cli.build_cmd("/root/x", "sid-1", "S", "U")
        self.assertIn("nope", str(ctx.exception))

    def test_default_backend_is_qodercli(self):
        with _default_env():
            self.assertEqual(agent_cli.backend(), "qodercli")

    def test_asset_dirs_qodercli(self):
        with _default_env():
            skills, agents = agent_cli.asset_dirs()
        self.assertEqual(skills, os.path.expanduser("~/.qoder/skills"))
        self.assertEqual(agents, os.path.expanduser("~/.qoder/agents"))

    def test_asset_dirs_pi(self):
        """Both pi directories were measured against a real pi + pi-subagents
        install; self-install writes here, so a guessed path would land profiles
        in a directory nothing loads from."""
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            skills, agents = agent_cli.asset_dirs()
        self.assertEqual(skills, os.path.expanduser("~/.pi/agent/skills"))
        self.assertEqual(agents, os.path.expanduser("~/.pi/agent/agents"))

    def test_asset_dirs_unknown_backend_fails(self):
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "nope"}):
            with self.assertRaises(RuntimeError):
                agent_cli.asset_dirs()


class PiCmdTest(unittest.TestCase):
    """Every assertion below is a flag list measured against a real pi 0.85.1
    run on this machine (2026-09-08) — the flags present, and just as much the
    ones pi does not have."""

    def setUp(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LOOP_ENGINE_")}
        env["LOOP_ENGINE_AGENT_CLI"] = "pi"
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cmd(self, **model_ret):
        with mock.patch.object(agent_cli, "_pi_model", **model_ret):
            return agent_cli.build_cmd("/root/x", "sid-1", "SYS PROMPT",
                                       '{"action": "SCORE"}')

    def test_pi_argv_matches_loop_contract(self):
        cmd = self._cmd(return_value="deepseek/deepseek-v4-flash")
        self.assertEqual(cmd[1:4], ["--print", "--session-id", "sid-1"])
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1],
                         os.path.join(os.path.dirname(
                             os.path.abspath(agent_cli.__file__)),
                             "minimal_mcp.json"))
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1],
                         "SYS PROMPT")
        self.assertEqual(cmd[cmd.index("--tools") + 1], agent_cli._PI_TOOLS)
        self.assertEqual(cmd[cmd.index("--model") + 1],
                         "deepseek/deepseek-v4-flash")
        self.assertEqual(cmd[-1], '{"action": "SCORE"}')
        self.assertTrue(cmd[0].endswith("pi"))

    def test_pi_argv_omits_flags_pi_does_not_have(self):
        """--cwd/--strict-mcp-config/--dangerously-skip-permissions are qodercli's;
        passing them to pi would be an unknown-argument failure on every step.
        --resume has no role either: --session-id creates what it cannot find."""
        cmd = self._cmd(return_value="")
        for flag in ("--cwd", "--resume", "--strict-mcp-config",
                     "--dangerously-skip-permissions", "--settings"):
            self.assertNotIn(flag, cmd)
        self.assertNotIn("--model", cmd)

    def test_pi_tools_keeps_mcp_meta_tool_and_drops_extension_tools(self):
        """MCP servers are reached through the adapter's single `mcp` tool, so
        naming codegraph_* in the allowlist would silently strip codegraph. The
        extension tools are the ones --tools exists to remove: pi runs whatever
        it allows, with no prompt behind it."""
        allowed = set(agent_cli._PI_TOOLS.split(","))
        self.assertIn("mcp", allowed)
        self.assertTrue(allowed.issuperset({"read", "bash", "edit", "write"}))
        self.assertFalse(allowed.intersection(
            {"subagent", "web_search", "fetch_content", "mcpScript"}))

    def test_pi_tools_and_binary_are_env_overridable(self):
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_PI_TOOLS": "read",
                                          "LOOP_ENGINE_PI_BIN": "/opt/pi"}):
            cmd = self._cmd(return_value="")
        self.assertEqual(cmd[0], "/opt/pi")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "read")

    def test_pi_session_continuation_needs_no_disk_probe(self):
        """qodercli flips to --resume once the session file exists. The same
        argv is correct for pi in both cases, so _session_file_exists has no
        pi counterpart to keep in sync."""
        first = self._cmd(return_value="")
        second = self._cmd(return_value="")
        self.assertEqual(first, second)
        self.assertEqual(first[1:3], ["--print", "--session-id"])


class ChatCmdTest(unittest.TestCase):
    """The WeCom G path: prompt on stdin, reply on stdout. So the chat argv
    carries no prompt at all — no --append-system-prompt, no trailing user
    text — and everything the router used to hardcode (binary, model, session
    continuation, startup noise) comes from here instead."""

    def _qodercli(self, is_new=True, settings=None):
        env = _default_env()
        with env, mock.patch.object(agent_cli, "_qodercli_model",
                                    return_value="gmodel"):
            return agent_cli.build_chat_cmd("sid-1", is_new, settings)

    def test_qodercli_chat_argv_matches_g_path_contract(self):
        cmd = self._qodercli(is_new=False, settings='{"hooks": 1}')
        self.assertEqual(cmd[1:4], ["--print", "--resume", "sid-1"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "gmodel")
        self.assertEqual(cmd[cmd.index("--settings") + 1], '{"hooks": 1}')
        self.assertTrue(cmd[0].endswith("qodercli"))
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertNotIn("--cwd", cmd)

    def test_qodercli_first_turn_creates_session(self):
        self.assertEqual(self._qodercli(is_new=True)[1:4],
                         ["--print", "--session-id", "sid-1"])

    def test_qodercli_without_audit_settings_omits_flag(self):
        """The classifier turn: one-off, no session reuse, nothing to audit."""
        self.assertNotIn("--settings", self._qodercli())

    def test_pi_chat_argv(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LOOP_ENGINE_")}
        env["LOOP_ENGINE_AGENT_CLI"] = "pi"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(agent_cli, "_pi_model", return_value=""):
            resumed = agent_cli.build_chat_cmd("sid-1", False, "SETTINGS")
        self.assertEqual(resumed[1:4], ["--print", "--session-id", "sid-1"])
        self.assertEqual(resumed[resumed.index("--tools") + 1],
                         agent_cli._PI_CHAT_TOOLS)
        self.assertNotIn("--resume", resumed)
        self.assertNotIn("--settings", resumed)
        self.assertNotIn("--mcp-config", resumed)
        self.assertNotIn("--model", resumed)

    def test_pi_chat_tools_drops_mcp_keeps_spec_edits(self):
        """G answers from state.json and edits spec.md itself: MCP would only
        add cold-start latency to a reply a human waits for, while edit/write
        are what the spec-session flow needs."""
        allowed = set(agent_cli._PI_CHAT_TOOLS.split(","))
        self.assertNotIn("mcp", allowed)
        self.assertTrue(allowed.issuperset({"read", "edit", "write", "bash"}))

    def test_clean_reply_strips_only_leading_qodercli_noise(self):
        with _default_env():
            reply = agent_cli.clean_reply(
                "MCP issues detected\nqodercli 1.0.45\n【req】正文\nqodercli 尾巴")
        self.assertEqual(reply, "【req】正文\nqodercli 尾巴")

    def test_clean_reply_pi_keeps_body(self):
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            self.assertEqual(agent_cli.clean_reply("  【req】hi \n"), "【req】hi")

    def test_chat_supports_audit_hook(self):
        """The flag is about handing a hook by value (--settings JSON). pi's
        before_tool hook needs a loaded extension we don't ship, so under pi the
        caller must learn that instead of watching the correction loop go quiet."""
        with _default_env():
            self.assertTrue(agent_cli.chat_supports_audit_hook())
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            self.assertFalse(agent_cli.chat_supports_audit_hook())

    def test_unknown_backend_fails_fast_on_chat_too(self):
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "nope"}):
            with self.assertRaises(RuntimeError):
                agent_cli.build_chat_cmd("sid-1")


class RouterChatSeamTest(unittest.TestCase):
    """Closes the gap where the WeCom path built qodercli argv itself: switching
    the backend had to retarget the chat turns as well, not just loop steps."""

    def setUp(self):
        from wecom_server import router
        self.router = router

    def _spawned_cmd(self, backend_name):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LOOP_ENGINE_")}
        if backend_name:
            env["LOOP_ENGINE_AGENT_CLI"] = backend_name
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return type("R", (), {"stdout": "ok", "returncode": 0})()

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(self.router.subprocess, "run", fake_run):
            reply = self.router._run_llm_turn("sid-1", True, "PROMPT")
        self.assertEqual(reply, "ok")
        return seen["cmd"]

    def test_default_backend_still_spawns_qodercli(self):
        cmd = self._spawned_cmd(None)
        self.assertTrue(cmd[0].endswith("qodercli"))

    def test_pi_backend_spawns_pi_without_audit_settings(self):
        cmd = self._spawned_cmd("pi")
        self.assertTrue(cmd[0].endswith("pi"))
        self.assertNotIn("--settings", cmd)

    def test_audit_settings_follow_the_backend(self):
        """The correction loop is a bedrock guard; under pi it has nothing to
        read, so it must be visibly off rather than silently kept configured."""
        with _default_env():
            self.assertIn("Bash|Edit|Write", self.router._chat_audit_settings())
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            self.assertIsNone(self.router._chat_audit_settings())


class PiModelTest(unittest.TestCase):
    def _with_settings(self, payload):
        """HOME substitution rather than an open() mock: _pi_model reads through
        expanduser, so this exercises the real path resolution too."""
        home = tempfile.mkdtemp()
        d = os.path.join(home, ".pi", "agent")
        os.makedirs(d)
        if payload is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump(payload, f)
        return mock.patch.dict(os.environ, {"HOME": home})

    def test_provider_and_model_become_a_pi_model_pattern(self):
        with self._with_settings({"defaultProvider": "deepseek",
                                  "defaultModel": "deepseek-v4-flash"}):
            self.assertEqual(agent_cli._pi_model(),
                             "deepseek/deepseek-v4-flash")

    def test_model_without_provider_passes_bare_id(self):
        with self._with_settings({"defaultModel": "deepseek-v4-flash"}):
            self.assertEqual(agent_cli._pi_model(), "deepseek-v4-flash")

    def test_no_default_configured_returns_empty(self):
        """This machine's actual state: settings.json has packages/theme only,
        and pi resolves the model itself. Empty means the builder omits --model."""
        with self._with_settings({"theme": "light", "packages": []}):
            self.assertEqual(agent_cli._pi_model(), "")

    def test_missing_or_corrupt_settings_returns_empty(self):
        with self._with_settings(None):
            self.assertEqual(agent_cli._pi_model(), "")
        home = tempfile.mkdtemp()
        d = os.path.join(home, ".pi", "agent")
        os.makedirs(d)
        with open(os.path.join(d, "settings.json"), "w") as f:
            f.write("{not json")
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(agent_cli._pi_model(), "")


if __name__ == "__main__":
    unittest.main()
