中文 | [English](#english-version)

<a id="chinese-version"></a>

# 参考数据的受审变更

日期：2026-10-25。状态：计划，尚未实现，为本文没有运行测试。代码基线：`8df240733dcd73f9d0fbd47b8dadfff3005f1ccc`。标为"实测"的行为是 2026-10-25 在本机 PostgreSQL 16 的一次性测试库中观察到的，未保留结果文件。

## 1. 目标与边界

许多团队通过后台管理界面或种子数据迁移修改价格表、商品目录、功能配置和权限表，往往缺少审阅，历史不完整，也没有干净的撤销办法。本计划让这些表像代码一样修改：在分支上改，以 diff 审阅，合入时针对真实数据校验，可以撤销。一切发生在团队现有的 PostgreSQL 中，应用照常读取普通表。

边界：继续使用既有 PostgreSQL；Git 仍是代码的权威来源，Forkpoint 只为纳管表的行记录版本；迁移、备份和部署交给现有工具；通用多 Agent 编程协调不在本计划内。本计划不代表已获准采购、访问真实云服务或修改生产数据库；云端部署仍暂缓。

此前面向整个生产库接入的三份计划（低成本接入、结构与迁移、版本监管）已于 2026-10-25 删除，由本计划取代；其中仍然适用的引擎约束、试点选择、只读盘点、短暂停写的纳管路径和退出方式已并入本文。原文保留在 git 历史中，例如 `git show 7875f59:docs/LOW_COST_ADOPTION_PLAN.md`。

## 2. 哪些表适用

- 应用读取、正常运行时不写入的表。价格表、商品目录、功能配置和权限表符合；库存不符合，因为每笔订单都会写它；订单、会话和计数器也不符合。
- 表的大小有上限，在 G1 与试点项目一起确定。引擎目前在每表约 4,000 行的规模上测试过；分支读取每次都经 `vc.visible_rows` 重建整张表，不使用索引，成本随表的大小增长。
- 外键方向：未纳管的表引用纳管表（例如订单引用商品）正是本计划要保护的情形，合入时由 PostgreSQL 检查。纳管表引用未纳管表时，合入同样检查外键，但固定版本读取只覆盖纳管表。
- 分支视图命名为 `agent.<表名>`，不同 schema 中的同名表会冲突（实测 42P07）；在视图按 schema 命名之前，纳管表的名称须唯一。

## 3. 工作方式

| 步骤 | 做法 | 现状 |
|---|---|---|
| 1 纳管 | 在一个事务中阻止对该表的写入（读取不受影响），把现有行记录为第一个 changeset，安装捕获触发器、TRUNCATE 防护和分支视图，再建立快照。 | 新增：`vc.register_table` 目前只接受空表（[001_core.sql](../sql/001_core.sql)） |
| 2 修改 | Agent 或人员创建分支，通过 `agent.<表名>` 编辑，以 `vc.diff_rows` 的输出作为审阅内容。Agent 使用 [agent_lab.py](../agent_lab.py)；人员的入口由 D3 决定。 | 已有 |
| 3 合入 | `vc.merge_validated` 先做读集检查：分支依据的行若在 main 上已被改动，报 RS001（[008_read_set_merge.sql](../sql/008_read_set_merge.sql)）；随后 `vc.merge` 把行写入真实表，因此 PostgreSQL 自己的约束针对真实数据运行，包括未纳管表的外键：删除仍被订单引用的商品会失败。按代码，分支上只检查主键唯一，CHECK、NOT NULL、其他 UNIQUE 和外键在合入时检查。合入持有 main 的锁直到提交，因此依次进行；在参考数据的修改频率下这不是问题。 | 已有 |
| 4 撤销 | 撤销是另一个变更：恢复某次合入之前的行，按同样方式校验后合入。 | 新增辅助函数 |
| 5 审计 | `vc.pin` 和 `vc.rows_at` 按 revision 给出表当时的内容；`vc.history` 给出每个 changeset 的作者、说明和时间。 | 已有 |
| 6 防护 | 变更之外的写入会失败（实测 P0001），TRUNCATE 被阻止。另加定期内容哈希：把每张纳管表与 `rows_at(main head)` 比较，发现绕过触发器的写入，例如禁用触发器的数据恢复，或有权限的角色把 `session_replication_role` 设为 `replica` 之后的写入。 | 已有；内容哈希为新增 |
| 7 结构变更 | 迁移前后各加一步，见下文。 | 新增 |
| 8 退出 | 删除捕获触发器、TRUNCATE 防护和分支视图，表恢复为普通表；历史留在 `vc` 中，可另行导出。 | 基本已有 |

**结构变更。** 实测在纳管表上：修改分支视图用到的列的类型失败（0A000），删除这样的列失败（2BP01）；加列成功，但分支视图保留创建时的列清单，head 处的历史也没有新列，于是在 head 分出的分支读到 NULL，而 main 上是默认值；重命名列成功，但分支视图仍使用旧列名；迁移中的种子数据 INSERT 因没有开启变更而失败（P0001）。因此迁移前后各加一步，迁移工具和迁移脚本本身不变：

- 迁移前：对全部纳管表（都是小表）删除分支视图并暂停捕获，即删除捕获触发器。
- 迁移后：重新基线，把每张表的当前内容与 `rows_at(main head)` 的差异记录为一个 changeset，作者记为该迁移；新的结构版本记入一张只追加的表，因为 `vc.tracked_table` 拒绝 UPDATE；然后恢复捕获触发器并重建分支视图。迁移带来的数据变更（种子数据、回填）也由此记录，它们已在 Git 中作为迁移代码审阅过。
- 早于结构变更创建的分支在合入时被拒绝，须先变基到变更之后。旧 revision 仍可按 JSON 读取。
- 多数迁移工具可以在迁移前后运行步骤（例如 Flyway 的 callbacks），具体用哪个钩子取决于试点项目的工具。迁移窗口内其他写入方的改动也会被并入这次重新基线，因此窗口要短，并在 G2 中测量。

## 4. 复用与新增

| 部分 | 现状 | 说明 |
|---|---|---|
| 分支、`agent` 视图、`begin_change`/`finish_change`、`merge_validated`、`diff_rows`、`rows_at`/`pin`、快照（`materialize`、`snapshot_due`）、`history_health`、受限角色、`agent_lab.py` | 已有 | 按现状使用 |
| 纳管已有数据的表 | 新增 | 第 3 节第 1 步 |
| 迁移前后步骤与重新基线 | 新增 | 含结构版本的只追加表，以及拒绝跨结构变更的旧分支合入 |
| 内容哈希检查 | 新增 | 小表可做全表哈希 |
| 撤销辅助函数 | 新增 | 第 3 节第 4 步 |
| 只能写分支的角色 | 视 D2 而定 | 示例安装中的 `agentdb_client` 可以对 main 调用 `vc.begin_change`，也可以直接写 `app` 下的表（[002_demo_app.sql](../sql/002_demo_app.sql)） |

粗略估计新增部分为数周工作，以 G2 的实际结果为准。

## 5. 不做的事

应用自己的写入路径、大表、变更数据捕获、迁移管理、备份、部署漂移（都交给现有工具）、版本监管、云端状态。管理 Agent 的正式任务状态仍等待云端工作安排；小表在初期不需要定时的快照维护。发布清单中可以可选地记录每次发布对应的参考数据 revision，但不建监管进程。

## 6. 待用户决定的事项

每项决策在用户选择前保持未定；"决定前需要"一列给出应先具备的证据。

| ID | 决策 | 选项及后果 | 阻塞 | 决定前需要 |
|---|---|---|---|---|
| D1 | 试点项目与纳管表 | 选哪个项目、纳管哪些表；该项目的迁移工具决定第 3 节的前后步骤挂在哪个钩子上。 | G1 及以后全部门槛 | 对候选项目做只读盘点 |
| D2 | 谁能把变更合入 main | (a) 通过校验即合入，与 AGENTS.md 中"不因操作者是 Agent 而额外设置人类审批角色"的约定一致。(b) 指定审阅人批准后合入，可按表设置；试点团队的业务规则可能要求这样做。要强制 (b)，需要一个只能写分支的角色（第 4 节）。 | G2、G3 | D1（试点团队的审阅要求） |
| D3 | 现有写入方如何处理 | 后台管理工具和脚本（迁移由第 3 节的前后步骤处理）：(a) 各自开启变更，即在业务代码之外加一层小适配；(b) 改为走分支流程；(c) 按写入方选择。不处理的写入方会失败（实测 P0001）。 | G2 | G1 的写入方清单 |
| D4 | 多个环境 | (a) 只在生产库中通过分支修改。(b) 在预发布库审阅后，把 diff 作为生产库的新分支提升，并逐行核对生产库当前的前值。(c) 暂不处理，试点只用一个环境。 | 接入第二个环境 | G3 的结果 |

## 7. 门槛与试点

| 门槛 | 在此之前 | 检查 | 产出 |
|---|---|---|---|
| G1 试点与盘点 | 开始实现 | 已选定 D1；只读盘点候选表的行数、写入方、指向它的外键、DDL 频率和同名表；确定表大小上限、纳管时的停写预算和读取延迟预算 | 纳管表清单与经用户确认的预算 |
| G2 构建核验 | 在试点项目上纳管任何表 | 在专用测试库中用试点表的副本检查：纳管时读取不中断、停写在预算内；第 3 节的四种 DDL 和一次带回填的迁移经前后步骤后，`rows_at(main head)` 与真实表一致，跨结构变更的旧分支合入被拒绝；内容哈希能发现绕过触发器的写入；撤销后再次通过校验；D2、D3 所选路径可用 | 逐项的通过/失败 |
| G3 试点运行 | 宣称本计划可用或扩大范围 | 让试点项目固定数量的真实变更走分支流程，度量事先登记 | 实测结果，按停止规则决定是否继续 |

G3 事先登记的度量：合入时拦下的错误变更（分别按约束和读集检查计）、从提出到生效的时间、撤销次数、被拦下的直接写入、纳管时的停写时长、需要前后步骤的迁移次数、业务代码改动行数（目标为 0）。停止规则：若固定样本中没有拦下任何现有流程会漏掉的错误，且新流程更慢，就停止，并照项目实验惯例保留失败记录。

顺序：G1（约第 1 周）→ 实现第 4 节的新增部分（若 D2 选 (b)，还有只能写分支的角色）→ G2（约第 2–3 周）→ G3。

---

<a id="english-version"></a>

[中文](#chinese-version) | English

# Reviewed Changes to Reference Data

Date: October 25, 2026. Status: plan, not implemented; no test was run for this document. Code baseline: `8df240733dcd73f9d0fbd47b8dadfff3005f1ccc`. Behaviors marked "observed" were seen on 2026-10-25 in a throwaway test database on the local PostgreSQL 16; no result file was kept.

## 1. Goal and boundaries

Many teams change price lists, product catalogs, feature configuration, and permission tables through admin screens or seed-data migrations, often with little review, patchy history, and no clean undo. This plan lets them change those tables the way they change code: on a branch, reviewed as a diff, checked against live data at merge, and reversible. It all happens inside the team's existing PostgreSQL, and the application keeps reading ordinary tables.

Boundaries: the existing PostgreSQL stays; Git remains the authority for code, and Forkpoint versions only the rows of enrolled tables; migrations, backups, and deployment stay with existing tools; general multi-agent coding coordination is outside this plan. This plan does not authorize procurement, real cloud access, or production database changes; cloud deployment remains on hold.

Three earlier plans for enrolling whole production databases (low-cost adoption, schema and migrations, release supervision) were deleted on 2026-10-25 and replaced by this plan; the parts that still apply (engine constraints, the pilot choice, the read-only inventory, the short write-pause enrollment path, and the exit) are folded in here. Their text remains in git history, for example `git show 7875f59:docs/LOW_COST_ADOPTION_PLAN.md`.

## 2. Which tables qualify

- Tables the application reads but does not write in normal operation. Price lists, product catalogs, feature configuration, and permission tables qualify; stock does not, because every order writes it, and neither do orders, sessions, or counters.
- Table size has a limit, fixed with the pilot project at G1. The engine has so far been tested at about 4,000 rows per table; every branch read rebuilds the whole table through `vc.visible_rows` without indexes, so its cost grows with table size.
- Foreign-key direction: an unmanaged table that references an enrolled one (orders referencing products) is exactly the case this plan protects, and PostgreSQL checks it at merge. When an enrolled table references an unmanaged one, the merge still checks the foreign key, but pinned reads cover only enrolled tables.
- Branch views are named `agent.<table>`, so same-named tables in different schemas collide (observed 42P07); until views are named by schema, enrolled table names must be unique.

## 3. How it works

| Step | What happens | Status |
|---|---|---|
| 1 Enroll | In one transaction, block writes to the table (reads continue), record the existing rows as the first changeset, install the capture trigger, the TRUNCATE guard, and the branch view, then take a snapshot. | New: `vc.register_table` accepts only empty tables today ([001_core.sql](../sql/001_core.sql)) |
| 2 Change | An agent or a person creates a branch and edits through `agent.<table>`; the output of `vc.diff_rows` is what gets reviewed. Agents use [agent_lab.py](../agent_lab.py); D3 decides the entry point for people. | Exists |
| 3 Merge | `vc.merge_validated` first checks the read set: if rows the branch relied on have changed on main, it raises RS001 ([008_read_set_merge.sql](../sql/008_read_set_merge.sql)). `vc.merge` then writes the rows to the real table, so PostgreSQL's own constraints run against live data, including foreign keys from unmanaged tables: deleting a product that orders still reference fails. From the code, only primary-key uniqueness is checked on a branch; CHECK, NOT NULL, other UNIQUE, and foreign-key constraints are checked at merge. A merge holds main's lock until commit, so merges run one at a time, which is no problem at the rate reference data changes. | Exists |
| 4 Undo | A revert is another change: it restores the rows from before a merge and is checked and merged the same way. | New helper |
| 5 Audit | `vc.pin` and `vc.rows_at` give a table's contents at a revision; `vc.history` gives each changeset's author, message, and time. | Exists |
| 6 Guard | Writes outside a change fail (observed P0001), and TRUNCATE is blocked. A periodic content hash compares each enrolled table with `rows_at(main head)` to catch writes that bypass triggers, such as a data restore with triggers disabled, or writes after a role with the privilege sets `session_replication_role` to `replica`. | Exists; the content hash is new |
| 7 Schema change | A step before and after each migration, described below. | New |
| 8 Exit | Drop the capture trigger, the TRUNCATE guard, and the branch view; the table is ordinary again, and history stays in `vc` for optional export. | Mostly exists |

**Schema changes.** Observed on an enrolled table: changing the type of a column the branch view uses fails (0A000), and dropping such a column fails (2BP01). Adding a column succeeds, but the branch view keeps the column list it was created with and history at head lacks the new column, so a branch forked at head reads NULL where main holds the default. Renaming a column succeeds, but the branch view keeps the old name. A seed-data INSERT in a migration fails because no change is open (P0001). So each migration gets a step before and after it, and the migration tool and scripts stay unchanged:

- Before the migration: for every enrolled table (all small), drop the branch view and suspend capture by dropping the capture trigger.
- After the migration: re-baseline. Record the difference between each table's current contents and `rows_at(main head)` as one changeset authored as that migration; record the new schema version in an append-only table, because `vc.tracked_table` rejects UPDATE; then restore the capture trigger and recreate the branch view. Data changes made by the migration (seed data, backfills) are recorded this way too; they were already reviewed in Git as migration code.
- Branches created before a schema change are refused at merge until rebased onto it. Old revisions stay readable as JSON.
- Most migration tools can run steps before and after migrating (for example, Flyway callbacks); which hook to use depends on the pilot's tool. Changes by other writers during the migration window are folded into the same re-baseline, so the window must be short, and G2 measures it.

## 4. Reused and new

| Piece | Status | Notes |
|---|---|---|
| Branches, `agent` views, `begin_change`/`finish_change`, `merge_validated`, `diff_rows`, `rows_at`/`pin`, snapshots (`materialize`, `snapshot_due`), `history_health`, restricted roles, `agent_lab.py` | Exists | Used as is |
| Enrolling a table that already has rows | New | Section 3, step 1 |
| Migration steps and re-baseline | New | Includes the append-only schema-version table and refusing old-branch merges across a schema change |
| Content-hash check | New | Small tables can be hashed in full |
| Revert helper | New | Section 3, step 4 |
| Branch-only role | Depends on D2 | The demo installation's `agentdb_client` can call `vc.begin_change` on main and can write the tables in `app` directly ([002_demo_app.sql](../sql/002_demo_app.sql)) |

A rough estimate for the new pieces is a few weeks of work; G2 gives the real answer.

## 5. Out of scope

The application's own write path, large tables, change data capture, migration management, backups, deployment drift (all left to existing tools), release supervision, and cloud state. The manager agent's formal task state still waits for cloud work; small tables need no scheduled snapshot maintenance at first. A release manifest may optionally record the reference-data revision each release was tested with, but no supervisor process is built.

## 6. Decisions left to the user

Each decision stays open until the user chooses; "Settle with" names the evidence that should exist first.

| ID | Decision | Options and consequences | Blocks | Settle with |
|---|---|---|---|---|
| D1 | Pilot project and tables | Which project, and which of its tables to enroll; the project's migration tool decides which hook carries the migration steps of section 3. | G1 and every later gate | A read-only inventory of candidate projects |
| D2 | Who may land changes on main | (a) A change lands once it passes the checks, in line with AGENTS.md's rule of no extra human approval role just because the operator is an agent. (b) A named reviewer approves first, settable per table; the pilot team's business rules may require this. Enforcing (b) needs a branch-only role (section 4). | G2, G3 | D1 (the pilot team's review requirements) |
| D3 | What happens to existing writers | Admin tools and scripts (migrations are covered by the steps in section 3): (a) each opens a change, a small adapter outside the business code; (b) they move to the branch workflow; (c) per writer. A writer left as is fails (observed P0001). | G2 | The list of writers from G1 |
| D4 | More than one environment | (a) Change production only through its own branches. (b) Review on staging, then promote the diff as a new branch in production, checking each row's before-value against production's current row. (c) Not yet; the pilot uses one environment. | Adding a second environment | The G3 result |

## 7. Gates and pilot

| Gate | Before | Checks | Output |
|---|---|---|---|
| G1 Pilot and inventory | Starting implementation | D1 chosen; read-only inventory of candidate tables: row counts, writers, foreign keys pointing at them, DDL frequency, and same-named tables; fix the table-size limit, the write-pause budget for enrollment, and the read-latency budget | The list of tables to enroll and a budget the user has confirmed |
| G2 Build check | Enrolling any table in the pilot project | In a dedicated test database with copies of the pilot's tables: reads continue during enrollment and the write pause stays within budget; after the four DDL cases of section 3 and one migration with a backfill, run through the migration steps, `rows_at(main head)` matches the table and old-branch merges across the schema change are refused; the content hash catches writes that bypass triggers; a revert passes the checks again; the paths chosen in D2 and D3 work | Pass/fail per check |
| G3 Pilot run | Claiming this plan works or widening its scope | A fixed number of the pilot project's real changes go through branches, with measures registered beforehand | Measured results, and a continue-or-stop decision by the stop rule |

Measures registered before G3: bad changes caught at merge (counted separately for constraints and for the read-set check), time from proposal to live, reverts, blocked direct writes, the write pause during enrollment, migrations that needed the steps, and lines of business code changed (target: 0). Stop rule: if the fixed sample catches no bad change the current process would miss and the new workflow is slower, stop, and keep the failure on record as the project's experiments do.

Order: G1 (about week 1) → build the new pieces of section 4 (plus the branch-only role if D2 is (b)) → G2 (about weeks 2–3) → G3.
