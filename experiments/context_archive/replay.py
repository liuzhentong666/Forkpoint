"""Replay recorded Claude rounds through the context archive, with no model calls.

  python experiments/context_archive/replay.py [--recorded DIR] [--output FILE] [--keep-database]

For each recorded agent process it feeds the prompts sent and the lines received, in the
order the runner saw them, through the capture code and Writer that run_archive_trials.py
uses, into one new agentdb_test_xcreplay_* database (dropped at the end unless
--keep-database). It checks that every entry comes back byte for byte, and finds where a
switch would have happened (archive.Trigger). There it renders setup B's handoff and
reports where the IDs the agent still needs are: shown in full, only as a label prefix,
or only by fetch. This is the gate in docs/CONTEXT_ARCHIVE_EXPERIMENT.md: it decides
whether the experiment can tell the two setups apart before any model quota is spent.
Only the first switch of a process is evaluated; after it the recorded agent went on
with its full context, so later behaviour says nothing about a switched one.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import archive
from archive import Trigger, Writer, handoff_parts, load_entries, received_entry, render_handoff, sent_entry
from db import Connection, add_connection_arguments

RECORDED = archive.ROOT / "experiments" / "gpt_repeat" / "results" / "2026-10-20-claude"
OUTPUT = archive.ROOT / "results" / "context_archive" / "replay-2026-10-20-claude.json"
ACTORS = ("gpt-a", "gpt-b")
GATE = ("passes if at least 5 recorded processes reach a switch and, at no fewer than half of them, "
        "the proposal revision is outside the in-full part")


def connect(args, database=None):
    options = copy.copy(args)
    options.database, options.role = database, None
    return Connection(options)


def nonblank(path):
    return [line for line in path.read_bytes().splitlines(keepends=True) if line.strip()]


def lines_in_order(raw, actor):
    """One process's prompts and received lines in the order its runner saw them: prompt k+1 after result k."""
    prompts = [line for line in nonblank(raw / f"{actor}.stdin.jsonl") if json.loads(line).get("type") == "user"]
    order = [("sent", prompts.pop(0))] if prompts else []
    for line in nonblank(raw / f"{actor}.stdout.jsonl"):
        order.append(("received", line))
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result" and prompts:
            order.append(("sent", prompts.pop(0)))
    return order


def proposal_from(event, calls):
    """The revision a propose call returned, if this event is that call's result."""
    for block in archive.content_of(event) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            calls[block.get("id")] = (block.get("input") or {}).get("action")
        elif block.get("type") == "tool_result" and calls.get(block.get("tool_use_id")) == "propose":
            try:
                text = "".join(c.get("text", "") for c in block.get("content") or [])
                return json.loads(text)["result"]["revision"]
            except (ValueError, KeyError, TypeError):
                return None
    return None


def location(value, catalog, full, entries):
    if value is None:
        return "not created yet"
    if any(value in line for line in full):
        return "in full"
    if any(value[:archive.SHORT_CHARS] in line for line in catalog):
        return "label prefix only"
    if any(value.encode("utf-8") in body for _, _, _, body in entries):
        return "fetch only"
    return "absent"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_arguments(parser)
    parser.add_argument("--recorded", type=Path, default=RECORDED, help="A run_claude_repeats.py output folder")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limit", type=int, default=archive.LIMIT_TOKENS)
    parser.add_argument("--keep-database", action="store_true")
    args = parser.parse_args()
    runs = sorted(p for p in args.recorded.iterdir() if p.is_dir() and p.name.startswith("run-"))
    name = "agentdb_test_xcreplay_" + uuid.uuid4().hex[:8]
    owner = connect(args)
    owner.create_database(name)
    database = connect(args, name)
    rows, captured = [], {}
    try:
        database.sql((archive.HERE / "archive.sql").read_text(encoding="utf-8"))
        writer = Writer(lambda: database.session(name))
        switches = {}
        for run in runs:
            base = json.loads((run / "setup.json").read_text(encoding="utf-8"))["result"]["base"]
            for actor in ACTORS:
                task, trigger, calls, proposal = f"{run.name}/{actor}", Trigger(args.limit), {}, None
                captured[task], turn = [], 0
                for direction, line in lines_in_order(run / "raw", actor):
                    entry = sent_entry(line) if direction == "sent" else received_entry(line)
                    if entry:
                        writer.put(task, entry)
                        captured[task].append(entry)
                    if direction == "sent" or task in switches:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    proposal = proposal_from(event, calls) or proposal
                    turn += event.get("type") == "result"
                    if trigger.observe(event):
                        switches[task] = dict(after_entry=len(captured[task]), request_tokens=trigger.crossed,
                                              turn=turn + 1, base=base, proposal=proposal)
        writer.flush(600)
        writer.close()
        reader = database.session(name)
        mismatches = 0
        try:
            for task, entries in captured.items():
                stored = load_entries(reader.sql, task)
                mismatches += sum(1 for want, got in zip(entries, stored) if tuple(want) != tuple(got[1:]))
                mismatches += abs(len(entries) - len(stored))
                row = dict(task=task, entries=len(stored), bytes=sum(len(e[3]) for e in stored), switch=None)
                point = switches.get(task)
                if point:
                    before = stored[:point["after_entry"]]
                    catalog, collapsed, full = handoff_parts(before)
                    row["switch"] = dict(after_entry=point["after_entry"], request_tokens=point["request_tokens"],
                                         turn=point["turn"], handoff_bytes=len(render_handoff(before).encode("utf-8")),
                                         in_full_lines=len(full), catalog_lines=len(catalog), collapsed=collapsed,
                                         base_revision=location(point["base"], catalog, full, before),
                                         proposal_revision=location(point["proposal"], catalog, full, before))
                rows.append(row)
        finally:
            reader.close()
    finally:
        if not args.keep_database:
            owner.sql(f'DROP DATABASE "{name}" WITH (FORCE);', owner.settings["maintenance_database"])
    switched = [r["switch"] for r in rows if r["switch"]]
    outside = [s for s in switched if s["proposal_revision"] != "in full"]
    gate = dict(rule=GATE, switches=len(switched), proposal_outside_in_full=len(outside),
                passed=len(switched) >= 5 and 2 * len(outside) >= len(switched))
    tally = {}
    for s in switched:
        for key in ("base_revision", "proposal_revision"):
            tally.setdefault(key, {}).setdefault(s[key], 0)
            tally[key][s[key]] += 1
    recorded = args.recorded.resolve()
    report = dict(
        recorded_at_utc=datetime.now(timezone.utc).isoformat(),
        recorded=(recorded.relative_to(archive.ROOT).as_posix() if recorded.is_relative_to(archive.ROOT) else str(recorded)),
        database=dict(name=name, dropped=not args.keep_database),
        constants=dict(limit_tokens=args.limit, in_full_bytes=archive.IN_FULL_BYTES, handoff_bytes=archive.HANDOFF_BYTES),
        sources_sha256={p: sha256(archive.ROOT / p) for p in ("experiments/context_archive/archive.sql",
                                                               "experiments/context_archive/archive.py",
                                                               "experiments/context_archive/replay.py")},
        processes=len(rows), entries=sum(r["entries"] for r in rows), entry_bytes=sum(r["bytes"] for r in rows),
        round_trip=dict(entries_compared=sum(len(e) for e in captured.values()), mismatches=mismatches),
        where_needed_ids_are_at_the_switch=tally, gate=gate, rows=rows, model_calls=0, real_cloud_verified=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({k: report[k] for k in ("processes", "entries", "entry_bytes", "round_trip",
                                             "where_needed_ids_are_at_the_switch", "gate")}, indent=2))
    raise SystemExit(0 if gate["passed"] and not mismatches else 1)


if __name__ == "__main__":
    main()
