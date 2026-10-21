"""Recount a read-set run from its raw calls.jsonl, without the harness's counters.

  python experiments/readset/audit_calls.py results/readset/<run>

Uses only the logged SQL text and return codes, per writer in call order:
  merged     the writer's last merge call succeeded
  abstained  the writer's last call is a data read with no proposal after it
  resubmit   a failed merge followed by another merge of the same revision (55P03 counted apart)
  rethink    a failed merge followed by a data read
  wasted     the next proposal after a rethink has the same UPDATE / write_file lines as the one before
Each count is compared with results.json in the same directory; exits 1 on any mismatch.
calls.jsonl is git-ignored, so this runs only where the run was made.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys

REVISION = re.compile(r"xr\.merge_(?:head|validated)\('main','([0-9a-f-]{36})'")
COUNTS = ("merged", "abstained", "failed", "resubmits", "lock_timeouts", "rethinks", "useful_rethinks", "wasted_rethinks")


def change_lines(sql):
    return tuple(line.strip() for line in sql.splitlines()
                 if line.startswith("UPDATE agent.products") or line.startswith("SELECT vc.write_file("))


def recount(calls):
    """calls: one writer's logged calls in order. Returns its counts, plus anything unexplained."""
    counts = defaultdict(int)
    merges = [c for c in calls if c["kind"] == "merge"]
    if merges and merges[-1]["returncode"] == 0:
        counts["merged"] += 1
    elif calls[-1]["kind"] == "read":
        counts["abstained"] += 1
    else:
        counts["failed"] += 1
    previous = None
    for i, call in enumerate(calls):
        if call["kind"] == "propose":
            previous = change_lines(call["sql"])
        if call["kind"] != "merge" or call["returncode"] == 0:
            continue
        if call["sqlstate"] == "55P03":
            counts["lock_timeouts"] += 1
            continue
        after = [c for c in calls[i+1:] if c["kind"] in ("merge", "read")]
        if not after:
            counts["unexplained"] += 1
        elif after[0]["kind"] == "merge":
            same = REVISION.search(after[0]["sql"]).group(1) == REVISION.search(call["sql"]).group(1)
            counts["resubmits" if same else "unexplained"] += 1
        else:
            counts["rethinks"] += 1
            following = [c for c in calls[i+1:] if c["kind"] in ("propose", "merge")]
            proposal = change_lines(following[0]["sql"]) if following and following[0]["kind"] == "propose" else None
            counts["wasted_rethinks" if proposal == previous else "useful_rethinks"] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="A run directory holding calls.jsonl and results.json")
    args = parser.parse_args()
    by_writer = defaultdict(list)
    with (args.folder / "calls.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            call = json.loads(line)
            by_writer[(call["run"], call["writer"])].append(call)
    totals = defaultdict(lambda: defaultdict(int))
    for (run, _), calls in by_writer.items():
        for key, value in recount(calls).items():
            totals[run][key] += value
    results = json.loads((args.folder / "results.json").read_text(encoding="utf-8"))
    mismatches = 0
    for run in results["runs"]:
        got = totals[run["run"]]
        for key in COUNTS:
            if got[key] != run["counts"][key]:
                mismatches += 1
                print(f"MISMATCH {run['run']} {key}: calls.jsonl {got[key]}, results.json {run['counts'][key]}")
        if got["unexplained"]:
            mismatches += 1
            print(f"UNEXPLAINED {run['run']}: {got['unexplained']} failed merge(s) with no retry or rethink after them")
    print(f"{args.folder}: {len(results['runs'])} runs, {sum(len(c) for c in by_writer.values())} logged calls, "
          f"{len(COUNTS)} counts per run compared, mismatches: {mismatches}")
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
