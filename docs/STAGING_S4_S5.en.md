English | [中文](STAGING_S4_S5.md)

# S4 Load and Growth, S5 Failure Drills (Local Virtual Machine)

2026-10-18. Phases S4 and S5 of the [Production test plan](PRODUCTION_TEST_PLAN.en.md), carried out on the VirtualBox virtual machine `agent-db` used for [S1](STAGING_S1_RESULTS.en.md) (Ubuntu 24.04.5, PostgreSQL 16.15); the client is the native Windows psql, connecting over TLS `verify-full` as the non-superuser `agentdb_staging`. **No cloud services were used**; object storage (S2) is still not verified against real cloud services.

Entry points: [staging_drills.py](../staging_drills.py) (`growth`, `backup`, `restart`, `crash`, `netcut`, `kill`) and the existing [benchmark_merge.py](../benchmark_merge.py). Each item creates its own `agentdb_test_*` database on the server, and the raw results are in [staging-results/](../staging-results/). Drills involving power-off, network cuts and restarts control the VM through the local `VBoxManage`, without needing the VM's shell or password.

**Conclusion**: all five S5 drills passed; S4's target load passed, but history growth revealed two costs that grow with history length (section 2), as well as one operational risk (section 4), which need to be dealt with before scaling up; the server and client timeouts of section 4 have been written into the scripts, waiting for the user to apply them on the VM and rerun the network-cut drill. The client and server share one laptop, so all timings are for relative comparison only.

## 1. S4a Target load: ten writers merging into main

`benchmark_merge.py --runs 3 --modes baseline persistent_concurrent --profile-runs 0` (profiling needs a superuser to set `track_functions`, so it was skipped here). [Raw results](../staging-results/merge-benchmark.json)

| Client | Merge phase (median, range) | Median single merge | Stale-head retries |
|---|---:|---:|---:|
| Start a psql process per call | 8.87 s (8.44–9.01) | 6.15 s | 45 |
| One long-lived connection per writer | 2.11 s (1.94–2.63) | 1.32 s | 45 (43 once) |

All 6 runs passed verification (4,000 rows, 100 rows changed, 10 files, no leftover transactions). In long-lived connection mode the main lock is held about 70% of the time, and the writers mainly queue waiting for the lock; one session round trip over TLS to the VM is about 8 ms (about 0.16 ms on the local WSL), starting psql once and logging in takes about 138 ms, and starting 10 at the same time about 638 ms.

## 2. S4b History growth

Each growth commit changes 10 product rows on main and writes one of 50 files; the seed commit has a materialized snapshot. Reads are `EXPLAIN ANALYZE` server execution times (median of 5), and merges and commits are client wall-clock times on a long-lived connection.

[Raw results](../staging-results/growth-before.json) (database `agentdb_test_growth_c0d98c8c27`, completed in 11 minutes, with the computer kept awake)

| New commits on main | Full-table read (head) | Single-row read (head) | Full-table read after materializing a snapshot | Materialize snapshot | Read seed pinned version | One page of history (20 entries) | One 10-row merge | One commit | Database size |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 10 ms | 11 ms | 9 ms | 2 ms | 12 ms | 0.8 ms | 98 ms | — | 12 MB |
| 100 | 26 ms | 24 ms | 13 ms | 73 ms | 8 ms | 1.2 ms | 372 ms | 20 ms | 13 MB |
| 1,000 | 236 ms | 238 ms | 17 ms | 230 ms | 8 ms | 1.6 ms | **15.3 s** | 156 ms | 20 MB |
| 2,000 | **1,205 ms** | 658 ms | 12 ms | 786 ms | 9 ms | 1.1 ms | **50.7 s** | **499 ms** | 26 MB |

- Not growing with history: reading a materialized pinned version (seed, about 10 ms), reads after materializing a snapshot (9–17 ms), history pagination (about 1 ms).
- Growing with history: head reads (1.2 s at 2,000), commits (20 ms → 499 ms), merges (98 ms → 50.7 s, increasing 3.3× from 1,000 to 2,000, more than linear). Single-row reads are as slow as full-table reads, because `rows_at` first computes the state of the whole table and then filters.
- The first run (interrupted by the laptop sleeping, see section 4) measured a consistent trend at 0/100/1,000 commits: merges 93 ms / 378 ms / 19.6 s, head reads 16 / 23 / 457 ms ([record](../staging-results/growth-2350-percall.json)). The difference between the two runs shows that single timings fluctuate considerably, but the orders of magnitude are the same.
- Under the target load (a single table of 4,000 rows), after the main branch accumulates about 1,000 commits, one merge exceeds 15 s; estimating that 10 Agents each commit several times a day, this could be reached within a few weeks. This is a problem that must be dealt with before wider use.

### Cost sources (measured directly on the 2,350-commit database)

[Raw record](../staging-results/growth-2350-percall.json)

| Operation | Time |
|---|---:|
| `vc._file_state(head, path)`, growth file | 761 ms |
| `vc._file_state(head, path)`, file that only appeared in the seed | 710 ms |
| `vc.rows_at(head, products)` full-table read | 871 ms |
| `vc._ancestors(head)` | 530 ms |
| One commit: 10 rows + `write_file` (rolled back) | 753 ms |
| The same commit without writing a file (rolled back) | 41 ms |

- The recursive CTE of **`vc._file_state`** ([001_core.sql](../sql/001_core.sql)) walks the first-parent chain all the way to the root, then joins with `path_change` and takes the most recent entry; it does not stop early even after finding the path. It is called once per `write_file`, and `vc.merge` calls it three times per registered path, so the costs of both commits and merges are proportional to the **whole length of history**, regardless of whether the file was changed recently.
- **`vc.rows_at`** replays along the first-parent chain back to the most recent snapshot. The more commits after the snapshot, the slower the read; immediately after materializing a snapshot it is back to about 10 ms. There is currently no automatic materialization.
- **`vc.merge`** also has to traverse all ancestors (`merge_base`, `_ancestors`). The three together make one 10-row merge take about 20 s at 1,000 commits.

This is consistent with items 5 and 6 of the README's "Current limitations", now with concrete numbers. Directions for improvement (not yet implemented; they are behavior-preserving changes to the version engine that need their own project and coverage by the existing assertions): make `_file_state` stop walking back after finding the path, or maintain a per-snapshot file list; materialize snapshots periodically by number of commits or read cost (which the existing maintenance executor could take on); cache the ancestor set during merges.

Follow-up (2026-10-18, local): the three items above have been implemented on the branch `perf/history-bounded-costs`, with the migration as `sql/005_bounded_history.sql`. It was also found that the main cause was that every step of the recursive traversal did a full-table scan of `vc.parent`. For the before/after numbers see [History growth](HISTORY_GROWTH.en.md); it has not yet been rerun on the staging VM.

## 3. S5 Failure drills

| Drill | Method | Result | Raw record |
|---|---|---|---|
| Logical backup and restore | As `agentdb_staging` over TLS, `pg_dump -Fc` of the 2,350-commit database (1.0 MB, 2.4 s), restored into a new database (2.8 s) | **Passed**: all 13 fingerprints (head, the various history counts and hashes, snapshots, 29 functions, `agentdb_client` grants) match; the restored database's main tables and version state match; after restoring, commits work, and `agentdb_client` can read the new head | [backup.json](../staging-results/backup.json) |
| Session terminated | After a session staged row and file changes on main and before `finish_change`, the owner ran `pg_terminate_backend` on it | **Passed**: the victim session reported `connection to server was lost`; no fingerprint differences, prices of rows 1–10 unchanged; then committed normally | [kill.json](../staging-results/kill.json) |
| Normal restart | ACPI shutdown (11.6 s), then start; connectable after 81.6 s | **Passed**: the postmaster start time changed; no fingerprint differences; main tables and version state match; commits work after the restart | [restart.json](../staging-results/restart.json) |
| Power-off | After 5 writers (1 on main, 4 on branches) committed continuously for 10 s, `VBoxManage controlvm poweroff`; connectable after 61 s, 88 s after the power-off | **Passed**: all 201 acknowledged commits exist and are reachable from their respective heads; `crash_w2` had one in-flight commit that was persisted but whose acknowledgement the client did not receive, and the head is exactly the one after the last acknowledged commit, with complete content; no leftover transactions; main tables and version state match before and after the power-off | [crash.json](../staging-results/crash.json) |
| Network cut | A session holds the main lock (after `begin_change`, not committed), the VM's host-only adapter is unplugged for 20 s, and its client is killed in the meantime | **Passed, and exposed a risk**: during the cut, new connections reported `timeout expired` clearly after 5.1 s; after the network was restored, the original session still held the main lock as "idle in transaction", and was still there throughout the 120 s observation period, with all other writers getting `lock_timeout`; it recovered after the owner ran `pg_terminate_backend`, head unchanged, no leftover transactions | [netcut-defaults.json](../staging-results/netcut-defaults.json) |

## 4. Risk: sessions of vanished clients hold locks for a long time

The server uses PostgreSQL's default settings: `tcp_keepalives_idle` 7200 s, interval 75 s, 9 probes, and `idle_in_transaction_session_timeout`, `client_connection_check_interval` and `lock_timeout` all 0. When a client vanishes mid-transaction (network cut, host sleep, process killed without the close packet arriving), the server only decides the connection is dead after about **2 hours 11 minutes**; until then the locks held by that session stay in effect. While it holds the main lock, all merges queue or time out.

This actually happened once during this testing: during the first growth test the laptop entered Modern Standby (Windows event log 04:25:00–05:07:34); after resuming, the client psql had exited, but the session on the server was still "idle in transaction", and was terminated manually by the owner. The network-cut drill then reproduced the same phenomenon by design ([record](../staging-results/netcut-defaults.json)).

**Handling (2026-10-18, written into the scripts, not yet applied to the VM)**: on the server side [configure_postgres.sh](../deploy/staging/configure_postgres.sh) writes `conf.d/agentdb-staging.conf`; on the client side `lock_timeout` is set during merges. Changing the server configuration is done by the user; for the steps see [Local VM steps](STAGING_VM.en.md#4a-add-the-vanished-client-settings-to-an-already-configured-vm-user-action). Until they are applied and the network-cut drill is rerun, the network-cut result in section 3 still represents the actual state of the VM.

### Longest legitimate idle time inside a transaction

`idle_in_transaction_session_timeout` only counts the time during which a transaction is open and the server is waiting for the client's next statement ("idle in transaction"), not statement execution time. The clients that might pause inside a transaction were checked one by one:

- [run_tests.py](../run_tests.py), [review_local.py](../review_local.py), [agent_lab.py](../agent_lab.py) (including the gateway), and `staging_drills.py`'s growth/crash/restart/backup: each `BEGIN … COMMIT` is sent as one whole psql script (`Connection.execute` writes it to standard input in one go, and `db.Session` executes it with `\i`). psql sends the next statement as soon as it receives the previous result, so the idle time inside a transaction seen by the server is about one round trip: about 0.16 ms on the local WSL, about 8 ms over TLS to the VM (section 1). No script waits inside a transaction for model output, user input or other processes.
- Each tool call of `agent_lab.py` is one complete transaction; model thinking time falls between two transactions. Sessions in the gateway pool roll back and run `DISCARD ALL` after each call, so their idle state is "idle", not inside a transaction.
- Intentional pauses happen only in the drills: kill's victim session is idle while the owner starts psql to query and terminate it, less than 1 s; netcut's lock-holding session goes through a 3 s `lock_timeout` probe, psql startup and VBoxManage between `begin_change` and the network cut, about 4 s. The new lock-timeout check in `review_local.py` holds the lock with `pg_sleep`, whose state is "active", so it is not affected by this setting.
- Long statements (one merge of 50.7 s at 2,000 commits in section 2) are "active" and not affected by this setting. `pg_dump`/`pg_restore` set `idle_in_transaction_session_timeout` to 0 for their own sessions, so the backup drill is not affected.

### Values

| Setting | Value | Effect | Basis |
|---|---|---|---|
| `idle_in_transaction_session_timeout` | 60 s | A session idle inside a transaction for 60 s is ended by the server (FATAL 25P03), the transaction rolls back, and the main lock is released; independent of whether the network recovers | The longest legitimate idle time is about 4 s (the netcut lock holder), and normally about one round trip; 60 s leaves a 15× margin, and still reduces the time main stays locked from about 2 h 11 min to about 1 min. Interactive manual transactions that need longer can `SET idle_in_transaction_session_timeout = 0` in the session |
| `tcp_keepalives_idle` / `_interval` / `_count` | 60 s / 10 s / 6 | Probing starts after 60 s without traffic on the connection. When the peer host is online but the connection no longer exists, the first probe gets an RST; when the peer is completely silent (network cut, sleep), it disconnects after 60 + 6 × 10 = 120 s | A live client's operating system answers the probes even if the process is idle, so it is not disconnected by mistake (the same for idle Sessions in the gateway pool). Cost: when the network is cut for more than about 2 minutes, idle connections are dropped, and a Session's next call fails once and then reconnects (existing behavior) |
| `tcp_user_timeout` | 120 s | Disconnects when sent data has not been acknowledged for 120 s | keepalive only probes when there is no unacknowledged data; without this item, a connection sending data to a vanished client waits for the retransmission limit (about 15 minutes by default on Linux). Consistent with keepalive's 120 s |
| `client_connection_check_interval` | 10 s | While a statement is executing, checks every 10 s whether the client is still there, and cancels the statement if not | When the client has closed (FIN/RST has arrived), cancels within 10 s; when the peer is silent, keepalive first decides the connection is dead (about 120 s), plus at most 10 s. The check is one `poll` call with negligible overhead; this feature depends on operating system support (Linux, macOS, BSD, etc.), which the VM's Ubuntu satisfies |

Client: `review_local.py`'s `merge_all` and `agent_lab.py merge` run `SET LOCAL lock_timeout = '10s'` inside the merge transaction. The legitimate lock wait time is the queue depth times the lock-holding time per merge: with the ten writers over TLS to the VM in section 1, each merge holds the lock for about 0.15 s (about 70% of the 2.11 s merge phase, over 10 merges), and in the worst case a writer queues behind 9 others, about 1.4 s; 10 s is about 7×. It is shorter than the server's 60 s, so a merge queued behind a vanished client fails within 10 s with `55P03` instead of waiting indefinitely.

Lock timeouts are reported separately and not treated as semantic conflicts: main is unchanged, and the same proposal can be tried again. `merge_all` counts it in `lock_timeout_retries` (separate from `stale_head_retries`, sharing the limit of 30) and then re-reads head and retries; `agent_lab.py` returns `{"error": "lock_timeout", "sqlstate": "55P03"}` (exit code 2), alongside `stale_head` (40001) and `merge_conflict` (P0001). `review_local.py` adds a real PostgreSQL check: while another session holds the main lock, merging with a 1 s lock timeout gets `lock_timeout` after about 1.1 s, with head unchanged; after the lock is released the same proposal merges normally. Limitation: when history is very long (section 2, one merge taking 15 s at 1,000 commits), legitimate queuing can also exceed 10 s, in which case lock-timeout retries will occur; until the cost problem of section 2 is solved, the limit of 30 may need to be relaxed. `benchmark_merge.py` is unchanged, to stay comparable with the existing timings.

### Upper bounds once the settings take effect

| State the client was in when it vanished | Longest time the main lock is held |
|---|---|
| Idle inside a transaction (network-cut drill, laptop sleep) | 60 s (`idle_in_transaction_session_timeout`) |
| Statement executing, client connection closed | About 10 s (`client_connection_check_interval`) |
| Statement executing, peer completely silent | About 130 s (keepalive 120 s + check 10 s), or idle another 60 s after the statement ends, whichever comes first |
| Not in a transaction | No lock held; the connection is cleaned up after about 120 s |

### Pass criteria for the network-cut drill

After applying the settings, run on Windows (20 s network cut, the same as section 3; the default of `--seconds` is 10):

```console
.\.venv-cloud\Scripts\python.exe staging_drills.py netcut --config connection.staging.local.json --seconds 20 --expect-gone-within 75
```

Pass criteria: `passed` is true, i.e. in addition to the original conditions (the main lock can be acquired after cleanup, head unchanged, no leftover transactions), the server ends the lock-holding session on its own within 75 s after that session's last statement (`main_locked_for_s` ≤ 75, measured by the server clock), without the owner needing `pg_terminate_backend` (`terminated_by_owner` is null). 75 s = the 60 s timeout + up to about 1.5 s of 1 s polling granularity + scheduling margin from sharing one laptop. Polling starts when the network is restored; if the session no longer exists when the server is first reached, `main_locked_for_s` is only an upper bound, and if it already exceeds 75 s, `bound_observable` is false and `bound_met` is null (cannot be judged, does not count as passing).

Expected results:

| Field | Expected |
|---|---|
| `server_settings` | `idle_in_transaction_session_timeout` 60000ms, `tcp_keepalives_idle` 60s, `tcp_keepalives_interval` 10s, `tcp_keepalives_count` 6, `tcp_user_timeout` 120000ms, `client_connection_check_interval` 10000ms, `lock_timeout` 0ms (the server default is unchanged; only the merge transaction sets it itself) |
| `probe_while_lock_held` | `lock_timeout` |
| `first_reachable_poll_after_s` | Time until the server is first reached after the network is restored (about 37 s in the 2026-10-18 retest) |
| `connect_during_cut` | `timeout expired` after about 5 s |
| `main_locked_for_s` | About 60–62 s; `stale_session_gone_after_s` about 35 s (counted from network restoration) |
| `terminated_by_owner` | null; `probe_after_cleanup` is `acquired`; `bound_observable` and `bound_met` are true |

With a 20 s network cut, `idle_in_transaction_session_timeout` expires at 60 s, and at the same time the first keepalive probe also gets an RST because the network has already been restored; the two happen almost simultaneously, and the result file does not distinguish which one ended the session (the VM's PostgreSQL log records "terminating connection due to idle-in-transaction timeout"). To verify separately that the lock is also released during a network cut, you can additionally run `--seconds 90 --expect-gone-within 110`: then keepalive gets no answer, and only the idle timeout can end the session at 60 s; `gone_at_first_poll` is true, and `main_locked_for_s` is only an upper bound (the 90 s cut plus about 4 s before and after taking the lock and the connection check after the network is restored, about 95–100 s). `--expect-gone-within` must be greater than `--seconds`, otherwise the script refuses to run, because nothing can be observed during the cut. Without this argument, the drill only records results as before, and `passed` does not check the bound.

New results are written to `staging-results/netcut.json`; the results under the default settings have been renamed to [netcut-defaults.json](../staging-results/netcut-defaults.json) and will not be overwritten. The drill logic has been checked locally with a simulated server and clock (passes when released at 60 s; fails with the default settings plus `--expect-gone-within` because manual termination is needed; passes as before without the argument), **but has not yet been run on the VM**.

## 7. Retest on the VM after the fixes (2026-10-18)

The two problems of sections 2 and 4 are handled respectively by `sql/005_bounded_history.sql` ([History growth fix](HISTORY_GROWTH.en.md)) and the vanished-client settings in `configure_postgres.sh`. After both were merged into main, they were rerun on the same VM:

### History growth

A new test database installed 001–005, so it includes the fix. The same command `staging_drills.py growth --levels 0 100 1000 2000`, with the second group adding `--snapshot-interval 100` (after every 10 commits, materialize due snapshots according to `vc.snapshot_due`). Each group completed in about 1 minute (11 minutes before the fix).

| At 2,000 commits on main | Before the fix | Fix only | Fix + materialize snapshots every 100 commits |
|---|---:|---:|---:|
| One 10-row merge | 50.7 s | 639 ms | 181 ms |
| One commit | 499 ms | 18 ms | 20 ms |
| head full-table read | 1,205 ms | 180 ms | 24 ms |
| Database size | 26 MB | 29 MB | 53 MB |

- With the fix only, commit cost no longer grows with history (about 20 ms at each level), and the merge at 2,000 commits drops to about 1/80 of the pre-fix value; merges and reads still grow slowly with "the number of commits since the last snapshot".
- With the snapshot policy added, merges (124–181 ms), commits and head reads (15–24 ms) stay essentially flat from 0 to 2,000 commits; each materialization takes about 165 ms, about once every 100 commits.
- The cost is storage: each snapshot saves a whole 4,000-row table, and at 2,000 commits the database is about 1.8× the size without materialization. Snapshot retention and reclamation have not been designed yet (README current limitation 9, "version retention/reclamation").
- Raw results: [before the fix](../staging-results/growth-before.json), [fix only](../staging-results/growth-after.json), [fix + snapshot policy](../staging-results/growth-after-policy100.json).

#### Ancestor traversal in merges and generation numbers

After the snapshot policy, what still grows with history in a merge is the two traversals by `vc._ancestors` and `vc._merge_base` of all changesets reachable from the target head. On the same VM, with 2,026 changesets reachable from main, one full traversal takes about 6.8 ms and merge_base about 16 ms, about 10% of a 181 ms merge, with about 8 ms more per additional 1,000 reachable changesets (this measurement was done by the coordinating session, and the raw record was not committed).

Conclusion: do not switch to a generation-number index now. Change it at about 20,000 reachable changesets (extrapolating linearly from the growth above, about 0.17 s for the two traversals together) or when the median merge time exceeds about 0.5 s. The manager Agent checks through `SnapshotMaintenance.check_history` (the read-only `vc.history_health`, `sql/006_history_health.sql`); when a threshold is crossed, the task's next_action is "merge history needs generation numbers (see docs/STAGING_S4_S5.md)", see [Manager Agent protocol](MANAGER_AGENT_PROTOCOL.en.md#history-growth-check). The change will be a separate version engine change, to be migrated and verified in the same way as [History growth](HISTORY_GROWTH.en.md).

### Network cut

After the user re-ran `configure_postgres.sh` on the VM according to [STAGING_VM section 4a](STAGING_VM.en.md) (keeping the installed certificates), `staging_drills.py netcut --seconds 20 --expect-gone-within 75` was run. [Raw results](../staging-results/netcut.json)

| | Default settings ([netcut-defaults.json](../staging-results/netcut-defaults.json)) | New settings |
|---|---|---|
| Server settings recorded by the drill | keepalive 7200 s, all timeouts 0 | `idle_in_transaction_session_timeout` 60 s, keepalive 6 probes 10 s apart after 60 s, `tcp_user_timeout` 120 s, `client_connection_check_interval` 10 s |
| Vanished session holding the main lock | Still there after 120 s of observation, about 2 hours 11 minutes under the default settings | **Ended by the server on its own after 60.7 s** (counted from the lock-holding session's last statement, including up to 1 s of polling error) |
| Owner needs manual `pg_terminate_backend` | Yes | **No** |
| Result | Passed (exposed a risk) | **Passed**: released within the 75 s bound; head unchanged, no leftover transactions, and after the lock was released other sessions immediately acquired the main lock |

During the network cut, new connections still reported `timeout expired` clearly after 5.1 s.

After code review, `idle_in_transaction_session_timeout` was moved from the cluster-level `conf.d` to being set only on the `agentdb_staging` role ([role script](../deploy/staging/create_staging_role.sql)), and administrator sessions returned to the default value 0. After the user re-ran `configure_postgres.sh` on the VM, it was checked: the `postgres` session shows `0`, and the `agentdb_staging` session shows `1min`. The same drill passed on retest: the server ended the vanished session on its own after 60.5 s and released the main lock (measured by the server clock; the server was first reached 36.8 s after the network was restored, at which point the session no longer existed, so 60.5 s is an upper bound, still within 75 s), with no manual termination needed, head unchanged, and no leftover transactions. [Raw results](../staging-results/netcut-role-timeout.json) The other S5 drills (backup restore, session termination, restart, power-off) are not affected by these two changes and were not rerun this time.

## 5. Environment problems and how they were handled

- **VirtualBox runs on top of the Hyper-V platform** (WSL2 is enabled): a live snapshot with memory got stuck in the `livesnapshotting` state for more than 30 minutes, the memory file stayed at 0 bytes, and the VM was frozen in the meantime; after the VM process was ended, VirtualBox rolled back that snapshot. After that, snapshots were taken in the powered-off state (`s1-configured`, completed instantly). Once, booting stopped at initramfs "Loading essential drivers", and it was fine after a reset; `staging_drills.py` automatically resets once if not ready within 200 s, which was not triggered again during these drills.
- **Laptop sleep** pauses the VM and the client, invalidating long-running tests. After that, the machine was kept awake through an application (this only prevents idle sleep; closing the lid still sleeps).
- When `db.Session` times out, the error message says "timed out after 0 seconds"; the number of seconds is inaccurate, which affects only the error message.

## 6. What is kept and cleanup

The VM keeps the test databases of each drill (names in each result file) and two snapshots: `clean-postgres` (before configuration) and `s1-configured` (after S1 was completed and the VM shut down normally; the reset point for S4/S5). `E:\agent-db\Snapshots` contains a 2 MB orphaned differencing disk `{81d5f3fc-…}.vdi`, from the rolled-back live snapshot. Whether to clean up is up to the user.
