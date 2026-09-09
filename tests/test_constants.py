"""Tests for constants.py — STATUS_TABLE completeness and derivation."""

import json
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_table_covers_all_statuses():
    from constants import ALL_STATUSES, STATUS_TABLE

    assert set(ALL_STATUSES) == set(STATUS_TABLE)


def test_each_row_has_required_fields():
    from constants import STATUS_TABLE

    required = {"next", "auto_exec", "trigger", "label", "idle_msg",
                "prefixes"}
    for status, row in STATUS_TABLE.items():
        assert required <= set(row), status


def test_priority_order_derived_from_table():
    from constants import PRIORITY_ORDER, STATUS_TABLE

    assert PRIORITY_ORDER == tuple(STATUS_TABLE)


def test_step_keys_cover_every_spawn():
    """agent_models.json is keyed by these. A loop action missing from the
    tuple would still spawn, just on an unconfigurable model — the failure is
    silent, so it has to be a test failure instead."""
    from constants import ALL_ACTIONS, ALL_STEP_KEYS, STATUS_TABLE

    assert set(ALL_ACTIONS) <= set(ALL_STEP_KEYS)
    assert len(ALL_STEP_KEYS) == len(set(ALL_STEP_KEYS))
    # every step the machine routes to is a step someone can name a model for
    # (None = a terminal row that never spawns anything)
    assert {row["next"] for row in STATUS_TABLE.values()
            if row["next"]} <= set(ALL_ACTIONS)


def test_shipped_model_config_names_known_steps_only():
    """A key the resolver never reads looks like configuration and behaves like
    a comment; the mirror checkout cannot tell the difference."""
    from constants import ALL_STEP_KEYS

    path = os.path.join(_REPO_ROOT, "agent_models.json")
    with open(path) as f:
        cfg = json.load(f)

    assert set(cfg) <= {"_comment", "qodercli", "pi"}
    for backend, section in cfg.items():
        if backend == "_comment":
            continue
        assert set(section) <= set(ALL_STEP_KEYS) | {"default"}, backend
        # nothing committed but the shape: a vendor name here would be pushed
        # into every mirror checkout and be wrong on most of them
        assert not [v for v in section.values() if v], backend


def test_data_dir_is_named_in_exactly_one_place():
    """The relocatability below only holds while one module spells the path. A
    second literal keeps pointing at ~/.qoder after the move, and the two ends
    of a guard — audit_hook.sh writing snapshots, router reading them — then
    live in different directories with nothing failing."""
    import re

    hits = []
    for entry in sorted(os.listdir(_REPO_ROOT)):
        if not entry.endswith(".py"):
            continue
        with open(os.path.join(_REPO_ROOT, entry)) as f:
            src = re.sub(r"\s+", " ", f.read())
        if 'expanduser( "~/.qoder/loop_engine"' in src:
            hits.append(entry)
    assert hits == ["constants.py"]


def test_one_env_relocates_every_derived_path(tmp_path):
    """A subprocess, because these are import-time bindings — which is exactly
    when a real run resolves them. Patching them in-process would prove nothing
    about the env this feature is for."""
    import json
    import subprocess
    import sys

    data = str(tmp_path)
    # the personal model config is the file most likely to be forgotten
    with open(os.path.join(data, "agent_models.json"), "w") as f:
        json.dump({"qodercli": {"default": ""}}, f)
    code = (
        "import json, constants, registry, scheduler, agent_cli;"
        "from wecom_server import router;from feishu_server import feishu_api;"
        "print(json.dumps({"
        "'registry': registry.REGISTRY_PATH,"
        "'pending': scheduler.PENDING_PATH,"
        "'schedule': scheduler.CONFIG_PATH,"
        "'log': scheduler.LOG_PATH,"
        "'runs': scheduler.RUNS_PATH,"
        "'snap': router._SPEC_SNAP_DIR,"
        "'sessions': router._SESSION_DIR,"
        "'files': feishu_api._FILES_DIR,"
        "'models': agent_cli._model_config_path()}))"
    )
    env = dict(os.environ, LOOP_ENGINE_DATA_DIR=data,
               LOOP_ENGINE_MODEL_CONFIG="")
    r = subprocess.run([sys.executable, "-c", code], cwd=_REPO_ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name, path in json.loads(r.stdout).items():
        assert path.startswith(data + os.sep), (name, path)
