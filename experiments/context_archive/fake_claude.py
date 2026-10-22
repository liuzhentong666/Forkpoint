"""Stand-in for Claude Code headless, for dry runs of run_archive_trials.py; no model is called.

It takes the flags run_archive_trials.command passes, starts the MCP server named in
--mcp-config, prints a stream-json init event, and answers each stream-json prompt with a
fixed script of agentdb calls for the K = 14 audit task. Each request's size is reported
with the token model in results/context_archive/long-task-check.json, so the runner's
trigger fires where it would. It is not an agent: it reads the task from its prompts and
tool results only, and keeps them in memory. After a reset it recovers from the handoff it
is given: setup A, the JSON note it wrote; setup B, the handoff's entries plus fetches of the
first prompt and of the results its labels name. It can recover in setup B only during the
preparation phase, where long_task.py puts the reset; elsewhere it exits with an error.
"""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archive  # noqa: E402
import long_task  # noqa: E402  (also puts experiments/gpt_repeat on sys.path)
from codex_rpc import wire  # noqa: E402

UUID = re.compile("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
NOTE = "HANDOFF-NOTE "
MARKERS = {"You are the inventory agent": "prepare", "You are the pricing agent": "price",
           "The pricing agent's stage is finished": "recover"}


class Interrupted(Exception):
    pass


class Mcp:
    """The agentdb MCP server from --mcp-config, spoken to as Claude Code would."""

    def __init__(self, config):
        server = json.loads(Path(config).read_text(encoding="utf-8"))["mcpServers"]["agentdb"]
        self.process = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.ids = 0
        self.request("initialize", {"protocolVersion": "2025-06-18"})
        self.process.stdin.write(wire({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.process.stdin.flush()
        self.request("tools/list", {})

    def request(self, method, params):
        self.ids += 1
        self.process.stdin.write(wire({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params}))
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())["result"]

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=30)


class StandIn:
    def __init__(self, model, tool, mcp, token_model):
        self.model, self.tool, self.mcp, self.tokens = model, tool, mcp, token_model
        self.session = str(uuid.uuid4())
        self.inbox, self.waiting, self.interrupted = queue.Queue(), [], False
        self.context = self.input = self.output = 0
        self.state = dict(phase=None, base=None, revisions=[], audited={}, proposal=None, done=[], done_labels=[])
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in iter(sys.stdin.buffer.readline, b""):
            self.inbox.put(json.loads(line))
        self.inbox.put(None)

    def emit(self, event):
        sys.stdout.buffer.write(wire(event))
        sys.stdout.buffer.flush()

    def check(self):
        """Take any messages that arrived during a turn; an interrupt ends the turn at the next call."""
        while True:
            try:
                message = self.inbox.get_nowait()
            except queue.Empty:
                break
            if isinstance(message, dict) and message.get("type") == "control_request":
                self.interrupted = self.interrupted or message.get("request", {}).get("subtype") == "interrupt"
            else:
                self.waiting.append(message)
        if self.interrupted:
            raise Interrupted()

    def respond(self, block):
        tokens = round(self.tokens["intercept"] + self.tokens["tokens_per_byte"] * self.context)
        self.input += tokens
        self.output += max(1, len(json.dumps(block)) // 4)
        self.emit({"type": "assistant", "parent_tool_use_id": None, "session_id": self.session,
                   "message": {"id": "msg_" + uuid.uuid4().hex[:16], "model": self.model, "role": "assistant",
                               "content": [block], "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0,
                                                             "cache_creation_input_tokens": 0, "output_tokens": 1}}})
        self.context += long_task.context_bytes(block, False)

    def call(self, action, **args):
        self.check()
        call_id = "toolu_" + uuid.uuid4().hex[:20]
        arguments = {"action": action, **args}
        self.respond({"type": "tool_use", "id": call_id, "name": self.tool, "input": arguments})
        reply = self.mcp.request("tools/call", {"name": "agentdb", "arguments": arguments,
                                                "_meta": {"claudecode/toolUseId": call_id}})
        text = reply["content"][0]["text"]
        block = {"type": "tool_result", "tool_use_id": call_id, "is_error": bool(reply.get("isError")),
                 "content": [{"type": "text", "text": text}]}
        self.emit({"type": "user", "parent_tool_use_id": None, "session_id": self.session,
                   "message": {"role": "user", "content": [block]}})
        self.context += long_task.context_bytes(block, False)
        value = json.loads(text)
        if value.get("error") == "context_switch":
            # The runner is replacing this process; its interrupt is on the way.
            deadline = time.monotonic() + 30
            while not self.interrupted and time.monotonic() < deadline:
                time.sleep(0.05)
                try:
                    self.check()
                except Interrupted:
                    break
            raise Interrupted()
        self.state["done"].append(arguments)
        time.sleep(0.3)  # a model takes seconds between calls; the runner's interrupt lands here
        return value

    def done(self, arguments):
        def matches(label):
            words = label.split(" ")
            if words[:2] != ["call", arguments["action"]]:
                return False
            given = dict(word.split("=", 1) for word in words[2:] if "=" in word)
            return set(given) == set(arguments) - {"action"} and all(
                str(arguments[k]).startswith(v.rstrip("…")) for k, v in given.items())
        return arguments in self.state["done"] or any(matches(label) for label in self.state["done_labels"])

    def once(self, action, **args):
        arguments = {"action": action, **args}
        return None if self.done(arguments) else self.call(action, **args)

    def table(self):
        s = self.state
        return "\n".join(f"# {r[:8]}: {s['audited'][r]}" for r in s["revisions"]) + f"\nSTOCK = {long_task.TARGET_STOCK}\n"

    def prepare(self):
        s, base = self.state, self.state["base"]
        self.once("info")
        self.once("read-code", revision=base, path="experiments/pricing.py")
        self.once("read-code", revision=base, path="experiments/inventory.py")
        for revision in s["revisions"]:
            if revision not in s["audited"]:
                s["audited"][revision] = self.call("info", revision=revision)["result"]["product"]["stock"]
        self.once("fork", branch="gpt-b-audit", base=base)
        if not s["proposal"]:
            s["proposal"] = self.call("propose", branch="gpt-b-audit", expected=base, field="stock",
                                      value=str(long_task.TARGET_STOCK), code=self.table())["result"]["revision"]
        self.once("diff", base=base, revision=s["proposal"])
        self.once("read-code", revision=s["proposal"], path="experiments/inventory.py")
        return f"Proposal revision {s['proposal']}; original expected main {base}."

    def price(self):
        base = self.state["base"]
        self.once("info")
        self.once("fork", branch="gpt-a-price", base=base)
        proposal = self.call("propose", branch="gpt-a-price", expected=base, field="price", value="11",
                             code="PRICE = 11\n")["result"]["revision"]
        self.once("diff", base=base, revision=proposal)
        self.once("read-code", revision=proposal, path="experiments/pricing.py")
        self.call("merge", revision=proposal, expected=base)
        return f"Price proposal {proposal} merged."

    def recover_inventory(self):
        s = self.state
        self.once("merge", revision=s["proposal"], expected=s["base"])  # refused: stale_head
        main = self.call("info")["result"]["current_main"]
        self.call("fork", branch="gpt-b-audit-2", base=main)
        proposal = self.call("propose", branch="gpt-b-audit-2", expected=main, field="stock",
                             value=str(long_task.TARGET_STOCK), code=self.table())["result"]["revision"]
        self.call("merge", revision=proposal, expected=main)
        self.call("verify")
        return f"Stock proposal {proposal} merged and verified."

    def from_prompt(self, text):
        self.state["base"] = UUID.search(text).group(0)
        self.state["revisions"] = [m.group(0) for line in text.splitlines() if line.startswith("- ")
                                   for m in [UUID.search(line)] if m]

    def learn(self, value):
        """What a tool result tells the script: an audited stock or its own proposal."""
        result = value.get("result") if isinstance(value, dict) and isinstance(value.get("result"), dict) else {}
        product, read = result.get("product"), result.get("read_revision")
        if read in self.state["revisions"] and isinstance(product, dict):
            self.state["audited"][read] = product["stock"]
        if result.get("code_path") == "experiments/inventory.py" and result.get("branch") == "gpt-b-audit":
            self.state["proposal"] = result["revision"]

    def recover(self, text):
        if text.startswith(archive.HEADER_A):
            note = text[len(archive.HEADER_A):text.rindex("\n" + archive.CONTINUE)]
            self.state = json.loads(note[note.index(NOTE) + len(NOTE):])
            return
        full, catalog, wanted = {}, [], [1]
        for line in text.splitlines():
            shown = re.match(r"#(\d+) (\{.*\})$", line)
            listed = re.match(r'#(\d+)(?:-(\d+))? (".*")$', line)
            if shown:
                try:
                    full[int(shown.group(1))] = json.loads(shown.group(2))
                except ValueError:
                    wanted.append(int(shown.group(1)))  # cut: fetch it whole
            elif listed:
                label = json.loads(listed.group(3))
                catalog.append(label)
                if listed.group(2) and label.startswith(("call info revision=", "call propose", "call fork")):
                    wanted.append(int(listed.group(2)))
        fetched, waiting = {}, list(dict.fromkeys(wanted))
        while waiting:
            chunk, waiting = waiting[:archive.FETCH_ENTRIES], waiting[archive.FETCH_ENTRIES:]
            result = self.call("fetch", entries=chunk)["result"]
            fetched.update({item["entry"]: item["content"] for item in result["entries"]})
            waiting = result["omitted"] + waiting
        prompt = fetched[1]["text"]
        if any(label.startswith("prompt: The pricing agent") for label in catalog) or any(
                str(block.get("text", "")).startswith("The pricing agent") for block in full.values()):
            raise SystemExit("stand-in: setup B reset outside the preparation phase is not scripted")
        self.state.update(phase="prepare")
        self.from_prompt(prompt)
        self.state["done_labels"] = [label.split(" -> ")[0] for label in catalog if label.startswith("call ")]
        for block in list(full.values()) + list(fetched.values()):
            if block and block.get("type") == "tool_use":
                self.state["done"].append(block["input"])
            elif block and block.get("type") == "tool_result":
                self.learn(json.loads(block["content"][0]["text"]))

    def turn(self, text):
        self.context += long_task.context_bytes({"type": "text", "text": text}, False)
        self.interrupted = False
        try:
            if text.startswith(archive.NOTE_REQUEST[:40]):
                reply = NOTE + json.dumps(self.state, ensure_ascii=False)
            else:
                if text.startswith("Your context was reset"):
                    self.recover(text)
                else:
                    self.state["phase"] = next(phase for start, phase in MARKERS.items() if text.startswith(start))
                    if self.state["phase"] in ("prepare", "price"):
                        self.from_prompt(text)
                reply = {"prepare": self.prepare, "price": self.price, "recover": self.recover_inventory}[self.state["phase"]]()
            self.respond({"type": "text", "text": reply})
            self.finish("success", reply)
        except Interrupted:
            self.finish("error_during_execution", "Interrupted", error=True)

    def finish(self, subtype, text, error=False):
        self.emit({"type": "result", "subtype": subtype, "is_error": error, "result": text, "session_id": self.session,
                   "num_turns": 1, "duration_ms": 1, "total_cost_usd": 0.0,
                   "modelUsage": {self.model: {"inputTokens": self.input, "outputTokens": self.output,
                                               "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "thinkingTokens": 0}}})

    def serve(self):
        self.emit({"type": "system", "subtype": "init", "session_id": self.session, "model": self.model,
                   "tools": [self.tool], "mcp_servers": [{"name": "agentdb", "status": "connected"}],
                   "claude_code_version": "0.0.0 (context archive stand-in)", "permissionMode": "dontAsk",
                   "apiKeySource": "none", "cwd": os.getcwd()})
        while True:
            message = self.waiting.pop(0) if self.waiting else self.inbox.get()
            if message is None:
                return
            if message.get("type") == "user":
                self.turn("".join(c.get("text", "") for c in message["message"]["content"]))


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("0.0.0 (context archive stand-in, not Claude Code)")
        return
    option = {name: argv[argv.index(name) + 1] for name in ("--model", "--mcp-config", "--allowedTools")}
    fits = json.loads((archive.ROOT / "results" / "context_archive" / "long-task-check.json").read_text(encoding="utf-8"))
    token_model = fits["token_model"]["fits"][fits["token_model"]["chosen"]]
    mcp = Mcp(option["--mcp-config"])
    try:
        StandIn(option["--model"], option["--allowedTools"], mcp, token_model).serve()
    finally:
        mcp.close()


if __name__ == "__main__":
    main()
