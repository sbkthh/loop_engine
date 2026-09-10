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
from constants import CHECKER


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
        --resume has no role either: --session-id creates what it cannot find.
        -e is flagged here because it is the chat audit bridge, not because pi
        lacks the flag: loop steps carry no guard on either backend."""
        cmd = self._cmd(return_value="")
        for flag in ("--cwd", "--resume", "--strict-mcp-config",
                     "--dangerously-skip-permissions", "--settings", "-e"):
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
        self.assertEqual(resumed[resumed.index("-e") + 1],
                         os.path.join(os.path.dirname(os.path.abspath(agent_cli.__file__)),
                                      "wecom_server", "hooks", "pi_audit_bridge.ts"))
        self.assertNotIn("--resume", resumed)
        self.assertNotIn("--settings", resumed)
        self.assertNotIn("--mcp-config", resumed)
        self.assertNotIn("--model", resumed)

    def test_pi_chat_argv_without_bridge(self):
        """A bridge the install dropped must degrade to an unguarded turn, not to
        pi exiting 1 on every WeCom reply."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LOOP_ENGINE_")}
        env["LOOP_ENGINE_AGENT_CLI"] = "pi"
        env["LOOP_ENGINE_PI_CHAT_EXTENSION"] = "/nonexistent/pi_audit_bridge.ts"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(agent_cli, "_pi_model", return_value=""):
            cmd = agent_cli.build_chat_cmd("sid-1", True, "SETTINGS")
        self.assertNotIn("-e", cmd)

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

    def test_chat_audit_mode(self):
        """The mode says how the hook reaches a turn, and "" is a state, not an
        afterthought: pi exits 1 on an unloadable -e path, so a bridge the
        packaging dropped has to degrade to an unguarded turn the caller warns
        about — not to every WeCom reply failing."""
        with _default_env():
            self.assertEqual(agent_cli.chat_audit_mode(), "settings")
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            self.assertEqual(agent_cli.chat_audit_mode(), "extension")
        with mock.patch.dict(os.environ, {
                "LOOP_ENGINE_AGENT_CLI": "pi",
                "LOOP_ENGINE_PI_CHAT_EXTENSION": "/nonexistent/bridge.ts"}):
            self.assertEqual(agent_cli.chat_audit_mode(), "")

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

    def test_pi_backend_spawns_pi_with_extension_instead_of_settings(self):
        cmd = self._spawned_cmd("pi")
        self.assertTrue(cmd[0].endswith("pi"))
        self.assertNotIn("--settings", cmd)
        self.assertIn("-e", cmd)

    def test_audit_settings_follow_the_backend(self):
        """Two delivery channels, one hook: qodercli gets it as a --settings JSON
        on every turn, pi gets it from the -e extension that build_chat_cmd puts
        on the argv — hence None here, which no longer means the chain is off."""
        with _default_env():
            self.assertIn("Bash|Edit|Write", self.router._chat_audit_settings())
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            self.assertIsNone(self.router._chat_audit_settings())

    def _steps_reaching_the_resolver(self, call):
        seen = []

        def fake_model(step=None):
            seen.append(step)
            return ""

        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LOOP_ENGINE_")}

        def fake_run(cmd, **kwargs):
            return type("R", (), {"stdout": "req", "returncode": 0})()

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(agent_cli, "_qodercli_model",
                                  side_effect=fake_model), \
                mock.patch.object(self.router.subprocess, "run", fake_run):
            call()
        return seen

    def test_classify_turn_asks_for_its_own_step(self):
        """One-shot session, cheap answer: this is the turn that gains most from
        a flash tier, and the only one where a per-message model is genuinely
        independent (a resumed G turn keeps whichever model its first message
        picked)."""
        seen = self._steps_reaching_the_resolver(
            lambda: self.router._classify_requirement(
                "改个报错", [{"name": "req", "root": "/tmp/req"}]))
        self.assertEqual(seen, [agent_cli.CLASSIFY_STEP])

    def test_g_conversation_turn_asks_for_chat_step(self):
        seen = self._steps_reaching_the_resolver(
            lambda: self.router._run_llm_turn("sid-1", True, "PROMPT"))
        self.assertEqual(seen, [agent_cli.CHAT_STEP])

    def test_missing_bridge_warns_once(self):
        """The one state where the correction loop truly has nothing to read. It
        has to be said out loud, once, not swallowed by the None return."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LOOP_ENGINE_")}
        env["LOOP_ENGINE_AGENT_CLI"] = "pi"
        env["LOOP_ENGINE_PI_CHAT_EXTENSION"] = "/nonexistent/bridge.ts"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(self.router, "_audit_gap_warned", False), \
                mock.patch.object(self.router.logger, "warning") as warn:
            self.assertIsNone(self.router._chat_audit_settings())
            self.assertIsNone(self.router._chat_audit_settings())
        self.assertEqual(warn.call_count, 1)


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


class ModelConfigTest(unittest.TestCase):
    """agent_models.json decides which model each step pays for. The measurements
    behind the two rules it encodes (2026-09-09, qodercli 1.0.45): resuming a
    session without --model keeps the previous turn's model rather than
    re-reading settings.json, and an unknown --model exits 0 while serving the
    session from "auto". Half a config is therefore worse than none."""

    def setUp(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("LOOP_ENGINE_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        warned = mock.patch.object(agent_cli, "_WARNED", set())
        warned.start()
        self.addCleanup(warned.stop)

    def _cfg(self, payload, known="__unset__"):
        """Point the resolver at a fixture file. Fresh directory per call:
        _step_models caches on (backend, path), so a reused path would hand back
        the previous test's answer."""
        path = os.path.join(tempfile.mkdtemp(), "agent_models.json")
        if payload is not None:
            with open(path, "w") as f:
                if isinstance(payload, str):
                    f.write(payload)
                else:
                    json.dump(payload, f)
        patcher = mock.patch.dict(os.environ,
                                  {"LOOP_ENGINE_MODEL_CONFIG": path})
        patcher.start()
        self.addCleanup(patcher.stop)
        if known != "__unset__":
            kn = mock.patch.object(agent_cli, "_known_models",
                                   return_value=known)
            kn.start()
            self.addCleanup(kn.stop)
        return path

    def test_step_key_wins_and_unnamed_step_uses_default(self):
        self._cfg({"qodercli": {"default": "mid", "CHECKER": "deep"}},
                  known={"mid", "deep"})
        self.assertEqual(agent_cli._qodercli_model("CHECKER"), "deep")
        self.assertEqual(agent_cli._qodercli_model("SCORE"), "mid")
        self.assertEqual(agent_cli._qodercli_model(), "mid")

    def test_unrecognised_step_stays_inside_the_section(self):
        """A torn stdout must not send the next turn of the same session back to
        settings.json — that would be the silent drift the section exists to
        prevent."""
        self._cfg({"qodercli": {"default": "mid"}}, known={"mid"})
        with mock.patch.object(agent_cli, "_qodercli_settings_model",
                               return_value="elsewhere"):
            self.assertEqual(agent_cli._qodercli_model("NOT_AN_ACTION"), "mid")

    def test_sections_are_per_backend(self):
        self._cfg({"qodercli": {"default": "q", "CHAT": "qc"},
                   "pi": {"default": "p"}}, known={"q", "qc", "p"})
        self.assertEqual(agent_cli._pi_model("CHAT"), "p")
        self.assertEqual(agent_cli._qodercli_model("CHAT"), "qc")

    def test_settings_model_is_the_last_link_only_when_section_is_inert(self):
        self._cfg({"qodercli": {"default": "mid"}}, known={"mid"})
        with mock.patch.object(agent_cli, "_qodercli_settings_model",
                               return_value="from-settings") as s:
            self.assertEqual(agent_cli._qodercli_model("CHECKER"), "mid")
            s.assert_not_called()

    def test_step_models_without_default_disable_the_section(self):
        self._cfg({"qodercli": {"CHECKER": "deep"}}, known={"deep"})
        with mock.patch.object(agent_cli, "_qodercli_settings_model",
                               return_value="from-settings"):
            self.assertEqual(agent_cli._qodercli_model("CHECKER"),
                             "from-settings")

    def test_unknown_model_name_disables_the_section(self):
        self._cfg({"qodercli": {"default": "Typo"}})
        with mock.patch.object(agent_cli, "_known_models",
                               return_value={"qwen3.8-max", "qwen3.8-flash"}), \
                mock.patch.object(agent_cli, "_qodercli_settings_model",
                                  return_value="from-settings"):
            self.assertEqual(agent_cli._qodercli_model("CHECKER"),
                             "from-settings")

    def test_unknown_step_model_falls_back_to_default(self):
        self._cfg({"qodercli": {"default": "mid", "CHECKER": "Typo"}},
                  known={"mid"})
        self.assertEqual(agent_cli._qodercli_model("CHECKER"), "mid")

    def test_unaskable_backend_skips_validation(self):
        """--list-models failing means we don't know the catalog, not that every
        name is wrong."""
        self._cfg({"qodercli": {"default": "mid"}}, known=None)
        self.assertEqual(agent_cli._qodercli_model("CHECKER"), "mid")

    def test_catalog_is_one_call_per_backend_not_per_spawn(self):
        """Validation runs a real CLI, so it has to be paid once. A per-spawn
        lookup would add seconds to every step to re-derive an answer that
        cannot change inside one process. The first call here is the genuine
        one-off; everything after it must be a cache hit."""
        real = agent_cli._known_models("qodercli")

        def fail_run(cmd, **kwargs):
            raise AssertionError(f"second lookup re-ran {cmd}")

        with mock.patch.object(agent_cli.subprocess, "run", fail_run):
            again = agent_cli._known_models("qodercli")
        self.assertEqual(again, real)

    def test_corrupt_file_warns_once_and_stays_inert(self):
        self._cfg("{not json")
        with mock.patch.object(agent_cli, "_qodercli_settings_model",
                               return_value="from-settings"), \
                self.assertLogs("agent_cli", level="WARNING") as logs:
            for _ in range(3):
                self.assertEqual(agent_cli._qodercli_model("CHECKER"),
                                 "from-settings")
        self.assertEqual(len(logs.output), 1)

    def test_missing_file_is_not_a_condition_anyone_must_fix(self):
        self._cfg(None)
        with mock.patch.object(agent_cli, "_qodercli_settings_model",
                               return_value="from-settings"):
            self.assertEqual(agent_cli._qodercli_model("CHECKER"),
                             "from-settings")

    def test_shipped_config_leaves_argv_exactly_as_it_was(self):
        """The repo file carries the shape and no names, and no personal copy
        sits in the data dir. Together those must be indistinguishable from the
        file not existing at all — otherwise committing it changed how this
        machine spends its credits."""
        absent = os.path.join(tempfile.mkdtemp(), "absent")
        with _default_env(), \
                mock.patch.object(agent_cli, "DATA_DIR", absent), \
                mock.patch.object(agent_cli, "_session_file_exists",
                                  return_value=False), \
                mock.patch.object(agent_cli, "_qodercli_settings_model",
                                  return_value="settings-model"):
            with_step = agent_cli.build_cmd("/root/x", "sid-1", "S", "U",
                                            CHECKER)
            without = agent_cli.build_cmd("/root/x", "sid-1", "S", "U")
            self.assertEqual(with_step, without)
            self.assertEqual(with_step[with_step.index("--model") + 1],
                             "settings-model")

    def _personal(self, payload):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "agent_models.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        return d, path

    def test_personal_config_in_data_dir_needs_no_env(self):
        """The reason the data dir is searched: a machine's model names have to
        survive `self-install` and a restart without anyone remembering to
        export something. Missing that export is how a filled config silently
        stops being one."""
        d, path = self._personal({"qodercli": {"default": "personal"}})
        with mock.patch.object(agent_cli, "DATA_DIR", d), \
                mock.patch.object(agent_cli, "_known_models",
                                  return_value={"personal"}):
            self.assertEqual(agent_cli._model_config_path(), path)
            self.assertEqual(agent_cli._qodercli_model("CHECKER"), "personal")

    def test_named_env_does_not_fall_back_to_the_data_dir_copy(self):
        """LOOP_ENGINE_MODEL_CONFIG is a statement about which file decides. It
        pointing at nothing stays inert like any other missing file rather than
        quietly resolving from the data dir, where a different machine's names
        may be."""
        d, _ = self._personal({"qodercli": {"default": "personal"}})
        missing = os.path.join(tempfile.mkdtemp(), "gone.json")
        with mock.patch.object(agent_cli, "DATA_DIR", d), \
                mock.patch.dict(os.environ,
                                {"LOOP_ENGINE_MODEL_CONFIG": missing}):
            self.assertEqual(agent_cli._model_config_path(), missing)
            self.assertEqual(agent_cli._configured_model("qodercli",
                                                         "CHECKER"), "")

    def test_repo_template_is_the_last_link(self):
        absent = os.path.join(tempfile.mkdtemp(), "absent")
        with mock.patch.object(agent_cli, "DATA_DIR", absent):
            self.assertEqual(agent_cli._model_config_path(),
                             agent_cli._MODEL_CONFIG)


class ChatStepTest(unittest.TestCase):
    """The classification turn is a one-shot session; G replies are a resumed
    conversation. Only the first is genuinely per-message, so they must not be
    forced to share one model."""

    def _cfg(self, payload):
        path = os.path.join(tempfile.mkdtemp(), "agent_models.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        return mock.patch.dict(os.environ, {
            "LOOP_ENGINE_MODEL_CONFIG": path,
            "LOOP_ENGINE_AGENT_CLI": "qodercli"})

    def test_classify_turn_and_g_turn_resolve_differently(self):
        cfg = {"qodercli": {"default": "deep",
                            "CLASSIFY_REQUIREMENT": "flash"}}
        with self._cfg(cfg), \
                mock.patch.object(agent_cli, "_known_models",
                                  return_value={"deep", "flash"}):
            classify = agent_cli.build_chat_cmd(
                "sid-1", True, None, step=agent_cli.CLASSIFY_STEP)
            chat = agent_cli.build_chat_cmd("sid-1", True, None)
        self.assertEqual(classify[classify.index("--model") + 1], "flash")
        self.assertEqual(chat[chat.index("--model") + 1], "deep")

    def test_g_turn_argv_unchanged_when_nothing_is_configured(self):
        with _default_env(), \
                mock.patch.object(agent_cli, "_qodercli_settings_model",
                                  return_value="settings-model"):
            classify = agent_cli.build_chat_cmd("sid-1", True, "SET",
                                                step=agent_cli.CLASSIFY_STEP)
            self.assertEqual(
                classify,
                [classify[0], "--print", "--session-id", "sid-1",
                 "--model", "settings-model",
                 "--dangerously-skip-permissions", "--settings", "SET"])
            self.assertEqual(agent_cli.build_chat_cmd("sid-1", True, "SET"),
                             classify)


class SessionProbeTest(unittest.TestCase):
    """P5: a probe that misses is not neutral — the step then gets
    --session-id for a session that already exists and qodercli exits 42."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        patcher = mock.patch.object(
            agent_cli.os.path, "expanduser",
            side_effect=lambda p: p.replace("~", self.home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_session(self, slug_cwd, sid):
        # qodercli's own slug comes from the path it booted in, which is the
        # resolved one (macOS tempdirs live under /var → /private/var)
        processed = os.path.realpath(slug_cwd).replace("/", "-").replace(".", "-")
        d = os.path.join(self.home, ".qoder", "projects", processed)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sid}.jsonl"), "w") as f:
            f.write("{}\n")

    def test_finds_session_of_a_symlinked_root(self):
        real = os.path.join(self.tmp.name, "real-ws")
        os.makedirs(real)
        link = os.path.join(self.tmp.name, "link-ws")
        os.symlink(real, link)
        self._write_session(real, "sid-1")
        self.assertTrue(agent_cli._session_file_exists("sid-1", link))

    def test_missing_session_stays_missing(self):
        real = os.path.join(self.tmp.name, "real-ws")
        os.makedirs(real)
        self.assertFalse(agent_cli._session_file_exists("sid-1", real))


class SessionDirsTest(unittest.TestCase):
    def test_covers_every_backend_regardless_of_selection(self):
        """P4: the weekly cron cleaned only qodercli, so pi sessions grew
        unbounded. Backend selection is per-spawn; disk hygiene is not."""
        expected = {os.path.expanduser("~/.qoder/projects"),
                    os.path.expanduser("~/.pi/agent/sessions")}
        with _default_env():
            self.assertEqual(set(agent_cli.session_dirs()), expected)
            os.environ["LOOP_ENGINE_AGENT_CLI"] = "pi"
            self.assertEqual(set(agent_cli.session_dirs()), expected)


if __name__ == "__main__":
    unittest.main()
