"""Check sql/006_history_health.sql on an installation made before it existed.

Existing local PostgreSQL only. Creates agentdb_test_hhmig_* with the schema of
--old-ref (001-005 as Connection.install applied them then), seeds it and adds
the history of scripts/check_history_migration.py (branches, merges both ways, a
criss-cross, table snapshots). It records data, history, every read and merge
outcome of that checker, and the attributes of the existing functions; applies
`db.py install-history-health` twice; and checks that nothing changed, that
vc.history_health is STABLE, executable by agentdb_manager only, equal to a
fresh install's (agentdb_test_hhnew_*), and that its numbers match independent
queries on every branch (commits_since_snapshot also against vc.snapshot_due).
Finally SnapshotMaintenance.check_history (MemoryStore) runs as agentdb_manager
twice, the second run publishing nothing, and the data is still unchanged.
Never writes to the project database and never starts or stops the server.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_state.engine import Engine
from agent_state.maintenance import SnapshotMaintenance
from agent_state.runtime import Runtime
from agent_state.storage import MemoryStore
from check_history_migration import FILE_CASES, HISTORY, compare, function_states, normalized, observe
from check_merge_migration import fingerprint
from db import add_connection_arguments
from review_local import connect, seed

OLD_FILES = ("001_core.sql", "002_demo_app.sql", "003_manager_role.sql", "004_merge_batch.sql",
             "005_bounded_history.sql")
EXISTING = ("vc._ancestors(uuid)", "vc.rows_at(uuid,uuid)", "vc._file_state(uuid,text)",
            "vc.merge_base(uuid,uuid)", "vc._merge_base(uuid[],uuid)", "vc.merge(text,uuid,uuid,text)",
            "vc.snapshot_due(text,integer)", "vc.snapshot_status(uuid,uuid)", "vc.materialize(uuid,uuid)",
            "vc.head(text)")
ADDED = "vc.history_health(text)"

# Everything a read-only check could touch, beyond check_merge_migration.fingerprint.
STATE = """
SELECT jsonb_build_object(
  'snapshot_manifest',(SELECT md5(coalesce(string_agg(to_jsonb(m)::text,',' ORDER BY changeset,table_id),''))
                       FROM vc.snapshot_manifest m),
  'snapshot_rows',(SELECT md5(coalesce(string_agg(to_jsonb(s)::text,',' ORDER BY changeset,table_id,pk::text),''))
                   FROM vc.snapshot_row s),
  'blobs',(SELECT md5(coalesce(string_agg(encode(hash,'hex'),',' ORDER BY hash),'')) FROM vc.blob),
  'tracked',(SELECT md5(string_agg(to_jsonb(t)::text,',' ORDER BY id)) FROM vc.tracked_table t),
  'open_work',(SELECT count(*) FROM vc.work_tx)+(SELECT count(*) FROM vc.pending_row)
              +(SELECT count(*) FROM vc.pending_path));
"""

# Independent answers: plain recursive joins, no vc functions besides vc.head.
EXPECTED = r"""
WITH RECURSIVE anc(branch,id) AS (
  SELECT name,head FROM vc.branch
  UNION SELECT anc.branch,p.parent FROM anc JOIN vc.parent p ON p.child=anc.id
), chain(branch,id,depth) AS (
  SELECT name,head,0 FROM vc.branch
  UNION ALL SELECT chain.branch,p.parent,chain.depth+1 FROM chain JOIN vc.parent p ON p.child=chain.id AND p.ordinal=1
)
SELECT jsonb_object_agg(b.name,jsonb_build_object(
  'reachable_changesets',(SELECT count(*) FROM anc WHERE anc.branch=b.name),
  'first_parent_depth',(SELECT max(depth) FROM chain WHERE chain.branch=b.name),
  'commits_since_snapshot',(SELECT jsonb_object_agg(t.relation::text,coalesce(
      (SELECT min(c.depth) FROM chain c JOIN vc.snapshot_manifest m ON m.changeset=c.id AND m.table_id=t.id
       WHERE c.branch=b.name),(SELECT max(depth) FROM chain WHERE chain.branch=b.name)))
    FROM vc.tracked_table t)))
FROM vc.branch b;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--old-ref", default="d5c68b0",
                        help="git commit whose sql/ predates 006_history_health.sql (default d5c68b0)")
    parser.add_argument("--output", type=Path, default=ROOT / "results" / "history-health-results.json")
    args = parser.parse_args()
    def old_file(name):
        return subprocess.run(["git", "show", f"{args.old_ref}:sql/{name}"], cwd=ROOT, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    newer = subprocess.run(["git", "cat-file", "-e", f"{args.old_ref}:sql/006_history_health.sql"],
                           cwd=ROOT, capture_output=True)
    assert newer.returncode != 0, "--old-ref already contains 006_history_health.sql"
    old_schema = "\n".join(re.sub(r"(?m)^BEGIN;\s*$|^COMMIT;\s*$", "", old_file(name)) for name in OLD_FILES)

    suffix = uuid.uuid4().hex[:10]
    names = dict(history="agentdb_test_hhmig_" + suffix, fresh="agentdb_test_hhnew_" + suffix)
    owner = connect(args, names["history"])
    details = owner.check()
    owner.create_database(names["history"])
    owner.sql("BEGIN;\n" + old_schema + "\nCOMMIT;")  # as Connection.install did before 006 existed
    seed(owner)
    owner.sql(HISTORY)
    print("Old-schema test database: " + names["history"], flush=True)

    def data():
        return dict(fingerprint(owner), **json.loads(owner.sql(STATE)))
    data_before = data()
    observed_before = observe(owner)
    files_before = json.loads(owner.sql(FILE_CASES))
    functions_before = function_states(owner, EXISTING)

    for _ in range(2):  # rerunnable
        owner.install_history_health()
    assert data() == data_before
    functions_after = function_states(owner, EXISTING + (ADDED,))
    assert {k: functions_after[k] for k in EXISTING} == functions_before, (functions_before, functions_after)
    added = functions_after[ADDED]
    assert added["volatility"] == "s" and added["security_definer"], added
    assert added["manager_can_execute"] and not added["client_can_execute"] and not added["public_can_execute"], added
    print("PASS migration keeps data, history and every existing function; vc.history_health is STABLE and "
          "granted to agentdb_manager only", flush=True)

    owner.create_database(names["fresh"])
    owner.install(names["fresh"])
    fresh = function_states(connect(args, names["fresh"]), EXISTING + (ADDED,))
    assert fresh == functions_after, (fresh, functions_after)
    print("PASS migrated functions equal a fresh install's", flush=True)

    manager = connect(args, names["history"], "agentdb_manager")
    expected = json.loads(owner.sql(EXPECTED))
    measured = {}
    for branch in sorted(expected):
        value = json.loads(manager.sql(f"SELECT vc.history_health('{branch}');"))
        measured[branch] = value
        assert value["branch"] == branch and value["head"] == owner.sql(f"SELECT vc.head('{branch}');"), value
        for key, want in expected[branch].items():
            assert value[key] == want, (branch, key, value[key], want)
        assert value["max_commits_since_snapshot"] == max(value["commits_since_snapshot"].values())
        assert value["changesets_total"] == int(owner.sql("SELECT count(*) FROM vc.changeset;"))
        assert isinstance(value["ancestor_walk_ms"], (int, float)) and value["ancestor_walk_ms"] >= 0
        for relation, since in value["commits_since_snapshot"].items():
            for interval in (since, since + 1):
                if interval < 1:
                    continue
                due = owner.sql(f"SELECT count(*) FROM vc.snapshot_due('{branch}',{interval}) d "
                                f"WHERE d.table_id=vc.table_id('{relation}');")
                assert due == ("1" if since >= interval else "0"), (branch, relation, since, interval, due)
    unknown = owner.execute("SELECT vc.history_health('no-such-branch');", names["history"])
    assert unknown.returncode != 0 and "Unknown branch: no-such-branch" in unknown.stderr, unknown.stderr
    client = connect(args, names["history"], "agentdb_client").execute("SELECT vc.history_health();",
                                                                       names["history"])
    assert client.returncode != 0 and "permission denied" in client.stderr, client.stderr
    assert data() == data_before
    print(f"PASS vc.history_health matches independent queries and vc.snapshot_due on {len(measured)} branches; "
          "unknown branch and agentdb_client are rejected", flush=True)

    observations = compare("reads", observed_before, observe(owner))
    files_after = json.loads(owner.sql(FILE_CASES))
    assert normalized(files_after) == normalized(files_before)
    store = MemoryStore()
    runner = SnapshotMaintenance(Runtime(Engine(store)), manager)
    first = runner.check_history("check-history-health")
    again = runner.check_history("check-history-health")
    assert first["published"] and not first["crossed"] and not again["published"], (first, again)
    state = Engine(store).read(first["task_id"])["state"]
    assert state["status"] == "completed" and state["facts"]["reachable_changesets"] == \
        expected["main"]["reachable_changesets"], state
    low = runner.check_history("check-history-health", task_id="history-health-low",
                               policy_ref=Engine(store).objects.put_bytes(json.dumps(dict(history_health=dict(
                                   max_reachable_changesets=10))).encode()))
    assert low["crossed"] and Engine(store).read("history-health-low")["state"]["status"] == "running", low
    assert data() == data_before
    print(f"PASS {observations} recorded reads and {len(files_before)} merge outcomes unchanged; "
          "check_history publishes once and never writes PostgreSQL", flush=True)

    result = dict(databases=names, postgresql=details["server_version"], old_ref=args.old_ref,
                  migration="sql/006_history_health.sql via Connection.install_history_health "
                            "(db.py install-history-health)",
                  migration_runs=2, data_and_history_unchanged=True, existing_functions_unchanged=True,
                  functions_equal_fresh_install=True, function=added, history_health=measured,
                  recorded_reads=observations, file_merge_cases=len(files_before), merge_outcomes_identical=True,
                  maintenance=dict(first=first, rerun_published=again["published"], low_threshold=low["crossed"]),
                  real_cloud_verified=False,
                  note=("Local dedicated test databases on the existing PostgreSQL server; the project database "
                        "was not touched. check_history ran with MemoryStore (test-only state backend) and the "
                        "agentdb_manager role."))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("function", "history_health")}, indent=2))


if __name__ == "__main__":
    main()
