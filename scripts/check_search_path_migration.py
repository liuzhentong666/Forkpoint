"""Check sql/007_search_path_pg_temp.sql on an installation made before it existed.

Existing local PostgreSQL only. Creates agentdb_test_spmig_* with the schema of
--old-ref (001-006 as Connection.install applied them then) and seeds it. It
records data, history and the attributes of every function in vc, applies
`db.py install-search-path-hardening` twice, and checks that data and history
are unchanged, that every function keeps its owner, grants, volatility and body,
that only the search_path of the SECURITY DEFINER functions changed (to
pg_catalog, vc, pg_temp), and that the functions equal a fresh install's
(agentdb_test_spnew_*), on which run_tests.py runs its assertions.
Never writes to the project database and never starts or stops the server.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from check_history_migration import function_states, load_old_schema
from check_merge_migration import fingerprint
from db import add_connection_arguments
from review_local import connect, seed

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
    old_schema = load_old_schema(args.old_ref, OLD_FILES, "007_search_path_pg_temp.sql")

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
    with_path = sorted(name for name, state in states_before.items() if state["config"])
    assert with_path and all(states_before[name]["config"] == BEFORE for name in with_path), states_before

    for _ in range(2):  # rerunnable
        owner.install_search_path_hardening()
    assert fingerprint(owner) == data_before
    assert signatures(owner) == functions
    states_after = function_states(owner, functions)
    for name in functions:
        expected = dict(states_before[name], config=AFTER) if name in with_path else states_before[name]
        assert states_after[name] == expected, (name, states_before[name], states_after[name])
    print(f"PASS migration keeps data, history and {len(functions)} functions; only the search_path of "
          f"{len(with_path)} functions that set one changed", flush=True)

    owner.create_database(names["fresh"])
    owner.install(names["fresh"])
    fresh_owner = connect(args, names["fresh"])
    assert signatures(fresh_owner) == functions
    assert function_states(fresh_owner, functions) == states_after
    print("PASS migrated functions equal a fresh install's", flush=True)

    result = dict(databases=names, postgresql=details["server_version"], old_ref=args.old_ref,
                  migration="sql/007_search_path_pg_temp.sql via Connection.install_search_path_hardening "
                            "(db.py install-search-path-hardening)",
                  migration_runs=2, functions_checked=len(functions), functions_with_search_path=with_path,
                  search_path_before=BEFORE, search_path_after=AFTER, data_and_history_unchanged=True,
                  other_function_attributes_unchanged=True, functions_equal_fresh_install=True,
                  real_cloud_verified=False,
                  note=("Local dedicated test databases on the existing PostgreSQL server; the project database "
                        "was not touched."))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "functions_with_search_path"}, indent=2))


if __name__ == "__main__":
    main()
