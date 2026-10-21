"""What does vc.merge_validated's read check cost next to the merge itself?

Local synthetic measurement on the existing PostgreSQL server only, in one
dedicated agentdb_test_rsbench_* database. Seeded as review_local.py does (4,000
products, snapshot at the seed), then main gains `commits` changes since that
snapshot, each to a row nobody reads; a source branch forked at the seed changes
one other row. For each (commits, keys) case, server-side clock_timestamp()
probes inside one rolled-back transaction time, `--repeats` times each:

  merge       vc.merge with the current head (what merge_validated finishes with)
  validated   vc.merge_validated with `keys` unchanged read rows (check + merge)
  check       the check alone: vc._rows_at_keys at the merge base and at the head
  head_only   vc._rows_at_keys at the head only (half of the check's two walks)
  diff_rows   vc.diff_rows(base, head) for the table, the experiment prototype's check

Every merge is undone in a subtransaction, so all repeats see the same history.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db import Connection, add_connection_arguments
from review_local import seed

CASES = [(commits, keys) for commits in (10, 100, 1000) for keys in (1, 10, 100)]


def connect(args, database):
    options = copy.copy(args)
    options.database, options.role = database, None
    return Connection(options)


def grow(owner, have, total):
    """Commit changes to main until `total` changes follow the seed, each to a row in 3001-3500."""
    owner.sql(f"""
      DO $grow$ BEGIN
        FOR i IN {have}..{total}-1 LOOP
          PERFORM vc.begin_change('main',vc.head(),'bench','grow '||i);
          UPDATE app.products SET price=price+1 WHERE id=3001+i%500;
          PERFORM vc.finish_change();
        END LOOP;
      END $grow$;""")


def measure(owner, name, source, base, keys, repeats):
    reads = json.dumps({"TID": [{"id": 1 + k} for k in range(keys)]})
    key_array = "ARRAY[" + ",".join(f"'{{\"id\":{1 + k}}}'" for k in range(keys)) + "]::jsonb[]"
    result = owner.execute(f"""BEGIN;
DO $m$ DECLARE t0 timestamptz; tid uuid:=vc.table_id('app.products'); h uuid:=vc.head(); n bigint;
  rs jsonb:=replace('{reads}','TID',vc.table_id('app.products')::text)::jsonb;
  res jsonb:='{{"merge":[],"validated":[],"check":[],"head_only":[],"diff_rows":[]}}';
BEGIN
  FOR i IN 1..{repeats} LOOP
    t0:=clock_timestamp();
    BEGIN PERFORM vc.merge('main','{source}',h,'bench'); RAISE EXCEPTION 'undo' USING ERRCODE='XX001';
    EXCEPTION WHEN SQLSTATE 'XX001' THEN NULL; END;
    res:=jsonb_set(res,'{{merge}}',res->'merge'||to_jsonb(extract(epoch FROM clock_timestamp()-t0)*1000));
    t0:=clock_timestamp();
    BEGIN PERFORM vc.merge_validated('main','{source}',rs,'bench'); RAISE EXCEPTION 'undo' USING ERRCODE='XX001';
    EXCEPTION WHEN SQLSTATE 'XX001' THEN NULL; END;
    res:=jsonb_set(res,'{{validated}}',res->'validated'||to_jsonb(extract(epoch FROM clock_timestamp()-t0)*1000));
    t0:=clock_timestamp();
    SELECT count(*) INTO n FROM vc._rows_at_keys('{base}',tid,{key_array}) a
      FULL JOIN vc._rows_at_keys(h,tid,{key_array}) z USING (pk) WHERE a.doc IS DISTINCT FROM z.doc;
    res:=jsonb_set(res,'{{check}}',res->'check'||to_jsonb(extract(epoch FROM clock_timestamp()-t0)*1000));
    t0:=clock_timestamp();
    SELECT count(*) INTO n FROM vc._rows_at_keys(h,tid,{key_array});
    res:=jsonb_set(res,'{{head_only}}',res->'head_only'||to_jsonb(extract(epoch FROM clock_timestamp()-t0)*1000));
    t0:=clock_timestamp();
    SELECT count(*) INTO n FROM vc.diff_rows('{base}',h,tid);
    res:=jsonb_set(res,'{{diff_rows}}',res->'diff_rows'||to_jsonb(extract(epoch FROM clock_timestamp()-t0)*1000));
  END LOOP;
  RAISE NOTICE 'RESULT %',res;
END $m$;
ROLLBACK;""", name)
    if result.returncode:
        raise RuntimeError(result.stderr)
    line = next(l for l in result.stderr.splitlines() if "RESULT " in l)
    times = json.loads(line.split("RESULT ", 1)[1])
    return {k: round(statistics.median(v), 3) for k, v in times.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "read-set-merge-benchmark.json")
    args = parser.parse_args()
    name = "agentdb_test_rsbench_" + uuid.uuid4().hex[:10]
    owner = connect(args, name)
    details = owner.check()
    owner.create_database(name)
    owner.install(name)
    seed(owner)
    base = owner.sql("SELECT vc.head();")
    source = owner.sql(f"""SELECT vc.fork('bench-source','{base}') AS h \\gset
BEGIN;
SELECT vc.begin_change('bench-source','{base}','bench','source') AS d \\gset
UPDATE agent.products SET stock=1 WHERE id=3999;
SELECT vc.finish_change() AS revision \\gset
COMMIT;
SELECT :'revision';""")
    cases, have = [], 0
    for commits, keys in CASES:
        grow(owner, have, commits)
        have = commits
        medians = measure(owner, name, source, base, keys, args.repeats)
        medians["check_share_of_merge"] = round(medians["check"] / medians["merge"], 3)
        cases.append(dict(commits_since_snapshot=commits, read_keys=keys, median_ms=medians))
        print(json.dumps(cases[-1]), flush=True)
    result = dict(recorded_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"), database=name,
                  postgresql=details["server_version"], repeats=args.repeats, products=4000,
                  setup="review_local seed with a snapshot; main grows by one row change per commit; source forked "
                        "at the seed changes one row; read rows are never changed, so every check passes",
                  cases=cases, real_cloud_verified=False,
                  note="Local dedicated test database on the existing PostgreSQL server; server-side "
                       "clock_timestamp() medians; the project database was not touched.")
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
