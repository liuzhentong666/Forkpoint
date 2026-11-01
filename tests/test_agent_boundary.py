"""The broker and the keeper, without a database; scripts/check_agent_boundaries.py attacks the real thing."""
import argparse
import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_boundary
from agent_boundary import issue_login, retire_login, scram_verifier
from agent_state.engine import Engine
from agent_state.maintenance import BOUNDARY_ACTION, BoundaryKeeper
from agent_state.runtime import Runtime
from agent_state.storage import Conflict, Invalid, MemoryStore

M1, M2, M3, BASE = (f"00000000-0000-0000-0000-00000000000{n}" for n in (1, 2, 3, 4))


class Recorder:
    def __init__(self, settings=None):
        self.statements, self.settings = [], settings or dict(database="agentdb_test_x", host="/var/run/postgresql", port=5432)

    def sql(self, statement, database=None):
        self.statements.append(statement)
        return "2026-11-01 00:00:00+00" if statement.startswith("SELECT (clock_timestamp()") else ""


class BrokerTests(unittest.TestCase):
    def test_a_verifier_matches_its_password_and_does_not_contain_it(self):
        password = "correct horse"
        verifier = scram_verifier(password)
        match = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)", verifier)
        iterations, salt, stored, server = int(match[1]), base64.b64decode(match[2]), base64.b64decode(match[3]), base64.b64decode(match[4])
        salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        self.assertEqual(stored, hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest())
        self.assertEqual(server, hmac.digest(salted, b"Server Key", "sha256"))
        self.assertNotIn(password, verifier)
        self.assertNotEqual(verifier, scram_verifier(password))

    def test_a_login_gets_a_fixed_budget_an_expiry_and_only_the_worker_role(self):
        broker = Recorder()
        name, password, until = issue_login(broker, "agentdb_test_x", 50, 1000, 2, connections=3)
        script = broker.statements[-1]
        self.assertRegex(name, r"^agent_[0-9a-f]{12}$")
        self.assertEqual(until, "2026-11-01 00:00:00+00")
        self.assertNotIn(password, "".join(broker.statements))
        self.assertIn("PASSWORD 'SCRAM-SHA-256$", script)
        for part in (f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT",
                     f"CONNECTION LIMIT 3 VALID UNTIL '{until}'", f"GRANT agentdb_worker TO {name} WITH INHERIT TRUE, SET FALSE;",
                     f'GRANT CONNECT ON DATABASE "agentdb_test_x" TO {name};', f"ALTER ROLE {name} SET temp_file_limit='100MB';",
                     f"ALTER ROLE {name} SET log_statement='all';", f"SELECT vc.issue_budget('{name}',50,1000,0.1);"):
            self.assertIn(part, script)
        self.assertEqual(script.count("GRANT agentdb"), 1)  # no other role
        self.assertNotEqual(name, issue_login(Recorder(), "agentdb_test_x", 50, 1000, 2)[0])

    def test_a_login_with_no_budget_can_only_read_and_a_wide_one_is_issued_on_purpose(self):
        broker = Recorder()
        name, _, _ = issue_login(broker, "agentdb_test_x", 0, 0, 1)
        self.assertIn(f"SELECT vc.issue_budget('{name}',0,0,0.1);", broker.statements[-1])
        name, _, _ = issue_login(broker, "agentdb_test_x", 5, 5, 1, max_table_share=1)
        self.assertIn(f"SELECT vc.issue_budget('{name}',5,5,1.0);", broker.statements[-1])

    def test_bad_arguments_stop_before_any_sql(self):
        for label, kwargs in (("rows", dict(rows=-1)), ("rows", dict(rows=True)), ("bytes", dict(size=-1)), ("hours", dict(hours=0)),
                              ("connections", dict(connections=0)), ("temp_file_limit", dict(temp_file_limit="100MB'; DROP ROLE x; --")),
                              ("share", dict(max_table_share=0)), ("share", dict(max_table_share=1.5)),
                              ("share", dict(max_table_share="0.1); DROP ROLE x; --")), ("share", dict(max_table_share=True)),
                              ("prefix", dict(prefix="a'; DROP ROLE x; --")), ("database", dict(database="a'b"))):
            broker = Recorder()
            arguments = dict(database="agentdb_test_x", rows=1, size=1, hours=1)
            arguments.update(kwargs)
            with self.assertRaises(ValueError, msg=label):
                issue_login(broker, **arguments)
            self.assertEqual(broker.statements, [], label)
        with self.assertRaises(ValueError):
            retire_login(Recorder(), Recorder(), "agentdb_test_x", "x; DROP ROLE postgres")

    def test_retiring_ends_the_sessions_before_touching_the_budget_row_they_could_hold(self):
        order = []
        broker, keeper = mock.Mock(), mock.Mock()
        broker.sql.side_effect = lambda statement, database: order.append(("broker", statement))
        keeper.sql.side_effect = lambda statement, database: order.append(("keeper", statement))
        retire_login(broker, keeper, "agentdb_test_x", "agent_ab")
        self.assertEqual([who for who, _ in order], ["broker", "keeper", "broker"])
        self.assertIn("NOLOGIN", order[0][1])
        self.assertIn("pg_terminate_backend", order[1][1])
        self.assertIn("vc.stop_agent('agent_ab')", order[2][1])
        self.assertIn("DROP ROLE agent_ab;", order[2][1])

    def arguments(self, **kwargs):
        options = dict(command=["--", "agent", "--flag"], rows=5, bytes=100, hours=1.0, connections=4,
                       temp_file_limit="100MB", prefix="agent", agent_host=None, max_table_share=0.1, pass_env=[])
        options.update(kwargs)
        return argparse.Namespace(**options)

    def run_with(self, args, outcome):
        broker, keeper = Recorder(), Recorder()
        with mock.patch.object(agent_boundary, "connect", side_effect=lambda _, role: broker if role == "agentdb_broker" else keeper), \
                mock.patch("builtins.print"), mock.patch.object(agent_boundary, "keep") as self.rounds, \
                mock.patch.object(agent_boundary.subprocess, "run", **outcome) as started:
            try:
                code = agent_boundary.run(args)
            except OSError:
                code = None
        return code, started, broker, keeper

    def test_run_hands_the_password_over_only_in_the_environment_and_retires_the_login(self):
        code, started, broker, keeper = self.run_with(self.arguments(), dict(return_value=subprocess.CompletedProcess([], 7)))
        self.assertEqual(code, 7)
        command, environment = started.call_args.args[0], started.call_args.kwargs["env"]
        self.assertEqual(command, ["agent", "--flag"])
        self.assertTrue(environment["PGPASSWORD"])
        self.assertNotIn(environment["PGPASSWORD"], " ".join(command))
        self.assertEqual((environment["PGDATABASE"], environment["PGPORT"]), ("agentdb_test_x", "5432"))
        self.assertEqual(environment["PGHOST"], "127.0.0.1")  # a socket's peer rule cannot serve a password
        self.assertIn(f"DROP ROLE {environment['PGUSER']};", broker.statements[-1])
        self.assertIn("pg_terminate_backend", keeper.statements[-1])

    def test_the_operators_environment_stays_behind_unless_named(self):
        with mock.patch.dict(agent_boundary.os.environ, dict(PATH="/bin", FAKE_CLOUD_TOKEN="secret", FAKE_MODEL_KEY="key", PGPASSWORD="owner")):
            _, started, _, _ = self.run_with(self.arguments(pass_env=["FAKE_MODEL_KEY"]), dict(return_value=subprocess.CompletedProcess([], 0)))
        environment = started.call_args.kwargs["env"]
        self.assertEqual((environment["PATH"], environment["FAKE_MODEL_KEY"]), ("/bin", "key"))
        self.assertNotIn("FAKE_CLOUD_TOKEN", environment)
        self.assertNotEqual(environment["PGPASSWORD"], "owner")

    def test_the_keepers_rounds_run_as_long_as_the_command(self):
        seen = []
        def command(*_, **__):
            seen.append(self.rounds.call_args.args[1].is_set())
            return subprocess.CompletedProcess([], 0)
        self.run_with(self.arguments(), dict(side_effect=command))
        self.assertEqual(seen, [False])  # running while the command does
        self.assertTrue(self.rounds.call_args.args[1].is_set())  # told to stop after

    def test_run_retires_the_login_even_if_the_command_cannot_start(self):
        code, _, broker, _ = self.run_with(self.arguments(), dict(side_effect=OSError("no such program")))
        self.assertIsNone(code)
        self.assertIn("DROP ROLE", broker.statements[-1])

    def test_run_uses_the_address_the_sandbox_sees(self):
        _, started, _, _ = self.run_with(self.arguments(agent_host="host.docker.internal"),
                                         dict(return_value=subprocess.CompletedProcess([], 0)))
        self.assertEqual(started.call_args.kwargs["env"]["PGHOST"], "host.docker.internal")

    def test_run_needs_a_command(self):
        for command in ([], ["--"], ["agent"]):
            with self.assertRaises(ValueError):
                agent_boundary.run(self.arguments(command=command))


class KeeperConnection:
    """What vc.boundary_check, vc.end_stuck_agent_sessions and vc.history answer."""

    def __init__(self):
        self.statements, self.problems, self.frozen, self.ended, self.open_databases = [], [], None, [], []
        self.commits, self.refuse, self.logins = [], {}, []

    def sql(self, statement):
        self.statements.append(statement)
        if statement.startswith("SELECT vc.boundary_check()"):
            if self.problems and not self.frozen:
                self.frozen = "; ".join(self.problems)
            return json.dumps(dict(frozen=self.frozen, problems=self.problems, logins=self.logins, open_databases=self.open_databases))
        if statement.startswith("SELECT vc.end_stuck_agent_sessions"):
            ended, self.ended = self.ended, []
            return json.dumps(ended)
        if statement.startswith("SELECT vc.head('main')"):
            return self.commits[0]["id"]
        if "FROM vc.history(" in statement:
            start = next(i for i, c in enumerate(self.commits) if c["id"] in statement)
            return json.dumps(self.commits[start:start + 1000])
        return ""

    def execute(self, statement):
        self.statements.append(statement)
        revision = statement.split("'")[1]
        if revision in self.refuse:
            return subprocess.CompletedProcess(statement, 3, "", self.refuse[revision])
        self.commits.insert(0, dict(id=f"10000000-0000-0000-0000-{len(self.commits):012d}", parents=[self.commits[0]["id"]],
                                    author="keeper", message=f"revert {revision}"))
        return subprocess.CompletedProcess(statement, 0, self.commits[0]["id"], "")


def keeper_with(connection):
    engine = Engine(MemoryStore())
    return BoundaryKeeper(Runtime(engine), connection), engine


class BoundaryCheckTests(unittest.TestCase):
    def test_a_quiet_round_publishes_once_and_then_nothing(self):
        connection = KeeperConnection()
        keeper, engine = keeper_with(connection)
        first = keeper.check("keeper")
        self.assertEqual((first["published"], first["version"], first["frozen"]), (True, 0, None))
        state = engine.read("agent-boundary")["state"]
        self.assertEqual((state["status"], state["next_action"], state["uncertainties"]), ("completed", "", []))
        self.assertEqual(state["facts"]["thresholds"], dict(max_idle_in_transaction_seconds=60, max_transaction_seconds=300))
        again = keeper.check("keeper")
        self.assertEqual((again["published"], again["version"]), (False, 0))
        self.assertIn("SELECT vc.end_stuck_agent_sessions(60,300,false);", connection.statements)

    def test_a_problem_is_reported_and_frozen_transactions_end_at_once(self):
        connection = KeeperConnection()
        keeper, engine = keeper_with(connection)
        keeper.check("keeper")
        connection.problems = ["disabled trigger vc.row_change.immutable"]
        result = keeper.check("keeper")
        self.assertEqual((result["frozen"], result["version"]), ("disabled trigger vc.row_change.immutable", 1))
        state = engine.read("agent-boundary")["state"]
        self.assertEqual((state["status"], state["next_action"]), ("running", BOUNDARY_ACTION))
        self.assertEqual(state["uncertainties"], connection.problems)
        self.assertEqual(connection.statements[-1], "SELECT vc.end_stuck_agent_sessions(60,300,true);")
        # The cause gone, the database stays frozen until the owner opens it, and the report says so.
        connection.problems = []
        keeper.check("keeper")
        state = engine.read("agent-boundary")["state"]
        self.assertEqual((state["status"], state["next_action"], state["facts"]["problems_total"]), ("running", BOUNDARY_ACTION, 0))
        connection.frozen = None
        keeper.check("keeper")
        self.assertEqual(engine.read("agent-boundary")["state"]["status"], "completed")

    def test_many_problems_stay_within_the_state_limits_and_are_counted(self):
        connection = KeeperConnection()
        connection.problems = [f"problem {n} " + "x" * 2000 for n in range(40)]
        connection.open_databases = [f"database_{n}_" + "y" * 50 for n in range(40)]
        keeper, engine = keeper_with(connection)
        keeper.check("keeper")
        state = engine.read("agent-boundary")["state"]
        self.assertEqual(len(state["uncertainties"]), 16)
        self.assertTrue(all(len(item) <= 1024 for item in state["uncertainties"]))
        self.assertEqual(state["facts"]["problems_total"], 40)

    def test_ended_sessions_are_counted_across_rounds_and_only_the_latest_are_kept(self):
        connection = KeeperConnection()
        keeper, engine = keeper_with(connection)
        for _ in range(3):
            connection.ended = [dict(pid=n, login=f"agent_{n}", reason="idle in transaction") for n in range(10)]
            result = keeper.check("keeper")
            self.assertEqual(len(result["ended"]), 10)
        facts = engine.read("agent-boundary")["state"]["facts"]
        self.assertEqual((facts["ended_total"], len(facts["last_ended"])), (30, 20))

    def test_limits_come_from_the_tasks_policy_and_a_bad_policy_stops_it(self):
        connection = KeeperConnection()
        keeper, engine = keeper_with(connection)
        policy = engine.objects.put_bytes(json.dumps(dict(agent_boundary=dict(max_transaction_seconds=30))).encode())
        keeper.check("keeper", task_id="boundary-policy", policy_ref=policy)
        self.assertIn("SELECT vc.end_stuck_agent_sessions(60,30,false);", connection.statements)
        self.assertEqual(engine.read("boundary-policy")["state"]["facts"]["thresholds_source"], "policy")
        with self.assertRaises(Conflict):
            keeper.check("keeper", task_id="boundary-policy", policy_ref=engine.objects.put_bytes(b"other text"))
        keeper.check("keeper", task_id="boundary-text", policy_ref=engine.objects.put_bytes(b"protocol text"))
        self.assertEqual(engine.read("boundary-text")["state"]["facts"]["thresholds_source"], "default")
        for section in ([], dict(max_transaction_seconds=0), dict(max_transaction_seconds=True),
                        dict(max_transaction_seconds="30; DROP TABLE x"), dict(max_idle=5)):
            before = len(connection.statements)
            bad = engine.objects.put_bytes(json.dumps(dict(agent_boundary=section)).encode())
            with self.assertRaises(Invalid, msg=section):
                keeper.check("keeper", task_id="boundary-bad", policy_ref=bad)
            self.assertEqual(len(connection.statements), before, section)

    def test_a_task_belonging_to_something_else_is_refused(self):
        keeper, engine = keeper_with(KeeperConnection())
        engine.publish("taken", command_id="c", actor="a", state=dict(goal="other", status="running", facts=dict(operation="other")))
        with self.assertRaises(Conflict):
            keeper.check("keeper", task_id="taken")


class UndoTests(unittest.TestCase):
    def history(self):
        connection = KeeperConnection()
        connection.commits = [
            dict(id=M3, parents=[M2, "20000000-0000-0000-0000-000000000000"], author="agent_a", message=f"merge {BASE}"),
            dict(id=M2, parents=[M1], author="agent_b", message=f"merge {BASE}"),
            dict(id=M1, parents=[BASE], author="agent_a", message=f"merge {BASE}"),
            dict(id=BASE, parents=[], author="system", message="genesis")]
        return connection

    def test_undo_stops_the_login_then_reverts_only_its_commits_newest_first(self):
        connection = self.history()
        keeper, engine = keeper_with(connection)
        result = keeper.undo_login("keeper", "agent_a")
        self.assertEqual((result["reverted"], result["remaining"], result["refused"]), ([M3, M1], [], None))
        # Sessions end before the budget row they could hold, and again for one that reconnected.
        self.assertIn("pg_terminate_backend", connection.statements[0])
        self.assertEqual(connection.statements[1], "SELECT vc.stop_agent('agent_a');")
        self.assertIn("pg_terminate_backend", connection.statements[2])
        reverts = [s for s in connection.statements if s.startswith("SELECT vc.revert")]
        self.assertEqual(reverts, [f"SELECT vc.revert('{M3}');", f"SELECT vc.revert('{M1}');"])
        state = engine.read("undo-agent_a")["state"]
        self.assertEqual((state["status"], state["facts"]["reverted_total"]), ("completed", 2))

    def test_undo_stops_at_a_refusal_says_why_and_a_rerun_finishes(self):
        connection = self.history()
        connection.refuse[M1] = "ERROR:  AB005: A row changed after this commit"
        keeper, engine = keeper_with(connection)
        result = keeper.undo_login("keeper", "agent_a")
        self.assertEqual((result["reverted"], result["remaining"]), ([M3], [M1]))
        self.assertIn("AB005", result["refused"]["error"])
        state = engine.read("undo-agent_a")["state"]
        self.assertEqual(state["status"], "running")
        self.assertIn("rerun undo", state["next_action"])
        self.assertIn("AB005", state["uncertainties"][0])
        connection.refuse.clear()
        result = keeper.undo_login("keeper", "agent_a")
        self.assertEqual((sorted(result["reverted"]), result["remaining"], result["refused"]), (sorted([M3, M1]), [], None))
        self.assertEqual(len([s for s in connection.statements if s == f"SELECT vc.revert('{M3}');"]), 1)  # not twice
        self.assertEqual(engine.read("undo-agent_a")["state"]["status"], "completed")

    def test_only_an_exact_revert_message_counts_as_reverted(self):
        connection = self.history()
        connection.commits.insert(0, dict(id="30000000-0000-0000-0000-000000000000", parents=[M3], author="seed", message="revert "))
        connection.commits.insert(0, dict(id="30000000-0000-0000-0000-000000000001", parents=["30000000-0000-0000-0000-000000000000"],
                                          author="seed", message=f"revert {M1} please"))
        keeper, _ = keeper_with(connection)
        self.assertEqual(keeper.commits_by("agent_a"), ([M3, M1], []))

    def test_a_login_name_that_is_not_a_name_never_reaches_sql(self):
        connection = KeeperConnection()
        keeper, _ = keeper_with(connection)
        for login in ("agent'; DROP TABLE x; --", "", "Agent", None):
            with self.assertRaises(Invalid, msg=login):
                keeper.undo_login("keeper", login)
        self.assertEqual(connection.statements, [])


if __name__ == "__main__":
    unittest.main()
