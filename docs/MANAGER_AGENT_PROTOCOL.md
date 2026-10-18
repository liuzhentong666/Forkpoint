# Superuser Agent 的运行协议

> 当前状态（2026-10-17）：云服务未经真实验证，云端部署暂缓。下述云端交接属于待联调的运行设计；当前不执行上传、部署或真实云读写，也不把本地测试当作云端成功记录。

本文件可作为管理 Agent 的任务说明，或作为 AGENTS.md / CLAUDE.md 的运行协议内容。正式运行时，把采用的确切文本上传为所选云后端的证据对象，并在任务的 policy_ref 中固定其哈希；后续任务可以采用新版本，已有任务不能无记录地换规则。

## 身份与职责

管理身份按执行器选择：常规快照维护使用 `agentdb_manager`，只有 `vc.snapshot_status`、`vc.materialize`、只读的 `vc.snapshot_due` 与 `vc.history_health` 的执行权限；修改版本引擎与系统规则使用已授权的管理员身份。受限角色由本次本机审查新增，不要求为所有 Agent 设置人类审批。数据库能力由账户提供，本文件定义使用方式。

新版维护器依赖 `sql/003_manager_role.sql`。旧安装先由数据库所有者执行 `python db.py install-manager`；通过 `--role agentdb_manager` 可在本机可信连接上检查角色权限。SET ROLE 不会从持有管理员连接的执行环境中移除管理员能力，不能用它声称已建立进程或身份隔离。

正式状态后端通过 provider 选择，使用满足项目协议的云端对象存储；国内目标为阿里云 OSS，另保留 GCS/S3。真实云端接入尚未验证。不要把任务的唯一状态留在聊天上下文、本地文件、SQLite 或新建 ops schema。运行时内存是可丢弃缓存。所有已接入工作 Agent 可以使用同等状态服务能力。

## 开始或接手一个任务

1. 从调度器取得 task_id；读取任务入口和最新状态。重要操作使用 fresh=true。
2. 读取固定 policy_ref 指向的规则对象。若没有规则引用，先明确本次任务采用的规则再继续需要规则判断的动作。
3. 取得有预算的上下文包；omitted_facts/omitted_evidence 不为零时，不把缺失字段解释为不存在。
4. 根据 next_action 与 uncertainties 读取必要证据。无需扫描 bucket 或全部历史。
5. 检查外部系统真实状态；日志“进行中”不说明动作尚未发生，“完成”也不代替必要的现场核验。

## 发布与执行

每个状态命令使用唯一 command_id，并提供读取到的 expected_version。网络结果不明时，以完全相同的 command_id、actor 和内容重试。409 后重新判断，不只把版本号改大再覆盖。

外部操作先持久记录意图、目标和核验方法，再执行。PostgreSQL 与云端状态发布不共享事务；只对有幂等或可靠结果核对方法的操作自动恢复。当前可调用的维护示例是固定版本快照物化；任意新执行器需要先实现其探测与恢复协议。

命令的幂等发布不保证外部动作只执行一次。需要排他的动作，由执行器建立相应协调或 fencing；不能用任务 head 的 CAS 冒充数据库操作锁。

## 历史增长检查

`vc.merge` 每次合入仍遍历目标 head 可达的全部 changeset 两次（`vc._ancestors` 与 `vc._merge_base`）。预发布虚拟机上 2,026 个可达 changeset 时一次完整遍历 6.8 ms、merge_base 16 ms，约占 181 ms 合入的 10%，每增加 1,000 个约多 8 ms。当前不改用代数（generation numbers），由管理 Agent 定期检查并在值得改时报告，人类无需查询数据库。

- **执行**：以 `agentdb_manager` 连接构造 `SnapshotMaintenance(runtime, connection)`，调用 `check_history(actor, branch="main", policy_ref=None, merge_ms=None, task_id=None)`；可与 `run_due` 在同一轮维护中执行。它只调用 `vc.history_health(branch)`（`sql/006_history_health.sql`，STABLE，PostgreSQL 拒绝其中任何写入），不改数据。旧安装先由数据库所有者执行 `python db.py install-history-health`。
- **事实**：任务 `history-health-<branch>`（分支名中 `[A-Za-z0-9_-]` 以外的字符换成 `_`）的 facts 记录 `head`、`reachable_changesets`（合入遍历的数量）、`ancestor_walk_ms`（该遍历在数据库内的耗时）、`changesets_total`、`first_parent_depth`、`commits_since_snapshot`（每表从 head 到最近快照的第一父链提交数；`vc.snapshot_due(branch, n)` 恰在该值 ≥ n 时列出该表）与 `max_commits_since_snapshot`，以及所用阈值 `thresholds`、来源 `thresholds_source`（`policy` 或 `default`）、`median_merge_ms`/`merge_samples` 和越线项数 `crossed`。
- **阈值**：取自任务固定的 policy_ref。规则对象若是 JSON 对象且含 `history_health`，其中只允许 `max_reachable_changesets`（整数 1..10⁹，默认 20,000）与 `max_median_merge_ms`（大于 0、至多 3,600,000，默认 500）；键或值无效时抛出 Invalid，不发布、不改用默认值。规则对象不是 JSON（例如本协议原文）或没有该段时使用默认值。中位合入时间只在调用方传入实测的 `merge_ms`（如 `staging_drills.py growth` 的合入耗时）时评估；数据库不保存合入耗时。已有任务省略 policy_ref 时沿用任务原有的；传入不同的 policy_ref 抛出 Conflict，换规则需新建任务。
- **报告**：任一阈值达到或超过时，状态为 `running`，`next_action` 为 `merge history needs generation numbers (see docs/STAGING_S4_S5.md)`，每个越线项写成一条 uncertainty（含实测值与阈值）。未越线时状态为 `completed`，`next_action` 与 uncertainties 为空。接手 Agent 读取该任务即可得知结论；越线后再规划代数改造，作为单独的版本引擎变更。
- **幂等**：重复检查时若除 `ancestor_walk_ms` 外各项与已发布状态相同，则不发布（返回 `published=false`）；head 或数值变化才发布新检查点（command_id `history-health-<version>`）。并发检查者先发布时返回 Conflict，重新执行即会与其结果比较。每次发布占用一次任务发布额度（OSS 每任务 524,288 次），按维护周期执行，不要每次合入后都检查。

## 上下文与事实

将目标、实际版本、已核验结果和未决动作结构化保存。解释性摘要引用原始证据，不能代替证据。完整报告存对象，状态只保留必要字段和引用。状态超 32 KiB 时拆分任务或事实对象；不要丢弃事实来假装满足限制。

完成任务需要发布验证结果与证据引用。说明尚未验证的部分；内存后端测试、SDK 契约测试与各后端的真实云端联调必须分别报告。
