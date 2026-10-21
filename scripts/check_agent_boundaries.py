"""Adversarial check of what an agent can and cannot do to a fresh installation.

Existing local PostgreSQL only. `agent_lab.py init` creates agentdb_test_gpt_*
(a fresh install, seeded). The check then audits the catalog (functions and
tables agentdb_client can reach, schemas it can create in, the search_path of
SECURITY DEFINER functions), tries with agentdb_client's own SQL what the
catalog cannot show, tries to break version history through the granted
functions, and feeds agent_lab.py, the tool behind the model agents, arguments
its validation must reject; each rejection has to come from the check under
test. Finally main's head and rows must be unchanged. An attempt that gets
through is a GAP and the exit code is 1. agent_lab.py runs without
AGENTDB_LAB_GATEWAY, so its own checks are the ones tested.
Never writes to the project database and never starts or stops the server.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import FLAGS, Connection, sqlstate

GENESIS = "00000000-0000-0000-0000-000000000001"
# 002_demo_app.sql grants agentdb_client exactly these.
GRANTED = {"vc.head(text)", "vc.table_id(regclass)", "vc.fork(text,uuid)", "vc.checkout(text)", "vc.pin(uuid)",
           "vc.begin_change(text,uuid,text,text)", "vc.finish_change()", "vc.rows_at(uuid,uuid)",
           "vc.visible_rows(uuid)", "vc.write_file(text,bytea,boolean)", "vc.read_file(uuid,text)",
           "vc.materialize(uuid,uuid)", "vc.diff_rows(uuid,uuid,uuid)", "vc.diff_files(uuid,uuid)",
           "vc.history(uuid,integer)", "vc.merge_base(uuid,uuid)", "vc.merge(text,uuid,uuid,text)"}
STATE = """SELECT json_build_object('main',vc.head(),
  'products',(SELECT count(*) FROM app.products),
  'checksum',(SELECT md5(string_agg(p::text,',' ORDER BY id)) FROM app.products p),
  'open_work',(SELECT count(*) FROM vc.work_tx));"""


def connect(config, database, role=None):
    return Connection(argparse.Namespace(config=config, database=database, role=role, password=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="Connection config, also passed to agent_lab.py; "
                                                    "default connection.local.json")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "agent-boundaries-results.json")
    args = parser.parse_args()
    config = ["--config", str(args.config)] if args.config else []
    env = {k: v for k, v in os.environ.items() if k != "AGENTDB_LAB_GATEWAY"}

    def lab(*argv):
        p = subprocess.run([sys.executable, str(ROOT / "agent_lab.py"), *argv, *config], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                           creationflags=FLAGS)
        return p.returncode, p.stdout, p.stderr

    code, out, err = lab("init")
    if code:
        raise SystemExit("agent_lab.py init failed:\n" + out + err)
    setup = json.loads(out)
    database, base = setup["database"], setup["base"]
    print(f"Test database: {database}", flush=True)
    owner, client = connect(args.config, database), connect(args.config, database, "agentdb_client")
    details = owner.check()
    before = json.loads(owner.sql(STATE))
    results = []

    def record(group, name, ok, detail):
        status = "HOLDS" if ok else "GAP"
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

    # D: agent_lab.py arguments. gpt-a-v exists, so only argument validation can stop these proposals.
    owner.sql(f"SELECT vc.fork('gpt-a-v','{base}');")
    target = ["--database", database]
    proposal = ["propose", *target, "--branch", "gpt-a-v", "--expected", base, "--actor", "gpt-a"]

    def lab_reject(name, reason, *argv):
        code, out, err = lab(*argv)
        last = (err.strip().splitlines() or [""])[-1]
        record("D", name, code != 0 and reason in out + err,
               f"exit {code}: {last[:160]}" if code else f"ACCEPTED: {out[-200:]!r}")

    gpt = "Use a gpt- branch name"
    lab_reject("branch_with_quote_rejected", gpt, "fork", *target, "--branch", "gpt-x'); SELECT 1; --", "--base", base)
    lab_reject("branch_main_rejected", gpt, "propose", *target, "--branch", "main", "--expected", base,
               "--actor", "gpt-a", "--field", "price", "--value", "11", "--code", "PRICE = 11")
    lab_reject("revision_not_uuid_rejected", "--revision must be a revision UUID",
               "info", *target, "--revision", "x' OR '1'='1")
    for value in ("NaN", "Infinity", "-1", "100001", "1e6"):
        lab_reject(f"price_{value}_rejected", "Finite nonnegative value up to 100000 required",
                   *proposal, "--field", "price", "--value", value, "--code", "PRICE = 1")
    lab_reject("fractional_stock_rejected", "Stock must be an integer",
               *proposal, "--field", "stock", "--value", "1.5", "--code", "STOCK = 1")
    lab_reject("oversized_code_rejected", "Policy code exceeds 4096 bytes",
               *proposal, "--field", "price", "--value", "11", "--code", "# " + "x" * 4100)
    lab_reject("unparsable_code_rejected", "SyntaxError",
               *proposal, "--field", "price", "--value", "11", "--code", "def (:")
    lab_reject("non_test_database_rejected", "dedicated agentdb_test_gpt_<10 hex> database is required",
               "info", "--database", "agentdb")
    lab_reject("path_outside_choices_rejected", "invalid choice",
               "read-code", *target, "--path", "src/app.py", "--revision", base)
    payload = 'PRICE = 11\nNOTE = "\'; DELETE FROM app.products; -- :\'revision\' \\\\gset $$ $x$"\n'
    stored, problems = None, ""
    fork_code, _, fork_err = lab("fork", *target, "--branch", "gpt-a-payload", "--base", base)
    code, out, err = lab("propose", *target, "--branch", "gpt-a-payload", "--expected", base, "--actor", "gpt-a",
                         "--field", "price", "--value", "11", "--code", payload)
    if fork_code or code:
        problems = (fork_err + err)[-200:]
    else:
        code, out, err = lab("read-code", *target, "--path", "experiments/pricing.py",
                             "--revision", json.loads(out)["revision"])
        stored = json.loads(out)["content"] if code == 0 else None
    rows = int(owner.sql("SELECT count(*) FROM app.products;"))
    record("D", "code_with_sql_and_psql_syntax_stored_verbatim", stored == payload and rows == before["products"],
           f"stored == sent: {stored == payload}; app.products rows {before['products']} -> {rows}"
           + (f"; helper error {problems!r}" if problems else ""))

    # E: nothing reached main.
    after = json.loads(owner.sql(STATE))
    record("E", "main_unchanged_after_all_attempts",
           after["main"] == before["main"] and after["checksum"] == before["checksum"] and after["open_work"] == 0,
           f"head unchanged: {after['main'] == before['main']}; rows unchanged: "
           f"{after['checksum'] == before['checksum']}; open changes: {after['open_work']}")

    summary = dict(database=database, postgresql=details["server_version"], checks=len(results),
                   holds=sum(r["status"] == "HOLDS" for r in results),
                   gaps=[r["name"] for r in results if r["status"] == "GAP"], real_cloud_verified=False,
                   note="Local dedicated test database on the existing PostgreSQL server; the project database "
                        "was not touched.")
    args.output.write_text(json.dumps(dict(summary, results=results), indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    raise SystemExit(1 if summary["gaps"] else 0)


if __name__ == "__main__":
    main()
