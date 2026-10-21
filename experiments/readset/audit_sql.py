"""Re-derive stale commits and rule violations from vc history in kept run databases.

  python experiments/readset/run_readset.py --seeds 1 --workloads W2 W3 --keep-databases --output-dir <dir>
  python experiments/readset/audit_sql.py <dir>

Independent of the harness's recorded reads and of oracle.py. Every proposal is
one commit on a branch forked from main, so the version a sale was decided on is
the source commit's parent. For each merge commit in main's history whose source
wrote a bundle row, the sale is stale when the other product of the bundle differs
between the merge's first parent and that version. Rule violations are bundle
totals below the minimum, read directly from app.products. Exits 1 on any mismatch
with results.json, or when the directory has no kept run database.
"""
import argparse
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from db import Connection, add_connection_arguments
from run_readset import BUNDLE_MINIMUM, BUNDLE_ROWS

FIRST, LAST = BUNDLE_ROWS[0], BUNDLE_ROWS[-1]
QUERY = f"""
WITH t AS (SELECT vc.table_id('app.products') AS id),
merges AS (
  SELECT p1.child AS merge, p1.parent AS first_parent, p2.parent AS source,
         (SELECT s.parent FROM vc.parent s WHERE s.child=p2.parent AND s.ordinal=1) AS decided_at
  FROM vc.parent p1 JOIN vc.parent p2 ON p2.child=p1.child AND p2.ordinal=2
  WHERE p1.ordinal=1 AND p1.child IN (SELECT id FROM vc._ancestors(vc.head('main')))),
sales AS (
  SELECT m.*, {FIRST}+(((r.pk->>'id')::int-{FIRST}) # 1) AS partner
  FROM merges m JOIN vc.row_change r ON r.changeset=m.source
  WHERE (r.pk->>'id')::int BETWEEN {FIRST} AND {LAST}),
totals AS (
  SELECT (id-{FIRST})/2 AS bundle, sum(stock) AS total FROM app.products
  WHERE id BETWEEN {FIRST} AND {LAST} GROUP BY 1)
SELECT json_build_object(
  'bundle_sales',(SELECT count(*) FROM sales),
  'stale',(SELECT count(*) FROM sales s WHERE
     (SELECT r.doc FROM vc.rows_at(s.first_parent,(SELECT id FROM t)) r WHERE r.pk=jsonb_build_object('id',s.partner))
     IS DISTINCT FROM
     (SELECT r.doc FROM vc.rows_at(s.decided_at,(SELECT id FROM t)) r WHERE r.pk=jsonb_build_object('id',s.partner))),
  'violations',(SELECT count(*) FROM totals WHERE total<{BUNDLE_MINIMUM}),
  'bundle_totals',(SELECT json_object_agg(bundle,total ORDER BY bundle) FROM totals));
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="A run directory holding results.json, made with --keep-databases")
    add_connection_arguments(parser)
    args = parser.parse_args()
    results = json.loads((args.folder / "results.json").read_text(encoding="utf-8"))
    retained = set(results["databases_retained"])
    runs = [run for run in results["runs"] if run["database"] in retained]
    if not runs:
        parser.exit(1, "No kept run database in this directory; rerun run_readset.py with --keep-databases.\n")
    owner = Connection(args)
    mismatches = 0
    for run in runs:
        got = json.loads(owner.sql(QUERY, run["database"]))
        ok = got["stale"] == run["counts"]["stale_commits"] and got["violations"] == run["counts"]["rule_violations"]
        mismatches += not ok
        print(f"{'ok' if ok else 'MISMATCH'} {run['run']}: SQL stale {got['stale']}, violations {got['violations']} "
              f"({got['bundle_sales']} bundle sales; totals {got['bundle_totals']}); results.json "
              f"{run['counts']['stale_commits']}, {run['counts']['rule_violations']}")
    print(f"{len(runs)} kept run database(s) checked, mismatches: {mismatches}")
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
