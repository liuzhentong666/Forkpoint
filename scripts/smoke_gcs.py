"""Real-bucket smoke test. Writes only under a fresh run prefix; retains its objects."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_state.engine import Engine
from agent_state.runtime import Runtime
from agent_state.storage import GCSStore, Conflict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--project")
    parser.add_argument("--prefix", default="agentdb-smoke")
    args = parser.parse_args()
    prefix = args.prefix + "/" + uuid.uuid4().hex
    store = GCSStore(args.bucket, args.project)
    engine = Engine(store, prefix)
    state = dict(goal="Verify real GCS state publication", status="running", facts={"number": 0})
    engine.publish("task", "create", "smoke", state)
    barrier = threading.Barrier(10)
    def update(number):
        barrier.wait(timeout=30)
        proposal = dict(state, facts={"number": number})
        try:
            return Engine(store, prefix).publish("task", f"write-{number}", "smoke", proposal, 0)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(update, range(10)))
    assert sum(item is not None for item in results) == 1
    restored = Runtime(Engine(store, prefix))
    record = restored.read("task")
    assert record["version"] == 1
    number = record["state"]["facts"]["number"]
    repeated = Engine(store, prefix).publish("task", f"write-{number}", "smoke",
                                             dict(state, facts={"number": number}), 0)
    assert repeated["deduplicated"]
    print(json.dumps(dict(bucket=args.bucket, prefix=prefix, winners=1, conflicts=9,
                          restored_version=record["version"], duplicate_verified=True,
                          context=restored.context("task"), metrics=store.metrics.snapshot(),
                          note="Real objects retained under this unique prefix. No bucket, IAM or PostgreSQL changes."), indent=2))


if __name__ == "__main__":
    main()
