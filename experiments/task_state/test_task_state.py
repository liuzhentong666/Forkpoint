"""Tests for task_state.py: checkpoints, freshness marks and the resume packet.

  python -m unittest discover -s experiments/task_state -v

Builds its own agentdb_test_taskstate_* database on the existing PostgreSQL server
(install and the 4,000-row seed, as the lab databases have) and drops it at the end.
Task state lives in agent_state's MemoryStore, as it will in the experiment.
"""
import argparse
import json
import unittest
import uuid

import task_state
from task_state import checkpoint, freshness, resume, tool_reply
from agent_state.engine import Engine
from agent_state.objects import encode
from agent_state.storage import BudgetError, Conflict, Invalid, MemoryStore
from db import Connection
from review_local import seed

PRODUCT = {"app.products": [{"id": 101}]}


def connect(database=None):
    return Connection(argparse.Namespace(config=None, password=False, role=None, database=database))


class TaskStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "agentdb_test_taskstate_" + uuid.uuid4().hex[:8]
        maintenance = connect()
        maintenance.create_database(cls.name)
        cls.addClassCleanup(maintenance.sql, f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE);',
                            maintenance.settings["maintenance_database"])
        cls.db = connect(cls.name)
        cls.db.install(cls.name)
        seed(cls.db)
        cls.base = cls.db.sql("SELECT vc.head();")

    def setUp(self):
        self.engine = Engine(MemoryStore())
        self.task = "t-" + uuid.uuid4().hex[:8]

    def commit(self, update):
        return self.db.sql(f"""BEGIN;
SELECT vc.begin_change('main',vc.head(),'test','{update}') AS d \\gset
UPDATE app.products SET {update};
SELECT vc.finish_change() AS r \\gset
COMMIT;
SELECT :'r';""").splitlines()[-1]

    def start(self, **facts):
        return checkpoint(self.engine, self.task, "c0", "gpt-b",
                          {"goal": "reduce stock of product 101", "facts": facts})

    def marks(self):
        return freshness(self.engine, self.engine.read(self.task)["state"], self.db.sql)

    def test_a_fact_turns_stale_when_its_row_changes_on_main_and_not_before(self):
        head = self.db.sql("SELECT vc.head();")
        self.start(stock={"value": 100, "based_on": {"revision": head, "rows": PRODUCT}})
        self.assertEqual(self.marks()["stock"]["status"], "fresh")
        self.commit("stock=50 WHERE id=202")  # another row
        self.assertEqual(self.marks()["stock"]["status"], "fresh")
        self.commit("stock=90 WHERE id=101")
        mark = self.marks()["stock"]
        self.assertEqual((mark["status"], mark["read_at"]), ("stale", head))
        self.assertEqual([(c["then"]["stock"], c["now"]["stock"]) for c in mark["changed"]], [(100, 90)])
        self.commit("stock=100 WHERE id=101")  # back to the value it was read at
        self.assertEqual(self.marks()["stock"]["status"], "fresh")

    def test_unknown_without_reads_or_when_a_row_cannot_be_found(self):
        self.start(note={"value": "no evidence"},
                   wrong_key={"value": 1, "based_on": {"revision": self.base, "rows": {"app.products": [{"id": "101"}]}}},
                   missing={"value": 1, "based_on": {"revision": self.base, "rows": {"app.products": [{"id": 999999}]}}})
        marks = self.marks()
        self.assertEqual({k: m["status"] for k, m in marks.items()}, {"note": "unknown", "wrong_key": "unknown", "missing": "unknown"})
        self.assertIn("not found", marks["missing"]["why"])

    def test_setting_or_dropping_a_fact_replaces_or_removes_its_reads(self):
        self.start(a={"value": 1, "based_on": {"revision": self.base, "rows": PRODUCT}},
                   b={"value": 2, "based_on": {"revision": self.base, "rows": PRODUCT}})
        self.assertEqual(len(self.engine.read(self.task)["state"]["evidence_refs"]), 2)
        checkpoint(self.engine, self.task, "c1", "gpt-b", {"facts": {"a": {"value": 3}}, "drop": ["b"]})
        state = self.engine.read(self.task)["state"]
        self.assertEqual((state["facts"], state["evidence_refs"]), ({"a": 3}, []))
        self.assertEqual(self.marks()["a"]["status"], "unknown")

    def test_checkpoints_update_the_state_and_retries_are_not_applied_twice(self):
        self.start()
        first = checkpoint(self.engine, self.task, "c1", "gpt-b", {"next_action": "merge", "open_items": ["confirm price"]})
        again = checkpoint(self.engine, self.task, "c1", "gpt-b", {"next_action": "merge", "open_items": ["confirm price"]})
        self.assertEqual((first["version"], again["version"], again["deduplicated"]), (1, 1, True))
        state = self.engine.read(self.task)["state"]
        self.assertEqual((state["next_action"], state["uncertainties"]), ("merge", ["confirm price"]))
        with self.assertRaises(Conflict):  # the same command ID for a different change
            checkpoint(self.engine, self.task, "c1", "gpt-b", {"next_action": "verify"})

    def test_bad_checkpoints_are_refused(self):
        self.start()
        for change in ({}, {"unknown": 1}, {"facts": []}, {"drop": "a"}, {"facts": {"a": {"based_on": None}}},
                       {"facts": {"a": {"value": 1, "based_on": {"revision": "x", "rows": PRODUCT}}}},
                       {"facts": {"a": {"value": 1, "based_on": {"revision": self.base, "rows": {"vc.branch": [{"name": "main"}]}}}}},
                       {"facts": {"a": {"value": 1, "based_on": {"revision": self.base,
                                                                  "rows": {"app.products": [{"id": i} for i in range(17)]}}}}}):
            with self.assertRaises(Invalid, msg=change):
                checkpoint(self.engine, self.task, "bad", "gpt-b", change)
        with self.assertRaises(Invalid):  # the engine's limit of 64 evidence references
            checkpoint(self.engine, self.task, "many", "gpt-b", {"facts": {
                f"f{i}": {"value": i, "based_on": {"revision": self.base, "rows": PRODUCT}} for i in range(65)}})

    def test_resume_marks_the_facts_it_shows_and_stays_within_its_budget(self):
        head = self.db.sql("SELECT vc.head();")
        facts = {f"fact-{i:03d}": {"value": "x" * 60, "based_on": {"revision": head, "rows": {"app.products": [{"id": 1 + i}]}}}
                 for i in range(40)}
        self.start(**facts)
        self.commit("stock=1 WHERE id=1")  # fact-000 turns stale
        packet = resume(self.engine, self.task, self.db.sql, max_bytes=4096)
        self.assertLessEqual(len(encode(packet)), 4096)
        self.assertEqual(set(packet["freshness"]), set(packet["facts"]))
        self.assertGreater(packet["omitted_facts"], 0)
        shown_stale = sum(m["status"] == "stale" for m in packet["freshness"].values())
        self.assertEqual(shown_stale + packet["omitted_stale"], 1)
        self.assertEqual(packet["main"], self.db.sql("SELECT vc.head();"))
        with self.assertRaises(BudgetError):
            resume(self.engine, self.task, self.db.sql, max_bytes=600)

    def test_marks_take_room_only_for_the_facts_shown(self):
        # 32 small facts whose rows all changed: the marks of all of them would not fit, but the
        # packet still comes back within budget, with the facts it can show and their marks.
        head = self.db.sql("SELECT vc.head();")
        self.start(**{f"f{i:02d}": {"value": i, "based_on": {"revision": head, "rows": {"app.products": [{"id": 201 + i}]}}}
                      for i in range(32)})
        self.commit("stock=stock+1 WHERE id BETWEEN 201 AND 232")
        marks = self.marks()
        self.assertGreater(len(encode({"freshness": marks})), 8192 - 256)  # what the old reservation refused
        packet = resume(self.engine, self.task, self.db.sql, max_bytes=8192)
        self.assertLessEqual(len(encode(packet)), 8192)
        self.assertTrue(packet["facts"])
        self.assertEqual(set(packet["freshness"]), set(packet["facts"]))
        self.assertEqual(len(packet["facts"]) + packet["omitted_stale"], 32)

    def test_a_state_written_without_this_module_still_resumes(self):
        ref = self.engine.objects.put_bytes(b"report written by an older client, not JSON")
        self.engine.publish(self.task, "old", "manager", {"goal": "old task", "status": "running",
                                                          "facts": {"stock": 100}, "evidence_refs": [ref]})
        packet = resume(self.engine, self.task, self.db.sql)
        self.assertEqual(packet["facts"], {"stock": 100})
        self.assertEqual(packet["freshness"]["stock"]["status"], "unknown")
        self.assertEqual(packet["evidence_refs"], [ref])

    def test_tool_reply_runs_checkpoint_and_resume_and_reports_errors_as_data(self):
        self.assertEqual(tool_reply(self.engine, self.db.sql, self.task, "gpt-b", "x1", {"action": "resume"})["error"], "no_task_state")
        reply = tool_reply(self.engine, self.db.sql, self.task, "gpt-b", "x2", {"action": "checkpoint", "change": {
            "goal": "reduce stock", "facts": {"stock": {"value": 100, "based_on": {"revision": self.base, "rows": PRODUCT}}}}})
        self.assertEqual(reply, {"exit_code": 0, "result": {"version": 0, "deduplicated": False}})
        result = tool_reply(self.engine, self.db.sql, self.task, "gpt-b", "x3", {"action": "resume"})["result"]
        self.assertEqual(result["facts"], {"stock": 100})
        bad = tool_reply(self.engine, self.db.sql, self.task, "gpt-b", "x4", {"action": "checkpoint", "change": {"facts": []}})
        self.assertEqual(bad["error"], "invalid_checkpoint")
        json.dumps(bad)  # replies are plain data


if __name__ == "__main__":
    unittest.main()
