"""Run integration tests in a new database on the existing local PostgreSQL server.

Uses the same connection settings as db.py. Never initializes, starts or stops
a PostgreSQL instance. The generated test database is retained for inspection.
"""
import argparse
import copy
import json
import statistics
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from db import Connection, add_connection_arguments

ROOT = Path(__file__).resolve().parent
CONNECTION = None
DBNAME = "agentdb_test_" + uuid.uuid4().hex[:12]


def sql(text, database=None, fail=None):
    result = CONNECTION.execute(text, database or DBNAME)
    if fail:
        if result.returncode == 0 or fail not in result.stderr:
            raise AssertionError(f"Expected SQLSTATE {fail}:\n{result.stdout}\n{result.stderr}")
    elif result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout.strip()


def transaction(body, role=False):
    return sql("BEGIN;\n" + ("SET LOCAL ROLE agentdb_client;\n" if role else "") +
               "DO $test$ DECLARE b uuid; r uuid; s uuid; t uuid; n bigint; v numeric; text_value text;\n"
               "BEGIN\n" + body + "\nEND $test$;\nROLLBACK;")


def test_suite():
    results = []

    def test(name, action):
        start = time.perf_counter()
        action()
        results.append({"name": name, "passed": True, "ms": round((time.perf_counter()-start)*1000, 2)})
        print("PASS " + name, flush=True)

    test("code_and_native_data_publish_atomically", lambda: transaction("""
      b:=vc.head();
      PERFORM vc.begin_change('main',b);
      UPDATE app.products SET price=77 WHERE id=1;
      PERFORM vc.write_file('src/app.py',convert_to('new code','UTF8'));
      r:=vc.finish_change();
      ASSERT convert_from(vc.read_file(r,'src/app.py'),'UTF8')='new code';
      ASSERT (SELECT (doc->>'price')::numeric FROM vc.rows_at(r,vc.table_id('app.products')) WHERE pk='{"id":1}')=77;
      ASSERT (SELECT (doc->>'price')::numeric FROM vc.rows_at(b,vc.table_id('app.products')) WHERE pk='{"id":1}')=10;
      ASSERT (SELECT count(*) FROM vc.diff_files(b,r))=1;
    """, role=True))

    test("branch_does_not_follow_live_main", lambda: transaction("""
      b:=vc.head(); PERFORM vc.fork('isolated',b);
      PERFORM vc.begin_change('main',b);
      UPDATE app.products SET price=88 WHERE id=1;
      r:=vc.finish_change();
      PERFORM vc.checkout('isolated');
      ASSERT (SELECT price FROM agent.products WHERE id=1)=10;
      PERFORM vc.begin_change('isolated',b);
      UPDATE agent.products SET price=22 WHERE id=1;
      ASSERT (SELECT price FROM agent.products WHERE id=1)=22;
      s:=vc.finish_change();
      ASSERT (SELECT price FROM app.products WHERE id=1)=88;
      ASSERT (SELECT (doc->>'price')::numeric FROM vc.rows_at(s,vc.table_id('app.products')) WHERE pk='{"id":1}')=22;
    """, role=True))

    test("forks_copy_no_rows_or_blobs", lambda: transaction("""
      b:=vc.head(); SELECT count(*) INTO n FROM vc.row_change;
      FOR i IN 1..20 LOOP PERFORM vc.fork('fork-'||i,b); END LOOP;
      ASSERT (SELECT count(*) FROM vc.row_change)=n;
      ASSERT (SELECT count(*) FROM vc.blob)=1;
    """))

    test("repeated_updates_coalesce_and_insert_delete_cancels", lambda: transaction("""
      b:=vc.head(); PERFORM vc.begin_change('main',b);
      UPDATE app.products SET price=20 WHERE id=1;
      UPDATE app.products SET price=30 WHERE id=1;
      INSERT INTO app.products VALUES(5001,'temporary','temp',1,1);
      DELETE FROM app.products WHERE id=5001;
      r:=vc.finish_change();
      ASSERT (SELECT count(*) FROM vc.row_change WHERE changeset=r)=1;
      ASSERT (SELECT (before_row->>'price')::numeric FROM vc.row_change WHERE changeset=r)=10;
      ASSERT (SELECT (after_row->>'price')::numeric FROM vc.row_change WHERE changeset=r)=30;
    """))

    test("primary_key_change_is_delete_plus_insert", lambda: transaction("""
      b:=vc.head(); PERFORM vc.fork('pk-change',b);
      PERFORM vc.begin_change('pk-change',b);
      UPDATE agent.products SET id=8001 WHERE id=1;
      r:=vc.finish_change();
      ASSERT (SELECT count(*) FROM vc.row_change WHERE changeset=r)=2;
      ASSERT NOT EXISTS(SELECT 1 FROM vc.rows_at(r,vc.table_id('app.products')) WHERE pk='{"id":1}');
      ASSERT EXISTS(SELECT 1 FROM vc.rows_at(r,vc.table_id('app.products')) WHERE pk='{"id":8001}');
    """))

    test("missing_finalize_rolls_back_native_table", lambda: sql("""
      BEGIN; SET LOCAL ROLE agentdb_client;
      SELECT vc.begin_change('main',vc.head());
      UPDATE app.products SET price=999 WHERE id=1;
      COMMIT;
    """, fail="23514"))
    assert sql("SELECT price=10 FROM app.products WHERE id=1;") == "t"

    test("direct_native_write_requires_context", lambda: sql(
        "SET ROLE agentdb_client; UPDATE app.products SET price=2 WHERE id=1;", fail="P0001"))

    test("duplicate_branch_pk_in_one_statement_is_rejected", lambda: sql("""
      BEGIN; SELECT vc.fork('duplicate-test',vc.head());
      SELECT vc.begin_change('duplicate-test',vc.head('duplicate-test'));
      INSERT INTO agent.products VALUES(5001,'a','a',1,1),(5001,'b','b',1,1);
    """, fail="23505"))

    test("published_history_cannot_be_updated", lambda: sql(
        "UPDATE vc.changeset SET message='changed';", fail="55000"))
    test("client_cannot_append_to_published_changeset", lambda: sql("""
      SET ROLE agentdb_client;
      INSERT INTO vc.path_change VALUES(vc.head(),'injected',NULL,false);
    """, fail="42501"))

    test("branch_unique_violation_rejected_when_merged", lambda: sql("""
      BEGIN; SELECT vc.fork('bad-unique',vc.head());
      SELECT vc.begin_change('bad-unique',vc.head('bad-unique'));
      INSERT INTO agent.products VALUES(5001,'sku-1','duplicate',1,1);
      SELECT vc.finish_change();
      SELECT vc.merge('main',vc.head('bad-unique'),vc.head());
    """, fail="23505"))

    test("branch_foreign_key_violation_rejected_when_merged", lambda: sql("""
      BEGIN; SELECT vc.fork('bad-fk',vc.head());
      SELECT vc.begin_change('bad-fk',vc.head('bad-fk'));
      INSERT INTO agent.orders VALUES(7001,999999,1);
      SELECT vc.finish_change();
      SELECT vc.merge('main',vc.head('bad-fk'),vc.head());
    """, fail="23503"))

    test("branch_check_violation_rejected_when_merged", lambda: sql("""
      BEGIN; SELECT vc.fork('bad-check',vc.head());
      SELECT vc.begin_change('bad-check',vc.head('bad-check'));
      UPDATE agent.products SET stock=-1 WHERE id=1;
      SELECT vc.finish_change();
      SELECT vc.merge('main',vc.head('bad-check'),vc.head());
    """, fail="23514"))

    test("disjoint_merges_preserve_both_parents_and_main_projection", lambda: transaction("""
      b:=vc.head(); PERFORM vc.fork('merge-a',b); PERFORM vc.fork('merge-b',b);
      PERFORM vc.begin_change('merge-a',b);
      UPDATE agent.products SET price=21 WHERE id=1;
      PERFORM vc.write_file('a.py',convert_to('a','UTF8')); r:=vc.finish_change();
      PERFORM vc.begin_change('merge-b',b);
      UPDATE agent.products SET price=22 WHERE id=2; s:=vc.finish_change();
      t:=vc.merge('main',r,b); t:=vc.merge('main',s,t);
      ASSERT (SELECT price FROM app.products WHERE id=1)=21;
      ASSERT (SELECT price FROM app.products WHERE id=2)=22;
      ASSERT (SELECT count(*) FROM vc.parent WHERE child=t)=2;
      ASSERT convert_from(vc.read_file(t,'a.py'),'UTF8')='a';
      ASSERT (SELECT count(*) FROM vc.rows_at(t,vc.table_id('app.products')))=4000;
      ASSERT vc.merge('main',s,t)=t;
    """))

    test("row_conflict_aborts_merge", lambda: sql("""
      BEGIN; SELECT vc.fork('conflict-a',vc.head()); SELECT vc.fork('conflict-b',vc.head());
      SELECT vc.begin_change('conflict-a',vc.head('conflict-a'));
      UPDATE agent.products SET price=21 WHERE id=1; SELECT vc.finish_change();
      SELECT vc.begin_change('conflict-b',vc.head('conflict-b'));
      UPDATE agent.products SET price=22 WHERE id=1; SELECT vc.finish_change();
      SELECT vc.merge('main',vc.head('conflict-a'),vc.head());
      SELECT vc.merge('main',vc.head('conflict-b'),vc.head());
    """, fail="P0001"))

    test("file_conflict_rolls_back_rows_applied_before_it", lambda: transaction("""
      b:=vc.head(); PERFORM vc.fork('file-a',b); PERFORM vc.fork('file-b',b);
      PERFORM vc.begin_change('file-a',b);
      PERFORM vc.write_file('src/app.py',convert_to('a','UTF8')); r:=vc.finish_change();
      PERFORM vc.begin_change('file-b',b);
      UPDATE agent.products SET price=91 WHERE id=3;
      PERFORM vc.write_file('src/app.py',convert_to('b','UTF8')); s:=vc.finish_change();
      t:=vc.merge('main',r,b);
      BEGIN
        PERFORM vc.merge('main',s,t);
        RAISE EXCEPTION 'Expected file conflict was not raised' USING ERRCODE='XX000';
      EXCEPTION WHEN SQLSTATE 'P0001' THEN NULL;
      END;
      ASSERT vc.head()=t;
      ASSERT (SELECT price FROM app.products WHERE id=3)=10;
      ASSERT NOT EXISTS(SELECT 1 FROM vc.work_tx);
    """))

    test("materialized_snapshot_matches_replay", lambda: transaction("""
      b:=vc.head(); PERFORM vc.fork('snapshot-test',b);
      PERFORM vc.begin_change('snapshot-test',b);
      UPDATE agent.products SET price=71 WHERE id=1;
      DELETE FROM agent.products WHERE id=2;
      r:=vc.finish_change();
      SELECT md5(string_agg(doc::text,',' ORDER BY pk::text)) INTO text_value
      FROM vc.rows_at(r,vc.table_id('app.products'));
      ASSERT vc.materialize(r,vc.table_id('app.products'))=3999;
      ASSERT (SELECT md5(string_agg(doc::text,',' ORDER BY pk::text))
        FROM vc.rows_at(r,vc.table_id('app.products')))=text_value;
    """))

    test("stale_head_is_rejected", lambda: transaction("""
      b:=vc.head(); PERFORM vc.begin_change('main',b);
      UPDATE app.products SET price=21 WHERE id=1; r:=vc.finish_change();
      BEGIN
        PERFORM vc.begin_change('main',b);
        RAISE EXCEPTION 'Expected stale head' USING ERRCODE='XX000';
      EXCEPTION WHEN serialization_failure THEN NULL;
      END;
      ASSERT vc.head()=r;
    """))
    return results


def concurrency():
    base = sql("SELECT vc.head();")
    before = int(sql("SELECT count(*) FROM vc.row_change;"))
    sql("\n".join(f"SELECT vc.fork('load-{i}','{base}');" for i in range(10)))
    copied = int(sql("SELECT count(*) FROM vc.row_change;")) - before
    barrier = threading.Barrier(10)

    def independent(i):
        barrier.wait(timeout=20)
        output = sql(f"""
          BEGIN; SET LOCAL ROLE agentdb_client;
          SELECT clock_timestamp() AS started \gset
          SELECT vc.begin_change('load-{i}','{base}') AS draft \gset
          UPDATE agent.products SET price={100+i} WHERE id={i+1};
          SELECT vc.finish_change() AS revision \gset
          SELECT jsonb_build_object('agent',{i},'revision',:'revision',
            'transaction_ms',extract(epoch FROM clock_timestamp()-:'started'::timestamptz)*1000);
          COMMIT;
        """)
        return json.loads(output)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=10) as pool:
        independent_results = list(pool.map(independent, range(10)))
    wall_ms = (time.perf_counter()-start)*1000
    assert sql("SELECT price=10 FROM app.products WHERE id=1;") == "t"
    assert sql("SELECT vc.head();") == base
    for i, item in enumerate(independent_results):
        price = sql(f"SELECT doc->>'price' FROM vc.rows_at('{item['revision']}',vc.table_id('app.products')) WHERE pk='{{\"id\":{i+1}}}';")
        assert float(price) == 100+i

    barrier = threading.Barrier(10)
    def contend(i):
        barrier.wait(timeout=20)
        result = CONNECTION.execute(f"""
          BEGIN; SET LOCAL ROLE agentdb_client;
          SELECT vc.begin_change('main','{base}');
          UPDATE app.products SET price={200+i} WHERE id=100;
          SELECT vc.finish_change(); COMMIT;
        """, DBNAME)
        if result.returncode == 0:
            return "committed"
        if "40001" in result.stderr:
            return "stale_head"
        raise AssertionError(result.stderr)
    with ThreadPoolExecutor(max_workers=10) as pool:
        contention = list(pool.map(contend, range(10)))
    assert contention.count("committed") == 1
    assert contention.count("stale_head") == 9
    times = [x["transaction_ms"] for x in independent_results]
    print("PASS ten_independent_agents_and_same_head_contention", flush=True)
    return dict(rows=4000, agents=10, new_row_log_entries_for_forks=copied,
                independent_successes=len(independent_results),
                parallel_wall_ms=round(wall_ms,2),
                transaction_median_ms=round(statistics.median(times),2),
                transaction_max_ms=round(max(times),2),
                same_head_winners=contention.count("committed"),
                same_head_stale_rejections=contention.count("stale_head"),
                measurements=independent_results,
                note="One local run. Transaction timing excludes final COMMIT; wall time includes CLI startup. Not a production benchmark.")


# How vc.merge resolves each changed row against the target ("ours"), recorded as
# normalized JSON (no random ids) so two installations can be compared. Runs in one
# transaction that is rolled back; uses rows 3001-3005 and 9001 of the 4,000-row seed.
MERGE_CASES = r"""
BEGIN;
CREATE TEMP TABLE merge_case(name text PRIMARY KEY, result jsonb) ON COMMIT DROP;
CREATE FUNCTION pg_temp.mc_summary(rev uuid,src uuid) RETURNS jsonb LANGUAGE sql AS $f$
  SELECT jsonb_build_object(
    'parents',(SELECT count(*) FROM vc.parent WHERE child=rev),
    'second_parent_is_source',EXISTS(SELECT 1 FROM vc.parent WHERE child=rev AND ordinal=2 AND parent=src),
    'message_names_source',(SELECT message='merge '||src::text FROM vc.changeset WHERE id=rev),
    'rows',(SELECT coalesce(jsonb_agg(jsonb_build_array(t.relation::text,c.pk,c.op,c.before_row,c.after_row)
                                      ORDER BY t.relation::text,c.pk::text),'[]')
            FROM vc.row_change c JOIN vc.tracked_table t ON t.id=c.table_id WHERE c.changeset=rev),
    'paths',(SELECT coalesce(jsonb_agg(path ORDER BY path),'[]') FROM vc.path_change WHERE changeset=rev))
$f$;
CREATE FUNCTION pg_temp.mc_state(rev uuid) RETURNS jsonb LANGUAGE sql AS $f$
  SELECT jsonb_build_object(
    'products',(SELECT jsonb_agg(r.doc ORDER BY r.pk::text) FROM vc.rows_at(rev,vc.table_id('app.products')) r
                WHERE (r.pk->>'id')::bigint IN(3001,3002,3003,3004,3005,9001)),
    'orders',(SELECT jsonb_agg(r.doc ORDER BY r.pk::text) FROM vc.rows_at(rev,vc.table_id('app.orders')) r
              WHERE (r.pk->>'id')::bigint=9101))
$f$;
CREATE FUNCTION pg_temp.mc_branch(name text,base uuid,body text) RETURNS uuid LANGUAGE plpgsql AS $f$
BEGIN
  PERFORM vc.fork(name,base); PERFORM vc.begin_change(name,base);
  EXECUTE body;
  RETURN vc.finish_change();
END $f$;
CREATE FUNCTION pg_temp.mc_try(name text,target text,source uuid) RETURNS void LANGUAGE plpgsql AS $f$
DECLARE before_head uuid := vc.head(target); merged uuid;
BEGIN
  BEGIN
    merged:=vc.merge(target,source,before_head);
    INSERT INTO merge_case VALUES(name,jsonb_build_object('merged',pg_temp.mc_summary(merged,source)));
  EXCEPTION WHEN OTHERS THEN
    INSERT INTO merge_case SELECT name,jsonb_build_object('sqlstate',SQLSTATE,
      'message',(SELECT replace(replace(SQLERRM,vc.table_id('app.products')::text,'app.products'),
                                vc.table_id('app.orders')::text,'app.orders')),
      'head_unchanged',vc.head(target)=before_head,
      'no_open_change',NOT EXISTS(SELECT 1 FROM vc.work_tx));
  END;
END $f$;
DO $case$
DECLARE h0 uuid := vc.head(); ra uuid; rb uuid; rc uuid; rd uuid; m1 uuid; m2 uuid; m3 uuid;
BEGIN
  ra:=pg_temp.mc_branch('mc-a',h0,$$UPDATE agent.products SET price=31 WHERE id=3001;
    INSERT INTO agent.products VALUES(9001,'mc-9001','mc',5,5);
    SELECT vc.write_file('mc/a.txt',convert_to('a','UTF8'))$$);
  rb:=pg_temp.mc_branch('mc-b',h0,$$UPDATE agent.products SET price=31 WHERE id=3001;
    UPDATE agent.products SET price=32 WHERE id=3002; DELETE FROM agent.products WHERE id=3003;
    INSERT INTO agent.orders VALUES(9101,3004,2)$$);
  rc:=pg_temp.mc_branch('mc-c',h0,$$UPDATE agent.products SET price=32 WHERE id=3002;
    UPDATE agent.products SET price=35 WHERE id=3005$$);
  m1:=vc.merge('main',ra,h0);
  INSERT INTO merge_case VALUES('main_inserts_and_file',pg_temp.mc_summary(m1,ra));
  m2:=vc.merge('main',rb,m1);
  INSERT INTO merge_case VALUES('main_skips_row_identical_to_source',pg_temp.mc_summary(m2,rb));
  INSERT INTO merge_case VALUES('main_state',pg_temp.mc_state(m2) ||
    jsonb_build_object('native',(SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id) FROM app.products p
                                 WHERE p.id IN(3001,3002,3003,3004,3005,9001))));
  m3:=vc.merge('mc-c',m2,rc);
  INSERT INTO merge_case VALUES('branch_stages_target_state_as_before_row',pg_temp.mc_summary(m3,m2));
  INSERT INTO merge_case VALUES('branch_state',pg_temp.mc_state(m3));
  INSERT INTO merge_case VALUES('already_merged_returns_head',to_jsonb(vc.merge('main',ra,m2)=m2));
  rd:=pg_temp.mc_branch('mc-d',h0,'UPDATE agent.products SET price=40 WHERE id=3002');
  PERFORM pg_temp.mc_try('conflict_both_modified','main',rd);
  PERFORM pg_temp.mc_try('conflict_into_branch','mc-c',rd);
  PERFORM pg_temp.mc_try('conflict_target_deleted_source_modified','main',
    pg_temp.mc_branch('mc-e',h0,'UPDATE agent.products SET price=41 WHERE id=3003'));
  PERFORM pg_temp.mc_try('conflict_target_modified_source_deleted','main',
    pg_temp.mc_branch('mc-f',h0,'DELETE FROM agent.products WHERE id=3001'));
  PERFORM pg_temp.mc_try('conflict_both_inserted_differently','main',
    pg_temp.mc_branch('mc-g',h0,$$INSERT INTO agent.products VALUES(9001,'mc-9001x','other',5,5)$$));
  PERFORM pg_temp.mc_try('conflict_first_of_several_in_diff_order','main',
    pg_temp.mc_branch('mc-i',h0,'UPDATE agent.products SET price=50 WHERE id=3001;
      UPDATE agent.products SET price=51 WHERE id=3002'));
  PERFORM pg_temp.mc_try('both_inserted_identically_is_skipped','main',
    pg_temp.mc_branch('mc-h',h0,$$INSERT INTO agent.products VALUES(9001,'mc-9001','mc',5,5)$$));
END $case$;
SELECT jsonb_object_agg(name,result) FROM merge_case;
ROLLBACK;
"""


def merge_semantics():
    cases = json.loads(sql(MERGE_CASES))
    def rows(name, case=None):
        merged = cases[name] if case is None else cases[name]["merged"]
        assert merged["parents"] == 2 and merged["second_parent_is_source"] and merged["message_names_source"], merged
        return [(r[0], r[1]["id"], r[2], r[3] and r[3].get("price"), r[4] and r[4].get("price")) for r in merged["rows"]]
    assert rows("main_inserts_and_file") == [("app.products", 3001, "U", 10, 31), ("app.products", 9001, "I", None, 5)]
    assert cases["main_inserts_and_file"]["paths"] == ["mc/a.txt"]
    # 3001 already has the source's value on main, so it is not changed again.
    assert rows("main_skips_row_identical_to_source") == [
        ("app.orders", 9101, "I", None, None), ("app.products", 3002, "U", 10, 32), ("app.products", 3003, "D", 10, None)]
    state = cases["main_state"]
    assert [p["id"] for p in state["products"]] == [3001, 3002, 3004, 3005, 9001]
    assert state["native"] == sorted(state["products"], key=lambda p: p["id"]), state
    # Merging into a branch stages the target's current row as before_row.
    assert rows("branch_stages_target_state_as_before_row") == [
        ("app.orders", 9101, "I", None, None), ("app.products", 3001, "U", 10, 31),
        ("app.products", 3003, "D", 10, None), ("app.products", 9001, "I", None, 5)]
    assert [p["price"] for p in cases["branch_state"]["products"]] == [31, 32, 10, 35, 5]
    assert cases["already_merged_returns_head"] is True
    for name, key in [("conflict_both_modified", 3002), ("conflict_into_branch", 3002),
                      ("conflict_target_deleted_source_modified", 3003),
                      ("conflict_target_modified_source_deleted", 3001),
                      ("conflict_both_inserted_differently", 9001)]:
        assert cases[name] == dict(sqlstate="P0001", message=f'Row conflict: table app.products, key {{"id": {key}}}',
                                   head_unchanged=True, no_open_change=True), (name, cases[name])
    several = cases["conflict_first_of_several_in_diff_order"]
    assert several["sqlstate"] == "P0001" and several["head_unchanged"] and several["no_open_change"], several
    assert rows("both_inserted_identically_is_skipped", "merged") == []
    print("PASS merge_resolves_target_state_per_changed_row", flush=True)
    return cases


def reads(*ids):
    """SQL for a p_reads value naming app.products rows by id."""
    keys = ",".join(f"jsonb_build_object('id',{i})" for i in ids)
    return f"jsonb_build_object(vc.table_id('app.products')::text,jsonb_build_array({keys}))"


def proposal(var, branch, statement):
    """SQL that commits statement on a new branch forked from b and keeps the revision in var."""
    return (f"PERFORM vc.fork('{branch}',b); PERFORM vc.begin_change('{branch}',b);\n"
            f"{statement}\n{var}:=vc.finish_change();\n")


def refused(call, code, detail=False):
    """SQL that requires call to fail with SQLSTATE code, keeping its DETAIL or message in text_value."""
    item = "PG_EXCEPTION_DETAIL" if detail else "MESSAGE_TEXT"
    return (f"BEGIN\n  PERFORM {call};\n  RAISE EXCEPTION 'Expected SQLSTATE {code}' USING ERRCODE='XX000';\n"
            f"EXCEPTION WHEN SQLSTATE '{code}' THEN GET STACKED DIAGNOSTICS text_value={item};\nEND;\n")


def read_set_merge():
    """vc.merge_validated (sql/008_read_set_merge.sql): merges that refuse write skew.

    Kept apart from test_suite(), which the migration checks also run on installs
    that predate 008. Each case runs as agentdb_client in a transaction that is
    rolled back, except the comparison with vc.rows_at, which calls an owner-only
    function. Rows 3201-3214, 9201 and 9301 are used only here; the last case,
    a race between two sessions, commits for real and leaves main two merges on.
    """
    results = []

    def test(name, body, role=True):
        start = time.perf_counter()
        transaction("b:=vc.head();\n" + body, role=role)
        results.append({"name": name, "passed": True, "ms": round((time.perf_counter()-start)*1000, 2)})
        print("PASS " + name, flush=True)

    keys = "ARRAY['{\"id\":3201}','{\"id\":3202}','{\"id\":3203}','{\"id\":9201}','{\"id\":999999}']::jsonb[]"
    test("rows_at_keys_matches_rows_at", proposal("r", "rk", """
        UPDATE agent.products SET price=61 WHERE id=3201; DELETE FROM agent.products WHERE id=3202;
        INSERT INTO agent.products VALUES(9201,'rk-9201','rk',1,1);""") + f"""
      FOR n IN 1..2 LOOP  -- the second pass reads r from its own snapshot
        FOREACH s IN ARRAY ARRAY[b,r] LOOP
          ASSERT (SELECT jsonb_agg(jsonb_build_array(k.pk,k.doc) ORDER BY k.pk::text)
                  FROM vc._rows_at_keys(s,vc.table_id('app.products'),{keys}) k) IS NOT DISTINCT FROM
                 (SELECT jsonb_agg(jsonb_build_array(a.pk,a.doc) ORDER BY a.pk::text)
                  FROM vc.rows_at(s,vc.table_id('app.products')) a WHERE a.pk=ANY({keys})), s::text;
        END LOOP;
        PERFORM vc.materialize(r,vc.table_id('app.products'));
      END LOOP;""", role=False)

    test("write_skew_is_refused_with_the_changed_rows",
         proposal("r", "ws-a", "UPDATE agent.products SET stock=45 WHERE id=3204;")
         + proposal("s", "ws-b", "UPDATE agent.products SET stock=45 WHERE id=3205;")
         + f"t:=vc.merge_validated('main',r,{reads(3205)},'ws-a');\n"
         + refused(f"vc.merge_validated('main',s,{reads(3204)},'ws-b')", "RS001", detail=True)
         + """ASSERT vc.head()=t AND (SELECT stock FROM app.products WHERE id=3205)=100;
           ASSERT jsonb_array_length(text_value::jsonb)=1
             AND text_value::jsonb @> '[{"key":{"id":3204},"base":{"stock":100},"now":{"stock":45}}]', text_value;""")

    test("unchanged_reads_merge_although_main_moved",
         proposal("r", "un-a", "UPDATE agent.products SET stock=45 WHERE id=3206;")
         + proposal("s", "un-b", "UPDATE agent.products SET stock=45 WHERE id=3207;")
         + f"t:=vc.merge_validated('main',r,{reads(3208)},'un-a');\n"
         + refused("vc.merge('main',s,b,'un-b')", "40001")  # what the stale-head check does with it
         + f"""t:=vc.merge_validated('main',s,{reads(3208)},'un-b');
           ASSERT vc.head()=t AND vc.merge_base(t,r)=r AND vc.merge_base(t,s)=s;
           ASSERT (SELECT sum(stock) FROM app.products WHERE id IN (3206,3207))=90;""")

    test("a_row_changed_and_changed_back_is_not_a_conflict",
         proposal("s", "cb", "UPDATE agent.products SET stock=45 WHERE id=3209;") + """
           PERFORM vc.begin_change('main',vc.head()); UPDATE app.products SET stock=50 WHERE id=3210; PERFORM vc.finish_change();
           PERFORM vc.begin_change('main',vc.head()); UPDATE app.products SET stock=100 WHERE id=3210; PERFORM vc.finish_change();
           """ + f"t:=vc.merge_validated('main',s,{reads(3210)},'cb');\nASSERT vc.merge_base(t,s)=s;")

    test("a_row_inserted_since_the_base_conflicts_with_a_read_of_its_absence",
         proposal("r", "ab-a", "INSERT INTO agent.products VALUES(9301,'sku-9301','late',1,1);")
         + proposal("s", "ab-b", "UPDATE agent.products SET stock=45 WHERE id=3211;")
         + "t:=vc.merge_validated('main',r,'{}'::jsonb,'ab-a');\n"
         + refused(f"vc.merge_validated('main',s,{reads(9301)},'ab-b')", "RS001", detail=True)
         + """ASSERT (text_value::jsonb)->0->'base'='null'::jsonb
             AND (text_value::jsonb)->0->'now'->>'name'='late', text_value;""")

    test("write_write_conflicts_stay_p0001",
         proposal("r", "ww-a", "UPDATE agent.products SET stock=45 WHERE id=3212;")
         + proposal("s", "ww-b", "UPDATE agent.products SET stock=30 WHERE id=3212;")
         + "t:=vc.merge_validated('main',r,'{}'::jsonb,'ww-a');\n"
         + refused("vc.merge_validated('main',s,'{}'::jsonb,'ww-b')", "P0001")
         + "ASSERT text_value LIKE 'Row conflict:%' AND vc.head()=t, text_value;")

    malformed = ["[]", None, '{"products":[{"id":1}]}', '{"00000000-0000-0000-0000-0000000000aa":[{"id":1}]}',
                 '{"TID":{"id":1}}', '{"TID":[1]}', '{"TID":[{"sku":"sku-1"}]}', '{"TID":[{"id":1,"sku":"sku-1"}]}',
                 # The right column but not the value history records: each would match no row.
                 '{"TID":[{"id":"3204"}]}', '{"TID":[{"id":null}]}', '{"TID":[{"id":1.5}]}', '{"TID":[{"id":"abc"}]}']
    literal = "ARRAY[" + ",".join("NULL" if m is None else f"'{m}'" for m in malformed) + "]::text[]"
    test("malformed_read_sets_are_refused_before_merging",
         proposal("r", "mf", "UPDATE agent.products SET stock=45 WHERE id=3201;") + f"""
           FOREACH text_value IN ARRAY {literal} LOOP
             BEGIN
               PERFORM vc.merge_validated('main',r,replace(text_value,'TID',vc.table_id('app.products')::text)::jsonb,'mf');
               RAISE EXCEPTION 'Accepted %',text_value USING ERRCODE='XX000';
             EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
             END;
           END LOOP;
           ASSERT vc.head()=b;""")

    test("an_already_merged_source_returns_the_head",
         proposal("r", "am", "UPDATE agent.products SET stock=45 WHERE id=3204;")
         + f"""t:=vc.merge_validated('main',r,{reads(3205)},'am');
           ASSERT vc.merge_validated('main',r,{reads(3205)},'am')=t AND vc.head()=t;""")

    # The read set stands in for the expected head, so an empty one declares that
    # the decision read nothing: it merges after main moved, like head_blind.
    test("an_empty_read_set_merges_without_a_head_check",
         proposal("r", "er-a", "UPDATE agent.products SET stock=45 WHERE id=3206;")
         + proposal("s", "er-b", "UPDATE agent.products SET stock=45 WHERE id=3207;")
         + """t:=vc.merge_validated('main',r,'{}'::jsonb,'er-a');
           t:=vc.merge_validated('main',s,'{}'::jsonb,'er-b');
           ASSERT vc.merge_base(t,s)=s AND vc.merge_base(t,r)=r;""")

    results.append(read_set_race())
    print("PASS " + results[-1]["name"], flush=True)
    return results


def read_set_race():
    """Two crossing read sets merged at the same time: the head lock comes before the check.

    A merges and holds main's lock (sleeping inside its transaction); B, started
    then, must wait for A's commit and refuse with RS001 against A's head. Checking
    before the lock would instead validate B against the old head and fail later
    with 40001, or merge both, so this case pins the order.
    """
    start = time.perf_counter()
    base = sql("SELECT vc.head();")
    revision = {}
    for name, row in (("rsc-a", 3213), ("rsc-b", 3214)):
        revision[name] = sql(f"""SET ROLE agentdb_client;
          SELECT vc.fork('{name}','{base}') AS head \\gset
          BEGIN;
          SELECT vc.begin_change('{name}','{base}') AS draft \\gset
          UPDATE agent.products SET stock=45 WHERE id={row};
          SELECT vc.finish_change() AS revision \\gset
          COMMIT;
          SELECT :'revision';""")
    tag = "rsc-a holds main " + uuid.uuid4().hex[:8]
    holder = ThreadPoolExecutor(max_workers=1).submit(CONNECTION.execute, f"""
      BEGIN; SET LOCAL ROLE agentdb_client;
      SELECT vc.merge_validated('main','{revision["rsc-a"]}',{reads(3214)},'rsc-a');
      SELECT pg_sleep(3) /* {tag} */;
      COMMIT;""", DBNAME)
    deadline = time.monotonic() + 30
    while sql(f"SELECT count(*) FROM pg_stat_activity WHERE state='active' AND query LIKE '%{tag}%' "
              f"AND pid<>pg_backend_pid() AND query NOT LIKE '%pg_stat_activity%';") != "1":
        if holder.done() or time.monotonic() > deadline:
            raise AssertionError("A never held main's lock: " + str(holder.result().stderr if holder.done() else "timeout"))
        time.sleep(0.05)
    waited = time.perf_counter()
    second = CONNECTION.execute(f"""
      BEGIN; SET LOCAL ROLE agentdb_client;
      SELECT vc.merge_validated('main','{revision["rsc-b"]}',{reads(3213)},'rsc-b');
      COMMIT;""", DBNAME)
    waited_ms = (time.perf_counter()-waited)*1000
    first = holder.result()
    assert first.returncode == 0, first.stderr
    assert second.returncode != 0 and "RS001" in second.stderr, (second.returncode, second.stderr)
    head = sql("SELECT vc.head();")
    assert sql(f"SELECT vc.merge_base('{head}','{revision['rsc-a']}')='{revision['rsc-a']}' "
               f"AND vc.merge_base('{head}','{revision['rsc-b']}')<>'{revision['rsc-b']}';") == "t"
    assert sql("SELECT string_agg(stock::text,',' ORDER BY id) FROM app.products WHERE id IN (3213,3214);") == "45,100"
    return {"name": "crossing_read_sets_at_the_same_time_refuse_the_second", "passed": True,
            "ms": round((time.perf_counter()-start)*1000, 2), "second_waited_ms": round(waited_ms, 2)}


def session():
    """A long-lived Session must behave like one fresh psql process per call."""
    sql("CREATE TABLE public.session_probe(id int PRIMARY KEY);")
    try:
        with CONNECTION.session(DBNAME) as s:
            result = s.execute("SELECT 1 AS one \\gset\nSELECT :one + 1;")
            assert result.returncode == 0 and result.stdout.strip() == "2", result
            assert s.sql("SELECT 5") == "5"  # unterminated last statement still runs
            # Stops at the first error; an earlier autocommit statement stays.
            result = s.execute("INSERT INTO session_probe VALUES(1);\nSELECT 1/0;\nINSERT INTO session_probe VALUES(2);")
            assert result.returncode and "22012" in result.stderr, result
            # A failed or unfinished transaction is rolled back, as at process exit.
            assert s.execute("BEGIN;\nINSERT INTO session_probe VALUES(3);\nSELECT 1/0;\nCOMMIT;").returncode
            assert s.execute("BEGIN;\nINSERT INTO session_probe VALUES(4);").returncode == 0
            assert s.sql("SELECT string_agg(id::text,',' ORDER BY id) FROM session_probe;") == "1"
            # Server settings, role, temp tables, advisory locks and psql variables do not leak.
            s.sql("""
              SET search_path=pg_catalog; SELECT set_config('vc.read_revision',vc.head()::text,false);
              CREATE TEMP TABLE leak(); SELECT pg_advisory_lock(42); SET ROLE agentdb_client;
              SELECT 7 AS leaked \\gset
              \\set ON_ERROR_STOP off
              \\set VERBOSITY terse
            """)
            state = """
              SELECT current_user,current_setting('search_path'),coalesce(current_setting('vc.read_revision',true),''),
                (SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()),
                to_regclass('pg_temp.leak') IS NULL;
              \\echo :{?leaked}
            """
            assert s.sql(state) == sql(state), (s.sql(state), sql(state))
            result = s.execute("SELECT 1/0;\nINSERT INTO session_probe VALUES(5);")
            assert result.returncode and "22012: division by zero" in result.stderr, result
            assert s.sql("SELECT count(*) FROM session_probe;") == "1"
            fresh = CONNECTION.execute("SELECT 1/0;", DBNAME)
            assert s.execute("SELECT 1/0;").stderr == fresh.stderr, fresh.stderr
            # The stale-head retry path that merge-all uses, inside one session.
            base = s.sql("SELECT vc.head();")
            proposal = s.sql(f"""
              SELECT vc.fork('session-check','{base}') \\gset
              BEGIN;
              SELECT vc.begin_change('session-check','{base}') AS draft \\gset
              UPDATE agent.products SET stock=7 WHERE id=3998;
              SELECT vc.finish_change() AS revision \\gset
              COMMIT;
              SELECT :'revision';
            """)
            merge = "BEGIN;\nSELECT vc.merge('main','{}','{}') AS merged \\gset\nCOMMIT;\nSELECT :'merged';"
            stale = s.execute(merge.format(proposal, "00000000-0000-0000-0000-000000000001"))
            assert stale.returncode and "40001" in stale.stderr and stale.stdout == "", stale
            merged = s.sql(merge.format(proposal, s.sql("SELECT vc.head();")))
            assert sql("SELECT vc.head();") == merged
            assert sql("SELECT stock FROM app.products WHERE id=3998;") == "7"
            # A timeout ends the process; the next call starts a new one.
            try:
                s.execute("SELECT pg_sleep(3);", timeout=1)
            except subprocess.TimeoutExpired:
                pass
            else:
                raise AssertionError("Session timeout was not enforced")
            assert s.sql("SELECT 1;") == "1"
            timings = []
            for _ in range(10):
                began = time.perf_counter()
                s.sql("SELECT 1;")
                timings.append((time.perf_counter()-began)*1000)
        restricted = copy.copy(CONNECTION)
        restricted.role = "agentdb_client"
        with restricted.session(DBNAME) as s:
            assert s.sql("RESET ROLE; SELECT current_user;") == CONNECTION.settings["user"]
            assert s.sql("SELECT current_user;") == "agentdb_client"  # role is set again per call
        spawned = []
        for _ in range(3):
            began = time.perf_counter()
            sql("SELECT 1;")
            spawned.append((time.perf_counter()-began)*1000)
    finally:
        sql("DROP TABLE public.session_probe;")
    print("PASS long_lived_session_matches_fresh_process_per_call", flush=True)
    return dict(session_select1_median_ms=round(statistics.median(timings), 2),
                spawned_select1_median_ms=round(statistics.median(spawned), 2),
                checks=["stop_at_first_error", "rollback_open_transaction", "no_state_or_variable_leak",
                        "per_call_role", "stale_head_retry", "timeout_restarts_process"])


def main():
    global CONNECTION
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    args = parser.parse_args()
    try:
        CONNECTION = Connection(args)
        details = CONNECTION.check()
        print("Using existing PostgreSQL " + details["server_version"], flush=True)
        CONNECTION.create_database(DBNAME)
        print("Installing into dedicated test database " + DBNAME, flush=True)
        CONNECTION.install(DBNAME)
        sql("""
          BEGIN; SELECT vc.begin_change('main',vc.head(),'seed','4000 baseline rows');
          INSERT INTO app.products SELECT n,'sku-'||n,'product-'||n,10,100 FROM generate_series(1,4000) n;
          SELECT vc.write_file('src/app.py',convert_to('base code','UTF8'));
          SELECT vc.finish_change(); COMMIT;
          SELECT vc.materialize(vc.head(),vc.table_id('app.products'));
        """)
        tests = test_suite()
        result = dict(postgresql=sql("SHOW server_version;"), database=DBNAME, connection=details,
                      tests=tests, concurrency=concurrency(), session=session(),
                      merge_semantics=merge_semantics(), read_set_merge=read_set_merge(),
                      native_products=int(sql("SELECT count(*) FROM app.products;")),
                      unfinished_transactions=int(sql("SELECT count(*) FROM vc.work_tx;")))
        assert result["native_products"] == 4000
        assert result["unfinished_transactions"] == 0
        (ROOT / "test-results-local.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"tests_passed":len(tests),"concurrency":result["concurrency"]},indent=2),flush=True)
        print("Retained test database: " + DBNAME + "; existing PostgreSQL server remains running.", flush=True)
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
