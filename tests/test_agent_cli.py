"""Tests for agent_cli.py — the loop-path spawn seam (backend switch + argv contract)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_cli


def _default_env():
    """Env without LOOP_ENGINE_AGENT_CLI but with everything else intact —
    clear=True would drop HOME too, and expanduser('~') then falls back to pwd,
    which differs whenever the suite runs under a substituted HOME."""
    env = {k: v for k, v in os.environ.items() if k != "LOOP_ENGINE_AGENT_CLI"}
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

    def test_pi_backend_reaches_no_spawn_fallback(self):
        """pi is a known profile with no argv builder yet: it must refuse to
        spawn, never quietly run qodercli under a pi label."""
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            with self.assertRaises(RuntimeError) as ctx:
                agent_cli.build_cmd("/root/x", "sid-1", "S", "U")
        self.assertIn("qodercli", str(ctx.exception))

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


if __name__ == "__main__":
    unittest.main()
