"""Real local PostgreSQL + the in-memory state store, with crash injection."""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import Connection, add_connection_arguments
from agent_state.engine import Engine
from agent_state.runtime import Runtime
from agent_state.storage import MemoryStore
from agent_state.maintenance import SnapshotMaintenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    args = parser.parse_args()
    connection = Connection(args)
    if not connection.settings["database"].startswith("agentdb_test_"):
        parser.error("Use an existing agentdb_test_ database; this test creates a branch there")
    details = connection.check()
    name = "state-recovery-" + uuid.uuid4().hex[:12]
    connection.sql(f"""
      BEGIN;
      SELECT vc.fork('{name}',vc.head());
      SELECT vc.begin_change('{name}',vc.head('{name}'),'integration','state resume test');
      UPDATE agent.products SET price=123 WHERE id=301;
      SELECT vc.finish_change();
      COMMIT;
    """)
    target = json.loads(connection.sql(f"""
      SELECT json_build_object('revision',vc.head('{name}'),'table_id',vc.table_id('app.products'));
    """))
    store = MemoryStore()
    runtime = Runtime(Engine(store))
    runner = SnapshotMaintenance(runtime, connection)
    assert not runner.inspect(target)["snapshot_exists"]

    def crash():
        raise TimeoutError("Injected interruption after PostgreSQL snapshot commit")
    try:
        runner.run(name, "admin-agent-a", **target, after_apply=crash)
    except TimeoutError:
        pass
    else:
        raise AssertionError("Crash hook did not run")
    assert runner.inspect(target)["snapshot_exists"]
    assert runtime.read(name, fresh=True)["state"]["status"] == "running"
    replacement = SnapshotMaintenance(Runtime(Engine(store)), connection)
    result = replacement.run(name, "admin-agent-b", **target)
    assert result["verified"] and not result["applied"]
    assert Engine(store).read(name)["state"]["status"] == "completed"
    state_price = connection.sql(f"""
      SELECT doc->>'price' FROM vc.rows_at('{target['revision']}','{target['table_id']}') WHERE pk='{{"id":301}}';
    """)
    assert float(state_price) == 123
    assert connection.sql("SELECT count(*) FROM vc.work_tx;") == "0"
    output = dict(postgresql=details, database=connection.settings["database"], branch=name,
                  target=target, resumed=result, fixed_revision_price=state_price,
                  state_backend="memory-test-only",
                  note="Existing local PostgreSQL; no instance initialized, started or stopped. Test branch retained.")
    Path("cloud-postgres-integration.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
