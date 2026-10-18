English | [中文](MERGE_BENCHMARK.md)

# Ten Branches Merging into main: Timing Breakdown

Date: 2026-10-17. **Local synthetic load only**: 10 scripted writers on the existing Ubuntu WSL PostgreSQL, not large-language-model Agents; it does not represent production capacity, and no cloud services were accessed. At the time of this measurement, neither `vc.merge` nor the version engine had been modified, and no fix had been implemented; the later implementation and re-measurements are in the two sections "Re-measurement after implementing long-lived connections" and "Re-measurement after batch-resolving the target state".

Entry point: [`benchmark_merge.py`](../benchmark_merge.py). Raw results: [`merge-benchmark-results.json`](../merge-benchmark-results.json), including the client time and server timestamps of every call. `python benchmark_merge.py --reanalyze` recomputes the derived fields from the raw calls without connecting to the database.

## Problem

[Section 4 of the local review record](LOCAL_REVIEW_RESULTS.en.md) measured: the phase of ten writers merging into main took 9.47 s, with a median single merge of 7.0 s and 45 stale-head (40001) retries. We first need to find out where the time goes, before choosing between a merge queue/backoff, optimizing `vc.merge`, and reducing client overhead.

## Method

- Only one template database is used, `agentdb_test_mergebench_758f297d_tpl`: installation, the 4,000-row seed, the original 18 assertions (the same as `review_local.py` before merge_all), then 10 branches created from the same baseline, each changing 10 rows and writing 1 code file. Each run clones a new database with `CREATE DATABASE … TEMPLATE`, so all runs have the same starting history and revisions. Template history: 12 changesets, 4,100 row changes, 11 paths.
- Each mode is run 3 times, interleaved by round, so that WSL warm-up or drift does not concentrate on one mode. After each run it is verified that there are 4,000 rows, 100 rows changed, 3,900 rows unchanged, 10 files, all 10 source commits reachable, the pinned baseline unchanged, and no leftover work transactions. All 15 runs and 3 profiles passed.
- Timing modes:

| Mode | Client | Scheduling |
|---|---|---|
| `baseline` | The original `review_local.py` merge code (each call starts wsl.exe + psql, role `agentdb_client`), not instrumented | 10 writers start at the same time, all with the baseline as expected |
| `spawn_concurrent` | Same as above, instrumented | Same as above |
| `spawn_serialized` | Same as above, instrumented | Merge one at a time, with expected set to the previous merge result (ideal queue, no 40001) |
| `persistent_concurrent` | One resident psql session per writer, instrumented | Start at the same time, retry rules unchanged |
| `persistent_serialized` | Resident sessions, instrumented | Merge one at a time |
| profile | `persistent_serialized` + `track_functions='all'` on that test database, with EXPLAIN ANALYZE additionally measuring the component queries | Merge one at a time |

- Instrumentation: in the same psql script, first `SELECT … FROM vc.branch WHERE name='main' FOR UPDATE` takes the main row lock, then the unmodified `vc.merge` is called, recording `clock_timestamp()` between the steps. Since this transaction already holds the lock, `begin_change`'s own FOR UPDATE no longer waits, and the expected check stays as it was. Locking `vc.branch` directly needs owner privileges, so the instrumented writers connect as the owner without SET ROLE; `vc.merge` itself is SECURITY DEFINER, so its internal execution identity is unchanged. Instrumented results match the original client: phase median 14.7 s vs 13.6 s, single-merge median 9.8 s for both, 45 retries for both.
- Breakdown (the parts do not overlap): client overhead = call wall-clock − server time from start to the last timestamp, i.e. process startup, connection and exit; lock wait = start → locked; SQL = locked → merged (the body of `vc.merge`); commit = merged → committed; retry cost = all the time of failed attempts (with their client overhead and lock wait listed separately) + the calls re-reading head after 40001.

## Results (median of 3 runs, min–max in parentheses)

### Merge-phase wall clock

| Mode | Phase time | Median single merge | 40001 retries | Share of time the main lock is held | Gap from one commit to the next lock acquisition (median) |
|---|---:|---:|---:|---:|---:|
| baseline | 13.61 s (13.24–15.75) | 9.84 s | 45 | — | — |
| spawn_concurrent | 14.71 s (14.20–15.73) | 9.80 s | 45 | 6.2 % | 1,604 ms |
| spawn_serialized | 2.82 s (2.59–2.89) | 0.27 s | 0 | 29 % (2 usable runs) | 202 ms |
| persistent_concurrent | 0.92 s (0.90–0.93) | 0.50 s | 45 | 93 % | 4.7 ms |
| persistent_serialized | 0.89 s (0.87–0.91) | 0.09 s | 0 | 97 % | 2.5 ms |

45 = 10×9/2 is deterministic: all writers use the baseline as expected, and in each round only the one that gets the lock succeeds, while the rest receive 40001 immediately after getting the lock.

### Writer time totals (spawn_concurrent, summed over the 10 writers, about 91 s)

| Part | Total | Share |
|---|---:|---:|
| Process/connection overhead of the final attempts | 6.54 s | 7 % |
| Process/connection overhead of failed attempts | 43.03 s | 47 % |
| Re-reading head after 40001 (almost all process startup) | 40.11 s | 44 % |
| Lock wait (final 0.01 s + failed 0.40 s) | 0.41 s | 0.4 % |
| `vc.merge` SQL | 0.81 s | 0.9 % |
| COMMIT | 0.03 s | < 0.1 % |

In all three runs, client process overhead accounted for 98.6–98.8 % of writer time. From the server's point of view, the main lock was held for only 0.86 s out of 13.6 s: after each commit, a loser first has to start a process to re-read head and then start another to retry, about two startups of roughly 1.6 s, during which the lock sits idle. Probes in the same run show that starting a single `SELECT 1` takes a median of 272 ms, starting 10 at the same time a median of 2,037 ms; a resident session round trip is 0.16 ms.

After switching to resident sessions, even with still 45 retries, the phase time dropped from 14.7 s to 0.92 s. The main lock is then held 93 % of the time; the writers' time is mainly queuing for the lock (4.06 s; this is concurrent writers waiting while others execute SQL, not on the critical path). The 45 re-reads took only 34 ms in total.

### Inside `vc.merge` (profile, 3 runs, 10 merges each)

The SQL per merge is about 87 ms (`vc.merge` totals 835 / 869 / 892 ms).

| Function | Calls | Total time | Share of `vc.merge` |
|---|---:|---:|---:|
| `vc.rows_at` (all) | 140 | 605–653 ms | 71–73 % |
| Of which: resolving the target state for each changed row (`SELECT … FROM vc.rows_at(ctx.base) WHERE pk=…`, 10 times per merge) | 100 | 453–496 ms | 54–56 % |
| Of which: inside `vc.diff_rows` (2 registered tables × 2 times) | 40 | 144–157 ms | about 17 % |
| `vc.diff_rows` itself | 20 | 63–65 ms | 7 % |
| `vc._apply_main` + triggers | 100 | 69–73 ms | 8 % |
| `finish_change` / `begin_change` | 10 / 10 | 20–33 / 14–19 ms | 4 % |
| `_file_state` (11 paths × 3) | 230 | 8–13 ms | 1 % |
| `_ancestors`, `merge_base` | 30 / 10 | about 10 / 8 ms | 2 % |

EXPLAIN ANALYZE (5 measurements each in the template state and in the state before the 10th merge): single-key `rows_at` 6.5–9.8 ms, each time resolving the complete 4,000 rows and then filtering; fetching all 10 changed keys at once (including diff_rows) 22–27 ms, while diff_rows alone is 19–21 ms, i.e. batch resolution is about 3–7 ms. From this it is estimated that batch resolution could reduce the SQL per merge from about 87 ms to about 40 ms. **This is an extrapolation; it has not been implemented or measured.** This cost grows as "number of changed rows × number of table rows", so the more rows or the larger a single change, the bigger the gap.

## Recommendations

1. **First reduce client process overhead**: each Agent uses a resident connection or a connection pool, instead of starting `wsl.exe + psql` for every call. This accounts for 98.6 % of writer time, and is why the main lock is idle 94 % of the time. Changing only this, without changing scheduling or touching the SQL, the phase time drops from 14.7 s to 0.92 s (about 16×).
2. **Then batch-resolve the target state inside `vc.merge`**: once the client overhead is removed, the main lock is held 93–97 % of the time, and the critical path is the SQL. More than half of it is the 10 per-row `rows_at` calls. The phase time is estimated to drop from about 0.9 s to about 0.45 s, with the benefit growing with the number of table rows and changed rows. The change must keep the conflict decision semantics and be covered by the original 18 assertions and the merge-all verification.
3. **No merge queue or backoff needed for now**: with resident sessions, the ideal queue is only 2 % faster than 45 retries (0.89 s vs 0.92 s), and the re-reads total 34 ms. The lock is already 93 % busy, so backoff would only add idle time. On a client that still starts a process per call, a queue could reduce 14.7 s to 2.8 s, but it only replaces the expensive retries without addressing the root cause, and is less effective than resident connections. When there are clearly more than 10 writers, the number of retries grows as N(N−1)/2, and it should be measured again then.

## Re-measurement after implementing long-lived connections

Recommendation 1 has been implemented as `db.Session` (see [README](../README.en.md)). `vc.merge` and the version engine are still unmodified, and recommendations 2 and 3 are not yet implemented. Each Session call keeps the semantics of a single process: stop at the first error, roll back an unfinished transaction, reset session state and psql variables. This costs about 1 ms more per call: in the probe the `SELECT 1` round trip median is 1.08 ms, while the bare pipe in the prototype was 0.16 ms. A new check in `run_tests.py` verifies these semantics item by item and compares with the output of a single process.

After the resident session modes of `benchmark_merge.py` were switched to `db.Session`, the measurement was repeated on a new template `agentdb_test_mergebench_05e9004d_tpl` (3 times each, [raw results](../merge-benchmark-session-results.json)):

| Mode | Phase time | 40001 retries | Share of time the main lock is held | Hand-off gap (median) |
|---|---:|---:|---:|---:|
| spawn_concurrent (control) | 15.33 s (14.15–15.49) | 45 | 7.1 % | 1,374 ms |
| `db.Session`, starting at the same time | 1.16 s (0.99–1.19) | 45 (44 once) | 93 % | 7.7 ms |
| `db.Session`, merging one at a time | 0.92 s (0.87–1.13) | 0 | 96 % | 4.1 ms |

The writers of `review_local.py` now each use a Session by default, and `--client spawn` restores the original approach. In one complete run (database `agentdb_test_review_0fbedc9103`), all ten branches were merged and all verifications passed: branch creation phase 0.39 s (originally 4.97 s), merge phase 1.19 s (originally 9.47 s), end to end 2.30 s (originally 14.93 s), still 45 stale-head retries. Opening the 10 sessions takes another 2.07 s, done before timing starts. The original record is saved in `local-review-results.2026-10-17-spawn.json`.

Each tool call of `agent_lab.py` is a new Python process and cannot keep a Session across calls. The optional resident gateway `agent_lab_gateway.py` holds Sessions for the Agents; it is off by default and has not yet been used for any real GPT experiment; see [agent_lab resident gateway](AGENT_LAB_GATEWAY.en.md).

## Re-measurement after batch-resolving the target state (2026-10-18)

Recommendation 2 has been implemented. `vc.merge` calls `vc.rows_at(ctx.base, t.id)` only once per registered table and left-joins it with the result of `diff_rows` to get the target state of all changed rows; when the table has no changes, this call is skipped by a one-time filter condition. The loop processes rows one by one in the original output order of `diff_rows` (WITH ORDINALITY), with the decisions unchanged: a row identical to the source is skipped, otherwise P0001 is raised if the target state differs from the common ancestor; writing to main is still per-row `_apply_main`, and writing to a branch still stages the target state as before_row; file conflicts, parent edges and commit information are unchanged. New installations already include it (`sql/001_core.sql`); old installations run `python db.py install-merge-batch`, i.e. `sql/004_merge_batch.sql`, which does CREATE OR REPLACE with the same definition and keeps the owner, grants and search_path.

Verification (all on dedicated local test databases):

- Before the change, the baseline was run first: `run_tests.py`'s 18 items and the Session check, and `review_local.py`, all passed, using the old `vc.merge`.
- `run_tests.py` adds `merge_resolves_target_state_per_changed_row`, with 13 merge cases in total: rows identical to the source are skipped, identical rows inserted on both sides, before_row taking the target state when merging into a branch, as well as modify/modify, delete/modify, modify/delete, insert/insert, conflict when merging into a branch, and which row is reported when multiple rows conflict. The original 18 assertions are unchanged.
- [`check_merge_migration.py`](../check_merge_migration.py) builds a database with the old `001_core.sql` from commit 8aeb3d7 (`agentdb_test_mergemig_502ef6643a`), first runs the 18 assertions and the cases above, and commits one merge. After running the migration twice in a row: the hashes of business data and history tables are unchanged; function owner, SECURITY DEFINER, configuration and ACL are unchanged; the normalized outputs of the 13 cases (row changes, parent edges, error codes and the keys in errors) are identical item by item to the old function; the 18 assertions pass again; `agentdb_client` can still merge. The script also confirms that the function bodies in 001 and 004 are identical. Results are in [`merge-batch-migration-results.json`](../merge-batch-migration-results.json): this file is a rerun during the pre-commit review (database `agentdb_test_mergemig_21ad82e33c`), with conclusions identical item by item to the first run, whose result file was overwritten. In addition, the migration was run once on an old-version test database through the command-line entry point `db.py install-merge-batch`.
- After the change, `run_tests.py` (database `agentdb_test_1ab8001efa3d`) and `review_local.py` (database `agentdb_test_review_16e28e9e61`) all passed. In `review_local.py`, the ten-branch merge phase took 0.49 s, end to end 1.79 s, with 45 stale-head retries. The baseline run with the old function on the same day before the change was 1.29 s and 2.65 s.

The results of `python benchmark_merge.py --runs 3 --modes persistent_concurrent persistent_serialized --output merge-benchmark-batched-results.json` are in the [raw results](../merge-benchmark-batched-results.json). The template database is `agentdb_test_mergebench_bb4c8a05_tpl`, with the same history structure as the previous two; verification of all runs and profiles passed. In the table below, "before the change" is taken from above: the SQL row comes from the first profile, and the other rows from the Session re-measurement.

| Metric | Before the change | After batch resolution |
|---|---:|---:|
| `vc.merge` SQL per merge (profile, median of 30 merges) | about 87 ms (84–89) | **42.8 ms** (37.6–59.1) |
| `vc.rows_at` calls / total in the profile (10 merges per run) | 140 calls / 605–653 ms | 50 calls / 186–195 ms |
| `db.Session` starting at the same time: phase time | 1.16 s (0.99–1.19) | 0.84 s (0.83–0.88) |
| Same: total writer `vc.merge` SQL | 986 ms | 508 ms |
| Same: share of time the main lock is held | 93 % | 77 % |
| `db.Session` merging one at a time: phase time | 0.92 s (0.87–1.13) | 0.66 s (0.54–0.79) |
| Same: median single merge | — | 64 ms |
| 40001 retries (starting at the same time) | 45 | 45 |

The SQL per merge dropped to about 43 ms, consistent with the earlier estimate of about 40 ms. `rows_at` is called 5 times per merge: 2 tables × 2 times inside `diff_rows`, plus 1 time for the target state of products; the orders table has no changes, so it is skipped. The remaining SQL time is mainly `diff_rows` (about 19 ms per merge) and per-row `_apply_main` (about 5 ms). The phase time dropped only about 28 %, not by half: as the SQL got shorter, the share of client round trips, COMMIT and lock hand-off on the critical path rose, and lock occupancy also dropped to 77–83 %. Optimizing further would require `diff_rows` to replay the whole table less (for example combining the baseline and source rows_at into one during a merge), or reducing round trips per merge; neither is implemented.

These results come from a different time period, and timing on WSL fluctuates considerably (see the next section); ratios are more reliable than absolute values. The load is the same as the previous two: 4,000 rows, 10 branches, each branch changing 10 rows and writing 1 file. Target state resolution used to grow as "number of changed rows × number of table rows", and now each table is replayed only once. With more rows or larger single changes the gap would be larger, but this was not measured.

## Environment and caveats

- PostgreSQL 16.15 (Ubuntu 24.04, WSL 2.6.3.0, kernel 6.6.87.2-1), unix socket; `shared_buffers` 128 MB, `synchronous_commit=on`, `fsync=on`, READ COMMITTED. The client is Windows 11 (26200), Python 3.10.11, 22 logical CPUs. The service was not started/stopped or replaced, and the `agentdb` project database was not written to.
- Process startup time fluctuates a lot at different times: in a smoke run earlier the same day, the median for 10 concurrent process starts was about 0.6 s, and 2.0 s in the formal runs; the baseline is therefore also slower (13.6 s) than the 9.47 s recorded in section 4. Conclusions are about ratios, not absolute values.
- Server timestamps come from the WSL guest clock and wall-clock times from Windows `perf_counter`; the two usually differ by about ±2 ms, and in one run the deviation reached about 4 %. So client overhead with resident sessions is below the resolution and shows up as close to 0 or even slightly negative; the actual round trip should be taken as the directly measured 0.16 ms. In the 1st run of spawn_serialized a clock step of about −0.95 s occurred: the 1 affected call is recorded as `clock_step_unattributed`, and that run's lock timeline is not included in the summary.
- The template contains only the history produced by the seed and the original 18 assertions, not the side branches created by `review_local.py`'s maintenance checks. Branch proposals are created one at a time, whereas in the original experiment they were created concurrently. With deeper history and more paths, the lookback costs of `_file_state` and `rows_at` would change.
- The phase time of the resident session modes is measured from after all sessions are established and does not include establishing them: each session takes a median of about 150–165 ms to establish, about 1.6 s in total for 10 established in sequence (see `session_open_ms` in the result JSON). This corresponds to connection-pool usage where a connection is established once and then reused; if each Agent merges only once and then disconnects, this cost cannot be left out.
- `track_functions` itself has overhead: the profile phase took 0.92 s, and an equivalent run without it 0.89 s. The EXPLAIN of the component queries was run outside `vc.merge` as equivalent read-only queries.
- The test databases kept have the prefix `agentdb_test_mergebench_758f297d_` (template, 15 runs, 3 profiles); the list is in `databases_retained` in the result JSON. One smoke run's `agentdb_test_mergebench_827453e5_*` is also kept; it is not counted in the results.

For the effect of history length on merges and commits and how it is handled (`sql/005_bounded_history.sql`), see [History growth](HISTORY_GROWTH.en.md).
