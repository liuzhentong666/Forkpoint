"""A resumable, idempotent PostgreSQL snapshot maintenance example.

The cloud task records intent and verification. PostgreSQL remains authoritative
for the actual snapshot. This executor does not claim cross-system transactions.
"""
import copy
import json
import uuid
from .storage import Conflict, Invalid, Missing


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
        # Even a cloud 'completed' record is checked against the real system.
        actual = self.inspect(target)
        if not actual["revision_exists"] or not actual["table_exists"]:
            raise Invalid("PostgreSQL target does not exist")
        applied = False
        if not actual["snapshot_exists"]:
            self.connection.sql(f"SELECT vc.materialize('{target['revision']}','{target['table_id']}');")
            applied = True
        if after_apply:
            after_apply()  # Test hook: crash after DB commit, before cloud receipt.
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
