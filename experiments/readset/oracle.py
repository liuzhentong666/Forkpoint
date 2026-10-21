"""Independent checks and scoring for the read-set experiment (docs/READSET_EXPERIMENT.md).

Nothing here looks at an arm's read set or goes through the xr prototype: stale
commits are judged from each writer's ground-truth dependencies against vc
history, rule violations from the final rows. The pure functions take plain
data so they can be tested on constructed histories; collect_parent_rows is the
only database read and uses vc.parent and vc.rows_at directly.
"""
import json
import statistics

READSET_ARMS = ("rs_declared", "rs_undercite", "rs_tracked")
# Written in docs/READSET_EXPERIMENT.md before any run: blind retry lets every
# second sale in a bundle through.
PREDICTED_BLIND_VIOLATIONS = {"W2": 5, "W3": 2}


def signature(decision):
    """What a decision would change: its row values and file; None when the writer abstains."""
    if decision is None:
        return None
    return json.dumps(dict(rows=decision["rows"], file=decision["file"]), sort_keys=True)


def classify_rethink(previous, current):
    """A rethink is wasted when the recomputed decision changes exactly what the old one did."""
    return "wasted" if signature(previous) == signature(current) else "useful"


def stale_commits(landed, parent_rows):
    """Merges whose decision used a value that main no longer had when they landed.

    landed:      [{"writer": 3, "merge_commit": "<uuid>", "used": {3002: {...doc}}}]
                 used holds the doc the writer read for each true dependency.
    parent_rows: {"<merge commit>": {3002: {...doc at its first parent}}}
    Returns one entry per stale merge commit, listing every dependency that differed.
    """
    stale = []
    for item in landed:
        if not item["used"]:
            continue
        if item["merge_commit"] not in parent_rows:
            raise ValueError(f"No first-parent rows for merge commit {item['merge_commit']}")
        before = parent_rows[item["merge_commit"]]
        changed = [dict(key=key, used=doc, at_parent=before.get(key))
                   for key, doc in sorted(item["used"].items()) if before.get(key) != doc]
        if changed:
            stale.append(dict(writer=item["writer"], merge_commit=item["merge_commit"], dependencies=changed))
    return stale


def rule_violations(stock, bundles, minimum):
    """Bundles whose total stock on the final main is below the minimum."""
    return [dict(bundle=list(pair), total=stock[pair[0]] + stock[pair[1]])
            for pair in bundles if stock[pair[0]] + stock[pair[1]] < minimum]


def collect_parent_rows(db, table_id, landed):
    """For each landed merge commit, the dependency rows at its first parent (main just before it)."""
    pairs = [(item["merge_commit"], key) for item in landed for key in item["used"]]
    if not pairs:
        return {}
    values = ",".join(f"('{commit}'::uuid,{int(key)})" for commit, key in pairs)
    rows = json.loads(db.sql(f"""
      SELECT coalesce(json_agg(json_build_object('commit',x.c,'key',x.k,
        'doc',(SELECT r.doc FROM vc.rows_at(p.parent,'{table_id}') r WHERE r.pk=jsonb_build_object('id',x.k)))),'[]')
      FROM (VALUES {values}) x(c,k) JOIN vc.parent p ON p.child=x.c AND p.ordinal=1;"""))
    result = {}
    for row in rows:
        result.setdefault(row["commit"], {})[row["key"]] = row["doc"]
    return result


def criteria(runs):
    """The adoption criteria of docs/READSET_EXPERIMENT.md, evaluated over run records."""
    def pick(arms, workloads):
        return [r for r in runs if r["arm"] in arms and r["workload"] in workloads]

    safe, blind = pick(("rs_declared", "rs_tracked"), ("W2", "W3")), pick(("head_blind",), ("W2", "W3"))
    safety = (bool(safe) and bool(blind)
              and all(r["counts"]["stale_commits"] == 0 and r["counts"]["rule_violations"] == 0 for r in safe)
              and all(r["counts"]["rule_violations"] == PREDICTED_BLIND_VIOLATIONS[r["workload"]] for r in blind))
    w1, w3 = pick(("rs_declared",), ("W1",)), pick(("rs_declared",), ("W3",))
    cost = (bool(w1) and bool(w3) and all(r["counts"]["rethinks"] == 0 for r in w1)
            and all(r["counts"]["rethinks"] <= 2 and r["counts"]["wasted_rethinks"] == 0 for r in w3))
    readset = pick(READSET_ARMS, ("W1", "W2", "W3"))
    validation = [v for r in readset for v in r["timing"]["validation_ms"]]
    merge = [v for r in readset for v in r["timing"]["vc_merge_ms"]]
    overhead = dict(validation_median_ms=round(statistics.median(validation), 3) if validation else None,
                    vc_merge_median_ms=round(statistics.median(merge), 3) if merge else None)
    overhead["passed"] = bool(validation and merge) and overhead["validation_median_ms"] <= overhead["vc_merge_median_ms"]
    integrity = (bool(runs) and all(r["verification"]["ok"] for r in runs)
                 and all(r["counts"]["failed"] == 0 for r in readset))
    return dict(safety=safety, cost=cost, overhead=overhead, integrity=integrity,
                adopt=safety and cost and overhead["passed"] and integrity)
