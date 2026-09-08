"""Tests for agent_cli.py — the loop-path spawn seam (backend switch + argv contract)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_cli


class BuildCmdTest(unittest.TestCase):
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
        """Setting pi must not silently fall back to qodercli — the caller
        would believe it was testing pi while running the old CLI."""
        with mock.patch.dict(os.environ, {"LOOP_ENGINE_AGENT_CLI": "pi"}):
            with self.assertRaises(RuntimeError) as ctx:
                agent_cli.build_cmd("/root/x", "sid-1", "S", "U")
        self.assertIn("qodercli", str(ctx.exception))

    def test_default_backend_is_qodercli(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(agent_cli.backend(), "qodercli")


if __name__ == "__main__":
    unittest.main()
