English | [中文](HISTORY_GROWTH.md)

# History Growth: Bounded History Traversal and a Snapshot Policy

2026-10-18. Deals with the costs growing with history length measured in section 2 of [S4/S5](STAGING_S4_S5.en.md). On the staging VM, with 2,000 commits on main, one 10-row merge took 50.7 s, a commit 499 ms, and a head read 1.2 s. The change is on the branch `perf/history-bounded-costs`, and the migration is [`sql/005_bounded_history.sql`](../sql/005_bounded_history.sql). All numbers come from the existing local Ubuntu WSL PostgreSQL 16.15, with client and server on the same laptop, and are only for before/after comparison. **Not yet rerun on the staging VM; no cloud services were used.**

## 1. Causes (measured on a 1,000-commit database)

The database is `agentdb_test_growth_67afbbec40`, kept from the baseline growth test, with 1,020 changesets.

- All recursive traversals were written as `lineage l JOIN vc.parent p ON p.child=l.id`. The planner chose a hash join for the recursive step, so every step did a full-table scan of `vc.parent` (`EXPLAIN ANALYZE`: Seq Scan on parent, loops=1011). So a single traversal was proportional to the square of the history length. Walking the first-parent chain over 1,000 commits once took 106 ms; the same traversal with an index lookup per step takes only 2.4 ms. `_ancestors`, `rows_at` and `_file_state` were all written this way.
- `vc._file_state` first walked to the root and then took the most recent record by depth, without stopping early even after finding the path. `write_file` called it once per call; `vc.merge` called it three times for **every** path that had ever appeared in history.
- `vc.merge` traversed all ancestors of the target twice (once to decide whether it had already been merged, once inside `merge_base`), and all ancestors of the source once.
- `rows_at` replays all commits since the most recent snapshot, and there was no mechanism to materialize snapshots automatically.

## 2. Changes

All are `CREATE OR REPLACE`: signatures, return values, error texts and SQLSTATEs, owners, SECURITY DEFINER, search_path and grants are unchanged. New installations run 005 after 001–004; on old installations the database owner runs `python db.py install-bounded-history`, which can be run repeatedly. The functions with the same names in `001_core.sql` are the old definitions, and 005 takes precedence.

1. **Parent edges walked step by step through the index** (`_ancestors`, `rows_at`, `_file_state`, `snapshot_due`): the recursive step is changed to `CROSS JOIN LATERAL (SELECT p.parent FROM vc.parent p WHERE p.child=… OFFSET 0)`. `OFFSET 0` prevents the subquery from being flattened into an ordinary join, so each step is a parameterized index probe, independent of statistics. The traversal's stop condition and output set are unchanged.
2. **`vc._file_state` stops when found**: during recursion each changeset is marked with "whether it recorded this path", and the traversal stops at the first first-parent ancestor that recorded the path, taking the state there. A path never recorded by any changeset is not traversed and NULL is returned directly. The return values are the same as before: a deletion is still NULL, and the executable flag is still taken from that record. The cost of a commit therefore depends on how long ago that path was last changed, not on the whole length of history.
3. **`vc.merge` does less repeated work** (the conflict rules and the order of decisions are unchanged: rows are handled table by table first, then files path by path; each path is still compared in order: common ancestor vs source, target vs source, target vs common ancestor):
   - The target's ancestors are traversed only once, used both for the "source already merged" decision and the common-ancestor computation. A new internal function `vc._merge_base(祖先数组, b)` (`祖先数组` = ancestor array) is added, and `vc.merge_base` delegates to it with unchanged semantics: the unique maximal common ancestor, otherwise it raises `A unique merge base is required`.
   - File states are evaluated along the first-parent chain. During a merge, the first-parent chains of the common ancestor and the source are walked down in step, until one of them reaches a changeset the other has already passed. From that point down the two chains are identical; for paths recorded by neither chain above that point, the states on both sides are necessarily the same, and the first comparison of the original loop would skip them too. So only paths recorded above that point need to be checked. This relies only on the definition of the first-parent chain, not on how the history was produced. The cost depends on the length of the divergence, not on the length of history.
   - Paths are checked in path-name order. The order of the original loop came from an unordered `SELECT DISTINCT` (hash aggregate) and was undefined. Only when multiple file conflicts exist at the same time does it affect which path the error names: the error code and rollback are the same, and the path reported is still one of the conflicting paths.
4. **Snapshot policy** `vc.snapshot_due(branch DEFAULT 'main', interval DEFAULT 100)`: read-only, looking back at most interval commits. If reading a registered table at the branch head requires replaying at least interval commits, it returns (head, table_id). Granted only to `agentdb_manager`; `agentdb_client` and PUBLIC cannot execute it. The executor `SnapshotMaintenance.run_due(actor, branch, interval, policy_ref)` calls the existing recoverable `run()` for each target. The task ID is `snapshot-<revision>-<table_id>`, and repeated execution only verifies, without materializing again. Snapshots are still derived data:
   - Input version: the branch head at the time the policy runs;
   - Completion condition: `vc.snapshot_status(...).snapshot_exists`, i.e. the row exists in `snapshot_manifest`;
   - Rebuild method: regenerated from history via `vc.materialize` (i.e. `rows_at` replay);
   - Read results do not depend on whether the snapshot exists (verified in section 4).

   Computing with one run every 10 commits, a head read replays at most about interval + 10 commits. The cost is materializing 4,000 rows each time (about 50–65 ms locally, done outside the commit), and snapshot rows add storage: at 1,000 commits with interval 100, the database grows from 21.7 MB to 33.4 MB. Snapshot tables, like history tables, cannot be modified, and there is no reclamation mechanism yet.
5. File-list snapshots (saving the complete file tree at each snapshot point) are not implemented. With stop-when-found, a single `_file_state` takes about 2 µs per step along the first-parent chain: for example, for `src/app.py`, written only in the seed and unchanged for 1,000 commits, one read takes 1.9 ms. If many long-unchanged files and very long histories appear in the future, this approach can be considered then.

## 3. Before/after comparison

### Growth test (`staging_drills.py growth --config connection.local.json --levels 0 100 1000`)

Each growth commit changes 10 product rows and writes one of 50 files; merges are the median of 3, commits the median of the last 10 commits, and reads the median of 5 `EXPLAIN ANALYZE` runs. The raw results are in [local-results/history-growth/](../local-results/history-growth/). When the test was copied locally, staging's `growth.json` was not overwritten. The note about "TLS connection to the VM" in the result files is fixed text from the script; this time the actual connection was to the local machine.

| Commits | Merge before → after (no policy) → after (policy 100) | Commit before → after → after (policy) | head full-table read before → after → after (policy) |
|---|---|---|---|
| 0 | 36.7 → 45.2 → 46.5 ms | — | 7.0 → 6.4 → 6.2 ms |
| 100 | 137.7 → 62.8 → 33.5 ms | 4.8 → 5.1 → 5.1 ms | 11.9 → 11.9 → 5.1 ms |
| 1,000 | **6,521 → 160 → 42.6 ms** | **58.4 → 5.5 → 6.3 ms** | **113.6 → 44.4 → 6.3 ms** |

- Commits no longer grow with history (changes 1 and 2).
- Without running the policy, merges and head reads still grow with history, but now linearly and cheaply: `rows_at` replays all commits since the seed snapshot, and a merge calls `rows_at` 3 times. With the policy running, both stay flat between 0 and 1,000 commits (the differences among merges of 33–47 ms are single-machine timing fluctuations). Between 100 and 1,000 commits the policy ran 9 times, each time materializing two tables (products and orders), median 65 ms.
- What is still proportional to the length of history: the traversal of all ancestors of the target and the source during a merge. It now takes about 1–2 µs per commit, and at 1,000 commits the whole `merge_base` takes about 5 ms (104 ms before). Eliminating it completely would need a derived index such as generation numbers; there is currently no load-based justification, so it is not implemented for now. `vc.history` still uses the old join form, but it traverses at most limit steps and was not changed this time.

### Function level (1,000-commit baseline database, replaced inside a transaction and then rolled back)

| Measurement | Old | New |
|---|---|---|
| `_file_state` for 25 random versions × 52 paths (1,300 calls) | 27,379 ms | 90 ms |
| `_ancestors` for 25 versions | 643 ms | 24 ms |
| `rows_at` for 25 versions × 2 tables | 1,365 ms | 657 ms |
| `merge_base(head, head 的父)` | 104 ms | 5.1 ms |
| `vc.snapshot_due('main',100)` | — | 7.5 ms |

(`head 的父` = the parent of head.)

The results of the three groups of reads are identical item by item (1,300 file states, 25 ancestor sets, 50 table state hashes).

### Existing suites (run on newly created local test databases both before and after the migration)

| Suite | Before | After |
|---|---|---|
| `python run_tests.py`: the 18 original assertions + concurrency, sessions, merge semantics | All passed (`agentdb_test_06f5945e3a67`) | All passed (`agentdb_test_ff98e378fcd4`) |
| `review_local.py`: restricted role, two interruption windows, all ten branches merged | Passed; merge phase 575 ms, 45 retries | Passed; merge phase 531 ms, 45 retries |
| `check_merge_migration.py` (004's migration verification) | Passed, 13 cases identical | Passed, 13 cases identical |
| `unittest discover -s tests` | — | 75 items passed (including the new snapshot policy tests) |

## 4. Migration verification: `check_history_migration.py`

[`check_history_migration.py`](../check_history_migration.py); results in [`history-migration-results.json`](../history-migration-results.json) (databases `agentdb_test_histmig_2b0e3f493e`, `agentdb_test_histreg_2b0e3f493e`, `agentdb_test_histnew_2b0e3f493e`).

- Two old-version databases are built with 001–004 from commit 0bf7309, installed the same way as `Connection.install` did at the time.
  - The `histreg` database runs the 18 original assertions and the 13 row-merge cases. These assertions assume the database has only the seed (for example only 1 blob), so it is built separately.
  - The `histmig` database commits a realistically shaped history: 24 commits on main, changing, deleting and restoring files and toggling the executable flag, while also changing rows; a table snapshot is built midway; branches have files and rows; main merges a branch and then it is merged back into main; there are branches of branches; and there is also a criss-cross (two merge bases).
- Before the migration, 1,991 read results are recorded: `_file_state` and `read_file` for every version × every path; `diff_files` for every parent edge and between each pair of branch heads; `diff_rows` for every parent edge; `_ancestors` and `rows_at` for every version; `merge_base` between each head and every version, including errors. In addition, 17 file merge cases are run in rolled-back transactions: one-sided change, two-sided changes that differ or are the same, delete on one side and change on the other, deletes on both sides, executable flag change only, multiple file conflicts, file conflict after rows were applied rolling back as a whole, merging a branch, a long-lived branch merged into main first and then back, already merged, criss-cross, nonexistent source, source being a descendant of the target.
- After running `install_bounded_history` twice:
  - The hashes of business data and history tables are unchanged; the owner, SECURITY DEFINER, configuration, ACL and execute permissions of the replaced functions are unchanged;
  - The migrated functions are identical item by item to a new installation (`histnew`), including function body hashes;
  - The 1,991 reads and the 30 merge results (17 file, 13 row) are the same as with the old functions;
  - The 18 original assertions pass again; `agentdb_client` can still merge.
  - The error for multiple file conflicts is `File conflict: hist/f0.txt` both before and after. The comparison still normalizes it as "one of the conflicting paths", because the old order was undefined.
- The snapshot policy is run as `agentdb_manager` through `SnapshotMaintenance.run_due` (MemoryStore, for testing only) with interval 5: each of the two tables is materialized once; on the second run there are no targets. Afterwards all reads and file merge cases are compared again, and the results are still the same.

## 5. Remaining limitations and next steps

- The S4 growth test on the staging VM has not yet been rerun with the new functions: the migration first needs to be run on the VM's test database, then `staging_drills.py growth --snapshot-interval 100`.
- The policy needs an executor to run it periodically; currently `maintain_snapshot.py` handles only a single target, `run_due` has no command-line entry point yet, and there is no resident scheduler. Cloud state is still not verified against real cloud services.
- Ancestor traversal is still linear in the length of history (see section 3); snapshots have no reclamation mechanism.
