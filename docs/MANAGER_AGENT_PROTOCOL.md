中文 | [English](#english-version)

# 管理 Agent 的运行协议

> 当前状态（2026-10-30）：云存储已移除，任务状态只保存在内存中（`MemoryStore`），进程结束即丢失。下述接手流程只在同一进程内的存储上成立。

本文件可作为管理 Agent 的任务说明，或作为 AGENTS.md / CLAUDE.md 的运行协议内容。运行时把采用的确切文本存为证据对象，并在任务的 policy_ref 中固定其哈希；后续任务可以采用新版本，已有任务不能无记录地换规则。

## 身份与职责

管理身份按执行器选择：常规快照维护使用 `agentdb_manager`，只有 `vc.snapshot_status`、`vc.materialize`、只读的 `vc.snapshot_due` 与 `vc.history_health` 的执行权限；收紧限制使用 `agentdb_keeper`。2026-10-31 起管理 Agent 不持有超级用户或表的所有权（见 [Agent 边界](AGENT_BOUNDARY.md)）：修改版本引擎与系统规则由 Agent 写成迁移文件和测试提出，由数据库属主执行。受限角色不要求为所有 Agent 设置人类审批。数据库能力由账户提供，本文件定义使用方式。

新版维护器依赖 `sql/003_manager_role.sql`。旧安装先由数据库所有者执行 `python db.py install-manager`；通过 `--role agentdb_manager` 可在本机可信连接上检查角色权限。SET ROLE 不会从持有管理员连接的执行环境中移除管理员能力，不能用它声称已建立进程或身份隔离。

状态引擎只有内存后端（`MemoryStore`），进程结束即丢失；持久的事实在 PostgreSQL 中，快照与历史检查都可以从那里重新得出。在预登记的任务证明需要之前，不建立持久的管理状态，也不要用本地文件、SQLite 或新建 ops schema 代替。所有已接入工作 Agent 可以使用同等状态能力。

## 开始或接手一个任务

1. 从调度器取得 task_id；读取任务入口和最新状态。重要操作使用 fresh=true。
2. 读取固定 policy_ref 指向的规则对象。若没有规则引用，先明确本次任务采用的规则再继续需要规则判断的动作。
3. 取得有预算的上下文包；omitted_facts/omitted_evidence 不为零时，不把缺失字段解释为不存在。
4. 根据 next_action 与 uncertainties 读取必要证据。无需扫描全部历史。
5. 检查外部系统真实状态；日志“进行中”不说明动作尚未发生，“完成”也不代替必要的现场核验。

## 发布与执行

每个状态命令使用唯一 command_id，并提供读取到的 expected_version。结果不明时，以完全相同的 command_id、actor 和内容重试。Conflict 后重新判断，不只把版本号改大再覆盖。

外部操作先持久记录意图、目标和核验方法，再执行。PostgreSQL 与状态发布不共享事务；只对有幂等或可靠结果核对方法的操作自动恢复。当前可调用的维护示例是固定版本快照物化；任意新执行器需要先实现其探测与恢复协议。

命令的幂等发布不保证外部动作只执行一次。需要排他的动作，由执行器建立相应协调或 fencing；不能用任务 head 的 CAS 冒充数据库操作锁。

## 历史增长检查

`vc.merge` 每次合入仍遍历目标 head 可达的全部 changeset 两次（`vc._ancestors` 与 `vc._merge_base`）。预发布虚拟机上 2,026 个可达 changeset 时一次完整遍历 6.8 ms、merge_base 16 ms，约占 181 ms 合入的 10%，每增加 1,000 个约多 8 ms。当前不改用代数（generation numbers），由管理 Agent 定期检查并在值得改时报告，人类无需查询数据库。

- **执行**：以 `agentdb_manager` 连接构造 `SnapshotMaintenance(runtime, connection)`，调用 `check_history(actor, branch="main", policy_ref=None, merge_ms=None, task_id=None)`；可与 `run_due` 在同一轮维护中执行。它只调用 `vc.history_health(branch)`（`sql/006_history_health.sql`，STABLE，PostgreSQL 拒绝其中任何写入），不改数据。旧安装先由数据库所有者执行 `python db.py install-history-health`。
- **事实**：任务 `history-health-<branch>`（分支名中 `[A-Za-z0-9_-]` 以外的字符换成 `_`）的 facts 记录 `head`、`reachable_changesets`（合入遍历的数量）、`ancestor_walk_ms`（该遍历在数据库内的耗时）、`changesets_total`、`first_parent_depth`、`commits_since_snapshot`（每表从 head 到最近快照的第一父链提交数；`vc.snapshot_due(branch, n)` 恰在该值 ≥ n 时列出该表）与 `max_commits_since_snapshot`，以及所用阈值 `thresholds`、来源 `thresholds_source`（`policy` 或 `default`）、`median_merge_ms`/`merge_samples` 和越线项数 `crossed`。
- **阈值**：取自任务固定的 policy_ref。规则对象若是 JSON 对象且含 `history_health`，其中只允许 `max_reachable_changesets`（整数 1..10⁹，默认 20,000）与 `max_median_merge_ms`（大于 0、至多 3,600,000，默认 500）；键或值无效时抛出 Invalid，不发布、不改用默认值。规则对象不是 JSON（例如本协议原文）或没有该段时使用默认值。中位合入时间只在调用方传入实测的 `merge_ms`（如 `scripts/staging_drills.py growth` 的合入耗时）时评估；数据库不保存合入耗时。已有任务省略 policy_ref 时沿用任务原有的；传入不同的 policy_ref 抛出 Conflict，换规则需新建任务。
- **报告**：任一阈值达到或超过时，状态为 `running`，`next_action` 为 `merge history needs generation numbers (see docs/STAGING_S4_S5.md)`，每个越线项写成一条 uncertainty（含实测值与阈值）。未越线时状态为 `completed`，`next_action` 与 uncertainties 为空。接手 Agent 读取该任务即可得知结论；越线后再规划代数改造，作为单独的版本引擎变更。
- **幂等**：重复检查时若除 `ancestor_walk_ms` 外各项与已发布状态相同，则不发布（返回 `published=false`）；head 或数值变化才发布新检查点（command_id `history-health-<version>`）。并发检查者先发布时返回 Conflict，重新执行即会与其结果比较。按维护周期执行，不要每次合入后都检查。

## 边界检查

Agent 能关掉自己的超时，而登录、预算和授权由数据库执行，所以管理循环要做的是从外面检查这些仍然成立。

- **执行**：以 `agentdb_keeper` 连接构造 `BoundaryKeeper(runtime, connection)`，按周期调用 `check(actor, policy_ref=None, task_id="agent-boundary")`，或运行 `python agent_boundary.py keep`（默认每 5 秒一轮）。它调用 `vc.boundary_check()`（发现问题就冻结写入），再调用 `vc.end_stuck_agent_sessions`。数据库先动手，发布状态在后。
- **事实**：任务的 facts 记录 `frozen`（原因，至多 2,000 字符）、`problems_total`、`logins`、`ended_total`、`last_ended`（最近 20 个）、`open_databases`，以及所用阈值 `thresholds` 和来源 `thresholds_source`。问题的前 15 条写成 uncertainty。冻结时状态为 `running`，`next_action` 说明由属主修好原因并运行 `SELECT vc.unfreeze()`；否则为 `completed`。
- **阈值**：规则对象若是 JSON 对象且含 `agent_boundary`，其中只允许 `max_idle_in_transaction_seconds`（默认 60）与 `max_transaction_seconds`（默认 300），都是整数 1..86400；无效时抛出 Invalid，不执行任何 SQL。其余同历史增长检查。
- **幂等**：一轮没有变化就不发布。并发检查者先发布时返回 Conflict，下一轮再比较。数据库的动作（冻结、结束会话）不因 Conflict 而撤回，这份状态只是报告；进程结束后状态丢失不影响边界。
- **撤销**：`undo_login(actor, login)` 或 `python agent_boundary.py undo <login>`：先用 `vc.stop_agent` 花光它的预算并结束它的会话，再从最新的开始用 `vc.revert` 撤销它在 main 上的提交，不判断这些提交做了什么。遇到 AB005（之后又有人改过）就停下并报告，因为跳过它去撤销更早的提交会留下一个没有人见过的状态；撤销更新的提交后重跑即可继续。
- **报告**：`vc.agent_report(login)` 或 `python agent_boundary.py report <login>` 从历史得出一个登录落到 main 的内容（每张表的增改删、文件数、被撤销的合入数，只算 main 第一父链上的撤销提交），不依赖它自己的说法，登录收回后也能查。它尝试过但被拒绝的语句在服务器日志里。
- **备份**：`python agent_boundary.py backup --to <文件夹>` 以属主运行，由系统计划任务按周期触发，因为管理循环以 `agentdb_keeper` 运行，读不了表。只有恢复验证通过才留下 `.dump` 文件。
- **不做的事**：不解冻，不加预算，不直接写表。这些都要属主。

## 上下文与事实

将目标、实际版本、已核验结果和未决动作结构化保存。解释性摘要引用原始证据，不能代替证据。完整报告存对象，状态只保留必要字段和引用。状态超 32 KiB 时拆分任务或事实对象；不要丢弃事实来假装满足限制。

完成任务需要发布验证结果与证据引用。说明尚未验证的部分；内存后端测试与真实 PostgreSQL 上的结果必须分别报告。

---

<a id="english-version"></a>

# Operating Protocol for the Manager Agent

> Current status (2026-10-30): cloud storage has been removed, and task state lives only in memory (`MemoryStore`) and is lost when the process ends. The hand-off below holds only on a store within the same process.

This file can serve as the task description for the manager Agent, or as the operating protocol content of AGENTS.md / CLAUDE.md. When running, store the exact text adopted as an evidence object, and pin its hash in the task's policy_ref; later tasks may adopt a new version, but existing tasks cannot switch rules without a record.

## Identity and responsibilities

The management identity is chosen per executor: routine snapshot maintenance uses `agentdb_manager`, which has execute permission only on `vc.snapshot_status`, `vc.materialize`, and the read-only `vc.snapshot_due` and `vc.history_health`; tightening limits uses `agentdb_keeper`. Since 2026-10-31 the manager Agent holds no superuser or table ownership (see [Agent boundary](AGENT_BOUNDARY.md)): an agent proposes changes to the version engine and system rules as migration files and tests, and the database owner applies them. The restricted roles do not require setting up human approval for all Agents. Database capabilities are provided by accounts; this file defines how they are used.

The new maintainer depends on `sql/003_manager_role.sql`. On old installations the database owner first runs `python db.py install-manager`; with `--role agentdb_manager` the role's permissions can be checked over a trusted local connection. SET ROLE does not remove administrator capabilities from an execution environment that holds an administrator connection, and cannot be used to claim that process or identity isolation has been established.

The state engine has only its in-memory backend (`MemoryStore`), which is lost when the process ends; the durable facts are in PostgreSQL, and snapshot and history checks can be worked out from there again. Until a pre-registered task shows a need, do not build durable management state, and do not substitute local files, SQLite or a newly created ops schema. All connected work Agents can use the same state capabilities.

## Starting or taking over a task

1. Get the task_id from the scheduler; read the task entry point and the latest state. Important operations use fresh=true.
2. Read the rule object that the pinned policy_ref points to. If there is no rule reference, first make explicit which rules this task adopts before continuing with actions that require rule judgments.
3. Get the budgeted context package; when omitted_facts/omitted_evidence are not zero, do not interpret missing fields as non-existent.
4. Read the necessary evidence according to next_action and uncertainties. There is no need to scan the whole history.
5. Check the real state of external systems; "in progress" in a log does not mean the action has not happened yet, and "completed" does not replace necessary on-site verification.

## Publishing and execution

Each state command uses a unique command_id and provides the expected_version that was read. When the result is unclear, retry with exactly the same command_id, actor and content. After a Conflict, re-evaluate; do not just bump the version number and overwrite.

For external operations, first durably record the intent, the target and the verification method, then execute. PostgreSQL and state publishing do not share a transaction; automatic recovery is only done for operations that have an idempotent or reliable result-checking method. The currently callable maintenance example is pinned-version snapshot materialization; any new executor must first implement its probing and recovery protocol.

Idempotent publishing of commands does not guarantee that an external action is executed only once. For actions that need exclusivity, the executor establishes the corresponding coordination or fencing; the task head's CAS must not be passed off as a database operation lock.

## History growth check

Each `vc.merge` still traverses all changesets reachable from the target head twice (`vc._ancestors` and `vc._merge_base`). On the staging VM, with 2,026 reachable changesets, one full traversal takes 6.8 ms and merge_base 16 ms, about 10% of a 181 ms merge, with about 8 ms more per additional 1,000. Generation numbers are not adopted for now; the manager Agent checks periodically and reports when a change is worthwhile, so humans do not need to query the database.

- **Execution**: construct `SnapshotMaintenance(runtime, connection)` with an `agentdb_manager` connection and call `check_history(actor, branch="main", policy_ref=None, merge_ms=None, task_id=None)`; it can run in the same maintenance round as `run_due`. It only calls `vc.history_health(branch)` (`sql/006_history_health.sql`, STABLE; PostgreSQL rejects any write inside it) and does not change data. On old installations the database owner first runs `python db.py install-history-health`.
- **Facts**: the facts of task `history-health-<branch>` (characters other than `[A-Za-z0-9_-]` in the branch name are replaced with `_`) record `head`, `reachable_changesets` (the number traversed by a merge), `ancestor_walk_ms` (the in-database time of that traversal), `changesets_total`, `first_parent_depth`, `commits_since_snapshot` (per table, the number of first-parent-chain commits from head to the most recent snapshot; `vc.snapshot_due(branch, n)` lists that table exactly when this value is ≥ n) and `max_commits_since_snapshot`, as well as the thresholds used `thresholds`, their source `thresholds_source` (`policy` or `default`), `median_merge_ms`/`merge_samples`, and the number of crossed items `crossed`.
- **Thresholds**: taken from the task's pinned policy_ref. If the rule object is a JSON object and contains `history_health`, only `max_reachable_changesets` (integer 1..10⁹, default 20,000) and `max_median_merge_ms` (greater than 0, at most 3,600,000, default 500) are allowed in it; an invalid key or value raises Invalid, with no publish and no fallback to defaults. When the rule object is not JSON (for example the original text of this protocol) or has no such section, the defaults are used. The median merge time is evaluated only when the caller passes a measured `merge_ms` (such as the merge time from `scripts/staging_drills.py growth`); the database does not store merge times. When policy_ref is omitted for an existing task, the task's original one is kept; passing a different policy_ref raises Conflict, and changing rules requires a new task.
- **Reporting**: when any threshold is reached or exceeded, the status is `running`, `next_action` is `merge history needs generation numbers (see docs/STAGING_S4_S5.md)`, and each crossed item is written as one uncertainty (including the measured value and the threshold). When nothing is crossed, the status is `completed`, and `next_action` and uncertainties are empty. An Agent taking over can learn the conclusion by reading this task; after a threshold is crossed, plan the generation-number change as a separate version engine change.
- **Idempotency**: on a repeated check, if every item except `ancestor_walk_ms` is the same as the published state, nothing is published (returns `published=false`); only a change in head or the values publishes a new checkpoint (command_id `history-health-<version>`). When a concurrent checker publishes first, Conflict is returned, and re-running compares against its result. Run it on the maintenance cycle, not after every merge.

## Boundary check

Agents can turn off their own timeouts, while logins, budgets and grants are enforced by the database, so the manager's loop checks from outside that all of this still holds.

- **Execution**: construct `BoundaryKeeper(runtime, connection)` with an `agentdb_keeper` connection and call `check(actor, policy_ref=None, task_id="agent-boundary")` on a cycle, or run `python agent_boundary.py keep` (a round every 5 seconds by default). It calls `vc.boundary_check()`, which freezes writes on a problem, then `vc.end_stuck_agent_sessions`. The database acts first and publishing comes after.
- **Facts**: the task's facts record `frozen` (the reason, at most 2,000 characters), `problems_total`, `logins`, `ended_total`, `last_ended` (the latest 20) and `open_databases`, as well as the thresholds used `thresholds` and their source `thresholds_source`. The first 15 problems are written as uncertainties. While frozen, the status is `running` and `next_action` says the owner fixes the cause and runs `SELECT vc.unfreeze()`; otherwise it is `completed`.
- **Thresholds**: if the rule object is a JSON object and contains `agent_boundary`, only `max_idle_in_transaction_seconds` (default 60) and `max_transaction_seconds` (default 300) are allowed in it, both integers 1..86400; an invalid value raises Invalid before any SQL runs. Otherwise as in the history growth check.
- **Idempotency**: a round with no change publishes nothing. When a concurrent checker publishes first, Conflict is returned and the next round compares again. A Conflict does not take back what the database did (a freeze, ended sessions): this state is only a report, and losing it when the process ends does not affect the boundary.
- **Undo**: `undo_login(actor, login)` or `python agent_boundary.py undo <login>` first spends the login's budget with `vc.stop_agent` and ends its sessions, then reverts its commits on main with `vc.revert`, newest first, without judging what they did. It stops at AB005 (someone changed the row since) and reports it, because skipping that commit to undo an older one would leave a state nobody had; rerun after undoing the newer commit.
- **Report**: `vc.agent_report(login)` or `python agent_boundary.py report <login>` works out from history what a login landed on main (rows inserted, updated and deleted per table, files, merges reverted, counting only reverts on main's first-parent chain), without relying on what it says, and works after the login is retired. Statements it tried and was refused are in the server log.
- **Backup**: `python agent_boundary.py backup --to <folder>` runs as the owner and is triggered on a cycle by the system's scheduler, because the manager's loop runs as `agentdb_keeper`, which can't read tables. It leaves a `.dump` file only once the restore check passes.
- **What it doesn't do**: unfreeze, add budget or write tables directly. Those are the owner's.

## Context and facts

Save the goal, the actual version, verified results and open actions in structured form. Explanatory summaries reference the original evidence and cannot replace it. Complete reports are stored as objects, and the state keeps only necessary fields and references. When the state exceeds 32 KiB, split the task or the fact objects; do not discard facts to pretend to meet the limit.

Completing a task requires publishing the verification results and evidence references. State which parts are not yet verified; in-memory backend tests and results on a real PostgreSQL must be reported separately.
