"""Tests for scripts/dgate_find_table.py (granted-database table lookup)."""
import importlib.util
import json
import os
from types import SimpleNamespace

PATH = os.path.join(os.path.dirname(__file__), "..", "scripts",
                    "dgate_find_table.py")
spec = importlib.util.spec_from_file_location("dgate_find_table", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

GRANTS = {"data": [
    {"effect": "Allow", "permType": "read", "catalogEnvType": "uat",
     "databaseUuid": "db-oms", "catalogUuid": "c-uat",
     "catalogAlias": "UAT", "qualifiedName": "zkh_oms_config"},
    {"effect": "Allow", "permType": "read", "catalogEnvType": "uat",
     "databaseUuid": "db-flux", "catalogUuid": "c-uat",
     "catalogAlias": "UAT", "qualifiedName": "fluxwmsdb"},
    {"effect": "Allow", "permType": "read", "catalogEnvType": "product",
     "databaseUuid": "db-prod", "catalogUuid": "c-prod",
     "catalogAlias": "OMS", "qualifiedName": "zkh_oms_config"},
    {"effect": "Deny", "permType": "read", "catalogEnvType": "uat",
     "databaseUuid": "db-denied", "catalogUuid": "c-uat",
     "catalogAlias": "UAT", "qualifiedName": "boss-service"},
]}


def _runner(tables_by_db):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[1:])
        joined = " ".join(cmd[1:])
        if "acl list --mine" in joined:
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps(GRANTS), stderr="")
        for db, tables in tables_by_db.items():
            if f"meta table list {db}" in joined:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"data": [{"name": t} for t in tables]}),
                    stderr="")
        return SimpleNamespace(returncode=1, stdout="",
                               stderr=f"unexpected: {joined}")

    return run, calls


def test_candidates_are_only_granted_databases_in_env():
    run, _ = _runner({})
    dbs = mod.granted_databases("uat", run)
    assert [d["databaseUuid"] for d in dbs] == ["db-oms", "db-flux"]


def test_finds_first_granted_database_containing_table():
    run, calls = _runner({"db-oms": [], "db-flux": ["tb_target"]})
    dbs = mod.granted_databases("uat", run)
    hit = mod.find_table("tb_target", dbs, run)
    assert hit["databaseUuid"] == "db-flux"
    assert all("db-prod" not in c for c in calls)
    assert all("db-denied" not in c for c in calls)


def test_returns_none_when_no_granted_database_has_table():
    run, _ = _runner({"db-oms": [], "db-flux": []})
    dbs = mod.granted_databases("uat", run)
    assert mod.find_table("tb_missing", dbs, run) is None
