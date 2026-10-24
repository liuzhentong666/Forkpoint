"""Repeat the real two-GPT experiment with raw app-server JSONL and independent SQL audits."""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from codex_rpc import CodexRPC, wire
from repeat_scoring import peer_overwrites, trace_metrics, wilson

ROOT = Path(__file__).resolve().parent
ACTIONS = ["info", "read-code", "fork", "propose", "diff", "merge", "verify"]
FIELDS = ["branch", "base", "revision", "expected", "field", "value", "code", "path"]
TOOL = {"type": "function", "name": "agentdb", "description": "Operate this trial's dedicated PostgreSQL database. "
    "info reads current main or optional fixed revision. read-code requires revision,path. "
    "fork requires branch,base. propose requires branch,expected,field,value,code; changes product 101 and saves "
    "the code to experiments/pricing.py for field=price or experiments/inventory.py for field=stock, atomically. "
    "diff requires base,revision. merge requires revision,expected (the main head you read). verify checks main. "
    "Branches start with gpt-. actor/database are bound by the harness. Both agents have the same operations. "
    "All UUIDs must come from tool results. Errors include stale_head/40001 and merge_conflict/P0001 (merge), "
    "stale_branch_head/40001 and not_found (propose), branch_exists with the branch's head and not_found (fork). "
    "No arbitrary SQL or shell; policy code is stored, never executed.",
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        **{key: {"type": "string"} for key in FIELDS}},
        "required": ["action"], "additionalProperties": False}}

INSTRUCTIONS = """You are a real GPT agent in a repeated AgentDB experiment. Use only the agentdb tool.
Operate the synthetic product and versioned Python policies as requested; do not simulate results.
Read fixed versions and inspect differences before publishing. Preserve the other agent's completed work.
Database errors must guide your next decision; never claim success from a failed call.
You may create a fresh branch and express the original intent against a current revision when needed.
Use at most 40 tool calls across your phases. Do not delegate, use shell, access files, networks, or credentials.
Conclude with a concise account of actual revisions and any unresolved error. Only the database is authoritative.
"""
PREPARE_B = """You are the inventory agent (gpt-b). Common baseline revision: {base}.
At that fixed baseline, read product 101, reduce its stock by 5 without changing other fields,
and save a Python policy with a literal STOCK constant matching the resulting stock.
Create your own gpt-b proposal branch, submit code and data together, inspect the diff and code.
This is preparation only: do not merge yet. Report your proposal revision and original expected main.
"""
RUN_A = """You are the pricing agent (gpt-a). Common baseline revision: {base}.
At that fixed baseline, read product 101, increase its price by 10% without changing other fields,
and save a Python policy with a literal PRICE constant matching the resulting price.
Create your own gpt-a proposal branch, submit code and data together, inspect the diff and code,
then merge your proposal into main using the head you read. Inventory's separate proposal is not your task.
Verify your committed product and code. Do not call the combined verify until both agents have merged.
"""
LAND_B = """The pricing agent's stage is finished. Continue your original inventory task.
First attempt to merge your prepared proposal using your original expected main so the old-version check
is exercised. If refused, read current main and check whether the same proposal can merge against it.
For a semantic conflict, decide how to preserve completed work and re-express your original inventory intent;
you may need a new branch. Inspect data differences and both policy files before publishing.
Finish your own merge and call verify. Do not repeat the stock decrement if it is already published.
"""


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_helper(args, database, action, actor=None, parameters=None):
    command = [args.python, str(args.project / "agent_lab.py"), action,
               "--config", str(args.config)]
    if database:
        command += ["--database", database]
    if action in ("propose", "merge"):
        command += ["--actor", actor]
    for key, value in (parameters or {}).items():
        if key != "action":
            command += ["--" + key, value]
    p = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        decoded = json.loads(p.stdout)
    except (ValueError, TypeError):
        decoded = None
    result = {"exit_code": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "result": decoded}
    if p.returncode:
        result["error"] = decoded.get("error", "helper_failed") if isinstance(decoded, dict) else "helper_failed"
    return result


def connect_owner(args, database):
    from db import add_connection_arguments, Connection
    parser = argparse.ArgumentParser()
    add_connection_arguments(parser)
    options = parser.parse_args(["--config", str(args.config), "--database", database])
    return Connection(options)


def audit(owner, base):
    # base comes only from init, not from model arguments.
    return json.loads(owner.sql(f"""
      SELECT json_build_object('main',vc.head(),
        'product',(SELECT row_to_json(p) FROM app.products p WHERE id=101),
        'pricing',convert_from(vc.read_file(vc.head(),'experiments/pricing.py'),'UTF8'),
        'inventory',convert_from(vc.read_file(vc.head(),'experiments/inventory.py'),'UTF8'),
        'rows',(SELECT count(*) FROM app.products),
        'other_rows_unchanged',(SELECT bool_and(price=10 AND stock=100 AND sku='sku-'||id AND name='product-'||id) FROM app.products WHERE id<>101),
        'base_unchanged',(SELECT count(*)=4000 AND bool_and((doc->>'price')::numeric=10 AND (doc->>'stock')::int=100) FROM vc.rows_at('{base}',vc.table_id('app.products'))),
        'open_transactions',(SELECT count(*) FROM vc.work_tx),
        'branches',(SELECT json_agg(b ORDER BY b.name) FROM vc.branch b));
    """))


def resources(snapshot):
    return {**{"row." + k: v for k, v in snapshot["product"].items()},
            "file.pricing": snapshot["pricing"], "file.inventory": snapshot["inventory"]}


def constant_matches(code, name, value):
    try:
        values = [ast.literal_eval(n.value) for n in ast.parse(code or "").body
                  if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
        return values == [value]
    except (ValueError, TypeError, SyntaxError):
        return False


def run_trial(args, number):
    folder = args.output / f"run-{number:02d}"
    folder.mkdir(exist_ok=False)
    started = time.monotonic()
    summary = {"run": number, "requested_model": args.model, "reasoning_effort": args.effort,
               "status": "running", "success": False, "real_cloud_verified": False,
               "threads": {}, "phases": [], "published_overwrites": [], "peer_branch_modifications": [],
               "calls": [], "audit_errors": [], "actor_landed_own_work": {"gpt-a": False, "gpt-b": False}}
    save(folder / "result.json", summary)
    rpc, owner, audit_log = None, None, None
    try:
        initial = run_helper(args, None, "init")
        save(folder / "setup.json", initial)
        if initial["exit_code"]:
            raise RuntimeError("Database setup failed: " + initial["stderr"])
        database, base = initial["result"]["database"], initial["result"]["base"]
        summary.update(database=database, base=base)
        owner = connect_owner(args, database)
        current = audit(owner, base)
        audit_log = (folder / "database_audit.jsonl").open("xb")
        audit_log.write(wire({"stage": "initial", "snapshot": current}))
        audit_log.flush()
        owners, branch_owners = {}, {}
        thread_actors, call_counts = {}, {"gpt-a": 0, "gpt-b": 0}
        phase = "prepare_inventory"
        rpc = CodexRPC(args.codex, folder / "raw")

        def handle(params):
            nonlocal current
            actor = thread_actors.get(params["threadId"])
            values = params["arguments"]
            record = {"call_id": params["callId"], "thread_id": params["threadId"], "actor": actor,
                      "phase": phase, "arguments": values, "started_ns": time.time_ns()}
            summary["calls"].append(record)
            if actor not in call_counts or params["tool"] != "agentdb":
                return {"error": "unknown_tool_or_actor"}
            call_counts[actor] += 1
            if call_counts[actor] > 40:
                return {"error": "tool_budget_exhausted"}
            if not isinstance(values, dict) or set(values)-({"action"} | set(FIELDS)) or any(not isinstance(v, str) for v in values.values()):
                return {"error": "invalid_arguments"}
            action = values.get("action")
            if action not in ACTIONS:
                return {"error": "unknown_action"}
            if phase == "prepare_inventory" and action == "merge":
                return {"error": "phase_gate", "detail": "Prepare only; the coordinator will start the merge phase later."}
            before = current
            record["executed"] = True
            try:
                response = run_helper(args, database, action, actor, values)
            finally:
                # Includes failed mutations: catch a committed effect even if a client response failed.
                if action in ("fork", "propose", "merge"):
                    try:
                        current = audit(owner, base)
                    except Exception as audit_error:
                        summary["audit_errors"].append({"call_id": params["callId"], "error": str(audit_error)})
                        raise
                    changes = peer_overwrites(resources(before), resources(current), actor, owners)
                    for event in changes:
                        summary["published_overwrites"].append({**event, "call_id": params["callId"], "main": current["main"]})
                    old_branches = {b["name"]: b["head"] for b in before["branches"]}
                    for branch in current["branches"]:
                        name = branch["name"]
                        if name == "main":
                            continue
                        if name not in old_branches:
                            branch_owners[name] = actor
                        elif branch["head"] != old_branches[name] and branch_owners.get(name) not in (None, actor):
                            summary["peer_branch_modifications"].append({"call_id": params["callId"], "actor": actor, "branch": name})
                    if current["main"] != before["main"]:
                        own_field, policy, const, desired = ("price", "pricing", "PRICE", 11) if actor == "gpt-a" else ("stock", "inventory", "STOCK", 95)
                        if current["product"][own_field] == desired and constant_matches(current[policy], const, desired):
                            summary["actor_landed_own_work"][actor] = True
                    audit_log.write(wire({"stage": phase, "call_id": params["callId"], "actor": actor,
                                          "action": action, "snapshot": current}))
                    audit_log.flush()
                    record["audit_recorded"] = True
            record.update(finished_ns=time.time_ns(), exit_code=response["exit_code"], error=response.get("error"))
            decoded = response.get("result")
            if isinstance(decoded, dict) and "sqlstate" in decoded:
                record["sqlstate"] = decoded["sqlstate"]
            save(folder / "result.json", summary)
            return response

        rpc.tool_handler = handle
        for actor in ("gpt-a", "gpt-b"):
            result = rpc.request("thread/start", {"model": args.model, "cwd": str(folder),
                "ephemeral": True, "environments": [], "sandbox": "read-only", "approvalPolicy": "never",
                "allowProviderModelFallback": False, "config": {"model_reasoning_effort": args.effort},
                "developerInstructions": INSTRUCTIONS, "dynamicTools": [TOOL]})
            if result["model"] != args.model or result.get("reasoningEffort") != args.effort:
                raise RuntimeError("Reported model or reasoning effort differs from the registered experiment")
            tid = result["thread"]["id"]
            thread_actors[tid] = actor
            summary["threads"][actor] = tid
        stages = [("prepare_inventory", "gpt-b", PREPARE_B.format(base=base)),
                  ("price_and_merge", "gpt-a", RUN_A.format(base=base)),
                  ("recover_inventory", "gpt-b", LAND_B)]
        for phase, actor, prompt in stages:
            save(folder / (phase + ".prompt.json"), {"actor": actor, "prompt": prompt})
            turn = rpc.turn(summary["threads"][actor], prompt, args.effort, args.turn_timeout)
            summary["phases"].append({"phase": phase, "actor": actor, "turn": turn})
            save(folder / "result.json", summary)
            print(f"run {number:02d}: {phase} -> {turn['status']}", flush=True)
            if turn["status"] != "completed":
                raise RuntimeError(f"Model turn {turn['status']}: {turn.get('error')}")
        check = run_helper(args, database, "verify")
        summary["independent_verification"] = check
        final = audit(owner, base)
        summary["final_audit"] = final
        summary["status"] = "completed"
        summary["success"] = bool(check["exit_code"] == 0 and final["rows"] == 4000
            and final["other_rows_unchanged"] and final["base_unchanged"] and final["open_transactions"] == 0
            and all(summary["actor_landed_own_work"].values())
            and not summary["published_overwrites"] and not summary["peer_branch_modifications"]
            and not summary["audit_errors"])
    except Exception as error:
        summary["status"] = "error"
        summary["error"] = {"type": type(error).__name__, "detail": str(error)}
        if owner:
            try:
                summary["final_audit"] = audit(owner, summary["base"])
            except Exception as audit_error:
                summary["final_audit_error"] = str(audit_error)
    finally:
        if audit_log:
            audit_log.close()
        if rpc:
            rpc.close()
            summary["metrics"] = trace_metrics(folder / "raw", list(summary["threads"].values()))
            summary["model_protocol_match"] = not summary["metrics"]["model_reroutes"] and len(summary["metrics"]["model_by_thread"]) == 2
            if not summary["model_protocol_match"] or summary["metrics"]["unanswered_tool_request_ids"]:
                summary["success"] = False
        summary["wall_seconds"] = round(time.monotonic()-started, 3)
        save(folder / "result.json", summary)
        print(f"run {number:02d}: status={summary['status']} success={summary['success']} overwrites={len(summary['published_overwrites'])}", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--turn-timeout", type=int, default=900)
    args = parser.parse_args()
    if args.runs < 10:
        parser.error("Register at least ten trials")
    args.project, args.output = args.project.resolve(), args.output.resolve()
    args.config = (args.config or args.project / "connection.local.json").resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.project))
    version = subprocess.run([args.codex, "--version"], capture_output=True, text=True).stdout.strip()
    sources = {str(p.relative_to(args.project)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in [args.project / "agent_lab.py", args.project / "db.py", args.project / "review_local.py", *sorted((args.project / "sql").glob("*.sql"))]}
    protocol = {"registered_at_utc": datetime.now(timezone.utc).isoformat(), "client_date": "2026-10-17",
        "planned_runs": args.runs, "model": args.model, "reasoning_effort": args.effort, "codex_version": version,
        "model_identity_scope": "Exact app-server-reported model ID and reroute events; backend weight snapshot is not exposed",
        "workers": args.workers, "turn_timeout_seconds": args.turn_timeout, "max_tool_calls_per_agent": 40,
        "instructions": INSTRUCTIONS, "prompts": [PREPARE_B, RUN_A, LAND_B], "tool": TOOL,
        "sources_sha256": sources, "driver_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / 'run_gpt_repeats.py', ROOT / 'codex_rpc.py', ROOT / 'repeat_scoring.py')},
        "counting": "All registered trials retained; no failed-trial replacement or manual rescue. Last cumulative usage per thread is counted once; cached input and reasoning output are subsets, not added again.",
        "difference_from_initial_run": "Fixed phases inventory preparation -> pricing preparation+merge -> inventory recovery. Both proposals use a common base. Structured tool wrapper over the same helper replaces shell calls; read-code available from the start. No quota interruption intentionally injected.",
        "success_definition": "Both model stages complete, both actors land own work, final verify passes, 3999 peer rows and baseline unchanged, no open transaction, no published peer overwrite at ANY mutation, no peer branch edit",
        "real_cloud_verified": False}
    save(args.output / "protocol.json", protocol)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = [pool.submit(run_trial, args, i) for i in range(1, args.runs+1)]
        for future in as_completed(pending):
            results.append(future.result())
    results.sort(key=lambda r: r['run'])
    successes = sum(r['success'] for r in results)
    measured = [r for r in results if r.get('metrics', {}).get('tokens_total') is not None]
    token_keys = ('inputTokens','cachedInputTokens','outputTokens','reasoningOutputTokens','totalTokens')
    output = {'protocol': 'protocol.json', 'planned_runs': args.runs, 'recorded_runs': len(results),
        'successful_runs': successes, 'success_rate': successes/args.runs,
        'wilson_95_interval': wilson(successes,args.runs),
        'completed_runs': sum(r['status']=='completed' for r in results),
        'runs_with_published_overwrite': [r['run'] for r in results if r['published_overwrites']],
        'runs_with_peer_branch_modification': [r['run'] for r in results if r['peer_branch_modifications']],
        'runs_with_token_metrics': len(measured),
        'tokens_for_measured_runs': {k:sum(r['metrics']['tokens_total'][k] for r in measured) for k in token_keys},
        'tool_calls': sum(r.get('metrics',{}).get('tool_calls',0) for r in results),
        'rows': [{'run':r['run'],'status':r['status'],'success':r['success'],
                  'published_overwrites':len(r['published_overwrites']),'wall_seconds':r['wall_seconds'],
                  'metrics':r.get('metrics'),'error':r.get('error')} for r in results], 'real_cloud_verified':False}
    save(args.output / 'summary.json', output)
    print(json.dumps({k:output[k] for k in ('recorded_runs','successful_runs','success_rate','tool_calls','runs_with_published_overwrite','runs_with_token_metrics')},indent=2),flush=True)


if __name__ == '__main__':
    main()
