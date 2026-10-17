"""agent_lab.py with and without the resident gateway, against a scripted worker."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import agent_lab  # noqa: E402
import agent_lab_gateway  # noqa: E402
from agent_lab_fake import BASE, DB, NEW, SCENARIOS, FakeWorker, last_line, run_cli  # noqa: E402

GOLDEN = json.loads((Path(__file__).resolve().parent / "agent_lab_golden.json").read_text(encoding="utf-8"))


class GatewayCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.config = self.folder / "connection.json"
        # Any existing file passes as psql: no process is started in these tests.
        self.config.write_text(json.dumps({"psql": sys.executable}), encoding="utf-8")
        self.rendezvous = self.folder / "gateway.json"
        self.workers = []

    def start(self, options=None, factory=None, **settings):
        def make(connection, database):
            worker = FakeWorker(**(options or {}))
            self.workers.append(worker)
            return worker
        gateway = agent_lab_gateway.Gateway(self.rendezvous, session_factory=factory or make, **settings)
        gateway.publish()
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()

        def stop():
            gateway.server.shutdown()
            thread.join(5)
        self.addCleanup(stop)
        return gateway, thread

    def client(self, argv):
        """agent_lab.main() as a separate process would behave, with the gateway configured."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with patch.dict(os.environ, {"AGENTDB_LAB_GATEWAY": str(self.rendezvous)}), \
                patch.object(sys, "argv", ["agent_lab.py", *argv, "--config", str(self.config)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                agent_lab.main()
            except SystemExit as stop:
                code = stop.code
            except Exception:
                traceback.print_exc()
                code = 1
        return dict(exit_code=code, stdout=out.getvalue(), stderr=err.getvalue())

    def post(self, gateway, body, token=None):
        from http.client import HTTPConnection
        client = HTTPConnection("127.0.0.1", gateway.port, timeout=10)
        client.request("POST", "/v1/run", json.dumps(body).encode(),
                       {"Authorization": "Bearer " + (token or gateway.token), "Content-Type": "application/json"})
        response = client.getresponse()
        value = response.status, json.loads(response.read())
        client.close()
        return value

    def fingerprint(self):
        args = agent_lab.build_parser().parse_args(["info", "--database", DB, "--config", str(self.config)])
        return agent_lab_gateway.fingerprint(agent_lab.connect(args, DB, "agentdb_client"))

    def assertMatchesGolden(self, name, result, statements):
        expected = GOLDEN[name]
        self.assertEqual(result["exit_code"], expected["exit_code"], (name, result))
        self.assertEqual(result["stdout"], expected["stdout"], name)
        self.assertEqual(statements, expected["statements"], name)
        if expected["exit_code"] == 1:
            self.assertEqual(last_line(result["stderr"]), expected["stderr_last_line"], name)
        else:
            self.assertEqual(result["stderr"], expected["stderr"], name)


class LocalPathTests(GatewayCase):
    def test_without_gateway_output_and_sql_match_recorded_baseline(self):
        with patch.dict(os.environ):
            os.environ.pop("AGENTDB_LAB_GATEWAY", None)
            for name, (argv, options) in SCENARIOS.items():
                worker = FakeWorker(**options)
                result = run_cli(agent_lab, argv, worker)
                self.assertMatchesGolden(name, result, worker.statements)

    def test_configured_but_not_running_gateway_runs_locally(self):
        with patch.dict(os.environ, {"AGENTDB_LAB_GATEWAY": str(self.rendezvous)}):
            for name in ("info", "merge_stale", "verify_fail"):
                argv, options = SCENARIOS[name]
                worker = FakeWorker(**options)
                self.assertMatchesGolden(name, run_cli(agent_lab, argv, worker), worker.statements)

    def test_stale_rendezvous_file_with_refused_port_runs_locally(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.rendezvous.write_text(json.dumps(dict(host="127.0.0.1", port=port, token="x")), encoding="utf-8")
        with patch.dict(os.environ, {"AGENTDB_LAB_GATEWAY": str(self.rendezvous)}):
            argv, options = SCENARIOS["info"]
            worker = FakeWorker(**options)
            with patch.object(agent_lab, "connect", lambda *a, **k: worker), \
                    patch.object(agent_lab_gateway, "fingerprint", lambda connection: {}):
                result = run_cli(agent_lab, argv, worker)
        self.assertMatchesGolden("info", result, worker.statements)


class GatewayTests(GatewayCase):
    def test_every_scenario_matches_baseline_through_gateway(self):
        for name, (argv, options) in SCENARIOS.items():
            with self.subTest(name):
                self.workers, self.rendezvous = [], self.folder / f"{name}.json"
                gateway, _ = self.start(options)
                result = self.client(argv)
                statements = [s for worker in self.workers for s in worker.statements]
                self.assertMatchesGolden(name, result, statements)
                # Argument errors stop in the client; only requests that pass reach the gateway.
                self.assertEqual(gateway.health()["requests"], 1 if self.workers else 0)

    def test_gateway_revalidates_requests_and_never_runs_rejected_ones(self):
        gateway, _ = self.start()
        mine = self.fingerprint()
        status, value = self.post(gateway, dict(argv=["info", "--database", "agentdb"], cwd=str(self.folder), fingerprint=mine))
        self.assertEqual((status, value["exit_code"]), (200, 2))
        self.assertIn("agentdb_test_gpt_<10 hex> database is required", value["stderr"])
        status, value = self.post(gateway, dict(argv=["merge", "--database", DB, "--config", str(self.config),
                                                      "--revision", NEW, "--expected", BASE],
                                                cwd=str(self.folder), fingerprint=mine))
        self.assertEqual((status, value["exit_code"]), (200, 2))
        self.assertTrue(value["stderr"].endswith("agent_lab.py: error: merge requires actor\n"))
        status, value = self.post(gateway, dict(argv=["init"], cwd=str(self.folder), fingerprint=mine))
        self.assertEqual((status, value["error"]), (400, "invalid_request"))
        status, value = self.post(gateway, dict(argv=["info", "--database", DB, "-h"], cwd=str(self.folder), fingerprint=mine))
        self.assertEqual(value["exit_code"], 2)
        info = ["info", "--database", DB, "--config", str(self.config)]
        status, value = self.post(gateway, dict(argv=info, cwd=str(self.folder), fingerprint=mine), token="wrong")
        self.assertEqual((status, value["error"]), (401, "unauthorized"))
        other = dict(mine, settings=dict(mine["settings"], port=5433))
        status, value = self.post(gateway, dict(argv=info, cwd=str(self.folder), fingerprint=other))
        self.assertEqual((status, value["error"]), (409, "gateway_mismatch"))
        self.assertEqual(self.workers, [])
        # A relative --config resolves against the client's directory, as it would locally.
        status, value = self.post(gateway, dict(argv=["info", "--database", DB, "--config", self.config.name],
                                                cwd=str(self.folder), fingerprint=mine))
        self.assertEqual((status, value["exit_code"]), (200, 0))

    def test_concurrent_agents_get_separate_sessions_then_reuse_them(self):
        both, release = threading.Barrier(2, timeout=10), threading.Event()

        class Blocking(FakeWorker):
            def sql(self, statement, database=None):
                if statement == "SELECT vc.head();" and not release.is_set():
                    both.wait()  # Both calls are inside the gateway at once.
                return super().sql(statement, database)

        def make(connection, database):
            self.workers.append(Blocking())
            return self.workers[-1]
        gateway, _ = self.start(factory=make)
        results = []
        threads = [threading.Thread(target=lambda: results.append(agent_lab_gateway.run(
            self.rendezvous, ["info", "--database", DB, "--config", str(self.config)], self.connection())))
            for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        release.set()
        self.assertEqual([r[2] for r in results], [0, 0])
        for _ in range(3):
            self.assertEqual(self.client(["info", "--database", DB])["exit_code"], 0)
        self.assertEqual(gateway.health()["sessions"], dict(open=2, idle=2, created=2))

    def test_busy_gateway_refuses_without_running_or_queueing(self):
        entered, release = threading.Event(), threading.Event()

        class Blocking(FakeWorker):
            def sql(self, statement, database=None):
                entered.set()
                release.wait(10)
                return super().sql(statement, database)

        gateway, _ = self.start(factory=lambda c, d: self.workers.append(Blocking()) or self.workers[-1], max_sessions=1)
        first = threading.Thread(target=lambda: agent_lab_gateway.run(
            self.rendezvous, ["info", "--database", DB, "--config", str(self.config)], self.connection()))
        first.start()
        self.assertTrue(entered.wait(10))
        result = self.client(["info", "--database", DB])
        release.set()
        first.join(10)
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(result["stderr"].startswith("gateway_busy: "), result)
        self.assertIn("nothing was executed", result["stderr"])
        self.assertEqual(len(self.workers), 1)

    def test_interrupted_request_is_reported_unknown_and_not_retried(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        accepted = []

        def drop():
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                accepted.append(connection.recv(65536))
                connection.close()  # The request arrived; no answer comes back.
        threading.Thread(target=drop, daemon=True).start()
        self.rendezvous.write_text(json.dumps(dict(host="127.0.0.1", port=listener.getsockname()[1], token="t")),
                                   encoding="utf-8")
        result = self.client(["merge", "--database", DB, "--revision", NEW, "--expected", BASE, "--actor", "gpt-a"])
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(result["stderr"].startswith("gateway_outcome_unknown: "), result)
        self.assertEqual(result["stdout"], "")
        time.sleep(0.2)
        self.assertEqual(len(accepted), 1)

    def test_idle_sessions_close_and_gateway_exits_removing_its_file(self):
        now = [0.0]
        gateway, thread = self.start(idle_timeout=1, clock=lambda: now[0])
        self.assertEqual(self.client(["info", "--database", DB])["exit_code"], 0)
        self.assertEqual(gateway.health()["sessions"]["open"], 1)
        now[0] += 2
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.workers[0].closed)
        self.assertFalse(self.rendezvous.exists())
        self.assertEqual(gateway.health()["sessions"]["open"], 0)
        # Gone gateway: the next call takes agent_lab's own path again.
        argv, options = SCENARIOS["info"]
        worker = FakeWorker(**options)
        with patch.dict(os.environ, {"AGENTDB_LAB_GATEWAY": str(self.rendezvous)}):
            self.assertMatchesGolden("info", run_cli(agent_lab, argv, worker), worker.statements)

    def test_second_gateway_does_not_take_over_a_rendezvous_file(self):
        self.start()
        second = agent_lab_gateway.Gateway(self.rendezvous, session_factory=lambda c, d: FakeWorker())
        with self.assertRaises(FileExistsError):
            second.publish()
        second.close()
        self.assertTrue(self.rendezvous.exists())

    def test_limits_are_bounded(self):
        for options in (dict(idle_timeout=0), dict(idle_timeout=3601), dict(max_sessions=0), dict(max_sessions=33)):
            with self.assertRaises(ValueError):
                agent_lab_gateway.Gateway(self.rendezvous, **options)

    def connection(self):
        args = agent_lab.build_parser().parse_args(["info", "--database", DB, "--config", str(self.config)])
        return agent_lab.connect(args, DB, "agentdb_client")


if __name__ == "__main__":
    unittest.main()
