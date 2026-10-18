English | [中文](VALIDATION.md)

# Local Validation Record

Validation date: 2026-10-15 (Asia/Shanghai). Runtime: **the existing PostgreSQL 16.15 in Ubuntu WSL**, cluster `16/main`, port `5432`. Connected through `/usr/bin/psql` and the Unix socket `/var/run/postgresql`.

**18 integration tests passed, and two groups of 10-connection concurrency checks were also completed.** The output of this run is recorded in `test-results-local.json`. The old `test-results.json` is a historical record from the earlier portable instance and does not represent this run.

## Connecting to the local instance

- Persistent project database: `agentdb`, with the version management objects and demo business tables installed.
- Integration test database: `agentdb_test_b44811c63482`; test data is stored separately and kept for inspection.
- Confirmed that the project database has 0 rows of products and of orders, and 0 unsealed transactions; test data was not written to the project database.
- Running the installation again on the installed project database returns a refuse-to-overwrite error; the original objects are kept.
- The data directory is the existing `/var/lib/postgresql/16/main`, and after validation the cluster state is `online`.
- The scripts no longer call `initdb`, `pg_ctl` or the PostgreSQL runtime programs in the project, and do not change the existing service's authentication configuration.

## Behavior validation

- Changes to code and to ordinary business tables become visible under the same commit; old versions remain readable.
- After main keeps updating, old branches still read the frozen baseline.
- Creating a branch does not copy row_change or blobs.
- Repeated updates are coalesced; an insert followed by a delete cancels out; a primary key change is recorded as a delete and an insert.
- When finish_change is omitted, COMMIT fails and the real business tables are rolled back.
- Unversioned direct writes are rejected; ordinary roles cannot append to published history.
- UNIQUE, FK and CHECK violations in a branch are rejected by native constraints when merging into main.
- Disjoint changes can be merged; row conflicts and file conflicts roll back the whole merge.
- Pinned-version state is identical before and after snapshot materialization.
- A stale branch-head condition cannot overwrite a newer commit.

## Concurrent connections corresponding to 4,000 rows and 10 Agents

| Scenario | Result |
|---|---|
| 10 connections each write a separate branch | 10 succeed, main unchanged |
| Create 10 branches | 0 new business history rows; only branch records added |
| 10 connections holding the same old main head compete to write | 1 succeeds, 9 receive SQLSTATE 40001 |
| End of test | The test database's business table still has 4,000 rows, no unsealed transactions; the original local service keeps running |

## Single-run timing

The total wall-clock time of the separate-branch trial was about 1,715.73 ms, including the overhead of Windows calling WSL and starting the psql client. Measured inside SQL, the median was about 15.38 ms and the maximum about 31.61 ms; this internal timing ends after finish_change and **does not include the final COMMIT**, so it cannot be called durable commit latency.

This is a single local synthetic load, not including real model thinking, cross-region networking or long-term history growth, and with no comparison against Spanner. So it is only used to validate functionality and the experimental method, not as a conclusion about production capacity or product merits.
