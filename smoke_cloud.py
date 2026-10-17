"""Real cloud protocol checks in a unique prefix; retains all test objects."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid
from agent_state.config import load_settings, create_store
from agent_state.engine import Engine
from agent_state.runtime import Runtime
from agent_state.storage import Conflict


class InterruptedHead:
    def __init__(self, original, when):
        self.original, self.when = original, when

    def get(self, *args, **kwargs):
        return self.original.get(*args, **kwargs)

    def publish(self, *args, **kwargs):
        if self.when == "before":
            raise TimeoutError("Injected before sending publication")
        self.original.publish(*args, **kwargs)
        raise TimeoutError("Injected after successful real publication")


def exercise(store, prefix):
    engine = Engine(store, prefix)
    state = dict(goal="Verify cloud publication protocol", status="running", facts={"number": 0})
    engine.publish("task", "create", "smoke", state)
    barrier = threading.Barrier(10)
    def update(number):
        barrier.wait(timeout=30)
        try:
            return Engine(store, prefix).publish("task", f"write-{number}", "smoke",
                                                dict(state, facts={"number": number}), 0)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(update, range(10)))
    assert sum(item is not None for item in results) == 1
    current = Engine(store, prefix).read("task")
    number = current["state"]["facts"]["number"]
    old_request = dict(task_id="task", command_id=f"write-{number}", actor="smoke",
                       state=dict(state, facts={"number": number}), expected_version=0)
    assert engine.publish(**old_request)["deduplicated"]

    for when in ("before", "after"):
        expected = engine.read("task")["version"]
        proposal = dict(state, facts={"number": 100 if when == "before" else 200})
        interrupted = Engine(store, prefix, head_store=InterruptedHead(engine.heads, when))
        try:
            interrupted.publish("task", "fault-" + when, "smoke", proposal, expected)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Fault injection did not run")
        recovered = Engine(store, prefix).publish("task", "fault-" + when, "smoke", proposal, expected)
        assert recovered["version"] == expected + 1
        assert recovered["deduplicated"] == (when == "after")
    assert engine.publish(**old_request)["deduplicated"]
    before = store.metrics.snapshot()
    runtime = Runtime(Engine(store, prefix))
    record = runtime.read("task")
    after = store.metrics.snapshot()
    assert record["version"] == 3
    assert after["reads"] - before["reads"] == 2
    runtime.read("task")
    assert store.metrics.snapshot()["reads"] == after["reads"]
    return dict(winners=1, conflicts=9, final_version=record["version"], old_duplicate_verified=True,
                interruption_recovery_verified=True, cold_logical_reads=2,
                cold_downloaded_bytes=after["downloaded_bytes"]-before["downloaded_bytes"],
                context=runtime.context("task"), metrics=store.metrics.snapshot())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    settings = load_settings(args.config)
    store = create_store(settings)
    checked = store.check()
    prefix = settings["prefix"] + "/smoke-" + uuid.uuid4().hex
    result = exercise(store, prefix)
    print(json.dumps(dict(provider=settings["provider"], bucket=settings["bucket"], prefix=prefix,
                          bucket_check=checked, result=result, real_cloud_verified=True,
                          note="Test objects retained; no bucket, IAM or PostgreSQL changes"), indent=2))


if __name__ == "__main__":
    main()
