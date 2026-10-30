"""A resumable, idempotent PostgreSQL snapshot maintenance example.

The task records intent and verification. PostgreSQL remains authoritative
for the actual snapshot. This executor does not claim cross-system transactions.
"""
import copy
import json
import re
import statistics
import uuid
from .storage import Conflict, Invalid, Missing

# vc.merge walks every changeset reachable from the target head twice; at about
# these sizes a generation-number index pays for itself (docs/STAGING_S4_S5.md).
HISTORY_THRESHOLDS = dict(max_reachable_changesets=20000, max_median_merge_ms=500)
HISTORY_ACTION = "merge history needs generation numbers (see docs/STAGING_S4_S5.md)"
BRANCH = r"[A-Za-z0-9._/-]{1,128}"


class SnapshotMaintenance:
    def __init__(self, runtime, connection):
        self.runtime, self.connection = runtime, connection

    @staticmethod
    def target(revision, table_id):
        try:
            return dict(revision=str(uuid.UUID(revision)), table_id=str(uuid.UUID(table_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise Invalid("Snapshot target requires revision and logical table UUIDs") from error

    def inspect(self, target):
        revision, table_id = target["revision"], target["table_id"]
        # UUID normalization above makes these literals safe. No relation names
        # or user-provided SQL are interpolated.
        return json.loads(self.connection.sql(
            f"SELECT vc.snapshot_status('{revision}','{table_id}');"))

    def due(self, branch="main", interval=100):
        """Targets that the snapshot policy (vc.snapshot_due) names for a branch head."""
        if not isinstance(branch, str) or not re.fullmatch(BRANCH, branch):
            raise Invalid("Branch name for the snapshot policy must match [A-Za-z0-9._/-]{1,128}")
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 100000:
            raise Invalid("Snapshot interval must be an integer 1..100000")
        rows = json.loads(self.connection.sql(
            "SELECT coalesce(json_agg(json_build_object('revision',revision,'table_id',table_id)),'[]') "
            f"FROM vc.snapshot_due('{branch}',{interval});"))
        return [self.target(row["revision"], row["table_id"]) for row in rows]

    def run_due(self, actor, branch="main", interval=100, policy_ref=None):
        """Materialize every due target through run(); one task per revision/table, so reruns resume."""
        return [self.run(f"snapshot-{target['revision']}-{target['table_id']}", actor,
                         target["revision"], target["table_id"], policy_ref)
                for target in self.due(branch, interval)]

    def history_health(self, branch="main"):
        """Read-only measurements of the history vc.merge walks (vc.history_health)."""
        if not isinstance(branch, str) or not re.fullmatch(BRANCH, branch):
            raise Invalid("Branch name for the history check must match [A-Za-z0-9._/-]{1,128}")
        return json.loads(self.connection.sql(f"SELECT vc.history_health('{branch}');"))

    def history_thresholds(self, policy_ref):
        """Thresholds from the policy object's "history_health" section, else the defaults.

        A policy that is not a JSON object (e.g. the protocol text itself) or has
        no such section uses the defaults; a section with unknown keys or values
        out of range is rejected instead of being silently replaced.
        """
        if policy_ref is None:
            return dict(HISTORY_THRESHOLDS), "default"
        engine = getattr(self.runtime, "engine", None)
        if engine is None:
            raise Invalid("Policy thresholds need a state interface that can read policy objects")
        try:
            policy = json.loads(engine.objects.get_bytes(policy_ref))
        except (ValueError, UnicodeDecodeError):
            policy = None
        if not isinstance(policy, dict) or "history_health" not in policy:
            return dict(HISTORY_THRESHOLDS), "default"
        section = policy["history_health"]
        if not isinstance(section, dict) or set(section) - set(HISTORY_THRESHOLDS):
            raise Invalid("Policy history_health must be an object with only " + ", ".join(HISTORY_THRESHOLDS))
        limit = section.get("max_reachable_changesets", HISTORY_THRESHOLDS["max_reachable_changesets"])
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10 ** 9:
            raise Invalid("max_reachable_changesets must be an integer 1..1000000000")
        merge_ms = section.get("max_median_merge_ms", HISTORY_THRESHOLDS["max_median_merge_ms"])
        if isinstance(merge_ms, bool) or not isinstance(merge_ms, (int, float)) or not 0 < merge_ms <= 3600000:
            raise Invalid("max_median_merge_ms must be a number above 0 and at most 3600000")
        return dict(max_reachable_changesets=limit, max_median_merge_ms=merge_ms), "policy"

    def check_history(self, actor, branch="main", policy_ref=None, merge_ms=None, task_id=None):
        """Publish history-growth facts for a branch and flag when a threshold is crossed.

        Never changes PostgreSQL data (vc.history_health is STABLE). merge_ms are
        merge durations the caller observed (e.g. scripts/staging_drills.py growth); without
        them the median-merge threshold is not evaluated. A rerun that measures the
        same values publishes nothing; the ancestor walk time alone is not a change.
        """
        if merge_ms is not None and (not isinstance(merge_ms, list) or not 1 <= len(merge_ms) <= 1000 or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 3600000 for x in merge_ms)):
            raise Invalid("merge_ms must be a list of 1..1000 durations in milliseconds")
        measured = self.history_health(branch)
        task_id = task_id or "history-health-" + re.sub(r"[^A-Za-z0-9_-]", "_", branch)
        try:
            record = self.runtime.read(task_id, fresh=True)
        except Missing:
            record = None
        if record:
            if record["state"]["facts"].get("operation") != "history_health" or \
                    record["state"]["facts"].get("branch") != branch:
                raise Conflict("Task ID belongs to a different maintenance target")
            if policy_ref is not None and record["state"]["policy_ref"] != policy_ref:
                raise Conflict("Task uses a different fixed policy version")
            policy_ref = record["state"]["policy_ref"]
        thresholds, source = self.history_thresholds(policy_ref)
        median = None if merge_ms is None else round(statistics.median(merge_ms), 3)
        crossed = []
        if measured["reachable_changesets"] >= thresholds["max_reachable_changesets"]:
            crossed.append(f"vc.merge walks {measured['reachable_changesets']} reachable changesets on {branch} "
                           f"(threshold {thresholds['max_reachable_changesets']})")
        if median is not None and median >= thresholds["max_median_merge_ms"]:
            crossed.append(f"Median merge time {median} ms over {len(merge_ms)} merges "
                           f"(threshold {thresholds['max_median_merge_ms']} ms)")
        facts = dict(operation="history_health", branch=branch, thresholds=thresholds, thresholds_source=source,
                     median_merge_ms=median, merge_samples=len(merge_ms or []), crossed=len(crossed),
                     **{k: v for k, v in measured.items() if k != "branch"})
        state = dict(goal=f"Report when merge history on {branch} needs generation numbers",
                     status="running" if crossed else "completed",
                     next_action=HISTORY_ACTION if crossed else "", uncertainties=crossed,
                     facts=facts, policy_ref=policy_ref, evidence_refs=[])
        def stable(value):
            value = copy.deepcopy(value)
            value["facts"].pop("ancestor_walk_ms", None)
            return {k: value.get(k) for k in ("status", "next_action", "uncertainties", "facts", "policy_ref")}
        result = dict(task_id=task_id, crossed=crossed, facts=facts)
        if record and stable(record["state"]) == stable(state):
            return dict(result, published=False, version=record["version"])
        version = record["version"] + 1 if record else 0
        # A concurrent checker that published first raises Conflict; rerun to compare with it.
        published = self.runtime.publish(task_id, command_id=f"history-health-{version}", actor=actor,
                                         expected_version=record["version"] if record else None, state=state,
                                         event=("History threshold crossed: " + HISTORY_ACTION) if crossed
                                         else "History below thresholds")
        return dict(result, published=True, version=published["version"])

    def run(self, task_id, actor, revision, table_id, policy_ref=None, after_apply=None, after_intent=None):
        target = self.target(revision, table_id)
        try:
            record = self.runtime.read(task_id, fresh=True)
        except Missing:
            state = dict(goal="Materialize a fixed AgentDB revision/table snapshot",
                         status="running", next_action="Inspect PostgreSQL snapshot state",
                         facts=dict(operation="materialize_snapshot", target=target),
                         uncertainties=["PostgreSQL operation has not yet been verified"],
                         policy_ref=policy_ref, evidence_refs=[])
            try:
                self.runtime.publish(task_id, command_id="snapshot-intent", actor=actor, state=state,
                                     event="Durable intent recorded before PostgreSQL maintenance")
            except Conflict:
                pass
            record = self.runtime.read(task_id, fresh=True)
        state = record["state"]
        if state["facts"].get("operation") != "materialize_snapshot" or state["facts"].get("target") != target:
            raise Conflict("Task ID belongs to a different maintenance target")
        if policy_ref is not None and state["policy_ref"] != policy_ref:
            raise Conflict("Task uses a different fixed policy version")
        if after_intent:
            after_intent()  # Test hook: durable intent, before database inspection/action.
        # Even a 'completed' record is checked against the real system.
        actual = self.inspect(target)
        if not actual["revision_exists"] or not actual["table_exists"]:
            raise Invalid("PostgreSQL target does not exist")
        applied = False
        if not actual["snapshot_exists"]:
            self.connection.sql(f"SELECT vc.materialize('{target['revision']}','{target['table_id']}');")
            applied = True
        if after_apply:
            after_apply()  # Test hook: crash after DB commit, before the receipt is published.
        actual = self.inspect(target)
        if not actual["snapshot_exists"]:
            raise Conflict("Snapshot verification failed")
        if state["status"] == "completed":
            return dict(task_id=task_id, verified=True, applied=applied, version=record["version"])
        verified = copy.deepcopy(state)
        verified.update(status="completed", next_action="", uncertainties=[])
        verified["facts"]["verification"] = actual
        # Completion by another executor does not authorize this stale attempt.
        # Identical-command network retries remain Engine's responsibility.
        result = self.runtime.publish(task_id, command_id="snapshot-verified", actor=actor,
                                      expected_version=record["version"], state=verified,
                                      event="Verified fixed revision/table snapshot in PostgreSQL")
        return dict(task_id=task_id, verified=True, applied=applied, version=result["version"])
