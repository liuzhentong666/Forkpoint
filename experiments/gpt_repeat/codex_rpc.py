"""Instrumented Codex app-server transport. Raw JSONL is written before parsing."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


def wire(message):
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class CodexRPC:
    def __init__(self, executable, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.inbox = queue.Queue()
        self.sequence = 0
        self.responses = {}
        self.completed = {}
        self.usage = {}
        self.reroutes = []
        self.items = {}
        self.tool_handler = None
        self.calls = []
        self.client = (self.directory / "client.jsonl").open("xb")
        self.server = (self.directory / "server.jsonl").open("xb")
        self.tools = (self.directory / "tool_calls.jsonl").open("xb")
        self.timing = (self.directory / "transport_timing.jsonl").open("xb")
        self.errors = (self.directory / "server.stderr.log").open("xb")
        env = dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH",
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_TOOL_CALL_ID",
                    "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY"):
            env.pop(key, None)
        self.command = [str(executable), "app-server", "--listen", "stdio://"]
        # Only this child process: no settings files are changed.
        for feature in ("shell_tool", "multi_agent", "apps", "plugins", "hooks",
                        "browser_use", "computer_use", "image_generation", "view_image"):
            self.command += ["-c", f"features.{feature}=false"]
        self.command += ["-c", 'web_search="disabled"']
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors, env=env,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.initialize = self.request("initialize", {"clientInfo": {"name": "agentdb_repeat_eval",
            "title": "AgentDB repeated GPT experiment", "version": "1"},
            "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})

    def _read(self):
        try:
            for raw in iter(self.process.stdout.readline, b""):
                self.server.write(raw)
                self.server.flush()
                self.inbox.put((time.time_ns(), raw))
        finally:
            self.inbox.put((time.time_ns(), None))

    def send(self, message, tool_response=False):
        raw = wire(message)
        self.client.write(raw)
        self.client.flush()
        if tool_response:
            self.tools.write(raw)
            self.tools.flush()
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def pump(self, timeout=30):
        stamp, raw = self.inbox.get(timeout=timeout)
        if raw is None:
            raise RuntimeError(f"Codex app-server exited ({self.process.poll()})")
        message = json.loads(raw)
        method, params = message.get("method"), message.get("params", {})
        self.timing.write(wire({"received_ns": stamp, "method": method, "id": message.get("id"),
                                "threadId": params.get("threadId"), "turnId": params.get("turnId")}))
        self.timing.flush()
        if method == "item/tool/call":
            # Original server request, not a narrative or reconstructed argument list.
            self.tools.write(raw)
            self.tools.flush()
            self.calls.append(params)
            try:
                result = self.tool_handler(params) if self.tool_handler else {"error": "No handler"}
                response = {"contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}],
                            "success": not bool(result.get("error"))}
            except Exception as error:
                response = {"contentItems": [{"type": "inputText", "text": json.dumps({"error": type(error).__name__, "detail": str(error)})}], "success": False}
            self.send({"id": message["id"], "result": response}, tool_response=True)
        elif method and "id" in message:
            # Unexpected tool/approval routes fail closed; never approve a shell or file write.
            self.send({"id": message["id"], "error": {"code": -32601, "message": "Only the registered experiment tool is supported"}})
        elif method == "thread/tokenUsage/updated":
            self.usage[params["threadId"]] = params["tokenUsage"]
        elif method == "model/rerouted":
            self.reroutes.append(params)
        elif method == "item/completed":
            self.items[(params.get("threadId"), params["item"]["id"])] = params["item"]
        elif method == "turn/completed":
            self.completed[params["turn"]["id"]] = params["turn"]
        elif "id" in message:
            self.responses[message["id"]] = message
        return message

    def request(self, method, params, timeout=120):
        self.sequence += 1
        rid = self.sequence
        self.send({"id": rid, "method": method, "params": params})
        end = time.monotonic() + timeout
        while rid not in self.responses:
            if time.monotonic() >= end:
                raise TimeoutError(method)
            try:
                self.pump(min(10, max(0.1, end-time.monotonic())))
            except queue.Empty:
                continue
        response = self.responses.pop(rid)
        if "error" in response:
            raise RuntimeError(json.dumps(response["error"]))
        return response["result"]

    def turn(self, thread, prompt, effort, timeout=900):
        result = self.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}],
                                           "effort": effort, "environments": []})
        tid = result["turn"]["id"]
        end = time.monotonic() + timeout
        while tid not in self.completed:
            if time.monotonic() >= end:
                self.request("turn/interrupt", {"threadId": thread, "turnId": tid})
                raise TimeoutError(f"Turn exceeded {timeout}s")
            try:
                self.pump(min(10, max(0.1, end-time.monotonic())))
            except queue.Empty:
                continue
        # Drain already-delivered usage notifications, without summing cumulative counters.
        while not self.inbox.empty():
            self.pump(0.1)
        return self.completed[tid]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        for stream in (self.client, self.server, self.tools, self.timing, self.errors):
            stream.close()
