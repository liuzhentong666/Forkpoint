English | [中文](PRODUCTION_TEST_PLAN.md)

# Production Test Plan (Draft)

2026-10-18. **Draft, pending the user's confirmation.** Cloud services are still not verified against real cloud services, and cloud work is still on hold; this document does not change that status, and does not authorize purchasing resources, running `smoke_cloud.py` or using real business data. Items marked "to be decided" need the user's decision; the rest are suggested defaults.

## 1. What needs to be proven

"Production testing" in this project means: in an environment consistent with the expected production shape, using controlled data and load, prove the following four things, leaving reviewable evidence at every phase.

1. **Correctness still holds in the real deployment shape**: joint commits, pinned-version reads, merge conflicts and native constraints, and repeatable migrations behave the same with PostgreSQL on a Linux host and a remote client over TLS as in the local results.
2. **Cloud persistence of management state actually works**: conditional publishing of the task entry point, deduplication of old commands, and hand-off in the two interruption windows hold on real object storage. Currently there are only results from the in-memory model and SDK test doubles.
3. **Failures can be recovered from**: when the database restarts, a backup is restored, the network between the client and the state service is cut, or the object storage returns errors, the system gives a clear result, does not lose committed content, and does not report unknown results as successes.
4. **Under the target load and its growth, costs can be explained**: the target load is a single table of about 4,000 rows with 10 concurrent Agents; additionally measure merge, read and recovery costs after history growth. Record only measured values and the environment; do not write a single result up as a capacity promise.

## 2. Out of scope for this round

- Real business data and production traffic (synthetic data is used throughout until phase 5 passes).
- Multi-region, high-availability failover, sharding and DDL versioning; these are known unimplemented capabilities, and testing only confirms that their absence is not misreported as available.
- A public HTTPS entry point, multi-tenancy and billing.

## 3. Phases and pass criteria

Each phase starts only after the previous phase passes; if any item fails, stop at that phase, locate the problem first and then continue. All results are recorded separately as "in-memory simulation / SDK contract / local PostgreSQL / staging PostgreSQL / real cloud service".

| Phase | Content | Pass criteria | Status |
|---|---|---|---|
| S0 Baseline | Freeze a reviewed commit, with all local suites passing | `run_tests.py`, the state unit tests, `review_local.py` and `check_merge_migration.py` all pass, with the commit hash recorded | Completed: commit 06243d7 |
| S1 Staging PostgreSQL | PostgreSQL 16+ on a Linux host, remote connection over TLS; a dedicated login role (see [Staging connection](STAGING.en.md)) | `db.py check` shows `ssl=true`, `sslmode=verify-full`; running `run_tests.py`, `review_local.py` and `check_merge_migration.py` as the dedicated role all pass; no superuser needed | **Passed (2026-10-18)**: local VirtualBox VM (option B), all four checks passed, see [S1 results](STAGING_S1_RESULTS.en.md) |
| S2 Real object storage | Lift the cloud hold, and run `smoke_cloud.py` only against one isolated prefix | Of ten writers only one publish succeeds; retrying an old command returns the original result; hand-off works after interruptions both before and after publishing; a cold read reads only two logical objects; results note the provider, region and date | Waiting for the user to lift the hold |
| S3 State service deployment | Long-running state service on ECS (or another host) + SSH tunnel | After a service restart, existing tasks are read back through the API and versions checked; `maintain_snapshot.py --state-url` completes one hand-off on the staging PostgreSQL | Waiting for S2 |
| S4 Load and growth | Repeat the target load 3 times; then deepen history by an order of magnitude | Record merge-phase time, retry counts, `vc.merge` SQL time and read volume; side by side with the local results; no assertion failures | **Executed (2026-10-18, local VM)**: target load passed; history growth measured cost growth caused by `_file_state` full-history lookback and the snapshot interval; after the fix (the 005 migration and snapshot policy) was merged, re-measured: at 2,000 commits merge 50.7 s → 181 ms, commit 499 ms → 20 ms, see [S4/S5 results section 7](STAGING_S4_S5.en.md) |
| S5 Failure drills | PostgreSQL restart, backup and restore, network cut, object storage error injection | After recovery `vc.work_tx` is 0; the restored database's head and business table hashes match the backup point; unknown results are all reported as unknown | **Database part passed (2026-10-18)**: backup restore, session termination, normal restart, power-off and network cut, all five passed; the risk exposed by the network cut of a vanished session holding locks has been limited to about 61 s through server timeout settings, and the retest passed, see [S4/S5 results section 7](STAGING_S4_S5.en.md). Object storage error injection waits for S2/S3 |
| S6 Real Agents | Repeat the registered two-Agent experiment in the staging environment | Register a new round according to the [experiment protocol](../experiments/gpt_repeat/PROTOCOL.en.md); 0 overwrite events; failed samples kept | Waiting for S1 |

## 4. Data and permission rules

- Use only synthetic data (the 4,000-row seed of `review_local.py` / `run_tests.py`) until S5 passes and a separate login identity has been established.
- The staging PostgreSQL uses a dedicated login role, and tests are not run as a superuser; the password is given only to `.pgpass` or the `--password` prompt, and is not written into JSON, command arguments or chat.
- Object storage writes only to one dedicated prefix; do not enable versioning, retention policies or lifecycle rules that delete an active prefix (see [Multi-backend design](STORAGE_BACKENDS.en.md)).
- At the end of each phase, record the test databases and object prefixes kept; after testing, the user decides whether to clean them up.

## 5. Items for the user to decide

1. **Where to put the staging PostgreSQL** (decided, 2026-10-18): S1 uses a local VirtualBox VM (option B), with no cloud costs. The client and server share one machine, so S1/S4 timings are for reference only. Whether production finally uses a self-managed cloud host or a managed database is left until the cloud hold is lifted, at which point S1 is briefly rerun in the chosen environment.
2. **Whether to lift the cloud hold** (to be decided): only for S2/S3; whether to use the reported Hangzhou OSS bucket `agentdb-dev-goudan` or a new bucket, and the acceptable cost ceiling.
3. **Whether production eventually touches real business data** (to be decided): if so, backup-restore drills, a separate login identity and auditing must be completed first, and a data migration plan drawn up separately.
4. **Whether the load target changes** (to be decided): keep 4,000 rows / 10 Agents, or give new numbers of rows, Agents and history length.
5. **Who can access the staging environment** (to be decided): decide the number of login roles and the source network ranges in `pg_hba.conf`.

## 6. Rollback and cleanup

Each phase only adds test databases, roles and object prefixes, and does not modify the project database `agentdb`. Rolling back code is not rolling back data: before a migration (such as `004_merge_batch.sql`) is run on the staging database, first confirm that the old function definitions have been saved with a commit. Cleaning up test databases, roles and prefixes is done only after the user confirms.
