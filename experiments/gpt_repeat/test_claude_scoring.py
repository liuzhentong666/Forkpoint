import json
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest

from claude_scoring import trace_metrics
from codex_rpc import wire
from run_claude_repeats import CLAUDE_INSTRUCTIONS
from run_gpt_repeats import INSTRUCTIONS, TOOL

HERE = Path(__file__).resolve().parent


def assistant(mid, model, usage, *blocks):
    return {"type": "assistant", "parent_tool_use_id": None,
            "message": {"id": mid, "model": model, "usage": usage, "content": list(blocks)}}


class ClaudeScoringTests(unittest.TestCase):
    def test_usage_is_last_cumulative_result_and_reroute_detected(self):
        snapshot = {"input_tokens": 2, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 1}
        tool = {"type": "tool_use", "id": "t1", "name": "mcp__agentdb__agentdb", "input": {"action": "info"}}
        usage = lambda n: {"m": {"inputTokens": n, "cacheReadInputTokens": 10 * n, "cacheCreationInputTokens": 5,
                                 "outputTokens": 3 * n, "thinkingTokens": n, "costUSD": 0.5}}
        events = [{"type": "system", "subtype": "init", "model": "m"},
                  assistant("a", "m", snapshot, {"type": "thinking", "thinking": ""}),
                  assistant("a", "m", snapshot, tool),  # same response, second content block
                  {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
                  {"type": "result", "subtype": "success", "modelUsage": usage(2), "total_cost_usd": 0.5},
                  {"type": "system", "subtype": "init", "model": "m"},
                  assistant("b", "other", snapshot, {"type": "text", "text": "done"}),
                  {"type": "result", "subtype": "success", "modelUsage": usage(4), "total_cost_usd": 0.9}]
        prompt = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "x.stdout.jsonl").write_bytes(b"".join(map(wire, events)))
            (root / "x.stdin.jsonl").write_bytes(wire(prompt) * 2)
            request = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "agentdb", "arguments": {}, "_meta": {"claudecode/toolUseId": "t1"}}}
            (root / "tool_calls.x.jsonl").write_bytes(wire(request) + wire({"jsonrpc": "2.0", "id": 3, "result": {}}))
            measured = trace_metrics(root, ["x"], "m")
            self.assertEqual(measured["api_responses_by_agent"], {"x": 2})
            # Only the last cumulative modelUsage counts, never the mid-stream snapshots.
            self.assertEqual(measured["tokens_total"], {"inputTokens": 49, "cachedInputTokens": 40, "cacheWriteInputTokens": 5,
                                                        "outputTokens": 12, "reasoningOutputTokens": 4, "totalTokens": 61})
            self.assertEqual(measured["list_cost_usd"], 0.9)
            self.assertEqual(measured["model_reroutes"], [{"actor": "x", "models": ["m", "other"]}])
            self.assertEqual((measured["tool_calls"], measured["unanswered_tool_request_ids"]), (1, []))
            self.assertEqual(measured["mcp_calls_without_stream_tool_use"], {})
            (root / "tool_calls.x.jsonl").write_bytes(wire(request))
            self.assertEqual(trace_metrics(root, ["x"], "m")["unanswered_tool_request_ids"], ["x:3"])
            (root / "x.stdin.jsonl").write_bytes(wire(prompt) * 3)  # a third turn without a result event
            partial = trace_metrics(root, ["x"], "m")
            self.assertIsNone(partial["tokens_total"])
            self.assertEqual(partial["observed_tokens_total"]["totalTokens"], 61)

    def test_instructions_differ_only_in_the_model_family(self):
        self.assertEqual(CLAUDE_INSTRUCTIONS.replace("real Claude agent", "real GPT agent"), INSTRUCTIONS)

    def test_mcp_server_exposes_the_same_tool_and_logs_raw_bytes(self):
        seen = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                message = json.loads(self.rfile.readline())
                seen.append(message)
                self.wfile.write(wire({"result": {"exit_code": 2, "error": "stale_head"}}))

        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as temp:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            process = subprocess.Popen([sys.executable, str(HERE / "agentdb_mcp_server.py"), "--port", str(server.server_address[1]),
                                        "--token", "secret", "--raw", temp, "--actor", "gpt-b"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "agentdb", "arguments": {"action": "merge"}}},
                     {"jsonrpc": "2.0", "id": 4, "method": "resources/list"}]
            sent = b"".join(map(wire, lines))
            stdout, _ = process.communicate(sent, timeout=60)
            server.shutdown()
            replies = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4])
            tool = replies[1]["result"]["tools"][0]
            self.assertEqual((tool["name"], tool["description"], tool["inputSchema"]),
                             (TOOL["name"], TOOL["description"], TOOL["inputSchema"]))
            self.assertTrue(replies[2]["result"]["isError"])
            self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"]), {"exit_code": 2, "error": "stale_head"})
            self.assertEqual(replies[3]["error"]["code"], -32601)
            self.assertEqual(seen, [{"token": "secret", "params": {"name": "agentdb", "arguments": {"action": "merge"}}}])
            self.assertEqual((Path(temp) / "gpt-b.mcp_in.jsonl").read_bytes(), sent)
            self.assertEqual((Path(temp) / "gpt-b.mcp_out.jsonl").read_bytes(), stdout)
            self.assertEqual((Path(temp) / "tool_calls.gpt-b.jsonl").read_bytes().splitlines(),
                             [wire(lines[3]).rstrip(b"\n"), stdout.splitlines()[2]])


if __name__ == "__main__":
    unittest.main()
