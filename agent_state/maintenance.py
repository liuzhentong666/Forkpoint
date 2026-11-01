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
# Agents can turn off their own timeouts, so the keeper enforces these from outside (sql/011_agent_boundary.sql).
BOUNDARY_THRESHOLDS = dict(max_idle_in_transaction_seconds=60, max_transaction_seconds=300)
BOUNDARY_ACTION = "writes are frozen: the database owner fixes the cause, then runs SELECT vc.unfreeze() (see docs/AGENT_BOUNDARY.md)"
LOGIN = r"[a-z][a-z0-9_]{0,62}"
REPORTED = 20  # items kept in task state, which is bounded; the rest are counted


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


class BoundaryKeeper:
    """The manager agent's loop around agent logins, on a connection running as agentdb_keeper.

    Fixed code decides, not a model: the self-check freezes writes when the catalog is wrong, and
    stuck sessions end by rule. Nothing here can loosen a limit. PostgreSQL holds the facts, so
    the published state only reports them; losing it loses nothing.
    """

    def __init__(self, runtime, connection):
        self.runtime, self.connection = runtime, connection

    def thresholds(self, policy_ref):
        """Limits from the policy object's "agent_boundary" section, else the defaults (as history_thresholds)."""
        if policy_ref is None:
            return dict(BOUNDARY_THRESHOLDS), "default"
        engine = getattr(self.runtime, "engine", None)
        if engine is None:
            raise Invalid("Policy thresholds need a state interface that can read policy objects")
        try:
            policy = json.loads(engine.objects.get_bytes(policy_ref))
        except (ValueError, UnicodeDecodeError):
            policy = None
        if not isinstance(policy, dict) or "agent_boundary" not in policy:
            return dict(BOUNDARY_THRESHOLDS), "default"
        section = policy["agent_boundary"]
        if not isinstance(section, dict) or set(section) - set(BOUNDARY_THRESHOLDS):
            raise Invalid("Policy agent_boundary must be an object with only " + ", ".join(BOUNDARY_THRESHOLDS))
        limits = dict(BOUNDARY_THRESHOLDS, **section)
        for name, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86400:
                raise Invalid(f"{name} must be an integer 1..86400")
        return limits, "policy"

    def _read(self, task_id, operation, key, value):
        try:
            record = self.runtime.read(task_id, fresh=True)
        except Missing:
            return None
        if record["state"]["facts"].get("operation") != operation or record["state"]["facts"].get(key) != value:
            raise Conflict("Task ID belongs to a different maintenance target")
        return record

    def _publish(self, task_id, record, command, actor, state, event):
        # A concurrent keeper that published first raises Conflict; the next round compares with it.
        version = record["version"] + 1 if record else 0
        return self.runtime.publish(task_id, command_id=f"{command}-{version}", actor=actor,
                                    expected_version=record["version"] if record else None,
                                    state=state, event=event)["version"]

    def check(self, actor, policy_ref=None, task_id="agent-boundary"):
        """One round: the self-check (vc.boundary_check freezes on a problem), then stuck sessions.

        The database acts first, so a Conflict while publishing costs only the report. Publishes
        when something changed; a quiet round publishes nothing.
        """
        record = self._read(task_id, "agent_boundary", "operation", "agent_boundary")
        if record:
            if policy_ref is not None and record["state"]["policy_ref"] != policy_ref:
                raise Conflict("Task uses a different fixed policy version")
            policy_ref = record["state"]["policy_ref"]
        limits, source = self.thresholds(policy_ref)
        status = json.loads(self.connection.sql("SELECT vc.boundary_check();"))
        ended = json.loads(self.connection.sql(
            f"SELECT vc.end_stuck_agent_sessions({limits['max_idle_in_transaction_seconds']},"
            f"{limits['max_transaction_seconds']},{'true' if status['frozen'] else 'false'});"))
        before = record["state"]["facts"] if record else dict(ended_total=0, last_ended=[])
        problems = status["problems"]
        facts = dict(operation="agent_boundary", thresholds=limits, thresholds_source=source,
                     frozen=status["frozen"] and status["frozen"][:2000], problems_total=len(problems),
                     logins=len(status["logins"]),
                     open_databases=status["open_databases"][:REPORTED],
                     ended_total=before["ended_total"] + len(ended),
                     last_ended=(before["last_ended"] + ended)[-REPORTED:])
        # The engine allows 16 uncertainties of 1024 characters; problems_total says how many there were.
        uncertainties = [problem[:1024] for problem in problems[:15]]
        if status["open_databases"]:
            uncertainties.append(("Other databases accept any login: " +
                                  ", ".join(status["open_databases"][:REPORTED]))[:1024])
        state = dict(goal="Keep agent logins inside the boundary", status="running" if status["frozen"] else "completed",
                     next_action=BOUNDARY_ACTION if status["frozen"] else "", uncertainties=uncertainties,
                     facts=facts, policy_ref=policy_ref, evidence_refs=[])
        result = dict(task_id=task_id, frozen=status["frozen"], problems=problems, ended=ended)
        keys = ("status", "next_action", "uncertainties", "facts", "policy_ref")
        if record and all(record["state"].get(key) == state[key] for key in keys):
            return dict(result, published=False, version=record["version"])
        event = "Writes frozen" if status["frozen"] else f"Boundary checked; {len(ended)} session(s) ended"
        return dict(result, published=True, version=self._publish(task_id, record, "boundary", actor, state, event))

    def commits_by(self, login):
        """(login's commits on main not yet reverted, newest first; its commits already reverted).

        main's first-parent chain, 1000 at a time. Commits an agent lands there are merges, whose
        message the engine writes, so no agent can name one as already reverted.
        """
        own, reverted, cursor = [], set(), self.connection.sql("SELECT vc.head('main');")
        while cursor:
            page = json.loads(self.connection.sql(
                "SELECT coalesce(json_agg(json_build_object('id',h.id,'parents',h.parents,'author',h.author,"
                f"'message',h.message) ORDER BY h.n),'[]') FROM vc.history('{uuid.UUID(cursor)}',1000) "
                "WITH ORDINALITY h(id,parents,author,message,created_at,n);"))
            for commit in page:
                revert = re.fullmatch(r"revert ([0-9a-f-]{36})", commit["message"])
                if revert:
                    reverted.add(revert.group(1))
                if commit["author"] == login:
                    own.append(commit["id"])
            cursor = page[-1]["parents"][0] if page and page[-1]["parents"] else None
        return [c for c in own if c not in reverted], [c for c in own if c in reverted]

    def undo_login(self, actor, login, task_id=None):
        """Stop a login, then revert its commits on main, newest first, without judging what they did.

        Stops at the first refusal instead of skipping it: a row changed since belongs to newer work, and
        undoing older commits around it would leave a state nobody had. A rerun picks up where this left off.
        """
        if not isinstance(login, str) or not re.fullmatch(LOGIN, login):
            raise Invalid("Not a login name")
        task_id = task_id or f"undo-{login}"
        record = self._read(task_id, "undo_login", "login", login)
        # Sessions end first, since an open transaction could hold the budget row vc.stop_agent updates,
        # and again after, for one that reconnected in between.
        end_sessions = f"SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE usename='{login}';"
        self.connection.sql(end_sessions)
        self.connection.sql(f"SELECT vc.stop_agent('{login}');")
        self.connection.sql(end_sessions)
        pending, reverted = self.commits_by(login)
        refused = None
        for position, revision in enumerate(pending):
            outcome = self.connection.execute(f"SELECT vc.revert('{revision}');")
            if outcome.returncode:
                refused = dict(revision=revision, error=outcome.stderr.strip()[:1000])
                pending = pending[position:]
                break
            reverted.append(revision)
        else:
            pending = []
        facts = dict(operation="undo_login", login=login, reverted_total=len(reverted), reverted=reverted[:REPORTED],
                     remaining_total=len(pending), remaining=pending[:REPORTED], refused=refused)
        state = dict(goal=f"Undo what {login} landed on main", status="running" if refused else "completed",
                     next_action=f"undo the newer commit that changed these rows, then rerun undo for {login}" if refused else "",
                     uncertainties=[refused["error"]] if refused else [], facts=facts, policy_ref=None, evidence_refs=[])
        version = self._publish(task_id, record, "undo", actor, state,
                                f"Reverted {len(reverted)} commit(s) by {login}, {len(pending)} left")
        return dict(task_id=task_id, login=login, reverted=reverted, remaining=pending, refused=refused, version=version)
