中文 | [English](#english-version)

# S4 负载与增长、S5 故障演练（本机虚拟机）

2026-10-18。[生产环境测试计划](PRODUCTION_TEST_PLAN.md) 的 S4、S5 阶段，在 [S1](STAGING_S1_RESULTS.md) 所用的 VirtualBox 虚拟机 `agent-db`（Ubuntu 24.04.5、PostgreSQL 16.15）上进行，客户端为 Windows 原生 psql，经 TLS `verify-full` 以非超级用户 `agentdb_staging` 连接。**未使用任何云服务**；对象存储（S2）仍未经真实验证。

入口：[staging_drills.py](../staging_drills.py)（`growth`、`backup`、`restart`、`crash`、`netcut`、`kill`）与已有的 [benchmark_merge.py](../benchmark_merge.py)。每项在服务器上新建自己的 `agentdb_test_*` 库，原始结果在 [staging-results/](../staging-results/)。涉及断电、断网与重启的演练通过本机 `VBoxManage` 控制虚拟机，不需要虚拟机的 shell 或口令。

**结论**：S5 的五项演练全部通过；S4 的目标负载通过，但历史增长测出两处随历史长度增长的成本（第 2 节），以及一处运维风险（第 4 节），需要在扩大规模前处理；第 4 节的服务器与客户端超时已写入脚本，待用户在虚拟机上应用并重跑断网演练。客户端与服务器共用一台笔记本，所有计时只作相对比较。

## 1. S4a 目标负载：十个写入者合入 main

`benchmark_merge.py --runs 3 --modes baseline persistent_concurrent --profile-runs 0`（profile 需要超级用户设置 `track_functions`，此处略过）。[原始结果](../staging-results/merge-benchmark.json)

| 客户端 | 合入阶段（中位，范围） | 单次合入中位 | 陈旧 head 重试 |
|---|---:|---:|---:|
| 每次调用启动 psql 进程 | 8.87 s（8.44–9.01） | 6.15 s | 45 |
| 每个写入者一个长连接 | 2.11 s（1.94–2.63） | 1.32 s | 45（一次 43） |

6 次运行全部核验通过（4,000 行、100 行改动、10 个文件、无遗留事务）。长连接模式下 main 锁被持有约 70% 的时间，写入者主要在排队等锁；经 TLS 到虚拟机的一次会话往返约 8 ms（本机 WSL 约 0.16 ms），启动一次 psql 并登录约 138 ms，10 个同时启动约 638 ms。

## 2. S4b 历史增长

每个增长提交在 main 上改 10 行商品并写 50 个文件之一；seed 提交已物化快照。读取为 `EXPLAIN ANALYZE` 服务器执行时间（5 次中位），合入与提交为长连接上的客户端墙钟。

[原始结果](../staging-results/growth-before.json)（库 `agentdb_test_growth_c0d98c8c27`，11 分钟完成，计算机保持唤醒）

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
| 断网 | 会话持有 main 锁（`begin_change` 后未提交），拔掉虚拟机仅主机网卡 20 s，期间杀掉其客户端 | **通过，并暴露风险**：断网期间新连接 5.1 s 后明确报 `timeout expired`；恢复网络后原会话仍以 “idle in transaction” 持有 main 锁，120 s 观察期内一直存在，其他写入者均 `lock_timeout`；所有者 `pg_terminate_backend` 后恢复，head 未变、无遗留事务 | [netcut-defaults.json](../staging-results/netcut-defaults.json) |

## 4. 风险：失联客户端的会话长时间持有锁

服务器使用 PostgreSQL 默认设置：`tcp_keepalives_idle` 7200 s、间隔 75 s、9 次，`idle_in_transaction_session_timeout`、`client_connection_check_interval`、`lock_timeout` 均为 0。客户端在事务中失联（断网、主机睡眠、进程被杀而关闭报文未到达）时，服务器约 **2 小时 11 分钟**后才判定连接失效；在此之前该会话持有的锁一直有效。持有 main 锁时，所有合入都会排队或超时。

这一情况在本次测试中真实发生过一次：首次增长测试期间笔记本进入 Modern Standby（Windows 事件日志 04:25:00–05:07:34），恢复后客户端 psql 已退出，服务器上的会话仍处于 “idle in transaction”，由所有者手动终止。断网演练随后按设计复现了同一现象（[记录](../staging-results/netcut-defaults.json)）。

**处理（2026-10-18，已写入脚本，尚未应用到虚拟机）**：服务器端由 [configure_postgres.sh](../deploy/staging/configure_postgres.sh) 写入 `conf.d/agentdb-staging.conf`；客户端在合入时设置 `lock_timeout`。修改服务器配置由用户执行，步骤见 [本机虚拟机步骤](STAGING_VM.md#4a-给已配置的虚拟机加上失联客户端设置用户操作)。应用并重跑断网演练之前，第 3 节的断网结果仍代表虚拟机的实际状态。

### 事务内最长的合法空闲

`idle_in_transaction_session_timeout` 只计算事务打开、服务器在等客户端下一条语句的时间（“idle in transaction”），不计算语句执行时间。逐个检查了会在事务中停顿的客户端：

- [run_tests.py](../run_tests.py)、[review_local.py](../review_local.py)、[agent_lab.py](../agent_lab.py)（含网关）、`staging_drills.py` 的 growth/crash/restart/backup：每个 `BEGIN … COMMIT` 都作为一个 psql 脚本整体发送（`Connection.execute` 一次写入标准输入，`db.Session` 用 `\i` 执行）。psql 收到上一条结果就发送下一条，因此服务器看到的事务内空闲约为一次往返：本机 WSL 约 0.16 ms，经 TLS 到虚拟机约 8 ms（第 1 节）。没有脚本在事务中等待模型输出、用户输入或其他进程。
- `agent_lab.py` 的每次工具调用是一个完整事务；模型思考时间落在两个事务之间。网关池中的 Session 每次调用后回滚并 `DISCARD ALL`，空闲时状态是 “idle”，不在事务内。
- 有意的停顿只在演练里：kill 的受害会话在所有者启动 psql 查询并终止它期间空闲，不到 1 s；netcut 的持锁会话从 `begin_change` 到断网要经过 3 s 的 `lock_timeout` 探测、psql 启动和 VBoxManage，约 4 s。`review_local.py` 新增的锁超时检查用 `pg_sleep` 持锁，状态是 “active”，不受此设置影响。
- 长语句（第 2 节中 2,000 个提交时一次合入 50.7 s）处于 “active”，不受此设置影响。`pg_dump`/`pg_restore` 会为自己的会话把 `idle_in_transaction_session_timeout` 设为 0，备份演练不受影响。

### 取值

| 设置 | 值 | 作用 | 依据 |
|---|---|---|---|
| `idle_in_transaction_session_timeout` | 60 s | 事务内空闲 60 s 的会话被服务器结束（FATAL 25P03），事务回滚，main 锁释放；与网络是否恢复无关 | 合法的最长空闲约 4 s（netcut 持锁者），常规约一次往返；60 s 留出 15 倍余量，仍把 main 被锁的时间从约 2 h 11 min 降到约 1 min。人工交互式事务需要更久时可在会话中 `SET idle_in_transaction_session_timeout = 0` |
| `tcp_keepalives_idle` / `_interval` / `_count` | 60 s / 10 s / 6 | 连接 60 s 无流量后开始探测。对端主机在线但连接已不存在时，第一个探测就收到 RST；对端完全静默（断网、睡眠）时 60 + 6 × 10 = 120 s 后断开 | 活着的客户端即使进程空闲，其操作系统也会应答探测，不会被误断（网关池中的空闲 Session 同样）。代价：断网超过约 2 分钟时空闲连接会被断开，Session 下一次调用失败一次，之后重新连接（现有行为） |
| `tcp_user_timeout` | 120 s | 已发出的数据 120 s 未被确认就断开 | keepalive 只在没有未确认数据时探测；没有这一项，向失联客户端发送数据的连接要等重传上限（Linux 默认约 15 分钟）。与 keepalive 的 120 s 一致 |
| `client_connection_check_interval` | 10 s | 语句执行期间每 10 s 检查客户端是否还在，不在就取消语句 | 客户端已关闭（FIN/RST 已到达）时 10 s 内取消；对端静默时先由 keepalive 判定失效（约 120 s），再加最多 10 s。检查是一次 `poll` 调用，开销可忽略；该功能依赖操作系统支持（Linux、macOS、BSD 等），虚拟机的 Ubuntu 满足 |

客户端：`review_local.py` 的 `merge_all` 与 `agent_lab.py merge` 在合入事务内执行 `SET LOCAL lock_timeout = '10s'`。合法的等锁时间是排队深度乘以每次合入持锁时间：第 1 节十个写入者经 TLS 到虚拟机时，每次合入持锁约 0.15 s（合入阶段 2.11 s 中约 70%，共 10 次），最坏约排在 9 个之后，约 1.4 s；10 s 约为 7 倍。它短于服务器的 60 s，因此排在失联客户端后面的合入在 10 s 内以 `55P03` 失败，而不是一直等。

锁超时单独报告，不当作语义冲突：main 不变，同一提案可以再试。`merge_all` 把它计入 `lock_timeout_retries`（与 `stale_head_retries` 分开，共用 30 次上限）后重读 head 重试；`agent_lab.py` 返回 `{"error": "lock_timeout", "sqlstate": "55P03"}`（退出码 2），与 `stale_head`（40001）、`merge_conflict`（P0001）并列。`review_local.py` 新增一项真实 PostgreSQL 检查：另一会话持有 main 锁时，以 1 s 锁超时合入，约 1.1 s 后得到 `lock_timeout`，head 不变；锁释放后同一提案正常合入。限制：历史很长时（第 2 节，1,000 个提交时一次合入 15 s），合法排队也可能超过 10 s，此时会出现锁超时重试；在第 2 节的成本问题解决前，30 次上限可能需要放宽。`benchmark_merge.py` 未改动，以保持与已有计时可比。

### 设置生效后的上限

| 客户端失联时所处的状态 | main 锁最长被占用 |
|---|---|
| 事务内空闲（断网演练、笔记本睡眠） | 60 s（`idle_in_transaction_session_timeout`） |
| 语句执行中，客户端连接已关闭 | 约 10 s（`client_connection_check_interval`） |
| 语句执行中，对端完全静默 | 约 130 s（keepalive 120 s + 检查 10 s），或语句结束后再空闲 60 s，取先到者 |
| 不在事务中 | 不持锁；连接约 120 s 后被清理 |

### 断网演练的通过条件

应用设置后，在 Windows 中运行（断网 20 s，与第 3 节相同；`--seconds` 的默认值是 10）：

```console
.\.venv-cloud\Scripts\python.exe staging_drills.py netcut --config connection.staging.local.json --seconds 20 --expect-gone-within 75
```

通过条件：`passed` 为 true，即在原有条件（清理后能取得 main 锁、head 未变、无遗留事务）之外，服务器在持锁会话最后一条语句之后 75 s 内自行结束该会话（`main_locked_for_s` ≤ 75，按服务器时钟计算），不需要所有者 `pg_terminate_backend`（`terminated_by_owner` 为 null）。75 s = 60 s 超时 + 最多约 1.5 s 的 1 s 轮询粒度 + 共用一台笔记本时的调度余量。轮询从网络恢复时开始；若首次连上服务器时会话已不存在，`main_locked_for_s` 只是上限，此时若已超过 75 s，则 `bound_observable` 为 false、`bound_met` 为 null（无法判断，不算通过）。

预期结果：

| 字段 | 预期 |
|---|---|
| `server_settings` | `idle_in_transaction_session_timeout` 60000ms、`tcp_keepalives_idle` 60s、`tcp_keepalives_interval` 10s、`tcp_keepalives_count` 6、`tcp_user_timeout` 120000ms、`client_connection_check_interval` 10000ms、`lock_timeout` 0ms（服务器默认值不变，只有合入事务自己设置） |
| `probe_while_lock_held` | `lock_timeout` |
| `first_reachable_poll_after_s` | 网络恢复后首次连上服务器的时间（2026-10-18 复测约 37 s） |
| `connect_during_cut` | 约 5 s 后 `timeout expired` |
| `main_locked_for_s` | 约 60–62 s；`stale_session_gone_after_s` 约 35 s（从网络恢复起算） |
| `terminated_by_owner` | null；`probe_after_cleanup` 为 `acquired`；`bound_observable` 与 `bound_met` 为 true |

断网 20 s 时，60 s 处 `idle_in_transaction_session_timeout` 到期，同时第一个 keepalive 探测也会因网络已恢复而收到 RST，两者几乎同时，结果文件不区分是哪一个结束了会话（虚拟机的 PostgreSQL 日志会记录 “terminating connection due to idle-in-transaction timeout”）。要单独验证断网期间也能释放，可再运行 `--seconds 90 --expect-gone-within 110`：这时 keepalive 收不到应答，只有空闲超时能在 60 s 处结束会话；`gone_at_first_poll` 为 true，`main_locked_for_s` 只是上限（断网 90 s 加持锁前后的约 4 s 和网络恢复后的连接检查，约 95–100 s）。`--expect-gone-within` 必须大于 `--seconds`，否则脚本拒绝运行，因为断网期间无法观察。不加该参数时，演练与原来一样只记录结果，`passed` 不检查上限。

新结果写入 `staging-results/netcut.json`；默认设置下的结果已改名为 [netcut-defaults.json](../staging-results/netcut-defaults.json)，不会被覆盖。演练逻辑已在本机用模拟的服务器和时钟检查过（60 s 释放时通过；默认设置并加 `--expect-gone-within` 时因需要手动终止而失败；不加参数时与原来一样通过），**尚未在虚拟机上运行**。

## 7. 修复后在虚拟机上复测（2026-10-18）

第 2 节与第 4 节的两个问题分别由 `sql/005_bounded_history.sql`（[历史增长修复](HISTORY_GROWTH.md)）和 `configure_postgres.sh` 中的失联客户端设置处理。两项合入 main 后，在同一台虚拟机上重跑：

### 历史增长

新建测试库安装 001–005，因此包含修复。同一命令 `staging_drills.py growth --levels 0 100 1000 2000`，第二组加 `--snapshot-interval 100`（每 10 次提交后按 `vc.snapshot_due` 物化到期快照）。每组约 1 分钟完成（修复前 11 分钟）。

| main 上 2,000 个提交时 | 修复前 | 仅修复 | 修复 + 每 100 个提交物化快照 |
|---|---:|---:|---:|
| 一次 10 行合入 | 50.7 s | 639 ms | 181 ms |
| 一次提交 | 499 ms | 18 ms | 20 ms |
| head 全表读取 | 1,205 ms | 180 ms | 24 ms |
| 库大小 | 26 MB | 29 MB | 53 MB |

- 仅修复时提交成本不再随历史增长（各级约 20 ms），合入在 2,000 个提交时降到修复前的约 1/80；合入与读取仍随“距上一个快照的提交数”缓慢增长。
- 加上快照策略后，合入（124–181 ms）、提交与 head 读取（15–24 ms）从 0 到 2,000 个提交基本持平；每次物化约 165 ms，约每 100 个提交一次。
- 代价是存储：每个快照保存一份 4,000 行的整表，2,000 个提交时库大小约为不物化时的 1.8 倍。快照的保留与回收尚未设计（README 当前限制第 9 条“版本保留/回收”）。
- 原始结果：[修复前](../staging-results/growth-before.json)、[仅修复](../staging-results/growth-after.json)、[修复 + 快照策略](../staging-results/growth-after-policy100.json)。

#### 合入的祖先遍历与代数（generation numbers）

快照策略之后，合入中仍随历史增长的是 `vc._ancestors` 与 `vc._merge_base` 对目标 head 全部可达 changeset 的两次遍历。在同一虚拟机上、main 可达 2,026 个 changeset 时，一次完整遍历约 6.8 ms，merge_base 约 16 ms，约占 181 ms 合入的 10%，每增加 1,000 个可达 changeset 约多 8 ms（该次测量由协调会话完成，原始记录未入库）。

结论：现在不改用代数索引。约 20,000 个可达 changeset（按上述增长线性外推，两次遍历合计约 0.17 s）或合入中位耗时超过约 0.5 s 时再改。管理 Agent 通过 `SnapshotMaintenance.check_history`（只读的 `vc.history_health`，`sql/006_history_health.sql`）检查；越线时任务的 next_action 为 “merge history needs generation numbers (see docs/STAGING_S4_S5.md)”，见 [管理 Agent 协议](MANAGER_AGENT_PROTOCOL.md#历史增长检查)。改造将是单独的版本引擎变更，需按 [历史增长](HISTORY_GROWTH.md) 的方式迁移并核验。

### 断网

用户在虚拟机上按 [STAGING_VM.md 4a 节](STAGING_VM.md) 重新执行 `configure_postgres.sh`（保留已安装证书）后，运行 `staging_drills.py netcut --seconds 20 --expect-gone-within 75`。[原始结果](../staging-results/netcut.json)

| | 默认设置（[netcut-defaults.json](../staging-results/netcut-defaults.json)） | 新设置 |
|---|---|---|
| 演练记录的服务器设置 | keepalive 7200 s，各项超时为 0 | `idle_in_transaction_session_timeout` 60 s，keepalive 60 s 后 6 次、间隔 10 s，`tcp_user_timeout` 120 s，`client_connection_check_interval` 10 s |
| 失联会话持有 main 锁 | 观察 120 s 仍在，按默认设置约 2 小时 11 分钟 | **60.7 s 后由服务器自行结束**（自持锁会话最后一条语句起算，含最多 1 s 轮询误差） |
| 需要所有者手动 `pg_terminate_backend` | 是 | **否** |
| 结果 | 通过（暴露风险） | **通过**：在 75 s 上限内释放；head 未变，无遗留事务，锁释放后其他会话立即取得 main 锁 |

断网期间新连接仍在 5.1 s 后明确报 `timeout expired`。

代码审查后，`idle_in_transaction_session_timeout` 从集群级 `conf.d` 改为只设在 `agentdb_staging` 角色上（[角色脚本](../deploy/staging/create_staging_role.sql)），管理员会话恢复默认值 0。用户在虚拟机上重新执行 `configure_postgres.sh` 后核对：`postgres` 会话为 `0`，`agentdb_staging` 会话为 `1min`。同一演练复测通过：服务器在 60.5 s 后自行结束失联会话并释放 main 锁（服务器时钟计算；网络恢复 36.8 s 后首次连上服务器，此时会话已不存在，60.5 s 为上限，仍在 75 s 内），无需手动终止，head 未变，无遗留事务。[原始结果](../staging-results/netcut-role-timeout.json)其余 S5 演练（备份恢复、会话终止、重启、断电）未受这两项修改影响，本次未重跑。

## 5. 环境问题与处理

- **VirtualBox 运行在 Hyper-V 平台之上**（WSL2 已启用）：带内存的在线快照卡在 `livesnapshotting` 状态 30 分钟以上，内存文件始终为 0 字节，期间虚拟机冻结；结束虚拟机进程后 VirtualBox 回滚了该快照。之后改为关机状态下拍快照（`s1-configured`，瞬间完成）。启动时曾有一次停在 initramfs “Loading essential drivers”，重置后正常；`staging_drills.py` 在 200 s 未就绪时自动重置一次，本次演练中未再触发。
- **笔记本睡眠**会暂停虚拟机和客户端，使长时间测试失效。之后通过应用保持唤醒（仅防止空闲睡眠；合上屏幕仍会睡眠）。
- `db.Session` 超时时的报错写作 “timed out after 0 seconds”，秒数不准确，只影响错误信息。

## 6. 保留与清理

虚拟机上保留了各演练的测试库（名称见各结果文件）与两个快照：`clean-postgres`（配置前）和 `s1-configured`（S1 完成并正常关机后，S4/S5 的重置点）。`<VM folder>\Snapshots` 中有一个 2 MB 的孤立差分盘 `{81d5f3fc-…}.vdi`，来自被回滚的在线快照。是否清理由用户决定。

---

<a id="english-version"></a>

# S4 Load and Growth, S5 Failure Drills (Local Virtual Machine)

2026-10-18. Phases S4 and S5 of the [Production test plan](PRODUCTION_TEST_PLAN.md), carried out on the VirtualBox virtual machine `agent-db` used for [S1](STAGING_S1_RESULTS.md) (Ubuntu 24.04.5, PostgreSQL 16.15); the client is the native Windows psql, connecting over TLS `verify-full` as the non-superuser `agentdb_staging`. **No cloud services were used**; object storage (S2) is still not verified against real cloud services.

Entry points: [staging_drills.py](../staging_drills.py) (`growth`, `backup`, `restart`, `crash`, `netcut`, `kill`) and the existing [benchmark_merge.py](../benchmark_merge.py). Each item creates its own `agentdb_test_*` database on the server, and the raw results are in [staging-results/](../staging-results/). Drills involving power-off, network cuts and restarts control the VM through the local `VBoxManage`, without needing the VM's shell or password.

**Conclusion**: all five S5 drills passed; S4's target load passed, but history growth revealed two costs that grow with history length (section 2), as well as one operational risk (section 4), which need to be dealt with before scaling up; the server and client timeouts of section 4 have been written into the scripts, waiting for the user to apply them on the VM and rerun the network-cut drill. The client and server share one laptop, so all timings are for relative comparison only.

## 1. S4a Target load: ten writers merging into main

`benchmark_merge.py --runs 3 --modes baseline persistent_concurrent --profile-runs 0` (profiling needs a superuser to set `track_functions`, so it was skipped here). [Raw results](../staging-results/merge-benchmark.json)

| Client | Merge phase (median, range) | Median single merge | Stale-head retries |
|---|---:|---:|---:|
| Start a psql process per call | 8.87 s (8.44–9.01) | 6.15 s | 45 |
| One long-lived connection per writer | 2.11 s (1.94–2.63) | 1.32 s | 45 (43 once) |

All 6 runs passed verification (4,000 rows, 100 rows changed, 10 files, no leftover transactions). In long-lived connection mode the main lock is held about 70% of the time, and the writers mainly queue waiting for the lock; one session round trip over TLS to the VM is about 8 ms (about 0.16 ms on the local WSL), starting psql once and logging in takes about 138 ms, and starting 10 at the same time about 638 ms.

## 2. S4b History growth

Each growth commit changes 10 product rows on main and writes one of 50 files; the seed commit has a materialized snapshot. Reads are `EXPLAIN ANALYZE` server execution times (median of 5), and merges and commits are client wall-clock times on a long-lived connection.

[Raw results](../staging-results/growth-before.json) (database `agentdb_test_growth_c0d98c8c27`, completed in 11 minutes, with the computer kept awake)

| New commits on main | Full-table read (head) | Single-row read (head) | Full-table read after materializing a snapshot | Materialize snapshot | Read seed pinned version | One page of history (20 entries) | One 10-row merge | One commit | Database size |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 10 ms | 11 ms | 9 ms | 2 ms | 12 ms | 0.8 ms | 98 ms | — | 12 MB |
| 100 | 26 ms | 24 ms | 13 ms | 73 ms | 8 ms | 1.2 ms | 372 ms | 20 ms | 13 MB |
| 1,000 | 236 ms | 238 ms | 17 ms | 230 ms | 8 ms | 1.6 ms | **15.3 s** | 156 ms | 20 MB |
| 2,000 | **1,205 ms** | 658 ms | 12 ms | 786 ms | 9 ms | 1.1 ms | **50.7 s** | **499 ms** | 26 MB |

- Not growing with history: reading a materialized pinned version (seed, about 10 ms), reads after materializing a snapshot (9–17 ms), history pagination (about 1 ms).
- Growing with history: head reads (1.2 s at 2,000), commits (20 ms → 499 ms), merges (98 ms → 50.7 s, increasing 3.3× from 1,000 to 2,000, more than linear). Single-row reads are as slow as full-table reads, because `rows_at` first computes the state of the whole table and then filters.
- The first run (interrupted by the laptop sleeping, see section 4) measured a consistent trend at 0/100/1,000 commits: merges 93 ms / 378 ms / 19.6 s, head reads 16 / 23 / 457 ms ([record](../staging-results/growth-2350-percall.json)). The difference between the two runs shows that single timings fluctuate considerably, but the orders of magnitude are the same.
- Under the target load (a single table of 4,000 rows), after the main branch accumulates about 1,000 commits, one merge exceeds 15 s; estimating that 10 Agents each commit several times a day, this could be reached within a few weeks. This is a problem that must be dealt with before wider use.

### Cost sources (measured directly on the 2,350-commit database)

[Raw record](../staging-results/growth-2350-percall.json)

| Operation | Time |
|---|---:|
| `vc._file_state(head, path)`, growth file | 761 ms |
| `vc._file_state(head, path)`, file that only appeared in the seed | 710 ms |
| `vc.rows_at(head, products)` full-table read | 871 ms |
| `vc._ancestors(head)` | 530 ms |
| One commit: 10 rows + `write_file` (rolled back) | 753 ms |
| The same commit without writing a file (rolled back) | 41 ms |

- The recursive CTE of **`vc._file_state`** ([001_core.sql](../sql/001_core.sql)) walks the first-parent chain all the way to the root, then joins with `path_change` and takes the most recent entry; it does not stop early even after finding the path. It is called once per `write_file`, and `vc.merge` calls it three times per registered path, so the costs of both commits and merges are proportional to the **whole length of history**, regardless of whether the file was changed recently.
- **`vc.rows_at`** replays along the first-parent chain back to the most recent snapshot. The more commits after the snapshot, the slower the read; immediately after materializing a snapshot it is back to about 10 ms. There is currently no automatic materialization.
- **`vc.merge`** also has to traverse all ancestors (`merge_base`, `_ancestors`). The three together make one 10-row merge take about 20 s at 1,000 commits.

This is consistent with items 5 and 6 of the README's "Current limitations", now with concrete numbers. Directions for improvement (not yet implemented; they are behavior-preserving changes to the version engine that need their own project and coverage by the existing assertions): make `_file_state` stop walking back after finding the path, or maintain a per-snapshot file list; materialize snapshots periodically by number of commits or read cost (which the existing maintenance executor could take on); cache the ancestor set during merges.

Follow-up (2026-10-18, local): the three items above have been implemented on the branch `perf/history-bounded-costs`, with the migration as `sql/005_bounded_history.sql`. It was also found that the main cause was that every step of the recursive traversal did a full-table scan of `vc.parent`. For the before/after numbers see [History growth](HISTORY_GROWTH.md); it has not yet been rerun on the staging VM.

## 3. S5 Failure drills

| Drill | Method | Result | Raw record |
|---|---|---|---|
| Logical backup and restore | As `agentdb_staging` over TLS, `pg_dump -Fc` of the 2,350-commit database (1.0 MB, 2.4 s), restored into a new database (2.8 s) | **Passed**: all 13 fingerprints (head, the various history counts and hashes, snapshots, 29 functions, `agentdb_client` grants) match; the restored database's main tables and version state match; after restoring, commits work, and `agentdb_client` can read the new head | [backup.json](../staging-results/backup.json) |
| Session terminated | After a session staged row and file changes on main and before `finish_change`, the owner ran `pg_terminate_backend` on it | **Passed**: the victim session reported `connection to server was lost`; no fingerprint differences, prices of rows 1–10 unchanged; then committed normally | [kill.json](../staging-results/kill.json) |
| Normal restart | ACPI shutdown (11.6 s), then start; connectable after 81.6 s | **Passed**: the postmaster start time changed; no fingerprint differences; main tables and version state match; commits work after the restart | [restart.json](../staging-results/restart.json) |
| Power-off | After 5 writers (1 on main, 4 on branches) committed continuously for 10 s, `VBoxManage controlvm poweroff`; connectable after 61 s, 88 s after the power-off | **Passed**: all 201 acknowledged commits exist and are reachable from their respective heads; `crash_w2` had one in-flight commit that was persisted but whose acknowledgement the client did not receive, and the head is exactly the one after the last acknowledged commit, with complete content; no leftover transactions; main tables and version state match before and after the power-off | [crash.json](../staging-results/crash.json) |
| Network cut | A session holds the main lock (after `begin_change`, not committed), the VM's host-only adapter is unplugged for 20 s, and its client is killed in the meantime | **Passed, and exposed a risk**: during the cut, new connections reported `timeout expired` clearly after 5.1 s; after the network was restored, the original session still held the main lock as "idle in transaction", and was still there throughout the 120 s observation period, with all other writers getting `lock_timeout`; it recovered after the owner ran `pg_terminate_backend`, head unchanged, no leftover transactions | [netcut-defaults.json](../staging-results/netcut-defaults.json) |

## 4. Risk: sessions of vanished clients hold locks for a long time

The server uses PostgreSQL's default settings: `tcp_keepalives_idle` 7200 s, interval 75 s, 9 probes, and `idle_in_transaction_session_timeout`, `client_connection_check_interval` and `lock_timeout` all 0. When a client vanishes mid-transaction (network cut, host sleep, process killed without the close packet arriving), the server only decides the connection is dead after about **2 hours 11 minutes**; until then the locks held by that session stay in effect. While it holds the main lock, all merges queue or time out.

This actually happened once during this testing: during the first growth test the laptop entered Modern Standby (Windows event log 04:25:00–05:07:34); after resuming, the client psql had exited, but the session on the server was still "idle in transaction", and was terminated manually by the owner. The network-cut drill then reproduced the same phenomenon by design ([record](../staging-results/netcut-defaults.json)).

**Handling (2026-10-18, written into the scripts, not yet applied to the VM)**: on the server side [configure_postgres.sh](../deploy/staging/configure_postgres.sh) writes `conf.d/agentdb-staging.conf`; on the client side `lock_timeout` is set during merges. Changing the server configuration is done by the user; for the steps see [Local VM steps](STAGING_VM.md#4a-add-the-vanished-client-settings-to-an-already-configured-vm-user-action). Until they are applied and the network-cut drill is rerun, the network-cut result in section 3 still represents the actual state of the VM.

### Longest legitimate idle time inside a transaction

`idle_in_transaction_session_timeout` only counts the time during which a transaction is open and the server is waiting for the client's next statement ("idle in transaction"), not statement execution time. The clients that might pause inside a transaction were checked one by one:

- [run_tests.py](../run_tests.py), [review_local.py](../review_local.py), [agent_lab.py](../agent_lab.py) (including the gateway), and `staging_drills.py`'s growth/crash/restart/backup: each `BEGIN … COMMIT` is sent as one whole psql script (`Connection.execute` writes it to standard input in one go, and `db.Session` executes it with `\i`). psql sends the next statement as soon as it receives the previous result, so the idle time inside a transaction seen by the server is about one round trip: about 0.16 ms on the local WSL, about 8 ms over TLS to the VM (section 1). No script waits inside a transaction for model output, user input or other processes.
- Each tool call of `agent_lab.py` is one complete transaction; model thinking time falls between two transactions. Sessions in the gateway pool roll back and run `DISCARD ALL` after each call, so their idle state is "idle", not inside a transaction.
- Intentional pauses happen only in the drills: kill's victim session is idle while the owner starts psql to query and terminate it, less than 1 s; netcut's lock-holding session goes through a 3 s `lock_timeout` probe, psql startup and VBoxManage between `begin_change` and the network cut, about 4 s. The new lock-timeout check in `review_local.py` holds the lock with `pg_sleep`, whose state is "active", so it is not affected by this setting.
- Long statements (one merge of 50.7 s at 2,000 commits in section 2) are "active" and not affected by this setting. `pg_dump`/`pg_restore` set `idle_in_transaction_session_timeout` to 0 for their own sessions, so the backup drill is not affected.

### Values

| Setting | Value | Effect | Basis |
|---|---|---|---|
| `idle_in_transaction_session_timeout` | 60 s | A session idle inside a transaction for 60 s is ended by the server (FATAL 25P03), the transaction rolls back, and the main lock is released; independent of whether the network recovers | The longest legitimate idle time is about 4 s (the netcut lock holder), and normally about one round trip; 60 s leaves a 15× margin, and still reduces the time main stays locked from about 2 h 11 min to about 1 min. Interactive manual transactions that need longer can `SET idle_in_transaction_session_timeout = 0` in the session |
| `tcp_keepalives_idle` / `_interval` / `_count` | 60 s / 10 s / 6 | Probing starts after 60 s without traffic on the connection. When the peer host is online but the connection no longer exists, the first probe gets an RST; when the peer is completely silent (network cut, sleep), it disconnects after 60 + 6 × 10 = 120 s | A live client's operating system answers the probes even if the process is idle, so it is not disconnected by mistake (the same for idle Sessions in the gateway pool). Cost: when the network is cut for more than about 2 minutes, idle connections are dropped, and a Session's next call fails once and then reconnects (existing behavior) |
| `tcp_user_timeout` | 120 s | Disconnects when sent data has not been acknowledged for 120 s | keepalive only probes when there is no unacknowledged data; without this item, a connection sending data to a vanished client waits for the retransmission limit (about 15 minutes by default on Linux). Consistent with keepalive's 120 s |
| `client_connection_check_interval` | 10 s | While a statement is executing, checks every 10 s whether the client is still there, and cancels the statement if not | When the client has closed (FIN/RST has arrived), cancels within 10 s; when the peer is silent, keepalive first decides the connection is dead (about 120 s), plus at most 10 s. The check is one `poll` call with negligible overhead; this feature depends on operating system support (Linux, macOS, BSD, etc.), which the VM's Ubuntu satisfies |

Client: `review_local.py`'s `merge_all` and `agent_lab.py merge` run `SET LOCAL lock_timeout = '10s'` inside the merge transaction. The legitimate lock wait time is the queue depth times the lock-holding time per merge: with the ten writers over TLS to the VM in section 1, each merge holds the lock for about 0.15 s (about 70% of the 2.11 s merge phase, over 10 merges), and in the worst case a writer queues behind 9 others, about 1.4 s; 10 s is about 7×. It is shorter than the server's 60 s, so a merge queued behind a vanished client fails within 10 s with `55P03` instead of waiting indefinitely.

Lock timeouts are reported separately and not treated as semantic conflicts: main is unchanged, and the same proposal can be tried again. `merge_all` counts it in `lock_timeout_retries` (separate from `stale_head_retries`, sharing the limit of 30) and then re-reads head and retries; `agent_lab.py` returns `{"error": "lock_timeout", "sqlstate": "55P03"}` (exit code 2), alongside `stale_head` (40001) and `merge_conflict` (P0001). `review_local.py` adds a real PostgreSQL check: while another session holds the main lock, merging with a 1 s lock timeout gets `lock_timeout` after about 1.1 s, with head unchanged; after the lock is released the same proposal merges normally. Limitation: when history is very long (section 2, one merge taking 15 s at 1,000 commits), legitimate queuing can also exceed 10 s, in which case lock-timeout retries will occur; until the cost problem of section 2 is solved, the limit of 30 may need to be relaxed. `benchmark_merge.py` is unchanged, to stay comparable with the existing timings.

### Upper bounds once the settings take effect

| State the client was in when it vanished | Longest time the main lock is held |
|---|---|
| Idle inside a transaction (network-cut drill, laptop sleep) | 60 s (`idle_in_transaction_session_timeout`) |
| Statement executing, client connection closed | About 10 s (`client_connection_check_interval`) |
| Statement executing, peer completely silent | About 130 s (keepalive 120 s + check 10 s), or idle another 60 s after the statement ends, whichever comes first |
| Not in a transaction | No lock held; the connection is cleaned up after about 120 s |

### Pass criteria for the network-cut drill

After applying the settings, run on Windows (20 s network cut, the same as section 3; the default of `--seconds` is 10):

```console
.\.venv-cloud\Scripts\python.exe staging_drills.py netcut --config connection.staging.local.json --seconds 20 --expect-gone-within 75
```

Pass criteria: `passed` is true, i.e. in addition to the original conditions (the main lock can be acquired after cleanup, head unchanged, no leftover transactions), the server ends the lock-holding session on its own within 75 s after that session's last statement (`main_locked_for_s` ≤ 75, measured by the server clock), without the owner needing `pg_terminate_backend` (`terminated_by_owner` is null). 75 s = the 60 s timeout + up to about 1.5 s of 1 s polling granularity + scheduling margin from sharing one laptop. Polling starts when the network is restored; if the session no longer exists when the server is first reached, `main_locked_for_s` is only an upper bound, and if it already exceeds 75 s, `bound_observable` is false and `bound_met` is null (cannot be judged, does not count as passing).

Expected results:

| Field | Expected |
|---|---|
| `server_settings` | `idle_in_transaction_session_timeout` 60000ms, `tcp_keepalives_idle` 60s, `tcp_keepalives_interval` 10s, `tcp_keepalives_count` 6, `tcp_user_timeout` 120000ms, `client_connection_check_interval` 10000ms, `lock_timeout` 0ms (the server default is unchanged; only the merge transaction sets it itself) |
| `probe_while_lock_held` | `lock_timeout` |
| `first_reachable_poll_after_s` | Time until the server is first reached after the network is restored (about 37 s in the 2026-10-18 retest) |
| `connect_during_cut` | `timeout expired` after about 5 s |
| `main_locked_for_s` | About 60–62 s; `stale_session_gone_after_s` about 35 s (counted from network restoration) |
| `terminated_by_owner` | null; `probe_after_cleanup` is `acquired`; `bound_observable` and `bound_met` are true |

With a 20 s network cut, `idle_in_transaction_session_timeout` expires at 60 s, and at the same time the first keepalive probe also gets an RST because the network has already been restored; the two happen almost simultaneously, and the result file does not distinguish which one ended the session (the VM's PostgreSQL log records "terminating connection due to idle-in-transaction timeout"). To verify separately that the lock is also released during a network cut, you can additionally run `--seconds 90 --expect-gone-within 110`: then keepalive gets no answer, and only the idle timeout can end the session at 60 s; `gone_at_first_poll` is true, and `main_locked_for_s` is only an upper bound (the 90 s cut plus about 4 s before and after taking the lock and the connection check after the network is restored, about 95–100 s). `--expect-gone-within` must be greater than `--seconds`, otherwise the script refuses to run, because nothing can be observed during the cut. Without this argument, the drill only records results as before, and `passed` does not check the bound.

New results are written to `staging-results/netcut.json`; the results under the default settings have been renamed to [netcut-defaults.json](../staging-results/netcut-defaults.json) and will not be overwritten. The drill logic has been checked locally with a simulated server and clock (passes when released at 60 s; fails with the default settings plus `--expect-gone-within` because manual termination is needed; passes as before without the argument), **but has not yet been run on the VM**.

## 7. Retest on the VM after the fixes (2026-10-18)

The two problems of sections 2 and 4 are handled respectively by `sql/005_bounded_history.sql` ([History growth fix](HISTORY_GROWTH.md)) and the vanished-client settings in `configure_postgres.sh`. After both were merged into main, they were rerun on the same VM:

### History growth

A new test database installed 001–005, so it includes the fix. The same command `staging_drills.py growth --levels 0 100 1000 2000`, with the second group adding `--snapshot-interval 100` (after every 10 commits, materialize due snapshots according to `vc.snapshot_due`). Each group completed in about 1 minute (11 minutes before the fix).

| At 2,000 commits on main | Before the fix | Fix only | Fix + materialize snapshots every 100 commits |
|---|---:|---:|---:|
| One 10-row merge | 50.7 s | 639 ms | 181 ms |
| One commit | 499 ms | 18 ms | 20 ms |
| head full-table read | 1,205 ms | 180 ms | 24 ms |
| Database size | 26 MB | 29 MB | 53 MB |

- With the fix only, commit cost no longer grows with history (about 20 ms at each level), and the merge at 2,000 commits drops to about 1/80 of the pre-fix value; merges and reads still grow slowly with "the number of commits since the last snapshot".
- With the snapshot policy added, merges (124–181 ms), commits and head reads (15–24 ms) stay essentially flat from 0 to 2,000 commits; each materialization takes about 165 ms, about once every 100 commits.
- The cost is storage: each snapshot saves a whole 4,000-row table, and at 2,000 commits the database is about 1.8× the size without materialization. Snapshot retention and reclamation have not been designed yet (README current limitation 9, "version retention/reclamation").
- Raw results: [before the fix](../staging-results/growth-before.json), [fix only](../staging-results/growth-after.json), [fix + snapshot policy](../staging-results/growth-after-policy100.json).

#### Ancestor traversal in merges and generation numbers

After the snapshot policy, what still grows with history in a merge is the two traversals by `vc._ancestors` and `vc._merge_base` of all changesets reachable from the target head. On the same VM, with 2,026 changesets reachable from main, one full traversal takes about 6.8 ms and merge_base about 16 ms, about 10% of a 181 ms merge, with about 8 ms more per additional 1,000 reachable changesets (this measurement was done by the coordinating session, and the raw record was not committed).

Conclusion: do not switch to a generation-number index now. Change it at about 20,000 reachable changesets (extrapolating linearly from the growth above, about 0.17 s for the two traversals together) or when the median merge time exceeds about 0.5 s. The manager Agent checks through `SnapshotMaintenance.check_history` (the read-only `vc.history_health`, `sql/006_history_health.sql`); when a threshold is crossed, the task's next_action is "merge history needs generation numbers (see docs/STAGING_S4_S5.md)", see [Manager Agent protocol](MANAGER_AGENT_PROTOCOL.md#history-growth-check). The change will be a separate version engine change, to be migrated and verified in the same way as [History growth](HISTORY_GROWTH.md).

### Network cut

After the user re-ran `configure_postgres.sh` on the VM according to [STAGING_VM section 4a](STAGING_VM.md) (keeping the installed certificates), `staging_drills.py netcut --seconds 20 --expect-gone-within 75` was run. [Raw results](../staging-results/netcut.json)

| | Default settings ([netcut-defaults.json](../staging-results/netcut-defaults.json)) | New settings |
|---|---|---|
| Server settings recorded by the drill | keepalive 7200 s, all timeouts 0 | `idle_in_transaction_session_timeout` 60 s, keepalive 6 probes 10 s apart after 60 s, `tcp_user_timeout` 120 s, `client_connection_check_interval` 10 s |
| Vanished session holding the main lock | Still there after 120 s of observation, about 2 hours 11 minutes under the default settings | **Ended by the server on its own after 60.7 s** (counted from the lock-holding session's last statement, including up to 1 s of polling error) |
| Owner needs manual `pg_terminate_backend` | Yes | **No** |
| Result | Passed (exposed a risk) | **Passed**: released within the 75 s bound; head unchanged, no leftover transactions, and after the lock was released other sessions immediately acquired the main lock |

During the network cut, new connections still reported `timeout expired` clearly after 5.1 s.

After code review, `idle_in_transaction_session_timeout` was moved from the cluster-level `conf.d` to being set only on the `agentdb_staging` role ([role script](../deploy/staging/create_staging_role.sql)), and administrator sessions returned to the default value 0. After the user re-ran `configure_postgres.sh` on the VM, it was checked: the `postgres` session shows `0`, and the `agentdb_staging` session shows `1min`. The same drill passed on retest: the server ended the vanished session on its own after 60.5 s and released the main lock (measured by the server clock; the server was first reached 36.8 s after the network was restored, at which point the session no longer existed, so 60.5 s is an upper bound, still within 75 s), with no manual termination needed, head unchanged, and no leftover transactions. [Raw results](../staging-results/netcut-role-timeout.json) The other S5 drills (backup restore, session termination, restart, power-off) are not affected by these two changes and were not rerun this time.

## 5. Environment problems and how they were handled

- **VirtualBox runs on top of the Hyper-V platform** (WSL2 is enabled): a live snapshot with memory got stuck in the `livesnapshotting` state for more than 30 minutes, the memory file stayed at 0 bytes, and the VM was frozen in the meantime; after the VM process was ended, VirtualBox rolled back that snapshot. After that, snapshots were taken in the powered-off state (`s1-configured`, completed instantly). Once, booting stopped at initramfs "Loading essential drivers", and it was fine after a reset; `staging_drills.py` automatically resets once if not ready within 200 s, which was not triggered again during these drills.
- **Laptop sleep** pauses the VM and the client, invalidating long-running tests. After that, the machine was kept awake through an application (this only prevents idle sleep; closing the lid still sleeps).
- When `db.Session` times out, the error message says "timed out after 0 seconds"; the number of seconds is inaccurate, which affects only the error message.

## 6. What is kept and cleanup

The VM keeps the test databases of each drill (names in each result file) and two snapshots: `clean-postgres` (before configuration) and `s1-configured` (after S1 was completed and the VM shut down normally; the reset point for S4/S5). `<VM folder>\Snapshots` contains a 2 MB orphaned differencing disk `{81d5f3fc-…}.vdi`, from the rolled-back live snapshot. Whether to clean up is up to the user.
