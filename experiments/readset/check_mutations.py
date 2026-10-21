"""Mutation check for the read-set tests: do they fail when the code is broken?

  python experiments/readset/check_mutations.py         # unit tests, no database
  python experiments/readset/check_mutations.py --sql   # also readset.sql against the contract tests

Each mutation changes one expression in a copy of experiments/readset/ made in a
temporary directory (the working tree is never modified); the matching tests must
then fail. The unmutated copy must pass first. --sql runs test_readset_sql.py,
which builds and drops its own database on the local PostgreSQL, about 25 s per
mutation. Exits 1 when a mutation survives or no longer applies (its text must
occur exactly once).
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GOLDEN = Path("results/readset/readset_df002d50/results.json")

MUTATIONS = [
    ("oracle.py", "if before.get(key) != doc]", "if before.get(key) == doc]", "stale check compares the wrong way"),
    ("oracle.py", "if changed:\n            stale.append(", "if not changed:\n            stale.append(", "stale merges dropped"),
    ("oracle.py", "raise ValueError(f\"No first-parent rows", "return stale  # (f\"No first-parent rows", "missing parent passes silently"),
    ("oracle.py", "stock[pair[1]] < minimum]", "stock[pair[1]] <= minimum]", "bundle at exactly the minimum counted as a violation"),
    ("oracle.py", "signature(previous) == signature(current)", "signature(previous) != signature(current)", "wasted and useful swapped"),
    ("oracle.py", '{"W2": 5, "W3": 2}', '{"W2": 4, "W3": 2}', "wrong blind-retry prediction"),
    ("oracle.py", 'r["counts"]["stale_commits"] == 0 and r["counts"]["rule_violations"] == 0 for r in safe',
     'r["counts"]["rule_violations"] == 0 for r in safe', "safety ignores stale commits"),
    ("oracle.py", 'r["counts"]["rethinks"] == 0 for r in w1', 'r["counts"]["rethinks"] <= 1 for r in w1', "cost allows a W1 rethink"),
    ("oracle.py", 'r["counts"]["rethinks"] <= 2 and', 'r["counts"]["rethinks"] <= 3 and', "cost allows a third W3 rethink"),
    ("oracle.py", 'overhead["validation_median_ms"] <= overhead["vc_merge_median_ms"]',
     'overhead["validation_median_ms"] < overhead["vc_merge_median_ms"]', "overhead fails at equality"),
    ("oracle.py", 'r["counts"]["failed"] == 0 for r in readset', 'r["counts"]["failed"] >= 0 for r in readset', "failed writers ignored"),
    ("run_readset.py", "if own + partner - SALE < BUNDLE_MINIMUM:", "if own + partner - SALE <= BUNDLE_MINIMUM:", "seller abstains at exactly the minimum"),
    ("run_readset.py", 'price = 100 + spec["writer"]', 'price = 101 + spec["writer"]', "W1 price off by one"),
    ("run_readset.py", '{"stock": own - SALE}', '{"stock": own - SALE + 1}', "sale amount off by one"),
    ("run_readset.py", "FIRST_BUNDLE_ROW + (slot ^ 1)", "FIRST_BUNDLE_ROW + (slot + 1)", "wrong partner product"),
    ("run_readset.py", "bundle_seller(i, i - 6)", "bundle_seller(i, i - 5)", "W3 bundles shifted"),
    ("run_readset.py", 'choices = [row for row in BROWSE_RANGE if row not in spec["writes"]]',
     "choices = list(BROWSE_RANGE)", "browsed rows may include own writes"),
    ("run_readset.py", 'reads = [row for row in spec["depends"] if row not in spec["writes"]]',
     'reads = list(spec["depends"])', "read set includes written rows"),
    ("run_readset.py", "drop.random() >= UNDERCITE", "drop.random() < UNDERCITE", "under-citation drops the other half"),
    ("run_readset.py", "reads = sorted(set(reads) | set(browse))", "reads = sorted(set(browse))", "tracked read set loses dependencies"),
    ("run_readset.py", 'f"{seed_value}:{workload}:{writer}:{purpose}"', 'f"{seed_value}:{writer}:{purpose}"', "seeding ignores the workload"),
    ("run_readset.py", "k = (seed_value - 1) % len(arms)", "k = seed_value % len(arms)", "arm rotation shifted"),
    ("run_readset.py", "validated_at=([0-9.]+)", "validated=([0-9.]+)", "HINT time not parsed"),
    ("run_readset.py", 'keys = [item["key"] for item', 'keys = [item["table"] for item', "conflict keys read from the wrong field"),
    ("run_readset.py", "if any(b < a for a, b in zip(order, order[1:])):", "if any(b > a for a, b in zip(order, order[1:])):",
     "clock check inverted"),
    ("run_readset.py", "<= wall_ms + CLOCK_TOLERANCE_MS", "<= wall_ms + 100 * CLOCK_TOLERANCE_MS", "clock tolerance too loose"),
    ("audit_calls.py", '"wasted_rethinks" if proposal == previous else "useful_rethinks"',
     '"wasted_rethinks" if proposal != previous else "useful_rethinks"', "audit swaps wasted and useful"),
    ("audit_calls.py", 'counts["resubmits" if same else "unexplained"] += 1', 'counts["resubmits"] += 1',
     "audit accepts a different revision as a resubmit"),
    ("audit_calls.py", 'elif calls[-1]["kind"] == "read":', 'elif calls[-1]["kind"] == "merge":', "audit misses abstentions"),
    ("audit_calls.py", 'if call["sqlstate"] == "55P03":', 'if call["sqlstate"] == "40001":', "audit counts stale heads as lock timeouts"),
]

# Checked by test_readset_sql.py. Not covered there: taking main's lock before
# validating (vc.merge takes the same lock anyway, so only a concurrent race would
# show it; the experiment's 0 rethinks in W1 rs_declared is the evidence for it).
SQL_MUTATIONS = [
    ("readset.sql", "IF base<>head THEN", "IF false THEN", "read set never validated"),
    ("readset.sql", "USING ERRCODE='RS001',DETAIL", "USING DETAIL", "read conflict reported as P0001"),
    ("readset.sql", "RETURN vc.merge(p_target,p_source,head,p_author);", "RETURN vc.merge(p_target,p_source,base,p_author);",
     "merges against the stale base"),
    ("readset.sql", "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA xr FROM PUBLIC;", "", "PUBLIC may execute xr functions"),
    ("readset.sql", "jsonb_typeof(r.value)<>'array'", "jsonb_typeof(r.value)='string'", "non-array read keys accepted"),
]


def copy_tree(target):
    """experiments/readset/*.py and readset.sql, plus the recorded results the golden test reads."""
    folder = target / "experiments" / "readset"
    folder.mkdir(parents=True)
    for source in list(HERE.glob("*.py")) + [HERE / "readset.sql"]:
        shutil.copyfile(source, folder / source.name)
    (target / GOLDEN).parent.mkdir(parents=True)
    shutil.copyfile(ROOT / GOLDEN, target / GOLDEN)
    return folder


def tests_pass(folder, pattern):
    # The copy's own directory comes first on sys.path, so its modules shadow the
    # originals; db.py and the other root modules resolve through PYTHONPATH.
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(folder), "-p", pattern],
                            capture_output=True, text=True, env=env, cwd=folder)
    return result.returncode == 0, result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""


def check(temp, mutations, pattern, start):
    """Returns how many mutations survived or did not apply; None when the unmutated copy fails."""
    passed, summary = tests_pass(copy_tree(Path(temp) / f"baseline-{start}"), pattern)
    print(f"{'ok ' if passed else 'FAIL'} unmutated copy, {pattern}: {summary}")
    if not passed:
        return None
    problems = 0
    for number, (name, old, new, what) in enumerate(mutations, start):
        folder = copy_tree(Path(temp) / f"m{number}")
        source = (folder / name).read_text(encoding="utf-8")
        if source.count(old) != 1:
            problems += 1
            print(f"SKIP #{number} {name}: text occurs {source.count(old)} times ({what})")
            continue
        (folder / name).write_text(source.replace(old, new), encoding="utf-8", newline="\n")
        passed, summary = tests_pass(folder, pattern)
        problems += passed
        print(f"{'SURVIVED' if passed else 'killed  '} #{number} {name}: {what}  [{summary}]")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sql", action="store_true", help="Also mutate readset.sql against test_readset_sql.py (needs the database)")
    args = parser.parse_args()
    runs = [(MUTATIONS, "test_readset_oracle.py")] + ([(SQL_MUTATIONS, "test_readset_sql.py")] if args.sql else [])
    total = problems = 0
    with tempfile.TemporaryDirectory() as temp:
        for mutations, pattern in runs:
            found = check(temp, mutations, pattern, total + 1)
            if found is None:
                sys.exit(1)
            total, problems = total + len(mutations), problems + found
    print(f"{total} mutations, {problems} survived or did not apply")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
