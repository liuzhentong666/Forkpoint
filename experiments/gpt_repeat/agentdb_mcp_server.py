"""MCP stdio server for the repeated Claude experiment: the same single agentdb tool.

Claude Code starts one server per agent. Each tools/call is forwarded to the runner's
coordinator, which binds the actor, gates phases, runs agent_lab.py and audits the database.
Every received line is logged before parsing and every sent line before writing, so the
raw files are the exact bytes exchanged with Claude Code. The coordinator address and
token are in the launch arguments only; the model never sees them.
"""
import argparse
import json
from pathlib import Path
import socket
import sys

from codex_rpc import wire
from run_gpt_repeats import TOOL

# Same name, description and JSON schema as the GPT run's dynamic tool.
MCP_TOOL = {"name": TOOL["name"], "description": TOOL["description"], "inputSchema": TOOL["inputSchema"]}


def forward(port, token, params, timeout):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.sendall(wire({"token": token, "params": params}))
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            reply += chunk
    message = json.loads(reply)
    if "exception" in message:
        raise RuntimeError(message["exception"])
    return message["result"]


def respond(message, port, token, timeout):
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agentdb", "version": "1"}}
    if method == "tools/list":
        return {"tools": [MCP_TOOL]}
    if method == "tools/call":
        try:
            result = forward(port, token, params, timeout)
        except Exception as error:
            result = {"error": type(error).__name__, "detail": str(error)}
        # Mirrors the GPT run: the JSON result as one text item, failure when it carries an error.
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": bool(result.get("error"))}
    if method == "ping":
        return {}
    raise LookupError(method)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--raw", type=Path, required=True, help="Directory for this agent's raw MCP logs")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    received = (args.raw / f"{args.actor}.mcp_in.jsonl").open("xb")
    sent = (args.raw / f"{args.actor}.mcp_out.jsonl").open("xb")
    tools = (args.raw / f"tool_calls.{args.actor}.jsonl").open("xb")
    try:
        for raw in iter(sys.stdin.buffer.readline, b""):
            received.write(raw)
            received.flush()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict) or "id" not in message or "method" not in message:
                continue  # notifications and responses need no reply
            if message["method"] == "tools/call":
                tools.write(raw)
                tools.flush()
            try:
                reply = {"jsonrpc": "2.0", "id": message["id"], "result": respond(message, args.port, args.token, args.timeout)}
            except LookupError:
                reply = {"jsonrpc": "2.0", "id": message["id"],
                         "error": {"code": -32601, "message": "Only the registered experiment tool is supported"}}
            out = wire(reply)
            sent.write(out)
            sent.flush()
            if message["method"] == "tools/call":
                tools.write(out)
                tools.flush()
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
    finally:
        for stream in (received, sent, tools):
            stream.close()


if __name__ == "__main__":
    main()
