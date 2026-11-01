"""Check sql/011_agent_boundary.sql on an installation made before it existed.

Existing local PostgreSQL only. Creates agentdb_test_abmig_* with the schema of
--old-ref (001-010 as Connection.install applied them then) and the history an
installation in use would have: main commits, branches with rows and files, merges
both ways, a criss-cross and snapshots. It records data, history and every function
in vc, applies `db.py install-agent-boundary` twice, and checks that data and history
are unchanged, that every existing function keeps its owner, settings, body and
grants, gaining only grants to the new roles, that the functions and the triggers on
vc tables equal a fresh install's
(agentdb_test_abnew_*), that the trusted path still works as before, that existing
branches belong to no agent, and that a rerun keeps budgets and a freeze. Last it
applies the rollback the migration's header gives, and migrates again.
Never writes to the project database and never starts or stops the server.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from check_history_migration import HISTORY, function_states, load_old_schema
from check_merge_migration import fingerprint
from db import add_connection_arguments
from review_local import connect, seed

OLD_FILES = ("001_core.sql", "002_demo_app.sql", "003_manager_role.sql", "004_merge_batch.sql", "005_bounded_history.sql",
             "006_history_health.sql", "007_search_path_pg_temp.sql", "008_read_set_merge.sql", "009_canonical_row_docs.sql",
             "010_unknown_branch_code.sql")
ADDED = {"vc._refuse(text,text,jsonb,text)", "vc._agent_roles()", "vc._is_agent()", "vc._gate_open()", "vc._charge_bytes(bigint)",
         "vc._agent_changeset()", "vc._agent_blob()", "vc.agent_fork(text,uuid)", "vc.agent_begin(text,uuid,text)",
         "vc._check_share(uuid,uuid)", "vc.agent_merge(text,jsonb)", "vc.agent_status()",
         "vc.agent_report(name)", "vc.issue_budget(name,bigint,bigint,numeric)", 'vc."freeze"(text)', "vc.unfreeze()",
         "vc.stop_agent(name)", "vc.revert(uuid)", "vc.end_stuck_agent_sessions(integer,integer,boolean)", "vc._engine_fingerprint()",
         "vc.boundary_problems()", "vc.boundary_status()", "vc.boundary_check()"}
TABLES = ("vc.branch_owner", "vc.agent_spend", "vc.agent_budget", "vc.gate")
NEW_ROLES = ("agentdb_worker=", "agentdb_keeper=", "agentdb_broker=")


def without_new_roles(states):
    """011 grants its own roles some existing functions; everything else about them has to stay."""
    return {name: dict(state, acl=[item for item in state["acl"] or [] if not item.startswith(NEW_ROLES)])
            for name, state in states.items()}


def signatures(owner):
    return json.loads(owner.sql("""
      SELECT json_agg(p.oid::regprocedure::text ORDER BY p.oid::regprocedure::text) FROM pg_proc p
      JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='vc';"""))


def triggers(owner):
    return json.loads(owner.sql("""
      SELECT json_agg(pg_get_triggerdef(t.oid) ORDER BY pg_get_triggerdef(t.oid)) FROM pg_trigger t
      JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relnamespace='vc'::regnamespace AND NOT t.tgisinternal;"""))


def database_access(owner):
    return owner.sql("""SELECT has_database_privilege('public',current_database(),'CONNECT')::text||','||
      has_database_privilege('public',current_database(),'TEMPORARY')::text||','||
      has_function_privilege('public','lo_create(oid)','EXECUTE')::text;""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--old-ref", default="5eec560",
                        help="git commit whose sql/ predates 011_agent_boundary.sql (default 5eec560)")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "agent-boundary-migration-results.json")
    args = parser.parse_args()
    old_schema = load_old_schema(args.old_ref, OLD_FILES, "011_agent_boundary.sql")

    suffix = uuid.uuid4().hex[:10]
    names = dict(migrated="agentdb_test_abmig_" + suffix, fresh="agentdb_test_abnew_" + suffix)
    owner = connect(args, names["migrated"])
    details = owner.check()
    owner.create_database(names["migrated"])
    owner.sql("BEGIN;\n" + old_schema + "\nCOMMIT;")  # as Connection.install did before 011 existed
    seed(owner)
    owner.sql(HISTORY)
    print("Old-schema test database: " + names["migrated"], flush=True)

    existing = signatures(owner)
    data_before, states_before = fingerprint(owner), function_states(owner, existing)
    triggers_before = triggers(owner)
    assert database_access(owner) == "true,true,true", database_access(owner)

    for _ in range(2):  # rerunnable
        owner.install_agent_boundary()
    assert fingerprint(owner) == data_before
    states_after = function_states(owner, existing)
    assert without_new_roles(states_after) == without_new_roles(states_before)
    gained = sorted(name for name in existing if states_after[name]["acl"] != states_before[name]["acl"])
    current = signatures(owner)
    assert set(current) - set(existing) == ADDED and set(existing) <= set(current), set(current) ^ set(existing) ^ ADDED
    assert set(triggers_before) <= set(triggers(owner))
    assert database_access(owner) == "false,false,false", database_access(owner)
    print(f"PASS migration keeps data, history and {len(existing)} functions ({len(gained)} gain grants to the new roles), "
          f"and adds {len(ADDED)}", flush=True)

    owner.create_database(names["fresh"])
    owner.install(names["fresh"])
    fresh = connect(args, names["fresh"])
    assert signatures(fresh) == current
    assert function_states(fresh, current) == function_states(owner, current)
    assert [t for t in triggers(fresh)] == [t for t in triggers(owner)]
    print("PASS migrated functions and triggers equal a fresh install's", flush=True)

    problems = owner.sql("SELECT vc.boundary_problems();")
    assert problems == "[]", problems
    print("PASS the self-check is clean on the migrated history", flush=True)

    # The trusted path works as it did: any author, any branch, directly to main, and temporary tables.
    owner.sql("""SET ROLE agentdb_client; CREATE TEMP TABLE still_allowed AS SELECT 1;
      SELECT vc.fork('after-011',vc.head());
      BEGIN; SELECT vc.begin_change('after-011',vc.head('after-011'),'gpt-a','x');
      UPDATE agent.products SET price=price+1 WHERE id=3000; SELECT vc.finish_change(); COMMIT;
      SELECT vc.merge('main',vc.head('after-011'),vc.head(),'gpt-a');""")
    author = owner.sql("SELECT author FROM vc.history(vc.head(),1);")
    assert author == "gpt-a", author
    print("PASS the trusted path keeps working and keeps the author it is given", flush=True)

    # Branches made before the boundary belong to no agent, and a rerun keeps what it recorded.
    assert owner.sql("SELECT count(*) FROM vc.branch_owner;") == "0"
    owner.sql("""SELECT vc.issue_budget('agentdb_test_login',5,5); SELECT vc.freeze('kept across a rerun');""")
    owner.install_agent_boundary()
    kept = owner.sql("""SELECT (SELECT bytes_left FROM vc.agent_budget WHERE login='agentdb_test_login')::text||'/'||
      (SELECT frozen_reason FROM vc.gate);""")
    assert kept == "5/kept across a rerun", kept
    owner.sql("SELECT vc.unfreeze();")
    print("PASS a rerun keeps budgets and a freeze", flush=True)

    # The rollback in the migration's header, then forward again.
    data_now = fingerprint(owner)
    database = names["migrated"]
    owner.sql("BEGIN;\nDROP TRIGGER agent_changeset ON vc.changeset; DROP TRIGGER agent_blob ON vc.blob;\n"
              + "".join(f"DROP FUNCTION {name};\n" for name in sorted(ADDED))
              + "DROP TABLE " + ",".join(TABLES) + ";\n"
              + f'GRANT CONNECT,TEMPORARY ON DATABASE "{database}" TO PUBLIC;\n'
              + "GRANT EXECUTE ON FUNCTION lo_from_bytea(oid,bytea),lo_create(oid),lo_creat(integer) TO PUBLIC;\nCOMMIT;")
    assert signatures(owner) == existing and triggers(owner) == triggers_before
    assert fingerprint(owner) == data_now
    assert database_access(owner) == "true,true,true"
    owner.sql("""SET ROLE agentdb_client; BEGIN; SELECT vc.begin_change('after-011',vc.head('after-011'),'gpt-a','x');
      SELECT vc.finish_change(); ROLLBACK;""")
    owner.install_agent_boundary()
    assert signatures(owner) == current and owner.sql("SELECT vc.boundary_problems();") == "[]"
    print("PASS the rollback restores the old functions and access, keeps history, and the migration applies again", flush=True)

    result = dict(databases=names, postgresql=details["server_version"], old_ref=args.old_ref,
                  migration="sql/011_agent_boundary.sql via Connection.install_agent_boundary (db.py install-agent-boundary)",
                  migration_runs=2, functions_kept=len(existing), functions_added=sorted(ADDED), data_and_history_unchanged=True,
                  existing_functions_unchanged_except_grants_to_new_roles=True, functions_gaining_those_grants=gained,
                  functions_and_triggers_equal_fresh_install=True,
                  self_check_clean_on_old_history=True, trusted_path_unchanged=True, rerun_keeps_budgets_and_freeze=True,
                  rollback_checked=True, real_cloud_verified=False,
                  note="Local dedicated test databases on the existing PostgreSQL server; the project database was not touched.")
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("functions_added", "functions_gaining_those_grants")}, indent=2))


if __name__ == "__main__":
    main()
