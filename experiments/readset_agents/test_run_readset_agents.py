"""Tests for run_readset_agents.py, and a dry run of both groups with fake_readset_claude.py.

  python -m unittest discover -s experiments/readset_agents -v

DryRunTests runs four rounds in this process, through the real MCP servers, coordinator, psql and
both kinds of database: each group with two guarding stand-ins, which must land exactly one sale,
and with two that do not guard, which must land both. It takes a few minutes and drops the round
databases it made.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import run_readset_agents as runner
from run_readset_agents import Collision, outcome, plan_sql, succeeded
from db import Connection

DOC = runner.ROOT / "docs" / "READSET_AGENTS_EXPERIMENT.md"


def final(stock_101, stock_202, **changes):
    bundle = {str(p): {"id": p, "sku": f"sku-{p}", "name": f"product-{p}", "price": 10.0, "stock": s}
              for p, s in ((101, stock_101), (202, stock_202))}
    return {"bundle": bundle, "rows": 4000, "other_rows_unchanged": True, "orders": 0, "open_transactions": 0, **changes}


class OutcomeTests(unittest.TestCase):
    def test_correct_stale_or_other(self):
        self.assertEqual([outcome(final(85, 100)), outcome(final(100, 85))], ["correct", "correct"])
        self.assertEqual(outcome(final(85, 85)), "stale")
        self.assertEqual(outcome(final(85, 85, orders=1)), "stale")  # both sales landed, whatever else changed
        changed = final(85, 100)
        changed["bundle"]["202"]["price"] = 11.0
        for other in (final(100, 100), final(70, 100), final(85, 100, other_rows_unchanged=False),
                      final(85, 100, orders=1), final(85, 100, rows=3999), changed, {"bundle": {}}, {}):
            self.assertEqual(outcome(other), "other", other)

    def test_success_needs_the_correct_outcome_no_open_transaction_and_no_audit_error(self):
        self.assertTrue(succeeded({"audit_errors": []}, final(85, 100)))
        self.assertFalse(succeeded({"audit_errors": []}, final(85, 85)))
        self.assertFalse(succeeded({"audit_errors": []}, final(85, 100, open_transactions=1)))
        self.assertFalse(succeeded({"audit_errors": [{"error": "lost"}]}, final(85, 100)))


class GuardTests(unittest.TestCase):
    """P's guard as the SQL of the later writer's first execute wrote it (the note of 2026-10-28)."""

    def test_four_scripts(self):
        check = "(SELECT sum(stock) FROM products WHERE id IN (101, 202)) - 15 >= 180;"
        guard = ("SELECT id, stock FROM products WHERE id IN (101, 202) FOR UPDATE;\n"
                 "UPDATE products SET stock = stock - 15 WHERE id = 202\n   AND " + check)
        weak = "UPDATE products SET stock = stock - 15\n WHERE id = 202\n   AND " + check  # waits, then lands
        blind = "UPDATE products SET stock = stock - 15 WHERE id = 202;"
        repeatable = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;\n" + weak
        for sql, locked, isolation in ((guard, True, "read committed"), (weak, False, "read committed"),
                                       (blind, False, "read committed"), (repeatable, False, "repeatable read")):
            self.assertEqual(runner.p_guard(sql), {"locked_before_write": locked, "isolation": isolation}, sql)
        for clause in ("FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE"):
            self.assertTrue(runner.p_guard(guard.replace("FOR UPDATE", clause))["locked_before_write"], clause)
        self.assertIsNone(runner.p_guard("DO $$ BEGIN " + blind + " END $$")["locked_before_write"])  # unseen write


class ProtocolTests(unittest.TestCase):
    def test_the_prompts_are_the_registered_text_word_for_word(self):
        halves = DOC.read_text(encoding="utf-8").split('<a id="english-version"></a>')
        blocks = [half.split("```text\n", 1)[1].split("\n```", 1)[0] for half in halves]
        self.assertEqual(blocks[0], blocks[1])  # both halves register the same English prompt
        lines = blocks[1].splitlines()
        common = [line for line in lines if not line.startswith(("P: ", "F: ", "   "))]
        for group in runner.GROUPS:
            own, keep = [], False
            for line in lines[len(common):]:
                keep = line.startswith(group + ": ") if line.startswith(("P: ", "F: ")) else keep
                if keep:
                    own.append(line[3:])
            for actor, product in runner.PRODUCTS.items():
                expected = "\n".join(common + own).replace("{product}", str(product))
                self.assertEqual(runner.prompt_for(group, actor), expected, (group, actor))

    def test_the_registered_timing_and_a_tool_call_limit_above_it(self):
        self.assertEqual((runner.HOLD_SECONDS, runner.POLL_SECONDS, runner.COMMIT_SECONDS, runner.LOCK_TIMEOUT,
                          runner.CALL_BUDGET), (300, 0.05, 10, "10s", 40))
        server = runner.mcp_servers(argparse.Namespace(python="python"), 1, "token", Path("raw"), "gpt-a",
                                    Path("tool.json"))["mcpServers"]["agentdb"]
        self.assertEqual(server["timeout"], runner.TOOL_TIMEOUT * 1000)
        self.assertEqual(server["args"][server["args"].index("--timeout") + 1], str(runner.TOOL_TIMEOUT))
        self.assertGreater(runner.TOOL_TIMEOUT, runner.HOLD_SECONDS + runner.COMMIT_SECONDS + 10 + runner.STATEMENT_SECONDS)

    def test_protocol_hashes_every_project_module_the_runner_loads(self):
        listing = ("import json, sys; from pathlib import Path; import run_readset_agents as r; "
                   "print(json.dumps(sorted(Path(m.__file__).resolve().relative_to(r.ROOT).as_posix() "
                   "for m in list(sys.modules.values()) if getattr(m, '__file__', None) "
                   "and Path(m.__file__).resolve().is_relative_to(r.ROOT))))")
        loaded = json.loads(subprocess.run([sys.executable, "-c", listing], cwd=runner.HERE, capture_output=True,
                                           text=True, check=True).stdout)
        self.assertIn("agent_lab.py", loaded)
        self.assertEqual([path for path in loaded if path not in runner.SOURCES], [])
        self.assertEqual([path for path in runner.SOURCES if not (runner.ROOT / path).is_file()], [])


class FakeTransaction:
    """A Transaction stand-in: its statements take `seconds`, or wait until `blocker` has ended (a lock)."""

    def __init__(self, log, name, seconds=0.0, blocker=None, ok=True):
        self.log, self.pid, self.seconds, self.blocker, self.ok = log, name, seconds, blocker, ok
        self.closed = threading.Event()
        self.result = self.ended = self.began = None

    def begin(self):
        self.began = time.monotonic()
        self.log.append((self.pid, "begin", self.began))

    def poll(self, timeout):
        end = time.monotonic() + timeout
        while self.result is None:
            if time.monotonic() - self.began >= self.seconds and (self.blocker is None or self.blocker.closed.is_set()):
                self.result = subprocess.CompletedProcess(self.pid, 0 if self.ok else 3, "", "")
                self.log.append((self.pid, "statements", time.monotonic()))
            elif time.monotonic() >= end:
                return None
            else:
                time.sleep(0.002)
        return self.result

    def settle(self):
        return self.poll(30)

    def end(self):
        if self.settle().returncode == 0:
            self.ended = subprocess.CompletedProcess(self.pid, 0, "", "")
            self.log.append((self.pid, "end", time.monotonic()))
        self.close()
        return self.ended

    def close(self):
        self.closed.set()


class CollisionTests(unittest.TestCase):
    """The hold and the commit order, on stand-in transactions with shortened times."""

    def setUp(self):
        self.log = []

    def collide(self, collision, *writes):
        """Each (delay, actor, transaction) write in its own thread; the records by actor."""
        records = {}

        def write(delay, actor, transaction):
            time.sleep(delay)
            records[actor] = collision.write(actor, transaction)
        threads = [threading.Thread(target=write, args=w) for w in writes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return records

    def order(self):
        return [(name, event) for name, event, _ in self.log]

    def at(self, name, event):
        return next(stamp for n, e, stamp in self.log if (n, e) == (name, event))

    @staticmethod
    def held(record):
        return (record["released_ns"] - record["arrived_ns"]) / 1e9

    def test_a_first_write_waits_for_the_other_and_the_earlier_commits_when_the_later_finishes(self):
        a, b = FakeTransaction(self.log, "a"), FakeTransaction(self.log, "b", seconds=0.05)
        records = self.collide(Collision(lambda pid: False, hold=5, poll=0.01, commit=5),
                               (0, "gpt-a", a), (0.2, "gpt-b", b))
        self.assertEqual((records["gpt-a"]["role"], records["gpt-a"]["release"], records["gpt-b"]["role"]),
                         ("earlier", "other_arrived", "later"))
        self.assertGreaterEqual(self.held(records["gpt-a"]), 0.15)
        self.assertEqual(self.order(), [("a", "begin"), ("a", "statements"), ("b", "begin"), ("b", "statements"),
                                        ("a", "end"), ("b", "end")])
        self.assertEqual(records["gpt-a"]["commit_reason"], "later_finished")
        self.assertTrue(records["gpt-b"]["in_flight"])

    def test_the_earlier_commits_when_the_later_is_seen_waiting_for_a_lock(self):
        a = FakeTransaction(self.log, "a")
        b = FakeTransaction(self.log, "b", blocker=a)
        records = self.collide(Collision(lambda pid: pid == "b", hold=5, poll=0.01, commit=5),
                               (0, "gpt-a", a), (0.1, "gpt-b", b))
        self.assertEqual((records["gpt-a"]["commit_reason"], records["gpt-b"]["waited_for_lock"]), ("later_waiting", True))
        self.assertEqual(self.order(), [("a", "begin"), ("a", "statements"), ("b", "begin"), ("a", "end"),
                                        ("b", "statements"), ("b", "end")])

    def test_the_earlier_commits_at_most_commit_seconds_after_the_later_starts(self):
        a, b = FakeTransaction(self.log, "a"), FakeTransaction(self.log, "b", seconds=1.0)
        records = self.collide(Collision(lambda pid: False, hold=5, poll=0.01, commit=0.2),
                               (0, "gpt-a", a), (0.1, "gpt-b", b))
        self.assertEqual(records["gpt-a"]["commit_reason"], "commit_cap")
        waited = self.at("a", "end") - self.at("b", "begin")
        self.assertTrue(0.2 <= waited < 0.6, waited)
        self.assertLess(self.at("a", "end"), self.at("b", "statements"))

    def test_a_hold_ends_when_the_other_agents_turn_ends(self):
        collision = Collision(lambda pid: False, hold=5, poll=0.01, commit=5)
        threading.Timer(0.2, collision.turn_ended, ["gpt-b"]).start()
        record = collision.write("gpt-a", FakeTransaction(self.log, "a"))
        self.assertEqual((record["role"], record["release"]), ("alone", "other_turn_ended"))
        self.assertGreaterEqual(self.held(record), 0.15)
        self.assertEqual(self.order(), [("a", "begin"), ("a", "statements"), ("a", "end")])

    def test_a_hold_ends_after_the_cap_and_a_later_arrival_is_not_held(self):
        a, b = FakeTransaction(self.log, "a"), FakeTransaction(self.log, "b")
        records = self.collide(Collision(lambda pid: False, hold=0.2, poll=0.01, commit=5),
                               (0, "gpt-a", a), (0.5, "gpt-b", b))
        self.assertEqual((records["gpt-a"]["role"], records["gpt-a"]["release"]), ("alone", "hold_cap"))
        self.assertGreaterEqual(self.held(records["gpt-a"]), 0.2)
        self.assertEqual((records["gpt-b"]["role"], records["gpt-b"]["release"]), ("alone", "not_held"))
        self.assertEqual(self.order(), [("a", "begin"), ("a", "statements"), ("a", "end"),
                                        ("b", "begin"), ("b", "statements"), ("b", "end")])

    def test_a_failed_earlier_call_leaves_nothing_in_flight(self):
        a, b = FakeTransaction(self.log, "a", ok=False), FakeTransaction(self.log, "b")
        records = self.collide(Collision(lambda pid: True, hold=5, poll=0.01, commit=5),
                               (0, "gpt-a", a), (0.1, "gpt-b", b))
        self.assertFalse(records["gpt-b"]["in_flight"])
        self.assertNotIn("commit_reason", records["gpt-a"])
        self.assertEqual(self.order(), [("a", "begin"), ("a", "statements"), ("b", "begin"), ("b", "statements"),
                                        ("b", "end")])

    def test_a_held_call_whose_own_turn_ended_does_not_run(self):
        collision = Collision(lambda pid: False, hold=5, poll=0.01, commit=5)
        threading.Timer(0.1, collision.turn_ended, ["gpt-a"]).start()
        records = self.collide(collision, (0, "gpt-a", FakeTransaction(self.log, "a")),
                               (0.3, "gpt-b", FakeTransaction(self.log, "b")))
        self.assertEqual(records["gpt-a"]["role"], "abandoned")
        self.assertFalse(records["gpt-b"]["in_flight"])
        self.assertEqual(self.order(), [("b", "begin"), ("b", "statements"), ("b", "end")])


class ToolTests(unittest.TestCase):
    """What each tool refuses before any SQL runs."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def trial(self, group, branch=False):
        trial = runner.ReadsetTrial(None, self.folder, {"calls": []}, group)
        if branch:
            trial.branches["gpt-a"].append("gpt-a-1")
        return trial

    def reply(self, trial, arguments):
        return trial.handle("gpt-a", {"name": "agentdb", "arguments": arguments})

    def test_transaction_control_is_refused_in_both_groups(self):
        for group in runner.GROUPS:
            trial = self.trial(group, branch=True)
            for sql in ("BEGIN; UPDATE products SET stock = 85 WHERE id = 101", "start transaction", "COMMIT",
                        "SELECT 1; end", "ROLLBACK TO SAVEPOINT s", "abort", "PREPARE TRANSACTION 'x'"):
                for action in ("query", "execute"):
                    self.assertEqual(self.reply(trial, {"action": action, "sql": sql})["error"], "refused_statement",
                                     (group, action, sql))

    def test_role_changes_are_refused(self):
        trial = self.trial("P")
        for sql in ("RESET ROLE", "SET ROLE postgres", "set local role postgres", "SET SESSION AUTHORIZATION postgres",
                    'SET "role" = postgres', "SELECT set_config('role', 'postgres', false)"):
            reply = self.reply(trial, {"action": "execute", "sql": sql})
            self.assertEqual((reply["error"], reply["detail"]), ("refused_statement", runner.ROLE_REFUSAL), sql)

    def test_f_sql_may_not_name_the_vc_or_app_schemas(self):
        trial = self.trial("F", branch=True)
        for sql in ("SELECT vc.head()", "SELECT stock FROM app.products WHERE id = 101", "SET search_path = app, agent",
                    'SELECT "vc".head()'):
            self.assertEqual(self.reply(trial, {"action": "query", "sql": sql})["error"], "refused_statement", sql)
        self.assertIsInstance(plan_sql("SELECT stock FROM app.products WHERE id = 101", "P"), tuple)

    def test_arguments_each_action_refuses_and_the_budget(self):
        p, f, fresh = self.trial("P"), self.trial("F", branch=True), self.trial("F")
        for trial, arguments, error in (
                (p, {"action": "fork"}, "unknown_action"), (p, "execute", "invalid_arguments"),
                (p, {"action": "query"}, "invalid_arguments"),
                (p, {"action": "query", "sql": " -- nothing\n;"}, "invalid_arguments"),
                (p, {"action": "query", "sql": "SELECT 1", "main": True}, "invalid_arguments"),
                (p, {"action": "execute", "sql": "x" * (runner.SQL_BYTES + 1)}, "invalid_arguments"),
                (f, {"action": "fork", "sql": "SELECT 1"}, "invalid_arguments"),
                (f, {"action": "query", "sql": "SELECT 1", "main": "yes"}, "invalid_arguments"),
                (f, {"action": "merge"}, "invalid_arguments"),
                (f, {"action": "merge", "reads": {"app.products": [{"id": 101}]}}, "invalid_arguments"),
                (f, {"action": "merge", "reads": {"products": [101]}}, "invalid_arguments"),
                (f, {"action": "merge", "reads": {"products": [{}]}}, "invalid_arguments"),
                (fresh, {"action": "execute", "sql": "SELECT 1"}, "no_branch"),
                (fresh, {"action": "merge", "reads": {}}, "no_branch")):
            self.assertEqual(self.reply(trial, arguments)["error"], error, arguments)
        replies = [self.reply(p, {"action": "fork"})["error"] for _ in range(runner.CALL_BUDGET)]
        self.assertEqual(replies[33:35], ["unknown_action", "tool_budget_exhausted"])  # p's 40th and 41st calls
        self.assertEqual(len(p.summary["calls"]), 6 + runner.CALL_BUDGET)  # refusals are recorded too

    def test_statements_split_as_postgresql_does_and_leading_transaction_modes_go_first(self):
        sql = ("set transaction isolation level serializable, read write; SELECT 'a;commit' -- ; rollback\n;"
               " /* ; begin /* nested ; */ */ SELECT $x$;commit;$x$, E'\\';commit', \"a;b\";"
               " DO $$ BEGIN PERFORM 1; END $$")
        prelude, body = plan_sql(sql, "F")
        self.assertEqual(prelude, ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE , READ WRITE"])
        self.assertEqual([text.split()[0] for text in body], ["SELECT", "/*", "DO"])
        self.assertEqual(plan_sql("SELECT 1; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", "P"),
                         ([], ["SELECT 1", " SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"]))
        self.assertEqual(len(runner.split_sql("SELECT 'unterminated; COMMIT")), 1)


class DryRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        temp = Path(directory.name)
        args = argparse.Namespace(project=runner.ROOT, config=(runner.ROOT / "connection.local.json").resolve(),
                                  python=sys.executable, claude=(runner.HERE / "fake_readset_claude.py").resolve(),
                                  config_dir=temp / "config", workdir=temp / "work", output=temp / "out",
                                  model="claude-opus-5-5", effort="xhigh", tool_name="mcp__agentdb__agentdb",
                                  turn_timeout=300)
        args.output.mkdir()
        args.workdir.mkdir()
        cls.maintenance = Connection(argparse.Namespace(config=None, password=False, role=None, database=None))
        cls.rounds = {}
        for number, (group, guards) in enumerate([(g, x) for g in runner.GROUPS for x in (True, False)], 1):
            round_args = argparse.Namespace(**vars(args), child_env={"READSET_STAND_IN_GUARDS": "1" if guards else "0"})
            result = cls.rounds[group, guards] = runner.run_trial(round_args, number, group)
            if result.get("database"):
                cls.addClassCleanup(cls.maintenance.sql, f'DROP DATABASE IF EXISTS "{result["database"]}" WITH (FORCE);',
                                    cls.maintenance.settings["maintenance_database"])

    def test_guarding_stand_ins_land_one_sale_and_unguarded_ones_both_in_both_groups(self):
        for (group, guards), result in self.rounds.items():
            self.assertEqual(result["status"], "completed", (group, guards, result.get("error")))
            self.assertEqual((result["outcome"], result["success"]), ("correct", True) if guards else ("stale", False),
                             (group, guards))
            self.assertEqual(result["metrics"]["tool_calls"], len(result["calls"]))
            self.assertEqual(result["final_audit"]["open_transactions"], 0)

    def test_the_later_writer_runs_while_the_earlier_change_is_in_flight(self):
        for (group, guards), result in self.rounds.items():
            collision = result["collision"]
            self.assertTrue(collision["both_wrote"] and collision["in_flight"], (group, guards))
            self.assertEqual(sorted(collision["roles"].values()), ["earlier", "later"])
            # An unguarded P execute writes only its own row and finishes; a guarded one waits for the
            # earlier one's row locks, and every F merge waits for the earlier one's lock on main.
            waits = group == "F" or guards
            self.assertEqual(collision["commit_reason"], "later_waiting" if waits else "later_finished", (group, guards))
            guard = collision["later_guard"]
            if group == "P":
                self.assertEqual((guard["locked_before_write"], guard["isolation"], guard["error"]),
                                 (guards, "read committed", None))
            else:
                self.assertEqual((guard["named_other"], guard["error"]), (guards, "read_conflict" if guards else None))

    def test_the_plain_database_has_the_app_tables_rows_and_privileges_of_an_enrolled_one(self):
        catalog = """SELECT json_build_object(
          'columns',(SELECT json_agg(json_build_object('table',table_name,'column',column_name,'type',data_type,
              'nullable',is_nullable,'default',column_default) ORDER BY table_name,ordinal_position)
            FROM information_schema.columns WHERE table_schema='app'),
          'constraints',(SELECT json_agg(json_build_object('table',conrelid::regclass::text,'name',conname,
              'definition',pg_get_constraintdef(oid),'deferrable',condeferrable,'deferred',condeferred) ORDER BY conname)
            FROM pg_constraint WHERE connamespace='app'::regnamespace),
          'grants',(SELECT json_agg(json_build_object('table',table_name,'privilege',privilege_type)
              ORDER BY table_name,privilege_type)
            FROM information_schema.role_table_grants WHERE grantee='agentdb_client' AND table_schema='app'),
          'others',(SELECT md5(string_agg(concat_ws(',',id,sku,name,price,stock),';' ORDER BY id))
            FROM app.products WHERE id NOT IN (101,202)),
          'vc',to_regnamespace('vc') IS NOT NULL);"""
        found = {group: json.loads(self.maintenance.sql(catalog, self.rounds[group, True]["database"]))
                 for group in runner.GROUPS}
        self.assertEqual((found["P"].pop("vc"), found["F"].pop("vc")), (False, True))
        self.assertEqual(found["P"], found["F"])


if __name__ == "__main__":
    unittest.main()
