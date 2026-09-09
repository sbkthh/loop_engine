"""Suite-wide hermeticity: no test may read this machine's home directory.

`agent_cli._model_config_path()` prefers <DATA_DIR>/agent_models.json, and on a
developer box that file carries real model names — which would silently decide
the `--model` in every argv assertion. Those assertions describe the shipped
shape, so the seam is pinned to a path that cannot exist. A test that wants a
config file sets LOOP_ENGINE_MODEL_CONFIG or patches DATA_DIR itself, which
runs after this fixture.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_cli  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    absent = os.path.join(tempfile.mkdtemp(), "absent")
    monkeypatch.setattr(agent_cli, "DATA_DIR", absent)
    monkeypatch.delenv("LOOP_ENGINE_MODEL_CONFIG", raising=False)
