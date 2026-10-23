"""Tests for run_archive_trials.py, the MCP server's --tool-file, and a dry run with fake_claude.py.

  python -m unittest discover -s experiments/context_archive -v

The dry run (DryRunTests) starts the runner with the stand-in for one round of each setup on
the existing PostgreSQL server: real MCP server, coordinator, agent_lab.py and archive, no model.
It takes a few minutes and drops the round databases it made.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import archive
import run_archive_trials as runner
from db import Connection

sys.path.insert(0, str(runner.GPT_REPEAT))
import agentdb_mcp_server  # noqa: E402
from codex_rpc import wire  # noqa: E402

REVISIONS = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(1, 4)]
# tools/list reply of agentdb_mcp_server.py at 48b01d8, before --tool-file existed
TOOLS_LIST_SHA256 = "1e7c30e04210f42b7041268ba1517b5bacaac61ae8f27d25d501666054f5ab78"


class AuditLinesTests(unittest.TestCase):
    def test_exact_table(self):
        code = "# 00000001: 118\n# 00000002: 104\n# 00000003: 111\nSTOCK = 95\n"
        self.assertEqual(runner.audit_lines(code, REVISIONS, (118, 104, 111)),
                         dict(exact=True, all_correct=True, correct=3, wrong=0, missing=0))

    def test_values_are_scored_apart_from_layout(self):
        code = "# Stock audit\n# 00000003: 111\n# 00000001: 118\n# 00000002: 104\nSTOCK = 95\n"
        result = runner.audit_lines(code, REVISIONS, (118, 104, 111))
        self.assertEqual((result["exact"], result["all_correct"]), (False, True))

    def test_wrong_and_missing_values(self):
        code = "# 00000001: 118\n# 00000002: 140\n# 00000001: 117\nSTOCK = 95\n"
        self.assertEqual(runner.audit_lines(code, REVISIONS, (118, 104, 111)),
                         dict(exact=False, all_correct=False, correct=0, wrong=2, missing=1))
        self.assertEqual(runner.audit_lines(None, REVISIONS, (118, 104, 111))["missing"], 3)


def call(sequence, action, error=None, refused=None, **arguments):
    record = {"sequence": sequence, "actor": "gpt-b", "arguments": {"action": action, **arguments}, "error": error}
    if refused:
        record["refused"] = refused
    return record


class SourcesTests(unittest.TestCase):
    def test_protocol_hashes_every_project_module_the_runner_loads(self):
        listing = ("import json, sys; from pathlib import Path; import run_archive_trials as r; "
                   "root = r.archive.ROOT; print(json.dumps(sorted(Path(m.__file__).resolve().relative_to(root).as_posix() "
                   "for m in list(sys.modules.values()) if getattr(m, '__file__', None) "
                   "and Path(m.__file__).resolve().is_relative_to(root))))")
        loaded = json.loads(subprocess.run([sys.executable, "-c", listing], cwd=archive.HERE, capture_output=True,
                                           text=True, check=True).stdout)
        self.assertIn("agent_state/objects.py", loaded)
        self.assertEqual([path for path in loaded if path not in runner.SOURCES], [])

    def test_protocol_hashes_every_sql_file_init_installs(self):
        installed = sorted(p.relative_to(archive.ROOT).as_posix() for p in (archive.ROOT / "sql").glob("*.sql"))
        self.assertEqual(len(installed), 9)
        self.assertEqual([path for path in installed if path not in runner.SOURCES], [])


class AfterSwitchTests(unittest.TestCase):
    def test_counts_repeats_of_fixed_reads_and_failures_after_the_reset(self):
        calls = [call(1, "info"), call(2, "info", revision=REVISIONS[0]), call(3, "info", revision=REVISIONS[1]),
                 call(4, "info", refused="context_switch", error="context_switch", revision=REVISIONS[2]),
                 call(5, "fetch", entries=[1, 5]), call(6, "info", revision=REVISIONS[0]), call(7, "info"),
                 call(8, "merge", error="stale_head", revision=REVISIONS[1], expected=REVISIONS[0]),
                 call(9, "read-code", error="not_found", revision=REVISIONS[2], path="experiments/inventory.py"),
                 call(10, "fetch", entries=[1, 5])]
        summary = {"switches": [{"actor": "gpt-b", "call_sequence": 3}], "calls": calls}
        self.assertEqual(runner.after_switch(summary, "gpt-b"),
                         dict(calls=6, repeats=2, failed=1, fetches=2, refused_while_switching=1, primary=3))
        self.assertIsNone(runner.after_switch(summary, "gpt-a"))


def stream(*events):
    return b"".join(wire(e) for e in events)


def result_event(input_tokens, output_tokens):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "done",
            "modelUsage": {"m": {"inputTokens": input_tokens, "outputTokens": output_tokens,
                                 "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "thinkingTokens": 0}}}


def process(folder, actor, turns, dangling=False):
    """Minimal logs trace_metrics reads for one process: one tool call per turn, answered unless dangling."""
    folder.mkdir(parents=True, exist_ok=True)
    out, prompts, mcp = [{"type": "system", "subtype": "init", "model": "m", "tools": ["t"]}], [], []
    for n, (tokens_in, tokens_out) in enumerate(turns, 1):
        prompts.append({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": f"turn {n}"}]}})
        out.append({"type": "assistant", "parent_tool_use_id": None, "message": {
            "id": f"msg{n}", "model": "m", "content": [{"type": "tool_use", "id": f"tu{n}", "name": "t", "input": {}}]}})
        mcp.append({"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"_meta": {"claudecode/toolUseId": f"tu{n}"}}})
        mcp.append({"jsonrpc": "2.0", "id": n, "result": {}})
        if not (dangling and n == len(turns)):
            out.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"tu{n}"}]}})
        out.append(result_event(tokens_in, tokens_out))
    (folder / f"{actor}.stdout.jsonl").write_bytes(stream(*out))
    (folder / f"{actor}.stdin.jsonl").write_bytes(stream(*prompts))
    (folder / f"tool_calls.{actor}.jsonl").write_bytes(stream(*mcp))


class MetricsTests(unittest.TestCase):
    def test_combines_processes_and_separates_calls_cut_off_by_a_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp)
            process(raw / "ctx-1", "gpt-a", [(100, 10)])
            process(raw / "ctx-1", "gpt-b", [(200, 20), (300, 30)], dangling=True)
            process(raw / "ctx-2", "gpt-b", [(50, 5)], dangling=True)
            metrics = runner.combined_metrics(raw, {"ctx-1": ["gpt-a", "gpt-b"], "ctx-2": ["gpt-b"]}, "m",
                                              {("ctx-1", "gpt-b")})
            self.assertEqual(metrics["tool_calls"], 4)
            self.assertEqual(metrics["abandoned_at_reset"], ["ctx-1/gpt-b:tu2"])
            self.assertEqual(metrics["unanswered_tool_request_ids"], ["ctx-2/gpt-b:tu1"])
            self.assertTrue(metrics["model_protocol_match"])
            self.assertEqual(metrics["tokens_total"]["inputTokens"], 100 + 300 + 50)

    def test_tokens_after_a_reset_are_the_later_process_plus_setup_a_note_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp)
            process(raw / "ctx-1", "gpt-b", [(200, 20), (260, 26), (300, 30)])  # cumulative per process
            process(raw / "ctx-2", "gpt-b", [(50, 5), (90, 9)])
            self.assertEqual([u["inputTokens"] for u in runner.turn_usages(raw / "ctx-1", "gpt-b")], [200, 60, 40])
            a = runner.usage_after_switch(raw, "gpt-b", "A", ["ctx-1", "ctx-2"])
            b = runner.usage_after_switch(raw, "gpt-b", "B", ["ctx-1", "ctx-2"])
            self.assertEqual((a["inputTokens"], a["outputTokens"]), (40 + 50 + 40, 4 + 5 + 4))
            self.assertEqual((b["inputTokens"], b["outputTokens"]), (50 + 40, 5 + 4))
            self.assertIsNone(runner.usage_after_switch(raw, "gpt-b", "B", ["ctx-1"]))


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.summary = {"calls": []}

    def tearDown(self):
        self.temp.cleanup()

    def trial(self, setup, rows=()):
        trial = runner.ArchiveTrial(None, Path(self.temp.name), self.summary, setup)
        trial.fetch_sql = lambda statement: json.dumps([[s, k, l, b.hex()] for s, k, l, b in rows])
        return trial

    def handle(self, trial, **arguments):
        return trial.handle("gpt-b", {"name": "agentdb", "arguments": arguments})

    def test_refuses_calls_while_the_process_is_replaced_without_counting_them(self):
        trial = self.trial("A")
        trial.switching.add("gpt-b")
        self.assertEqual(self.handle(trial, action="info")["error"], "context_switch")
        self.assertEqual((self.summary["calls"][0]["refused"], trial.call_counts["gpt-b"]), ("context_switch", 0))

    def test_budget_is_sixty_calls(self):
        trial = self.trial("B")
        trial.call_counts["gpt-b"] = runner.CALL_BUDGET
        self.assertEqual(self.handle(trial, action="info")["error"], "tool_budget_exhausted")
        self.assertEqual(self.summary["calls"][0]["error"], "tool_budget_exhausted")

    def reset(self, trial):
        trial.fetching.add("gpt-b")  # what switch() does in setup B once the fresh process runs
        return trial

    def test_fetch_exists_only_in_setup_b_after_the_reset(self):
        for trial in (self.trial("A"), self.trial("B")):  # before the reset B's tool is A's
            self.assertEqual(self.handle(trial, action="fetch", entries=[1])["error"], "invalid_arguments")
            self.assertEqual(self.handle(trial, action="fetch")["error"], "unknown_action")
        self.assertEqual(self.handle(self.reset(self.trial("B")), action="info", entries=[1])["error"], "invalid_arguments")

    def test_fetch_returns_stored_entries_and_refuses_bad_numbers(self):
        body = wire({"type": "user", "message": {"content": [{"type": "text", "text": "the prompt"}]}}).rstrip(b"\n")
        trial = self.reset(self.trial("B", rows=[(1, "prompt", "prompt: the prompt", body)]))
        reply = self.handle(trial, action="fetch", entries=[1])
        self.assertEqual(reply["result"]["entries"], [{"entry": 1, "kind": "prompt", "content": {"type": "text", "text": "the prompt"}}])
        self.assertEqual(self.handle(trial, action="fetch", entries=["1"])["error"], "invalid_entries")
        # Beyond PostgreSQL's integer range: refused before the query, and recorded as a failed call.
        self.assertEqual(self.handle(trial, action="fetch", entries=[3000000000])["error"], "invalid_entries")
        self.assertEqual(self.handle(self.reset(self.trial("B")), action="fetch", entries=[2])["error"], "unknown_entry")
        self.assertEqual([c.get("error") for c in self.summary["calls"]],
                         [None, "invalid_entries", "invalid_entries", "unknown_entry"])


class NullSession:
    def sql(self, statement):
        return "1"

    def close(self):
        pass


class CaptureFailureTests(unittest.TestCase):
    def test_a_line_the_capture_cannot_read_stops_the_round_but_not_the_log(self):
        lines = [{"type": "user", "message": {"content": ["not a block"]}}, {"type": "system", "subtype": "init"}]
        script = "import sys; sys.stdout.write(%r)" % "".join(json.dumps(line) + "\n" for line in lines)
        with tempfile.TemporaryDirectory() as temp:
            writer = archive.Writer(NullSession)
            agent = runner.ArchivingAgent([sys.executable, "-c", script], None, temp, temp, "gpt-b", writer, runner.Count())
            with self.assertRaisesRegex(archive.ArchiveError, "capture failed: AttributeError"):
                agent.pump(30)
            agent.close()
            writer.close()
            logged = (Path(temp) / "gpt-b.stdout.jsonl").read_bytes().splitlines()
            self.assertEqual([json.loads(line) for line in logged], lines)  # the reader kept logging


# A process that answers an interrupt only after argv[1] seconds and each prompt with a note.
SLOW_INTERRUPT = """
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "control_request":
        time.sleep(float(sys.argv[1]))
        reply = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Interrupted"}
    elif message.get("type") == "user":
        reply = {"type": "result", "subtype": "success", "is_error": False,
                 "result": "note for " + message["message"]["content"][0]["text"]}
    else:
        continue
    print(json.dumps(reply), flush=True)
"""


class InterruptTests(unittest.TestCase):
    def agent(self, delay):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)  # cleanups run last-in first-out: the agent closes its logs first
        writer = archive.Writer(NullSession)
        self.addCleanup(writer.close)
        agent = runner.ArchivingAgent([sys.executable, "-c", SLOW_INTERRUPT, str(delay)], None, directory.name,
                                      directory.name, "gpt-b", writer, runner.Count(), limit=None)
        self.addCleanup(agent.close)
        return agent

    def test_the_note_turn_reads_its_own_result_after_a_slow_interrupt(self):
        agent = self.agent(1.5)
        agent.interrupt(10)
        kind, event = agent.turn("write the note", 30, switching=False)
        self.assertEqual((kind, event["result"]), ("result", "note for write the note"))

    def test_an_interrupt_that_is_not_answered_stops_the_switch(self):
        agent = self.agent(3)
        with self.assertRaisesRegex(TimeoutError, "Interrupt not answered within 1s"):
            agent.interrupt(1)


class FailedCallTests(unittest.TestCase):
    """A call that raises gets the coordinator's reply (error type and text) as its recorded outcome."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.summary = {"calls": [], "switches": [{"actor": "gpt-b", "call_sequence": 0}]}
        self.trial = runner.ArchiveTrial(None, Path(self.temp.name), self.summary, "B")
        self.trial.fetching.add("gpt-b")

    def call(self, **arguments):
        with self.assertRaises(Exception) as raised:
            self.trial.handle("gpt-b", {"name": "agentdb", "arguments": arguments})
        return raised.exception

    def test_a_failed_archive_read_counts_as_a_failed_call(self):
        def broken(statement):
            raise RuntimeError("ERROR:  57P01: terminating connection")
        self.trial.fetch_sql = broken
        error = self.call(action="fetch", entries=[1])
        record = self.summary["calls"][-1]
        self.assertEqual((record["error"], record["detail"]), ("RuntimeError", str(error)))
        self.assertEqual(runner.after_switch(self.summary, "gpt-b")["failed"], 1)

    def test_an_agent_lab_call_that_raises_counts_as_a_failed_call(self):
        timeout = subprocess.TimeoutExpired("agent_lab.py", 120)
        with mock.patch.object(sys.modules["run_claude_repeats"], "run_helper", side_effect=timeout):
            self.call(action="info")
        self.assertEqual(self.summary["calls"][-1]["error"], "TimeoutExpired")
        self.assertEqual(runner.after_switch(self.summary, "gpt-b")["failed"], 1)


class NoteTurnTests(unittest.TestCase):
    """Setup A's switch with stand-ins for the agent processes: the note turn's outcome decides."""

    class Agent:
        trigger = SimpleNamespace(crossed=10500)
        process = SimpleNamespace(pid=1)

        def __init__(self, note_event=None):
            self.note_event = note_event

        def interrupt(self, timeout):
            pass

        def turn(self, prompt, timeout, switching=True):
            return "result", self.note_event

        def close(self):
            pass

    def switch(self, note_event):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.summary = {"calls": [], "switches": [], "agents": {"gpt-b": {"processes": []}}}
        trial = runner.ArchiveTrial(None, self.folder, self.summary, "A")
        rnd = SimpleNamespace(contexts={"gpt-b": ["ctx-1"]}, switched=set(), writer=SimpleNamespace(flush=lambda: None),
                              start=lambda actor, limit=None, fetch=False: self.Agent())
        return runner.switch(SimpleNamespace(turn_timeout=60), trial, rnd, "gpt-b", self.Agent(note_event), 27)

    def test_the_note_is_handed_over(self):
        fresh, handoff = self.switch({"type": "result", "subtype": "success", "is_error": False, "result": "the note"})
        self.assertEqual(handoff, archive.HEADER_A + "the note\n" + archive.CONTINUE)
        self.assertTrue(self.summary["switches"][0]["note_turn_ok"])

    def test_a_failed_note_turn_stops_the_round(self):
        failed = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error: 529"}
        with self.assertRaisesRegex(RuntimeError, "note_turn_failed: error_during_execution: API Error: 529"):
            self.switch(failed)
        self.assertFalse(self.summary["switches"][0]["note_turn_ok"])
        self.assertEqual(list(self.folder.glob("handoff.*")), [])  # nothing was handed over


class McpToolTests(unittest.TestCase):
    def tools_list(self, *extra):
        process = subprocess.run([sys.executable, str(runner.GPT_REPEAT / "agentdb_mcp_server.py"), "--port", "1",
                                  "--token", "t", "--raw", self.temp, "--actor", "gpt-b", *extra],
                                 input=wire({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), capture_output=True, timeout=60)
        return process.stdout

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.temp = self.directory.name

    def tearDown(self):
        self.directory.cleanup()

    def test_without_a_tool_file_the_reply_is_byte_for_byte_the_old_one(self):
        reply = wire({"jsonrpc": "2.0", "id": 1, "result": agentdb_mcp_server.respond({"method": "tools/list", "id": 1}, 0, "", 1)})
        self.assertEqual(hashlib.sha256(reply).hexdigest(), TOOLS_LIST_SHA256)
        self.assertEqual(self.tools_list(), reply)

    def test_a_tool_file_replaces_the_tool(self):
        path = Path(self.temp) / "tool.json"
        path.write_text(json.dumps(runner.TOOL_B), encoding="utf-8")
        tool = json.loads(self.tools_list("--tool-file", str(path)))["result"]["tools"][0]
        self.assertEqual(tool, {k: runner.TOOL_B[k] for k in ("name", "description", "inputSchema")})
        self.assertIn("fetch", tool["inputSchema"]["properties"]["action"]["enum"])


class DryRunTests(unittest.TestCase):
    """One round of each setup with fake_claude.py: the switch, interrupt, handoff, fetch and scoring for real."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(cls.directory.cleanup)
        temp = Path(cls.directory.name)
        cls.output = temp / "out"
        # Registered before the run (class cleanups run last-in first-out, before the folder goes), so the
        # round databases are dropped even if the run fails or times out.
        cls.addClassCleanup(cls.drop_round_databases)
        cls.completed = subprocess.run([sys.executable, str(archive.HERE / "run_archive_trials.py"),
                                  "--claude", str(archive.HERE / "fake_claude.py"), "--config-dir", str(temp / "config"),
                                  "--workdir", str(temp / "work"), "--output", str(cls.output),
                                  "--runs-per-setup", "1", "--pilot", "--workers", "2", "--turn-timeout", "300"],
                                 capture_output=True, text=True, timeout=1200)
        cls.rounds = {}
        for path in sorted(cls.output.glob("run-*/result.json")):
            result = json.loads(path.read_text(encoding="utf-8"))
            cls.rounds[result["setup"]] = result

    @classmethod
    def drop_round_databases(cls):
        maintenance = Connection(argparse.Namespace(config=None, password=False, role=None, database=None))
        for path in cls.output.glob("run-*/result.json"):
            name = json.loads(path.read_text(encoding="utf-8")).get("database")
            if name:
                maintenance.sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);', maintenance.settings["maintenance_database"])

    def test_both_rounds_complete_with_one_reset_during_the_audit(self):
        self.assertEqual(self.completed.returncode, 0, self.completed.stdout[-3000:] + self.completed.stderr[-3000:])
        self.assertEqual(sorted(self.rounds), ["A", "B"])
        for setup, result in self.rounds.items():
            self.assertTrue(result["success"], (setup, result.get("error"), result.get("audit_table")))
            (reset,) = result["switches"]
            self.assertEqual((reset["actor"], reset["phase"]), ("gpt-b", "prepare_inventory"))
            self.assertGreater(reset["request_tokens"], archive.LIMIT_TOKENS)
            self.assertEqual(result["metrics"]["processes"], {"ctx-1": ["gpt-a", "gpt-b"], "ctx-2": ["gpt-b"]})
            self.assertEqual(result["metrics"]["tool_calls"], len(result["calls"]))
            self.assertTrue(result["audit_table"]["all_correct"])
            self.assertIn("gpt-b", result["tokens_after_switch"])
            # Only setup B's process after the reset is given the fetch tool.
            folder = self.output / f"run-{result['run']:02d}"
            for name in ("gpt-a.ctx-1", "gpt-b.ctx-1", "gpt-b.ctx-2"):
                server = json.loads((folder / f"{name}.mcp-config.json").read_text(encoding="utf-8"))["mcpServers"]["agentdb"]
                self.assertEqual("--tool-file" in server["args"], setup == "B" and name == "gpt-b.ctx-2", (setup, name))

    def test_setup_a_hands_over_its_note_and_setup_b_fetches_from_the_archive(self):
        a, b = self.rounds["A"], self.rounds["B"]
        self.assertGreater(a["switches"][0]["note_bytes"], 0)
        self.assertEqual(a["after_switch"]["gpt-b"]["fetches"], 0)
        self.assertGreaterEqual(b["after_switch"]["gpt-b"]["fetches"], 1)
        handoff = (self.output / f"run-{b['run']:02d}" / "handoff.gpt-b.1.txt").read_text(encoding="utf-8")
        self.assertTrue(handoff.startswith(archive.HEADER_B) and handoff.endswith(archive.CONTINUE))
        self.assertLessEqual(len(handoff.encode("utf-8")), archive.HANDOFF_BYTES)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual({s: summary["by_setup"][s]["successes"] for s in "AB"}, {"A": 1, "B": 1})


if __name__ == "__main__":
    unittest.main()
