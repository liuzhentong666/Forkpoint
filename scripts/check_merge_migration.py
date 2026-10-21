"""Check sql/004_merge_batch.sql on an installation made with the old vc.merge.

Existing local PostgreSQL only: creates one dedicated agentdb_test_mergemig_*
database, installs the schema with vc.merge from an earlier commit (one
vc.rows_at() call per changed row), builds history, records how merges resolve
changed rows, applies `db.py install-merge-batch` twice and checks that data,
history, grants and every recorded merge outcome are unchanged. Never writes to
the project database and never starts or stops the server.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import add_connection_arguments
from review_local import connect, seed
import run_tests

OLD_LOOKUP = "SELECT r.doc INTO ours FROM vc.rows_at(ctx.base,t.id) r WHERE r.pk=d.pk;"
MERGE = "'vc.merge(text,uuid,uuid,text)'::regprocedure"


def merge_body(source):
    match = re.search(r"FUNCTION vc\.merge\(.*?AS \$\$(.*?)\$\$;", source, re.S)
    return match.group(1)


def install_old(owner, database, old_core):
    # Same as Connection.install before 004 existed: 001-003 in one transaction.
    parts = [old_core] + [(ROOT / "sql" / f).read_text(encoding="utf-8")
                          for f in ("002_demo_app.sql", "003_manager_role.sql")]
    parts = [re.sub(r"(?m)^BEGIN;\s*$|^COMMIT;\s*$", "", p) for p in parts]
    owner.sql("BEGIN;\n" + "\n".join(parts) + "\nCOMMIT;", database)


def fingerprint(owner):
    return json.loads(owner.sql("""
      SELECT jsonb_build_object(
        'products',(SELECT md5(coalesce(string_agg(to_jsonb(p)::text,',' ORDER BY id),'')) FROM app.products p),
        'orders',(SELECT md5(coalesce(string_agg(to_jsonb(o)::text,',' ORDER BY id),'')) FROM app.orders o),
        'branches',(SELECT md5(string_agg(to_jsonb(b)::text,',' ORDER BY name)) FROM vc.branch b),
        'changesets',(SELECT md5(string_agg(to_jsonb(c)::text,',' ORDER BY id)) FROM vc.changeset c),
        'parents',(SELECT md5(string_agg(to_jsonb(p)::text,',' ORDER BY child,ordinal)) FROM vc.parent p),
        'row_changes',(SELECT md5(string_agg(to_jsonb(r)::text,',' ORDER BY changeset,table_id,pk::text)) FROM vc.row_change r),
        'path_changes',(SELECT md5(string_agg(to_jsonb(p)::text,',' ORDER BY changeset,path)) FROM vc.path_change p),
        'snapshot_rows',(SELECT count(*) FROM vc.snapshot_row),
        'counts',jsonb_build_object('changesets',(SELECT count(*) FROM vc.changeset),
          'merge_commits',(SELECT count(*) FROM vc.parent WHERE ordinal=2),
          'row_changes',(SELECT count(*) FROM vc.row_change)));
    """))


def function_state(owner):
    return json.loads(owner.sql(f"""
      SELECT jsonb_build_object('owner',pg_get_userbyid(proowner),'security_definer',prosecdef,
        'config',proconfig,'acl',proacl::text[],'volatility',provolatile,
        'client_can_execute',has_function_privilege('agentdb_client',{MERGE},'EXECUTE'),
        'public_can_execute',has_function_privilege('public',{MERGE},'EXECUTE'),
        'manager_can_execute',has_function_privilege('agentdb_manager',{MERGE},'EXECUTE'))
      FROM pg_proc WHERE oid={MERGE};
    """))


def source_of_merge(owner):
    return json.loads(owner.sql(f"SELECT to_jsonb(prosrc) FROM pg_proc WHERE oid={MERGE};"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--old-ref", default="8aeb3d7",
                        help="git commit whose sql/001_core.sql has the per-row vc.merge (default 8aeb3d7)")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "merge-batch-migration-results.json")
    args = parser.parse_args()
    old_core = subprocess.run(["git", "show", f"{args.old_ref}:sql/001_core.sql"], cwd=ROOT, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    assert OLD_LOOKUP in old_core, "--old-ref does not contain the per-row vc.merge"
    new_core = (ROOT / "sql/001_core.sql").read_text(encoding="utf-8")
    migration = (ROOT / "sql/004_merge_batch.sql").read_text(encoding="utf-8")
    # A fresh install runs 001 then 004; both must define the same vc.merge.
    assert merge_body(new_core) == merge_body(migration), "vc.merge in 001_core.sql and 004_merge_batch.sql differ"

    database = "agentdb_test_mergemig_" + uuid.uuid4().hex[:10]
    owner = connect(args, database)
    details = owner.check()
    owner.create_database(database)
    install_old(owner, database, old_core)
    seed(owner)
    print("Old-schema test database: " + database, flush=True)
    assert source_of_merge(owner) == merge_body(old_core)
    run_tests.CONNECTION, run_tests.DBNAME = owner, database
    # History an installation in use would have: the original assertions plus a committed merge.
    regression_old = run_tests.test_suite()
    owner.sql("""
      SELECT vc.fork('pre-migration',vc.head()) AS base \\gset
      BEGIN; SELECT vc.begin_change('pre-migration',:'base');
      UPDATE agent.products SET stock=55 WHERE id=3500; SELECT vc.finish_change() AS revision \\gset
      SELECT vc.merge('main',:'revision',vc.head()); COMMIT;
    """)
    cases_old = run_tests.merge_semantics()
    data_before, function_before = fingerprint(owner), function_state(owner)

    for _ in range(2):  # rerunnable
        owner.install_merge_batch(database)
    data_after, function_after = fingerprint(owner), function_state(owner)
    assert data_after == data_before, (data_before, data_after)
    # 004's definition lists pg_temp last in search_path; every other attribute is unchanged.
    expected = dict(function_before, config=["search_path=pg_catalog, vc, pg_temp"])
    assert function_after == expected, (expected, function_after)
    assert source_of_merge(owner) == merge_body(new_core)
    print("PASS migration keeps data, history, owner and grants; search_path gains pg_temp", flush=True)

    cases_new = run_tests.merge_semantics()
    assert cases_new == cases_old, json.dumps(dict(old=cases_old, new=cases_new), indent=1)
    print("PASS every recorded merge outcome matches the old vc.merge", flush=True)
    regression_new = run_tests.test_suite()
    worker = connect(args, database, "agentdb_client")
    merged = worker.sql("""
      BEGIN; SELECT vc.fork('client-merge',vc.head()) AS base \\gset
      SELECT vc.begin_change('client-merge',:'base') AS draft \\gset
      UPDATE agent.products SET stock=56 WHERE id=3501; SELECT vc.finish_change() AS revision \\gset
      SELECT vc.merge('main',:'revision',vc.head()) IS NOT NULL; ROLLBACK;
    """)
    assert merged == "t", merged
    print("PASS agentdb_client still merges after migration", flush=True)
    assert fingerprint(owner) == data_before  # the checks above all rolled back

    result = dict(database=database, postgresql=details["server_version"], old_ref=args.old_ref,
                  migration="sql/004_merge_batch.sql via Connection.install_merge_batch (db.py install-merge-batch)",
                  migration_runs=2, regression_tests_before=len(regression_old),
                  regression_tests_after=len(regression_new), data_and_history_unchanged=True,
                  function_attributes_unchanged_except_search_path=True, function=function_after, before=data_before,
                  merge_cases=len(cases_new), merge_outcomes_identical=True, client_merge_after_migration=True,
                  real_cloud_verified=False,
                  note="Local dedicated test database on the existing PostgreSQL server; the project database was not touched.")
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("before", "function")}, indent=2))


if __name__ == "__main__":
    main()
