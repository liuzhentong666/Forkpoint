import copy
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_state.engine import Engine
from agent_state.maintenance import SnapshotMaintenance
from agent_state.objects import digest, encode
from agent_state.runtime import Runtime
from agent_state.server import make_server
from agent_state.storage import (MemoryStore, Object, Conflict, Corrupt, Missing,
                                 Invalid, TooLarge, BudgetError, GCSStore)


def state(number=0):
    return dict(goal="Maintain the version engine", status="running", next_action="Verify outcome",
                facts={"number": number}, uncertainties=["Outcome requires verification"])


class FaultStore(MemoryStore):
    fault = None

    def put(self, key, data, expected_generation):
        if key.endswith("/head.json") and self.fault == "before":
            self.fault = None
            raise TimeoutError("injected before publication")
        result = super().put(key, data, expected_generation)
        if key.endswith("/head.json") and self.fault == "after":
            self.fault = None
            raise TimeoutError("injected lost successful response")
        return result


class StateTests(unittest.TestCase):
    def setUp(self):
        self.store = FaultStore()
        self.engine = Engine(self.store)
        self.engine.publish("task", "create", "agent", state())

    def test_same_head_has_one_winner_with_ten_writers(self):
        barrier = threading.Barrier(10)
        def write(number):
            barrier.wait(timeout=10)
            try:
                Engine(self.store).publish("task", "write-" + str(number), "agent", state(number), 0)
                return "published"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=10) as pool:
            result = list(pool.map(write, range(10)))
        self.assertEqual(result.count("published"), 1)
        self.assertEqual(result.count("conflict"), 9)
        self.assertEqual(self.engine.read("task")["version"], 1)

    def test_duplicate_concurrent_command_has_one_publication(self):
        barrier = threading.Barrier(10)
        def write(_):
            barrier.wait(timeout=10)
            return Engine(self.store).publish("task", "same", "agent", state(1), 0)
        with ThreadPoolExecutor(max_workers=10) as pool:
            result = list(pool.map(write, range(10)))
        self.assertEqual(sum(not item["deduplicated"] for item in result), 1)
        self.assertTrue(all(item["version"] == 1 for item in result))

    def test_old_command_remains_idempotent_after_many_commits(self):
        first = self.engine.publish("task", "first", "agent", state(1), 0)
        for n in range(2, 201):
            self.engine.publish("task", "step-" + str(n), "agent", state(n), n-1)
        restored = Engine(self.store)
        replay = restored.publish("task", "first", "agent", state(1), 0)
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["version"], first["version"])
        self.assertEqual(restored.read("task")["version"], 200)

    def test_same_command_id_different_content_rejected(self):
        self.engine.publish("task", "first", "agent", state(1), 0)
        with self.assertRaises(Conflict):
            self.engine.publish("task", "first", "agent", state(2), 0)

    def test_failure_before_publish_leaves_old_state(self):
        self.store.fault = "before"
        with self.assertRaises(TimeoutError):
            self.engine.publish("task", "first", "agent", state(1), 0)
        self.assertEqual(Engine(self.store).read("task")["version"], 0)
        result = self.engine.publish("task", "first", "agent", state(1), 0)
        self.assertEqual(result["version"], 1)

    def test_lost_publish_response_and_later_write_recover(self):
        self.store.fault = "after"
        with self.assertRaises(TimeoutError):
            self.engine.publish("task", "first", "agent", state(1), 0)
        self.engine.publish("task", "second", "agent", state(2), 1)
        result = Engine(self.store).publish("task", "first", "agent", state(1), 0)
        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(self.engine.read("task")["version"], 2)

    def test_history_pages_resume_without_repeating_events(self):
        for n in range(1, 11):
            self.engine.publish("task", "step-" + str(n), "agent", state(n), n-1)
        first = self.engine.history("task", limit=4)
        second = self.engine.history("task", limit=4, cursor=first["next_cursor"])
        self.assertEqual([x["version"] for x in first["events"]], [10,9,8,7])
        self.assertEqual([x["version"] for x in second["events"]], [6,5,4,3])
        with self.assertRaises(Invalid):
            self.engine.history("another", cursor=first["next_cursor"])

    def test_cold_state_never_replays_history(self):
        for n in range(1, 151):
            self.engine.publish("task", "step-" + str(n), "agent", state(n), n-1)
        before = self.store.metrics.snapshot()["reads"]
        self.assertEqual(Engine(self.store).read("task")["version"], 150)
        self.assertEqual(self.store.metrics.snapshot()["reads"] - before, 2)

    def test_hash_mismatch_detected(self):
        ref = self.engine.read("task")["state_ref"]
        key = "agentdb-state/objects/" + ref
        obj = self.store.get(key)
        self.store.put(key, b"{}", obj.generation)
        with self.assertRaises(Corrupt):
            Engine(self.store).read("task")

    def test_oversize_and_missing_evidence_cannot_publish(self):
        big = state()
        big["facts"]["blob"] = "x" * 40000
        with self.assertRaises(TooLarge):
            self.engine.publish("task", "big", "agent", big, 0)
        missing = state()
        missing["evidence_refs"] = ["a" * 64]
        with self.assertRaises(Missing):
            self.engine.publish("task", "missing", "agent", missing, 0)
        self.assertEqual(self.engine.read("task")["version"], 0)

    def test_artifacts_and_policy_are_content_addressed(self):
        ref = self.engine.objects.put_bytes("规则内容".encode())
        self.assertEqual(ref, self.engine.objects.put_bytes("规则内容".encode()))
        value = state()
        value.update(policy_ref=ref, evidence_refs=[ref])
        self.engine.publish("task", "with-evidence", "agent", value, 0)
        restored = Engine(self.store)
        self.assertEqual(restored.read("task")["state"]["policy_ref"], ref)
        self.assertEqual(restored.objects.get_bytes(ref).decode(), "规则内容")

    def test_hot_reads_do_not_contact_store_and_return_copies(self):
        runtime = Runtime(self.engine)
        record = runtime.read("task")
        before = self.store.metrics.snapshot()["reads"]
        record["state"]["goal"] = "mutated by caller"
        self.assertEqual(runtime.read("task")["state"]["goal"], state()["goal"])
        self.assertEqual(self.store.metrics.snapshot()["reads"], before)

    def test_cache_limits_ttl_and_notifications(self):
        clock = [0.0]
        runtime = Runtime(self.engine, max_tasks=2, max_bytes=1400, ttl=10, clock=lambda: clock[0])
        runtime.read("task")
        original = runtime.read("task")
        self.engine.publish("task", "step", "agent", state(1), 0)
        self.assertEqual(runtime.read("task")["version"], 0)
        clock[0] = 11
        self.assertEqual(runtime.read("task")["version"], 1)
        self.assertFalse(runtime.notify(dict(objectId=self.engine.head_key("task"), eventType="OBJECT_FINALIZE",
                                             objectGeneration=str(original["generation"]))))
        self.engine.publish("task", "step2", "agent", state(2), 1)
        _, generation = self.engine.head("task")
        notification = dict(objectId=self.engine.head_key("task"), eventType="OBJECT_FINALIZE",
                            objectGeneration=str(generation), bucketId="test")
        self.assertFalse(runtime.notify(notification, bucket="wrong"))
        self.assertTrue(runtime.notify(notification, bucket="test"))
        self.assertEqual(runtime.read("task")["version"], 2)
        self.assertFalse(runtime.notify(notification, bucket="test"))
        for i in range(5):
            self.engine.publish(f"task-{i}", "create", "agent", state())
            runtime.read(f"task-{i}")
        stats = runtime.metrics()["cache"]
        self.assertLessEqual(stats["tasks"], 2)
        self.assertLessEqual(stats["bytes"], 1400)

    def test_context_byte_budget_reports_omitted_facts(self):
        value = state()
        value["facts"] = {f"fact-{i}": "中文证据" * 30 for i in range(20)}
        self.engine.publish("task", "large-facts", "agent", value, 0)
        result = Runtime(self.engine).context("task", max_bytes=1400)
        self.assertLessEqual(len(encode(result)), 1400)
        self.assertGreater(result["omitted_facts"], 0)
        self.assertEqual(result["goal"], value["goal"])
        self.assertEqual(result["uncertainties"], value["uncertainties"])
        self.assertEqual(result["omitted_facts"] + len(result["facts"]), 20)

    def test_notification_during_cold_read_does_not_cache_stale_state(self):
        entered, release = threading.Event(), threading.Event()
        store = self.store
        class SlowEngine(Engine):
            first = True
            def read(self, task_id):
                result = super().read(task_id)
                if self.first:
                    self.first = False
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test synchronization timed out")
                return result
        runtime = Runtime(SlowEngine(store))
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(runtime.read, "task")
            self.assertTrue(entered.wait(timeout=5))
            self.engine.publish("task", "new", "agent", state(1), 0)
            _, generation = self.engine.head("task")
            runtime.invalidate("task", generation)
            release.set()
            self.assertEqual(reading.result(timeout=5)["version"], 0)
        self.assertEqual(runtime.metrics()["cache"]["tasks"], 0)
        self.assertEqual(runtime.read("task")["version"], 1)

    def test_immutable_cache_bounds_small_objects_and_total_bytes(self):
        for i in range(600):
            self.engine.objects.put_bytes(str(i).encode())
        self.assertLessEqual(self.engine.objects.cache_metrics()["objects"], 512)
        tiny = Engine(self.store, object_cache_bytes=64)
        for i in range(50):
            tiny.objects.put_bytes((str(i) + "-payload").encode())
        self.assertLessEqual(tiny.objects.cache_metrics()["bytes"], 64)

    def test_context_token_budget_is_enforced_for_full_envelope(self):
        # Inject a deterministic tokenizer; production supplies tiktoken.
        counter = lambda text: len(text)
        runtime = Runtime(self.engine, token_counter=counter, tokenizer_name="character-test")
        result = runtime.context("task", max_tokens=1100)
        self.assertLessEqual(counter(encode(result).decode()), 1100)
        with self.assertRaises(BudgetError):
            runtime.context("task", max_tokens=50)
        with self.assertRaises(Invalid):
            Runtime(self.engine).context("task", max_tokens=1000)

    def test_essential_context_is_never_silently_truncated(self):
        with self.assertRaises(BudgetError):
            Runtime(self.engine).context("task", max_bytes=256)


class HTTPTests(unittest.TestCase):
    def test_http_authenticated_publish_read_context_and_conflict(self):
        store = MemoryStore()
        server = make_server(Runtime(Engine(store)), port=0, token="test-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        def request(path, method="GET", body=None, token=True):
            headers = {"Content-Type":"application/json"}
            if token:
                headers["Authorization"] = "Bearer test-token"
            req = Request(url + path, data=encode(body) if body is not None else None,
                          headers=headers, method=method)
            with urlopen(req, timeout=5) as response:
                return json.loads(response.read())
        try:
            with self.assertRaises(HTTPError) as error:
                request("/health", token=False)
            self.assertEqual(error.exception.code, 401)
            create = dict(command_id="create", actor="agent", state=state())
            self.assertEqual(request("/v1/tasks/t", "POST", create)["version"], 0)
            self.assertEqual(request("/v1/tasks/t")["version"], 0)
            self.assertEqual(request("/v1/tasks/t/context", "POST", {})["goal"], state()["goal"])
            update = dict(command_id="update", actor="agent", state=state(1), expected_version=0)
            self.assertEqual(request("/v1/tasks/t", "PUT", update)["version"], 1)
            update["command_id"] = "another"
            with self.assertRaises(HTTPError) as error:
                request("/v1/tasks/t", "PUT", update)
            self.assertEqual(error.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class FakeConnection:
    present = False
    applications = 0

    def sql(self, statement):
        if "vc.materialize" in statement:
            self.present = True
            self.applications += 1
            return "4000"
        return json.dumps(dict(revision_exists=True, table_exists=True, snapshot_exists=self.present))


class MaintenanceTests(unittest.TestCase):
    def test_crash_after_postgresql_commit_does_not_reapply(self):
        store = MemoryStore()
        connection = FakeConnection()
        runner = SnapshotMaintenance(Runtime(Engine(store)), connection)
        target = ("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002")
        def crash():
            raise TimeoutError("agent disconnected after DB commit")
        with self.assertRaises(TimeoutError):
            runner.run("maintenance", "agent-a", *target, after_apply=crash)
        self.assertEqual(Engine(store).read("maintenance")["state"]["status"], "running")
        replacement = SnapshotMaintenance(Runtime(Engine(store)), connection)
        result = replacement.run("maintenance", "agent-b", *target)
        self.assertTrue(result["verified"])
        self.assertFalse(result["applied"])
        self.assertEqual(connection.applications, 1)
        self.assertEqual(Engine(store).read("maintenance")["state"]["status"], "completed")


class GCSContractTests(unittest.TestCase):
    def test_sdk_generation_preconditions_and_pinned_download(self):
        try:
            from google.api_core.exceptions import PreconditionFailed, NotFound
        except ImportError:
            self.skipTest("Install requirements-cloud.txt to test the official SDK adapter")
        memory = MemoryStore()
        calls = []
        class Blob:
            def __init__(self, key, obj=None):
                self.key = key
                self.generation = obj.generation if obj else None
                self.size = len(obj.data) if obj else None
            def upload_from_string(self, data, **kwargs):
                calls.append(("put", kwargs))
                try:
                    self.generation = memory.put(self.key, data, kwargs["if_generation_match"])
                except Conflict as error:
                    raise PreconditionFailed("condition") from error
            def download_as_bytes(self, **kwargs):
                calls.append(("get", kwargs))
                obj = memory.get(self.key)
                if obj.generation != kwargs["if_generation_match"]:
                    raise NotFound("generation disappeared")
                return obj.data
        class Bucket:
            def blob(self, key):
                return Blob(key)
            def get_blob(self, key, **kwargs):
                try:
                    return Blob(key, memory.get(key))
                except Missing:
                    return None
        class Client:
            def bucket(self, name):
                self.name = name
                return Bucket()
        store = GCSStore("contract-test", client=Client())
        generation = store.put("key", b"value", 0)
        self.assertEqual(store.get("key").data, b"value")
        with self.assertRaises(Conflict):
            store.put("key", b"bad", 0)
        with self.assertRaises(TooLarge):
            store.get("key", max_bytes=2)
        self.assertEqual(calls[0][1]["if_generation_match"], 0)
        self.assertEqual(calls[1][1]["if_generation_match"], generation)
        self.assertIsNotNone(calls[0][1]["retry"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
