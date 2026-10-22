"""Offline check of a long-memory task for the context archive (no model calls).

The task: before preparing its proposal, gpt-b audits product 101's stock at K earlier
revisions listed in its prompt, then sets the stock to the lowest audited stock minus 5
(95, which agent_lab verify checks) and saves an inventory policy that starts with one
"# <first 8 characters of the revision>: <stock>" line per audited revision. Until the
proposal writes them to the database, the revision list and the rule exist only in the
prompt and the audited values only in tool results, so a switch in between needs them
back. The pricing agent's task and the recovery prompt are unchanged.

  python experiments/context_archive/long_task.py [--output FILE] [--keep-database]

1. Fits request input tokens to the bytes of the context so far, over the recorded
   Claude rounds (replay.RECORDED).
2. Builds the task in a new agentdb_test_gpt_* database (agent_lab.py init, one commit
   per audited stock value, then one back to stock 100: the common baseline) and runs
   a scripted agent's preparation calls through agent_lab.py, so every tool result is real.
3. Lays the calls out as Claude Code stream-json in two patterns (one call per response;
   audit reads four per response) with the fitted token estimate and 20% either side,
   and runs the archive's capture, trigger and handoff on each of the six layouts.
Gate: the smallest K in CANDIDATES for which, in all six layouts, the switch comes right
after audit reads, at least three audited values and the audit rule are reachable only
by fetch, fetching them back takes at most three calls, and the task fits the call
budget. Exits 0 if some K passes, else 1.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import archive
from archive import Trigger, block_of, fetch_reply, handoff_parts, received_entry, sent_entry, shown
from db import add_connection_arguments
import replay

sys.path.insert(0, str(archive.ROOT / "experiments" / "gpt_repeat"))
from codex_rpc import wire  # noqa: E402
from run_gpt_repeats import run_helper  # noqa: E402

STOCK_HISTORY = (118, 104, 111, 100, 109, 115, 103, 121, 107, 112, 105, 119, 102, 116, 108, 113)
TARGET_STOCK = min(STOCK_HISTORY) - 5
CANDIDATES = (8, 10, 12, 14, 16)
CALL_BUDGET = 60        # per agent, both phases; 40 in the recorded rounds
RECOVERY_CALLS = 15     # gpt-b's median calls in the recorded recovery phase (2026-10-20)
BATCH = 4               # audit reads per response in the batched pattern
SPREAD = 0.2            # token estimate checked at -20%, fitted and +20%
SIGNATURE = "E" * 1412  # mean signature length of the recorded thinking blocks
RULE = "lowest stock seen in the audit minus 5"
OUTPUT = archive.ROOT / "results" / "context_archive" / "long-task-check.json"

PREPARE_AUDIT = """You are the inventory agent (gpt-b). Common baseline revision: {base}.
Before changing anything, audit product 101's stock history: read product 101 at each of these {k} earlier revisions, in this order:
{revisions}
Then, at the common baseline, set product 101's stock to the lowest stock seen in the audit minus 5, without changing other fields.
Save a Python policy that starts with one comment line per audited revision, in the order above, exactly "# <first 8 characters of the revision>: <stock>", followed by a literal STOCK constant matching the resulting stock.
Create your own gpt-b proposal branch, submit code and data together, inspect the diff and code.
This is preparation only: do not merge yet. Report your proposal revision and original expected main.
"""
assert RULE in PREPARE_AUDIT and TARGET_STOCK == 95


def context_bytes(block, signatures):
    """Bytes a block adds to the model's context, as the handoff would show it, optionally with its signature."""
    view = shown(block)
    size = len(json.dumps(view, ensure_ascii=False, sort_keys=True).encode("utf-8")) if view else 0
    if signatures and block.get("type") == "thinking":
        size += len(str(block.get("signature", "")).encode("utf-8"))
    return size


def request_points(recorded, signatures):
    """(context bytes before the request, its input tokens), once per response of every recorded process."""
    points = []
    for run in sorted(p for p in recorded.iterdir() if p.is_dir() and p.name.startswith("run-")):
        for actor in replay.ACTORS:
            size, seen = 0, set()
            for direction, line in replay.lines_in_order(run / "raw", actor):
                if direction == "sent":
                    entry = sent_entry(line)
                    size += context_bytes(block_of(entry[2]), signatures) if entry else 0
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                tokens, message = archive.request_tokens(event), (event.get("message") or {}).get("id")
                if tokens is not None and message not in seen:
                    seen.add(message)
                    points.append((size, tokens))
                if archive.content_of(event) is not None:
                    size += context_bytes(block_of(line.rstrip(b"\r\n")), signatures)
    return points


def fit(points):
    """Least squares: tokens = intercept + tokens_per_byte * bytes."""
    n = len(points)
    mean_c, mean_t = sum(c for c, _ in points) / n, sum(t for _, t in points) / n
    slope = (sum((c - mean_c) * (t - mean_t) for c, t in points) / sum((c - mean_c) ** 2 for c, _ in points))
    intercept = mean_t - slope * mean_c
    residuals = [t - intercept - slope * c for c, t in points]
    total = sum((t - mean_t) ** 2 for _, t in points)
    return dict(intercept=round(intercept, 1), tokens_per_byte=round(slope, 5), points=n,
                rmse=round(math.sqrt(sum(r * r for r in residuals) / n), 1),
                max_abs_residual=round(max(abs(r) for r in residuals), 1),
                r2=round(1 - sum(r * r for r in residuals) / total, 5))


def commit_stock(owner, stock):
    return owner.sql(f"""BEGIN;
SELECT vc.begin_change('main',vc.head(),'history','product 101 stock {stock}') AS draft \\gset
UPDATE app.products SET stock={int(stock)} WHERE id=101;
SELECT vc.finish_change() AS revision \\gset
COMMIT;
SELECT :'revision';""").splitlines()[-1]


def policy(revisions, k):
    lines = [f"# {r[:8]}: {s}" for r, s in zip(revisions[:k], STOCK_HISTORY[:k])]
    return "\n".join(lines + [f"STOCK = {TARGET_STOCK}"]) + "\n"


def prompt_for(base, revisions, k):
    return PREPARE_AUDIT.format(base=base, k=k, revisions="\n".join("- " + r for r in revisions[:k]))


def report_for(base, revisions, k, proposal):
    audit = ", ".join(f"{r[:8]}: {s}" for r, s in zip(revisions[:k], STOCK_HISTORY[:k]))
    return (f"Proposal ready, not merged.\n- Proposal revision: {proposal}\n- Branch: gpt-b-audit-{k}\n"
            f"- Original expected main: {base}\n- Audit: {audit}\n"
            f"- Lowest audited stock {min(STOCK_HISTORY[:k])}, so STOCK = {TARGET_STOCK}.")


def responses_for(calls, pattern):
    """Group the calls into model responses: [(blocks, results)]. calls are (kind, action, args, response)."""
    groups, audit = [], []
    for call in calls:
        if call[0] == "audit" and pattern == "batched":
            audit.append(call)
            if len(audit) == BATCH:
                groups.append(audit)
                audit = []
            continue
        if audit:
            groups.append(audit)
            audit = []
        groups.append([call])
    if audit:
        groups.append(audit)
    return groups


def layout(prompt, calls, report, pattern, estimate, signatures):
    """The prompt line and the stream-json lines a Claude Code process would print for these calls.

    Every response starts with an estimated request size: estimate(bytes of context so far).
    Thinking blocks (empty, with a signature, as 79 of 99 recorded ones were) open the first
    response, the propose call and the report.
    """
    prompt_line = wire({"type": "user", "session_id": "", "parent_tool_use_id": None,
                        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
    size, lines, number = context_bytes(block_of(prompt_line.rstrip(b"\n")), signatures), [], 0
    groups = responses_for(calls, pattern) + [None]
    for index, group in enumerate(groups):
        tokens = round(estimate(size))
        blocks = []
        if index == 0 or group is None or group[0][1] == "propose":
            blocks.append({"type": "thinking", "thinking": "", "signature": SIGNATURE})
        results = []
        for kind, action, args, response in group or []:
            number += 1
            call_id = f"toolu_{number:03d}"
            blocks.append({"type": "tool_use", "id": call_id, "name": "mcp__agentdb__agentdb",
                           "input": {"action": action, **args}})
            results.append({"type": "tool_result", "tool_use_id": call_id, "is_error": bool(response.get("error")),
                            "content": [{"type": "text", "text": json.dumps(response, ensure_ascii=False)}]})
        if group is None:
            blocks.append({"type": "text", "text": report})
        for block in blocks:
            lines.append(wire({"type": "assistant", "parent_tool_use_id": None, "session_id": "offline",
                               "message": {"id": f"msg_{index:03d}", "model": "claude-opus-5-5", "content": [block],
                                           "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0,
                                                     "cache_creation_input_tokens": 0}}}))
            size += context_bytes(block, signatures)
        for block in results:
            lines.append(wire({"type": "user", "parent_tool_use_id": None, "session_id": "offline",
                               "message": {"role": "user", "content": [block]}}))
            size += context_bytes(block, signatures)
    return prompt_line, lines


def where(text, catalog, full, entries):
    if any(text in line for line in full):
        return "in full"
    if any(text in line for line in catalog):
        return "in a label"
    if any(text.encode("utf-8") in body for *_, body in entries):
        return "fetch only"
    return "absent"


def fetch_calls(entries, needed):
    """Fetch calls needed to get these entries back: 8 per call, and again for any a reply left out."""
    found, calls, queue = {e[0]: e for e in entries}, 0, list(needed)
    while queue:
        chunk, queue = queue[:archive.FETCH_ENTRIES], queue[archive.FETCH_ENTRIES:]
        reply = fetch_reply({n: found[n] for n in chunk}, chunk)
        calls += 1
        queue = reply["result"]["omitted"] + queue
    return calls


def evaluate(prompt_line, lines, calls, revisions, k):
    """Run capture, trigger and handoff over one layout; report the switch and where the needed facts are."""
    entries, trigger, boundary, results = [], Trigger(), None, {}
    entries.append((1, *sent_entry(prompt_line)))
    calls_by_id = {}
    for line in lines:
        entry = received_entry(line)
        if entry:
            entries.append((len(entries) + 1, *entry))
            block = block_of(entry[2])
            if block.get("type") == "tool_use":
                calls_by_id[block["id"]] = len(calls_by_id)
            elif block.get("type") == "tool_result":
                results[calls_by_id[block["tool_use_id"]]] = len(entries)
        if boundary is None and trigger.observe(json.loads(line)):
            boundary = (len(entries), trigger.crossed, len(results))
    if boundary is None:
        return dict(switch=None, passed=False, reasons=["no switch before the end of the preparation phase"])
    seq, tokens, answered = boundary
    before = entries[:seq]
    catalog, collapsed, full = handoff_parts(before)
    in_full = {int(line.split(" ", 1)[0][1:]) for line in full}
    audit = [i for i, call in enumerate(calls) if call[0] == "audit"]
    read = [i for i in audit if i in results and results[i] <= seq]
    old = [results[i] for i in read if results[i] not in in_full]
    unread = [revisions[audit.index(i)] for i in audit if i not in read]
    rule = where(RULE, catalog, full, before)
    unread_places = {}
    for revision in unread:
        place = where(revision, catalog, full, before)
        unread_places[place] = unread_places.get(place, 0) + 1
    last = calls[answered - 1]
    needed_fetches = fetch_calls(before, [1] + old)
    total_calls = len(calls) + RECOVERY_CALLS + needed_fetches
    reasons = []
    if last[0] != "audit":
        reasons.append(f"switch after {last[1]}, not after an audit read")
    if len(old) < 3:
        reasons.append(f"only {len(old)} audited values are fetch-only")
    if rule != "fetch only":
        reasons.append(f"audit rule is {rule}")
    if needed_fetches > 3:
        reasons.append(f"{needed_fetches} fetch calls needed")
    if total_calls > CALL_BUDGET:
        reasons.append(f"{total_calls} calls exceed the budget of {CALL_BUDGET}")
    return dict(switch=dict(after_call=answered, after_action=last[1], request_tokens=tokens, entries_before=seq,
                            in_full_lines=len(full), catalog_lines=len(catalog), collapsed=collapsed,
                            handoff_bytes=len(archive.render_handoff(before).encode("utf-8"))),
                audited_before_switch=len(read), audited_values_fetch_only=len(old), revisions_unread=len(unread),
                unread_revisions=unread_places, audit_rule=rule, fetch_calls=needed_fetches,
                calls_estimate=dict(preparation=len(calls), recovery=RECOVERY_CALLS, fetch=needed_fetches, total=total_calls),
                passed=not reasons, reasons=reasons)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("--recorded", type=Path, default=replay.RECORDED)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--keep-database", action="store_true")
    args = parser.parse_args()
    fits = {name: fit(request_points(args.recorded, signatures)) for name, signatures in
            (("without_signatures", False), ("with_signatures", True))}
    chosen = min(fits, key=lambda name: fits[name]["rmse"])
    model, signatures = fits[chosen], chosen == "with_signatures"
    helper = SimpleNamespace(python=sys.executable, project=archive.ROOT,
                             config=(args.config or archive.ROOT / "connection.local.json").resolve())
    init = run_helper(helper, None, "init")
    if init["exit_code"]:
        raise SystemExit("agent_lab.py init failed: " + init["stderr"])
    database = init["result"]["database"]
    owner = replay.connect(args, database)
    rows = []
    try:
        revisions = [commit_stock(owner, stock) for stock in STOCK_HISTORY[:max(CANDIDATES)]]
        base = commit_stock(owner, 100)

        def run(action, **parameters):
            response = run_helper(helper, database, action, "gpt-b", {"action": action, **parameters})
            if response["exit_code"]:
                raise SystemExit(f"agent_lab.py {action} failed: {response['stderr'] or response['stdout']}")
            return action, parameters, response

        opening = [run("info"), run("read-code", revision=base, path="experiments/pricing.py"),
                   run("read-code", revision=base, path="experiments/inventory.py")]
        audits = [run("info", revision=r) for r in revisions]
        if [a[2]["result"]["product"]["stock"] for a in audits] != list(STOCK_HISTORY[:len(revisions)]):
            raise SystemExit("The audited stock values differ from STOCK_HISTORY")
        for k in CANDIDATES:
            branch = f"gpt-b-audit-{k}"
            closing = [run("fork", branch=branch, base=base)]
            closing.append(run("propose", branch=branch, expected=base, field="stock", value=str(TARGET_STOCK),
                               code=policy(revisions, k)))
            proposal = closing[-1][2]["result"]["revision"]
            closing += [run("diff", base=base, revision=proposal),
                        run("read-code", revision=proposal, path="experiments/inventory.py")]
            calls = ([("open",) + c for c in opening] + [("audit",) + c for c in audits[:k]] +
                     [("close",) + c for c in closing])
            for pattern in ("sequential", "batched"):
                for name, factor in (("low", 1 - SPREAD), ("fitted", 1.0), ("high", 1 + SPREAD)):
                    def estimate(size, factor=factor):
                        return model["intercept"] + model["tokens_per_byte"] * size * factor
                    prompt_line, lines = layout(prompt_for(base, revisions, k), calls,
                                                report_for(base, revisions, k, proposal), pattern, estimate, signatures)
                    rows.append(dict(k=k, pattern=pattern, estimate=name,
                                     **evaluate(prompt_line, lines, calls, revisions, k)))
    finally:
        if not args.keep_database:
            owner.sql(f'DROP DATABASE "{database}" WITH (FORCE);', owner.settings["maintenance_database"])
    passing = [k for k in CANDIDATES if all(r["passed"] for r in rows if r["k"] == k)]
    gate = dict(rule="smallest K in CANDIDATES that passes in all six layouts", candidates=list(CANDIDATES),
                passing=passing, chosen_k=passing[0] if passing else None, passed=bool(passing))
    report = dict(
        recorded_at_utc=datetime.now(timezone.utc).isoformat(),
        token_model=dict(fits=fits, chosen=chosen, recorded=str(args.recorded)),
        task=dict(prompt=PREPARE_AUDIT, stock_history=list(STOCK_HISTORY), target_stock=TARGET_STOCK, rule=RULE,
                  call_budget=CALL_BUDGET, recovery_calls_assumed=RECOVERY_CALLS, batch=BATCH, spread=SPREAD,
                  limit_tokens=archive.LIMIT_TOKENS, in_full_bytes=archive.IN_FULL_BYTES),
        database=dict(name=database, dropped=not args.keep_database), rows=rows, gate=gate,
        sources_sha256={p: replay.sha256(archive.ROOT / p) for p in (
            "experiments/context_archive/archive.py", "experiments/context_archive/long_task.py", "agent_lab.py")},
        model_calls=0, real_cloud_verified=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(dict(token_model={chosen: model}, gate=gate), indent=2))
    for r in rows:
        s = r["switch"] or {}
        print(r["k"], r["pattern"], r["estimate"], "after", s.get("after_action"), s.get("after_call"),
              "tokens", s.get("request_tokens"), "| fetch-only values", r.get("audited_values_fetch_only"),
              "unread", r.get("revisions_unread"), "rule", r.get("audit_rule"), "fetches", r.get("fetch_calls"),
              "PASS" if r["passed"] else "FAIL " + "; ".join(r["reasons"]))
    raise SystemExit(0 if gate["passed"] else 1)


if __name__ == "__main__":
    main()
