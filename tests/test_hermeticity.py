"""The suite must never touch this machine's runtime ledgers.

conftest sets LOOP_ENGINE_DATA_DIR before the first import because every module
derives its ledger path from constants.DATA_DIR at import time. Drop that line
and these assertions fail — which is the point: the alternative is a green suite
that silently appends fake entries to schedule.log and audit.log.
"""
import os
import tempfile
import unittest

import constants
import scheduler


class LedgerIsolationTest(unittest.TestCase):
    def setUp(self):
        self.home = os.path.expanduser("~/.qoder/loop_engine")

    def test_ledger_paths_are_not_under_home(self):
        for label, path in (("DATA_DIR", constants.DATA_DIR),
                            ("LOG_PATH", scheduler.LOG_PATH),
                            ("RUNS_PATH", scheduler.RUNS_PATH),
                            ("PENDING_PATH", scheduler.PENDING_PATH),
                            ("REGISTRY_PATH", scheduler.REGISTRY_PATH)):
            self.assertFalse(
                os.path.abspath(path).startswith(self.home + os.sep),
                f"{label} points into the real data dir: {path}")

    def test_wecom_router_audit_line_lands_in_the_temp_ledger(self):
        # The one writer that resolves DATA_DIR at call time, so patching the
        # module attribute is enough to prove the seam without a real session.
        from wecom_server import router
        tmp = tempfile.mkdtemp()
        old = router.DATA_DIR
        try:
            router.DATA_DIR = tmp
            router._audit_line("hermeticity probe")
            with open(os.path.join(tmp, "audit.log")) as f:
                self.assertIn("hermeticity probe", f.read())
            real = os.path.join(self.home, "audit.log")
            if os.path.exists(real):        # absent on a fresh install
                with open(real) as f:
                    self.assertNotIn("hermeticity probe", f.read())
        finally:
            router.DATA_DIR = old


if __name__ == "__main__":
    unittest.main()
