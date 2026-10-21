"""Read-set validation vs. the stale-head check on the ten-writer merge race.

Pre-registered in docs/READSET_EXPERIMENT.md. Local synthetic measurement on the
existing PostgreSQL server only: 10 scripted writers per run, no model, no cloud
service. One template database gets the version engine, the 4,000-row seed of
review_local.py, one commit setting the bundle stock, and readset.sql; every run
clones it, so all runs start from identical history. vc.* is called unchanged.

The registered schedule is 5 arms x 3 workloads x seeds 1-5 (75 runs). --seeds,
--workloads and --arms run a subset, recorded as complete_schedule=false. Each
run database is dropped once its checks are recorded, unless --keep-databases;
the template is kept. Every SQL call is saved verbatim in calls.jsonl.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import statistics
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from agent_lab import MERGE_LOCK_TIMEOUT
from db import Connection, add_connection_arguments, sqlstate
from review_local import seed
import oracle

WRITERS = 10
ARMS = ("head_blind", "head_rethink", "rs_declared", "rs_undercite", "rs_tracked")
SHORT = dict(head_blind="hb", head_rethink="hr", rs_declared="rd", rs_undercite="ru", rs_tracked="rt")
WORKLOADS = ("W1", "W2", "W3")
SEEDS = (1, 2, 3, 4, 5)
FIRST_BUNDLE_ROW, BUNDLE_STOCK, SALE, BUNDLE_MINIMUM = 3001, 60, 15, 100
BUNDLE_ROWS = range(FIRST_BUNDLE_ROW, FIRST_BUNDLE_ROW + 10)
BROWSE_ROWS, BROWSE_RANGE = 20, range(1, 1001)
UNDERCITE = 0.5
# The bounds of review_local.merge_all: 30 attempts that are not lock timeouts, and 60 lock timeouts.
MAX_ATTEMPTS, MAX_LOCK_TIMEOUTS = 30, 60
# As scripts/benchmark_merge.py: server stamps (WSL clock) and client wall time
# (Windows) agree to about +-2 ms; a call that disagrees by more is not timed.
CLOCK_TOLERANCE_MS = 10.0
STAMPS = ("start", "locked", "validated", "merged", "committed")


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


# --- Workloads: each writer's ground truth --------------------------------------

def disjoint(writer):
    return dict(writer=writer, kind="disjoint", writes=list(range(10*writer + 1, 10*writer + 11)), depends=[])


def bundle_seller(writer, slot):
    own, partner = FIRST_BUNDLE_ROW + slot, FIRST_BUNDLE_ROW + (slot ^ 1)
    return dict(writer=writer, kind="bundle", own=own, partner=partner, writes=[own], depends=[own, partner])


def workload_writers(workload):
    """The rows each writer writes and the rows its decision truly depends on."""
    if workload == "W1":
        return [disjoint(i) for i in range(WRITERS)]
    if workload == "W2":
        return [bundle_seller(i, i) for i in range(WRITERS)]
    return [disjoint(i) for i in range(6)] + [bundle_seller(i, i - 6) for i in range(6, WRITERS)]


def bundles(specs):
    return sorted({tuple(sorted((s["own"], s["partner"]))) for s in specs if s["kind"] == "bundle"})


def decide(spec, rows):
    """The writer's decision from the rows it read at its base, or None to abstain."""
    if spec["kind"] == "disjoint":
        price = 100 + spec["writer"]
        return dict(rows={row: {"price": price} for row in spec["writes"]},
                    file=[f"src/agent_{spec['writer']}.py", f"PRICE = {price}"])
    own, partner = rows[spec["own"]]["stock"], rows[spec["partner"]]["stock"]
    if own + partner - SALE < BUNDLE_MINIMUM:
        return None
    return dict(rows={spec["own"]: {"stock": own - SALE}}, file=None)


def rng(seed_value, workload, writer, purpose):
    return random.Random(f"{seed_value}:{workload}:{writer}:{purpose}")


def browse_rows(spec, seed_value, workload):
    """Rows the writer looks at but does not use; the same in every arm."""
    choices = [row for row in BROWSE_RANGE if row not in spec["writes"]]
    return sorted(rng(seed_value, workload, spec["writer"], "browse").sample(choices, BROWSE_ROWS))


def read_set(arm, spec, browse, seed_value, workload):
    """Keys sent with xr.merge_validated: read and not written. None for the head arms."""
    if arm.startswith("head"):
        return None
    reads = [row for row in spec["depends"] if row not in spec["writes"]]
    if arm == "rs_undercite":
        drop = rng(seed_value, workload, spec["writer"], "undercite")
        reads = [row for row in reads if drop.random() >= UNDERCITE]
    elif arm == "rs_tracked":
        reads = sorted(set(reads) | set(browse))
    return reads


def schedule(seeds, workloads, arms):
    """Runs interleaved by round; the arm order rotates with the seed."""
    order = []
    for seed_value in seeds:
        for workload in workloads:
            k = (seed_value - 1) % len(arms)
            order += [(seed_value, workload, arm) for arm in arms[k:] + arms[:k]]
    return order


# --- psql output ----------------------------------------------------------------

def parse_output(stdout):
    stamps, result = {}, None
    for line in stdout.splitlines():
        fields = line.split("|")
        if fields[0] == "T" and len(fields) == 3 and fields[2]:
            stamps[fields[1]] = float(fields[2])
        elif fields[0] == "R" and len(fields) == 2:
            result = fields[1]
    return stamps, result


def parse_conflicts(stderr):
    """The keys an RS001 reports in its DETAIL, and the validation time in its HINT."""
    detail = re.search(r"^DETAIL:\s+(.*)$", stderr, re.M)
    hint = re.search(r"^HINT:\s+validated_at=([0-9.]+)", stderr, re.M)
    keys = [item["key"] for item in json.loads(detail.group(1))] if detail else []
    return keys, float(hint.group(1)) if hint else None


def clock_ok(stamps, wall_ms):
    order = [stamps[k] for k in STAMPS if k in stamps]
    if any(b < a for a, b in zip(order, order[1:])):
        return False
    return not order or (order[-1] - order[0]) * 1000 <= wall_ms + CLOCK_TOLERANCE_MS


class CallLog:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def write(self, record):
        line = json.dumps(record) + "\n"
        with self.lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)


# --- One writer -----------------------------------------------------------------

class Writer:
    """One scripted agent: reads at its base, decides, proposes on a branch, merges into main."""

    def __init__(self, args, database, run, spec, arm, table_id, seed_value, workload, log):
        self.run, self.spec, self.arm, self.table_id, self.log = run, spec, arm, table_id, log
        self.browse = browse_rows(spec, seed_value, workload)
        self.reads = read_set(arm, spec, self.browse, seed_value, workload)
        self.proposals, self.merges = [], []
        self.counts = dict(resubmits=0, lock_timeouts=0, rethinks=0, useful_rethinks=0, wasted_rethinks=0)
        self.outcome = self.merge_commit = self.error = None
        self.session = connect(args, database, "agentdb_client").session(database)

    def call(self, kind, statement, attempt=None):
        started, began = time.time(), time.perf_counter()
        result = self.session.execute(statement)
        wall = ms(time.perf_counter() - began)
        self.log.write(dict(run=self.run, writer=self.spec["writer"], kind=kind, attempt=attempt, sql=statement,
                            returncode=result.returncode, sqlstate=sqlstate(result.stderr) if result.returncode else "00000",
                            stdout=result.stdout, stderr=result.stderr, client_started=started, wall_ms=wall))
        return result, wall

    def checked(self, kind, statement):
        result, _ = self.call(kind, statement)
        if result.returncode:
            raise RuntimeError(f"{kind} failed: {result.stderr.strip()}")
        return result.stdout

    def head(self):
        return self.checked("reread", "SELECT vc.head();").strip()

    def propose(self, head):
        """Read at head and decide; unless the writer abstains, commit the decision on a new branch."""
        keys = sorted(set(self.spec["depends"]) | set(self.browse))
        literals = ",".join(f"'{{\"id\":{key}}}'" for key in keys)
        rows = json.loads(self.checked("read", f"""SELECT coalesce(json_object_agg(r.pk->>'id',r.doc),'{{}}')
          FROM vc.rows_at('{head}','{self.table_id}') r WHERE r.pk=ANY(ARRAY[{literals}]::jsonb[]);"""))
        rows = {int(key): doc for key, doc in rows.items()}
        decision = decide(self.spec, rows)
        proposal = dict(number=len(self.proposals) + 1, base=head, revision=None, decision=decision,
                        used={key: rows[key] for key in self.spec["depends"]})
        self.proposals.append(proposal)
        if decision is None:
            return proposal
        groups = {}
        for row, values in decision["rows"].items():
            groups.setdefault(tuple(sorted(values.items())), []).append(row)
        updates = "".join(
            "UPDATE agent.products SET " + ",".join(f"{column}={value}" for column, value in values) +
            f" WHERE id IN ({','.join(str(row) for row in sorted(rows))});\n" for values, rows in sorted(groups.items()))
        if decision["file"]:
            path, content = decision["file"]
            updates += f"SELECT vc.write_file('{path}',convert_to('{content}','UTF8')) AS hash \\gset\n"
        branch, author = f"rs-w{self.spec['writer']}-p{proposal['number']}", f"readset-{self.spec['writer']}"
        _, proposal["revision"] = parse_output(self.checked("propose", f"""SELECT vc.fork('{branch}','{head}');
BEGIN;
SELECT vc.begin_change('{branch}','{head}','{author}','{self.run} proposal {proposal['number']}') AS draft \\gset
{updates}SELECT vc.finish_change() AS revision \\gset
COMMIT;
SELECT 'R',:'revision';"""))
        return proposal

    def merge(self, proposal, expected, attempt):
        author = f"readset-{self.spec['writer']}"
        if self.reads is None:
            call = f"xr.merge_head('main','{proposal['revision']}','{expected}','{author}')"
        else:
            reads = json.dumps({self.table_id: [{"id": row} for row in self.reads]})
            call = f"xr.merge_validated('main','{proposal['revision']}','{reads}'::jsonb,'{author}')"
        # The lock is taken first through xr.lock_main so lock wait and SQL can be
        # told apart; the merge functions take it again, which is then a no-op.
        result, wall = self.call("merge", f"""SELECT 'T','start',extract(epoch FROM clock_timestamp());
BEGIN;
SET LOCAL lock_timeout = '{MERGE_LOCK_TIMEOUT}';
SELECT 'T','locked',extract(epoch FROM xr.lock_main('main'));
SELECT {call} AS merged \\gset
SELECT 'T','validated',current_setting('xr.validated');
SELECT 'T','merged',extract(epoch FROM clock_timestamp());
COMMIT;
SELECT 'T','committed',extract(epoch FROM clock_timestamp());
SELECT 'R',:'merged';""", attempt)
        stamps, merged = parse_output(result.stdout)
        code = "00000" if result.returncode == 0 and merged else (sqlstate(result.stderr) or "unknown")
        record = dict(attempt=attempt, proposal=proposal["number"], sqlstate=code, wall_ms=wall, server=stamps)
        if code == "RS001":
            record["conflict_keys"], validated = parse_conflicts(result.stderr)
            if validated is not None:
                stamps["validated"] = validated
        self.merges.append(record)
        return code, merged

    def land(self):
        """Merge the current proposal, handling each rejection as the arm prescribes."""
        proposal, attempts = self.proposals[-1], 0
        expected = proposal["base"]
        while True:
            if proposal["decision"] is None:
                self.outcome = "abstained"
                return
            if attempts >= MAX_ATTEMPTS or self.counts["lock_timeouts"] >= MAX_LOCK_TIMEOUTS:
                self.outcome, self.error = "failed", "retry bound exhausted"
                return
            code, merged = self.merge(proposal, expected, len(self.merges) + 1)
            if code == "00000":
                self.outcome, self.merge_commit = "merged", merged
                return
            if code == "55P03":  # still queued behind main's lock: the same proposal again
                self.counts["lock_timeouts"] += 1
                continue
            attempts += 1
            if code == "40001" and self.arm == "head_blind":
                self.counts["resubmits"] += 1
                expected = self.head()
                continue
            if code not in ("40001", "RS001", "P0001"):
                self.outcome, self.error = "failed", f"unexpected SQLSTATE {code}"
                return
            head = self.head()
            recomputed = self.propose(head)
            self.counts["rethinks"] += 1
            self.counts[oracle.classify_rethink(proposal["decision"], recomputed["decision"]) + "_rethinks"] += 1
            proposal, expected = recomputed, head

    def result(self):
        return dict(writer=self.spec["writer"], kind=self.spec["kind"], outcome=self.outcome, error=self.error,
                    merge_commit=self.merge_commit, read_set=self.reads, browse=self.browse, counts=self.counts,
                    proposals=self.proposals, merges=self.merges)


# --- One run --------------------------------------------------------------------

def run_phase(writers, action):
    """All writers start the action together; an error is recorded on its writer."""
    barrier = threading.Barrier(len(writers))
    def one(writer):
        barrier.wait(timeout=120)
        if writer.outcome == "failed":
            return
        try:
            action(writer)
        except Exception as error:  # a harness or connection failure: kept as a failed writer
            writer.outcome, writer.error = "failed", repr(error)
    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(writers)) as pool:
        list(pool.map(one, writers))
    return ms(time.perf_counter() - began)


def verify(db, template, results):
    """The merge_all checks for this design: rows, untouched rows, base, files, reachability."""
    final = db.sql("SELECT vc.head();")
    landed = [r for r in results if r["outcome"] == "merged"]
    expected = [(row, column, value) for r in landed
                for row, values in r["proposals"][-1]["decision"]["rows"].items() for column, value in values.items()]
    written = sorted({row for row, _, _ in expected})
    values = ",".join(f"({row},'{column}',{value})" for row, column, value in expected) or "(0,'none',0)"
    files = [(r["proposals"][-1]["decision"]["file"]) for r in landed if r["proposals"][-1]["decision"]["file"]]
    file_check = " AND ".join(f"convert_from(vc.read_file('{final}','{path}'),'UTF8')='{content}'"
                              for path, content in files) or "true"
    commits = ",".join(f"'{r['merge_commit']}'::uuid,'{r['proposals'][-1]['revision']}'::uuid" for r in landed)
    template_row = (f"price=10 AND stock=CASE WHEN id BETWEEN {BUNDLE_ROWS[0]} AND {BUNDLE_ROWS[-1]} "
                    f"THEN {BUNDLE_STOCK} ELSE 100 END")
    checks = json.loads(db.sql(f"""
      SELECT json_build_object(
        'rows',(SELECT count(*) FROM app.products),
        'changed_mismatches',(SELECT count(*) FROM (VALUES {values}) x(id,col,val) JOIN app.products p USING(id)
          WHERE CASE x.col WHEN 'price' THEN p.price WHEN 'stock' THEN p.stock END IS DISTINCT FROM x.val),
        'untouched_mismatches',(SELECT count(*) FROM app.products
          WHERE NOT (id=ANY('{{{",".join(map(str, written))}}}'::bigint[])) AND NOT ({template_row})),
        'base_fixed',(SELECT bool_and((doc->>'price')::numeric=10 AND (doc->>'stock')::int=CASE
          WHEN (doc->>'id')::bigint BETWEEN {BUNDLE_ROWS[0]} AND {BUNDLE_ROWS[-1]} THEN {BUNDLE_STOCK} ELSE 100 END)
          FROM vc.rows_at('{template["base"]}','{template["table_id"]}')),
        'files_ok',({file_check}),
        'reachable',(SELECT count(*) FROM vc._ancestors('{final}') WHERE id IN ({commits or "NULL"})),
        'unfinished',(SELECT count(*) FROM vc.work_tx));
    """))
    checks["ok"] = (checks["rows"] == 4000 and checks["changed_mismatches"] == 0 and checks["untouched_mismatches"] == 0
                    and checks["base_fixed"] and checks["files_ok"] and checks["reachable"] == 2 * len(landed)
                    and checks["unfinished"] == 0)
    return dict(final_head=final, **checks)


def timing(results, span_ms):
    """Per-merge times under main's lock, from the server stamps of each merge call."""
    validation, vc_merge, hold, lock_wait, held, starts, untimed = [], [], [], [], [], [], 0
    for r in results:
        for m in r["merges"]:
            s = m["server"]
            if not clock_ok(s, m["wall_ms"]):
                untimed += 1
                continue
            if "start" in s:
                starts.append(s["start"])
            if m["sqlstate"] == "00000":
                validation.append(ms(s["validated"] - s["locked"]))
                vc_merge.append(ms(s["merged"] - s["validated"]))
                hold.append(ms(s["merged"] - s["locked"]))
                lock_wait.append(ms(s["locked"] - s["start"]))
                held.append((s["locked"], s["committed"]))
            elif m["sqlstate"] == "RS001" and "locked" in s and "validated" in s:
                validation.append(ms(s["validated"] - s["locked"]))
                held.append((s["locked"], s["validated"]))
    fraction = None
    if held and starts:
        span = max(end for _, end in held) - min(starts)
        fraction = round(sum(end - begin for begin, end in held) / span, 4) if span > 0 else None
    return dict(validation_ms=validation, vc_merge_ms=vc_merge, hold_ms=hold, lock_wait_ms=lock_wait,
                lock_held_fraction=fraction, untimed_calls=untimed, merge_phase_ms=span_ms)


def run_once(args, owner, template, seed_value, workload, arm, name, log):
    owner.sql(f'CREATE DATABASE "{name}" TEMPLATE "{template["database"]}";', owner.settings["maintenance_database"])
    db = connect(args, name)
    run = f"s{seed_value}-{workload}-{arm}"
    specs = workload_writers(workload)
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        writers = list(pool.map(lambda spec: Writer(args, name, run, spec, arm, template["table_id"],
                                                     seed_value, workload, log), specs))
    try:
        propose_ms = run_phase(writers, lambda w: w.propose(template["base"]))
        merge_ms = run_phase(writers, lambda w: w.land())
    finally:
        for writer in writers:
            writer.session.close()
    results = [w.result() for w in writers]
    landed = [dict(writer=w.spec["writer"], merge_commit=w.merge_commit, used=w.proposals[-1]["used"])
              for w in writers if w.outcome == "merged"]
    stale = oracle.stale_commits(landed, oracle.collect_parent_rows(db, template["table_id"], landed))
    pairs = bundles(specs)
    stock = {}
    if pairs:
        ids = ",".join(str(row) for pair in pairs for row in pair)
        stock = {int(k): v for k, v in json.loads(db.sql(
            f"SELECT json_object_agg(id,stock) FROM app.products WHERE id IN ({ids});")).items()}
    violations = oracle.rule_violations(stock, pairs, BUNDLE_MINIMUM)
    record = dict(run=run, seed=seed_value, workload=workload, arm=arm, database=name, propose_phase_ms=propose_ms,
                  counts=dict(merged=sum(r["outcome"] == "merged" for r in results),
                              abstained=sum(r["outcome"] == "abstained" for r in results),
                              failed=sum(r["outcome"] == "failed" for r in results),
                              **{k: sum(r["counts"][k] for r in results) for k in results[0]["counts"]},
                              stale_commits=len(stale), rule_violations=len(violations)),
                  stale_commits=stale, rule_violations=violations, verification=verify(db, template, results),
                  timing=timing(results, merge_ms), writers=results)
    if not args.keep_databases:
        owner.sql(f'DROP DATABASE "{name}" WITH (FORCE);', owner.settings["maintenance_database"])
    return record


# --- Experiment -----------------------------------------------------------------

def prepare_template(args, owner, name):
    owner.create_database(name)
    template = connect(args, name)
    template.install(name)
    seed(template)
    template.sql(f"""BEGIN;
SELECT vc.begin_change('main',vc.head(),'seed','bundle stock for W2 and W3') AS draft \\gset
UPDATE app.products SET stock={BUNDLE_STOCK} WHERE id BETWEEN {BUNDLE_ROWS[0]} AND {BUNDLE_ROWS[-1]};
SELECT vc.finish_change() AS revision \\gset
COMMIT;
SELECT vc.materialize(vc.head(),vc.table_id('app.products'));""")
    template.sql((HERE / "readset.sql").read_text(encoding="utf-8"))
    history = json.loads(template.sql("""
      SELECT json_build_object('changesets',(SELECT count(*) FROM vc.changeset),
        'row_changes',(SELECT count(*) FROM vc.row_change),'paths',(SELECT count(DISTINCT path) FROM vc.path_change));"""))
    return dict(database=name, base=template.sql("SELECT vc.head();"),
                table_id=template.sql("SELECT vc.table_id('app.products');"), history=history)


def aggregate(runs):
    out = {}
    for workload in WORKLOADS:
        for arm in ARMS:
            mine = [r for r in runs if r["workload"] == workload and r["arm"] == arm]
            if not mine:
                continue
            entry = dict(runs=len(mine))
            for key in mine[0]["counts"]:
                entry[key] = dict(values=[r["counts"][key] for r in mine], **summary([r["counts"][key] for r in mine]))
            entry["merge_phase_ms"] = summary([r["timing"]["merge_phase_ms"] for r in mine])
            entry["validation_ms"] = summary([v for r in mine for v in r["timing"]["validation_ms"]])
            entry["vc_merge_ms"] = summary([v for r in mine for v in r["timing"]["vc_merge_ms"]])
            entry["lock_held_fraction"] = summary([r["timing"]["lock_held_fraction"] for r in mine])
            entry["verification_ok"] = all(r["verification"]["ok"] for r in mine)
            out[f"{workload}/{arm}"] = entry
    return out


def environment(owner):
    details = owner.check()
    settings = json.loads(owner.sql("""
      SELECT json_object_agg(name,setting) FROM pg_settings WHERE name IN ('shared_buffers','synchronous_commit',
        'fsync','max_connections','default_transaction_isolation');""", owner.settings["maintenance_database"]))
    return dict(postgresql=details, server_settings=settings, client_os=platform.platform(),
                client_python=sys.version.split()[0], cpu_count=os.cpu_count())


def sources():
    files = [HERE / "readset.sql", HERE / "run_readset.py", HERE / "oracle.py", ROOT / "docs" / "READSET_EXPERIMENT.md"]
    return {str(f.relative_to(ROOT).as_posix()): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(SEEDS))
    parser.add_argument("--workloads", nargs="+", choices=WORKLOADS, default=list(WORKLOADS))
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--output-dir", type=Path, help="Default results/readset/<run prefix>")
    parser.add_argument("--keep-databases", action="store_true", help="Keep every run database, not only the template")
    args = parser.parse_args()
    arms = tuple(a for a in ARMS if a in args.arms)
    workloads = tuple(w for w in WORKLOADS if w in args.workloads)
    complete = sorted(set(args.seeds)) == list(SEEDS) and arms == ARMS and workloads == WORKLOADS
    prefix = "agentdb_test_readset_" + uuid.uuid4().hex[:8]
    output = args.output_dir or ROOT / "results" / "readset" / prefix.removeprefix("agentdb_test_")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "calls.jsonl").exists():
        parser.error(f"{output} already holds calls.jsonl; choose a new --output-dir")
    log = CallLog(output / "calls.jsonl")
    owner = connect(args, prefix + "_tpl")
    started = datetime.now(timezone.utc)
    env = environment(owner)
    print("Preparing template " + prefix + "_tpl", flush=True)
    template = prepare_template(args, owner, prefix + "_tpl")
    runs = []
    for seed_value, workload, arm in schedule(args.seeds, workloads, arms):
        name = f"{prefix}_{seed_value}_{workload.lower()}_{SHORT[arm]}"
        record = run_once(args, owner, template, seed_value, workload, arm, name, log)
        runs.append(record)
        c = record["counts"]
        print(f"{record['run']}: merged {c['merged']}, abstained {c['abstained']}, failed {c['failed']}, "
              f"resubmits {c['resubmits']}, rethinks {c['rethinks']} (wasted {c['wasted_rethinks']}), "
              f"stale {c['stale_commits']}, violations {c['rule_violations']}, "
              f"phase {record['timing']['merge_phase_ms']} ms, checks {'ok' if record['verification']['ok'] else 'FAILED'}",
              flush=True)
    result = dict(generated_at=started.isoformat(), finished_at=datetime.now(timezone.utc).isoformat(),
                  design="docs/READSET_EXPERIMENT.md", complete_schedule=complete,
                  scope="Local synthetic load on the existing WSL PostgreSQL; 10 script writers per run, not LLM "
                        "agents; not a production capacity measurement. No cloud service used.",
                  parameters=dict(writers=WRITERS, bundle_rows=[BUNDLE_ROWS[0], BUNDLE_ROWS[-1]], bundle_stock=BUNDLE_STOCK,
                                  sale=SALE, bundle_minimum=BUNDLE_MINIMUM, browse_rows=BROWSE_ROWS,
                                  browse_range=[BROWSE_RANGE[0], BROWSE_RANGE[-1]], undercite=UNDERCITE,
                                  max_attempts=MAX_ATTEMPTS, max_lock_timeouts=MAX_LOCK_TIMEOUTS,
                                  merge_lock_timeout=MERGE_LOCK_TIMEOUT, clock_tolerance_ms=CLOCK_TOLERANCE_MS),
                  sources=sources(), environment=env, template=template,
                  schedule=[dict(seed=s, workload=w, arm=a) for s, w, a in schedule(args.seeds, workloads, arms)],
                  summary=aggregate(runs), criteria=oracle.criteria(runs), runs=runs, calls="calls.jsonl",
                  databases_retained=[template["database"]] + ([r["database"] for r in runs] if args.keep_databases else []))
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("Criteria: " + json.dumps(result["criteria"]), flush=True)
    print("Results: " + str(output / "results.json"), flush=True)
    if not all(r["verification"]["ok"] for r in runs):
        sys.exit(1)


if __name__ == "__main__":
    main()
