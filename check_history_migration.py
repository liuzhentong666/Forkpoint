"""Check sql/005_bounded_history.sql on an installation made before it existed.

Existing local PostgreSQL only. Creates two databases with the schema of
--old-ref (001-004 as Connection.install applied them then):
agentdb_test_histreg_* runs the 18 original assertions and the row merge cases
before and after the migration (they assume the seed-only state), and
agentdb_test_histmig_* gets the history an installation in use would have:
main commits that change, delete, restore and chmod files, branches with files and
rows, main merged into a branch and back, a branch of a branch, a criss-cross
and table snapshots. It records file states, file reads, file and row diffs,
ancestor sets, merge bases and rows_at at every revision, plus the outcome of
row and file merge cases (rolled back). Then it applies
`db.py install-bounded-history` twice and checks that data, history, function
owner/grants/settings and every recorded outcome are unchanged, that the
functions equal those of a fresh install (agentdb_test_histnew_*), and that the
snapshot policy (vc.snapshot_due through SnapshotMaintenance.run_due as
agentdb_manager) only adds derived snapshots without changing any outcome.
Never writes to the project database and never starts or stops the server.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import uuid

from agent_state.engine import Engine
from agent_state.maintenance import SnapshotMaintenance
from agent_state.runtime import Runtime
from agent_state.storage import MemoryStore
from check_merge_migration import fingerprint
from db import add_connection_arguments
from review_local import connect, seed
import run_tests

ROOT = Path(__file__).resolve().parent
OLD_FILES = ("001_core.sql", "002_demo_app.sql", "003_manager_role.sql", "004_merge_batch.sql")
REPLACED = ("vc._ancestors(uuid)", "vc.rows_at(uuid,uuid)", "vc._file_state(uuid,text)",
            "vc.merge_base(uuid,uuid)", "vc.merge(text,uuid,uuid,text)")
ADDED = ("vc._merge_base(uuid[],uuid)", "vc.snapshot_due(text,integer)")

HELPERS = r"""
CREATE FUNCTION pg_temp.hc(p_branch text,p_files jsonb,p_rows text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql AS $f$
DECLARE k text; v jsonb;
BEGIN
  -- One commit: a JSON null deletes the path, a string writes it, {c,x} sets content and flag.
  PERFORM vc.begin_change(p_branch,vc.head(p_branch),'history','history');
  FOR k,v IN SELECT * FROM jsonb_each(p_files) ORDER BY 1 LOOP
    IF jsonb_typeof(v)='null' THEN PERFORM vc.write_file(k,NULL);
    ELSIF jsonb_typeof(v)='string' THEN PERFORM vc.write_file(k,convert_to(v#>>'{}','UTF8'));
    ELSE PERFORM vc.write_file(k,convert_to(v->>'c','UTF8'),(v->>'x')::boolean); END IF;
  END LOOP;
  IF p_rows IS NOT NULL THEN EXECUTE p_rows; END IF;
  RETURN vc.finish_change();
END $f$;
CREATE FUNCTION pg_temp.hm(p_target text,p_source uuid) RETURNS uuid LANGUAGE sql AS
$f$ SELECT vc.merge(p_target,p_source,vc.head(p_target),'history') $f$;
CREATE FUNCTION pg_temp.hb(p_name text,p_base uuid,p_files jsonb,p_rows text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql AS $f$
BEGIN PERFORM vc.fork(p_name,p_base); RETURN pg_temp.hc(p_name,p_files,p_rows); END $f$;
"""

HISTORY = HELPERS + r"""
BEGIN;
DO $h$
DECLARE i int; x1 uuid; x2 uuid;
BEGIN
  FOR i IN 1..24 LOOP
    PERFORM pg_temp.hc('main',
      jsonb_build_object('hist/f'||(i%6)||'.txt','main '||i)
      || CASE i%5 WHEN 0 THEN '{"hist/gone.txt":null}'::jsonb WHEN 1 THEN '{"hist/gone.txt":"back"}' ELSE '{}' END
      || CASE WHEN i%4=0 THEN jsonb_build_object('hist/run.sh',
           jsonb_build_object('c','#!/bin/sh '||(i/8),'x',i%8=0)) ELSE '{}' END
      || CASE WHEN i=3 THEN '{"hist/static.txt":"static"}'::jsonb ELSE '{}' END,
      format('UPDATE app.products SET price=price+1 WHERE id=%s',200+i%9));
    IF i=5 THEN PERFORM vc.fork('h-b',vc.head()); END IF;
    IF i=10 THEN PERFORM vc.fork('h-a',vc.head()); END IF;
    IF i=12 THEN PERFORM vc.materialize(vc.head(),vc.table_id('app.products')); END IF;
    IF i=16 THEN PERFORM vc.materialize(vc.head(),vc.table_id('app.orders')); END IF;
  END LOOP;
  PERFORM pg_temp.hc('h-a','{"hist/a.txt":"a1","hist/static.txt":null}',
                     'UPDATE agent.products SET stock=stock-1 WHERE id=301');
  PERFORM pg_temp.hc('h-a','{"hist/a.txt":null,"hist/a2.txt":{"c":"a2","x":true}}');
  PERFORM pg_temp.hc('h-a','{"src/app.py":"a edits app"}');
  PERFORM pg_temp.hc('h-b','{"hist/b.txt":"b1"}','INSERT INTO agent.orders VALUES(9201,302,1)');
  PERFORM pg_temp.hm('h-b',vc.head());
  PERFORM vc.fork('h-c',vc.head('h-b'));
  PERFORM pg_temp.hc('h-c','{"hist/c.txt":"c1","hist/f2.txt":null}');
  PERFORM pg_temp.hc('h-b','{"hist/b.txt":"b2","hist/run.sh":{"c":"#!/bin/sh 3","x":false}}');
  PERFORM pg_temp.hm('h-b',vc.head('h-c'));
  PERFORM pg_temp.hm('main',vc.head('h-a'));
  PERFORM pg_temp.hc('main','{"hist/f3.txt":"main after a"}');
  PERFORM pg_temp.hm('main',vc.head('h-b'));
  -- Criss-cross: each branch merges the other's head, so they have two merge bases.
  PERFORM vc.fork('x-1',vc.head()); PERFORM vc.fork('x-2',vc.head());
  x1:=pg_temp.hc('x-1','{"hist/x1.txt":"1"}'); x2:=pg_temp.hc('x-2','{"hist/x2.txt":"2"}');
  PERFORM vc.merge('x-1',x2,x1,'history'); PERFORM vc.merge('x-2',x1,x2,'history');
END $h$;
COMMIT;
"""

OBSERVE = r"""
CREATE FUNCTION pg_temp.mb(a uuid,b uuid) RETURNS jsonb LANGUAGE plpgsql AS $f$
BEGIN RETURN to_jsonb(vc.merge_base(a,b)::text);
EXCEPTION WHEN OTHERS THEN RETURN jsonb_build_object('sqlstate',SQLSTATE,'message',SQLERRM); END $f$;
WITH revs AS (SELECT id FROM vc.changeset),
paths AS (SELECT DISTINCT path FROM vc.path_change UNION SELECT 'never/recorded.txt'),
heads AS (SELECT head FROM vc.branch UNION SELECT '00000000-0000-0000-0000-000000000001'::uuid)
SELECT jsonb_build_object(
  'file_state',(SELECT jsonb_object_agg(r.id::text||' '||p.path,vc._file_state(r.id,p.path)) FROM revs r,paths p),
  'read_file',(SELECT jsonb_object_agg(r.id::text||' '||p.path,md5(vc.read_file(r.id,p.path))) FROM revs r,paths p),
  'diff_files_parent',(SELECT jsonb_object_agg(e.child::text||' '||e.ordinal,(
      SELECT coalesce(jsonb_agg(jsonb_build_array(d.path,d.before_state,d.after_state) ORDER BY d.path),'[]')
      FROM vc.diff_files(e.parent,e.child) d)) FROM vc.parent e),
  'diff_files_heads',(SELECT jsonb_object_agg(a.head::text||' '||b.head::text,(
      SELECT coalesce(jsonb_agg(jsonb_build_array(d.path,d.before_state,d.after_state) ORDER BY d.path),'[]')
      FROM vc.diff_files(a.head,b.head) d)) FROM heads a,heads b),
  'ancestors',(SELECT jsonb_object_agg(r.id::text,(SELECT jsonb_agg(a.id ORDER BY a.id) FROM vc._ancestors(r.id) a))
      FROM revs r),
  'merge_base',(SELECT jsonb_object_agg(a.head::text||' '||b.id::text,pg_temp.mb(a.head,b.id)) FROM heads a,revs b),
  'rows_at',(SELECT jsonb_object_agg(r.id::text||' '||t.relation::text,(
      SELECT md5(coalesce(string_agg(x.pk::text||'='||x.doc::text,',' ORDER BY x.pk::text),''))
      FROM vc.rows_at(r.id,t.id) x)) FROM revs r,vc.tracked_table t),
  'diff_rows_parent',(SELECT jsonb_object_agg(e.child::text||' '||e.ordinal||' '||t.relation::text,(
      SELECT coalesce(jsonb_agg(jsonb_build_array(d.pk,d.before_row,d.after_row) ORDER BY d.pk::text),'[]')
      FROM vc.diff_rows(e.parent,e.child,t.id) d)) FROM vc.parent e,vc.tracked_table t));
"""

# How vc.merge resolves files (and rows with them), recorded as normalized JSON in
# one transaction that is rolled back.
FILE_CASES = HELPERS + r"""
BEGIN;
CREATE TEMP TABLE file_case(name text PRIMARY KEY,result jsonb) ON COMMIT DROP;
CREATE FUNCTION pg_temp.fc_summary(rev uuid,src uuid) RETURNS jsonb LANGUAGE sql AS $f$
  SELECT jsonb_build_object(
    'parents',(SELECT count(*) FROM vc.parent WHERE child=rev),
    'second_parent_is_source',EXISTS(SELECT 1 FROM vc.parent WHERE child=rev AND ordinal=2 AND parent=src),
    'paths',(SELECT coalesce(jsonb_agg(jsonb_build_array(path,encode(blob,'hex'),executable) ORDER BY path),'[]')
             FROM vc.path_change WHERE changeset=rev),
    'rows',(SELECT coalesce(jsonb_agg(jsonb_build_array(t.relation::text,c.pk,c.op,c.before_row,c.after_row)
                                      ORDER BY t.relation::text,c.pk::text),'[]')
            FROM vc.row_change c JOIN vc.tracked_table t ON t.id=c.table_id WHERE c.changeset=rev),
    'files',(SELECT jsonb_object_agg(p.path,vc._file_state(rev,p.path))
             FROM (SELECT DISTINCT path FROM vc.path_change) p))
$f$;
CREATE FUNCTION pg_temp.fc_try(name text,target text,source uuid) RETURNS void LANGUAGE plpgsql AS $f$
DECLARE before_head uuid := vc.head(target); merged uuid;
BEGIN
  BEGIN
    merged:=vc.merge(target,source,before_head);
    INSERT INTO file_case VALUES(name,CASE WHEN merged=before_head THEN jsonb_build_object('no_op',true)
      ELSE jsonb_build_object('merged',pg_temp.fc_summary(merged,source)) END);
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO file_case VALUES(name,jsonb_build_object('sqlstate',SQLSTATE,'message',SQLERRM,
      'head_unchanged',vc.head(target)=before_head,'no_open_change',NOT EXISTS(SELECT 1 FROM vc.work_tx)));
  END;
END $f$;
DO $case$
DECLARE h0 uuid := vc.head();
BEGIN
  PERFORM pg_temp.fc_try('file_source_only_change','main',pg_temp.hb('fc-1',h0,'{"hist/f4.txt":"fc-1"}'));
  PERFORM pg_temp.fc_try('file_both_modified_differently','main',pg_temp.hb('fc-2',h0,'{"hist/f4.txt":"fc-2"}'));
  PERFORM pg_temp.fc_try('file_both_modified_identically','main',pg_temp.hb('fc-3',h0,'{"hist/f4.txt":"fc-1"}'));
  PERFORM pg_temp.hc('main','{"hist/f5.txt":null,"hist/f0.txt":"main again"}');
  PERFORM pg_temp.fc_try('file_target_deleted_source_modified','main',pg_temp.hb('fc-4',h0,'{"hist/f5.txt":"fc-4"}'));
  PERFORM pg_temp.fc_try('file_target_modified_source_deleted','main',pg_temp.hb('fc-5',h0,'{"hist/f0.txt":null}'));
  PERFORM pg_temp.fc_try('file_both_deleted','main',pg_temp.hb('fc-6',h0,'{"hist/f5.txt":null}'));
  PERFORM pg_temp.fc_try('executable_flag_only','main',pg_temp.hb('fc-7',h0,jsonb_build_object('hist/run.sh',
    jsonb_build_object('c',convert_from(vc.read_file(h0,'hist/run.sh'),'UTF8'),
                       'x',NOT (vc._file_state(h0,'hist/run.sh')->>'executable')::boolean))));
  PERFORM pg_temp.fc_try('several_file_conflicts','main',
    pg_temp.hb('fc-8',h0,'{"hist/f0.txt":"fc-8","hist/f4.txt":"fc-8","hist/f5.txt":"fc-8"}'));
  PERFORM pg_temp.fc_try('rows_then_file_conflict_rolls_back','main',
    pg_temp.hb('fc-9',h0,'{"hist/f0.txt":"fc-9"}','UPDATE agent.products SET price=99 WHERE id=310'));
  PERFORM vc.fork('fc-target',h0);
  PERFORM pg_temp.hc('fc-target','{"hist/f1.txt":"target side"}');
  PERFORM pg_temp.fc_try('into_branch_disjoint_files','fc-target',
    pg_temp.hb('fc-10',h0,'{"hist/f2.txt":"fc-10","hist/new.txt":{"c":"n","x":true}}'));
  PERFORM pg_temp.fc_try('into_branch_conflict','fc-target',pg_temp.hb('fc-11',h0,'{"hist/f1.txt":"fc-11"}'));
  PERFORM pg_temp.hb('fc-old',vc.head('h-a'),'{"hist/old.txt":"o1","src/app.py":"old branch app"}');
  PERFORM pg_temp.fc_try('main_into_long_lived_branch','fc-old',vc.head());
  PERFORM pg_temp.hc('fc-old','{"hist/old.txt":"o2","hist/f1.txt":"old branch f1"}');
  PERFORM pg_temp.fc_try('long_lived_branch_into_main','main',vc.head('fc-old'));
  PERFORM pg_temp.fc_try('already_merged_is_no_op','main',vc.head('h-a'));
  PERFORM pg_temp.fc_try('criss_cross_needs_unique_base','x-1',vc.head('x-2'));
  PERFORM pg_temp.fc_try('unknown_source','main','00000000-0000-0000-0000-0000000000ff');
  PERFORM pg_temp.fc_try('source_descends_from_target','main',pg_temp.hb('fc-12',vc.head(),'{"hist/ff.txt":"ff"}'));
END $case$;
SELECT jsonb_object_agg(name,result) FROM file_case;
ROLLBACK;
"""
# Several conflicting paths: the old loop took them in the order of an unordered
# SELECT DISTINCT (a hash aggregate); the new one checks them in path order. Either
# reports one of the conflicting paths with the same SQLSTATE and rolls back.
SEVERAL = ("hist/f0.txt", "hist/f4.txt", "hist/f5.txt")


def normalized(cases):
    cases = json.loads(json.dumps(cases))
    several = cases["several_file_conflicts"]
    path = several["message"].removeprefix("File conflict: ")
    assert several["sqlstate"] == "P0001" and path in SEVERAL, several
    several["message"] = "File conflict: one of " + ", ".join(SEVERAL)
    return cases


def check_file_cases(cases):
    def merged(name):
        assert "merged" in cases[name], (name, cases[name])
        return cases[name]["merged"]
    def conflict(name, message):
        assert cases[name] == dict(sqlstate="P0001", message=message, head_unchanged=True,
                                   no_open_change=True), (name, cases[name])
    assert [p[0] for p in merged("file_source_only_change")["paths"]] == ["hist/f4.txt"]
    conflict("file_both_modified_differently", "File conflict: hist/f4.txt")
    assert merged("file_both_modified_identically")["paths"] == []
    conflict("file_target_deleted_source_modified", "File conflict: hist/f5.txt")
    conflict("file_target_modified_source_deleted", "File conflict: hist/f0.txt")
    assert merged("file_both_deleted")["paths"] == []
    flag = merged("executable_flag_only")["paths"]
    assert [p[0] for p in flag] == ["hist/run.sh"] and flag[0][2] is True, flag
    conflict("several_file_conflicts", "File conflict: one of " + ", ".join(SEVERAL))
    conflict("rows_then_file_conflict_rolls_back", "File conflict: hist/f0.txt")
    assert [p[0] for p in merged("into_branch_disjoint_files")["paths"]] == ["hist/f2.txt", "hist/new.txt"]
    conflict("into_branch_conflict", "File conflict: hist/f1.txt")
    assert merged("main_into_long_lived_branch")["paths"]
    assert {"hist/f1.txt", "hist/old.txt", "src/app.py"} <= {p[0] for p in merged("long_lived_branch_into_main")["paths"]}
    assert cases["already_merged_is_no_op"] == dict(no_op=True)
    assert cases["criss_cross_needs_unique_base"]["message"] == "A unique merge base is required"
    assert cases["unknown_source"]["message"] == "Unknown source"
    assert [p[0] for p in merged("source_descends_from_target")["paths"]] == ["hist/ff.txt"]


def observe(owner):
    return json.loads(owner.sql(OBSERVE))


def compare(name, before, after):
    """Equal dicts of observations; on failure name the first differing keys."""
    if before == after:
        return sum(len(v) if isinstance(v, dict) else 1 for v in before.values())
    differing = [f"{group}/{key}" for group in before for key in set(before[group]) | set(after.get(group, {}))
                 if before[group].get(key) != after.get(group, {}).get(key)]
    raise AssertionError(f"{name}: {len(differing)} observations differ, e.g. {differing[:5]}")


def function_states(owner, signatures):
    listed = ",".join(f"'{s}'::regprocedure" for s in signatures)
    return json.loads(owner.sql(f"""
      SELECT jsonb_object_agg(oid::regprocedure::text,jsonb_build_object(
        'owner',pg_get_userbyid(proowner),'security_definer',prosecdef,'config',proconfig,
        'acl',proacl::text[],'volatility',provolatile,'language',(SELECT lanname FROM pg_language WHERE oid=prolang),
        'source_md5',md5(prosrc),
        'client_can_execute',has_function_privilege('agentdb_client',oid,'EXECUTE'),
        'public_can_execute',has_function_privilege('public',oid,'EXECUTE'),
        'manager_can_execute',has_function_privilege('agentdb_manager',oid,'EXECUTE')))
      FROM pg_proc WHERE oid IN({listed});
    """))


def without_source(states):
    return {k: {a: b for a, b in v.items() if a != "source_md5"} for k, v in states.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--old-ref", default="0bf7309",
                        help="git commit whose sql/ predates 005_bounded_history.sql (default 0bf7309)")
    parser.add_argument("--output", type=Path, default=ROOT / "history-migration-results.json")
    args = parser.parse_args()
    def old_file(name):
        return subprocess.run(["git", "show", f"{args.old_ref}:sql/{name}"], cwd=ROOT, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    newer = subprocess.run(["git", "cat-file", "-e", f"{args.old_ref}:sql/005_bounded_history.sql"],
                           cwd=ROOT, capture_output=True)
    assert newer.returncode != 0, "--old-ref already contains 005_bounded_history.sql"
    old_schema = "\n".join(re.sub(r"(?m)^BEGIN;\s*$|^COMMIT;\s*$", "", old_file(name)) for name in OLD_FILES)

    suffix = uuid.uuid4().hex[:10]
    names = dict(history="agentdb_test_histmig_" + suffix, regression="agentdb_test_histreg_" + suffix,
                 fresh="agentdb_test_histnew_" + suffix)
    owner = connect(args, names["history"])
    details = owner.check()
    connections = {}
    for kind in ("history", "regression"):
        # Same as Connection.install before 005 existed: 001-004 in one transaction.
        connections[kind] = connect(args, names[kind])
        owner.create_database(names[kind])
        connections[kind].sql("BEGIN;\n" + old_schema + "\nCOMMIT;")
        seed(connections[kind])
    print("Old-schema test databases: " + names["history"] + ", " + names["regression"], flush=True)
    history, regression = connections["history"], connections["regression"]

    # The 18 original assertions assume the seed-only state (one blob), so they and
    # the row merge cases run in their own old-schema database.
    run_tests.CONNECTION, run_tests.DBNAME = regression, names["regression"]
    regression_old = run_tests.test_suite()
    rows_old = run_tests.merge_semantics()
    history.sql(HISTORY)
    observed_old = observe(history)
    files_old = json.loads(history.sql(FILE_CASES))
    check_file_cases(normalized(files_old))
    data_before = {kind: fingerprint(c) for kind, c in connections.items()}
    functions_before = function_states(history, REPLACED)
    assert function_states(regression, REPLACED) == functions_before

    for kind, connection in connections.items():
        for _ in range(2):  # rerunnable
            connection.install_bounded_history()
        assert fingerprint(connection) == data_before[kind], kind
    functions_after = function_states(history, REPLACED + ADDED)
    assert function_states(regression, REPLACED + ADDED) == functions_after
    assert without_source({k: functions_after[k] for k in functions_before}) == without_source(functions_before), \
        (functions_before, functions_after)
    assert not functions_after["vc._merge_base(uuid[],uuid)"]["client_can_execute"]
    assert not functions_after["vc.snapshot_due(text,integer)"]["client_can_execute"]
    assert functions_after["vc.snapshot_due(text,integer)"]["manager_can_execute"]
    assert not any(state["public_can_execute"] for state in functions_after.values())
    print("PASS migration keeps data, history, owner, grants and settings", flush=True)

    owner.create_database(names["fresh"])
    owner.install(names["fresh"])
    fresh = function_states(connect(args, names["fresh"]), REPLACED + ADDED)
    assert fresh == functions_after, (fresh, functions_after)
    print("PASS migrated functions equal a fresh install's", flush=True)

    observations = compare("reads", observed_old, observe(history))
    files_new = json.loads(history.sql(FILE_CASES))
    assert normalized(files_new) == normalized(files_old), json.dumps(dict(old=files_old, new=files_new), indent=1)
    rows_new = run_tests.merge_semantics()
    assert rows_new == rows_old
    print(f"PASS {observations} recorded reads and {len(files_old) + len(rows_old)} merge outcomes match the "
          "old functions", flush=True)
    regression_new = run_tests.test_suite()
    worker = connect(args, names["regression"], "agentdb_client")
    merged = worker.sql("""
      BEGIN; SELECT vc.fork('client-merge',vc.head()) AS base \\gset
      SELECT vc.begin_change('client-merge',:'base') AS draft \\gset
      UPDATE agent.products SET stock=56 WHERE id=3501; SELECT vc.write_file('client.txt','x') AS blob \\gset
      SELECT vc.finish_change() AS revision \\gset
      SELECT vc.merge('main',:'revision',vc.head()) IS NOT NULL; ROLLBACK;
    """)
    assert merged == "t", merged
    assert fingerprint(regression) == data_before["regression"]  # the checks above all rolled back
    print("PASS 18 original assertions and an agentdb_client merge after migration", flush=True)

    manager = connect(args, names["history"], "agentdb_manager")
    runner = SnapshotMaintenance(Runtime(Engine(MemoryStore())), manager)
    due = runner.due("main", 5)
    assert {t["table_id"] for t in due} == set(history.sql("SELECT id FROM vc.tracked_table;").split()), due
    made = runner.run_due("check-history-migration", "main", 5)
    assert len(made) == len(due) and all(r["verified"] and r["applied"] for r in made), made
    assert runner.due("main", 5) == [] and runner.run_due("check-history-migration", "main", 5) == []
    snapshots = int(history.sql("SELECT count(*) FROM vc.snapshot_manifest;"))
    compare("reads after policy snapshots", observed_old, observe(history))
    files_policy = json.loads(history.sql(FILE_CASES))
    assert normalized(files_policy) == normalized(files_old)
    print("PASS snapshot policy materializes due heads once and changes no outcome", flush=True)

    result = dict(databases=names, postgresql=details["server_version"], old_ref=args.old_ref,
                  migration="sql/005_bounded_history.sql via Connection.install_bounded_history "
                            "(db.py install-bounded-history)",
                  migration_runs=2, regression_tests_before=len(regression_old),
                  regression_tests_after=len(regression_new), data_and_history_unchanged=True,
                  function_attributes_unchanged=True, functions_equal_fresh_install=True,
                  client_merge_after_migration=True, functions=functions_after, before=data_before,
                  recorded_reads=observations, reads_per_group={k: len(v) for k, v in observed_old.items()},
                  file_merge_cases=len(files_old), row_merge_cases=len(rows_old), merge_outcomes_identical=True,
                  several_file_conflicts_message=dict(old=files_old["several_file_conflicts"]["message"],
                                                      new=files_new["several_file_conflicts"]["message"]),
                  snapshot_policy=dict(interval=5, targets=due, materialized=len(made),
                                       snapshot_manifests_after=snapshots, outcomes_unchanged=True),
                  real_cloud_verified=False,
                  note=("Local dedicated test databases on the existing PostgreSQL server; the project database "
                        "was not touched. Snapshot policy ran through SnapshotMaintenance with MemoryStore "
                        "(test-only state backend) and the agentdb_manager role."))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("before", "functions")}, indent=2))


if __name__ == "__main__":
    main()
