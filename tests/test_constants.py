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
