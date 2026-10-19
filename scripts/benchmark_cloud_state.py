"""Structural read-bound experiment; not a cloud latency or pricing benchmark."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_state.engine import Engine
from agent_state.runtime import Runtime
from agent_state.storage import MemoryStore
from agent_state.objects import encode


def main():
    store = MemoryStore()
    writer = Engine(store)
    samples = []
    start = time.perf_counter()
    for version in range(5001):
        value = dict(goal="Measure bounded resume cost", status="running", next_action="Check next result",
                     facts={"counter": version})
        writer.publish("long-running", f"command-{version}", "benchmark", value,
                       None if version == 0 else version - 1)
        if version in {0, 99, 999, 5000}:
            before = store.metrics.snapshot()
            runtime = Runtime(Engine(store))
            packet = runtime.context("long-running")
            after = store.metrics.snapshot()
            cold_reads = after["reads"] - before["reads"]
            hot_before = after["reads"]
            runtime.context("long-running")
            hot_reads = store.metrics.snapshot()["reads"] - hot_before
            assert cold_reads == 2 and hot_reads == 0
            assert len(encode(packet)) <= 8192
            samples.append(dict(published_events=version + 1, cold_logical_reads=cold_reads,
                                cold_downloaded_bytes=after["downloaded_bytes"] - before["downloaded_bytes"],
                                hot_logical_reads=hot_reads, context_bytes=len(encode(packet))))
    result = dict(backend="memory-test-only", samples=samples,
                  elapsed_seconds=round(time.perf_counter()-start, 3),
                  note="Counts demonstrate structural bounds only. Real GCS may use two SDK calls per read; cloud latency, billing and IAM are untested.")
    Path("cloud-read-benchmark.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
