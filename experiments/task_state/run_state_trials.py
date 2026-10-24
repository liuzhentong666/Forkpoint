"""Run the checkable task state experiment: do freshness marks stop decisions on stale facts?

The harness is experiments/context_archive/run_archive_trials.py's (Claude Code headless, one MCP
tool, agent_lab.py, an SQL audit after every write, every line archived), with this task:
- product 101 is restocked from product 202: its stock must be product 202's stock minus 5;
- gpt-b prepares at the baseline (202 has 100, so 95) and checkpoints what it will need;
- gpt-a raises product 101's price (its unchanged task), so gpt-b's merge needs a new proposal;
- the runner then commits a warehouse update on main: product 202's stock becomes 60;
- gpt-b resumes and lands its change; correct is 55, a decision on the stale fact gives 95.
Both setups get the same tool: agent_lab's actions without verify (it checks the old task's
95), plus read-product, checkpoint and resume (task_state.py), answered by the runner. Task
state lives in the runner's MemoryStore, so it survives agent resets. The only difference:
in setup F resume marks each fact fresh, stale or unknown; in setup U it does not.
An agent is reset whenever a request's input first passes LIMIT (archive.LIMIT_TOKENS), at
most MAX_RESETS times; its new process gets the stage's instructions and its resume packet.
Rounds alternate U, F. --claude may name a .py stand-in (fake_state_claude.py) for a dry run.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import statistics
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "experiments" / "context_archive"))

import archive  # noqa: E402
from archive import ArchiveError, CONTINUE, Writer  # noqa: E402
import run_archive_trials as base_runner  # noqa: E402
from run_archive_trials import (ArchiveTrial, CALL_BUDGET, INSTRUCTIONS, Round, after_switch,  # noqa: E402
                                combined_metrics, source_hash, subprocess_output, usage_after_switch)
from task_state import hex_text, tool_reply  # noqa: E402
from claude_stream import child_environment  # noqa: E402
from codex_rpc import wire  # noqa: E402
from repeat_scoring import peer_overwrites, wilson  # noqa: E402
from run_claude_repeats import ACTORS, Coordinator, check_init, claude_command, save, sha256  # noqa: E402
from run_gpt_repeats import ACTIONS, FIELDS, RUN_A, TOOL, connect_owner, constant_matches, resources, run_helper  # noqa: E402
from agent_state.engine import Engine  # noqa: E402
from agent_state.storage import MemoryStore  # noqa: E402

SOURCE = 202            # the product whose stock decides product 101's
START, UPDATED = 100, 60  # its stock at the baseline and after the warehouse update
TARGET, STALE = UPDATED - 5, START - 5  # correct final stock of 101, and the stale decision's
LIMIT = archive.LIMIT_TOKENS  # a request with more input resets the agent (tests lower it)
MAX_RESETS = 3          # per agent and round; no trigger after the last
STATE_BYTES = 8192      # resume packets, for calls and handoffs
SETUPS = ("U", "F")     # U: no freshness marks; F: marks

PREPARE_S = """You are the inventory agent (gpt-b). Common baseline revision: {base}.
Product 101 is restocked from product 202: product 101's stock must equal product 202's current stock minus 5.
At that fixed baseline, read products 101 and 202, propose product 101's stock by that rule without changing other fields,
and save a Python policy with a literal STOCK constant matching the resulting stock.
Create your own gpt-b proposal branch, submit code and data together, inspect the diff and code.
This is preparation only: do not merge yet. Before you finish, checkpoint what you will need later: your proposal revision,
the original expected main, and each fact your stock value depends on, with the revision and rows it was read from.
"""
LAND_S = """The pricing agent's stage is finished. Continue your original inventory task: start by resuming your saved task state.
Land product 101's stock change on main, following the rule, with code and data together; you may need a new branch.
"""
HEADER_S = """Your context was reset because it reached its size limit. Your instructions for the current stage were:
{prompt}
Your saved task state, as resume returns it now:
{state}
"""
STATE_ACTIONS = ("read-product", "checkpoint", "resume")
ACTIONS_S = ["info", "read-product"] + [a for a in ACTIONS if a not in ("info", "verify")] + ["checkpoint", "resume"]
TOOL_S = {"type": "function", "name": TOOL["name"], "description": (
    "Operate this trial's dedicated PostgreSQL database. info reads product 101 at current main or an optional fixed "
    "revision. read-product requires product (an id) and revision, and reads that product at that revision. "
    "read-code requires revision,path. fork requires branch,base. propose requires branch,expected,field,value,code; "
    "changes product 101 and saves the code to experiments/pricing.py for field=price or experiments/inventory.py for "
    "field=stock, atomically. diff requires base,revision. merge requires revision,expected (the main head you read). "
    "checkpoint requires change and saves your task state: {goal, next_action, open_items, facts: {name: {value, "
    'based_on: {revision, rows: {"app.products": [{"id": N}]}}}}, drop}; based_on is optional. resume returns your '
    "saved task state. Branches start with gpt-. actor/database are bound by the harness. Both agents have the same "
    "operations. All UUIDs must come from tool results. Errors include stale_head/40001 and merge_conflict/P0001. "
    "No arbitrary SQL or shell; policy code is stored, never executed."),
    "inputSchema": {"type": "object", "properties": {
        **{k: v for k, v in TOOL["inputSchema"]["properties"].items()},
        "action": {"type": "string", "enum": ACTIONS_S},
        "product": {"type": "string"},
        "change": {"type": "object"}},
        "required": ["action"], "additionalProperties": False}}
SOURCES = tuple(p for p in base_runner.SOURCES if p != "docs/CONTEXT_ARCHIVE_EXPERIMENT.md") + (
    "experiments/task_state/task_state.py", "experiments/task_state/run_state_trials.py",
    "agent_state/engine.py", "agent_state/runtime.py", "agent_state/heads.py", "docs/TASK_STATE_EXPERIMENT.md")


def state_audit(owner, base):
    """run_gpt_repeats.audit for this task: product 202 is reported, and left out of the unchanged rows."""
    return json.loads(owner.sql(f"""
      SELECT json_build_object('main',vc.head(),
        'product',(SELECT row_to_json(p) FROM app.products p WHERE id=101),
        'source',(SELECT row_to_json(p) FROM app.products p WHERE id={SOURCE}),
        'pricing',convert_from(vc.read_file(vc.head(),'experiments/pricing.py'),'UTF8'),
        'inventory',convert_from(vc.read_file(vc.head(),'experiments/inventory.py'),'UTF8'),
        'rows',(SELECT count(*) FROM app.products),
        'other_rows_unchanged',(SELECT bool_and(price=10 AND stock=100 AND sku='sku-'||id AND name='product-'||id)
          FROM app.products WHERE id NOT IN (101,{SOURCE})),
        'base_unchanged',(SELECT count(*)=4000 AND bool_and((doc->>'price')::numeric=10 AND (doc->>'stock')::int=100)
          FROM vc.rows_at('{base}',vc.table_id('app.products'))),
        'open_transactions',(SELECT count(*) FROM vc.work_tx),
        'branches',(SELECT json_agg(b ORDER BY b.name) FROM vc.branch b));
    """))


def commit_row(owner, product, stock, note):
    return owner.sql(f"""BEGIN;
SELECT vc.begin_change('main',vc.head(),'warehouse',{hex_text(note)}) AS draft \\gset
UPDATE app.products SET stock={int(stock)} WHERE id={int(product)};
SELECT vc.finish_change() AS revision \\gset
COMMIT;
SELECT :'revision';""").splitlines()[-1]


def outcome(final):
    """What gpt-b landed on main: correct (TARGET, with a matching STOCK), stale (STALE) or other."""
    stock = (final.get("product") or {}).get("stock")
    if stock == TARGET and constant_matches(final.get("inventory"), "STOCK", TARGET):
        return "correct"
    return "stale" if stock == STALE else "other"


class StateTrial(ArchiveTrial):
    """ArchiveTrial with this experiment's tool: read-product, checkpoint and resume are answered here,
    verify is refused, and gpt-b's own work is the TARGET stock."""

    def __init__(self, args, folder, summary, setup):
        super().__init__(args, folder, summary, setup)
        self.engine, self.state_sql = Engine(MemoryStore()), None

    def fetch(self, actor, values, record):
        """The runner's own actions (the archive runner's hook for setup B's fetch); None for the others."""
        if not isinstance(values, dict) or values.get("action") not in STATE_ACTIONS:
            return None
        if set(values) - ({"action", "product", "change"} | set(FIELDS)):
            reply = {"error": "invalid_arguments"}
        else:
            record["executed"] = True
            try:
                reply = (self.read_product(values) if values["action"] == "read-product" else
                         tool_reply(self.engine, self.state_sql, actor, actor, record["call_id"], values,
                                    check=self.setup == "F", max_bytes=STATE_BYTES))
            except Exception as error:
                self.record_failure(record, error)
                raise
        record.update(finished_ns=time.time_ns(), exit_code=2 if "error" in reply else 0, error=reply.get("error"))
        save(self.folder / "result.json", self.summary)
        return reply

    def read_product(self, values):
        product, revision = values.get("product"), values.get("revision")
        if not isinstance(product, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", product) or int(product) > 4000:
            return {"error": "invalid_arguments", "detail": "product must be an id from 1 to 4000"}
        try:
            revision = str(uuid.UUID(revision))
        except (ValueError, TypeError, AttributeError):
            return {"error": "invalid_arguments", "detail": "read-product requires a revision UUID"}
        found = json.loads(self.state_sql(f"""SELECT json_build_object('main',vc.head(),'known',
          EXISTS(SELECT 1 FROM vc.changeset WHERE id='{revision}'),
          'doc',(SELECT doc FROM vc.rows_at('{revision}',vc.table_id('app.products')) WHERE pk='{{"id":{int(product)}}}'));"""))
        if not found["known"]:
            return {"error": "not_found", "detail": "unknown revision"}
        return {"exit_code": 0, "result": {"product": found["doc"], "read_revision": revision, "current_main": found["main"]}}

    def check_arguments(self, values):
        if isinstance(values, dict) and values.get("action") == "verify":
            return {"error": "unknown_action"}
        return super().check_arguments(values)

    def audit_after(self, record, actor, action, before):
        """Trial.audit_after with this task's audit and own-work targets (gpt-a price 11, gpt-b stock TARGET)."""
        summary, call_id = self.summary, record["call_id"]
        try:
            self.current = current = state_audit(self.owner, self.base)
        except Exception as audit_error:
            summary["audit_errors"].append({"call_id": call_id, "error": str(audit_error)})
            raise
        for event in peer_overwrites(resources(before), resources(current), actor, self.owners):
            summary["published_overwrites"].append({**event, "call_id": call_id, "main": current["main"]})
        old_branches = {b["name"]: b["head"] for b in before["branches"]}
        for branch in current["branches"]:
            name = branch["name"]
            if name == "main":
                continue
            if name not in old_branches:
                self.branch_owners[name] = actor
            elif branch["head"] != old_branches[name] and self.branch_owners.get(name) not in (None, actor):
                summary["peer_branch_modifications"].append({"call_id": call_id, "actor": actor, "branch": name})
        if current["main"] != before["main"]:
            field, policy, const, desired = (("price", "pricing", "PRICE", 11) if actor == "gpt-a"
                                             else ("stock", "inventory", "STOCK", TARGET))
            if current["product"][field] == desired and constant_matches(current[policy], const, desired):
                summary["actor_landed_own_work"][actor] = True
        self.audit_log.write(wire({"stage": self.phase, "call_id": call_id, "actor": actor,
                                   "action": action, "snapshot": current}))
        self.audit_log.flush()
        record["audit_recorded"] = True


class StateRound(Round):
    """Every process serves this experiment's tool, in both setups and before and after a reset."""

    def start(self, actor, limit=None, fetch=True):
        return super().start(actor, limit, fetch=True)


def switch(args, trial, rnd, actor, agent, boundary, prompt):
    """Replace actor's process by a fresh one that starts from the stage's instructions and its resume packet."""
    with trial.lock:
        number = sum(s["actor"] == actor for s in trial.summary["switches"]) + 1
        trial.switching.add(actor)
        record = {"actor": actor, "phase": trial.phase, "number": number, "setup": trial.setup,
                  "after_entry": boundary, "request_tokens": agent.trigger.crossed,
                  "call_sequence": trial.sequence, "started_ns": time.time_ns()}
        trial.summary["switches"].append(record)
    agent.interrupt(args.turn_timeout)
    agent.close()
    rnd.switched.add((rnd.contexts[actor][-1], actor))
    rnd.writer.flush()
    with trial.lock:
        reply = tool_reply(trial.engine, trial.state_sql, actor, actor, f"handoff-{number}", {"action": "resume"},
                           check=trial.setup == "F", max_bytes=STATE_BYTES)
    packet = reply.get("result")
    state = json.dumps(packet, ensure_ascii=False, sort_keys=True) if packet else "(nothing saved yet)"
    handoff = HEADER_S.format(prompt=prompt.rstrip("\n"), state=state) + CONTINUE
    data = handoff.encode("utf-8")
    (trial.folder / f"handoff.{actor}.{number}.txt").write_bytes(data)
    marks = [m["status"] for m in ((packet or {}).get("freshness") or {}).values()]
    record.update(handoff_bytes=len(data), handoff_sha256=hashlib.sha256(data).hexdigest(), finished_ns=time.time_ns(),
                  state_saved=packet is not None, facts=len((packet or {}).get("facts") or {}),
                  marks={s: marks.count(s) for s in ("fresh", "stale", "unknown") if s in marks})
    fresh = rnd.start(actor, limit=LIMIT if number < MAX_RESETS else None)
    trial.summary["agents"][actor]["processes"].append({"context": rnd.contexts[actor][-1], "pid": fresh.process.pid})
    with trial.lock:
        trial.switching.discard(actor)
    save(trial.folder / "result.json", trial.summary)
    return fresh, handoff


def source_rereads(summary, after_sequence):
    """How many successful reads of product SOURCE gpt-b made after the warehouse update."""
    reads = [c for c in summary["calls"] if c["actor"] == "gpt-b" and c["sequence"] > after_sequence
             and (c.get("arguments") or {}).get("action") == "read-product"
             and (c.get("arguments") or {}).get("product") == str(SOURCE) and not c.get("error")]
    return len(reads)


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
    trial = StateTrial(args, folder, summary, setup)
    server = writer = reader = rnd = None
    agents = {}
    try:
        initial = run_helper(args, None, "init")
        save(folder / "setup.json", initial)
        if initial["exit_code"]:
            raise RuntimeError("Database setup failed: " + initial["stderr"])
        trial.database = summary["database"] = initial["result"]["database"]
        trial.owner = connect_owner(args, trial.database)
        trial.base = summary["base"] = initial["result"]["base"]
        trial.owner.sql((archive.HERE / "archive.sql").read_text(encoding="utf-8"))
        trial.current = state_audit(trial.owner, trial.base)
        trial.audit_log = (folder / "database_audit.jsonl").open("xb")
        trial.audit_log.write(wire({"stage": "initial", "snapshot": trial.current}))
        trial.audit_log.flush()
        writer = Writer(lambda: trial.owner.session(trial.database))
        reader = trial.owner.session(trial.database)
        trial.state_sql = reader.sql
        server = Coordinator(trial)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tool_file = folder / "tool.json"
        save(tool_file, TOOL_S)
        tokens = {actor: secrets.token_hex(16) for actor in ACTORS}
        trial.tokens.update({token: actor for actor, token in tokens.items()})
        rnd = StateRound(args, folder, raw, workdir, server.server_address[1], tokens, writer, tool_file)
        for actor in ACTORS:
            agents[actor] = rnd.start(actor, limit=LIMIT)
            summary["agents"][actor] = {"processes": [{"context": "ctx-1", "pid": agents[actor].process.pid}]}
        stages = [("prepare_inventory", "gpt-b", PREPARE_S.format(base=trial.base)),
                  ("price_and_merge", "gpt-a", RUN_A.format(base=trial.base)),
                  ("warehouse_update", None, None),
                  ("recover_inventory", "gpt-b", LAND_S)]
        for phase, actor, prompt in stages:
            trial.phase = phase
            if actor is None:
                with trial.lock:
                    summary["warehouse_update"] = {"revision": commit_row(trial.owner, SOURCE, UPDATED, f"product {SOURCE} stock {UPDATED}"),
                                                   "after_sequence": trial.sequence}
                    trial.current = state_audit(trial.owner, trial.base)
                continue
            save(folder / (phase + ".prompt.json"), {"actor": actor, "prompt": prompt})
            kind, value = agents[actor].turn(prompt, args.turn_timeout)
            while kind == "switch":  # value: entries archived up to the boundary
                agents[actor], handoff = switch(args, trial, rnd, actor, agents[actor], value, prompt)
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
        final = summary["final_audit"] = state_audit(trial.owner, trial.base)
        summary["outcome"] = outcome(final)
        summary["status"] = "completed"
        summary["success"] = bool(final["rows"] == 4000 and final["other_rows_unchanged"] and final["base_unchanged"]
            and final["open_transactions"] == 0 and (final["source"] or {}).get("stock") == UPDATED
            and summary["outcome"] == "correct" and all(summary["actor_landed_own_work"].values())
            and not summary["published_overwrites"] and not summary["peer_branch_modifications"]
            and not summary["audit_errors"])
    except Exception as error:
        summary["status"] = "archive_error" if isinstance(error, ArchiveError) else "error"
        summary["success"] = False
        summary["error"] = {"type": type(error).__name__, "detail": str(error)}
        if trial.owner and trial.base:
            try:
                summary["final_audit"] = state_audit(trial.owner, trial.base)
                summary["outcome"] = outcome(summary["final_audit"])
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
        if "warehouse_update" in summary:
            summary["source_rereads_after_update"] = source_rereads(summary, summary["warehouse_update"]["after_sequence"])
        summary["wall_seconds"] = round(time.monotonic() - started, 3)
        save(folder / "result.json", summary)
        print(f"run {number:02d} ({setup}): status={summary['status']} success={summary['success']} "
              f"outcome={summary.get('outcome')} resets={len(summary['switches'])}", flush=True)
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
        "pilot": args.pilot, "runs_per_setup": args.runs_per_setup, "order": "rounds alternate U, F: odd runs U, even runs F",
        "model": args.model, "reasoning_effort": args.effort, "claude_code_version": version,
        "claude_executable": str(args.claude), "claude_executable_sha256": sha256(args.claude), "account": account,
        "workers": args.workers, "turn_timeout_seconds": args.turn_timeout, "max_tool_calls_per_agent": CALL_BUDGET,
        "instructions": INSTRUCTIONS,
        "prompts": {"prepare_inventory": PREPARE_S, "price_and_merge": RUN_A, "recover_inventory": LAND_S},
        "task": {"source_product": SOURCE, "source_stock_at_base": START, "source_stock_after_update": UPDATED,
                 "correct_stock": TARGET, "stale_stock": STALE,
                 "warehouse_update": "after price_and_merge the runner commits product 202's stock 60 on main"},
        "handoff": {"header": HEADER_S, "continue": CONTINUE, "state_bytes": STATE_BYTES},
        "constants": {"limit_tokens": LIMIT, "max_resets_per_agent": MAX_RESETS},
        "setups": {"U": "resume without freshness marks", "F": "resume with a fresh, stale or unknown mark per fact"},
        "tool": TOOL_S, "tool_name_seen_by_model": args.tool_name,
        "claude_command": claude_command(args, Path("<run>/<actor>.<ctx>.mcp-config.json"), INSTRUCTIONS),
        "child_environment": {"CLAUDE_CONFIG_DIR": str(args.config_dir), **{
            k: v for k, v in child_environment(args.config_dir, args.child_env).items() if k.startswith(("CLAUDE", "DISABLE"))}},
        "agent_working_directory": str(args.workdir) + "/run-NN (empty, outside any git repository, no CLAUDE.md or AGENTS.md)",
        "sources_sha256": {p: source_hash(args.project / p) for p in SOURCES},
        "success_definition": "Both model stages complete; main has product 101 at stock 55 with STOCK = 55 and price 11 "
                              "with PRICE = 11 (both actors land own work), product 202 at 60, every other row and the "
                              "baseline unchanged, no open transaction, no published peer overwrite or peer branch edit, "
                              "every API response from the registered model, every MCP request answered, no archive error.",
        "primary_metric": "Per round, whether gpt-b landed the stale decision: product 101's stock 95 on main at the end.",
        "counting": "All registered rounds kept; no replacement of failed rounds; no manual rescue.",
        "real_cloud_verified": False}
    save(args.output / "protocol.json", protocol)
    return protocol


def aggregate(results, setup):
    rows = [r for r in results if r["setup"] == setup]
    successes = sum(r["success"] for r in rows)
    resets = [len(r["switches"]) for r in rows]
    rereads = [r["source_rereads_after_update"] for r in rows if "source_rereads_after_update" in r]
    return {"rounds": len(rows), "successes": successes, "success_rate": successes / len(rows) if rows else None,
            "wilson_95_interval": wilson(successes, len(rows)) if rows else None,
            "outcomes": {o: sum(r.get("outcome") == o for r in rows) for o in ("correct", "stale", "other")},
            "resets_per_round": resets, "source_rereads_per_round": rereads,
            "source_rereads_median": statistics.median(rereads) if rereads else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, default=ROOT)
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
    protocol = register(args)
    print(f"registered {args.runs_per_setup} rounds per setup{' (pilot)' if args.pilot else ''}: "
          f"{protocol['model']} / {protocol['reasoning_effort']} / {protocol['claude_code_version']}", flush=True)
    plan = [(n, SETUPS[(n - 1) % 2]) for n in range(1, 2 * args.runs_per_setup + 1)]
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = [pool.submit(run_trial, args, n, setup) for n, setup in plan]
        for future in as_completed(pending):
            results.append(future.result())
    results.sort(key=lambda r: r["run"])
    output = {"protocol": "protocol.json", "pilot": args.pilot, "planned_rounds": len(plan), "recorded_rounds": len(results),
              "by_setup": {setup: aggregate(results, setup) for setup in SETUPS},
              "rows": [{"run": r["run"], "setup": r["setup"], "status": r["status"], "success": r["success"],
                        "outcome": r.get("outcome"), "switches": r["switches"],
                        "source_rereads_after_update": r.get("source_rereads_after_update"),
                        "tokens_after_switch": r.get("tokens_after_switch"), "wall_seconds": r["wall_seconds"],
                        "error": r.get("error")} for r in results],
              "real_cloud_verified": False}
    save(args.output / "summary.json", output)
    print(json.dumps(output["by_setup"], indent=2), flush=True)


if __name__ == "__main__":
    main()
