"""Check sql/007_search_path_pg_temp.sql on an installation made before it existed.

Existing local PostgreSQL only. Creates agentdb_test_spmig_* with the schema of
--old-ref (001-006 as Connection.install applied them then) and seeds it. It
records data, history and the attributes of every function in vc, applies
`db.py install-search-path-hardening` twice, and checks that data and history
are unchanged, that every function keeps its owner, grants, volatility and body,
that only the search_path of the SECURITY DEFINER functions changed (to
pg_catalog, vc, pg_temp), and that the functions equal a fresh install's
(agentdb_test_spnew_*). Then the original assertions of run_tests.test_suite
and an agentdb_client merge (rolled back) run on the migrated database.
Never writes to the project database and never starts or stops the server.
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

from check_history_migration import function_states
from check_merge_migration import fingerprint
from db import add_connection_arguments
from review_local import connect, seed
import run_tests

OLD_FILES = ("001_core.sql", "002_demo_app.sql", "003_manager_role.sql", "004_merge_batch.sql",
             "005_bounded_history.sql", "006_history_health.sql")
BEFORE, AFTER = ["search_path=pg_catalog, vc"], ["search_path=pg_catalog, vc, pg_temp"]


def signatures(owner):
    return json.loads(owner.sql("""
      SELECT json_agg(p.oid::regprocedure::text ORDER BY p.oid::regprocedure::text) FROM pg_proc p
      JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='vc';"""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--old-ref", default="a7e60d9",
                        help="git commit whose sql/ predates 007_search_path_pg_temp.sql (default a7e60d9)")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "search-path-migration-results.json")
    args = parser.parse_args()
    def old_file(name):
        return subprocess.run(["git", "show", f"{args.old_ref}:sql/{name}"], cwd=ROOT, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    newer = subprocess.run(["git", "cat-file", "-e", f"{args.old_ref}:sql/007_search_path_pg_temp.sql"],
                           cwd=ROOT, capture_output=True)
    assert newer.returncode != 0, "--old-ref already contains 007_search_path_pg_temp.sql"
    old_schema = "\n".join(re.sub(r"(?m)^BEGIN;\s*$|^COMMIT;\s*$", "", old_file(name)) for name in OLD_FILES)

    suffix = uuid.uuid4().hex[:10]
    names = dict(migrated="agentdb_test_spmig_" + suffix, fresh="agentdb_test_spnew_" + suffix)
    owner = connect(args, names["migrated"])
    details = owner.check()
    owner.create_database(names["migrated"])
    owner.sql("BEGIN;\n" + old_schema + "\nCOMMIT;")  # as Connection.install did before 007 existed
    seed(owner)
    print("Old-schema test database: " + names["migrated"], flush=True)

    functions = signatures(owner)
    data_before = fingerprint(owner)
    states_before = function_states(owner, functions)
    definers = sorted(name for name, state in states_before.items() if state["security_definer"])
    assert definers and all(states_before[name]["config"] == BEFORE for name in definers), states_before

    for _ in range(2):  # rerunnable
        owner.install_search_path_hardening()
    assert fingerprint(owner) == data_before
    assert signatures(owner) == functions
    states_after = function_states(owner, functions)
    for name in functions:
        expected = dict(states_before[name], config=AFTER) if name in definers else states_before[name]
        assert states_after[name] == expected, (name, states_before[name], states_after[name])
    print(f"PASS migration keeps data, history and {len(functions)} functions; only the search_path of "
          f"{len(definers)} SECURITY DEFINER functions changed", flush=True)

    owner.create_database(names["fresh"])
    owner.install(names["fresh"])
    fresh_owner = connect(args, names["fresh"])
    assert signatures(fresh_owner) == functions
    assert function_states(fresh_owner, functions) == states_after
    print("PASS migrated functions equal a fresh install's", flush=True)

    run_tests.CONNECTION, run_tests.DBNAME = owner, names["migrated"]
    regression = run_tests.test_suite()
    worker = connect(args, names["migrated"], "agentdb_client")
    merged = worker.sql("""
      BEGIN; SELECT vc.fork('client-merge',vc.head()) AS base \\gset
      SELECT vc.begin_change('client-merge',:'base') AS draft \\gset
      UPDATE agent.products SET stock=56 WHERE id=3501; SELECT vc.write_file('client.txt','x') AS blob \\gset
      SELECT vc.finish_change() AS revision \\gset
      SELECT vc.merge('main',:'revision',vc.head()) IS NOT NULL; ROLLBACK;
    """)
    assert merged == "t", merged
    assert fingerprint(owner) == data_before  # the checks above all rolled back
    print(f"PASS {len(regression)} original assertions and an agentdb_client merge after migration", flush=True)

    result = dict(databases=names, postgresql=details["server_version"], old_ref=args.old_ref,
                  migration="sql/007_search_path_pg_temp.sql via Connection.install_search_path_hardening "
                            "(db.py install-search-path-hardening)",
                  migration_runs=2, functions_checked=len(functions), security_definer_functions=definers,
                  search_path_before=BEFORE, search_path_after=AFTER, data_and_history_unchanged=True,
                  other_function_attributes_unchanged=True, functions_equal_fresh_install=True,
                  regression_tests_after=len(regression), client_merge_after_migration=True,
                  real_cloud_verified=False,
                  note=("Local dedicated test databases on the existing PostgreSQL server; the project database "
                        "was not touched."))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "security_definer_functions"}, indent=2))


if __name__ == "__main__":
    main()
