# Forkpoint

**Git-style branches for AI agents, inside PostgreSQL.** Each agent works on its own branch, commits code files and table rows together, and merges into `main` only if nobody else's work gets overwritten and your real constraints still hold.

[中文说明](README.zh-CN.md) · [Technical blog](docs/TECHNICAL_BLOG.md) · [Design notes](docs/DESIGN_REVIEW.md)

![License: MIT](https://img.shields.io/badge/license-MIT-blue) ![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)

Formerly AgentDB, renamed on 2026-10-20. Database and role names such as `agentdb_demo` and `agentdb_manager` keep the old prefix.

## Why

When several agents edit the same database, they run into the problems Git solved for developers. One agent overwrites another's change. Another reads data that moved under it halfway through its task. A third changes a pricing rule in code but not the prices that depend on it.

Forkpoint adds Git's answers to an ordinary PostgreSQL database:

- **Branches.** An agent forks a branch from a pinned version. Later changes on `main` don't leak into it.
- **Commits that hold code and data.** A changed file and the changed rows are published in one transaction. If either fails, neither lands.
- **Merges that refuse to guess.** A merge based on an old `main` is rejected. So is a merge that conflicts with someone else's change. Each failure has its own error code, so the agent knows whether to retry or redo its work.

It is plain SQL and PL/pgSQL, with no extension and no separate server. The Python files are a thin CLI and test harness.

## See it work

Two agents start from the same version of product 101 (price 10, stock 100). One raises the price, the other cuts the stock. `docker compose up` runs [`examples/two_agents.sql`](examples/two_agents.sql) and prints:

```text
1. main: product 101 costs 10 and has 100 in stock
2. price-agent and stock-agent each fork a branch from that version
3. price-agent: price 11 and src/pricing.py, in one commit
4. stock-agent: stock 95, on a branch that never sees the price change
5. price-agent merges into main
6. stock-agent merges, still expecting main at the old version (should fail: stale head)
psql:examples/two_agents.sql:44: ERROR:  Stale head: expected 2265b903-…, actual cd557e23-…
   SQLSTATE 40001 -> re-read main and retry
7. stock-agent retries against the latest main (should fail: its row 101 still has price 10)
psql:examples/two_agents.sql:49: ERROR:  Row conflict: table 3c320fad-…, key {"id": 101}
   SQLSTATE P0001 -> re-propose from the latest main
8. stock-agent re-proposes from the latest main and merges
9. main now has both changes, and the price change brought its code
 id  | price | stock
-----+-------+-------
 101 | 11.00 |    95

 pricing_code
--------------
 PRICE = 11

OK: both conflicts were caught, and no agent overwrote the other.
```

Step 3 is one ordinary transaction. The row change and the code file land in the same commit:

```sql
BEGIN;
SELECT vc.begin_change('price-agent', :'base', 'price-agent', 'raise price to 11');
UPDATE agent.products SET price = 11 WHERE id = 101;
SELECT vc.write_file('src/pricing.py', convert_to('PRICE = 11', 'UTF8'));
SELECT vc.finish_change() AS price_rev \gset
COMMIT;
```

Each merge is one call, `SELECT vc.merge('main', :'stock_rev', :'base', 'stock-agent');`, and it either lands completely or fails with one of the codes above.

That is the output of a real run on PostgreSQL 16.15, with the UUIDs shortened. The same scenario was run with two real GPT agents in a [pre-registered ten-round experiment](docs/GPT_REPEAT_EXPERIMENT.md). All 8 rounds that finished reached the goal, and no round overwrote the other agent's work. The other 2 rounds stopped when the model service hit its usage limit; they count as failures.

## Quick start

The fastest way to try it is Docker. One command starts PostgreSQL 16, installs Forkpoint and runs the two-agent example:

```bash
git clone https://github.com/liuzhentong666/Forkpoint.git
cd Forkpoint
docker compose up
```

The run ends with `OK: both conflicts were caught, and no agent overwrote the other.` PostgreSQL keeps running so you can look around with `docker compose exec db psql -U postgres -d agentdb_demo`. Press Ctrl+C to stop it, and run `docker compose down` to remove it.

To use your own PostgreSQL instead, you need a PostgreSQL server (tested on 16), the `psql` client, and Python 3.10+. There are no pip packages to install.

```bash
python db.py check                              # find psql, connect, print the server version
python db.py install --database agentdb_demo    # create the database and install the schemas
psql -h 127.0.0.1 -U postgres -d agentdb_demo -f examples/two_agents.sql
python run_tests.py                             # integration tests in a new agentdb_test_* database, kept for inspection
```

By default `db.py` connects to `127.0.0.1:5432` as `postgres` and finds `psql` on your `PATH`. To change that, copy `connection.example.json` to `connection.local.json`. [docs/SETUP.md](docs/SETUP.md) covers passwords, remote servers (TLS `verify-full` only), PostgreSQL inside WSL, and upgrading an existing install.

To version your own table, create it empty with a primary key and register it. Its branch view then appears in the `agent` schema:

```sql
SELECT vc.register_table('app.products');   -- agents write through agent.products
```

## Highlights

| | What happens | Evidence |
|---|---|---|
| **Code and data commit together** | Files, row changes and the changeset are published in one PostgreSQL transaction; any failure rolls back all of it | `code_and_native_data_publish_atomically` in [run_tests.py](run_tests.py) |
| **Branches are cheap** | Forking copies no rows and no files (0 new row-history entries measured) | [Validation record](docs/VALIDATION.md) |
| **Errors an agent can act on** | `40001` stale head → re-read and retry · `P0001` merge conflict → re-propose · `55P03` lock timeout → wait and retry · `RS001` read conflict → re-read those rows and decide again · `P0002` no such branch (or, in `merge_validated`, no such revision) → check the name or ID | 10 connections racing on one stale head: 1 succeeded, 9 rejected; write skew and a missing branch refused in [run_tests.py](run_tests.py) |
| **Real constraints guard `main`** | Merges run as ordinary SQL against the real tables, so UNIQUE, foreign keys and CHECK constraints apply | three constraint-violation cases in [run_tests.py](run_tests.py) |
| **Stays fast as history grows** | With 2,000 commits on `main`: a 10-row merge takes 181 ms and a commit about 20 ms (50.7 s and 499 ms before the history fix) | [S4/S5 report, section 7](docs/STAGING_S4_S5.md) |
| **Failure-drilled** | A power cut kept all 201 acknowledged commits and no partial one; backup and restore matched on 13 fingerprints; a vanished client's lock is freed in about 60 s | [S4/S5 report](docs/STAGING_S4_S5.md) |
| **Evidence you can rerun** | Every number links to a raw result file in [results/](results/) and the script that produced it | [scripts/](scripts/) |

The numbers come from synthetic load on one laptop and a local VM. They show the design works at the first-phase target (one table of about 4,000 rows, 10 concurrent agents); they are not a production capacity promise.

## How it works

```text
pick a pinned version
  → fork a branch                          (a new pointer; no table copy)
  → BEGIN; begin_change(branch, expected_head)
  → edit rows through agent.* views, edit files with write_file()
  → finish_change(); COMMIT
  → diff_rows() / diff_files()
  → merge(main, source, expected_head)     (conflict check + native constraints)
  → code, history and main's tables are published in one transaction
```

| Schema | What it holds |
|---|---|
| `app.*` | Your real tables. They are `main`. |
| `agent.*` | One view per registered table. It shows the chosen branch or version; writes are captured by `INSTEAD OF` triggers. |
| `vc.*` | Changesets, branch heads, file blobs, row history, and rebuildable snapshots. |

Guarantees enforced by the database:

- Writes to `app.*` outside a versioned change are rejected.
- A published changeset can't be updated, deleted or appended to.
- If you forget `finish_change()`, `COMMIT` fails and the table changes roll back.
- Repeated updates to a row in one change collapse to one before/after pair. An insert followed by a delete cancels out.

Main SQL functions: `vc.head`, `vc.fork`, `vc.pin`, `vc.checkout`, `vc.begin_change`, `vc.finish_change`, `vc.write_file`, `vc.read_file`, `vc.diff_rows`, `vc.diff_files`, `vc.history`, `vc.merge`, `vc.merge_validated`, `vc.register_table`. The full design and the trade-offs are in [docs/DESIGN_REVIEW.md](docs/DESIGN_REVIEW.md).

## How it compares

| | Forkpoint | [Dolt](https://github.com/dolthub/dolt) | [Neon](https://neon.tech) branching |
|---|---|---|---|
| Runs in | Your existing PostgreSQL | Its own database engine (MySQL-compatible; Doltgres for PostgreSQL) | Neon's hosted PostgreSQL |
| A branch is | A pointer plus the rows changed on it | A branch of the versioned tables | A copy-on-write copy of the whole database |
| Merge back into `main` | Row-level three-way merge, checked by your constraints | Cell-level three-way merge | Not row by row |
| Versions code files too | Yes, in the same commit as the rows | No | No |
| Maturity | Early: single node, first-phase scale | Mature, used in production | Managed service |

If you need a mature, general-purpose versioned database, use Dolt. Forkpoint is for when you want to stay on your own PostgreSQL, and when you want an agent's code change and data change to be one reviewable, mergeable unit. We haven't benchmarked Forkpoint against either.

## Limitations

- **Fixed schema.** No DDL branching or migration merging yet.
- **Whole-row conflicts.** Two agents changing different columns of the same row still conflict; there is no per-column merge.
- **Cross-row rules aren't inferred.** Native constraints are checked; for a rule like "orders can't exceed stock", merge with `vc.merge_validated` and the rows the decision read (rows it wrote are checked as well), and the merge is refused if they changed since the branch was forked (write skew).
- **Branches may be temporarily invalid.** UNIQUE and foreign keys are checked only when merging into `main`.
- **Reads replay history.** Branch views resolve from history plus snapshots, not the table's own indexes, so a maintenance job should create snapshots (`vc.snapshot_due` says when). Snapshots and history are not reclaimed yet.
- **Single node and single tenant.** There is no replication, quota or tenant isolation. Writes to one branch are serialized, and every merge into `main` takes `main`'s lock.
- **Limited table types.** Tables need a primary key and must be registered while empty. Generated and identity columns aren't supported.
- **The cloud state service is unverified.** The manager agent's state service has GCS, Alibaba Cloud OSS and S3 adapters, but none has run against a real cloud, and deployment is on hold. See [docs/STORAGE_BACKENDS.md](docs/STORAGE_BACKENDS.md).

## Project layout

```text
db.py                 CLI: check the connection, install, upgrade an existing install
run_tests.py          integration tests (creates its own agentdb_test_* database)
review_local.py       local review: maintenance recovery, restricted role, ten branches merged
agent_lab.py          the tool CLI the GPT and Claude agents use in the experiments
agent_lab_gateway.py  optional loopback gateway that keeps psql sessions warm for agent_lab.py
smoke_cloud.py        cloud state-service smoke test (not run yet)
compose.yaml          the one-command Docker demo
sql/                  the version engine, installed in order 001–010
examples/             runnable SQL examples
scripts/              benchmarks, migration checks, staging drills
results/              raw results behind the numbers in the docs
agent_state/          manager-agent state service and storage adapters
experiments/          pre-registered experiments: GPT and Claude repeat rounds, read-set validation, context archive, checkable task state
deploy/               staging VM setup and the on-hold ECS bundle
docs/                 setup, design, validation records, plans, technical blog
tests/                unit tests
```

## Documentation

- [Setup and upgrades](docs/SETUP.md): connection options, passwords, TLS, WSL, migrations, tests
- [Technical blog](docs/TECHNICAL_BLOG.md): architecture and what two real GPT agents taught us
- [Design notes](docs/DESIGN_REVIEW.md): the version model and revisions to the original plan
- [GPT and Claude repeat experiments](docs/GPT_REPEAT_EXPERIMENT.md): ten pre-registered rounds with each model, failures kept
- [Read-set experiment](docs/READSET_EXPERIMENT.md): read-set validation vs. the stale-head check in 75 pre-registered synthetic runs, replicated
- [Context archive experiment](docs/CONTEXT_ARCHIVE_EXPERIMENT.md): a word-for-word archive of the context vs. the model's own handoff note after a reset; by its adoption rule, no evidence for the archive
- [Checkable task state](docs/TASK_STATE_EXPERIMENT.md): checkpointed facts marked fresh or stale on resume; pilot only, not adopted
- [Read sets with real agents](docs/READSET_AGENTS_EXPERIMENT.md): Forkpoint's read-set merge vs. agents on plain PostgreSQL told to lock and re-check, two real agents per round; registered, pilot only (both rounds correct), sample not run
- [agent_lab gateway](docs/AGENT_LAB_GATEWAY.md): an optional loopback process that keeps psql sessions warm for the agents' tool
- [History growth](docs/HISTORY_GROWTH.md) and [merge timing](docs/MERGE_BENCHMARK.md)
- [Staging S1](docs/STAGING_S1_RESULTS.md) and [S4/S5 drills](docs/STAGING_S4_S5.md): TLS access, growth, backup, crash, network cut
- [Reference-data plan](docs/REFERENCE_DATA_PLAN.md): reviewed changes to small tables the application reads but doesn't write, inside an existing PostgreSQL; not implemented
- [Manager agent protocol](docs/MANAGER_AGENT_PROTOCOL.md) and [AGENTS.md](AGENTS.md): conventions for agents that work on this repo

Most documents in `docs/` are written in Chinese first, with the English version below it in the same file.

## How it was built

This project was developed with AI coding assistants, including Anthropic's Claude Code. Commits Claude contributed to carry a `Co-Authored-By: Claude` line, which is why Claude appears in the contributors list. The project's direction, review and decisions are the author's.

## License

[MIT](LICENSE)
