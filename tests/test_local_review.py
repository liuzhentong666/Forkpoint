from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from agent_state.engine import Engine
from agent_state.maintenance import SnapshotMaintenance
from agent_state.oss import OSSStore
from agent_state.runtime import Runtime
from agent_state.storage import Conflict, MemoryStore, Missing
from agent_state import server
import maintain_snapshot
from oss_fake import FakeOSS

TARGET = ("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002")


class DatabaseDouble:
    def __init__(self):
        self.applications = 0
        self.lock = threading.Lock()

    def sql(self, statement):
        with self.lock:
            if "SELECT vc.materialize(" in statement:
                self.applications += 1
                return "4000"
            return json.dumps(dict(revision_exists=True, table_exists=True,
                                   snapshot_exists=self.applications > 0))


class MaintenanceReviewTests(unittest.TestCase):
    def test_zombie_maintainer_cannot_report_another_agents_publication_as_its_own(self):
        store, database = MemoryStore(), DatabaseDouble()
        paused, release = threading.Event(), threading.Event()
        def stall():
            paused.set()
            if not release.wait(10):
                raise TimeoutError("Test coordinator failed to release A")
        a = SnapshotMaintenance(Runtime(Engine(store)), database)
        b = SnapshotMaintenance(Runtime(Engine(store)), database)
        with ThreadPoolExecutor(max_workers=1) as pool:
            asleep = pool.submit(a.run, "zombie", "agent-a", *TARGET, after_apply=stall)
            try:
                self.assertTrue(paused.wait(5))
                finished = b.run("zombie", "agent-b", *TARGET)
                self.assertTrue(finished["verified"])
                head = Engine(store).read("zombie")
            finally:
                release.set()
            with self.assertRaises(Conflict):
                asleep.result(timeout=5)
        self.assertEqual(Engine(store).read("zombie"), head)
        self.assertEqual(database.applications, 1)
        history = Engine(store).history("zombie")["events"]
        self.assertEqual(history[0]["actor"], "agent-b")

    def test_crash_after_intent_before_database_action_is_resumable(self):
        store, database = MemoryStore(), DatabaseDouble()
        a = SnapshotMaintenance(Runtime(Engine(store)), database)
        def crash():
            self.assertEqual(Engine(store).read("intent-crash")["state"]["status"], "running")
            raise InterruptedError("Intent is durable; no database action yet")
        with self.assertRaises(InterruptedError):
            a.run("intent-crash", "agent-a", *TARGET, after_intent=crash)
        self.assertEqual(database.applications, 0)
        resumed = SnapshotMaintenance(Runtime(Engine(store)), database).run("intent-crash", "agent-b", *TARGET)
        self.assertTrue(resumed["verified"])
        self.assertTrue(resumed["applied"])
        self.assertEqual(database.applications, 1)
        self.assertEqual(Engine(store).read("intent-crash")["state"]["status"], "completed")

    def test_nondefault_prefix_through_json_factory_maintenance_cli_and_server(self):
        fake, database = FakeOSS(), DatabaseDouble()
        prefix = "review/nondefault"
        store = OSSStore("test-bucket", "cn-hangzhou", client=fake)
        def factory(bucket, region, endpoint=None):
            self.assertEqual((bucket, region), ("test-bucket", "cn-hangzhou"))
            return OSSStore(bucket, region, endpoint, client=fake)
        observed = []
        def listener(runtime, *args):
            handle = Mock()
            def serve(**kwargs):
                observed.append(runtime.read("config-task", fresh=True))
                self.assertEqual(runtime.engine.objects.prefix, prefix)
            handle.serve_forever.side_effect = serve
            return handle
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(dict(provider="oss", bucket="test-bucket", region="cn-hangzhou", prefix=prefix)))
            command = ["maintain_snapshot.py", "--cloud-config", str(path), "--task", "config-task",
                       "--actor", "agent", "--revision", TARGET[0], "--table-id", TARGET[1]]
            with patch("agent_state.oss.OSSStore", side_effect=factory), \
                    patch("maintain_snapshot.Connection", return_value=database), \
                    patch.object(sys, "argv", command), contextlib.redirect_stdout(io.StringIO()):
                maintain_snapshot.main()
            with patch("agent_state.oss.OSSStore", side_effect=factory), \
                    patch("agent_state.server.make_server", side_effect=listener), \
                    patch.object(sys, "argv", ["server", "--config", str(path)]):
                server.main()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["state"]["status"], "completed")
        self.assertEqual(Engine(store, prefix).read("config-task"), observed[0])
        with self.assertRaises(Missing):
            Engine(store).read("config-task")


if __name__ == "__main__":
    unittest.main()
