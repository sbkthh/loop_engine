"""Suite-wide hermeticity: no test may read or write this machine's home.

Two separate hazards, two mechanisms:

1. `constants.DATA_DIR` resolves `LOOP_ENGINE_DATA_DIR` **at import**, and every
   module derives its ledger path from it right there (scheduler.LOG_PATH,
   audit.log, runs.json, pending.json, schedule.json). A suite run that leaves
   that env unset appends fake entries to the real ones — schedule.log picked up
   four `run_spec: forked req --module c/m (pid 4242)` lines in one day, all of
   them from `tests/test_router_async.py`, and cost diagnosis time twice. Those
   files are append-only audit trails, so writing to them is never a harmless
   side effect. Hence the env is pinned to a temp dir before the first import.

2. `agent_cli._model_config_path()` prefers <DATA_DIR>/agent_models.json, and on
   a developer box that file carries real model names — which would silently
   decide the `--model` in every argv assertion. Those assertions describe the
   shipped shape, so the fixture below pins the seam to a path that cannot
   exist. A test that wants a config file sets LOOP_ENGINE_MODEL_CONFIG or
   patches DATA_DIR itself, which runs after this fixture.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LOOP_ENGINE_DATA_DIR"] = tempfile.mkdtemp(prefix="loop-engine-tests-")

import agent_cli  # noqa: E402
import pytest  # noqa: E402
import spec_utils  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    absent = os.path.join(tempfile.mkdtemp(), "absent")
    monkeypatch.setattr(agent_cli, "DATA_DIR", absent)
    # directives.build resolves prev-spec baselines out of <DATA_DIR>/spec-snapshots
    monkeypatch.setattr(spec_utils, "DATA_DIR", absent)
    monkeypatch.delenv("LOOP_ENGINE_MODEL_CONFIG", raising=False)
