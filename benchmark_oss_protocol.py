"""Structural OSS protocol benchmark; no bucket, network latency or billing test."""
import json
from pathlib import Path
import time
from agent_state.engine import Engine
from agent_state.objects import encode
from agent_state.oss import OSSStore, BLOCK
from agent_state.runtime import Runtime
from tests.oss_fake import FakeOSS


def main():
    store = OSSStore("test-bucket", "cn-hangzhou", client=FakeOSS())
    engine = Engine(store)
    samples = []
    started = time.perf_counter()
    for n in range(5001):
        engine.publish("task", f"command-{n}", "agent",
                       dict(goal="Measure bounded OSS recovery", facts={"number": n}),
                       None if n == 0 else n-1)
        if n + 1 in {1, 100, 1000, 5001}:
            runtime = Runtime(Engine(store))
            before = store.metrics.snapshot()
            current = runtime.read("task")
            after = store.metrics.snapshot()
            assert current["version"] == n
            assert after["reads"] - before["reads"] == 2
            runtime.read("task")
            hot = store.metrics.snapshot()["reads"] - after["reads"]
            assert hot == 0
            context = runtime.context("task")
            assert len(encode(context)) <= 8192
            samples.append(dict(published_events=n+1, cold_logical_reads=2,
                                cold_sdk_calls=after["sdk_calls"]-before["sdk_calls"],
                                cold_downloaded_bytes=after["downloaded_bytes"]-before["downloaded_bytes"],
                                append_record_bytes=BLOCK, hot_logical_reads=hot,
                                context_bytes=len(encode(context))))
    result = dict(backend="OSS SDK + in-memory service model", samples=samples,
                  elapsed_seconds=round(time.perf_counter()-started, 3), real_cloud_verified=False,
                  note="Bounded access structure only. No real OSS consistency, throughput, latency or cost claim.")
    Path("oss-protocol-benchmark.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
