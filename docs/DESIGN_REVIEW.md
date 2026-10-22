中文 | [English](#english-version)

# PostgreSQL 方案评审与第一阶段设计

日期：2026-10-15。目标规模：单表 4,000 行、10 个并发 Agent。

## 设计假设

用户的假设是：以人类开发工作节奏为背景形成的基础设施，可能不适合 Agent 大量并发、频繁试验和持续生成变更的工作模式。需要通过新的数据库接口、数据模型和存储组织来检验这一点。

这个假设不要求先证明 Spanner 已经故障。它应被转化为可测量的目标：每个任务的分支成本、固定版本首读延迟、小提交写放大、热点合并吞吐、失败恢复、保留历史成本等。

数据库长期以来已经由程序自动调用。Agent 的特殊性需要体现为上述负载特征，而不是简单把“原来有人类开发者使用”当作存储引擎失效的充分条件。

选择 PostgreSQL 是第一阶段的实现方式：复用事务、恢复、SQL 类型和约束，集中验证 Agent 工作流程。它不自动带来分布式扩展，也不排除将来更换底层存储。

## 原计划中保留的部分

- 不可变 changeset，分支为版本指针。
- 代码 blob 与业务数据历史位于同一 PostgreSQL 数据库。
- main 保留普通关系表，通过触发器捕获变化。
- 分支采用逻辑覆盖层，通过视图与 INSTEAD OF 触发器提供 SQL 读写。
- 历史状态由 changeset 回放，并可物化为派生快照。
- 三方比较、冲突检测和合入 main 的原生约束校验。

单个事务可以原子发布代码、行历史和 main 业务表的变化。这个保证限于数据库内部；不会自动原子部署外部代码文件、发送消息或执行付款等外部副作用。

## 必须修订的细节

### 1. 不可变提交需要封口

仅禁止 UPDATE/DELETE 不够：如果客户端还能 INSERT 到已有 changeset 的 `row_change` 或 `path_change`，这个版本仍然会变化。

当前采用独立工作区：`work_tx`、`pending_row`、`pending_path` 只承载当前 SQL 事务的暂存操作。`finish_change()` 一次写入历史，客户端无权直接访问历史写接口。事务结束前未完成封口，会被延迟约束触发器拒绝。

数据库管理员仍是可信方。它不是对超级用户防篡改的密码学账本。

### 2. before/after 是方便，不是版本控制唯一可能的基础

只保存 after，并从父版本读取 before，也可以做 diff、merge 和 revert。双向行镜像使比较和反向补偿更方便，代价是写放大与历史体积。

当前规模较小，因此保留双向镜像。未来应测量完整行、字段增量、对象引用和压缩之间的成本，而不是预设一种格式永远最优。

### 3. 一次事务内的重复修改必须归并

直接在逐行触发器中 INSERT `(changeset,table,pk)` 会在第二次修改同一行时遇到主键冲突。

正确做法是在暂存区保留第一次 before、不断更新最后 after。插入后删除应抵消；删除后插入成为更新；主键改变表现为旧键删除和新键插入。

### 4. 分支必须读取冻结的基线

`branch_overlay UNION live_main` 会让分支看到创建后才出现的主线数据，不能提供固定版本隔离。

当前按提交 ID 解析状态。合并提交的行变化相对第一父提交记录，因此状态回放只走第一父链；查找共同祖先则使用所有父边。两种遍历不能混用。

### 5. regclass 不应是历史中唯一的表身份

`regclass` 对当前 PostgreSQL 表绑定很方便，但表被删除/重建、跨库搬迁或 schema 改变时，不足以表示长期逻辑身份。

当前分配稳定的逻辑 `table_id` 和 `schema_version`，将 regclass 作为当前绑定。第一阶段冻结 schema，尚未实现历史 schema 的完整解释器。

### 6. 应缓存提交快照，不是动态分支名

快照键采用 `(changeset, table_id)`。缓存建立完毕后与完成标记一起原子可见，后续读取从最近的已完成快照开始回放。

创建分支不建立全表快照；基线快照可被多个 Agent 共享。按需物化新版本仍会复制该表当时的行，这是逻辑层的折衷，不是存储页级 copy-on-write。

### 7. 约束与合并必须处于同一个发布事务

仅在事务外预检查 UNIQUE/FK，随后写入，会留下并发竞态。普通表上的约束也不会自动作用于 JSON 覆盖层。

当前主线合入在一个事务中完成：锁定分支头，比较固定版本，执行普通表 DML，再校验可延迟的原生约束，最后封口历史。错误使整次合入回滚。

这只覆盖已声明的 SQL 约束。不同记录上的独立变化可能共同违反业务规则；这些规则仍需数据库约束或专门的提交验证器表达。

### 8. 现有应用并非完全无改动

main 的业务表和 SQL 查询形式可以保留，但若要绑定代码与多条业务写入，应用需要事务包装、调用 begin/finish，并且不得绕过捕获。

当前对 main 未登记的 DML 和 TRUNCATE 都拒绝。现有应用、批量导入、连接池、后台维护和其他触发器需要逐项接入验证。

### 9. 时间分区不是现成答案

PostgreSQL 分区表的 PRIMARY KEY / UNIQUE 必须包含分区键。若 changeset 按 `created_at` 分区，不能照搬仅以 `id` 为主键的原始草图，还会影响外键设计。

因此第一阶段不做时间分区。4,000 行/10 Agent 的实验先测历史增长与索引访问模式。分区也不等于跨机器分片。

### 10. DDL 事件捕获不等于 DDL 版本化

event trigger 能观察一部分 DDL 事件，但不能单独解决历史类型解释、重命名、列默认值、schema 分支和迁移依赖。

初期固定 schema；之后可以把迁移文件纳入代码版本，逐步支持受限的 additive 变更。DDL 重放与 SQL 兼容性应单独设计、测试。

## 并发与扩展边界

不同 Agent 分支各自锁自己的分支记录；main 上的发布事务仍按同一个分支头串行。`expected_head` 使旧版本写入明确失败，而不是覆盖。

这保留了简明的正确性模型，但不能据此声称“无锁”或线性扩展。大量 Agent 同时修改 main 时，锁等待和重试率会成为关键指标。

如果未来按 workspace 分片，代码与数据应尽量位于同一个事务分片中。跨 workspace 的原子提交要明确采用分布式事务还是改变接口语义；不能只把数据库拆到几台机器上就保留原有保证。

## 第一阶段验证矩阵

| 场景 | 应成立的行为 |
|---|---|
| 4,000 行基线创建多个分支 | 不复制业务历史和 blob |
| main 继续更新 | 已建立的分支仍读取原基线 |
| 同一事务更新一行多次 | 一条归并后的变化记录 |
| 忘记 finish_change | COMMIT 失败，普通业务表也回滚 |
| 修改代码后业务约束失败 | 代码版本和业务数据一起回滚 |
| 10 Agent 各写自己的分支 | 各自成功，main 不受影响 |
| 10 Agent 使用同一个旧 main head 写入 | 一个成功，其余明确获得陈旧版本错误 |
| 同一路径/同一行冲突 | 整次合入不发布 |
| 快照物化前后 | 固定版本读取结果一致 |
| 普通角色尝试追加旧 changeset | 权限拒绝 |

实际通过情况只以 `run_tests.py` 的结果文件（当时为 `test-results.json`，现为 `test-results-local.json`）和测试运行输出为准。单次本机实验不等同于生产容量规划。

## 后续顺序

1. 补充真实应用数据类型、查询形态、约束和连接池集成测试。
2. 优化分支点查与谓词下推，批量读取合并目标状态，避免逐行重复解析全表。
3. 增加严格定义的字段级合并；冲突报告返回 base/ours/theirs 与 schema 版本。
4. 设计版本保留和回收；不可变历史不代表可以无限保留而没有成本。
5. 支持受限迁移，并处理原始表已有触发器和外部副作用。
6. 在更大负载下与普通 PostgreSQL、数据库复制/分支方案进行同条件对照，再决定分片、存储页级分支或更换持久化引擎。

## 官方资料

- [PostgreSQL 事务隔离](https://www.postgresql.org/docs/current/transaction-iso.html)
- [PostgreSQL 行触发器与 INSTEAD OF 触发器](https://www.postgresql.org/docs/current/sql-createtrigger.html)
- [PostgreSQL 分区限制](https://www.postgresql.org/docs/current/ddl-partitioning.html#DDL-PARTITIONING-DECLARATIVE-LIMITATIONS)
- [PostgreSQL event trigger 行为](https://www.postgresql.org/docs/current/event-trigger-definition.html)
- [Anthropic Scion 设计](https://www.anthropic.com/engineering/redesigning-source-control)

---

<a id="english-version"></a>

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

Actual pass status is determined only by `run_tests.py`'s results file (then `test-results.json`, now `test-results-local.json`) and the test run output. A single local experiment is not equivalent to production capacity planning.

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
