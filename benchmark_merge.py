"""Where does the time go when ten branches all merge into main?

Local synthetic measurement on the existing PostgreSQL server only. Every run
gets its own dedicated agentdb_test_* database cloned from one prepared
template, so all runs start from identical history. vc.merge and the version
engine are called unchanged; the instrumented client only adds clock_timestamp()
probes and takes the main branch row lock itself before calling vc.merge, so
lock wait and SQL execution can be told apart.

Modes:
  baseline               review_local.py merge-all client, uninstrumented
  spawn_concurrent       10 writers, one wsl.exe + psql process per call
  spawn_serialized       writers take turns with the known head (no 40001)
  persistent_concurrent  10 writers, one long-lived db.Session each
  persistent_serialized  persistent sessions taking turns
  profile                persistent_serialized with track_functions=all plus
                         EXPLAIN ANALYZE of the merge's component queries
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
import uuid

from db import FLAGS, Connection, add_connection_arguments
from review_local import seed
import run_tests

ROOT = Path(__file__).resolve().parent
WRITERS = 10
MODES = ("baseline", "spawn_concurrent", "spawn_serialized", "persistent_concurrent", "persistent_serialized")


def connect(args, database, role=None):
    options = copy.copy(args)
    options.database, options.role = database, role
    return Connection(options)


def ms(seconds):
    return round(seconds * 1000, 3)


def summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return dict(n=len(values), median=round(statistics.median(values), 3),
                min=round(min(values), 3), max=round(max(values), 3))


def stamp(label):
    return f"SELECT 'T','{label}',extract(epoch FROM clock_timestamp());"


def merge_script(item, expected):
    # Taking the main row lock first is a no-op for begin_change's own FOR
    # UPDATE in the same transaction; its expected-head check is unchanged.
    return f"""{stamp('start')}
BEGIN;
SELECT 1 FROM vc.branch WHERE name='main' FOR UPDATE;
{stamp('locked')}
SELECT vc.merge('main','{item['revision']}','{expected}','synthetic-{item['agent']}') AS merged \\gset
{stamp('merged')}
COMMIT;
{stamp('committed')}
SELECT 'R',:'merged';"""


def parse(lines):
    stamps, merged = {}, None
    for line in lines:
        fields = line.split("|")
        if fields[0] == "T" and len(fields) == 3:
            stamps[fields[1]] = float(fields[2])
        elif fields[0] == "R" and len(fields) == 2:
            merged = fields[1]
    return stamps, merged


class Writer:
    """Runs one agent's merge with the review_local retry rule, timing each call."""

    def __init__(self, args, database, persistent, role=None):
        self.connection = connect(args, database, role)
        began = time.perf_counter()
        # db.Session keeps Connection.execute semantics, so both clients run the same script.
        self.client = self.connection.session(database) if persistent else self.connection
        self.open_ms = ms(time.perf_counter() - began) if persistent else None
        self.calls = []

    def _merge_once(self, item, expected):
        began = time.perf_counter()
        result = self.client.execute(merge_script(item, expected))
        wall = time.perf_counter() - began
        stamps, merged = parse(result.stdout.splitlines())
        failed = None
        if result.returncode or merged is None:
            failed = "40001" if "40001" in result.stderr else "other"
        return wall, stamps, merged, failed, result.stderr

    def _read_head(self):
        began = time.perf_counter()
        head = self.client.sql("SELECT vc.head();")
        self.calls.append(dict(kind="reread", wall_ms=ms(time.perf_counter() - began)))
        return head

    def land(self, item, expected):
        began = time.perf_counter()
        for attempt in range(1, 31):
            wall, stamps, merged, failed, stderr = self._merge_once(item, expected)
            call = dict(kind="merge", attempt=attempt, expected=expected, wall_ms=ms(wall),
                        server=stamps, sqlstate=failed or "00000")
            self.calls.append(call)
            if merged:
                return dict(**item, merged_head=merged, attempts=attempt, stale_head_retries=attempt-1,
                            merge_wall_ms=ms(time.perf_counter() - began), calls=self.calls)
            if failed != "40001":
                raise AssertionError(stderr)  # Never retry semantic/constraint conflicts blindly.
            expected = self._read_head()
        raise AssertionError("Merge retry bound exhausted")

    def close(self):
        if self.client is not self.connection:
            self.client.close()


# Server stamps come from the WSL guest clock, client wall time from Windows
# perf_counter. They agree to about +-2 ms per call, but the guest clock is
# occasionally stepped by time sync; a call whose stamps run backwards or exceed
# its own wall time by more than this is not split.
CLOCK_TOLERANCE_MS = 10.0


def clock_ok(call):
    s = call["server"]
    order = [s[k] for k in ("start", "locked", "merged", "committed") if k in s]
    if any(b < a for a, b in zip(order, order[1:])):
        return False
    return not order or (order[-1] - order[0]) * 1000 <= call["wall_ms"] + CLOCK_TOLERANCE_MS


def breakdown(writer):
    """Split one writer's merge wall time into exclusive parts (ms)."""
    parts = dict(final_client_overhead=0.0, final_lock_wait=0.0, merge_sql=0.0, commit=0.0,
                 retry_client_overhead=0.0, retry_lock_wait=0.0, retry_reread=0.0,
                 clock_step_unattributed=0.0)
    for call in writer["calls"]:
        if call["kind"] == "reread":
            parts["retry_reread"] += call["wall_ms"]
            continue
        if not clock_ok(call):
            parts["clock_step_unattributed"] += call["wall_ms"]
            continue
        s = call["server"]
        if call["sqlstate"] == "00000":
            server = (s["committed"] - s["start"]) * 1000
            parts["final_client_overhead"] += call["wall_ms"] - server
            parts["final_lock_wait"] += (s["locked"] - s["start"]) * 1000
            parts["merge_sql"] += (s["merged"] - s["locked"]) * 1000
            parts["commit"] += (s["committed"] - s["merged"]) * 1000
        else:
            # The 40001 is raised by begin_change right after the lock is granted;
            # the last server stamp is "locked", the error follows within microseconds.
            server = (s["locked"] - s["start"]) * 1000 if "locked" in s else 0.0
            parts["retry_client_overhead"] += call["wall_ms"] - server
            parts["retry_lock_wait"] += server
    return {k: round(v, 3) for k, v in parts.items()}


def lock_timeline(landed):
    """Server-side view of the main row lock across all writers in one run."""
    steps = sum(not clock_ok(c) for w in landed for c in w["calls"] if c["kind"] == "merge")
    if steps:
        # Stamps before and after a clock step are not comparable across calls.
        return dict(clock_step_detected=True, calls_affected=steps)
    starts = [c["server"]["start"] for w in landed for c in w["calls"] if c["kind"] == "merge" and "start" in c["server"]]
    held = sorted((c["server"]["locked"], c["server"]["committed"]) for w in landed for c in w["calls"]
                  if c["kind"] == "merge" and c["sqlstate"] == "00000")
    span = held[-1][1] - min(starts)
    busy = sum(end - begin for begin, end in held)
    gaps = [held[i+1][0] - held[i][1] for i in range(len(held)-1)]
    return dict(server_span_ms=ms(span), lock_held_by_winners_ms=ms(busy),
                lock_free_ms=ms(span - busy), lock_held_fraction=round(busy / span, 4),
                first_start_to_first_lock_ms=ms(held[0][0] - min(starts)),
                handoff_gap_ms=summary([ms(g) for g in gaps]))


def run_writers(args, database, base, proposals, mode):
    # Instrumented writers connect as the database owner: agentdb_client may not
    # lock vc.branch directly. vc.merge is SECURITY DEFINER, so it runs as the
    # owner either way; the baseline keeps the review_local role.
    persistent = mode.startswith("persistent")
    writers = [Writer(args, database, persistent) for _ in proposals]
    try:
        session_open = [w.open_ms for w in writers if w.open_ms is not None]
        started = time.perf_counter()
        if mode.endswith("serialized"):
            landed, expected = [], base
            for writer, item in zip(writers, proposals):
                landed.append(writer.land(item, expected))
                expected = landed[-1]["merged_head"]
        else:
            barrier = threading.Barrier(len(writers))
            def land(pair):
                barrier.wait(timeout=60)
                return pair[0].land(pair[1], base)
            with ThreadPoolExecutor(max_workers=len(writers)) as pool:
                landed = list(pool.map(land, zip(writers, proposals)))
        phase = time.perf_counter() - started
    finally:
        for writer in writers:
            writer.close()
    return landed, ms(phase), session_open


def baseline(worker, base, proposals):
    """The merge phase of review_local.merge_all, verbatim apart from the setup split."""
    barrier = threading.Barrier(len(proposals))
    def land(item):
        barrier.wait(timeout=30)
        began, expected, stale = time.perf_counter(), base, 0
        for attempt in range(1, 31):
            result = worker.execute(f"""
              BEGIN;
              SELECT vc.merge('main','{item['revision']}','{expected}','synthetic-{item['agent']}') AS merged \\gset
              COMMIT;
              SELECT :'merged';
            """)
            if result.returncode == 0:
                return dict(**item, merged_head=result.stdout.strip(), attempts=attempt, stale_head_retries=stale,
                            merge_wall_ms=ms(time.perf_counter()-began))
            if "40001" not in result.stderr:
                raise AssertionError(result.stderr)
            stale += 1
            expected = worker.sql("SELECT vc.head();")
        raise AssertionError("Merge retry bound exhausted")
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(proposals)) as pool:
        landed = list(pool.map(land, proposals))
    return landed, ms(time.perf_counter() - started)


def verify(owner, base, landed):
    final = owner.sql("SELECT vc.head();")
    revisions = ",".join(f"'{item['revision']}'::uuid" for item in landed)
    files = " AND ".join(f"convert_from(vc.read_file('{final}','src/agent_{item['agent']}.py'),'UTF8')='PRICE = {100+item['agent']}'"
                         for item in landed)
    checks = json.loads(owner.sql(f"""
      SELECT json_build_object(
        'rows',(SELECT count(*) FROM app.products),
        'changed_ok',(SELECT bool_and(price=100+((id-1)/10)) FROM app.products WHERE id<=100),
        'unchanged_ok',(SELECT bool_and(price=10) FROM app.products WHERE id>100),
        'base_fixed',(SELECT bool_and((doc->>'price')::numeric=10) FROM vc.rows_at('{base}',vc.table_id('app.products'))),
        'files_ok',({files}),
        'sources_reachable',(SELECT count(*) FROM vc._ancestors('{final}') WHERE id IN ({revisions})),
        'unfinished',(SELECT count(*) FROM vc.work_tx));
    """))
    ok = (checks["rows"] == 4000 and checks["changed_ok"] and checks["unchanged_ok"] and checks["base_fixed"]
          and checks["files_ok"] and checks["sources_reachable"] == len(landed) and checks["unfinished"] == 0)
    if not ok:
        raise AssertionError(checks)
    return dict(final_head=final, **checks)


def prepare_template(args, owner, name):
    owner.create_database(name)
    template = connect(args, name)
    template.install(name)
    seed(template)
    # review_local runs the original assertions in the same database before
    # merge_all; repeat that so history depth matches the earlier record.
    run_tests.CONNECTION, run_tests.DBNAME = template, name
    regression = run_tests.test_suite()
    base = template.sql("SELECT vc.head();")
    worker = connect(args, name, "agentdb_client")
    proposals = []
    for i in range(WRITERS):
        # Same branch commit as review_local.merge_all, done one at a time.
        revision = worker.sql(f"""
          SELECT vc.fork('merge-all-{i}','{base}');
          BEGIN;
          SELECT vc.begin_change('merge-all-{i}','{base}','synthetic-{i}','ten rows and code') AS draft \\gset
          UPDATE agent.products SET price={100+i} WHERE id BETWEEN {10*i+1} AND {10*i+10};
          SELECT vc.write_file('src/agent_{i}.py',convert_to('PRICE = {100+i}','UTF8')) AS hash \\gset
          SELECT vc.finish_change() AS revision \\gset
          COMMIT;
          SELECT :'revision';
        """).splitlines()[-1]
        proposals.append(dict(agent=i, revision=revision))
    history = json.loads(template.sql("""
      SELECT json_build_object('changesets',(SELECT count(*) FROM vc.changeset),
        'row_changes',(SELECT count(*) FROM vc.row_change),'paths',(SELECT count(DISTINCT path) FROM vc.path_change),
        'main_first_parent_depth',(SELECT count(*)-1 FROM vc.history(vc.head(),1000)));
    """))
    return dict(database=name, base=base, proposals=proposals, regression_tests=len(regression), history=history)


def explain_ms(owner, statement, repeats=5):
    times = []
    for _ in range(repeats):
        plan = json.loads(owner.sql("EXPLAIN (ANALYZE, FORMAT JSON) " + statement))
        times.append(plan[0]["Execution Time"])
    return summary(times)


def components(owner, head, source):
    """Timed pieces of one vc.merge call, run read-only outside vc.merge."""
    table = owner.sql("SELECT vc.table_id('app.products');")
    base_id = owner.sql(f"SELECT vc.merge_base('{head}','{source}');")
    changed = int(owner.sql(f"SELECT count(*) FROM vc.diff_rows('{base_id}','{source}','{table}');"))
    paths = int(owner.sql("SELECT count(DISTINCT path) FROM vc.path_change;"))
    return dict(
        target_head=head, source=source, changed_rows=changed, distinct_paths=paths,
        source_in_target_ancestors=explain_ms(owner, f"SELECT 1 FROM vc._ancestors('{head}') WHERE id='{source}'"),
        merge_base=explain_ms(owner, f"SELECT vc.merge_base('{head}','{source}')"),
        diff_rows=explain_ms(owner, f"SELECT * FROM vc.diff_rows('{base_id}','{source}','{table}')"),
        # Before 004_merge_batch, vc.merge ran one full rows_at per changed row to
        # resolve "ours"; it now runs the batched form once per table.
        rows_at_single_key=explain_ms(owner, f"SELECT r.doc FROM vc.rows_at('{head}','{table}') r WHERE r.pk='{{\"id\": 1}}'::jsonb"),
        rows_at_batched_keys=explain_ms(owner, f"""SELECT r.pk,r.doc FROM vc.rows_at('{head}','{table}') r
          WHERE r.pk IN (SELECT pk FROM vc.diff_rows('{base_id}','{source}','{table}'))"""),
        file_state_loop=explain_ms(owner, f"""SELECT vc._file_state('{base_id}',p.path),vc._file_state('{source}',p.path),
          vc._file_state('{head}',p.path) FROM (SELECT DISTINCT path FROM vc.path_change) p"""))


def profile(args, owner, template, name):
    owner.sql(f'CREATE DATABASE "{name}" TEMPLATE "{template["database"]}";', owner.settings["maintenance_database"])
    owner.sql(f"ALTER DATABASE \"{name}\" SET track_functions='all';", owner.settings["maintenance_database"])
    db = connect(args, name)
    proposals = template["proposals"]
    before = components(db, template["base"], proposals[0]["revision"])
    db.sql("SELECT pg_stat_reset();")
    landed, phase, _ = run_writers(args, name, template["base"], proposals, "persistent_serialized")
    time.sleep(1.5)  # sessions have exited; let their pending function stats arrive
    stats = json.loads(db.sql("""
      SELECT json_agg(json_build_object('function',schemaname||'.'||funcname,'calls',calls,
        'total_ms',round(total_time::numeric,3),'self_ms',round(self_time::numeric,3)) ORDER BY total_time DESC)
      FROM pg_stat_user_functions WHERE schemaname='vc';
    """))
    previous = landed[-2]["merged_head"]
    after = components(db, previous, proposals[-1]["revision"])
    return dict(database=name, merge_phase_wall_ms=phase, verification=verify(db, template["base"], landed),
                merge_sql_ms=[breakdown(w)["merge_sql"] for w in landed],
                function_stats=stats, components_first_merge=before, components_last_merge=after,
                note="function total_ms includes callees; self_ms excludes them. Component timings are EXPLAIN ANALYZE "
                     "medians of the same queries vc.merge issues, run read-only outside it.")


def spawn_probe(owner, database, rounds=3):
    sequential = []
    for _ in range(10):
        began = time.perf_counter()
        owner.sql("SELECT 1;", database)
        sequential.append(ms(time.perf_counter() - began))
    concurrent = []
    for _ in range(rounds):
        barrier = threading.Barrier(WRITERS)
        def one(_):
            barrier.wait(timeout=30)
            began = time.perf_counter()
            owner.sql("SELECT 1;", database)
            return ms(time.perf_counter() - began)
        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            concurrent.extend(pool.map(one, range(WRITERS)))
    began = time.perf_counter()
    session = owner.session(database)
    opened = ms(time.perf_counter() - began)
    roundtrip = []
    for _ in range(20):
        began = time.perf_counter()
        session.sql("SELECT 1;")
        roundtrip.append(ms(time.perf_counter() - began))
    session.close()
    return dict(spawn_select1_sequential_ms=summary(sequential), spawn_select1_10_concurrent_ms=summary(concurrent),
                persistent_session_open_ms=opened, persistent_select1_roundtrip_ms=summary(roundtrip),
                raw=dict(sequential=sequential, concurrent=concurrent, roundtrip=roundtrip))


def run_once(args, owner, template, mode, repetition, name):
    owner.sql(f'CREATE DATABASE "{name}" TEMPLATE "{template["database"]}";', owner.settings["maintenance_database"])
    db = connect(args, name)
    proposals, base = template["proposals"], template["base"]
    record = dict(mode=mode, repetition=repetition, database=name)
    if mode == "baseline":
        landed, phase = baseline(connect(args, name, "agentdb_client"), base, proposals)
    else:
        landed, phase, session_open = run_writers(args, name, base, proposals, mode)
        if session_open:
            record["session_open_ms"] = summary(session_open)
    latencies = [w["merge_wall_ms"] for w in landed]
    record.update(merge_phase_wall_ms=phase, merge_median_ms=round(statistics.median(latencies), 3),
                  merge_max_ms=max(latencies), stale_head_retries=sum(w["stale_head_retries"] for w in landed),
                  verification=verify(db, base, landed), writers=landed)
    return derive(record)


def derive(record):
    """Recompute the breakdown and lock view from the raw per-call records."""
    if record["mode"] == "baseline":
        return record
    parts = [breakdown(w) for w in record["writers"]]
    record["writer_breakdown_sum_ms"] = {k: round(sum(p[k] for p in parts), 3) for k in parts[0]}
    record["writer_breakdown_median_ms"] = {k: round(statistics.median(p[k] for p in parts), 3) for k in parts[0]}
    record["lock_timeline"] = lock_timeline(record["writers"])
    return record


def aggregate(runs):
    out = {}
    for mode in MODES:
        mine = [r for r in runs if r["mode"] == mode]
        if not mine:
            continue
        entry = dict(runs=len(mine),
                     merge_phase_wall_ms=summary([r["merge_phase_wall_ms"] for r in mine]),
                     merge_median_ms=summary([r["merge_median_ms"] for r in mine]),
                     stale_head_retries=summary([r["stale_head_retries"] for r in mine]))
        if "writer_breakdown_sum_ms" in mine[0]:
            entry["writer_breakdown_sum_ms"] = {k: summary([r["writer_breakdown_sum_ms"][k] for r in mine])
                                                for k in mine[0]["writer_breakdown_sum_ms"]}
            clean = [r["lock_timeline"] for r in mine if not r["lock_timeline"].get("clock_step_detected")]
            entry["lock_timeline_runs"] = len(clean)
            for key in ("server_span_ms", "lock_held_by_winners_ms", "lock_free_ms", "lock_held_fraction"):
                entry[key] = summary([t[key] for t in clean])
            entry["handoff_gap_median_ms"] = summary([t["handoff_gap_ms"]["median"] for t in clean])
        out[mode] = entry
    return out


def environment(owner):
    details = owner.check()
    settings = json.loads(owner.sql("""
      SELECT json_object_agg(name,setting) FROM pg_settings WHERE name IN ('shared_buffers','synchronous_commit',
        'fsync','max_connections','work_mem','jit','default_transaction_isolation','track_functions');
    """, owner.settings["maintenance_database"]))
    try:
        wsl = subprocess.run(["wsl.exe", "--version"], capture_output=True, timeout=30, creationflags=FLAGS)
        wsl_version = wsl.stdout.decode("utf-16-le", "replace").replace("\x00", "").strip().splitlines()[:2]
    except (OSError, subprocess.TimeoutExpired):
        wsl_version = None
    return dict(postgresql=details, server_settings=settings, server_version_string=owner.sql(
                    "SELECT version();", owner.settings["maintenance_database"]),
                client_os=platform.platform(), client_python=sys.version.split()[0], cpu_count=os.cpu_count(),
                wsl_version=wsl_version, transport="wsl.exe -> /usr/bin/psql -> unix socket")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("--runs", type=int, default=3, help="Repetitions per mode (default 3)")
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--profile-runs", type=int, default=3, help="track_functions profiling runs (0 to skip)")
    parser.add_argument("--output", type=Path, default=ROOT / "merge-benchmark-results.json")
    parser.add_argument("--reanalyze", action="store_true",
                        help="Rebuild derived fields of --output from its raw calls; no database access")
    args = parser.parse_args()
    if args.reanalyze:
        output = json.loads(args.output.read_text(encoding="utf-8"))
        output["runs"] = [derive(r) for r in output["runs"]]
        output["summary"] = aggregate(output["runs"])
        output["clock_tolerance_ms"] = CLOCK_TOLERANCE_MS
        args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(output["summary"], indent=2), flush=True)
        return
    prefix = "agentdb_test_mergebench_" + uuid.uuid4().hex[:8]
    owner = connect(args, prefix + "_tpl")
    started = datetime.now(timezone.utc)
    env = environment(owner)
    print("Preparing template " + prefix + "_tpl", flush=True)
    template = prepare_template(args, owner, prefix + "_tpl")
    probe = spawn_probe(owner, template["database"])
    print("Spawn probe: " + json.dumps({k: v for k, v in probe.items() if k != "raw"}), flush=True)
    runs = []
    # Interleave modes per repetition so WSL warm-up or drift does not land on one mode.
    for repetition in range(1, args.runs + 1):
        for mode in args.modes:
            name = f"{prefix}_{mode[:4]}{'c' if mode.endswith('concurrent') else 's'}_{repetition}"
            if mode == "baseline":
                name = f"{prefix}_base_{repetition}"
            record = run_once(args, owner, template, mode, repetition, name)
            runs.append(record)
            print(f"{mode} #{repetition}: phase {record['merge_phase_wall_ms']} ms, median {record['merge_median_ms']} ms, "
                  f"retries {record['stale_head_retries']}", flush=True)
    output = dict(generated_at=started.isoformat(), finished_at=datetime.now(timezone.utc).isoformat(),
                  clock_tolerance_ms=CLOCK_TOLERANCE_MS,
                  scope="Local synthetic load on the existing WSL PostgreSQL; 10 script writers, not LLM agents; "
                        "not a production capacity measurement. No cloud service used.",
                  environment=env, template=template, spawn_probe=probe, summary=aggregate(runs), runs=runs)
    output["profile"] = []
    for repetition in range(1, args.profile_runs + 1):
        output["profile"].append(profile(args, owner, template, f"{prefix}_prof_{repetition}"))
        print(f"profile #{repetition} merge_sql_ms: " + json.dumps(output["profile"][-1]["merge_sql_ms"]), flush=True)
    output["databases_retained"] = ([template["database"]] + [r["database"] for r in runs] +
                                    [p["database"] for p in output["profile"]])
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output["summary"], indent=2), flush=True)
    print("Raw results: " + str(args.output), flush=True)


if __name__ == "__main__":
    main()
