"""Tests for run_state_trials.py, and a dry run with fake_state_claude.py.

  python -m unittest discover -s experiments/task_state -v

DryRunTests runs one round of each setup in this process, through the real MCP server,
coordinator, agent_lab.py, archive and task state, with the reset limit lowered so that
resets happen (the stand-in's requests stay below the real 10,000). It takes a few minutes
and drops the round databases it made.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

import run_state_trials as runner
from run_state_trials import STALE, TARGET, StateTrial, outcome
from db import Connection

HEAD = str(uuid.uuid4())


class OutcomeTests(unittest.TestCase):
    def test_correct_stale_or_other(self):
        self.assertEqual(outcome({"product": {"stock": TARGET}, "inventory": f"STOCK = {TARGET}\n"}), "correct")
        self.assertEqual(outcome({"product": {"stock": TARGET}, "inventory": f"STOCK = {STALE}\n"}), "other")
        self.assertEqual(outcome({"product": {"stock": STALE}, "inventory": f"STOCK = {STALE}\n"}), "stale")
        self.assertEqual(outcome({"product": {"stock": 100}, "inventory": None}), "other")
        self.assertEqual(outcome({}), "other")

    def test_the_tool_is_the_same_for_both_setups_and_has_no_verify(self):
        actions = runner.TOOL_S["inputSchema"]["properties"]["action"]["enum"]
        self.assertEqual(actions, ["info", "read-product", "read-code", "fork", "propose", "diff", "merge",
                                   "checkpoint", "resume"])
        self.assertNotIn("verify", runner.TOOL_S["description"].split("Errors")[0])


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.summary = {"calls": []}

    def tearDown(self):
        self.temp.cleanup()

    def trial(self, setup):
        trial = StateTrial(None, Path(self.temp.name), self.summary, setup)
        def sql(statement):
            if statement.strip() == "SELECT vc.head();":
                return HEAD
            if "rows_at" in statement:
                return json.dumps({"main": HEAD, "known": HEAD in statement, "doc": {"id": 202, "stock": 100}})
            raise AssertionError(statement)
        trial.state_sql = sql
        return trial

    def handle(self, trial, **arguments):
        return trial.handle("gpt-b", {"name": "agentdb", "arguments": arguments})

    def test_read_product_checks_its_arguments(self):
        trial = self.trial("U")
        self.assertEqual(self.handle(trial, action="read-product", product="202", revision=HEAD)["result"]["product"]["stock"], 100)
        for product, revision in (("0", HEAD), ("4001", HEAD), ("02", HEAD), ("x", HEAD), ("202", "nope")):
            self.assertEqual(self.handle(trial, action="read-product", product=product, revision=revision)["error"],
                             "invalid_arguments", (product, revision))
        self.assertEqual(self.handle(trial, action="read-product", product="202", revision=str(uuid.uuid4()))["error"], "not_found")

    def test_verify_and_unknown_fields_are_refused(self):
        trial = self.trial("U")
        self.assertEqual(self.handle(trial, action="verify")["error"], "unknown_action")
        self.assertEqual(self.handle(trial, action="info", product="202")["error"], "invalid_arguments")
        self.assertEqual(self.handle(trial, action="resume", entries=[1])["error"], "invalid_arguments")

    def test_only_setup_f_marks_facts(self):
        change = {"goal": "restock 101", "facts": {"stock_202": {"value": 100}}}
        for setup, marked in (("U", False), ("F", True)):
            trial = self.trial(setup)
            self.assertEqual(self.handle(trial, action="checkpoint", change=change)["exit_code"], 0)
            packet = self.handle(trial, action="resume")["result"]
            self.assertEqual(packet["facts"], {"stock_202": 100})
            self.assertEqual("freshness" in packet, marked, setup)
        self.assertEqual([c.get("error") for c in self.summary["calls"]], [None] * 4)


class DryRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        temp = Path(cls.directory.name)
        project = runner.ROOT
        cls.args = argparse.Namespace(
            project=project, config=(project / "connection.local.json").resolve(), python=sys.executable,
            claude=(runner.HERE / "fake_state_claude.py").resolve(), config_dir=temp / "config", child_env={},
            workdir=temp / "work", output=temp / "out", model="claude-opus-5-5", effort="xhigh",
            tool_name="mcp__agentdb__agentdb", turn_timeout=300)
        cls.args.output.mkdir()
        cls.args.workdir.mkdir()
        cls.saved_limit, runner.LIMIT = runner.LIMIT, 4000
        cls.addClassCleanup(setattr, runner, "LIMIT", cls.saved_limit)
        cls.rounds = {}
        maintenance = Connection(argparse.Namespace(config=None, password=False, role=None, database=None))
        for number, setup in enumerate(runner.SETUPS, 1):
            result = runner.run_trial(cls.args, number, setup)
            cls.rounds[setup] = result
            if result.get("database"):
                cls.addClassCleanup(maintenance.sql, f'DROP DATABASE IF EXISTS "{result["database"]}" WITH (FORCE);',
                                    maintenance.settings["maintenance_database"])

    def test_both_rounds_complete_with_up_to_three_resets_per_agent(self):
        for setup, result in self.rounds.items():
            self.assertEqual(result["status"], "completed", (setup, result.get("error")))
            resets = [s for s in result["switches"] if s["actor"] == "gpt-b"]
            self.assertTrue(1 <= len(resets) <= runner.MAX_RESETS, (setup, len(resets)))
            self.assertEqual(result["metrics"]["tool_calls"], len(result["calls"]))
            self.assertEqual(result["final_audit"]["source"]["stock"], runner.UPDATED)
            for reset in resets:
                text = (self.args.output / f"run-{result['run']:02d}" / f"handoff.gpt-b.{reset['number']}.txt").read_text(encoding="utf-8")
                self.assertIn("Your instructions for the current stage were:", text)
                self.assertEqual('"freshness"' in text, setup == "F" and reset["state_saved"], (setup, reset["number"]))

    def test_the_scripted_stand_in_lands_the_stale_value_without_marks_and_the_right_one_with_them(self):
        self.assertEqual((self.rounds["U"]["outcome"], self.rounds["U"]["success"]), ("stale", False))
        self.assertEqual((self.rounds["F"]["outcome"], self.rounds["F"]["success"]), ("correct", True))
        self.assertEqual(self.rounds["U"]["source_rereads_after_update"], 0)
        self.assertGreaterEqual(self.rounds["F"]["source_rereads_after_update"], 1)


if __name__ == "__main__":
    unittest.main()
