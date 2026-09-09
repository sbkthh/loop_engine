"""Tests for the pi audit bridge (wecom_server/hooks/pi_audit_bridge.ts).

The bridge is loaded by pi through `-e`; these drive its pure mapping function
via `node <file> --map` and then feed the result to the real shell hook, so pi
itself is never spawned.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.test_audit_hook import HOOK, SESSION_UUID, _read, _run

BRIDGE = os.path.join(os.path.dirname(__file__), "..", "wecom_server",
                      "hooks", "pi_audit_bridge.ts")
NODE = shutil.which("node")


def _map(tool_name, tool_input, cwd="/req", session_id=SESSION_UUID):
    r = subprocess.run(
        [NODE, BRIDGE, "--map"], capture_output=True, text=True,
        input=json.dumps({"toolName": tool_name, "input": tool_input,
                          "cwd": cwd, "sessionId": session_id}))
    if not r.stdout:
        raise unittest.SkipTest("node cannot run the .ts bridge: " + r.stderr[-200:])
    return None if r.stdout == "null" else json.loads(r.stdout)


@unittest.skipIf(not NODE, "node not on PATH")
class PiAuditBridgeTest(unittest.TestCase):

    def test_bash_maps_to_command(self):
        self.assertEqual(_map("bash", {"command": "git push origin main"}), {
            "session_id": SESSION_UUID, "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"}})

    def test_edit_path_is_resolved_against_ctx_cwd(self):
        """Guard 1's regex needs a leading slash; an unresolved relative
        pending.json would sail past it."""
        mapped = _map("edit", {"path": "pending.json"}, cwd="/work/req")
        self.assertEqual(mapped["tool_input"]["file_path"], "/work/req/pending.json")
        r, _, _ = _run(mapped["tool_input"], mapped["session_id"], mapped["tool_name"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("已阻止", r.stdout)

    def test_write_expands_home(self):
        """node's path.resolve keeps a literal `~` segment; pi's own tool does
        not, so the bridge has to mirror that expansion."""
        mapped = _map("write", {"path": "~/notes.md"})
        self.assertEqual(mapped["tool_input"]["file_path"],
                         os.path.join(os.path.expanduser("~"), "notes.md"))

    def test_unmapped_tools_pass_through_as_null(self):
        for name in ("read", "grep", "custom_thing", "constructor"):
            self.assertIsNone(_map(name, {"path": "/req/.loop/state.json"}), name)

    def test_missing_path_stays_empty(self):
        self.assertEqual(_map("edit", {})["tool_input"]["file_path"], "")

    def test_mapped_edit_of_state_json_hits_shell_guard(self):
        mapped = _map("edit", {"path": "/req/.loop/state.json"})
        r, log, _ = _run(mapped["tool_input"], mapped["session_id"], mapped["tool_name"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("已阻止", r.stdout)
        self.assertIn("BLOCKED", _read(log))

    def test_mapped_edit_of_spec_md_produces_router_readable_snapshot(self):
        from wecom_server.router import _SPEC_SNAP_RE
        tmp = tempfile.mkdtemp()
        spec = os.path.join(tmp, "specs", "opc-sna", "spec.md")
        os.makedirs(os.path.dirname(spec))
        with open(spec, "w") as f:
            f.write("# Spec\n\nScenario: 对账\n")

        mapped = _map("edit", {"path": spec}, cwd=tmp)
        r, _, snap_dir = _run(mapped["tool_input"], mapped["session_id"],
                              mapped["tool_name"])
        self.assertEqual(r.returncode, 0, r.stderr)
        names = os.listdir(snap_dir)
        self.assertEqual(len(names), 1, names)
        self.assertTrue(_SPEC_SNAP_RE.match(names[0]), names[0])
        self.assertIn("对账", _read(os.path.join(snap_dir, names[0])))


if __name__ == "__main__":
    unittest.main()
