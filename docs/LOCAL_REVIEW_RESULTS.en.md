English | [中文](LOCAL_REVIEW_RESULTS.md)

# Record of Local Maintenance Recovery, Permissions and Agent Experiments

Date: 2026-10-17. Uses the existing Ubuntu WSL PostgreSQL 16.15, without starting/stopping or replacing the service. Automated experiments use dedicated test databases; the existing `agentdb` only had the migration applied that adds new functions and grants, and its business data and version history are unchanged.

**All cloud storage is still not verified against real cloud services, and ECS is not deployed.** Maintenance task state uses MemoryStore; the OSS configuration check uses SDK test doubles. The local reports here are not the recovery storage for formal management tasks, and do not change the design in which formal state uses cloud object storage. Claude was not run.

## 1. A stale maintainer must get a conflict after waking up

Event barriers fix the order: A publishes the intent and completes the database snapshot, then stalls before publishing the completed state; B takes over, checks that the snapshot already exists and publishes the completed state; A wakes up and continues publishing with the old version.

The original implementation, after catching a publishing Conflict, returned success if it found the task already completed. The new test failed on the original implementation, proving that it can detect this problem. After the fix, A receives the Conflict, the final head stays at the version published by B, and the actor of the completion event is B.

The fix is in `agent_state/maintenance.py`, and only removes the handling that swallowed the conflict when publishing completion. Engine still returns the original result for network retries of the same command with the same request. This experiment uses different actors; it does not prove isolation between execution instances under the same identity, nor does it prevent a stale executor from causing side effects in other systems. The head's conditional publishing is not a cross-system transaction or general fencing.

## 2. Both interruption windows can be taken over

| Injection point | Real PostgreSQL state before B takes over | B's result |
|---|---|---|
| Intent recorded, database action not yet performed | Snapshot does not exist | Performs materialization, verification succeeds, `applied=true` |
| Database already committed, completed state not yet published | Snapshot already exists | Skips materialization, verification succeeds, `applied=false` |

A new `after_intent` injection point was added, keeping the position of the original `after_apply` parameter for compatibility with callers. Interruptions are injected by exceptions, and the taker-over reads the checkpoint from the same in-memory store; this verifies the control flow and the PostgreSQL state probe, and is not a power-loss or cloud durability test. Both cases above and the stale-maintainer scenario passed again on the real local PostgreSQL under the restricted maintenance role.

## 3. Checking prefix from the configuration entry point

The new test uses the `review/nondefault` prefix, going through JSON configuration reading, the storage factory, the maintenance CLI and the service startup wiring. A task written by the maintenance CLI can be read by the service, and the task cannot be found under the default prefix.

This check already passed on the source before the current fix; **this time no prefix mismatch was reproduced, and no claim is made to have fixed such a defect**; the new coverage is meant to prevent future regressions in which prefix is passed only when constructing an Engine manually. The OSS client uses FakeOSS, and the service listener was replaced with a test object; no real bucket was accessed, and this item is not treated as complete HTTP integration testing.

## 4. Restricted management role and merging all ten branches

### Role and migration

Added `sql/003_manager_role.sql` and `db.py install-manager`. `agentdb_manager` is a role with no login and no superuser privileges, whose only business capabilities are checking snapshot status and calling the existing materialize function. `vc.snapshot_status` returns the version, the logical table and whether the snapshot exists, so the maintainer does not need to read the history tables directly. The function has a fixed search_path, and PUBLIC execute permission is revoked.

The real PostgreSQL verified the allowed maintenance operations, and rejected seven kinds of operations with SQLSTATE `42501`: reading history directly, rewriting history, deleting snapshot records directly, modifying business tables, creating versions, creating functions, creating roles.

The role test uses `SET ROLE` on a trusted administrator connection. It verifies database authorization, **and does not mean an Agent holding administrator credentials has been isolated**; a real deployment needs a separate restricted login identity. No login password was created this time. The administrator who can change system rules is kept, and the routine maintenance role does not replace it.

The migration was run twice each on the old-structure test database `agentdb_test_7a1b14ad0135` and the existing project database `agentdb`, confirming that it can be applied repeatedly. Products, orders and branch summaries are the same before and after, and the counts of changesets, row history, file history and snapshots are the same. The raw record is in [migration results](../manager-migration-results.json).

### Merge-everything experiment

Dedicated database: `agentdb_test_review_2a02f90171`. 4,000 product rows; ten scripted writers branch from the same baseline, each modifying ten non-overlapping rows and one code file, and then merge concurrently into the same main. They are real database connections, **not ten large-language-model Agents**.

| Metric | This run's result |
|---|---:|
| Branches finally merged | 10 / 10 |
| Product rows modified / kept at original value | 100 / 3,900 |
| Code files finally kept | 10 |
| Row history copied when creating branches | 0 |
| Branch preparation and commit time | 4,967.083 ms |
| Whole merge phase time | 9,467.149 ms |
| End-to-end time | 14,931.300 ms |
| Median / maximum single merge task time | 6,967.983 / 9,461.335 ms |
| Re-read retries caused by stale head | 45 |

All ten source commits are reachable from the final main along parent edges, the pinned baseline still keeps its original values, and there are no leftover work transactions. Only `40001` stale-version errors trigger bounded re-read retries; semantic conflicts are not retried blindly.

Timing includes the client, WSL/psql startup, waiting, retries and the final COMMIT, but not test database initialization, other checks or the final integrity verification. All writers deliberately compete for the same head at first; this is a local contention experiment, from which production throughput or model collaboration ability cannot be inferred. The 45 retries also suggest that the benefit of backoff/merge scheduling should be measured later. The raw results are in [local review results](../local-review-results.2026-10-17-spawn.json) (`local-review-results.json` is overwritten by later runs). For the timing breakdown and follow-up recommendations see [Merge timing breakdown](MERGE_BENCHMARK.en.md). After this, the writers of `review_local.py` reuse long-lived connections by default, and the re-measurement results are also recorded in that document.

## 5. Real GPT experiment

Uses `agent_lab.py` and another dedicated database `agentdb_test_gpt_70846a148f`. Two GPT/Codex sub-Agents work on product 101 from the same baseline: one raises the price by 10%, the other reduces stock by 5, each also committing the corresponding Python rule. Code and row changes are in the same changeset; the rule code is only saved and syntax-checked, not executed.

The first attempt was interrupted by the account's quota, leaving one stock proposal and one price branch with nothing written, with main unchanged. After the user continued, the two real GPT sub-Agents resumed from the existing state and completed the experiment; this resumption did not hit the quota limit again. They inherited the current session's model and did not explicitly select another specific version.

| Actual step | Result |
|---|---|
| Price Agent reads the baseline, commits the price change and code | Price 10 → 11, stock still 100, merge succeeded |
| Stock Agent merges the original proposal with the old expected | `40001 / stale_head`, rejected |
| After re-reading the current main, check again whether the old stock proposal can be merged | `P0001 / merge_conflict`, whole-row conflict on product 101 |
| Stock Agent creates a recovery branch from the latest main | Re-expresses stock 100 → 95, keeping price 11 |
| Check the diff, read both pieces of code and merge | Both pieces of code kept, final price 11, stock 95 |

The whole-row state of the original stock proposal contains price 10, so updating only expected cannot resolve the semantic conflict. GPT re-created the proposal based on the error, and did not overwrite the other Agent's price change. The final main is `497b67f6-cbd5-4c01-981e-ab6241f16e4f`; independent checks confirmed that the total number of rows is still 4,000, the other 3,999 rows keep their original values, the pinned baseline is unchanged, both accepted proposals are reachable from the final main, and there are no leftover work transactions. The original stock proposal is still kept by the old branch; no history was deleted.

The experiment found and fixed a gap in the client interface: the stock Agent could only see file hashes through `info/diff` and could not check the code text before merging. So `read-code --revision UUID --path ...` was added, allowing the two experiment rule files to be read at a pinned version; both the existing stock proposal and the recovery proposal were checked through this entry point. The final verification checks `PRICE = 11.0`, `STOCK = 95` and the real row data, without executing the policy code.

This is a two-Agent experiment with a clear goal, limited tools and phases arranged by a coordinator; it is not a blind test of free exploration, and does not represent the ability of ten models running concurrently or long-term autonomous operation. Database operations were initiated by GPT, and the database results were read and checked independently by the coordinator; some operations before the interruption were confirmed through the database state. The structured record contains an organized version of the actual Agent reports and the independent database verification, and does not pass itself off as the complete raw tool trace. See [GPT experiment record](../gpt-agent-experiment.json).

What this run's behavior shows: the client should provide interfaces for pinned versions, structured conflicts and on-demand code reading together. A bare "failed, please retry" is not enough to distinguish a stale pointer from a conflict that requires re-expressing the intent; the database side must still reject wrong merges, and cannot rely on Agents voluntarily staying correct.

## Regression scope and reproduction

Before this change, the existing 50 local state/adapter/HTTP tests passed. The new stale-maintainer test failed before the fix, the interrupted-after-intent test errored before the new injection point was added, and the configuration prefix test passed from the start. After the fix **all 53 passed**, with the original assertions kept; the original **18 version engine assertions passed** in a dedicated PostgreSQL test database.

From the project directory, with the existing local connection configuration, run:

```console
.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests -v
.\.venv-cloud\Scripts\python.exe review_local.py
```

`review_local.py` creates a new dedicated test database in the same existing service each time, and keeps the results for checking. Existing projects upgrade the maintenance API with `python db.py install-manager`; new installations already include this migration. For the GPT experiment entry point see `python agent_lab.py --help`; only `agentdb_test_gpt_<10位十六进制>` databases (`<10位十六进制>` = 10 hex digits) can be work targets; neither the tool wrapper nor SET ROLE is an operating system sandbox.

This change does not alter the persistent object encoding. Rolling back the Python maintainer does not require modifying history; the new role and status function can be kept. Do not understand rollback as deleting experimental or formal history.
