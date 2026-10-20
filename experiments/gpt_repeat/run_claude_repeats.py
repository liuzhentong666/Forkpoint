"""Repeat the two-agent experiment with Claude Code headless agents, raw stream-json logs and SQL audits.

The task, prompts, tool, helper, audit SQL and success rule are imported unchanged from
run_gpt_repeats.py. Only the model runner differs: each agent is one `claude -p` process
(stream-json in and out) whose only tool is the agentdb MCP server in agentdb_mcp_server.py.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time

from claude_scoring import add_tokens, trace_metrics
from claude_stream import ClaudeAgent, child_environment
from codex_rpc import wire
from repeat_scoring import peer_overwrites, wilson
from run_gpt_repeats import (ACTIONS, FIELDS, INSTRUCTIONS, LAND_B, PREPARE_B, RUN_A, TOOL,
                             audit, connect_owner, constant_matches, resources, run_helper)

ROOT = Path(__file__).resolve().parent
ACTORS = ("gpt-a", "gpt-b")  # agent_lab.py actor and branch names; labels bound by the harness
# The only text difference from the GPT run: the instructions name the model family.
CLAUDE_INSTRUCTIONS = INSTRUCTIONS.replace("You are a real GPT agent", "You are a real Claude agent", 1)
assert CLAUDE_INSTRUCTIONS != INSTRUCTIONS
DRIVERS = ("run_claude_repeats.py", "claude_stream.py", "agentdb_mcp_server.py", "claude_scoring.py",
           "repeat_scoring.py", "run_gpt_repeats.py", "codex_rpc.py")


def save(path, value):
    Path(path).write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CoordinatorHandler(socketserver.StreamRequestHandler):
    def handle(self):
        trial = self.server.trial
        try:
            message = json.loads(self.rfile.readline())
            actor = trial.tokens.get(message.get("token"))
            result = trial.handle(actor, message.get("params") or {})
        except Exception as error:
            # Same shape the GPT transport sent back when its handler raised.
            result = {"error": type(error).__name__, "detail": str(error)}
        self.wfile.write(wire({"result": result}))


class Coordinator(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, trial):
        self.trial = trial
        super().__init__(("127.0.0.1", 0), CoordinatorHandler)


class Trial:
    """Per-round tool handler: identical gating, budget, helper call and audit to the GPT run."""

    def __init__(self, args, folder, summary):
        self.args, self.folder, self.summary = args, folder, summary
        self.lock = threading.Lock()
        self.tokens = {}
        self.phase = "prepare_inventory"
        self.sequence = 0
        self.owners, self.branch_owners = {}, {}
        self.call_counts = {actor: 0 for actor in ACTORS}
        self.database = self.base = self.owner = self.current = self.audit_log = None

    def handle(self, actor, params):
        with self.lock:  # one tool call at a time per round, like the GPT app-server loop
            self.sequence += 1
            values = params.get("arguments")
            record = {"call_id": f"{self.sequence:03d}-{actor}", "actor": actor, "phase": self.phase,
                      "tool": params.get("name"), "arguments": values, "started_ns": time.time_ns(),
                      "tool_use_id": (params.get("_meta") or {}).get("claudecode/toolUseId")}
            self.summary["calls"].append(record)
            if actor not in self.call_counts or params.get("name") != "agentdb":
                return {"error": "unknown_tool_or_actor"}
            self.call_counts[actor] += 1
            if self.call_counts[actor] > 40:
                return {"error": "tool_budget_exhausted"}
            if not isinstance(values, dict) or set(values)-({"action"} | set(FIELDS)) or any(not isinstance(v, str) for v in values.values()):
                return {"error": "invalid_arguments"}
            action = values.get("action")
            if action not in ACTIONS:
                return {"error": "unknown_action"}
            if self.phase == "prepare_inventory" and action == "merge":
                return {"error": "phase_gate", "detail": "Prepare only; the coordinator will start the merge phase later."}
            before = self.current
            record["executed"] = True
            try:
                response = run_helper(self.args, self.database, action, actor, values)
            finally:
                # Includes failed mutations: catch a committed effect even if a client response failed.
                if action in ("fork", "propose", "merge"):
                    self.audit_after(record, actor, action, before)
            record.update(finished_ns=time.time_ns(), exit_code=response["exit_code"], error=response.get("error"))
            decoded = response.get("result")
            if isinstance(decoded, dict) and "sqlstate" in decoded:
                record["sqlstate"] = decoded["sqlstate"]
            save(self.folder / "result.json", self.summary)
            return response

    def audit_after(self, record, actor, action, before):
        summary, call_id = self.summary, record["call_id"]
        try:
            self.current = current = audit(self.owner, self.base)
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
            own_field, policy, const, desired = ("price", "pricing", "PRICE", 11) if actor == "gpt-a" else ("stock", "inventory", "STOCK", 95)
            if current["product"][own_field] == desired and constant_matches(current[policy], const, desired):
                summary["actor_landed_own_work"][actor] = True
        self.audit_log.write(wire({"stage": self.phase, "call_id": call_id, "actor": actor,
                                   "action": action, "snapshot": current}))
        self.audit_log.flush()
        record["audit_recorded"] = True


def claude_command(args, mcp_config):
    launcher = [args.python, str(args.claude)] if args.claude.suffix == ".py" else [str(args.claude)]
    return launcher + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                       "--model", args.model, "--effort", args.effort,
                       "--tools", "", "--mcp-config", str(mcp_config), "--strict-mcp-config",
                       "--allowedTools", args.tool_name, "--permission-mode", "dontAsk",
                       "--append-system-prompt", CLAUDE_INSTRUCTIONS,
                       "--disable-slash-commands", "--no-session-persistence", "--no-chrome"]


def check_init(agent, args):
    """Like the GPT thread/start check: reported model and tool surface must match the registration."""
    init = agent.init or {}
    servers = {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])}
    problems = []
    if init.get("model") != args.model:
        problems.append(f"model {init.get('model')!r}")
    if servers.get("agentdb") != "connected":
        problems.append(f"agentdb MCP server {servers.get('agentdb')!r}")
    if sorted(init.get("tools", [])) != [args.tool_name]:
        problems.append(f"tools {init.get('tools')!r}")
    if problems:
        raise RuntimeError("Reported model or tool surface differs from the registered experiment: " + "; ".join(problems))


def run_trial(args, number):
    folder = args.output / f"run-{number:02d}"
    folder.mkdir(exist_ok=False)
    raw = folder / "raw"
    raw.mkdir()
    workdir = args.workdir / f"run-{number:02d}"
    workdir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    summary = {"run": number, "requested_model": args.model, "reasoning_effort": args.effort,
               "status": "running", "success": False, "real_cloud_verified": False,
               "agents": {}, "phases": [], "published_overwrites": [], "peer_branch_modifications": [],
               "calls": [], "audit_errors": [], "actor_landed_own_work": {"gpt-a": False, "gpt-b": False}}
    save(folder / "result.json", summary)
    trial = Trial(args, folder, summary)
    server, agents = None, {}
    try:
        initial = run_helper(args, None, "init")
        save(folder / "setup.json", initial)
        if initial["exit_code"]:
            raise RuntimeError("Database setup failed: " + initial["stderr"])
        trial.database, trial.base = initial["result"]["database"], initial["result"]["base"]
        summary.update(database=trial.database, base=trial.base)
        trial.owner = connect_owner(args, trial.database)
        trial.current = audit(trial.owner, trial.base)
        trial.audit_log = (folder / "database_audit.jsonl").open("xb")
        trial.audit_log.write(wire({"stage": "initial", "snapshot": trial.current}))
        trial.audit_log.flush()
        server = Coordinator(trial)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        env = child_environment(args.config_dir, args.child_env)
        for actor in ACTORS:
            token = secrets.token_hex(16)
            trial.tokens[token] = actor
            mcp_config = folder / f"{actor}.mcp-config.json"
            save(mcp_config, {"mcpServers": {"agentdb": {"type": "stdio", "command": args.python,
                "args": [str(ROOT / "agentdb_mcp_server.py"), "--port", str(port), "--token", token,
                         "--raw", str(raw), "--actor", actor], "env": {}}}})
            agents[actor] = ClaudeAgent(claude_command(args, mcp_config), env, workdir, raw, actor)
            summary["agents"][actor] = {"pid": agents[actor].process.pid}
        stages = [("prepare_inventory", "gpt-b", PREPARE_B.format(base=trial.base)),
                  ("price_and_merge", "gpt-a", RUN_A.format(base=trial.base)),
                  ("recover_inventory", "gpt-b", LAND_B)]
        for phase, actor, prompt in stages:
            trial.phase = phase
            save(folder / (phase + ".prompt.json"), {"actor": actor, "prompt": prompt})
            event = agents[actor].turn(prompt, args.turn_timeout)
            ok = event.get("subtype") == "success" and not event.get("is_error")
            turn = {"status": "completed" if ok else "failed", "error": None if ok else event.get("result"), "result": event}
            summary["agents"][actor]["session_id"] = event.get("session_id")
            summary["phases"].append({"phase": phase, "actor": actor, "turn": turn})
            save(folder / "result.json", summary)
            print(f"run {number:02d}: {phase} -> {turn['status']}", flush=True)
            if not ok:
                raise RuntimeError(f"Model turn {event.get('subtype')}: {event.get('result')}")
            check_init(agents[actor], args)
        check = run_helper(args, trial.database, "verify")
        summary["independent_verification"] = check
        final = audit(trial.owner, trial.base)
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
        if trial.owner:
            try:
                summary["final_audit"] = audit(trial.owner, summary["base"])
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
        if agents:
            metrics = summary["metrics"] = trace_metrics(raw, list(agents), args.model)
            summary["model_protocol_match"] = (not metrics["model_reroutes"] and not metrics["init_mismatches"]
                                               and len(metrics["model_by_agent"]) == 2)
            if (not summary["model_protocol_match"] or metrics["unanswered_tool_request_ids"]
                    or metrics["mcp_calls_without_stream_tool_use"] or metrics["tool_calls"] != len(summary["calls"])):
                summary["success"] = False
        summary["wall_seconds"] = round(time.monotonic()-started, 3)
        save(folder / "result.json", summary)
        print(f"run {number:02d}: status={summary['status']} success={summary['success']} overwrites={len(summary['published_overwrites'])}", flush=True)
    return summary


def register(args):
    version = subprocess.run(args.launcher_version, capture_output=True, text=True).stdout.strip()
    account = {}
    if args.claude.suffix != ".py":
        status = subprocess.run([str(args.claude), "auth", "status"], capture_output=True, text=True,
                                env=child_environment(args.config_dir, args.child_env))
        try:
            account = {k: v for k, v in json.loads(status.stdout).items() if k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
        except ValueError:
            account = {"auth_status_unreadable": status.stdout[-500:]}
    sources = {str(p.relative_to(args.project)): sha256(p)
               for p in [args.project / "agent_lab.py", args.project / "db.py", args.project / "review_local.py", *sorted((args.project / "sql").glob("*.sql"))]}
    protocol = {"registered_at_utc": datetime.now(timezone.utc).isoformat(), "client_date": args.client_date,
        "planned_runs": args.runs, "model": args.model, "reasoning_effort": args.effort,
        "runner": "Claude Code headless (claude -p, stream-json input and output)", "claude_code_version": version,
        "claude_executable": str(args.claude), "claude_executable_sha256": sha256(args.claude),
        "account": account, "preflight": str(args.preflight) if args.preflight else None,
        "model_identity_scope": "Exact model ID in Claude Code's init event and in every API response (message.model); backend weight snapshot is not exposed",
        "workers": args.workers, "turn_timeout_seconds": args.turn_timeout, "max_tool_calls_per_agent": 40,
        "instructions": CLAUDE_INSTRUCTIONS, "instructions_gpt_run": INSTRUCTIONS,
        "instructions_difference": "Only 'You are a real GPT agent' -> 'You are a real Claude agent'. Appended to Claude Code's default system prompt with --append-system-prompt, as the GPT run added developerInstructions to the Codex default instructions.",
        "prompts": [PREPARE_B, RUN_A, LAND_B], "tool": TOOL,
        "tool_name_seen_by_model": args.tool_name,
        "claude_command": claude_command(args, Path("<run>/<actor>.mcp-config.json")),
        "child_environment": {"CLAUDE_CONFIG_DIR": str(args.config_dir), **{k: v for k, v in child_environment(args.config_dir, args.child_env).items()
                                                                            if k.startswith(("CLAUDE", "DISABLE"))}},
        "agent_working_directory": str(args.workdir) + "/run-NN (empty, outside any git repository, no CLAUDE.md or AGENTS.md)",
        "sources_sha256": sources,
        "driver_sha256": {name: sha256(ROOT / name) for name in DRIVERS},
        "counting": "All registered trials retained; no failed-trial replacement or manual rescue. Tokens: each agent's last result event's modelUsage, which is cumulative for its Claude Code process (preflight: the second turn's modelUsage equals the sum of both turns' usage), then summed over the two agents; assistant-event usage is a mid-stream snapshot and is not used. inputTokens = uncached input + cache read + cache creation; cachedInputTokens (cache read), cacheWriteInputTokens (cache creation) and reasoningOutputTokens (thinkingTokens, inside outputTokens) are subsets, not added again. An agent whose last turn has no result event has partial usage only: tokens_total is null, observed_tokens_total keeps what was reported. costUSD is Claude Code's list-price estimate, not a bill (subscription login).",
        "difference_from_gpt_run": "Same three prompts, tool name/description/schema, agent_lab.py helper, phase gate, 40-call budget, 900 s turn limit, audit after every fork/propose/merge and success rule. Runner is Claude Code headless instead of the Codex app-server; the tool reaches the model through an MCP stdio server; Claude Code's built-in tools are disabled with --tools \"\" and only the agentdb MCP tool is allowed. The instructions name Claude instead of GPT.",
        "success_definition": "Both model stages complete, both actors land own work, final verify passes, 3999 peer rows and baseline unchanged, no open transaction, no published peer overwrite at ANY mutation, no peer branch edit; plus every API response reports the registered model and every MCP tool request is answered",
        "real_cloud_verified": False}
    reference = args.gpt_protocol
    if reference and reference.is_file():
        gpt = json.loads(reference.read_text(encoding="utf-8"))
        protocol["gpt_run_reference"] = {"protocol": str(reference), "protocol_sha256": sha256(reference),
            "prompts_identical": gpt["prompts"] == protocol["prompts"], "tool_identical": gpt["tool"] == TOOL,
            "instructions_identical_except_model_word": gpt["instructions"].replace("GPT agent", "Claude agent", 1) == CLAUDE_INSTRUCTIONS,
            "sources_changed_since_gpt_run": sorted(k for k in sources if gpt["sources_sha256"].get(k) != sources[k]),
            "gpt_drivers_unchanged": {k: gpt["driver_sha256"][k] == sha256(ROOT / k) for k in gpt["driver_sha256"]}}
        if not (protocol["gpt_run_reference"]["prompts_identical"] and protocol["gpt_run_reference"]["tool_identical"]):
            raise SystemExit("Prompts or tool differ from the GPT run's protocol.json")
    save(args.output / "protocol.json", protocol)
    return protocol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--claude", type=Path, default=shutil.which("claude"))
    parser.add_argument("--config-dir", type=Path, required=True, help="Dedicated CLAUDE_CONFIG_DIR holding only the login")
    parser.add_argument("--workdir", type=Path, required=True, help="Parent of the agents' empty working directories")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpt-protocol", type=Path, help="The GPT run's protocol.json, to prove identical prompts and tool")
    parser.add_argument("--preflight", type=Path, help="Preflight folder (not part of the sample), recorded in protocol.json")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="xhigh")
    parser.add_argument("--tool-name", default="mcp__agentdb__agentdb", help="Name Claude Code gives the MCP tool")
    parser.add_argument("--child-env", type=json.loads, default={}, help="Extra environment for claude, as JSON")
    parser.add_argument("--client-date", default=datetime.now().date().isoformat())
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    parser.add_argument("--turn-timeout", type=int, default=900)
    args = parser.parse_args()
    if args.runs < 10:
        parser.error("Register at least ten trials")
    args.project, args.output, args.workdir = args.project.resolve(), args.output.resolve(), args.workdir.resolve()
    args.claude = args.claude.resolve()
    args.config = (args.config or args.project / "connection.local.json").resolve()
    args.launcher_version = ([args.python, str(args.claude)] if args.claude.suffix == ".py" else [str(args.claude)]) + ["--version"]
    args.output.mkdir(parents=True, exist_ok=False)
    args.workdir.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.project))
    protocol = register(args)
    print(f"registered {args.runs} runs: {protocol['model']} / {protocol['reasoning_effort']} / {protocol['claude_code_version']}", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = [pool.submit(run_trial, args, i) for i in range(1, args.runs+1)]
        for future in as_completed(pending):
            results.append(future.result())
    results.sort(key=lambda r: r['run'])
    successes = sum(r['success'] for r in results)
    measured = [r for r in results if r.get('metrics', {}).get('tokens_total') is not None]
    output = {'protocol': 'protocol.json', 'planned_runs': args.runs, 'recorded_runs': len(results),
        'successful_runs': successes, 'success_rate': successes/args.runs,
        'wilson_95_interval': wilson(successes, args.runs),
        'completed_runs': sum(r['status'] == 'completed' for r in results),
        'runs_with_published_overwrite': [r['run'] for r in results if r['published_overwrites']],
        'runs_with_peer_branch_modification': [r['run'] for r in results if r['peer_branch_modifications']],
        'runs_with_token_metrics': len(measured),
        'tokens_for_measured_runs': add_tokens(r['metrics']['tokens_total'] for r in measured),
        'observed_tokens_all_runs': add_tokens(r.get('metrics', {}).get('observed_tokens_total') for r in results),
        'list_cost_usd_estimate': round(sum(r.get('metrics', {}).get('list_cost_usd', 0) for r in results), 4),
        'tool_calls': sum(r.get('metrics', {}).get('tool_calls', 0) for r in results),
        'rows': [{'run': r['run'], 'status': r['status'], 'success': r['success'],
                  'published_overwrites': len(r['published_overwrites']), 'wall_seconds': r['wall_seconds'],
                  'metrics': r.get('metrics'), 'error': r.get('error')} for r in results], 'real_cloud_verified': False}
    save(args.output / 'summary.json', output)
    print(json.dumps({k: output[k] for k in ('recorded_runs', 'successful_runs', 'success_rate', 'tool_calls', 'runs_with_published_overwrite', 'runs_with_token_metrics')}, indent=2), flush=True)


if __name__ == '__main__':
    main()
