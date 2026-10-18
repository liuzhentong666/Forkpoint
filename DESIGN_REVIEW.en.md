English | [中文](DESIGN_REVIEW.md)

# PostgreSQL Plan Review and First-Phase Design

Date: 2026-10-15. Target scale: a single table of 4,000 rows, 10 concurrent Agents.

## Design assumption

The user's assumption is that infrastructure shaped around the working rhythm of human developers may not suit a working mode in which Agents run with heavy concurrency, experiment frequently and continuously generate changes. This needs to be tested with new database interfaces, data models and storage organization.

This assumption does not require first proving that Spanner has already failed. It should be turned into measurable goals: the branching cost per task, first-read latency at a pinned version, write amplification of small commits, merge throughput on hot spots, failure recovery, the cost of retained history, and so on.

Databases have long been called automatically by programs. What is special about Agents needs to show up as the load characteristics above, rather than simply treating "it used to be used by human developers" as a sufficient condition for a storage engine failing.

Choosing PostgreSQL is the first-phase implementation approach: reuse its transactions, recovery, SQL types and constraints, and concentrate on validating the Agent workflow. It does not automatically bring distributed scaling, nor does it rule out replacing the underlying storage in the future.

## What is kept from the original plan

- Immutable changesets, with branches as version pointers.
- Code blobs and business data history live in the same PostgreSQL database.
- main keeps ordinary relational tables, and changes are captured through triggers.
- Branches use a logical overlay layer, with SQL reads and writes provided through views and INSTEAD OF triggers.
- Historical states are replayed from changesets and can be materialized as derived snapshots.
- Three-way comparison, conflict detection, and native constraint checks when merging into main.

A single transaction can atomically publish the changes to code, row history and main's business tables. This guarantee is limited to the inside of the database; it does not automatically make atomic external side effects such as deploying external code files, sending messages or making payments.

## Details that must be revised

### 1. Immutable commits need to be sealed

Forbidding only UPDATE/DELETE is not enough: if clients can still INSERT into an existing changeset's `row_change` or `path_change`, that version can still change.

An independent work area is currently used: `work_tx`, `pending_row` and `pending_path` only hold the staged operations of the current SQL transaction. `finish_change()` writes the history in one go, and clients have no right to access the history write interface directly. If sealing is not completed before the transaction ends, a deferred constraint trigger rejects it.

The database administrator is still a trusted party. This is not a cryptographic ledger that is tamper-proof against the superuser.

### 2. before/after is a convenience, not the only possible basis for version control

Saving only after and reading before from the parent version can also support diff, merge and revert. Bidirectional row images make comparison and reverse compensation more convenient, at the cost of write amplification and history size.

The current scale is small, so bidirectional images are kept. In the future the costs of full rows, field deltas, object references and compression should be measured, instead of presuming that one format is always optimal.

### 3. Repeated modifications within one transaction must be coalesced

INSERTing `(changeset,table,pk)` directly in a per-row trigger hits a primary key conflict the second time the same row is modified.

The correct approach is to keep the first before in the staging area and keep updating the last after. An insert followed by a delete should cancel out; a delete followed by an insert becomes an update; a primary key change appears as a delete of the old key and an insert of the new key.

### 4. Branches must read a frozen baseline

`branch_overlay UNION live_main` would let a branch see mainline data that appeared only after it was created, and cannot provide pinned-version isolation.

State is currently resolved by commit ID. The row changes of a merge commit are recorded relative to the first parent commit, so state replay follows only the first-parent chain; finding the common ancestor uses all parent edges. The two traversals must not be mixed.

### 5. regclass should not be the only table identity in history

`regclass` is convenient for binding to a current PostgreSQL table, but when a table is dropped/recreated, moved across databases or its schema changes, it is not enough to represent a long-term logical identity.

A stable logical `table_id` and `schema_version` are currently assigned, with regclass as the current binding. The first phase freezes the schema; a complete interpreter for historical schemas is not implemented yet.

### 6. Cache commit snapshots, not dynamic branch names

The snapshot key is `(changeset, table_id)`. Once the cache is built, it becomes visible atomically together with a completion marker, and later reads start replaying from the most recent completed snapshot.

Creating a branch does not build a full-table snapshot; a baseline snapshot can be shared by multiple Agents. Materializing a new version on demand still copies that table's rows at that time; this is a logical-layer compromise, not storage-page-level copy-on-write.

### 7. Constraints and merging must be in the same publishing transaction

Pre-checking UNIQUE/FK only outside the transaction and writing afterwards leaves concurrency races. Constraints on ordinary tables also do not automatically apply to the JSON overlay layer.

Mainline merges are currently completed in one transaction: lock the branch head, compare the pinned versions, execute DML on the ordinary tables, then check the deferrable native constraints, and finally seal the history. An error rolls back the whole merge.

This covers only declared SQL constraints. Independent changes on different records may together violate business rules; these rules still need to be expressed by database constraints or a dedicated commit validator.

### 8. Existing applications are not completely unchanged

main's business tables and the form of SQL queries can be kept, but to bind code with multiple business writes, the application needs transaction wrapping, calls to begin/finish, and must not bypass capture.

Unregistered DML and TRUNCATE on main are currently all rejected. Existing applications, bulk imports, connection pools, background maintenance and other triggers need to be onboarded and verified one by one.

### 9. Time partitioning is not a ready-made answer

The PRIMARY KEY / UNIQUE of a PostgreSQL partitioned table must include the partition key. If changesets are partitioned by `created_at`, the original sketch with only `id` as the primary key cannot be copied as is, and the foreign key design is affected too.

So the first phase does not do time partitioning. The 4,000-row/10-Agent experiment first measures history growth and index access patterns. Partitioning also does not equal sharding across machines.

### 10. DDL event capture does not equal DDL versioning

Event triggers can observe some DDL events, but cannot on their own solve interpreting historical types, renames, column defaults, schema branches and migration dependencies.

The schema is fixed initially; later, migration files can be brought under code versioning, gradually supporting restricted additive changes. DDL replay and SQL compatibility should be designed and tested separately.

## Concurrency and scaling boundaries

Each Agent branch locks its own branch record; publishing transactions on main are still serialized on the same branch head. `expected_head` makes writes from an old version fail explicitly instead of overwriting.

This keeps a simple correctness model, but it cannot be used to claim "lock-free" or linear scaling. When many Agents modify main at the same time, lock waits and retry rates become key metrics.

If sharding by workspace in the future, code and data should as far as possible live in the same transactional shard. Atomic commits across workspaces must explicitly choose between distributed transactions and changing the interface semantics; the original guarantees cannot be kept just by splitting the database over a few machines.

## First-phase validation matrix

| Scenario | Behavior that should hold |
|---|---|
| Create multiple branches from a 4,000-row baseline | Business history and blobs are not copied |
| main keeps updating | Already created branches still read the original baseline |
| Update one row several times in the same transaction | One coalesced change record |
| Forget finish_change | COMMIT fails, and the ordinary business tables are rolled back too |
| Business constraint fails after modifying code | The code version and the business data are rolled back together |
| 10 Agents each write their own branch | Each succeeds, main is unaffected |
| 10 Agents write using the same old main head | One succeeds, the rest explicitly get a stale-version error |
| Conflict on the same path/same row | The whole merge is not published |
| Before and after snapshot materialization | Pinned-version read results are identical |
| Ordinary role tries to append to an old changeset | Permission denied |

Actual pass status is determined only by `test-results.json` and the test run output. A single local experiment is not equivalent to production capacity planning.

## Next steps, in order

1. Add integration tests for real application data types, query shapes, constraints and connection pools.
2. Optimize branch point lookups and predicate pushdown, read the merge target state in batches, and avoid repeatedly resolving the whole table row by row.
3. Add strictly defined field-level merging; conflict reports return base/ours/theirs and the schema version.
4. Design version retention and reclamation; immutable history does not mean it can be retained indefinitely at no cost.
5. Support restricted migrations, and handle triggers already on the original tables and external side effects.
6. Under larger loads, compare under the same conditions with plain PostgreSQL and database replication/branching solutions, before deciding on sharding, storage-page-level branching or replacing the persistence engine.

## Official references

- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
- [PostgreSQL row triggers and INSTEAD OF triggers](https://www.postgresql.org/docs/current/sql-createtrigger.html)
- [PostgreSQL partitioning limitations](https://www.postgresql.org/docs/current/ddl-partitioning.html#DDL-PARTITIONING-DECLARATIVE-LIMITATIONS)
- [PostgreSQL event trigger behavior](https://www.postgresql.org/docs/current/event-trigger-definition.html)
- [Anthropic Scion design](https://www.anthropic.com/engineering/redesigning-source-control)
