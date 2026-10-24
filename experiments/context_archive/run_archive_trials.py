"""Run the context archive experiment on the K = 14 audit task (docs/CONTEXT_ARCHIVE_EXPERIMENT.md).

The harness is experiments/gpt_repeat/run_claude_repeats.py's (Claude Code headless, one MCP
tool, agent_lab.py, an SQL audit after every write), with these changes:
- the inventory agent first audits product 101's stock at 14 earlier revisions (long_task.py),
  and its policy must record them (audit_lines); the call budget is 60 per agent;
- every prompt sent and every content line received is archived as it passes (archive.py);
- the first time a request's input passes archive.LIMIT_TOKENS, the agent's process is
  replaced at the next tool-call boundary by a fresh one that starts from the round's handoff:
  setup A, the note the old process writes when asked; setup B, archive.render_handoff, and a
  tool with the fetch action. Until its reset, an agent has the same tool in both setups, so
  every request before the switch is the same size in A and B. One reset per agent and round
  (archive.MAX_SWITCHES).
Rounds alternate A, B. --claude may name a .py stand-in (fake_claude.py) for a dry run.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import secrets
import shutil
import statistics
import subprocess
import sys
import threading
import time

import archive
from archive import (ArchiveError, CONTINUE, HEADER_A, NOTE_REQUEST, Trigger, Writer, fetch_reply, load_entries,
                     received_entry, render_handoff, sent_entry)
import long_task

GPT_REPEAT = archive.ROOT / "experiments" / "gpt_repeat"
sys.path.insert(0, str(GPT_REPEAT))
from claude_scoring import TOKEN_KEYS, add_tokens, model_usage_tokens, read_jsonl, trace_metrics  # noqa: E402
from claude_stream import ClaudeAgent, child_environment  # noqa: E402
from codex_rpc import wire  # noqa: E402
from repeat_scoring import wilson  # noqa: E402
from run_claude_repeats import (ACTORS, CLAUDE_INSTRUCTIONS, Coordinator, Trial, check_init, claude_command,  # noqa: E402
                                save, sha256)
from run_gpt_repeats import ACTIONS, FIELDS, LAND_B, RUN_A, TOOL, audit, connect_owner, run_helper  # noqa: E402

K = 14
CALL_BUDGET = long_task.CALL_BUDGET
ARRANGED = ("stale_head", "merge_conflict")  # refusals the recovery prompt sets up in every round
PINNED = ("revision", "base")  # a call with one of these reads a fixed version; repeating it repeats its result
WRITES = ("fork", "propose", "merge")
INSTRUCTIONS = CLAUDE_INSTRUCTIONS.replace("Use at most 40 tool calls", f"Use at most {CALL_BUDGET} tool calls", 1)
assert INSTRUCTIONS != CLAUDE_INSTRUCTIONS
TOOL_B = {"type": "function", "name": TOOL["name"],
          "description": TOOL["description"] + " fetch requires entries (an array of 1-8 entry numbers) and "
                         "returns your own earlier entries word for word.",
          "inputSchema": {"type": "object", "properties": {
              **TOOL["inputSchema"]["properties"], "action": {"type": "string", "enum": ACTIONS + ["fetch"]},
              "entries": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": archive.MAX_ENTRY},
                          "minItems": 1, "maxItems": archive.FETCH_ENTRIES}},
              "required": ["action"], "additionalProperties": False}}
# Every project file a run loads or runs (a test checks the loaded modules are all here).
SOURCES = ("experiments/context_archive/archive.sql", "experiments/context_archive/archive.py",
           "experiments/context_archive/long_task.py", "experiments/context_archive/replay.py",
           "experiments/context_archive/run_archive_trials.py",
           "experiments/gpt_repeat/run_claude_repeats.py", "experiments/gpt_repeat/claude_stream.py",
           "experiments/gpt_repeat/agentdb_mcp_server.py", "experiments/gpt_repeat/claude_scoring.py",
           "experiments/gpt_repeat/repeat_scoring.py", "experiments/gpt_repeat/run_gpt_repeats.py",
           "experiments/gpt_repeat/codex_rpc.py", "agent_state/__init__.py", "agent_state/objects.py",
           "agent_state/storage.py", "agent_lab.py", "db.py", "review_local.py", "docs/CONTEXT_ARCHIVE_EXPERIMENT.md",
           # agent_lab.py init installs the version engine with db.Connection.install
           "sql/001_core.sql", "sql/002_demo_app.sql", "sql/003_manager_role.sql", "sql/004_merge_batch.sql",
           "sql/005_bounded_history.sql", "sql/006_history_health.sql", "sql/007_search_path_pg_temp.sql",
           "sql/008_read_set_merge.sql", "sql/009_canonical_row_docs.sql", "sql/010_unknown_branch_code.sql")


def audit_lines(code, revisions, stocks):
    """How a policy records the audit. exact: its first lines are the table as asked. Per revision:
    correct (its '# <first 8 characters>: <stock>' line has the right stock, and no such line another),
    wrong, or missing. all_correct is what success needs; order and other lines are not scored."""
    lines = (code or "").splitlines()
    counts = {"correct": 0, "wrong": 0, "missing": 0}
    for revision, stock in zip(revisions, stocks):
        prefix = f"# {revision[:8]}:"
        values = [line[len(prefix):].strip() for line in lines if line.startswith(prefix)]
        counts["missing" if not values else "correct" if set(values) == {str(stock)} else "wrong"] += 1
    expected = [f"# {r[:8]}: {s}" for r, s in zip(revisions, stocks)]
    return dict(exact=lines[:len(expected)] == expected, all_correct=counts["correct"] == len(expected), **counts)


def after_switch(summary, actor):
    """The actor's calls after its reset began: repeats of an identical earlier call that reads fixed data
    again (a read at a fixed revision, a fetch) or does the same write again (fork, propose, merge),
    failed calls (other than the arranged refusals), and fetches. Calls refused while the process was
    being replaced did not run and are counted apart."""
    starts = [s["call_sequence"] for s in summary["switches"] if s["actor"] == actor]
    if not starts:
        return None
    seen, counts = set(), dict(calls=0, repeats=0, failed=0, fetches=0, refused_while_switching=0)
    for call in summary["calls"]:
        if call["actor"] != actor:
            continue
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        key = json.dumps(arguments, sort_keys=True)
        if call.get("refused") == "context_switch":
            counts["refused_while_switching"] += 1
            continue
        if call["sequence"] > starts[0]:
            action = arguments.get("action")
            fixed = action == "fetch" or action in WRITES or any(k in arguments for k in PINNED)
            counts["calls"] += 1
            counts["repeats"] += fixed and key in seen
            counts["failed"] += bool(call.get("error")) and call.get("error") not in ARRANGED
            counts["fetches"] += action == "fetch"
        seen.add(key)
    counts["primary"] = counts["repeats"] + counts["failed"]
    return counts


def turn_usages(directory, actor):
    """Each turn's usage in one process: differences of the cumulative usage its result events report."""
    totals = [model_usage_tokens(e.get("modelUsage")) for e in read_jsonl(Path(directory) / f"{actor}.stdout.jsonl")
              if e.get("type") == "result"]
    usages, previous = [], {k: 0 for k in TOKEN_KEYS}
    for total in totals:
        if total is None or previous is None:  # unknown, and so is the next difference
            usages.append(None)
        else:
            usages.append({k: None if total[k] is None or previous[k] is None else total[k] - previous[k]
                           for k in TOKEN_KEYS})
        previous = total
    return usages


def usage_after_switch(raw, actor, setup, contexts):
    """Tokens the reset cost the actor: every turn of its later processes, plus setup A's note turn."""
    rows = []
    for index, name in enumerate(contexts):
        usages = turn_usages(raw / name, actor)
        if index > 0:
            rows += usages
        elif len(contexts) > 1 and setup == "A" and usages:
            rows.append(usages[-1])
    return None if not rows or None in rows else add_tokens(rows)


def combined_metrics(raw, processes, model, switched):
    """trace_metrics for every process folder, combined for the success rule.

    processes maps a folder (ctx-1, ctx-2, ...) to the actors that logged there. A stream tool use
    without a result in a process replaced at a reset (switched holds (folder, actor)) was cut off by
    the interrupt: it is listed as abandoned, not as unanswered.
    """
    parts = {name: trace_metrics(raw / name, actors, model) for name, actors in processes.items()}
    unanswered, abandoned, without_stream, models, tokens, observed = [], [], [], {}, [], []
    for name, part in parts.items():
        for actor, agent in part["agents"].items():
            label = f"{name}/{actor}"
            unanswered += [f"{label}:{i}" for i in agent["mcp_unanswered_ids"]]
            (abandoned if (name, actor) in switched else unanswered).extend(
                f"{label}:{i}" for i in agent["stream_unanswered_tool_uses"])
            without_stream += [f"{label}:{i}" for i in agent["mcp_calls_without_stream_tool_use"]]
            models[label] = {"init_model": agent["init"]["model"], "response_models": agent["response_models"]}
            tokens.append(agent["tokens"])
            observed.append(agent["observed_tokens"])
    reroutes = [x for part in parts.values() for x in part["model_reroutes"]]
    mismatches = [x for part in parts.values() for x in part["init_mismatches"]]
    return {"processes": {name: actors for name, actors in processes.items()}, "model_by_process": models,
            "model_protocol_match": not reroutes and not mismatches and all(m["init_model"] == model for m in models.values()),
            "model_reroutes": reroutes, "init_mismatches": mismatches,
            "tool_calls": sum(p["tool_calls"] for p in parts.values()),
            "tool_responses": sum(p["tool_responses"] for p in parts.values()),
            "unanswered_tool_request_ids": unanswered, "abandoned_at_reset": abandoned,
            "mcp_calls_without_stream_tool_use": without_stream,
            "tokens_total": None if None in tokens else add_tokens(tokens),
            "observed_tokens_total": add_tokens(observed),
            "list_cost_usd": round(sum(p["list_cost_usd"] for p in parts.values()), 6)}


class Count:
    """Entries archived for one actor so far, shared by its processes; read and written on the runner's thread."""

    def __init__(self):
        self.value = 0


class ArchivingAgent(ClaudeAgent):
    """A ClaudeAgent whose prompts and content lines go to the archive as they pass, and whose turn
    stops at the trigger's boundary when it has one (limit None: no trigger, as after a reset)."""

    def __init__(self, command, env, cwd, directory, actor, writer, count, limit=archive.LIMIT_TOKENS):
        # Set before ClaudeAgent starts the reader thread, which uses the writer.
        self.writer, self.count = writer, count
        self.trigger = Trigger(limit) if limit else None
        super().__init__(command, env, cwd, directory, actor)

    def _read(self):
        try:
            for raw in iter(self.process.stdout.readline, b""):
                self.stdout_log.write(raw)
                self.stdout_log.flush()
                try:
                    self.writer.put(self.actor, received_entry(raw))
                except Exception as error:  # any capture failure stops the round; the line is still logged and passed on
                    self.writer.fail(str(error) if isinstance(error, ArchiveError) else f"capture failed: {error!r}")
                self.inbox.put((time.time_ns(), raw))
        finally:
            self.inbox.put((time.time_ns(), None))

    def send(self, message):
        entry = sent_entry(wire(message))
        if entry:
            self.writer.put(self.actor, entry)
            self.count.value += 1
        super().send(message)

    def pump(self, timeout):
        event = super().pump(timeout)
        if archive.content_of(event) is not None:
            self.count.value += 1
        self.writer.check()
        return event

    def interrupt(self, timeout):
        """Interrupt the current turn and wait up to timeout seconds for its result event.

        Raises TimeoutError if it does not come; the next turn (setup A's note) would otherwise
        read the interrupted turn's late result as its own. A process that exits sends no more.
        """
        self.requests += 1
        self.send({"type": "control_request", "request_id": f"interrupt-{self.requests}",
                   "request": {"subtype": "interrupt"}})
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"Interrupt not answered within {timeout}s")
            try:
                if self.pump(min(5, left)).get("type") == "result":
                    return
            except queue.Empty:
                continue
            except ArchiveError:
                raise
            except RuntimeError:
                return  # the process exited, so no late result can follow

    def turn(self, prompt, timeout=900, switching=True):
        """ClaudeAgent.turn, stopping at the trigger's boundary: ("result", event) or ("switch", entries so far)."""
        self.send({"type": "user", "session_id": "", "parent_tool_use_id": None,
                   "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
        end = time.monotonic() + timeout
        while True:
            if time.monotonic() >= end:
                with contextlib.suppress(TimeoutError):  # the turn's own timeout is what is reported
                    self.interrupt(60)
                raise TimeoutError(f"Turn exceeded {timeout}s")
            try:
                event = self.pump(min(10, max(0.1, end - time.monotonic())))
            except queue.Empty:
                continue
            if event.get("type") == "result":
                return "result", event
            if self.trigger and self.trigger.observe(event) and switching:
                return "switch", self.count.value


class ArchiveTrial(Trial):
    """Trial with a 60-call budget, a refusal for an agent whose process is being replaced, and setup B's
    fetch. Every call is recorded with its sequence number and every refusal with its error, which
    after_switch counts; Trial records neither."""

    budget = CALL_BUDGET

    def __init__(self, args, folder, summary, setup):
        super().__init__(args, folder, summary)
        self.setup, self.switching, self.fetch_sql = setup, set(), None
        self.fetching = set()  # setup B: agents whose process since the reset has the fetch tool

    def answer_without_helper(self, actor, params, values, record):
        record["sequence"] = self.sequence
        if params.get("name") == "agentdb" and actor in self.switching:
            record["refused"] = "context_switch"
            refusal = {"error": "context_switch", "detail": "Your context is being reset; this call did not run."}
        else:
            refusal = self.count_call(actor, params)
            if refusal is None:
                fetched = self.fetch(actor, values, record)
                if fetched is not None:
                    return fetched
                refusal = self.check_arguments(values)
        if refusal is not None:
            record.update(refusal)
        return refusal

    def fetch(self, actor, values, record):
        """Setup B's fetch after the agent's reset, answered from the archive; None for any other call
        (check_arguments then refuses a fetch, as in setup A)."""
        if (self.setup != "B" or actor not in self.fetching or not isinstance(values, dict)
                or values.get("action") != "fetch" or set(values) - ({"action", "entries"} | set(FIELDS))):
            return None
        requested = values.get("entries")
        valid = fetch_reply({}, requested).get("error") != "invalid_entries"
        record["executed"] = True
        try:
            found = {e[0]: e for e in load_entries(self.fetch_sql, actor, requested)} if valid else {}
        except Exception as error:
            self.record_failure(record, error)
            raise
        reply = fetch_reply(found, requested)
        record.update(finished_ns=time.time_ns(), exit_code=2 if "error" in reply else 0,
                      error=reply.get("error"))
        save(self.folder / "result.json", self.summary)
        return reply

    def run_call(self, actor, values, record):
        try:
            return super().run_call(actor, values, record)
        except Exception as error:  # e.g. agent_lab.py timed out
            self.record_failure(record, error)
            raise

    def record_failure(self, record, error):
        """Record an exception as the call's outcome, as the coordinator reports it (type and text), so
        after_switch counts the call as failed."""
        record.update(error=type(error).__name__, detail=str(error), finished_ns=time.time_ns())


class Round:
    """One round's agent processes: their log folders and MCP configs, and the archive they share."""

    def __init__(self, args, folder, raw, workdir, port, tokens, writer, tool_file):
        self.args, self.folder, self.raw, self.workdir, self.port = args, folder, raw, workdir, port
        self.tokens, self.writer, self.tool_file = tokens, writer, tool_file
        self.env = child_environment(args.config_dir, args.child_env)
        self.contexts = {actor: [] for actor in ACTORS}  # each actor's process folders, in order
        self.processes = {}  # folder -> actors that logged there
        self.switched = set()  # (folder, actor) of processes replaced at a reset
        self.counts = {actor: Count() for actor in ACTORS}

    def start(self, actor, limit=archive.LIMIT_TOKENS, fetch=False):
        name = f"ctx-{len(self.contexts[actor]) + 1}"
        directory = self.raw / name
        directory.mkdir(exist_ok=True)
        server = [str(GPT_REPEAT / "agentdb_mcp_server.py"), "--port", str(self.port), "--token", self.tokens[actor],
                  "--raw", str(directory), "--actor", actor]
        if fetch:  # setup B after a reset; before it, both setups serve the same tool
            server += ["--tool-file", str(self.tool_file)]
        mcp_config = self.folder / f"{actor}.{name}.mcp-config.json"
        save(mcp_config, {"mcpServers": {"agentdb": {"type": "stdio", "command": self.args.python,
                                                     "args": server, "env": {}}}})
        agent = ArchivingAgent(claude_command(self.args, mcp_config, INSTRUCTIONS), self.env, self.workdir, directory, actor,
                               self.writer, self.counts[actor], limit)
        self.contexts[actor].append(name)
        self.processes.setdefault(name, []).append(actor)
        return agent


def switch(args, trial, rnd, actor, agent, boundary):
    """Replace actor's process by a fresh one that starts from this round's handoff; return it and the handoff."""
    with trial.lock:
        number = sum(s["actor"] == actor for s in trial.summary["switches"]) + 1
        if number > archive.MAX_SWITCHES:
            raise RuntimeError(f"switch_limit: {actor} would be reset {number} times")
        trial.switching.add(actor)
        record = {"actor": actor, "phase": trial.phase, "number": number, "setup": trial.setup,
                  "after_entry": boundary, "request_tokens": agent.trigger.crossed,
                  "call_sequence": trial.sequence, "started_ns": time.time_ns()}
        trial.summary["switches"].append(record)
    agent.interrupt(args.turn_timeout)
    if trial.setup == "A":
        kind, event = agent.turn(NOTE_REQUEST, args.turn_timeout, switching=False)
        note = str(event.get("result") or "")
        record.update(note_bytes=len(note.encode("utf-8")),
                      note_turn_ok=event.get("subtype") == "success" and not event.get("is_error"))
        if not record["note_turn_ok"]:
            # There is no note to hand over: the round stops and is kept as a failure (no retry, no rescue).
            raise RuntimeError(f"note_turn_failed: {event.get('subtype')}: {note[:300]}")
        handoff = HEADER_A + note + "\n" + CONTINUE
    agent.close()
    rnd.switched.add((rnd.contexts[actor][-1], actor))
    rnd.writer.flush()
    if trial.setup == "B":
        handoff = render_handoff(load_entries(trial.fetch_sql, actor, range(1, boundary + 1)))
    data = handoff.encode("utf-8")
    (trial.folder / f"handoff.{actor}.{number}.txt").write_bytes(data)
    record.update(handoff_bytes=len(data), handoff_sha256=hashlib.sha256(data).hexdigest(), finished_ns=time.time_ns())
    # archive.MAX_SWITCHES == 1: no trigger after the reset
    fresh = rnd.start(actor, limit=None, fetch=trial.setup == "B")
    trial.summary["agents"][actor]["processes"].append({"context": rnd.contexts[actor][-1], "pid": fresh.process.pid})
    with trial.lock:
        trial.switching.discard(actor)
        if trial.setup == "B":
            trial.fetching.add(actor)
    save(trial.folder / "result.json", trial.summary)
    return fresh, handoff


def run_trial(args, number, setup):
    folder = args.output / f"run-{number:02d}"
    folder.mkdir(exist_ok=False)
    raw = folder / "raw"
    raw.mkdir()
    workdir = args.workdir / f"run-{number:02d}"
    workdir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    summary = {"run": number, "setup": setup, "requested_model": args.model, "reasoning_effort": args.effort,
               "status": "running", "success": False, "real_cloud_verified": False,
               "agents": {}, "phases": [], "switches": [], "published_overwrites": [], "peer_branch_modifications": [],
               "calls": [], "audit_errors": [], "actor_landed_own_work": {"gpt-a": False, "gpt-b": False}}
    save(folder / "result.json", summary)
    trial = ArchiveTrial(args, folder, summary, setup)
    server = writer = reader = rnd = None
    agents = {}
    try:
        initial = run_helper(args, None, "init")
        save(folder / "setup.json", initial)
        if initial["exit_code"]:
            raise RuntimeError("Database setup failed: " + initial["stderr"])
        trial.database = summary["database"] = initial["result"]["database"]  # named even if the round fails next
        trial.owner = connect_owner(args, trial.database)
        revisions = [long_task.commit_stock(trial.owner, stock) for stock in long_task.STOCK_HISTORY[:K]]
        trial.base = long_task.commit_stock(trial.owner, 100)
        trial.owner.sql((archive.HERE / "archive.sql").read_text(encoding="utf-8"))
        summary.update(base=trial.base, audited_revisions=revisions)
        trial.current = audit(trial.owner, trial.base)
        trial.audit_log = (folder / "database_audit.jsonl").open("xb")
        trial.audit_log.write(wire({"stage": "initial", "snapshot": trial.current}))
        trial.audit_log.flush()
        writer = Writer(lambda: trial.owner.session(trial.database))
        reader = trial.owner.session(trial.database)
        trial.fetch_sql = reader.sql
        server = Coordinator(trial)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tool_file = None
        if setup == "B":
            tool_file = folder / "tool-b.json"
            save(tool_file, TOOL_B)
        tokens = {actor: secrets.token_hex(16) for actor in ACTORS}
        trial.tokens.update({token: actor for actor, token in tokens.items()})
        rnd = Round(args, folder, raw, workdir, server.server_address[1], tokens, writer, tool_file)
        for actor in ACTORS:
            agents[actor] = rnd.start(actor)
            summary["agents"][actor] = {"processes": [{"context": "ctx-1", "pid": agents[actor].process.pid}]}
        stages = [("prepare_inventory", "gpt-b", long_task.prompt_for(trial.base, revisions, K)),
                  ("price_and_merge", "gpt-a", RUN_A.format(base=trial.base)),
                  ("recover_inventory", "gpt-b", LAND_B)]
        for phase, actor, prompt in stages:
            trial.phase = phase
            save(folder / (phase + ".prompt.json"), {"actor": actor, "prompt": prompt})
            kind, value = agents[actor].turn(prompt, args.turn_timeout)
            while kind == "switch":  # value: entries archived up to the boundary
                agents[actor], handoff = switch(args, trial, rnd, actor, agents[actor], value)
                kind, value = agents[actor].turn(handoff, args.turn_timeout)
            event = value
            ok = event.get("subtype") == "success" and not event.get("is_error")
            turn = {"status": "completed" if ok else "failed", "error": None if ok else event.get("result"), "result": event}
            summary["phases"].append({"phase": phase, "actor": actor, "turn": turn})
            save(folder / "result.json", summary)
            print(f"run {number:02d} ({setup}): {phase} -> {turn['status']}", flush=True)
            if not ok:
                raise RuntimeError(f"Model turn {event.get('subtype')}: {event.get('result')}")
            check_init(agents[actor], args)
        check = run_helper(args, trial.database, "verify")
        summary["independent_verification"] = check
        final = audit(trial.owner, trial.base)
        summary["final_audit"] = final
        summary["audit_table"] = audit_lines(final["inventory"], revisions, long_task.STOCK_HISTORY[:K])
        summary["status"] = "completed"
        summary["success"] = bool(check["exit_code"] == 0 and final["rows"] == 4000
            and final["other_rows_unchanged"] and final["base_unchanged"] and final["open_transactions"] == 0
            and all(summary["actor_landed_own_work"].values()) and summary["audit_table"]["all_correct"]
            and not summary["published_overwrites"] and not summary["peer_branch_modifications"]
            and not summary["audit_errors"])
    except Exception as error:
        summary["status"] = "archive_error" if isinstance(error, ArchiveError) else "error"
        summary["success"] = False
        summary["error"] = {"type": type(error).__name__, "detail": str(error)}
        if trial.owner and trial.base:
            try:
                summary["final_audit"] = audit(trial.owner, trial.base)
            except Exception as audit_error:
                summary["final_audit_error"] = str(audit_error)
    finally:
        for agent in agents.values():
            agent.close()
        if server:
            server.shutdown()
            server.server_close()
        if trial.audit_log:
            trial.audit_log.close()
        if writer:
            try:
                writer.flush(120)
            except ArchiveError as error:
                summary.update(status="archive_error", success=False, error={"type": "ArchiveError", "detail": str(error)})
            summary["archived_entries"] = writer.written
            writer.close()
        if reader:
            reader.close()
        if rnd and rnd.processes:
            metrics = summary["metrics"] = combined_metrics(raw, rnd.processes, args.model, rnd.switched)
            summary["model_protocol_match"] = metrics["model_protocol_match"]
            if (not metrics["model_protocol_match"] or metrics["unanswered_tool_request_ids"]
                    or metrics["mcp_calls_without_stream_tool_use"] or metrics["tool_calls"] != len(summary["calls"])):
                summary["success"] = False
            summary["after_switch"] = {actor: after_switch(summary, actor) for actor in ACTORS}
            summary["tokens_after_switch"] = {actor: usage_after_switch(raw, actor, setup, rnd.contexts[actor])
                                              for actor in ACTORS if len(rnd.contexts[actor]) > 1}
        summary["wall_seconds"] = round(time.monotonic() - started, 3)
        save(folder / "result.json", summary)
        print(f"run {number:02d} ({setup}): status={summary['status']} success={summary['success']} "
              f"switches={len(summary['switches'])}", flush=True)
    return summary


def register(args):
    version = subprocess_output(args.launcher_version)
    account = {}
    if args.claude.suffix != ".py":
        status = subprocess_output([str(args.claude), "auth", "status"], child_environment(args.config_dir, args.child_env))
        try:
            account = {k: v for k, v in json.loads(status).items() if k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
        except ValueError:
            account = {"auth_status_unreadable": status[-500:]}
    protocol = {
        "registered_at_utc": datetime.now(timezone.utc).isoformat(), "client_date": args.client_date,
        "pilot": args.pilot, "runs_per_setup": args.runs_per_setup, "order": "rounds alternate A, B: odd runs A, even runs B",
        "model": args.model, "reasoning_effort": args.effort, "claude_code_version": version,
        "claude_executable": str(args.claude), "claude_executable_sha256": sha256(args.claude), "account": account,
        "workers": args.workers, "turn_timeout_seconds": args.turn_timeout, "max_tool_calls_per_agent": CALL_BUDGET,
        "instructions": INSTRUCTIONS,
        "prompts": {"prepare_inventory": long_task.PREPARE_AUDIT, "price_and_merge": RUN_A, "recover_inventory": LAND_B},
        "task": {"audited_revisions": K, "stock_history": list(long_task.STOCK_HISTORY[:K]), "target_stock": long_task.TARGET_STOCK,
                 "setup": "agent_lab.py init, one main commit per audited stock value of product 101, then one back to stock 100 (the common baseline)"},
        "handoffs": {"A": {"note_request": NOTE_REQUEST, "header": HEADER_A, "continue": CONTINUE},
                     "B": {"header": archive.HEADER_B, "continue": CONTINUE}},
        "constants": {"limit_tokens": archive.LIMIT_TOKENS, "in_full_bytes": archive.IN_FULL_BYTES,
                      "handoff_bytes": archive.HANDOFF_BYTES, "fetch_entries": archive.FETCH_ENTRIES,
                      "fetch_bytes": archive.FETCH_BYTES, "max_switches_per_agent": archive.MAX_SWITCHES},
        "tools": {"before_reset": TOOL, "A_after_reset": TOOL, "B_after_reset": TOOL_B},
        "tool_name_seen_by_model": args.tool_name,
        "claude_command": claude_command(args, Path("<run>/<actor>.<ctx>.mcp-config.json"), INSTRUCTIONS),
        "child_environment": {"CLAUDE_CONFIG_DIR": str(args.config_dir), **{
            k: v for k, v in child_environment(args.config_dir, args.child_env).items() if k.startswith(("CLAUDE", "DISABLE"))}},
        "agent_working_directory": str(args.workdir) + "/run-NN (empty, outside any git repository, no CLAUDE.md or AGENTS.md)",
        "sources_sha256": {p: source_hash(args.project / p) for p in SOURCES},
        "success_definition": "As the 2026-10-20 Claude run (both stages complete, both actors land own work, final verify "
                              "passes, 3999 peer rows and the baseline unchanged, no open transaction, no published peer "
                              "overwrite, no peer branch edit, every API response from the registered model, every MCP "
                              "request answered), plus: main's inventory.py records all 14 audited stocks correctly "
                              "(audit_lines all_correct) and no archive error. A stream tool use cut off by a reset's "
                              "interrupt is listed as abandoned, not unanswered.",
        "primary_metric": "Per round, after the reset began: repeats of an identical earlier call that reads fixed data "
                          "again (a read at a fixed revision, a fetch) or does the same write again (fork, propose, merge), "
                          "plus failed calls other than stale_head and merge_conflict; calls refused while the process was "
                          "replaced did not run and are reported apart.",
        "counting": "All registered rounds kept; no replacement of failed rounds; no manual rescue. Tokens as in the "
                    "2026-10-20 Claude run, per process; tokens_after_switch is every turn of the later process plus "
                    "setup A's note turn.",
        "real_cloud_verified": False}
    save(args.output / "protocol.json", protocol)
    return protocol


def subprocess_output(command_line, env=None):
    return subprocess.run(command_line, capture_output=True, text=True, env=env).stdout.strip()


def source_hash(path):
    return sha256(path) if path.is_file() else None


def aggregate(results, setup):
    rows = [r for r in results if r["setup"] == setup]
    successes = sum(r["success"] for r in rows)
    primary = [sum(a["primary"] for a in (r.get("after_switch") or {}).values() if a) for r in rows
               if any((r.get("after_switch") or {}).values())]
    fetches = [sum(a["fetches"] for a in (r.get("after_switch") or {}).values() if a) for r in rows
               if any((r.get("after_switch") or {}).values())]
    return {"rounds": len(rows), "successes": successes, "success_rate": successes / len(rows) if rows else None,
            "wilson_95_interval": wilson(successes, len(rows)) if rows else None,
            "rounds_with_reset": len(primary), "primary_per_round": primary,
            "primary_median": statistics.median(primary) if primary else None,
            "fetches_per_round": fetches, "audit_all_correct": sum(bool((r.get("audit_table") or {}).get("all_correct")) for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, default=archive.ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--claude", type=Path, default=shutil.which("claude"))
    parser.add_argument("--config-dir", type=Path, required=True, help="Dedicated CLAUDE_CONFIG_DIR holding only the login")
    parser.add_argument("--workdir", type=Path, required=True, help="Parent of the agents' empty working directories")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--tool-name", default="mcp__agentdb__agentdb", help="Name Claude Code gives the MCP tool")
    parser.add_argument("--child-env", type=json.loads, default={}, help="Extra environment for claude, as JSON")
    parser.add_argument("--client-date", default=datetime.now().date().isoformat())
    parser.add_argument("--runs-per-setup", type=int, default=10)
    parser.add_argument("--pilot", action="store_true", help="Not part of the sample; allows fewer rounds")
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--turn-timeout", type=int, default=900)
    args = parser.parse_args()
    if args.runs_per_setup < 10 and not args.pilot:
        parser.error("Register at least ten rounds per setup, or mark the run --pilot")
    if args.claude is None:
        parser.error("Claude Code not found; pass --claude")
    args.project, args.output, args.workdir = args.project.resolve(), args.output.resolve(), args.workdir.resolve()
    args.claude = args.claude.resolve()
    args.config = (args.config or args.project / "connection.local.json").resolve()
    args.launcher_version = ([args.python, str(args.claude)] if args.claude.suffix == ".py" else [str(args.claude)]) + ["--version"]
    args.output.mkdir(parents=True, exist_ok=False)
    args.workdir.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.project))
    protocol = register(args)
    print(f"registered {args.runs_per_setup} rounds per setup{' (pilot)' if args.pilot else ''}: "
          f"{protocol['model']} / {protocol['reasoning_effort']} / {protocol['claude_code_version']}", flush=True)
    plan = [(n, "A" if n % 2 else "B") for n in range(1, 2 * args.runs_per_setup + 1)]
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = [pool.submit(run_trial, args, n, setup) for n, setup in plan]
        for future in as_completed(pending):
            results.append(future.result())
    results.sort(key=lambda r: r["run"])
    output = {"protocol": "protocol.json", "pilot": args.pilot, "planned_rounds": len(plan), "recorded_rounds": len(results),
              "by_setup": {setup: aggregate(results, setup) for setup in ("A", "B")},
              "rows": [{"run": r["run"], "setup": r["setup"], "status": r["status"], "success": r["success"],
                        "switches": r["switches"], "after_switch": r.get("after_switch"), "audit_table": r.get("audit_table"),
                        "tokens_after_switch": r.get("tokens_after_switch"), "wall_seconds": r["wall_seconds"],
                        "error": r.get("error")} for r in results],
              "real_cloud_verified": False}
    save(args.output / "summary.json", output)
    print(json.dumps(output["by_setup"], indent=2), flush=True)


if __name__ == "__main__":
    main()
