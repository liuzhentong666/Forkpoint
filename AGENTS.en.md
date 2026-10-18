English | [中文](AGENTS.md)

# AgentDB: Conventions for Agent Development and Maintenance

Scope: this directory and its subdirectories. Last updated: 2026-10-18.

**Current cloud service status: not verified against real cloud services; cloud deployment is on hold.** OSS, GCS and S3 have no real integration results, and ECS has not been deployed. The user is not currently scheduling cloud resource purchases or deployment; only continue local development and simulated testing, and do not automatically purchase, deploy, or run tests that access real cloud services. Results from SDK test doubles, the in-memory service model, local HTTP or the local PostgreSQL must not be described as cloud storage having been verified.

This file is the task entry point and the architectural constraints, not a complete operations history. Read this file first, then read the modules and documents relevant to the task, rather than loading the whole project into context every time. The user's current explicit requests take precedence over this file; this file does not change the permission boundaries of the runtime environment.

## Sources and how they are adopted

- [Anthropic: Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control): re-read on 2026-10-16; the page is marked as updated on 2026-10-15. Its architectural principles are adopted and applied to this project's database and management-state scenarios.
- [Module refactoring framework](F:/模块重构原则/模块重构框架.md): its staged, behavior-preserving, verifiable and reversible refactoring process is adopted.
- The current implementation and the constraints the user has settled are in [README](README.en.md), [Version engine design](DESIGN_REVIEW.en.md), [Cloud state design](docs/CLOUD_STATE_DESIGN.en.md) and [Manager Agent protocol](docs/MANAGER_AGENT_PROTOCOL.en.md). The specific interface, capacity and transaction requirements below come from this project.

## Distilled Scion principles

Oriented toward online clients; the server takes on the complex computation and clients stay lightweight; full capability is provided through APIs; components can scale independently; commits are incremental and global coordination is reduced; working copies are built on demand. Immutable content supports reuse, derived indexes are rebuildable, and cache layers isolate failures. Measure the actual load, avoid replacing indexes with full-history scans, and also avoid piling up complexity for hypothetical problems.

These are design directions. Scion's scale, performance data and internal implementation are not capabilities this project already has; its product choices on permissions and approvals are not transplanted directly into this project.

## Project positioning and current boundaries

- The first-phase load is a single table of about 4,000 rows with 10 concurrent Agents. The goal is to test the Agent workflows for branching, pinned versions, joint commits, maintenance and recovery; do not infer that PostgreSQL or Spanner can no longer meet the needs just because the databases were once aimed at human developers.
- Code and business data versions are managed by the existing Ubuntu WSL PostgreSQL. Use `connection.local.json` and `db.py`; do not replace it with SQLite, a temporary PostgreSQL instance, or the service in the old `.runtime`.
- The formal persistent state of management tasks is designed to use cloud object storage that satisfies the project protocol; GCS, Alibaba Cloud OSS and S3 are candidate adapters and have not completed real cloud integration; at runtime a bounded in-memory cache is used. Do not switch to a PostgreSQL `ops` schema, local recovery files or an on-disk state store because cloud work is on hold. Source code, configuration and local test reports can still be kept in the project directory.
- The GCS, OSS and S3 adapter code, the ECS configuration and the HTTP maintenance client have been written. The user reports having created the Hangzhou OSS bucket `agentdb-dev-goudan`, but its configuration, permissions or real reads/writes have not been verified, and there is no ECS yet. MemoryStore is for tests only. The deployment document is only a [draft on hold](docs/ALIYUN_ECS_DEPLOYMENT.en.md), not proof of a completed capability.
- The current version engine is still single-node PostgreSQL, and branch head publishing uses locks; multi-region caching, data sharding, a virtual file system and a unified version-service RPC are not completed capabilities. Adding these capabilities requires a load-based justification and a clear migration plan.

## Design constraints that must be kept

### Server-side semantics and on-demand reads

Version decisions, conflict handling, idempotent publishing and recovery decisions are server-side domain logic. Agent clients submit intent, pinned versions and necessary inputs, and consume structured results; do not make each Agent copy the full history or rewrite its own set of merge rules.

Prefer locating data by pinned revision, task_id, object hash and pagination cursor. Operations such as `vc.diff_files` and `vc.diff_rows` should not be forced to check out the whole code repository because of a new client wrapper. Tools that really need files materialize only the paths they need; file virtualization is a later capability.

Design control requests separately from large-object transfer: state and pointers stay small, and evidence is fetched by reference. New cache layers follow the stable object contract and specify capacity, timeouts, backoff and limits on fallback to the origin, so that a downstream failure does not shift the whole load directly onto the upstream. Until the corresponding load appears, do not pre-build multi-layer services for the sake of being "distributed" in form.

### Version facts, derived data and publishing points

- Sealed changesets, code content and history records cannot be appended to or modified; work transactions are staged first and then sealed by `finish_change()`. The manager Agent's superuser capability does not turn breaking history consistency into a normal way of writing.
- Branches read a pinned version and do not overlay the live main. State replay follows the first-parent chain; common-ancestor queries use all parent edges; the two must not be mixed.
- Snapshots, caches and query indexes should state their input version, completion conditions and how they are rebuilt. Derived data must not become the only source of truth. The task head and the command deduplication index it publishes involve protocol semantics and must not be discarded at will like an ordinary cache.
- In PG, the joint change of code and business records, native constraint checks and version publishing stay in the same transaction; on failure everything rolls back. Cloud state publishing and PG execution do not share a transaction.
- All backends first write immutable objects and then conditionally publish the task entry point through HeadStore. GCS uses generation, S3 uses ETag conditional writes, and OSS uses conditional appends of fixed records; storage markers are used only as equality conditions, and cache ordering uses the application-level version. A repeated command first looks up the published result; the same command_id with the same request returns the original result, while a reused ID with changed content is rejected. After a conflict, re-evaluate the intent; do not just change expected_version and overwrite.
- Content hashes depend on stable encoding rules. Changes to compression, chunking or serialization affect object-format compatibility; they must specify which bytes the hash covers, how versions are identified, and how old objects are read; they must not be treated as a file-move style refactoring.

### Growth cost and context budget

New write paths should state whether their cost depends on the amount changed, the number of table rows or the length of history; first measure write amplification, lock contention, read volume and recovery cost, then decide whether to change the storage engine or shard. Avoid introducing global write locks across tasks; the existing branch locks and snapshot coordination may only be adjusted once correctness is guaranteed by a replacement.

Management state is currently capped at 32 KiB, and each publish saves a complete bounded checkpoint. A routine cold recovery reads two logical objects, the task entry point and the current state; it does not scan the bucket or replay the whole event chain; OSS reads only the fixed record at the tail of the append file each time; this is a constraint on the management context, not a performance promise for all SQL history queries. Logical reads, SDK calls and actual HTTP requests are counted separately.

The default context limit is 8,192 UTF-8 bytes. Required fields are kept; when optional content exceeds the limit, an omission marker and a reference are returned; silently dropping open items or replacing evidence with summaries is forbidden. An exact token budget must specify the tokenizer. Caches limit both the number of entries and bytes; read fresh state before important operations. Being continuously online does not mean the context exists permanently, nor that the network and processes never fail.

In the first version of OSS each task can publish at most 524,288 times, and writes are refused once the 4 GiB append-file limit is reached; when more publishes are needed, explicitly create a successor task; transparent rotation is not implemented. Do not delete/overwrite an active append file, reuse a task_id, or turn on bucket versioning/retention policies while running. History and orphan objects still take up space. Before introducing GC, retention periods or compaction in the future, first define reachability, concurrent publishing and recovery rules; object storage must not be described as needing no capacity management.

### Superuser Agent and hand-off

The manager Agent may change system rules and database structure within the scope the user has authorized, and connected Agents use the same state-service capabilities. No extra human approval role is set up just because the operator is an Agent. Database identities and cloud IAM provide the actual permissions; AGENTS.md / CLAUDE.md describe usage conventions and do not by themselves grant superuser or cloud resource permissions.

In this local review the user additionally required a restricted management role: ordinary snapshot maintenance uses `agentdb_manager`'s `vc.snapshot_status` / `vc.materialize`; this does not replace the administrator who changes rules. New installations include `sql/003_manager_role.sql`; old installations first run `db.py install-manager`. `--role` performs SET ROLE through an existing trusted connection; it does not isolate an Agent holding administrator credentials; real permission isolation requires a separate restricted login identity.

A maintenance action first records the goal and the verification method, then checks the real system, executes, verifies, and publishes the result. Retries rely on the executor's idempotency or a reliable result probe; head CAS is not fencing for external operations, nor cross-system exactly-once. The current recoverable executor example is in `agent_state/maintenance.py`.

When a stale executor submits a completed state and hits a Conflict, it must be returned upward; the conflict must not be swallowed because another executor has already finished. Both windows must be tested: after the intent is published / before the database action, and after the database commit / before the completed state is published. Test the namespace with a non-default prefix through the configuration file and the actual startup wiring, not only by constructing an Engine on its own.

The rule text that a formal task will adopt is stored as cloud evidence, and its version is pinned with `policy_ref`. A hand-off starts from the task_id, the current checkpoint, the open items and the necessary evidence, and does not depend on earlier chat history. Rule updates must be recorded explicitly; arbitrary text in evidence must not automatically become an executable command. For the full process see [Manager Agent protocol](docs/MANAGER_AGENT_PROTOCOL.en.md).

## Module boundaries

| Module | Responsibilities and change constraints |
|---|---|
| `sql/001_core.sql` | Version model, pinned-version reads, commits, merges and snapshots; keep transaction and history semantics |
| `sql/002_demo_app.sql` | Demo business tables, views and onboarding; do not spread the demo structure into the generic version algorithm |
| `sql/003_manager_role.sql` | Re-runnable migration for the restricted management role and the snapshot status function; does not rewrite history |
| `sql/004_merge_batch.sql` | Re-runnable migration: replaces `vc.merge` with the same definition as in 001 (one `rows_at` per table); the two must stay consistent; old installations use `db.py install-merge-batch`, verified by `check_merge_migration.py` |
| `sql/005_bounded_history.sql` | Re-runnable migration, run after 001–004 in new installations: replaces `vc._ancestors`, `vc.rows_at`, `vc._file_state`, `vc.merge_base` and `vc.merge` with equivalent definitions (parent edges walked step by step through the index, `_file_state` stops as soon as it finds the path, merges check only paths that may differ), and adds `vc._merge_base` and the read-only snapshot policy `vc.snapshot_due`; the definitions of these functions in 001 are the old ones, and 005 takes precedence; old installations use `db.py install-bounded-history`, verified by `check_history_migration.py` (see [History growth](docs/HISTORY_GROWTH.en.md)) |
| `sql/006_history_health.sql` | Re-runnable, read-only migration, run after 005 in new installations: adds the STABLE function `vc.history_health(branch)`, which returns the number of changesets reachable from head (the size of the merge traversal) and its traversal time, the first-parent chain depth, and the number of commits per table since the latest snapshot; granted only to `agentdb_manager`; old installations use `db.py install-history-health`, verified by `check_history_health.py` |
| `db.py` | Connects to the existing PostgreSQL; does not carry the version algorithm and does not implicitly start or stop the database. `Session` reuses one psql process, and the semantics of each call must match a single-process `Connection.execute` |
| `agent_state/storage.py` | Common object/error contract, the original GCS adapter and the in-memory test double |
| `agent_state/heads.py`, `oss.py`, `s3.py` | Conditional publishing interface and provider adapters; ordinary overwrites must not replace atomic conditional operations |
| `agent_state/config.py`, `notifications.py` | Backend selection, configuration validation and notification conversion; SDKs are loaded on demand |
| `agent_state/objects.py` | Stable encoding, content addressing, immutable objects and the command index |
| `agent_state/engine.py` | State publishing, version races, idempotency and history semantics |
| `agent_state/runtime.py` | Bounded cache, notification invalidation and context budget |
| `agent_state/server.py` | HTTP, authentication, startup wiring and notification intake; does not duplicate domain rules |
| `agent_state/client.py` | Local maintenance executor's access to the remote state API; no persistent cache, no implicit retries or rewriting of commands |
| `agent_state/maintenance.py` | Performs probeable, recoverable maintenance through the state interface and the database connection; `run_due` calls `run` for each target according to the snapshot policy `vc.snapshot_due`; `check_history` reads `vc.history_health` and publishes facts according to the thresholds in the task's policy_ref (default 20,000 reachable changesets, median merge 500 ms), and when a threshold is crossed reports through uncertainty and next_action that a generation-number index is needed (see [Manager Agent protocol](docs/MANAGER_AGENT_PROTOCOL.en.md#history-growth-check)) |
| `deploy/` | ECS packaging, first installation and systemd; explicit file list, excluding local connection and credential configuration |
| `deploy/staging/` | Script for the staging PostgreSQL's dedicated login role (not a superuser, only CREATEDB, with only SET privilege on the two NOLOGIN roles); run by the server administrator, password set interactively and not written to files (see [Staging connection](docs/STAGING.en.md)) |
| `review_local.py`, `agent_lab.py` | Only for dedicated test databases on the existing local PostgreSQL; record scripted concurrency and real GPT behavior respectively |
| `agent_lab_gateway.py` | Optional loopback long-running process that reuses `db.Session` for `agent_lab.py`'s work actions; off by default, and when it is not running `agent_lab.py` behaves unchanged; no retries, not merged into `agent_state/server.py` (see [design](docs/AGENT_LAB_GATEWAY.en.md)) |
| `experiments/gpt_repeat/` | Real GPT repeat experiment, raw RPC JSONL, independent SQL verification and offline statistics; does not modify the production version algorithm, and does not hide expected conflicts or service interruptions as successes |

The core dependency direction is `runtime → engine → objects / HeadStore → storage adapters`. Network, clock, database and storage are connected through explicit boundaries, which makes replacement and fault injection easy. SDK integration stays in the adapter or startup-wiring layer; a new thin layer must solve an actual coupling, not create an interface for every function.

Public contracts include SQL functions, HTTP requests/responses and errors, configuration items, object encoding/hashing, the head protocol, cursors, command retry semantics and cache freshness. Changes to these are behavioral or compatibility changes, even if a function signature does not change.

## Module refactoring process

The core rule of the source document is adopted: **never change behavior and structure at the same time.** Features or bug fixes and pure structural adjustments are split into separate, verifiable steps; fixes the user has asked for are still completed, and "keeping the old behavior" is not used as an excuse to miss the fix goal.

| Phase | Actual actions | Pass criteria |
|---|---|---|
| 0: Goal and baseline | Write down the motivation, public interfaces, non-goals, acceptance metrics and rollback method; use `rg` to find callers and run the relevant existing suites | Relevant suites are stably all green; existing failures are located first and handled separately from structural changes |
| 1: Safety net | Inventory coverage gaps in public behavior; add characterization/contract tests, keeping golden samples when necessary; cover errors, concurrency, clocks and ordering | Cases are stable and actually catch behavior breakage; small-scale mutation or fault injection can be done on critical paths |
| 2: Seams | Extract external dependencies, inject clock/storage/connection; build a thin facade when necessary; separate side effects from pure computation | Small changes, the relevant full suites pass, external behavior is preserved |
| 3: Mechanical moves | Each step only renames, extracts, moves, or deletes proven dead code | Old contract assertions need no changes; each separate step stays usable and reversible |
| 4: Structural adjustment | Choose incremental replacement, parallel implementation, or switching behind an abstraction depending on risk; compare old and new with recorded inputs | Contract and relevant integration suites pass; performance targets are comparable with the baseline |
| 5: Migrate callers | Switch in small batches, keeping necessary compatibility entry points; watch errors, latency and conflict rates | Each batch passes integration validation; when actual deployment is possible, do staging or canary validation first |
| 6: Wrap-up | After confirming there are no callers, delete the old implementation, switches and temporary adapters; update the documents | Relevant full suites pass, `rg` finds no leftover references, and there are conclusions on the compatibility and recovery paths |

Additional execution rules:

- Do not modify existing public behavior assertions to make a refactoring pass. If a contract really needs to change, split out the behavior change and the compatibility/migration steps explicitly; within the scope the user has already authorized, proceed directly without asking for confirmation again.
- Tests focus on public behavior, boundaries and failure recovery, not on mechanically adding tests per function. Add valuable unit tests for new internal algorithms only when they carry their own risk; for documentation-only changes, checking the content and references is enough.
- When shadow-running a write path with side effects, isolate the target or do only a verifiable dry run; do not execute the same real maintenance action twice to "compare".
- Persistent format migrations must consider old readers, old rules and old data; rolling back code is not rolling back data. The rollback method must be settled before the format is changed.
- Commits should be small enough that their purpose can be stated in one sentence. On unexpected failures, prefer shrinking the change; only revert your own changes from this session and keep the user's existing work. Set phase checkpoints, re-evaluate the benefit, and do not do unbounded rewrites.

## Verification entry points and delivery requirements

The protocol and limitations are as defined in [Multi-backend design](docs/STORAGE_BACKENDS.en.md). The full local adapter test suite requires installing `requirements-storage-tests.txt`; running a single backend needs only its dependencies. Local records are in [Multi-backend local tests](docs/STORAGE_BACKENDS_VALIDATION.en.md), which contain no real cloud results.

From the project root, choose checks according to the scope of the change; after the relevant checks are done, extend/repeat tests only for new changes, failures or unresolved risks.

| Scope | Command and conditions |
|---|---|
| PostgreSQL version engine | `python run_tests.py`: connects to the existing service and creates a dedicated test database; does not import test data into the project business database |
| Management state and HTTP | `.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests -v` |
| agent_lab gateway | Unit tests are included in the previous line (based on golden samples, no database connection); `python compare_agent_lab_gateway.py` creates two `agentdb_test_gpt_*` databases and compares the output and timing of the spawn-a-process-per-call path and the gateway path |
| Recovery read structure | `.\.venv-cloud\Scripts\python.exe benchmark_cloud_state.py`: in-memory backend, checks read volume as history grows |
| Maintenance and PG hand-off | `.\.venv-cloud\Scripts\python.exe integration_cloud_postgres.py --database agentdb_test_EXISTING`: replace with a test database that has the demo installed |
| OSS read structure | `.\.venv-cloud\Scripts\python.exe benchmark_oss_protocol.py`: uses the in-memory OSS service model, does not represent cloud performance |
| Local review and merging everything | `.\.venv-cloud\Scripts\python.exe review_local.py`: creates a dedicated test database; the 18 original assertions, the restricted role, the two interruption windows and the final merging of all ten branches, plus a merge reporting `lock_timeout` while the main lock is held; timings include COMMIT; by default each writer reuses one long-lived connection, and `--client spawn` restores starting a process per call |
| History growth migration | `.\.venv-cloud\Scripts\python.exe check_history_migration.py`: creates an old-version (0bf7309) test database, migrates twice, then compares read and merge results, grants and the new-installation definitions item by item, and runs the snapshot policy through `SnapshotMaintenance.run_due` (MemoryStore) |
| History health check | `.\.venv-cloud\Scripts\python.exe check_history_health.py`: creates a pre-006 (d5c68b0) test database and writes branch, merge and snapshot history, migrates twice, then checks that data, read and merge results and existing functions are unchanged, that `vc.history_health` is STABLE, executable only by `agentdb_manager` and identical to a new installation, and that each branch's values match independent queries and `vc.snapshot_due`; then publishes once through `SnapshotMaintenance.check_history` (MemoryStore), and a rerun does not publish; unit tests are `HistoryHealthTests` in `tests/test_cloud_state.py` |
| Staging VM S4/S5 | `.\.venv-cloud\Scripts\python.exe staging_drills.py growth|backup|restart|crash|netcut|kill --config connection.staging.local.json`: run only against the [staging VM](docs/STAGING_VM.en.md); restart/crash/netcut power off, restart or disconnect the network through VBoxManage, so never point it at an important database; results are in [S4/S5](docs/STAGING_S4_S5.en.md) |
| Real cloud (not run, currently on hold) | `smoke_cloud.py` is kept only as a future integration entry point; it is not run now, and no provider is claimed to have passed verification on its basis |

Real-model experiments follow a pre-registered protocol: record the exact model ID returned by the interface, reasoning effort, runner version, and hashes of the prompts and source files; do not guess an unexposed weight snapshot as a definite version. Fix the sample size and keep failures; do not replace failed rounds with new ones. Save tool request/response JSONL without summarizing or truncating, and save the last value of each Agent's cumulative usage; cached input and reasoning output are sub-items, and missing usage stays unknown. After each actual write, independently check the published content and the other party's branch; an overwrite that is later restored still counts as a failure. Distinguish controlled recovery prompts, scripted stress tests and autonomous model behavior; cloud state is still not verified against real cloud services.

Existing records are in [Real GPT repeat experiment](docs/GPT_REPEAT_EXPERIMENT.en.md). From the project root, use `python -m unittest discover -s experiments/gpt_repeat -p test_repeat_scoring.py -v` to check counting and overwrite judgments; use `python experiments/gpt_repeat/analyze_repeat_logs.py experiments/gpt_repeat/results/2026-10-17` and `python experiments/gpt_repeat/audit_repeat_logs.py experiments/gpt_repeat/results/2026-10-17` to re-analyze the local evidence. These offline commands do not call models and do not write to the database; the commands to rerun the models and their quota impact are in the experiment report, and they must not be rerun automatically for documentation changes.

Changes involving concurrency/recovery must at least verify: stale-version races, retries of old commands, interruptions before and after publishing, cache invalidation races, pinned-version consistency and context omission markers. When a new format is involved, add a compatibility check against old data; do not substitute running successfully on a newly created empty database for migration verification.

Delivery notes state what was changed, why, the local check results and the remaining limitations. Distinguish in-memory simulation, SDK contracts and the real local PostgreSQL; each cloud provider must be noted as not verified against real cloud services, and any wording that packages local results as cloud success must be removed. Capacity and latency conclusions must state the load and environment. Local records are in [Version engine validation](VALIDATION.en.md) and [State module local tests](docs/CLOUD_STATE_VALIDATION.en.md); they cannot be taken as proof that later changes have automatically passed.
