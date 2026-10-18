"""S4 history growth and S5 failure drills against the staging PostgreSQL VM.

Plan and results: docs/STAGING_S4_S5.md. Every run creates its own
agentdb_test_* database on the server named by --config (normally
connection.staging.local.json) and writes staging-results/<command>.json.

  growth   S4: deepen main's history and measure reads, merges and commits
  backup   S5: pg_dump / pg_restore a populated database and compare it
  restart  S5: graceful VM shutdown and start; committed state must be unchanged
  crash    S5: hard power-off of the VM during concurrent commits
  netcut   S5: unplug the VM's host-only link while a session holds main's lock;
           --expect-gone-within asserts how soon the server releases it
  kill     S5: terminate a session in the middle of a versioned transaction

restart, crash and netcut drive the VirtualBox VM (--vm) through VBoxManage on
this host; they need no shell or password on the VM. Never point this at a
database that matters: the drills power the server off.
"""
import argparse
import copy
import json
from pathlib import Path
import statistics
import subprocess
import threading
import time
import uuid

from db import FLAGS, Connection, add_connection_arguments
from review_local import seed

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "staging-results"
VBOX = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"


def connect(args, database=None, role=None):
    options = copy.copy(args)
    options.database, options.role = database or args.database, role
    return Connection(options)


def new_database(args, owner, kind):
    name = f"agentdb_test_{kind}_" + uuid.uuid4().hex[:10]
    owner.create_database(name)
    connection = connect(args, name)
    connection.install(name)
    seed(connection)
    return name, connection


def median(values):
    return round(statistics.median(values), 3) if values else None


def explain_ms(session, statement, repeats=5):
    """Server execution time of one statement, median of repeats (EXPLAIN ANALYZE)."""
    times = []
    for _ in range(repeats):
        plan = json.loads(session.sql("EXPLAIN (ANALYZE, FORMAT JSON) " + statement))
        times.append(plan[0]["Execution Time"])
    return median(times)


def json_values(text):
    """Every JSON value in text, in order (psql prints one EXPLAIN plan after another)."""
    decoder, values, position = json.JSONDecoder(), [], 0
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            return values
        value, position = decoder.raw_decode(text, position)
        values.append(value)


def write(name, result, quiet=False):
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if not quiet:
        print(json.dumps(result, indent=2))
        print("Results: " + str(path))


# --- VirtualBox -------------------------------------------------------------

def vbox(*argv, timeout=120):
    result = subprocess.run([VBOX, *argv], capture_output=True, timeout=timeout, creationflags=FLAGS)
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    if result.returncode:
        raise RuntimeError("VBoxManage " + " ".join(argv) + " failed:\n" + output)
    return output


def vm_state(vm):
    for line in vbox("showvminfo", vm, "--machinereadable", timeout=60).splitlines():
        if line.startswith("VMState="):
            return line.split("=", 1)[1].strip('"')
    return "unknown"


def wait_vm_state(vm, wanted, limit=180):
    began = time.monotonic()
    while time.monotonic() - began < limit:
        if vm_state(vm) in wanted:
            return round(time.monotonic() - began, 1)
        time.sleep(2)
    raise RuntimeError(f"{vm} did not reach {wanted} within {limit} s")


def server_ready(owner):
    try:
        return owner.execute("SELECT 1;", owner.settings["maintenance_database"], timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def start_and_wait(owner, vm, limit=600, reset_after=200):
    """Start the VM headless and wait for PostgreSQL; reset once if boot hangs.

    VirtualBox on the Hyper-V platform occasionally hangs early in the guest's
    boot (seen on this laptop on 2026-10-18); one reset has always recovered it.
    """
    vbox("startvm", vm, "--type", "headless")
    began, reset = time.monotonic(), False
    while time.monotonic() - began < limit:
        if server_ready(owner):
            return dict(ready_after_s=round(time.monotonic() - began, 1), boot_hang_reset=reset)
        if not reset and time.monotonic() - began > reset_after:
            vbox("controlvm", vm, "reset")
            reset = True
        time.sleep(5)
    raise RuntimeError(f"PostgreSQL on {vm} not ready within {limit} s")


# --- Shared checks ----------------------------------------------------------

FINGERPRINT = """
  SELECT json_build_object(
    'head', vc.head(),
    'changesets', (SELECT count(*) FROM vc.changeset),
    'parents', (SELECT count(*) FROM vc.parent),
    'row_changes', (SELECT count(*) FROM vc.row_change),
    'path_changes', (SELECT count(*) FROM vc.path_change),
    'blobs', (SELECT count(*) FROM vc.blob),
    'snapshots', (SELECT count(*) FROM vc.snapshot_manifest),
    'unfinished_work_tx', (SELECT count(*) FROM vc.work_tx),
    'branches_md5', (SELECT md5(string_agg(to_jsonb(b)::text, ',' ORDER BY to_jsonb(b)::text)) FROM vc.branch b),
    'row_change_md5', (SELECT md5(string_agg(r.changeset::text||r.table_id::text||r.pk::text||coalesce(r.after_row::text,''),
                         ',' ORDER BY r.changeset, r.table_id, r.pk::text)) FROM vc.row_change r),
    'products_md5', (SELECT md5(string_agg(to_jsonb(p)::text, ',' ORDER BY p.id)) FROM app.products p),
    'vc_functions', (SELECT count(*) FROM pg_proc WHERE pronamespace='vc'::regnamespace),
    'client_may_merge', has_function_privilege('agentdb_client','vc.merge(text,uuid,uuid,text)','EXECUTE'));
"""

# Rows of the native main table that differ from main's versioned state.
PROJECTION = """
  SELECT count(*) FROM (SELECT r.pk, r.doc FROM vc.rows_at(vc.head(), vc.table_id('app.products')) r) v
  FULL JOIN (SELECT jsonb_build_object('id', p.id) AS pk, to_jsonb(p) AS doc FROM app.products p) a USING (pk)
  WHERE v.doc IS DISTINCT FROM a.doc;
"""


def fingerprint(connection):
    return json.loads(connection.sql(FINGERPRINT))


def commit_main(connection, actor, note, row=1):
    return connection.sql(f"""
      BEGIN;
      SELECT vc.begin_change('main', vc.head(), '{actor}', '{note}') AS draft \\gset
      UPDATE app.products SET price = price + 1 WHERE id = {row};
      SELECT vc.finish_change() AS revision \\gset
      COMMIT;
      SELECT :'revision';
    """).splitlines()[-1]


def postmaster_start(owner):
    return owner.sql("SELECT pg_postmaster_start_time();", owner.settings["maintenance_database"])


# --- S4: history growth -----------------------------------------------------

def grow(session, start, count, batch=10, snapshot_interval=0, policy=None):
    """Append count commits to main; each changes 10 rows and one of 50 files.

    With snapshot_interval, the snapshot policy runs after every batch: the
    statement a maintenance executor runs for each target vc.snapshot_due names
    (agent_state.maintenance.SnapshotMaintenance.run_due). Its time is appended
    to policy, not to the commit times."""
    walls = []
    for first in range(start, start + count, batch):
        script = []
        for n in range(first, min(first + batch, start + count)):
            low = (n * 10) % 4000 + 1
            script.append(f"""BEGIN;
SELECT vc.begin_change('main', vc.head(), 'growth', 'growth {n}') AS draft \\gset
UPDATE app.products SET price = price + 1 WHERE id BETWEEN {low} AND {low + 9};
SELECT vc.write_file('growth/file_{n % 50}.txt', convert_to('{n}', 'UTF8')) AS blob \\gset
SELECT vc.finish_change() AS revision \\gset
COMMIT;""")
        began = time.perf_counter()
        session.sql("\n".join(script))
        walls.append((time.perf_counter() - began) * 1000 / len(script))
        if snapshot_interval:
            began = time.perf_counter()
            made = int(session.sql("SELECT count(vc.materialize(revision, table_id)) "
                                   f"FROM vc.snapshot_due('main', {int(snapshot_interval)});"))
            if made:
                policy.append(round((time.perf_counter() - began) * 1000, 3))
    return walls


def measure_level(session, base, table, level, merges=3):
    head = session.sql("SELECT vc.head();")
    rows_at = f"vc.rows_at('{head}', '{table}')"
    result = dict(
        commits_added=level, head=head,
        read_full_head_ms=explain_ms(session, f"SELECT count(*) FROM {rows_at}"),
        read_point_head_ms=explain_ms(session, f"SELECT doc FROM {rows_at} WHERE pk = '{{\"id\": 1}}'::jsonb"),
        read_full_seed_ms=explain_ms(session, f"SELECT count(*) FROM vc.rows_at('{base}', '{table}')"),
        history_page_ms=explain_ms(session, f"SELECT * FROM vc.history('{head}', 20)"),
        database_mb=round(int(session.sql("SELECT pg_database_size(current_database());")) / 1048576, 1))
    # Snapshot effect, rolled back so later levels keep the same history shape.
    plans = session.execute(f"""BEGIN;
EXPLAIN (ANALYZE, FORMAT JSON) SELECT vc.materialize('{head}', '{table}');
EXPLAIN (ANALYZE, FORMAT JSON) SELECT count(*) FROM {rows_at};
ROLLBACK;""")
    if plans.returncode:
        raise RuntimeError(plans.stderr)
    timings = [plan[0]["Execution Time"] for plan in json_values(plans.stdout)]
    result.update(materialize_ms=round(timings[0], 3), read_full_after_snapshot_ms=round(timings[1], 3))
    merge_ms = []
    for i in range(merges):
        target = session.sql("SELECT vc.head();")
        branch = f"growth_{level}_{i}"
        low = (i * 10) % 4000 + 1
        revision = session.sql(f"""
          SELECT vc.fork('{branch}', '{target}') AS forked \\gset
          BEGIN;
          SELECT vc.begin_change('{branch}', '{target}', 'growth', 'branch change') AS draft \\gset
          UPDATE agent.products SET stock = stock - 1 WHERE id BETWEEN {low} AND {low + 9};
          SELECT vc.finish_change() AS revision \\gset
          COMMIT;
          SELECT :'revision';
        """).splitlines()[-1]
        began = time.perf_counter()
        session.sql(f"BEGIN; SELECT vc.merge('main', '{revision}', '{target}', 'growth') AS merged \\gset\nCOMMIT;")
        merge_ms.append((time.perf_counter() - began) * 1000)
    result["merge_ms"] = median(merge_ms)
    return result


def run_growth(args, owner):
    name, connection = new_database(args, owner, "growth")
    table = connection.sql("SELECT vc.table_id('app.products');")
    base = connection.sql("SELECT vc.head();")  # seed commit, materialized by seed()
    levels, done = [], 0
    result = dict(
        database=name, server=owner.check(), table_rows=4000,
        commit_shape="each growth commit: 10 rows of app.products and 1 of 50 files on main",
        levels=levels, complete=False, snapshot_interval=args.snapshot_interval or None,
        note=("Reads are EXPLAIN ANALYZE server execution times (median of 5). merge_ms and commit times are client "
              "wall times over one long-lived session (TLS to the VM). The seed commit is materialized; reads at head "
              "replay every commit since it. Snapshot rows are measured inside a rolled-back transaction. Client and "
              "server share one laptop: reference values only."))
    if args.snapshot_interval:
        result["note"] += (" snapshot_interval: after every 10 growth commits the snapshot policy materializes "
                           "what vc.snapshot_due names, so reads at head replay only the commits since the "
                           "nearest policy snapshot; policy time is reported separately, not in commit times.")
    with connection.session() as session:
        for target in args.levels:
            policy = []
            walls = (grow(session, done, target - done, snapshot_interval=args.snapshot_interval, policy=policy)
                     if target > done else [])
            done = target
            level = measure_level(session, base, table, target)
            level["growth_commit_ms_per_commit"] = median(walls[-10:]) if walls else None
            if args.snapshot_interval:
                level["policy_snapshot_batches"], level["policy_snapshot_ms_median"] = len(policy), median(policy)
            levels.append(level)
            write("growth", result, quiet=True)  # keep finished levels if a later one fails
            print(f"level {target}: read head {level['read_full_head_ms']} ms, after snapshot "
                  f"{level['read_full_after_snapshot_ms']} ms, merge {level['merge_ms']} ms, commit "
                  f"{level['growth_commit_ms_per_commit']} ms", flush=True)
        result.update(final=fingerprint(session), complete=True)
    write("growth", result)


# --- S5: backup and restore -------------------------------------------------

def run_backup(args, owner):
    if owner.settings["wsl_distribution"]:
        raise SystemExit("backup expects the native client (pg_dump.exe next to psql.exe)")
    source = args.source
    if not source:
        source, _ = new_database(args, owner, "backupsrc")
    bin_dir = Path(owner.settings["psql"]).parent
    dump = RESULTS / f"{source}.dump"
    RESULTS.mkdir(exist_ok=True)
    common = ["-h", owner.settings["host"], "-p", str(owner.settings["port"]), "-U", owner.settings["user"],
              "--no-password"]
    before = fingerprint(connect(args, source))
    began = time.perf_counter()
    subprocess.run([str(bin_dir / "pg_dump.exe"), *common, "-d", source, "-Fc", "-f", str(dump)],
                   check=True, env=owner.env, creationflags=FLAGS, timeout=600)
    dump_s = time.perf_counter() - began
    target = "agentdb_test_restore_" + uuid.uuid4().hex[:10]
    owner.create_database(target)
    began = time.perf_counter()
    restore = subprocess.run([str(bin_dir / "pg_restore.exe"), *common, "-d", target, "--exit-on-error", str(dump)],
                             capture_output=True, env=owner.env, creationflags=FLAGS, timeout=600)
    restore_s = time.perf_counter() - began
    if restore.returncode:
        raise RuntimeError(restore.stderr.decode("utf-8", "replace"))
    restored = connect(args, target)
    after = fingerprint(restored)
    differences = sorted(k for k in before if before[k] != after[k])
    projection = int(restored.sql(PROJECTION))
    revision = commit_main(restored, "backup-drill", "commit after restore")
    client = connect(args, target, "agentdb_client")
    client_head = client.sql("SELECT vc.head();")
    size = dump.stat().st_size
    dump.unlink()
    write("backup", dict(
        source=source, restored=target, dump_format="custom (-Fc)", dump_bytes=size,
        dump_s=round(dump_s, 2), restore_s=round(restore_s, 2), before=before, after_restore=after,
        fingerprint_differences=differences, restored_projection_mismatches=projection,
        commit_after_restore=revision, client_role_reads_head=client_head == revision,
        passed=not differences and projection == 0 and client_head == revision,
        note="Logical backup as the non-superuser owner over TLS; roles are cluster objects and already existed. "
             "The dump file was deleted after the check."))


# --- S5: graceful restart ---------------------------------------------------

def run_restart(args, owner):
    name, connection = new_database(args, owner, "restart")
    commit_main(connection, "restart-drill", "before restart")
    before, started_before = fingerprint(connection), postmaster_start(owner)
    vbox("controlvm", args.vm, "acpipowerbutton")
    shutdown_s = wait_vm_state(args.vm, {"poweroff"})
    boot = start_and_wait(owner, args.vm)
    after = fingerprint(connection)
    revision = commit_main(connection, "restart-drill", "after restart")
    differences = sorted(k for k in before if before[k] != after[k])
    write("restart", dict(
        database=name, shutdown_s=shutdown_s, **boot, postmaster_start_before=started_before,
        postmaster_start_after=postmaster_start(owner), before=before, after=after,
        fingerprint_differences=differences, projection_mismatches=int(connection.sql(PROJECTION)),
        commit_after_restart=revision, passed=not differences))


# --- S5: power cut during commits -------------------------------------------

class Writer(threading.Thread):
    """Commits in a loop; records each revision only after COMMIT returned."""

    def __init__(self, args, database, branch, index):
        super().__init__(daemon=True)
        self.connection, self.branch, self.index = connect(args, database), branch, index
        self.acked, self.error, self.stop = [], None, threading.Event()

    def run(self):
        try:
            with self.connection.session() as session:
                expected = session.sql(f"SELECT vc.head('{self.branch}');")
                view = "app" if self.branch == "main" else "agent"
                n = 0
                while not self.stop.is_set():
                    n += 1
                    row = self.index * 100 + n % 100 + 1
                    result = session.execute(f"""
                      BEGIN;
                      SELECT vc.begin_change('{self.branch}', '{expected}', 'crash-w{self.index}', 'commit {n}') AS draft \\gset
                      UPDATE {view}.products SET stock = stock - 1 WHERE id = {row};
                      SELECT vc.write_file('crash/w{self.index}.txt', convert_to('{n}', 'UTF8')) AS blob \\gset
                      SELECT vc.finish_change() AS revision \\gset
                      COMMIT;
                      SELECT :'revision';
                    """, timeout=20)
                    if result.returncode:
                        raise RuntimeError(result.stderr.strip())
                    expected = result.stdout.strip().splitlines()[-1]
                    self.acked.append(expected)
        except Exception as error:  # the power cut ends every writer this way
            self.error = f"{type(error).__name__}: {str(error)[:200]}"


def run_crash(args, owner):
    name, connection = new_database(args, owner, "crash")
    base = connection.sql("SELECT vc.head();")
    branches = ["main"] + [f"crash_w{i}" for i in range(1, args.writers)]
    for branch in branches[1:]:
        connection.sql(f"SELECT vc.fork('{branch}', '{base}');")
    pre_projection = int(connection.sql(PROJECTION))
    started_before = postmaster_start(owner)
    writers = [Writer(args, name, branch, i) for i, branch in enumerate(branches)]
    for writer in writers:
        writer.start()
    time.sleep(args.seconds)
    cut_at = time.perf_counter()
    vbox("controlvm", args.vm, "poweroff")
    for writer in writers:
        writer.stop.set()
    for writer in writers:
        writer.join(60)
    boot = start_and_wait(owner, args.vm)
    recovered_s = round(time.perf_counter() - cut_at, 1)
    checks = []
    for writer, branch in zip(writers, branches):
        head = connection.sql(f"SELECT vc.head('{branch}');")
        last = writer.acked[-1] if writer.acked else base
        parent = connection.sql(f"SELECT coalesce((SELECT parent::text FROM vc.parent WHERE child='{head}' AND ordinal=1),'');")
        ids = ",".join(f"'{r}'" for r in writer.acked) or "NULL"
        present = int(connection.sql(f"SELECT count(*) FROM vc.changeset WHERE id IN ({ids});")) if writer.acked else 0
        reachable = int(connection.sql(f"SELECT count(*) FROM vc._ancestors('{head}') WHERE id IN ({ids});")) if writer.acked else 0
        state = "last_acked" if head == last else "one_unacked_commit_landed" if parent == last else "unexpected"
        checks.append(dict(branch=branch, acked=len(writer.acked), acked_present=present, acked_reachable=reachable,
                           head_state=state, writer_error=writer.error))
    after = fingerprint(connection)
    post_projection = int(connection.sql(PROJECTION))
    revision = commit_main(connection, "crash-drill", "after recovery")
    passed = (all(c["acked_present"] == c["acked"] == c["acked_reachable"] and c["head_state"] != "unexpected"
                  for c in checks) and after["unfinished_work_tx"] == 0 and pre_projection == post_projection == 0)
    write("crash", dict(
        database=name, writers=len(writers), seconds_before_cut=args.seconds, **boot,
        recovered_after_cut_s=recovered_s, postmaster_start_before=started_before,
        postmaster_start_after=postmaster_start(owner), per_branch=checks,
        projection_mismatches_before=pre_projection, projection_mismatches_after=post_projection,
        after=after, commit_after_recovery=revision, passed=passed,
        note=("VBoxManage controlvm poweroff while every writer had commits in flight. Acked means COMMIT returned to "
              "the client. A commit in flight at the cut may land without an ack (head is then one past the last ack); "
              "it must never be partial.")))


# --- S5: network cut while main's lock is held ------------------------------

def hold_main_lock(owner, database):
    process = subprocess.Popen(owner.client_args(database, stop_on_error=False), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=owner.env, creationflags=FLAGS)
    process.stdin.write(b"BEGIN;\nSELECT vc.begin_change('main', vc.head(), 'netcut-A', 'hold') AS d \\gset\n"
                        b"SELECT 'LOCKED|'||pg_backend_pid();\n")
    process.stdin.flush()
    line = process.stdout.readline().decode().strip()
    if not line.startswith("LOCKED|"):
        process.kill()
        raise RuntimeError("could not take main's lock: " + line + process.stderr.read().decode())
    return process, int(line.split("|")[1])


def try_main_lock(connection):
    """Can another session take main's lock within 3 s?"""
    result = connection.execute("SET lock_timeout = '3s';\nBEGIN;\n"
                                "SELECT vc.begin_change('main', vc.head(), 'netcut-B', 'probe') AS d \\gset\nROLLBACK;",
                                timeout=30)
    return "acquired" if result.returncode == 0 else ("lock_timeout" if "55P03" in result.stderr else result.stderr.strip()[-160:])


# Server settings that bound how long a vanished client's session survives
# (deploy/staging/configure_postgres.sh). Read over TCP, so the keepalive values
# are the ones in effect for the drill's own connections.
VANISHED_CLIENT_SETTINGS = """
  SELECT json_object_agg(name, setting || coalesce(unit, '') ORDER BY name) FROM pg_settings
  WHERE name IN ('idle_in_transaction_session_timeout', 'tcp_keepalives_idle', 'tcp_keepalives_interval',
                 'tcp_keepalives_count', 'tcp_user_timeout', 'client_connection_check_interval', 'lock_timeout');
"""


def run_netcut(args, owner):
    name, connection = new_database(args, owner, "netcut")
    head_before = connection.sql("SELECT vc.head();")
    settings = json.loads(connection.sql(VANISHED_CLIENT_SETTINGS))
    holder, pid = hold_main_lock(owner, name)
    locked = time.perf_counter()  # the holder's last statement has just returned
    contended = try_main_lock(connection)
    vbox("controlvm", args.vm, "setlinkstate2", "off")
    cut = time.perf_counter()
    began = time.perf_counter()
    during = owner.execute("SELECT 1;", owner.settings["maintenance_database"], timeout=30)
    connect_during_cut = dict(returncode=during.returncode, seconds=round(time.perf_counter() - began, 1),
                              error=during.stderr.strip().splitlines()[-1] if during.stderr.strip() else "")
    holder.kill()  # the client host loses the session while the link is down
    holder.wait(10)
    time.sleep(max(0, args.seconds - (time.perf_counter() - cut)))
    vbox("controlvm", args.vm, "setlinkstate2", "on")
    restored = time.perf_counter()
    while not server_ready(owner):
        time.sleep(1)
    first_probe = try_main_lock(connection)
    expect = args.expect_gone_within
    deadline = max(restored + args.wait, locked + expect + 15 if expect else 0)
    stale_s, gone, polls, state = None, None, 0, None
    while time.perf_counter() < deadline:
        state = connection.sql(f"SELECT coalesce((SELECT state FROM pg_stat_activity WHERE pid={pid}),'gone');")
        polls += 1
        if state == "gone":
            gone = time.perf_counter()
            stale_s = round(gone - restored, 1)
            break
        time.sleep(1)
    terminated = None
    if stale_s is None:
        terminated = connection.sql(f"SELECT pg_terminate_backend({pid});") == "t"
        time.sleep(1)
    final_probe = try_main_lock(connection)
    after = fingerprint(connection)
    locked_for = round(gone - locked, 1) if gone else None
    bound_met = None if expect is None else locked_for is not None and locked_for <= expect
    write("netcut", dict(
        database=name, server_settings=settings, link_down_s=args.seconds, holder_pid=pid,
        probe_while_lock_held=contended, connect_during_cut=connect_during_cut,
        probe_right_after_link_restored=first_probe, stale_session_gone_after_s=stale_s,
        main_locked_for_s=locked_for, gone_at_first_poll=polls == 1 and gone is not None,
        stale_state_at_deadline=None if stale_s else state, waited_s=round(deadline - restored, 1),
        expect_gone_within_s=expect, bound_met=bound_met,
        terminated_by_owner=terminated, probe_after_cleanup=final_probe, head_unchanged=after["head"] == head_before,
        unfinished_work_tx=after["unfinished_work_tx"],
        passed=(final_probe == "acquired" and after["head"] == head_before and after["unfinished_work_tx"] == 0
                and (expect is None or (bound_met and terminated is None))),
        note=("The holder kept an open versioned transaction (main's branch row locked) when the host-only link was "
              "unplugged and its client process died. The server ends such a session only through "
              "idle_in_transaction_session_timeout or when TCP declares the peer dead (server_settings: the values "
              "in effect for this TCP connection); with PostgreSQL's defaults that takes about 2 h 11 min. "
              "stale_session_gone_after_s counts from the link coming back; main_locked_for_s from the holder's "
              "last statement to the first 1 s poll that no longer finds the session, so it overstates the "
              "server's time by up to one poll. With --expect-gone-within the drill passes only if the server "
              "released main within that many seconds without the owner's pg_terminate_backend.")))


# --- S5: terminated session mid-transaction ---------------------------------

def run_kill(args, owner):
    name, connection = new_database(args, owner, "kill")
    before = fingerprint(connection)
    victim = subprocess.Popen(owner.client_args(name, stop_on_error=False), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=owner.env, creationflags=FLAGS)
    victim.stdin.write(b"BEGIN;\nSELECT vc.begin_change('main', vc.head(), 'kill-A', 'doomed') AS d \\gset\n"
                       b"UPDATE app.products SET price = 999 WHERE id <= 10;\n"
                       b"SELECT vc.write_file('kill/doomed.txt', convert_to('x','UTF8')) AS b \\gset\n"
                       b"SELECT 'STAGED|'||pg_backend_pid();\n")
    victim.stdin.flush()
    line = victim.stdout.readline().decode().strip()
    pid = int(line.split("|")[1])
    # Uncommitted work is invisible to other sessions; record the victim's state instead.
    victim_state = connection.sql(f"SELECT state FROM pg_stat_activity WHERE pid = {pid};")
    terminated = connection.sql(f"SELECT pg_terminate_backend({pid});") == "t"
    try:  # the victim tries to finish; its connection is already gone
        victim.stdin.write(b"SELECT vc.finish_change();\nCOMMIT;\n")
        victim.stdin.close()
    except OSError:
        pass
    _, victim_err = victim.communicate(timeout=30)
    after = fingerprint(connection)
    prices_unchanged = connection.sql("SELECT bool_and(price = 10) FROM app.products WHERE id <= 10;") == "t"
    revision = commit_main(connection, "kill-drill", "after termination")
    differences = sorted(k for k in before if before[k] != after[k])
    write("kill", dict(
        database=name, victim_pid=pid, victim_state_before_termination=victim_state, terminated=terminated,
        victim_error=victim_err.decode("utf-8", "replace").strip().splitlines()[-1:],
        fingerprint_differences=differences, prices_rows_1_to_10_unchanged=prices_unchanged, commit_after=revision,
        passed=terminated and not differences and prices_unchanged,
        note="A session staged a versioned change on main (rows and a file) and was terminated before finish_change; "
             "nothing of it may remain."))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("command", choices=("growth", "backup", "restart", "crash", "netcut", "kill"))
    parser.add_argument("--vm", default="agent-db", help="VirtualBox VM that runs the staging server")
    parser.add_argument("--levels", type=int, nargs="+", default=[0, 100, 1000, 3000],
                        help="growth: total commits added to main at each measurement")
    parser.add_argument("--snapshot-interval", type=int, default=0,
                        help="growth: apply the snapshot policy (vc.snapshot_due) with this interval after "
                             "every 10 commits; 0 (default) materializes nothing, as before")
    parser.add_argument("--source", help="backup: database to dump (default: a new seeded one)")
    parser.add_argument("--writers", type=int, default=5, help="crash: main plus N-1 branch writers")
    parser.add_argument("--seconds", type=float, default=10, help="crash: commit time before the cut; netcut: link-down time")
    parser.add_argument("--wait", type=float, default=120,
                        help="netcut: how long to watch for the stale session after the link is back")
    parser.add_argument("--expect-gone-within", type=float, metavar="SECONDS",
                        help="netcut: pass only if the server ends the stale session (releasing main) within "
                             "SECONDS of its last statement, with no manual termination; 75 for the staging "
                             "settings (docs/STAGING_S4_S5.md section 4)")
    args = parser.parse_args()
    if args.expect_gone_within is not None and args.seconds >= args.expect_gone_within:
        parser.error("--expect-gone-within must exceed --seconds: the bound cannot be observed while the link is down")
    owner = connect(args)
    owner.check()
    globals()["run_" + args.command](args, owner)


if __name__ == "__main__":
    main()
