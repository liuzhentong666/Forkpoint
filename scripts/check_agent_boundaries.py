"""Adversarial check of what an agent can and cannot do to a fresh installation.

Existing local PostgreSQL only. It creates agentdb_test_boundary_* (a fresh
install, seeded as review_local.py seeds). The check then audits the catalog
(functions and tables agentdb_client can reach, schemas it can create in, the
search_path of SECURITY DEFINER functions), tries with agentdb_client's own SQL
what the catalog cannot show, and tries to break version history through the
granted functions. Then main's head and rows must be unchanged. After that it
attacks the boundary around agent logins as a real login with raw SQL
(agent_login_attacks.py). An attempt that gets through is a GAP and the exit
code is 1; a NOTE records something allowed that a reader should know.
Never writes to the project database and never starts or stops the server.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_login_attacks
from db import Connection, sqlstate
from review_local import seed

GENESIS = "00000000-0000-0000-0000-000000000001"
# 002_demo_app.sql grants agentdb_client exactly these, and 008_read_set_merge.sql adds vc.merge_validated.
GRANTED = {"vc.head(text)", "vc.table_id(regclass)", "vc.fork(text,uuid)", "vc.checkout(text)", "vc.pin(uuid)",
           "vc.begin_change(text,uuid,text,text)", "vc.finish_change()", "vc.rows_at(uuid,uuid)",
           "vc.visible_rows(uuid)", "vc.write_file(text,bytea,boolean)", "vc.read_file(uuid,text)",
           "vc.materialize(uuid,uuid)", "vc.diff_rows(uuid,uuid,uuid)", "vc.diff_files(uuid,uuid)",
           "vc.history(uuid,integer)", "vc.merge_base(uuid,uuid)", "vc.merge(text,uuid,uuid,text)",
           "vc.merge_validated(text,uuid,jsonb,text)"}
STATE = """SELECT json_build_object('main',vc.head(),
  'products',(SELECT count(*) FROM app.products),
  'checksum',(SELECT md5(string_agg(p::text,',' ORDER BY id)) FROM app.products p),
  'open_work',(SELECT count(*) FROM vc.work_tx));"""


def connect(config, database, role=None):
    return Connection(argparse.Namespace(config=config, database=database, role=role, password=False))


def fresh_install(config):
    """A new agentdb_test_boundary_* database with every migration installed and the demo rows seeded."""
    database = "agentdb_test_boundary_" + uuid.uuid4().hex[:10]
    owner = connect(config, database)
    owner.check()
    owner.create_database(database)
    owner.install()
    seed(owner)
    return database


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="Connection config; default connection.local.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "agent-boundaries-results.json")
    parser.add_argument("--sandbox-image", help="Docker image with psql and python3; also runs the sandbox check in a real "
                                                "container, which must reach the server at host.docker.internal")
    args = parser.parse_args()

    database = fresh_install(args.config)
    print(f"Test database: {database}", flush=True)
    owner, client = connect(args.config, database), connect(args.config, database, "agentdb_client")
    details = owner.check()
    before = json.loads(owner.sql(STATE))
    results = []

    def record(group, name, ok, detail):
        status = "NOTE" if ok is None else "HOLDS" if ok else "GAP"
        results.append(dict(group=group, name=name, status=status, detail=detail))
        print(f"[{status:>5}] {group} {name}: {detail}", flush=True)

    def reject(group, name, statement, states):
        r = client.execute(statement)
        state = sqlstate(r.stderr)
        ok = r.returncode != 0 and state in states
        record(group, name, ok, f"rejected with {state}" if ok else (
            f"ACCEPTED, output {r.stdout.strip()[-200:]!r}" if r.returncode == 0
            else f"failed with unexpected {state}: {r.stderr.strip()[-200:]!r}"))

    # A: what agentdb_client can reach, from the catalog.
    executable = set(json.loads(owner.sql("""
      SELECT coalesce(json_agg(p.oid::regprocedure::text),'[]') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname IN ('vc','app','agent') AND has_function_privilege('agentdb_client',p.oid,'EXECUTE');""")))
    record("A", "executable_functions_are_the_granted_ones", executable == GRANTED,
           f"extra: {sorted(executable - GRANTED) or 'none'}; missing: {sorted(GRANTED - executable) or 'none'}")
    vc_privileges = json.loads(owner.sql("""
      SELECT coalesce(json_agg(c.relname||':'||priv),'[]') FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) priv
      WHERE n.nspname='vc' AND c.relkind IN ('r','v','m','p') AND has_table_privilege('agentdb_client',c.oid,priv);"""))
    record("A", "no_privileges_on_vc_tables", not vc_privileges, f"found: {vc_privileges or 'none'}")
    app_privileges = json.loads(owner.sql("""
      SELECT coalesce(json_agg(n.nspname||'.'||c.relname||':'||priv),'[]') FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN unnest(ARRAY['TRUNCATE','REFERENCES','TRIGGER']) priv
      WHERE n.nspname IN ('app','agent') AND has_table_privilege('agentdb_client',c.oid,priv);"""))
    record("A", "no_truncate_references_trigger_on_app_tables", not app_privileges,
           f"found: {app_privileges or 'none'}")
    schemas = json.loads(owner.sql("""
      SELECT coalesce(json_agg(nspname),'[]') FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%'
        AND nspname<>'information_schema' AND has_schema_privilege('agentdb_client',oid,'CREATE');"""))
    record("A", "cannot_create_in_any_schema", not schemas, f"CREATE allowed in: {schemas or 'none'}")
    definers = json.loads(owner.sql(r"""
      SELECT json_build_object('total',count(*),
        'not_pg_temp_last',coalesce(json_agg(p.oid::regprocedure::text ORDER BY p.oid::regprocedure::text)
          FILTER (WHERE NOT EXISTS(SELECT 1 FROM unnest(coalesce(p.proconfig,'{}')) c
                                   WHERE c ~ '^search_path=.*\mpg_temp\s*$')),'[]'))
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname IN ('vc','app','agent') AND (p.prosecdef OR p.proconfig IS NOT NULL);"""))
    record("A", "functions_with_search_path_search_pg_temp_last",
           definers["total"] > 0 and not definers["not_pg_temp_last"],
           f"{definers['total']} functions that are SECURITY DEFINER or set search_path; without pg_temp last: "
           f"{definers['not_pg_temp_last'] or 'none'} (sql/007_search_path_pg_temp.sql)")

    # B: what the catalog cannot show, with agentdb_client's own SQL.
    reject("B", "cannot_disable_capture_trigger", "ALTER TABLE app.products DISABLE TRIGGER vc_capture;", {"42501"})
    reject("B", "cannot_switch_off_triggers_for_session", "SET session_replication_role = replica;", {"42501"})
    reject("B", "branch_view_write_needs_open_change", "UPDATE agent.products SET price=1 WHERE id=101;", {"P0001"})

    # C: version history through the granted functions (everything accepted is rolled back).
    reject("C", "fork_cannot_recreate_main", f"SELECT vc.fork('main','{GENESIS}');", {"23505"})
    reject("C", "merge_rejects_unpublished_revision",
           f"BEGIN; SELECT vc.merge('main','{uuid.uuid4()}',vc.head(),'gpt-a'); COMMIT;", {"P0001"})
    for label, path in (("parent_dir", "../outside.py"), ("absolute", "/etc/outside.py"), ("double_slash", "a//b.py"),
                        ("trailing_slash", "dir/"), ("dot_segment", "a/./b.py"), ("backslash", "a\\b.py"),
                        ("empty", "")):
        branch = "path-" + label.replace("_", "-")
        reject("C", f"write_file_rejects_{label}_path", f"""
          BEGIN; SELECT vc.fork('{branch}',vc.head()) AS b \\gset
          SELECT vc.begin_change('{branch}',:'b'::uuid,'gpt-a','path probe');
          SELECT vc.write_file('{path}',convert_to('x','UTF8'));
          ROLLBACK;""", {"P0001"})

    # E: nothing reached main.
    after = json.loads(owner.sql(STATE))
    record("E", "main_unchanged_after_all_attempts",
           after["main"] == before["main"] and after["checksum"] == before["checksum"] and after["open_work"] == 0,
           f"head unchanged: {after['main'] == before['main']}; rows unchanged: "
           f"{after['checksum'] == before['checksum']}; open changes: {after['open_work']}")

    # The login attacks land commits on main, so they come after E. A second install stands in for another project.
    agent_login_attacks.run(owner, args.config, database, record, fresh_install(args.config), args.sandbox_image)

    summary = dict(database=database, postgresql=details["server_version"], checks=len(results),
                   holds=sum(r["status"] == "HOLDS" for r in results), notes=sum(r["status"] == "NOTE" for r in results),
                   gaps=[r["name"] for r in results if r["status"] == "GAP"], real_cloud_verified=False,
                   note="Local dedicated test database on the existing PostgreSQL server; the project database "
                        "was not touched.")
    args.output.write_text(json.dumps(dict(summary, results=results), indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    raise SystemExit(1 if summary["gaps"] else 0)


if __name__ == "__main__":
    main()
