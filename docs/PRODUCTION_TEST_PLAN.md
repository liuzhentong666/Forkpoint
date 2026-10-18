中文 | [English](#english-version)

# 生产环境测试计划（草案）

2026-10-18。**草案，待用户确认。** 云服务仍未经真实验证，云端工作仍暂缓；本文不改变这一状态，也不授权采购资源、运行 `smoke_cloud.py` 或使用真实业务数据。文中标注「待定」的条目需要用户决定，其余是建议的默认做法。

## 1. 要证明什么

“生产环境测试”在本项目中指：在与预期生产形态一致的环境中，用可控的数据和负载证明下面四件事，并在每一阶段留下可复查的证据。

1. **正确性在真实部署形态下仍然成立**：联合提交、固定版本读取、合入冲突与原生约束、迁移可重复执行，这些在 Linux 主机上的 PostgreSQL、经 TLS 的远程客户端下与本机结果一致。
2. **管理状态的云端持久化真正可用**：任务入口条件发布、旧命令去重、两个中断窗口的接手，在真实对象存储上成立。目前只有内存模型与 SDK 测试替身的结果。
3. **故障可以恢复**：数据库重启、备份恢复、客户端与状态服务之间断网、对象存储报错时，系统给出明确结果，不丢失已提交内容，不把未知结果报告为成功。
4. **在目标负载及其增长下，成本可解释**：目标负载为单表约 4,000 行、10 个并发 Agent；另测历史增长后的合入、读取与恢复成本。只记录测量值和环境，不把单次结果写成容量承诺。

## 2. 不在本轮范围

- 真实业务数据与生产流量（第 5 阶段通过前一律使用合成数据）。
- 多区域、高可用切换、分片和 DDL 版本化；这些是已知未实现的能力，测试只确认它们的缺失不会被误报为可用。
- 公网 HTTPS 入口、多租户和计费。

## 3. 阶段与通过条件

每个阶段都在前一阶段通过后才开始；任何一项失败都停在该阶段，先定位再继续。所有结果按“内存模拟 / SDK 契约 / 本机 PostgreSQL / 预发布 PostgreSQL / 真实云服务”分别记录。

| 阶段 | 内容 | 通过条件 | 状态 |
|---|---|---|---|
| S0 基线 | 冻结一个已审查的提交，本机全部套件通过 | `run_tests.py`、状态单元测试、`review_local.py`、`check_merge_migration.py` 全部通过，记录提交号 | 已完成：提交 06243d7 |
| S1 预发布 PostgreSQL | Linux 主机上的 PostgreSQL 16+，经 TLS 远程连接；专用登录角色（见 [预发布接入](STAGING.md)） | `db.py check` 显示 `ssl=true`，`sslmode=verify-full`；以专用角色运行 `run_tests.py`、`review_local.py`、`check_merge_migration.py` 全部通过；不需要超级用户 | **已通过（2026-10-18）**：本机 VirtualBox 虚拟机（方案 B），四项核验全部通过，见 [S1 结果](STAGING_S1_RESULTS.md) |
| S2 真实对象存储 | 解除云端暂缓，仅对一个隔离 prefix 运行 `smoke_cloud.py` | 十写者只有一个发布成功；旧命令重试返回原结果；发布前后中断均可接手；冷读取只读两个逻辑对象；结果标注提供商、地域与日期 | 待用户解除暂缓 |
| S3 状态服务部署 | ECS（或其他主机）常驻状态服务 + SSH 隧道 | 服务重启后经 API 读回已有任务并核对版本；`maintain_snapshot.py --state-url` 在预发布 PostgreSQL 上完成一次接手 | 待 S2 |
| S4 负载与增长 | 目标负载重复 3 次；再把历史加深一个数量级 | 记录合入阶段耗时、重试次数、`vc.merge` SQL 时间、读取量；与本机结果并列；无断言失败 | **已执行（2026-10-18，本机虚拟机）**：目标负载通过；历史增长测出 `_file_state` 全历史回溯与快照间隔导致的成本增长；修复（005 迁移与快照策略）合入后复测，2,000 个提交时合入 50.7 s → 181 ms、提交 499 ms → 20 ms，见 [S4/S5 结果第 7 节](STAGING_S4_S5.md) |
| S5 故障演练 | PostgreSQL 重启、备份与恢复、断网、对象存储错误注入 | 恢复后 `vc.work_tx` 为 0；恢复库的 head 与业务表哈希与备份点一致；未知结果都被报告为未知 | **数据库部分已通过（2026-10-18）**：备份恢复、会话终止、正常重启、断电、断网五项通过；断网暴露的失联会话持锁风险已通过服务器超时设置限制在约 61 s 内，复测通过，见 [S4/S5 结果第 7 节](STAGING_S4_S5.md)。对象存储错误注入待 S2/S3 |
| S6 真实 Agent | 在预发布环境重复已登记的两 Agent 实验 | 按 [实验协议](../experiments/gpt_repeat/PROTOCOL.md) 新登记一轮；覆盖事件为 0；失败样本保留 | 待 S1 |

## 4. 数据与权限规则

- 只用合成数据（`review_local.py` / `run_tests.py` 的 4,000 行种子），直到 S5 通过且已建立独立登录身份。
- 预发布 PostgreSQL 使用专用登录角色，不使用超级用户运行测试；口令只交给 `.pgpass` 或 `--password` 提示，不写进 JSON、命令参数或聊天。
- 对象存储只写一个专用 prefix；不开启版本控制、保留策略或删除活跃前缀的生命周期规则（见 [多后端设计](STORAGE_BACKENDS.md)）。
- 每个阶段结束时记录保留的测试库与对象前缀，测试完成后由用户决定是否清理。

## 5. 需要用户决定的事项

1. **预发布 PostgreSQL 放在哪里**（已定，2026-10-18）：S1 使用本机 VirtualBox 虚拟机（方案 B），不产生云费用。客户端与服务器共用一台机器，S1/S4 的计时只作参考。生产环境最终用自管云主机还是托管数据库，留到解除云端暂缓时再定，届时在所选环境上简短重跑 S1。
2. **是否解除云端暂缓**（待定）：仅限 S2/S3，使用已报告的杭州 OSS 桶 `<your-oss-bucket>` 还是新桶，以及可接受的费用上限。
3. **生产环境最终是否接触真实业务数据**（待定）：若是，需要先补齐备份恢复演练、独立登录身份与审计，再单独制定数据迁移方案。
4. **负载目标是否改变**（待定）：维持 4,000 行 / 10 Agent，还是给出新的行数、Agent 数和历史长度。
5. **谁可以访问预发布环境**（待定）：决定登录角色的数量与 `pg_hba.conf` 的来源网段。

## 6. 回退与清理

每个阶段只新增测试库、角色和对象前缀，不修改项目库 `agentdb`。回退代码不等于回退数据：迁移（如 `004_merge_batch.sql`）在预发布库上执行前，先确认旧函数定义已随提交保存。清理测试库、角色和 prefix 需要用户确认后执行。

---

<a id="english-version"></a>

# Production Test Plan (Draft)

2026-10-18. **Draft, pending the user's confirmation.** Cloud services are still not verified against real cloud services, and cloud work is still on hold; this document does not change that status, and does not authorize purchasing resources, running `smoke_cloud.py` or using real business data. Items marked "to be decided" need the user's decision; the rest are suggested defaults.

## 1. What needs to be proven

"Production testing" in this project means: in an environment consistent with the expected production shape, using controlled data and load, prove the following four things, leaving reviewable evidence at every phase.

1. **Correctness still holds in the real deployment shape**: joint commits, pinned-version reads, merge conflicts and native constraints, and repeatable migrations behave the same with PostgreSQL on a Linux host and a remote client over TLS as in the local results.
2. **Cloud persistence of management state actually works**: conditional publishing of the task entry point, deduplication of old commands, and hand-off in the two interruption windows hold on real object storage. Currently there are only results from the in-memory model and SDK test doubles.
3. **Failures can be recovered from**: when the database restarts, a backup is restored, the network between the client and the state service is cut, or the object storage returns errors, the system gives a clear result, does not lose committed content, and does not report unknown results as successes.
4. **Under the target load and its growth, costs can be explained**: the target load is a single table of about 4,000 rows with 10 concurrent Agents; additionally measure merge, read and recovery costs after history growth. Record only measured values and the environment; do not write a single result up as a capacity promise.

## 2. Out of scope for this round

- Real business data and production traffic (synthetic data is used throughout until phase 5 passes).
- Multi-region, high-availability failover, sharding and DDL versioning; these are known unimplemented capabilities, and testing only confirms that their absence is not misreported as available.
- A public HTTPS entry point, multi-tenancy and billing.

## 3. Phases and pass criteria

Each phase starts only after the previous phase passes; if any item fails, stop at that phase, locate the problem first and then continue. All results are recorded separately as "in-memory simulation / SDK contract / local PostgreSQL / staging PostgreSQL / real cloud service".

| Phase | Content | Pass criteria | Status |
|---|---|---|---|
| S0 Baseline | Freeze a reviewed commit, with all local suites passing | `run_tests.py`, the state unit tests, `review_local.py` and `check_merge_migration.py` all pass, with the commit hash recorded | Completed: commit 06243d7 |
| S1 Staging PostgreSQL | PostgreSQL 16+ on a Linux host, remote connection over TLS; a dedicated login role (see [Staging connection](STAGING.md)) | `db.py check` shows `ssl=true`, `sslmode=verify-full`; running `run_tests.py`, `review_local.py` and `check_merge_migration.py` as the dedicated role all pass; no superuser needed | **Passed (2026-10-18)**: local VirtualBox VM (option B), all four checks passed, see [S1 results](STAGING_S1_RESULTS.md) |
| S2 Real object storage | Lift the cloud hold, and run `smoke_cloud.py` only against one isolated prefix | Of ten writers only one publish succeeds; retrying an old command returns the original result; hand-off works after interruptions both before and after publishing; a cold read reads only two logical objects; results note the provider, region and date | Waiting for the user to lift the hold |
| S3 State service deployment | Long-running state service on ECS (or another host) + SSH tunnel | After a service restart, existing tasks are read back through the API and versions checked; `maintain_snapshot.py --state-url` completes one hand-off on the staging PostgreSQL | Waiting for S2 |
| S4 Load and growth | Repeat the target load 3 times; then deepen history by an order of magnitude | Record merge-phase time, retry counts, `vc.merge` SQL time and read volume; side by side with the local results; no assertion failures | **Executed (2026-10-18, local VM)**: target load passed; history growth measured cost growth caused by `_file_state` full-history lookback and the snapshot interval; after the fix (the 005 migration and snapshot policy) was merged, re-measured: at 2,000 commits merge 50.7 s → 181 ms, commit 499 ms → 20 ms, see [S4/S5 results section 7](STAGING_S4_S5.md) |
| S5 Failure drills | PostgreSQL restart, backup and restore, network cut, object storage error injection | After recovery `vc.work_tx` is 0; the restored database's head and business table hashes match the backup point; unknown results are all reported as unknown | **Database part passed (2026-10-18)**: backup restore, session termination, normal restart, power-off and network cut, all five passed; the risk exposed by the network cut of a vanished session holding locks has been limited to about 61 s through server timeout settings, and the retest passed, see [S4/S5 results section 7](STAGING_S4_S5.md). Object storage error injection waits for S2/S3 |
| S6 Real Agents | Repeat the registered two-Agent experiment in the staging environment | Register a new round according to the [experiment protocol](../experiments/gpt_repeat/PROTOCOL.md); 0 overwrite events; failed samples kept | Waiting for S1 |

## 4. Data and permission rules

- Use only synthetic data (the 4,000-row seed of `review_local.py` / `run_tests.py`) until S5 passes and a separate login identity has been established.
- The staging PostgreSQL uses a dedicated login role, and tests are not run as a superuser; the password is given only to `.pgpass` or the `--password` prompt, and is not written into JSON, command arguments or chat.
- Object storage writes only to one dedicated prefix; do not enable versioning, retention policies or lifecycle rules that delete an active prefix (see [Multi-backend design](STORAGE_BACKENDS.md)).
- At the end of each phase, record the test databases and object prefixes kept; after testing, the user decides whether to clean them up.

## 5. Items for the user to decide

1. **Where to put the staging PostgreSQL** (decided, 2026-10-18): S1 uses a local VirtualBox VM (option B), with no cloud costs. The client and server share one machine, so S1/S4 timings are for reference only. Whether production finally uses a self-managed cloud host or a managed database is left until the cloud hold is lifted, at which point S1 is briefly rerun in the chosen environment.
2. **Whether to lift the cloud hold** (to be decided): only for S2/S3; whether to use the reported Hangzhou OSS bucket `<your-oss-bucket>` or a new bucket, and the acceptable cost ceiling.
3. **Whether production eventually touches real business data** (to be decided): if so, backup-restore drills, a separate login identity and auditing must be completed first, and a data migration plan drawn up separately.
4. **Whether the load target changes** (to be decided): keep 4,000 rows / 10 Agents, or give new numbers of rows, Agents and history length.
5. **Who can access the staging environment** (to be decided): decide the number of login roles and the source network ranges in `pg_hba.conf`.

## 6. Rollback and cleanup

Each phase only adds test databases, roles and object prefixes, and does not modify the project database `agentdb`. Rolling back code is not rolling back data: before a migration (such as `004_merge_batch.sql`) is run on the staging database, first confirm that the old function definitions have been saved with a commit. Cleaning up test databases, roles and prefixes is done only after the user confirms.
