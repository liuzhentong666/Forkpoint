English | [中文](MANAGER_AGENT_PROTOCOL.md)

# Operating Protocol for the Superuser Agent

> Current status (2026-10-17): cloud services are not verified against real cloud services, and cloud deployment is on hold. The cloud hand-off described below is an operating design awaiting integration testing; no uploads, deployments or real cloud reads/writes are currently performed, and local tests are not taken as a record of cloud success.

This file can serve as the task description for the manager Agent, or as the operating protocol content of AGENTS.md / CLAUDE.md. In formal operation, upload the exact text adopted as an evidence object in the selected cloud backend, and pin its hash in the task's policy_ref; later tasks may adopt a new version, but existing tasks cannot switch rules without a record.

## Identity and responsibilities

The management identity is chosen per executor: routine snapshot maintenance uses `agentdb_manager`, which has execute permission only on `vc.snapshot_status`, `vc.materialize`, and the read-only `vc.snapshot_due` and `vc.history_health`; changing the version engine and system rules uses an authorized administrator identity. The restricted role was added by this local review; it does not require setting up human approval for all Agents. Database capabilities are provided by accounts; this file defines how they are used.

The new maintainer depends on `sql/003_manager_role.sql`. On old installations the database owner first runs `python db.py install-manager`; with `--role agentdb_manager` the role's permissions can be checked over a trusted local connection. SET ROLE does not remove administrator capabilities from an execution environment that holds an administrator connection, and cannot be used to claim that process or identity isolation has been established.

The formal state backend is selected through provider and uses cloud object storage that satisfies the project protocol; the domestic (China) target is Alibaba Cloud OSS, with GCS/S3 also kept. Real cloud integration has not been verified. Do not leave a task's only state in the chat context, local files, SQLite or a newly created ops schema. Runtime memory is a discardable cache. All connected work Agents can use the same state-service capabilities.

## Starting or taking over a task

1. Get the task_id from the scheduler; read the task entry point and the latest state. Important operations use fresh=true.
2. Read the rule object that the pinned policy_ref points to. If there is no rule reference, first make explicit which rules this task adopts before continuing with actions that require rule judgments.
3. Get the budgeted context package; when omitted_facts/omitted_evidence are not zero, do not interpret missing fields as non-existent.
4. Read the necessary evidence according to next_action and uncertainties. There is no need to scan the bucket or the whole history.
5. Check the real state of external systems; "in progress" in a log does not mean the action has not happened yet, and "completed" does not replace necessary on-site verification.

## Publishing and execution

Each state command uses a unique command_id and provides the expected_version that was read. When the network result is unclear, retry with exactly the same command_id, actor and content. After a 409, re-evaluate; do not just bump the version number and overwrite.

For external operations, first durably record the intent, the target and the verification method, then execute. PostgreSQL and cloud state publishing do not share a transaction; automatic recovery is only done for operations that have an idempotent or reliable result-checking method. The currently callable maintenance example is pinned-version snapshot materialization; any new executor must first implement its probing and recovery protocol.

Idempotent publishing of commands does not guarantee that an external action is executed only once. For actions that need exclusivity, the executor establishes the corresponding coordination or fencing; the task head's CAS must not be passed off as a database operation lock.

## History growth check

Each `vc.merge` still traverses all changesets reachable from the target head twice (`vc._ancestors` and `vc._merge_base`). On the staging VM, with 2,026 reachable changesets, one full traversal takes 6.8 ms and merge_base 16 ms, about 10% of a 181 ms merge, with about 8 ms more per additional 1,000. Generation numbers are not adopted for now; the manager Agent checks periodically and reports when a change is worthwhile, so humans do not need to query the database.

- **Execution**: construct `SnapshotMaintenance(runtime, connection)` with an `agentdb_manager` connection and call `check_history(actor, branch="main", policy_ref=None, merge_ms=None, task_id=None)`; it can run in the same maintenance round as `run_due`. It only calls `vc.history_health(branch)` (`sql/006_history_health.sql`, STABLE; PostgreSQL rejects any write inside it) and does not change data. On old installations the database owner first runs `python db.py install-history-health`.
- **Facts**: the facts of task `history-health-<branch>` (characters other than `[A-Za-z0-9_-]` in the branch name are replaced with `_`) record `head`, `reachable_changesets` (the number traversed by a merge), `ancestor_walk_ms` (the in-database time of that traversal), `changesets_total`, `first_parent_depth`, `commits_since_snapshot` (per table, the number of first-parent-chain commits from head to the most recent snapshot; `vc.snapshot_due(branch, n)` lists that table exactly when this value is ≥ n) and `max_commits_since_snapshot`, as well as the thresholds used `thresholds`, their source `thresholds_source` (`policy` or `default`), `median_merge_ms`/`merge_samples`, and the number of crossed items `crossed`.
- **Thresholds**: taken from the task's pinned policy_ref. If the rule object is a JSON object and contains `history_health`, only `max_reachable_changesets` (integer 1..10⁹, default 20,000) and `max_median_merge_ms` (greater than 0, at most 3,600,000, default 500) are allowed in it; an invalid key or value raises Invalid, with no publish and no fallback to defaults. When the rule object is not JSON (for example the original text of this protocol) or has no such section, the defaults are used. The median merge time is evaluated only when the caller passes a measured `merge_ms` (such as the merge time from `staging_drills.py growth`); the database does not store merge times. When policy_ref is omitted for an existing task, the task's original one is kept; passing a different policy_ref raises Conflict, and changing rules requires a new task.
- **Reporting**: when any threshold is reached or exceeded, the status is `running`, `next_action` is `merge history needs generation numbers (see docs/STAGING_S4_S5.md)`, and each crossed item is written as one uncertainty (including the measured value and the threshold). When nothing is crossed, the status is `completed`, and `next_action` and uncertainties are empty. An Agent taking over can learn the conclusion by reading this task; after a threshold is crossed, plan the generation-number change as a separate version engine change.
- **Idempotency**: on a repeated check, if every item except `ancestor_walk_ms` is the same as the published state, nothing is published (returns `published=false`); only a change in head or the values publishes a new checkpoint (command_id `history-health-<version>`). When a concurrent checker publishes first, Conflict is returned, and re-running compares against its result. Each publish uses one unit of the task's publish quota (524,288 per task on OSS); run it on the maintenance cycle, not after every merge.

## Context and facts

Save the goal, the actual version, verified results and open actions in structured form. Explanatory summaries reference the original evidence and cannot replace it. Complete reports are stored as objects, and the state keeps only necessary fields and references. When the state exceeds 32 KiB, split the task or the fact objects; do not discard facts to pretend to meet the limit.

Completing a task requires publishing the verification results and evidence references. State which parts are not yet verified; in-memory backend tests, SDK contract tests and each backend's real cloud integration testing must be reported separately.
