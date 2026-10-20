"""Instrumented Claude Code headless transport (stream-json). Raw JSONL is written before parsing."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from codex_rpc import wire

# Variables of the session that launches the runner; a child must not inherit its identity,
# messaging channel or minimal-mode switch.
INHERITED_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
INHERITED_NAMES = ("CLAUDECODE", "CLAUDE_PID", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
                   "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR")


def child_environment(config_dir, extra=None):
    env = {k: v for k, v in os.environ.items()
           if k not in INHERITED_NAMES and not k.startswith(INHERITED_PREFIXES)}
    env.update(CLAUDE_CONFIG_DIR=str(config_dir), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
               CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", DISABLE_AUTOUPDATER="1")
    env.update(extra or {})
    return env


class ClaudeAgent:
    """One Claude Code process = one agent context; later turns reuse it like a thread."""

    def __init__(self, command, env, cwd, directory, actor):
        self.actor = actor
        self.directory = Path(directory)
        self.inbox = queue.Queue()
        self.events = []
        self.init = None
        self.requests = 0
        self.stdin_log = (self.directory / f"{actor}.stdin.jsonl").open("xb")
        self.stdout_log = (self.directory / f"{actor}.stdout.jsonl").open("xb")
        self.timing = (self.directory / f"{actor}.timing.jsonl").open("xb")
        self.errors = (self.directory / f"{actor}.stderr.log").open("xb")
        self.command = command
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors, cwd=str(cwd), env=env,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for raw in iter(self.process.stdout.readline, b""):
                self.stdout_log.write(raw)
                self.stdout_log.flush()
                self.inbox.put((time.time_ns(), raw))
        finally:
            self.inbox.put((time.time_ns(), None))

    def send(self, message):
        raw = wire(message)
        self.stdin_log.write(raw)
        self.stdin_log.flush()
        self.process.stdin.write(raw)
        self.process.stdin.flush()

    def pump(self, timeout):
        stamp, raw = self.inbox.get(timeout=timeout)
        if raw is None:
            raise RuntimeError(f"Claude Code exited ({self.process.poll()})")
        try:
            event = json.loads(raw)
        except ValueError:
            event = {"type": "unparsed"}
        kind = event.get("type")
        self.timing.write(wire({"received_ns": stamp, "type": kind, "subtype": event.get("subtype"),
                                "message_id": (event.get("message") or {}).get("id") if kind == "assistant" else None}))
        self.timing.flush()
        if kind == "system" and event.get("subtype") == "init" and self.init is None:
            self.init = event
        elif kind == "control_request":
            # Permission or other host callbacks fail closed; nothing is approved from here.
            self.send({"type": "control_response", "response": {"subtype": "error",
                       "request_id": event.get("request_id"), "error": "Only the registered experiment tool is supported"}})
        self.events.append(event)
        return event

    def turn(self, prompt, timeout=900):
        self.send({"type": "user", "session_id": "", "parent_tool_use_id": None,
                   "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
        end = time.monotonic() + timeout
        while True:
            if time.monotonic() >= end:
                self.requests += 1
                self.send({"type": "control_request", "request_id": f"interrupt-{self.requests}",
                           "request": {"subtype": "interrupt"}})
                self._drain_until_result(60)
                raise TimeoutError(f"Turn exceeded {timeout}s")
            try:
                event = self.pump(min(10, max(0.1, end - time.monotonic())))
            except queue.Empty:
                continue
            if event.get("type") == "result":
                return event

    def _drain_until_result(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if self.pump(min(5, max(0.1, end - time.monotonic()))).get("type") == "result":
                    return
            except (queue.Empty, RuntimeError):
                return

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        while not self.inbox.empty():
            try:
                self.pump(0.1)
            except (queue.Empty, RuntimeError):
                break
        for stream in (self.stdin_log, self.stdout_log, self.timing, self.errors):
            stream.close()
