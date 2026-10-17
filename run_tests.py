"""Run integration tests in a new database on the existing local PostgreSQL server.

Uses the same connection settings as db.py. Never initializes, starts or stops
a PostgreSQL instance. The generated test database is retained for inspection.
"""
import argparse
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
                      tests=tests, concurrency=concurrency(),
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
