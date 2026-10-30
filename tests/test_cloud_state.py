import copy
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest

from agent_state.engine import Engine
from agent_state.maintenance import HISTORY_ACTION, SnapshotMaintenance
from agent_state.objects import digest, encode
from agent_state.runtime import Runtime
from agent_state.storage import (MemoryStore, Object, Conflict, Corrupt, Missing,
                                 Invalid, TooLarge, BudgetError)


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

    def test_cache_limits_ttl_and_invalidation(self):
        clock = [0.0]
        runtime = Runtime(self.engine, max_tasks=2, max_bytes=1400, ttl=10, clock=lambda: clock[0])
        runtime.read("task")
        self.engine.publish("task", "step", "agent", state(1), 0)
        self.assertEqual(runtime.read("task")["version"], 0)
        clock[0] = 11
        self.assertEqual(runtime.read("task")["version"], 1)
        self.assertFalse(runtime.invalidate("task", runtime.read("task")["generation"]))  # already current
        self.engine.publish("task", "step2", "agent", state(2), 1)
        _, generation = self.engine.head("task")
        self.assertTrue(runtime.invalidate("task", generation))
        self.assertEqual(runtime.read("task")["version"], 2)
        self.assertFalse(runtime.invalidate("task", generation))
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

    def test_invalidation_during_cold_read_does_not_cache_stale_state(self):
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

    def test_policy_materializes_each_due_target_once(self):
        class PolicyConnection(FakeConnection):
            statements = []
            def sql(self, statement):
                self.statements.append(statement)
                if "vc.snapshot_due" in statement:
                    due = [] if self.present else [dict(revision="00000000-0000-0000-0000-000000000001",
                                                        table_id="00000000-0000-0000-0000-000000000002")]
                    return json.dumps(due)
                return super().sql(statement)
        store, connection = MemoryStore(), PolicyConnection()
        runner = SnapshotMaintenance(Runtime(Engine(store)), connection)
        first = runner.run_due("agent-a", "main", 100)
        self.assertEqual([r["applied"] for r in first], [True])
        self.assertIn("vc.snapshot_due('main',100)", connection.statements[0])
        task = "snapshot-00000000-0000-0000-0000-000000000001-00000000-0000-0000-0000-000000000002"
        self.assertEqual(first[0]["task_id"], task)
        self.assertEqual(Engine(store).read(task)["state"]["status"], "completed")
        self.assertEqual(runner.run_due("agent-b"), [])  # nothing due after the snapshot
        self.assertEqual(connection.applications, 1)
        for branch, interval in [("main'; DROP TABLE x; --", 100), ("main", 0), ("main", True), ("main", "100")]:
            with self.assertRaises(Invalid):
                runner.due(branch, interval)


class HistoryConnection:
    """Answers vc.history_health only; the ancestor walk time differs on every call."""
    def __init__(self, reachable):
        self.reachable, self.statements = reachable, []

    def sql(self, statement):
        self.statements.append(statement)
        assert statement.startswith("SELECT vc.history_health('"), statement
        return json.dumps(dict(branch="main", head="00000000-0000-0000-0000-000000000009",
                               reachable_changesets=self.reachable, ancestor_walk_ms=6.8 + len(self.statements),
                               changesets_total=self.reachable + 3, first_parent_depth=self.reachable // 2,
                               commits_since_snapshot={"app.products": 40}, max_commits_since_snapshot=40))


class HistoryHealthTests(unittest.TestCase):
    def test_below_threshold_reports_facts_and_rerun_publishes_nothing(self):
        store, connection = MemoryStore(), HistoryConnection(2026)
        runner = SnapshotMaintenance(Runtime(Engine(store)), connection)
        first = runner.check_history("manager", merge_ms=[181, 170, 190])
        self.assertEqual((first["task_id"], first["published"], first["version"]), ("history-health-main", True, 0))
        state = Engine(store).read("history-health-main")["state"]
        self.assertEqual((state["status"], state["next_action"], state["uncertainties"]), ("completed", "", []))
        facts = state["facts"]
        self.assertEqual(facts["reachable_changesets"], 2026)
        self.assertEqual(facts["thresholds"], dict(max_reachable_changesets=20000, max_median_merge_ms=500))
        self.assertEqual((facts["thresholds_source"], facts["median_merge_ms"], facts["crossed"]), ("default", 181, 0))
        self.assertEqual(facts["max_commits_since_snapshot"], 40)
        again = SnapshotMaintenance(Runtime(Engine(store)), connection).check_history("manager", merge_ms=[181, 170, 190])
        self.assertEqual((again["published"], again["version"]), (False, 0))  # only the walk time differed
        connection.reachable = 2100
        self.assertEqual(runner.check_history("manager")["version"], 1)
        self.assertTrue(all(s.startswith("SELECT vc.history_health('main')") for s in connection.statements))

    def test_crossed_thresholds_add_uncertainty_and_next_action(self):
        store = MemoryStore()
        engine = Engine(store)
        runner = SnapshotMaintenance(Runtime(engine), HistoryConnection(20000))
        result = runner.check_history("manager")
        state = engine.read("history-health-main")["state"]
        self.assertEqual((state["status"], state["next_action"]), ("running", HISTORY_ACTION))
        self.assertEqual(state["uncertainties"], result["crossed"])
        self.assertIn("20000 reachable changesets", state["uncertainties"][0])
        # Thresholds from the task's fixed policy; later runs keep that policy.
        policy = engine.objects.put_bytes(json.dumps(dict(history_health=dict(
            max_reachable_changesets=1000, max_median_merge_ms=150))).encode())
        runner = SnapshotMaintenance(Runtime(engine), HistoryConnection(999))
        runner.check_history("manager", task_id="history-policy", policy_ref=policy)
        self.assertEqual(engine.read("history-policy")["state"]["status"], "completed")
        result = runner.check_history("manager", task_id="history-policy", merge_ms=[120, 160, 175])
        state = engine.read("history-policy")["state"]
        self.assertEqual((state["policy_ref"], state["facts"]["thresholds_source"]), (policy, "policy"))
        self.assertEqual((state["next_action"], len(result["crossed"])), (HISTORY_ACTION, 1))
        self.assertIn("Median merge time 160 ms", state["uncertainties"][0])
        other = engine.objects.put_bytes(b"protocol text without thresholds")
        with self.assertRaises(Conflict):
            runner.check_history("manager", task_id="history-policy", policy_ref=other)
        runner.check_history("manager", task_id="history-text", policy_ref=other)
        self.assertEqual(engine.read("history-text")["state"]["facts"]["thresholds_source"], "default")

    def test_invalid_policy_values_are_rejected_without_publishing(self):
        store = MemoryStore()
        engine = Engine(store)
        runner = SnapshotMaintenance(Runtime(engine), HistoryConnection(2026))
        for section in ([], dict(max_reachable_changesets=0), dict(max_reachable_changesets=True),
                        dict(max_reachable_changesets="20000"), dict(max_reachable_changesets=1.5),
                        dict(max_median_merge_ms=0), dict(max_median_merge_ms=None), dict(max_changesets=10)):
            policy = engine.objects.put_bytes(json.dumps(dict(history_health=section)).encode())
            with self.assertRaises(Invalid, msg=section):
                runner.check_history("manager", task_id="history-bad", policy_ref=policy)
        for kwargs in (dict(branch="main'; DROP TABLE x; --"), dict(merge_ms=[]), dict(merge_ms=[-1]),
                       dict(merge_ms=[True]), dict(merge_ms="181")):
            with self.assertRaises(Invalid, msg=kwargs):
                runner.check_history("manager", task_id="history-bad", **kwargs)
        with self.assertRaises(Missing):
            engine.read("history-bad")


if __name__ == "__main__":
    unittest.main(verbosity=2)
