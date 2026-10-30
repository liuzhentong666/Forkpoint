from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest

from agent_state.engine import Engine
from agent_state.maintenance import SnapshotMaintenance
from agent_state.runtime import Runtime
from agent_state.storage import Conflict, MemoryStore, Missing

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

    def test_nondefault_prefix_keeps_tasks_apart(self):
        store, database = MemoryStore(), DatabaseDouble()
        prefix = "review/nondefault"
        SnapshotMaintenance(Runtime(Engine(store, prefix)), database).run("prefix-task", "agent", *TARGET)
        self.assertEqual(Engine(store, prefix).read("prefix-task")["state"]["status"], "completed")
        with self.assertRaises(Missing):
            Engine(store).read("prefix-task")


if __name__ == "__main__":
    unittest.main()
