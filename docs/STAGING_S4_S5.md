# S4 负载与增长、S5 故障演练（本机虚拟机）

2026-10-18。[生产环境测试计划](PRODUCTION_TEST_PLAN.md) 的 S4、S5 阶段，在 [S1](STAGING_S1_RESULTS.md) 所用的 VirtualBox 虚拟机 `agent-db`（Ubuntu 24.04.5、PostgreSQL 16.15）上进行，客户端为 Windows 原生 psql，经 TLS `verify-full` 以非超级用户 `agentdb_staging` 连接。**未使用任何云服务**；对象存储（S2）仍未经真实验证。

入口：[staging_drills.py](../staging_drills.py)（`growth`、`backup`、`restart`、`crash`、`netcut`、`kill`）与已有的 [benchmark_merge.py](../benchmark_merge.py)。每项在服务器上新建自己的 `agentdb_test_*` 库，原始结果在 [staging-results/](../staging-results/)。涉及断电、断网与重启的演练通过本机 `VBoxManage` 控制虚拟机，不需要虚拟机的 shell 或口令。

**结论**：S5 的五项演练全部通过；S4 的目标负载通过，但历史增长测出两处随历史长度增长的成本（第 2 节），以及一处运维风险（第 4 节），需要在扩大规模前处理。客户端与服务器共用一台笔记本，所有计时只作相对比较。

## 1. S4a 目标负载：十个写入者合入 main

`benchmark_merge.py --runs 3 --modes baseline persistent_concurrent --profile-runs 0`（profile 需要超级用户设置 `track_functions`，此处略过）。[原始结果](../staging-results/merge-benchmark.json)

| 客户端 | 合入阶段（中位，范围） | 单次合入中位 | 陈旧 head 重试 |
|---|---:|---:|---:|
| 每次调用启动 psql 进程 | 8.87 s（8.44–9.01） | 6.15 s | 45 |
| 每个写入者一个长连接 | 2.11 s（1.94–2.63） | 1.32 s | 45（一次 43） |

6 次运行全部核验通过（4,000 行、100 行改动、10 个文件、无遗留事务）。长连接模式下 main 锁被持有约 70% 的时间，写入者主要在排队等锁；经 TLS 到虚拟机的一次会话往返约 8 ms（本机 WSL 约 0.16 ms），启动一次 psql 并登录约 138 ms，10 个同时启动约 638 ms。

## 2. S4b 历史增长

每个增长提交在 main 上改 10 行商品并写 50 个文件之一；seed 提交已物化快照。读取为 `EXPLAIN ANALYZE` 服务器执行时间（5 次中位），合入与提交为长连接上的客户端墙钟。

[原始结果](../staging-results/growth.json)（库 `agentdb_test_growth_c0d98c8c27`，11 分钟完成，计算机保持唤醒）

| main 上新增提交 | 全表读取（head） | 单行读取（head） | 物化快照后全表读取 | 物化快照 | 读取 seed 固定版本 | 历史一页（20 条） | 一次 10 行合入 | 一次提交 | 库大小 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 10 ms | 11 ms | 9 ms | 2 ms | 12 ms | 0.8 ms | 98 ms | — | 12 MB |
| 100 | 26 ms | 24 ms | 13 ms | 73 ms | 8 ms | 1.2 ms | 372 ms | 20 ms | 13 MB |
| 1,000 | 236 ms | 238 ms | 17 ms | 230 ms | 8 ms | 1.6 ms | **15.3 s** | 156 ms | 20 MB |
| 2,000 | **1,205 ms** | 658 ms | 12 ms | 786 ms | 9 ms | 1.1 ms | **50.7 s** | **499 ms** | 26 MB |

- 不随历史增长的：读取已物化的固定版本（seed，约 10 ms）、物化快照之后的读取（9–17 ms）、历史分页（约 1 ms）。
- 随历史增长的：head 读取（2,000 时 1.2 s）、提交（20 ms → 499 ms）、合入（98 ms → 50.7 s，1,000 到 2,000 时增至 3.3 倍，超过线性）。单行读取与全表读取同样慢，因为 `rows_at` 先算出整张表的状态再过滤。
- 首次运行（被笔记本睡眠中断，见第 4 节）在 0/100/1,000 个提交时测得一致的趋势：合入 93 ms / 378 ms / 19.6 s，head 读取 16 / 23 / 457 ms（[记录](../staging-results/growth-2350-percall.json)）。两次的差异说明单次计时波动较大，但数量级相同。
- 在目标负载（单表 4,000 行）下，主分支积累约 1,000 个提交后，一次合入就超过 15 s；按 10 个 Agent 各自每天提交数次估算，几周内即可达到。这是扩大使用前必须处理的问题。

### 成本来源（在 2,350 个提交的库上直接测量）

[原始记录](../staging-results/growth-2350-percall.json)

| 操作 | 耗时 |
|---|---:|
| `vc._file_state(head, path)`，增长文件 | 761 ms |
| `vc._file_state(head, path)`，只在 seed 中出现过的文件 | 710 ms |
| `vc.rows_at(head, products)` 全表读取 | 871 ms |
| `vc._ancestors(head)` | 530 ms |
| 一次提交：10 行 + `write_file`（回滚） | 753 ms |
| 同样的提交，不写文件（回滚） | 41 ms |

- **`vc._file_state`**（[001_core.sql](../sql/001_core.sql)）的递归 CTE 沿第一父链一直走到根，再与 `path_change` 连接并取最近一条；找到该路径也不会提前停止。每次 `write_file` 调用一次，`vc.merge` 对每个已登记路径调用三次，因此提交与合入的成本都与**全部历史长度**成正比，与文件最近是否改过无关。
- **`vc.rows_at`** 沿第一父链回放到最近的快照。快照之后的提交越多，读取越慢；物化快照后立即回到约 10 ms。目前没有自动物化。
- **`vc.merge`** 还要遍历全部祖先（`merge_base`、`_ancestors`）。三者叠加，使 1,000 个提交时一次 10 行合入约 20 s。

这与 README“当前限制”第 5、6 条一致，现在有了具体数字。改进方向（尚未实现，属于版本引擎的行为保持修改，需要单独立项并用原有断言覆盖）：`_file_state` 在找到路径后停止回溯，或维护按快照的文件清单；按提交数或读取成本定期物化快照（可由现有维护执行器承担）；合入时缓存祖先集合。

后续（2026-10-18，本机）：以上三项已在分支 `perf/history-bounded-costs` 中实现，迁移为 `sql/005_bounded_history.sql`。另查明主因是递归遍历每一步都全表扫描 `vc.parent`。前后数字见 [历史增长](HISTORY_GROWTH.md)；预发布虚拟机上尚未重跑。

## 3. S5 故障演练

| 演练 | 做法 | 结果 | 原始记录 |
|---|---|---|---|
| 逻辑备份与恢复 | 以 `agentdb_staging` 经 TLS 对 2,350 提交的库 `pg_dump -Fc`（1.0 MB，2.4 s），恢复到新库（2.8 s） | **通过**：13 项指纹（head、各类历史计数与哈希、快照、29 个函数、`agentdb_client` 授权）全部一致；恢复库的 main 表与版本状态一致；恢复后可提交，`agentdb_client` 可读到新 head | [backup.json](../staging-results/backup.json) |
| 会话被终止 | 会话在 main 上暂存行与文件改动后、`finish_change` 前被所有者 `pg_terminate_backend` | **通过**：受害会话报 `connection to server was lost`；指纹无差异、1–10 行价格未变；随后正常提交 | [kill.json](../staging-results/kill.json) |
| 正常重启 | ACPI 关机（11.6 s）后启动，81.6 s 可连接 | **通过**：postmaster 启动时间改变；指纹无差异；main 表与版本状态一致；重启后可提交 | [restart.json](../staging-results/restart.json) |
| 断电 | 5 个写入者（main 1 个、分支 4 个）连续提交 10 s 后 `VBoxManage controlvm poweroff`；61 s 后可连接，距断电 88 s | **通过**：已确认的 201 个提交全部存在且可从各自 head 到达；`crash_w2` 有一个在途提交已落盘但客户端未收到确认，head 恰为最后确认提交的下一个，内容完整；无遗留事务；断电前后 main 表与版本状态均一致 | [crash.json](../staging-results/crash.json) |
| 断网 | 会话持有 main 锁（`begin_change` 后未提交），拔掉虚拟机仅主机网卡 20 s，期间杀掉其客户端 | **通过，并暴露风险**：断网期间新连接 5.1 s 后明确报 `timeout expired`；恢复网络后原会话仍以 “idle in transaction” 持有 main 锁，120 s 观察期内一直存在，其他写入者均 `lock_timeout`；所有者 `pg_terminate_backend` 后恢复，head 未变、无遗留事务 | [netcut.json](../staging-results/netcut.json) |

## 4. 风险：失联客户端的会话长时间持有锁

服务器使用 PostgreSQL 默认设置：`tcp_keepalives_idle` 7200 s、间隔 75 s、9 次，`idle_in_transaction_session_timeout`、`client_connection_check_interval`、`lock_timeout` 均为 0。客户端在事务中失联（断网、主机睡眠、进程被杀而关闭报文未到达）时，服务器约 **2 小时 11 分钟**后才判定连接失效；在此之前该会话持有的锁一直有效。持有 main 锁时，所有合入都会排队或超时。

这一情况在本次测试中真实发生过一次：首次增长测试期间笔记本进入 Modern Standby（Windows 事件日志 04:25:00–05:07:34），恢复后客户端 psql 已退出，服务器上的会话仍处于 “idle in transaction”，由所有者手动终止。断网演练随后按设计复现了同一现象。

建议（需要服务器管理员决定，本次未修改服务器配置）：为该集群设置 `idle_in_transaction_session_timeout`（例如 60 s）、较短的 TCP keepalive（例如 idle 60 s、间隔 10 s、6 次）和 `client_connection_check_interval`；客户端在合入前设置 `lock_timeout`。这些设置可以加入 `configure_postgres.sh` 写出的 `conf.d` 文件。

## 5. 环境问题与处理

- **VirtualBox 运行在 Hyper-V 平台之上**（WSL2 已启用）：带内存的在线快照卡在 `livesnapshotting` 状态 30 分钟以上，内存文件始终为 0 字节，期间虚拟机冻结；结束虚拟机进程后 VirtualBox 回滚了该快照。之后改为关机状态下拍快照（`s1-configured`，瞬间完成）。启动时曾有一次停在 initramfs “Loading essential drivers”，重置后正常；`staging_drills.py` 在 200 s 未就绪时自动重置一次，本次演练中未再触发。
- **笔记本睡眠**会暂停虚拟机和客户端，使长时间测试失效。之后通过应用保持唤醒（仅防止空闲睡眠；合上屏幕仍会睡眠）。
- `db.Session` 超时时的报错写作 “timed out after 0 seconds”，秒数不准确，只影响错误信息。

## 6. 保留与清理

虚拟机上保留了各演练的测试库（名称见各结果文件）与两个快照：`clean-postgres`（配置前）和 `s1-configured`（S1 完成并正常关机后，S4/S5 的重置点）。`E:\agent-db\Snapshots` 中有一个 2 MB 的孤立差分盘 `{81d5f3fc-…}.vdi`，来自被回滚的在线快照。是否清理由用户决定。
