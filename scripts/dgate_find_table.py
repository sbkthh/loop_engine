#!/usr/bin/env python3
"""Find which *granted* dgate database contains a table.

Candidates come from `dgate acl list --mine` (authorization), never from
`dgate meta database list` (inventory). Querying an inventory database is how
the chat bot kept hitting ACCESS_DENIED on databases it was never granted.

    python3 scripts/dgate_find_table.py --env uat --table tb_xxx
"""
import argparse
import json
import subprocess
import sys

DGATE = "dgate"


def _json(args, run=subprocess.run):
    r = run([DGATE, *args, "-o", "json"], capture_output=True, text=True,
            timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:500])
    return json.loads(r.stdout)


def granted_databases(env, run=subprocess.run):
    # ponytail: first 100 grants cover a personal account; add nextToken
    # paging if the list ever grows past that.
    data = _json(["acl", "list", "--mine", "--max-results", "100"], run)
    found, seen = [], set()
    for grant in data.get("data") or []:
        if (grant.get("effect") or "").lower() != "allow":
            continue
        if "read" not in (grant.get("permType") or "").lower():
            continue
        if env and (grant.get("catalogEnvType") or "").lower() != env:
            continue
        db = grant.get("databaseUuid")
        if not db or db in seen:
            continue
        seen.add(db)
        found.append({
            "databaseUuid": db,
            "catalogUuid": grant.get("catalogUuid"),
            "catalogAlias": grant.get("catalogAlias"),
            "qualifiedName": grant.get("qualifiedName"),
            "env": grant.get("catalogEnvType"),
        })
    return found


def find_table(table, databases, run=subprocess.run):
    for db in databases:
        data = _json(["meta", "table", "list", db["databaseUuid"],
                      "--keyword", table, "--max-results", "100"], run)
        names = {t.get("name") for t in data.get("data") or []}
        if table in names:
            return db
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--env", choices=["uat", "product"], default="uat")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        databases = granted_databases(args.env)
        hit = find_table(args.table, databases)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"dgate lookup failed: {e}", file=sys.stderr)
        return 2
    if not hit:
        names = ", ".join(db["qualifiedName"] or db["databaseUuid"]
                          for db in databases) or "(none)"
        print(f"{args.table}: not found in granted {args.env} databases: "
              f"{names}")
        return 1
    if args.json:
        print(json.dumps(hit, ensure_ascii=False, indent=2))
    else:
        print(f"{args.table} -> {hit['qualifiedName']} "
              f"(database-uuid={hit['databaseUuid']}, "
              f"catalog-uuid={hit['catalogUuid']}, env={hit['env']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
