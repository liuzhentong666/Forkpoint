English | [中文](README.md)

# AgentDB PostgreSQL: Joint Version Control of Code and Business Tables

> **Cloud service status (2026-10-17): not verified against real cloud services.** None of Alibaba Cloud OSS, GCS or S3 has completed real cloud reads/writes or integration testing, and the ECS state service has not been deployed. Cloud deployment is on hold; only local development and simulated testing are being carried out. Local test results cannot be used as evidence that cloud storage has been verified or that a deployment is usable.

Target scale: for the first phase, a single business table of about **4,000 rows, with 10 Agents writing concurrently**.

This direction follows the PostgreSQL plan you provided: code objects, changesets and the history of business records are kept in the same PostgreSQL database, and ordinary main tables coexist with branch views. The core logic is implemented in SQL / PL/pgSQL, with no SQLite dependency.

It is the first phase of building an Agent-oriented database system: first validate the semantics of versions, isolation, transactions and merging. PostgreSQL is responsible for transactions, recovery and native constraints; this project implements the version model for code and data. It is still a single-node implementation and does not claim to have replaced a distributed Spanner.

Technical blog: [Making Agent work committable, branchable and hand-off-able: the first-phase implementation of AgentDB](docs/TECHNICAL_BLOG.en.md), covering the current architecture, key implementation points, validation results and the questions for the next phase.

Supplementary local review: [Maintenance recovery, restricted roles and multi-Agent experiments](docs/LOCAL_REVIEW_RESULTS.en.md). It includes a publishing conflict from a stale maintainer, two interruption windows, the non-default prefix configuration path, and merging all ten branches. All cloud storage remains not verified against real cloud services.

Real GPT repeat experiment: [ten-round results, per-Agent usage and raw JSONL](docs/GPT_REPEAT_EXPERIMENT.en.md). This group recorded 10 rounds, 8 completed and 8 successful, keeping all failed samples; the raw logs are in the local project's `experiments/gpt_repeat/results/2026-10-17/` and are not committed automatically with the source code.

Before an existing installation uses the new snapshot maintainer, the database owner first runs `python db.py install-manager`, which adds `vc.snapshot_status` and `agentdb_manager` without changing business records or version history. Routine maintenance can add `--role agentdb_manager` to use only the snapshot check/materialize permissions; changing system rules is still done under the original administrator identity. This NOLOGIN role is not automatically granted to other login users.

`vc.merge` now calls `vc.rows_at` only once per table to resolve the target state of all changed rows; conflict rules and history are unchanged (see [Merge timing breakdown](docs/MERGE_BENCHMARK.en.md)). New installations already include it; for existing installations the database owner runs `python db.py install-merge-batch` (`sql/004_merge_batch.sql`, can be run repeatedly, replaces functions only; a database that already has 005 installed refuses to run it, so that 005's `vc.merge` is not reverted; grants are preserved, business records and version history are not changed). `python check_merge_migration.py` verifies this migration on a newly created old-version test database.

History traversal no longer slows down with the length of the whole history (see [History growth](docs/HISTORY_GROWTH.en.md)): parent edges are walked step by step through the index, `vc._file_state` stops as soon as it finds the path, and `vc.merge` traverses the target's ancestors only once and checks only paths that may differ; results, errors and grants are unchanged. A new read-only snapshot policy `vc.snapshot_due` has been added, and the maintenance executor `SnapshotMaintenance.run_due` materializes snapshots according to its result. New installations already include it; for existing installations the database owner runs `python db.py install-bounded-history` (`sql/005_bounded_history.sql`, can be run repeatedly). `check_history_migration.py` verifies it on a newly created old-version test database.

## Manager Agent state service with replaceable cloud storage

The design goal is to save the latest state, events, rule versions and evidence of management tasks to the cloud object storage selected by configuration (GCS / Alibaba Cloud OSS / S3), with the long-running process keeping only a bounded in-memory cache. The related adapter code has been written, but there are no results from running against real cloud services yet. The task entry point, conditional publishing, command deduplication, paginated history and the budgeted context API currently have only local implementations and tests.

- [Multi-backend design](docs/STORAGE_BACKENDS.en.md): OSS conditional-append publishing, GCS/S3 conditional writes, compatibility and capacity limits.
- [Original design](docs/CLOUD_STATE_DESIGN.en.md): bounded state, idempotent commands and cross-system recovery.
- [Operation and API](docs/CLOUD_STATE_RUNBOOK.en.md): GCS configuration, the long-running service, context reads and PostgreSQL maintenance examples.
- [Manager Agent protocol](docs/MANAGER_AGENT_PROTOCOL.en.md): operating conventions for the superuser Agent that can be pinned as a cloud rule object.
- [Multi-backend local tests](docs/STORAGE_BACKENDS_VALIDATION.en.md): results from the in-memory service model and SDK test doubles, not including real cloud storage.
- [ECS deployment draft (on hold)](docs/ALIYUN_ECS_DEPLOYMENT.en.md): configuration and scripts kept for future reference; the [local check of the deployment scripts](docs/ALIYUN_ECS_VALIDATION.en.md) does not mean anything has been deployed or that cloud verification has been completed.

The user has reported creating the Hangzhou OSS bucket `agentdb-dev-goudan`, but this project has not verified that bucket's configuration, permissions or real reads/writes; there is also no ECS. No cloud resource purchasing, cloud integration testing or deployment is currently scheduled. The retained deployment draft does not bind a custom domain. The OSS adapter code sets a limit of 524,288 publishes per task, after which a successor task must be created explicitly; this is not a measured cloud capacity, and there is no transparent log rotation yet. PostgreSQL continues to be responsible for versioning code and business data, and management state is not written to the ops schema. Each backend's dependency files and the ECS configuration are kept for later development.

## Current workflow

```text
Agent 选择固定版本
  → 创建分支（新增指针，不复制业务表）
  → BEGIN + begin_change(branch, expected_head)
  → SQL 修改分支视图 + write_file 修改代码
  → finish_change() + COMMIT
  → 比较差异
  → merge(main, source_revision, expected_head)
  → 冲突检查 + PostgreSQL 原生约束检查
  → 一个事务发布代码、历史和 main 业务表
```

In English, the flow above is: the Agent picks a pinned version → creates a branch (adds a pointer, does not copy business tables) → `BEGIN` + `begin_change(branch, expected_head)` → modifies the branch views with SQL + modifies code with `write_file` → `finish_change()` + `COMMIT` → compares differences → `merge(main, source_revision, expected_head)` → conflict check + PostgreSQL native constraint check → one transaction publishes the code, the history and the main business tables.

- `app.products` / `app.orders`: real PostgreSQL business tables, representing main.
- `agent.products` / `agent.orders`: views that read at the selected version; branch writes are captured by `INSTEAD OF` triggers.
- `vc.*`: history, version pointers, immutable content, staged changes and rebuildable snapshots.

Creating a branch does not copy business rows or code content; it still writes one branch record and has index/transaction overhead. "O(1)" only describes logical copying work that does not grow with the number of data rows; it must not be taken as a strict latency guarantee.

## Using the existing local PostgreSQL

The current connection is configured for the **existing PostgreSQL 16.15 in Ubuntu WSL**:

| Setting | Current value |
|---|---|
| WSL distribution | Ubuntu |
| Existing cluster | 16/main |
| Client | Ubuntu's `/usr/bin/psql` |
| Connection | Unix socket `/var/run/postgresql`, port 5432 |
| Local system account / database account | postgres / postgres |
| Persistent project database | agentdb |

Windows Python calls the already installed psql through WSL and uses the existing local authentication settings. The connection parameters are in `connection.local.json`, which has been added to the ignore list. The project scripts do not initialize, start, stop or reconfigure PostgreSQL; the old `.runtime` directory is no longer used.

Each call to `Connection.execute()` starts a `wsl.exe` + psql process, which takes about 0.2–2 s on this machine. Clients that need consecutive calls can use `connection.session()` instead: it keeps one psql process and provides the same `execute()` / `sql()`, at about 1–2 ms per call on this machine. Each call behaves the same as starting a separate process: it stops at the first error (non-zero return code, error written to stderr); an unfinished transaction is rolled back; server-side session state (settings, role, temporary tables, advisory locks) and psql variables are reset before the next call. A Session executes only one call at a time; use one per thread or writer. For the merge timing comparison see [Merge timing breakdown](docs/MERGE_BENCHMARK.en.md).

Python 3.10+ is required; no third-party Python packages are needed. Run in this project directory:

```console
python db.py check
```

`check` connects to the maintenance database and prints the real version and connection information. The current `agentdb` project database is installed, and on 2026-10-18 the user ran `install-merge-batch`, `install-bounded-history` and `install-history-health` in order, so it is at the latest state of 001–006 (business data is still empty); for a first installation in another environment, run:

```console
python db.py install
```

The install command first creates the project database from the configuration (if it does not exist yet), then installs the six SQL files in order in one transaction (version engine, demo business tables, restricted maintenance role, batched merge, bounded history traversal and snapshot policy, read-only history health check). Existing installations are upgraded in the order 004 → 005 → 006 with the corresponding `db.py install-*` commands; 004 refuses to run on a database that already has 005 installed. It refuses a target database that already has project schemas or user tables. The project database starts with empty business data and does not import the 4,000 test products.

The installation includes the demo tables `app.products` and `app.orders` and the `agentdb_client` NOLOGIN role. Roles are shared across the whole PostgreSQL instance; if it already exists, only this project's in-database privileges are granted and its role attributes are not modified. Installation and testing currently go through the local postgres administrator account; the Agent permission examples still use `agentdb_client`.

Enter the project database:

```console
wsl --distribution Ubuntu --user postgres --exec /usr/bin/psql -h /var/run/postgresql -p 5432 -U postgres -d agentdb
```

### Changing the local connection configuration

In a new environment, copy `connection.example.json` to `connection.local.json`. Native Windows PostgreSQL uses `127.0.0.1`; set `wsl_distribution` and `wsl_user` to empty, and fill in the path of the installed psql or add it to PATH. Native Linux can also use the local psql directly.

Parameter precedence: command line > `PGHOST` / `PGPORT` / `PGUSER` / `PGDATABASE` / `AGENTDB_PSQL` environment variables > configuration file > defaults.

A remote (non-loopback) PostgreSQL only accepts connections with `sslmode` set to `verify-full` and `sslrootcert` specified; TLS settings are taken only from the configuration file or `--sslmode` / `--sslrootcert`, and inherited `PGSSL*` environment variables are cleared. For the staging connection steps and the dedicated login role see [Staging PostgreSQL connection](docs/STAGING.en.md); for the overall phases and pass criteria see [Production test plan (draft)](docs/PRODUCTION_TEST_PLAN.en.md).

When a password is needed, you can run `python db.py check --password` in your own terminal; input is hidden and not saved. For long-term configuration PostgreSQL's [password file](https://www.postgresql.org/docs/current/libpq-pgpass.html) is also supported: the native Windows client uses `%APPDATA%\postgresql\pgpass.conf`; the WSL client uses the corresponding Linux user's `~/.pgpass`. Passwords are not written into JSON or command arguments.

## Running the integration tests

```console
python run_tests.py
```

The tests use the same existing PostgreSQL service and each time create a dedicated `agentdb_test_<random suffix>` database, in which they install the SQL, generate 4,000 rows and run the integration tests and a 10-connection concurrency check. The tests do not write to the persistent project database `agentdb`.

Results are saved to `test-results-local.json`. The test database is kept for inspection, and the service keeps running. The connecting account needs permission to create databases, install objects and switch to the `agentdb_client` role; the local postgres account currently meets these conditions.

## SQL usage examples

The following uses psql's `\gset` to save query results. Each code block corresponds to one complete transaction or operation.

Initialize main's code and business data:

```sql
BEGIN;
SELECT vc.begin_change('main', vc.head(), 'developer', 'initial data');
INSERT INTO app.products VALUES (1, 'book-1', 'Book', 100, 10);
SELECT vc.write_file('src/pricing.py', convert_to('DISCOUNT = 0', 'UTF8'));
SELECT vc.finish_change() AS initial_revision \gset
COMMIT;
```

Create an isolated branch and modify code and data together:

```sql
SELECT vc.fork('agent-pricing', :'initial_revision');

BEGIN;
SELECT vc.begin_change('agent-pricing', :'initial_revision', 'agent-pricing', 'pricing experiment');
UPDATE agent.products SET price = 90 WHERE id = 1;
SELECT vc.write_file('src/pricing.py', convert_to('DISCOUNT = 10', 'UTF8'));
SELECT vc.finish_change() AS proposed_revision \gset
COMMIT;
```

Read a pinned version:

```sql
BEGIN;
SELECT vc.pin(:'initial_revision');
SELECT * FROM agent.products WHERE id = 1; -- 仍然是 100
COMMIT;

SELECT * FROM vc.diff_rows(:'initial_revision', :'proposed_revision', vc.table_id('app.products'));
SELECT * FROM vc.diff_files(:'initial_revision', :'proposed_revision');
SELECT * FROM vc.history(:'proposed_revision');
```

(The comment `-- 仍然是 100` means "still 100".)

Merge into main:

```sql
BEGIN;
SELECT vc.merge('main', :'proposed_revision', :'initial_revision', 'reviewed-agent');
COMMIT;
```

If main has already moved forward, the `expected_head` check fails. The caller should re-read and decide whether to compare/merge again; it must not overwrite unconditionally.

`finish_change()` only builds the complete version within the current transaction; as with business data, it becomes visible to other connections only at the actual COMMIT. If you forget to call it, a deferred constraint trigger blocks the COMMIT.

## Key protections implemented

- DML on the ordinary main tables must happen inside a versioned write transaction; unregistered writes are rejected.
- Multiple updates to the same row keep only the original `before` and the final `after`; an insert followed by a delete cancels out.
- Branch reads use a pinned baseline; main continuing to update does not change older branches.
- Branch deltas overlay the baseline, including explicit delete markers and support for composite primary keys.
- Code and row changes are published in the same transaction, under the same changeset.
- Merges into main keep the first parent commit and the source commit, and detect conflicts per whole file / whole row.
- When merging into main, ordinary SQL is executed, and the real UNIQUE, FK, CHECK and NOT NULL constraints decide the result.
- Ordinary clients cannot write the history tables directly, nor append rows to an already published changeset.
- Published history does not allow UPDATE, DELETE or TRUNCATE; the database owner/superuser is still a trusted administrator.
- The snapshot cache is identified by `(commit ID, logical table ID)`, not by the ever-changing branch name.

## Current limitations

1. **Fixed schema**: DDL branching, migration merging, historical schema reconstruction and event trigger capture are not implemented yet.
2. **Whole-row conflicts**: concurrent changes to different columns are still reported as a conflict on the same row; automatic per-column merging is not implemented yet.
3. **Business rules**: native constraints can check the rules they cover; cross-row rules such as "the total quantity ordered does not exceed stock" still need a dedicated implementation and cannot be inferred automatically from the diff.
4. **Branches allow partially invalid states**: apart from primary key/type checks, a branch may temporarily violate constraints such as unique keys and foreign keys; only merging into main validates against the real tables.
5. **Read cost**: branch views may resolve/scan the whole logical table; a logical snapshot does not equal the index query performance of an ordinary table. `materialize` only reduces history replay; it does not make JSON views automatically get the original tables' indexes.
6. **Merge cost**: files are checked only for paths recorded since the divergence, and replay of row state is bounded by the snapshot interval; but each merge still traverses all ancestors of the target and the source in full (cheap, but proportional to the length of history, see [History growth](docs/HISTORY_GROWTH.en.md)), and a maintenance executor is needed to materialize snapshots according to `vc.snapshot_due`, otherwise `rows_at` replays all commits since the last snapshot. Not suitable for direct extension to large-scale history or large merges.
7. **Compatibility scope**: register an empty ordinary table and import through versioning first; generated/identity columns, arbitrary extension types, general compatibility with other business triggers, or zero-downtime onboarding of existing databases are not supported.
8. **Writes to the same branch are serialized**: the branch head row lock guarantees consistency; different branches can proceed concurrently, but all merging into the same main still contend.
9. **Local single-tenant experiment**: there is no complete authentication, tenant isolation, resource quotas, audit identity binding, version retention/reclamation, cross-node replication or disaster recovery plan.
10. **Publishing boundary for test data**: this example can merge branch data into the local main; in real development, orders produced by experiments should not be merged directly into production; a validated migration plan should be published instead.

For design trade-offs and the revisions to the original plan see [DESIGN_REVIEW.en.md](DESIGN_REVIEW.en.md).
