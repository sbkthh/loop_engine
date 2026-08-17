"""Tests for constants.py — STATUS_TABLE completeness and derivation."""


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
