中文 | [English](#english-version)

<a id="chinese-version"></a>

# 数据库结构与迁移：能力补齐计划

日期：2026-10-25；同日按设计审查修订。状态：设计与实施待办，未实现。代码核查基线：`c281b37cec116a03e6b2a98b42418fe373d86bf3`。

用户确定的方向：面向成熟项目，以 Forkpoint 承担数据库版本管理和原先需要人工执行的维护工作，追求低成本接入。通用多 Agent 编程协调、接口协商、测试编排、代码审查及合并队列保持现状，后续需要时接已有工具。本文件只规划数据库结构能力。入口文档是[接入成本](LOW_COST_ADOPTION_PLAN.md#chinese-version)，其中有现有引擎的约束（第 2 节）、决策 D1–D9 和门槛 G0–G4；配套见[版本监管](RELEASE_VERSION_SUPERVISION.md#chinese-version)。

## 1. 现状与必须解决的问题

`vc.tracked_table` 保存当前 `schema_version` 和有限的列描述；`vc.row_change` 带结构版本，但尚无完整的历史结构解释器。`vc.register_table` 只接受带主键的空普通表，拒绝 generated/identity 列；生成的 `agent` 视图通过当前真实表的复合类型解释 JSON（[001_core.sql](../sql/001_core.sql)）。因此，给表加列或改类型后，不能默认旧版本读取、分支写入和合入仍保持原语义。

现有两处行为使最简单的结构变更也不安全：

- `ALTER TABLE` 从不触发行级触发器，即使重写整表也一样。加带默认值的列或改列类型会改变 main 表的每一行，却没有捕获任何变更，`rows_at(main head)` 与真实表不再一致：在 head 处分出的分支读到 NULL，而 main 上是默认值。
- `vc._apply_main` 用 `jsonb_populate_record` 和对全部当前列赋值的 upsert 写入合入的行（[001_core.sql](../sql/001_core.sql)）。`ADD COLUMN` 之前的行文档没有新列的键，因此合入更早分出的分支时，分支改过的每一行的新列都会被写成 NULL；若新列为 NOT NULL，合入失败。

需要分别交付三项能力：迁移可追踪和可恢复；历史记录按当时结构解释；Agent 能在隔离环境验证结构不同的候选版本。保存 DDL 文本只完成其中一部分。

## 2. 必须新增或调整的对象

以下为设计对象，名称不代表已存在 SQL 表或 API。

| 对象 | 必要内容与规则 |
|---|---|
| 结构清单 | 稳定的表/列逻辑 ID、名称、类型与修饰、默认值、空值规则、主键、索引、约束、序列、生成表达式、依赖对象；另记录扩展、权限、RLS、触发器和函数等兼容范围。未知对象显式标记。规范编码及哈希格式带版本。同一库内 `attnum` 在重命名后不变；经 `pg_dump`/`pg_restore` 后已删除的列消失，编号可能改变，因此逻辑列 ID 由 Forkpoint 保存，恢复时重新映射。 |
| 迁移计划 | `migration_id`（沿用迁移工具自己的 revision ID）、执行时观察到的 SQL 的哈希（由 D11 的执行点记录；对 Alembic 等以代码编写的迁移，脚本文件的哈希不能代表其行为）、输入/输出结构引用、依赖迁移、目标对象、事务类别、锁与时限预算、验证和恢复方法。同 ID 换内容必须拒绝；若 D10 以迁移工具为准，可沿用工具自己的校验。 |
| 执行凭据 | 数据库侧记录每一步的执行身份、幂等键、输入/输出结构及完成事实；可与 DDL 同事务的步骤共同提交。云端管理任务只关联证据，不能替代数据库现场。 |
| 版本绑定 | changeset、行历史、快照、分支和发布清单均能定位确切结构；历史格式只读兼容，不改写封口历史。`regclass`/OID 仅作当前绑定，不作为跨恢复的逻辑身份。 |
| 转换规则 | 区分字段不存在与值为 NULL，读取和合入（`vc._apply_main`）都遵守；重命名沿用列 ID；默认值不在历史读取时重新求值；显式记录旧行向新结构投影/回填规则及其版本。 |

升级现有安装时，先核对历史涉及的结构是否可证明一致。无法还原的结构标为未知并限制相关读取/合入，不能拿当前目录冒充全部历史。先引入格式与兼容读取，再增加执行能力。

## 3. 操作支持顺序

| 优先级 | 操作 | 具体交付与边界 |
|---|---|---|
| P0 | 读取结构、迁移清单、漂移检测 | 对比目录事实与登记结构；接入现有迁移工具，不替换其脚本格式；复用哪种检测工具按 D8 决定。未登记 DDL 产生明确漂移状态。 |
| P1 | 新表、无默认值的可空列、普通索引 | 明确支持的类型与依赖白名单；更新版本绑定和分支视图，验证旧版本读写约定。无默认值的可空列也要先在读取和合入中落实第 2 节"转换规则"的不存在/NULL 区分（隐患见第 1 节，验收见 G5）。普通索引构建也要评估阻塞。 |
| P1 | 有默认值的加列、数据回填、CHECK/外键 | 区分默认值类别、历史投影和真实回填；真实回填是否记入历史由 D12 决定；回填分批、可续跑；约束的"已安装"和"已验证"分开记账。 |
| P2 | 在线索引、identity/sequence、generated 列 | 非事务步骤独立恢复；定义分支 ID 分配、冲突及合入策略；计算列在目标结构上验证，不能直接当普通可写列回放。 |
| P2 | 重命名、类型转换、NOT NULL | 跟踪稳定列身份、转换脚本和现存数据校验；类型转换重写整表而不触发行触发器，按 D12 处理；检查旧程序与旧分支是否仍兼容。 |
| P3 | 删列/删表、主键变化、分区、复杂依赖 | 先扩展、再迁移使用方、最后收缩；旧读者退出和数据保留条件具备后执行。早期版本明确报 unsupported，禁止猜测转换。 |

这些是计划顺序，不是支持声明。PostgreSQL 的 `ALTER TABLE` 锁级别依操作而异；`NOT VALID` 不能泛用于所有约束。执行器必须按操作分类。[PostgreSQL 16 文档](https://www.postgresql.org/docs/16/sql-altertable.html)

## 4. 分支结构和合并语义

本节仅在 D6 保留分支工作区时适用。若分支移出接入路径，只保留本节最后一段的迁移排序规则，验证环境按 D14 提供。

同一真实表不能同时拥有两种列结构。分两阶段实现：

1. **迁移管理阶段：** 分支可以携带候选迁移及结构引用，但实际 SQL 仍只使用当前支持的结构。结构不同的候选代码不得被描述为已经在分支环境运行通过。
2. **隔离验证阶段：** 按候选结构物化验证环境，再装载固定版本所需数据；环境形式与数据范围由 D14 决定。开发阶段使用现有本机 PostgreSQL 的专用测试库验证，不替换现有服务。

JSON 历史读取必须带结构引用；面向 SQL 的类型化视图按结构版本构建，覆盖范围由 D13 决定，不能让旧读取无声地跟随当前表类型。旧分支合入新主线前，先检查结构兼容，必要时执行已登记的投影/回填并重新验收，再做行冲突和原生约束检查。

隔离环境是派生验证环境，不自动成为版本事实来源。测试产生的数据默认不发布；需交付的数据变更必须明确选择范围，映射稳定表/列身份，形成可审查提案并走正常合入校验。禁止把整个测试库内容直接覆盖主线。

迁移按依赖图合并，不做 SQL 文本拼接；以哪一张依赖图为准由 D10 决定（迁移工具多已自带依赖图，例如 Alembic 的 `down_revision` 与 merge revision）。同一基线下两个独立加列计划也要检查名称、默认值、约束及依赖。相同 ID 和内容只执行一次；同 ID 不同内容拒绝；同对象重命名与类型变化、删除与写入等冲突返回结构化原因。主线变化后重新生成合并候选，旧测试证据不能自动沿用。

## 5. 执行、恢复与回退

流程：检查真实结构 → 固定计划及证据 → 获取数据库侧协调 → 复查前提 → 执行 → 核验结构和数据 → 发布结果。执行前设置锁等待、语句时间和资源预算；超限返回可恢复状态。

- **执行点（D11）：** 操作分类、白名单和锁预算需要一个执行点。若由现有迁移工具执行，Forkpoint 并不是执行者。库内拦截依赖事件触发器：PostgreSQL 16 上创建事件触发器需要超级用户；在 PL/pgSQL 中，`pg_event_trigger_ddl_commands()` 不提供 ALTER TABLE 的子命令（其 `command` 列是只能由 C 函数解读的内部类型），因此分类需要 C 扩展，或在每条 DDL 前后对比目录。事件触发器结合目录对比也用于发现未登记 DDL 的漂移；观察到 DDL 不代表已获得完整历史结构。[事件触发器文档](https://www.postgresql.org/docs/16/event-triggers.html)
- **事务内步骤：** 对允许同事务执行的 DDL、必要 DML、结构绑定及版本发布，失败整体回滚。具体操作必须经过白名单和迁移测试。
- **事务外步骤：** 如 `CREATE INDEX CONCURRENTLY` 不能放进事务块。按准备、执行中、已核验分别记录；中断后检查索引定义和有效性，不能仅因名字存在就认定成功。此类步骤不得标注为整组原子发布。[索引文档](https://www.postgresql.org/docs/16/sql-createindex.html)
- **并发与失联：** 数据库/执行器提供锁或 fencing，防止旧执行者继续发布；任务状态 CAS 只保护管理状态。数据库已完成而管理状态未更新时，先查凭据再续跑。
- **回退：** 优先使用验证过的前向修复或兼容代码回滚。删列、类型收窄、回填和序列变化不能依靠倒放 SQL 保证无损；数据库恢复会影响之后的业务写入，必须有独立的恢复范围及核验方案。

## 6. 待用户决定的事项

格式与 D1–D9 相同（见[接入成本](LOW_COST_ADOPTION_PLAN.md#chinese-version)第 4 节）。

| ID | 决策 | 选项及后果 | 阻塞 | 决定前需要 |
|---|---|---|---|---|
| D10 | 以哪一张迁移依赖图为准 | (a) 项目自己的工具（Alembic revision、Flyway 版本等）：Forkpoint 记录并按它核对，分支之间的迁移合并交给工具。(b) Forkpoint 自己的图：工具只作执行器，其自身顺序须与之保持一致，否则两张图会冲突。 | S1、第 4 节 | D1（用哪个工具） |
| D11 | 操作分类与白名单在哪里执行 | (a) Forkpoint 包装迁移工具，由自己执行其 SQL（需要工具的离线/SQL 输出模式或驱动钩子）。(b) 库内用事件触发器中止受管表上未支持的 DDL（需要超级用户，以及 C 扩展或目录前后对比）。(c) 只观察：事后检测并报告，不拦截。 | S1、S4、P1–P3 的执行 | G6 |
| D12 | 表重写与真实回填 | (a) 作为行变更捕获：历史与真实表保持一致，代价是每个受影响的行一条行变更（在 D2 (a) 下还会长时间持有 main 锁）。(b) 在某个 revision 处记录带版本的投影规则：历史读取时应用该规则，不逐行记录历史。(c) 按操作选择。 | S2、S4、第 3 节 P1 各行 | G5 |
| D13 | 类型化历史读取覆盖到哪里 | (a) 为曾用过的每个结构版本建类型化视图。(b) 只为活跃分支或发布引用的版本建类型化视图；更早的 revision 以 JSON 经串联投影读取。(c) 迁移时把历史改写为新结构：需要用户修改"不改写封口历史"的规则。 | S2 | 试点项目迁移历史会产生的结构版本数 |
| D14 | 验证环境与数据 | 环境：(a) 独立 schema 命名空间（遇到隐式 `public`、扩展或共享函数即失效）；(b) 用 `pg_dump`/`pg_restore` 建独立库（耗时与空间随数据量增长；`CREATE DATABASE … TEMPLATE` 要求源库没有其他连接，不能复制在线的生产库）；(c) 平台或文件系统克隆（取决于托管方式）。数据：全量复制、按外键闭包取子集或脱敏副本；生产数据的任何副本都适用个人数据规则。 | S3 | D1、D6、D9 |

## 7. 实施包、门槛与完成标准

| 包 | 工作项 | 完成标准 | 需要 |
|---|---|---|---|
| S0 结构事实 | 目录清单、稳定身份、规范编码、现有安装基线审计 | 恢复到新数据库后仍能对应逻辑对象；未知项如实报告；不改现有业务数据。 | D8 |
| S1 迁移执行 | 计划/凭据、既有迁移工具适配、受限操作执行、漂移检测 | 同 ID 重试不重复执行；内容不同则拒绝；失败不发布完成；主线/结构竞争可重现并被拦截。 | D10、D11、G6 |
| S2 历史兼容 | 修改 `rows_at`/投影、快照、捕获、视图、合入（含 `vc._apply_main`）的结构绑定 | 加列/重命名/转换前后的旧版本可按原结构解释；旧安装迁移及回退均有结果。 | D12、D13、G5 |
| S3 隔离验证 | 验证环境物化、固定数据装载、现有测试命令接入 | 两个不同结构候选可验证且不污染主线；权限、扩展和依赖限制明确。 | D6、D9、D14 |
| S4 在线演进 | 回填、在线索引、生成列和序列、分阶段收缩 | 中断可恢复；有真实规模下的锁时长、WAL、空间和延迟报告；无丢失写入。 | S1、S2、D12 |

| 门槛 | 在此之前 | 检查 |
|---|---|---|
| G5 DDL 与合入隐患 | S1 在受管表上执行任何操作 | 测试证明：(1) 加带默认值的列和改列类型之后，`rows_at(main head)` 与真实表一致，或差异已按 D12 记录；(2) 合入 `ADD COLUMN` 之前分出的分支不会覆盖新列；(3) 在 head 分出的分支读到的内容与 main 相同。 |
| G6 执行点可行性 | 决定 D11 | 用试点项目的迁移工具，证明所选执行点能看到并分类工具发出的每条 DDL，包括 `CREATE INDEX CONCURRENTLY` 和多语句迁移；确认试点托管环境上的权限。 |

依赖门槛：S0 先行；S1 可以先交付计划、观察和预演，但真正改变受管表结构前，必须通过 G5 和相关操作的 S2 历史兼容验收。S3 依赖可用的结构描述和读取契约，S4 依赖对应 S1/S2 及恢复验收。表中顺序不是允许先执行迁移、以后再补历史正确性。

所有包先写契约，再改行为；不与纯结构重构混合。必须覆盖：两迁移并发、旧分支合入新结构、迁移中断前后、失败回填、无效索引、外部 DDL、连接池旧计划、RLS/触发器、备份恢复、旧对象格式。根据变更扩展现有 `run_tests.py` 与迁移检查。

首个验收样例：已存在数据的商品表新增可空展示字段，新旧应用同时可读；随后回填并核验，再按兼容条件收紧约束。代码回滚期间不立即删字段。验收包括 G5 的三项、历史读取、失败整体回滚或可恢复分步状态，以及运行版本监管能按 D15 识别实际结构兼容情况。

---

<a id="english-version"></a>

[中文](#chinese-version) | English

# Database Schema and Migrations: Capability Plan

Date: October 25, 2026; revised the same day after a design review. Status: design and implementation backlog, not implemented. Code baseline: `c281b37cec116a03e6b2a98b42418fe373d86bf3`.

The user has chosen low-cost adoption in established projects, with Forkpoint taking on database version management and maintenance previously performed manually. General multi-agent coding coordination, interface negotiation, test orchestration, code review, and merge queues remain unchanged; existing tools can supply them when needed. This document plans database schema capabilities only. The entry document is [adoption costs](LOW_COST_ADOPTION_PLAN.md#english-version), which holds the current engine's constraints (section 2), decisions D1–D9, and gates G0–G4; the related plan is [release supervision](RELEASE_VERSION_SUPERVISION.md#english-version).

## 1. Current state and required outcomes

`vc.tracked_table` stores the current `schema_version` and a limited column description. Row history carries a schema version but has no complete historical schema interpreter. `vc.register_table` accepts only empty ordinary tables with primary keys, rejects generated/identity columns, and builds `agent` views that interpret JSON using the current physical table's composite type ([001_core.sql](../sql/001_core.sql)). Adding columns or changing types therefore does not automatically preserve historical reads, branch writes, or merge semantics.

Two current behaviors make even the simplest schema change unsafe:

- `ALTER TABLE` never fires row triggers, even when it rewrites the table. Adding a column with a default, or changing a column's type, changes every row of the main table without a captured change, so `rows_at(main head)` no longer matches the table: a branch forked at head reads NULL where main holds the default.
- `vc._apply_main` writes merged rows with `jsonb_populate_record` and an upsert that assigns every current column ([001_core.sql](../sql/001_core.sql)). A row document from before `ADD COLUMN` has no key for the new column, so merging a branch forked earlier writes NULL over that column in every row the branch touched, and fails if the column is NOT NULL.

Three separate outcomes are needed: traceable and recoverable migrations; historical records interpreted under their original schema; and isolated execution of candidates with different schemas. Saving DDL text addresses only part of this work.

## 2. Objects to introduce or change

These are proposed design objects, not existing SQL tables or APIs.

| Object | Required content and rules |
|---|---|
| Schema manifest | Stable table/column IDs, names, types and modifiers, defaults, nullability, keys, indexes, constraints, sequences, generated expressions, and dependencies. Also inventory extensions, permissions, RLS, triggers, and functions within the supported compatibility scope. Mark unknown objects explicitly. Version the canonical encoding and hash format. Within one database, `attnum` survives a rename; after `pg_dump`/`pg_restore`, dropped columns disappear and numbers can change, so Forkpoint stores logical column IDs and remaps them on restore. |
| Migration plan | `migration_id` (the migration tool's own revision ID), a hash of the SQL observed at execution (recorded by the D11 enforcement point; for code-based migrations such as Alembic's, a hash of the script file does not identify what it does), input/output schema references, prerequisite migrations, target objects, transaction class, lock/time budgets, verification, and recovery methods. Reject reuse of an ID with different content; if D10 makes the migration tool authoritative, the tool's own check can be reused. |
| Execution receipt | Database-side identity, idempotency key, input/output schema, and completion facts for each step. Commit these with DDL when the operation permits it. Cloud task state references evidence and does not replace database inspection. |
| Version binding | Changesets, row history, snapshots, branches, and releases identify an exact schema. Preserve readers for old formats and never rewrite sealed history. `regclass`/OIDs are current bindings, not logical identities across restores. |
| Conversion rules | Distinguish absent fields from NULL in reads and in merges (`vc._apply_main`); preserve column IDs across renames; do not reevaluate defaults during historical reads. Explicitly version projections and backfills between schemas. |

Upgrades must first establish whether the schemas used by existing history are provably consistent. Mark unrecoverable schema information as unknown and restrict affected reads/merges rather than substituting today's catalog for historical facts. Introduce formats and compatible readers before execution features.

## 3. Order of operation support

| Priority | Operation | Deliverable and boundary |
|---|---|---|
| P0 | Schema inspection, migration inventory, drift detection | Compare catalog facts with registered schema; integrate existing migration tools without replacing script formats; D8 decides which detection tool to reuse. Report unregistered DDL as drift. |
| P1 | New tables, nullable columns without defaults, ordinary indexes | Explicit type/dependency allowlist; update version bindings and branch views; verify old-version read/write behavior. Nullable columns without defaults also need section 2's absent-versus-NULL conversion rule applied in reads and merges first (hazard in section 1, acceptance in G5). Assess blocking even for ordinary index builds. |
| P1 | Columns with defaults, backfills, CHECK/foreign keys | Separate default categories, historical projection, and physical backfill; D12 decides whether a physical backfill is recorded as history. Make backfills resumable in batches; distinguish installed constraints from validated constraints. |
| P2 | Concurrent indexes, identity/sequences, generated columns | Recover nontransactional steps independently. Define branch ID allocation, conflicts, and merge policy. Validate computed columns under the target schema rather than replaying them as ordinary writable values. |
| P2 | Renames, type conversions, NOT NULL | Track stable column identities, conversion scripts, and existing-data validation; a type conversion rewrites the table without firing row triggers and follows D12; check compatibility with old applications and branches. |
| P3 | Dropping columns/tables, changing primary keys, partitions, complex dependencies | Expand, migrate consumers, then contract. Require old readers to exit and retention conditions to hold. Early releases report unsupported operations rather than guessing conversions. |

This is a plan, not a support claim. PostgreSQL's `ALTER TABLE` lock levels vary by operation, and `NOT VALID` is not available for every constraint. Executors must classify operations. [PostgreSQL 16 documentation](https://www.postgresql.org/docs/16/sql-altertable.html)

## 4. Branch schemas and merge semantics

This section applies only if D6 keeps branch workspaces. If branches leave the adoption path, only the migration-ordering rules in the last paragraph of this section remain, and validation environments come from D14.

A physical table cannot have two column layouts simultaneously. Implement this in two stages:

1. **Migration management:** a branch may carry a candidate migration and schema reference, while actual SQL still uses the currently supported schema. Do not claim that code requiring a different schema has already passed execution in that branch.
2. **Isolated validation:** materialize a validation environment with the candidate schema, then load the required fixed-version data; D14 decides the kind of environment and the data scope. During development, validate with dedicated test databases on the existing local PostgreSQL service, without replacing that service.

JSON history must carry schema references; typed SQL views are built per schema version, to the extent D13 decides, so old reads cannot silently follow today's table type. Before merging an old branch into a newer main, check schema compatibility, apply registered projections/backfills where necessary, revalidate, and then check row conflicts and native constraints.

Isolated environments are derived validation environments, not automatic sources of version truth. Test-generated data is unpublished by default. Data changes intended for delivery must have an explicitly selected scope, map to stable table/column identities, become reviewable proposals, and pass normal merge validation. Never overwrite main with an entire test database.

Merge migrations as a dependency graph, not concatenated SQL text; D10 decides which graph is authoritative (most migration tools already keep one, for example Alembic's `down_revision` and merge revisions). Even independent column additions must be checked for names, defaults, constraints, and dependencies. Execute an identical ID and content once; reject changed content under the same ID. Return structured conflicts for incompatible renames/conversions and deletion versus writing. Rebuild the candidate when main changes; earlier test evidence does not automatically remain valid.

## 5. Execution, recovery, and rollback

Flow: inspect the actual schema → pin the plan and evidence → obtain database-side coordination → recheck preconditions → execute → verify schema and data → publish results. Set lock-wait, statement-time, and resource budgets before execution; budget exhaustion returns a recoverable state.

- **Enforcement point (D11):** classification, the allowlist, and lock budgets need an enforcement point. When an existing migration tool runs the migration, Forkpoint is not the executor. Enforcement inside the database relies on event triggers: creating one needs superuser on PostgreSQL 16, and in PL/pgSQL `pg_event_trigger_ddl_commands()` does not expose ALTER TABLE subcommands (its `command` column is an internal type that only C functions can read), so classification needs a C extension or a catalog comparison before and after each DDL command. Event triggers combined with catalog comparison also detect drift from unregistered DDL; observing DDL does not establish complete historical schemas. [Event trigger documentation](https://www.postgresql.org/docs/16/event-triggers.html)
- **Transactional steps:** where allowed, commit DDL, required DML, schema bindings, and version publication together, with full rollback on failure. Each operation requires allowlist coverage and migration tests.
- **Nontransactional steps:** `CREATE INDEX CONCURRENTLY`, for example, cannot run inside a transaction block. Record preparation, execution, and verified completion separately. After interruption, inspect index definition and validity; existence of its name is insufficient. Do not label the entire sequence atomic. [Index documentation](https://www.postgresql.org/docs/16/sql-createindex.html)
- **Concurrency and lost workers:** database/executor locks or fencing prevent stale executors from publishing. Task-state CAS protects management state only. If the database completed before task state was updated, inspect the receipt before resuming.
- **Rollback:** prefer tested forward repairs or compatible code rollback. SQL reversal does not guarantee lossless recovery after column deletion, narrowing conversions, backfills, or sequence changes. Restoring a database affects later business writes and needs a separate recovery scope and verification plan.

## 6. Decisions left to the user

Same format as D1–D9 (section 4 of [adoption costs](LOW_COST_ADOPTION_PLAN.md#english-version)).

| ID | Decision | Options and consequences | Blocks | Settle with |
|---|---|---|---|---|
| D10 | Which migration graph is authoritative | (a) The project's own tool (Alembic revisions, Flyway versions, and so on): Forkpoint records and checks against it, and merging migrations between branches is the tool's job. (b) Forkpoint's own graph: the tool becomes an executor, and its own ordering must stay consistent with Forkpoint's, or the two graphs conflict. | S1, section 4 | D1 (which tool) |
| D11 | Where classification and the allowlist are enforced | (a) Forkpoint wraps the migration tool and executes its SQL itself (needs the tool's offline/SQL output mode or a driver hook). (b) Inside the database, with event triggers that abort unsupported DDL on managed tables (needs superuser, plus a C extension or a catalog comparison). (c) Observe only: detect and report after the fact, with no blocking. | S1, S4, execution of P1–P3 | G6 |
| D12 | Table rewrites and physical backfills | (a) Captured as row changes: history stays equal to the table, at the cost of one row change per affected row (and, under D2 (a), a long hold on main's lock). (b) Recorded as a versioned projection rule at a revision: history readers apply the rule, with no per-row history. (c) Per operation. | S2, S4, the P1 rows of section 3 | G5 |
| D13 | How far typed historical reads go | (a) Typed views for every schema version ever used. (b) Typed views only for versions referenced by live branches or releases; older revisions are read as JSON through chained projections. (c) Rewrite history into the new schema at migration time: requires the user to change the "never rewrite sealed history" rule. | S2 | The number of schema versions the pilot's migration history would create |
| D14 | Validation environment and data | Environment: (a) a separate schema namespace (fails with implicit `public`, extensions, or shared functions); (b) a separate database by `pg_dump`/`pg_restore` (time and space grow with data; `CREATE DATABASE … TEMPLATE` requires no other connections to the source, so it cannot copy a live production database); (c) a platform or filesystem clone (depends on hosting). Data: a full copy, a subset closed under foreign keys, or a masked copy; personal-data rules apply to every copy of production data. | S3 | D1, D6, D9 |

## 7. Implementation packages, gates, and acceptance

| Package | Work | Acceptance | Needs |
|---|---|---|---|
| S0 Schema facts | Catalog inventory, stable identities, canonical encoding, baseline audit for existing installations | Logical objects remain identifiable after restore to another database; unknowns are explicit; business data remains unchanged. | D8 |
| S1 Migration execution | Plans/receipts, existing-tool adapters, restricted execution, drift detection | Retries do not execute twice; changed content is rejected; failure never publishes completion; main/schema races are reproducible and detected. | D10, D11, G6 |
| S2 Historical compatibility | Schema bindings in `rows_at`/projection, snapshots, capture, views, and merge (including `vc._apply_main`) | Versions before additions/renames/conversions retain their original interpretation; upgrade and rollback of existing installations have recorded results. | D12, D13, G5 |
| S3 Isolated validation | Validation-environment materialization, fixed-data loading, integration with existing test commands | Two different-schema candidates can be validated without polluting main; permission, extension, and dependency limits are explicit. | D6, D9, D14 |
| S4 Online evolution | Backfills, concurrent indexes, generated columns/sequences, staged contraction | Recovery after interruption; measured lock time, WAL, space, and latency at representative scale; no lost writes. | S1, S2, D12 |

| Gate | Before | Checks |
|---|---|---|
| G5 DDL and merge hazards | S1 executing anything on a managed table | Tests show that (1) after adding a column with a default and after changing a column type, `rows_at(main head)` matches the table, or the difference is recorded according to D12; (2) merging a branch forked before `ADD COLUMN` does not overwrite the new column; (3) a branch forked at head reads what main reads. |
| G6 Enforcement feasibility | Choosing D11 | With the pilot's migration tool, show that the chosen enforcement point sees and classifies every DDL command the tool emits, including `CREATE INDEX CONCURRENTLY` and multi-statement migrations; confirm the privileges on the pilot's hosting. |

Dependency gates: S0 comes first. S1 can initially deliver planning, observation, and rehearsal, but changing a managed table's schema also requires G5 and the relevant operation's S2 historical-compatibility acceptance. S3 needs usable schema descriptions and read contracts; S4 needs the corresponding S1/S2 and recovery checks. The table order does not permit executing migrations first and adding historical correctness later.

Define contracts before changing behavior, separately from structural refactoring. Cover concurrent migrations, old-branch merges into new schemas, interruption windows, failed backfills, invalid indexes, external DDL, stale connection-pool plans, RLS/triggers, backup/restore, and old object formats. Extend `run_tests.py` and migration checks as appropriate.

First acceptance scenario: add a nullable display field to a populated products table, preserve reads by old and new applications, backfill and verify, then tighten constraints when compatibility permits. Do not immediately remove the field during code rollback. Verify the three G5 checks, historical reads, atomic rollback or recoverable multi-step state, and the supervisor's ability to assess actual schema compatibility as D15 defines it.
