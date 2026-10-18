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

### 断网

用户在虚拟机上按 [STAGING_VM.md 4a 节](STAGING_VM.md) 重新执行 `configure_postgres.sh`（保留已安装证书）后，运行 `staging_drills.py netcut --seconds 20 --expect-gone-within 75`。[原始结果](../staging-results/netcut.json)

| | 默认设置（[netcut-defaults.json](../staging-results/netcut-defaults.json)） | 新设置 |
|---|---|---|
| 演练记录的服务器设置 | keepalive 7200 s，各项超时为 0 | `idle_in_transaction_session_timeout` 60 s，keepalive 60 s 后 6 次、间隔 10 s，`tcp_user_timeout` 120 s，`client_connection_check_interval` 10 s |
| 失联会话持有 main 锁 | 观察 120 s 仍在，按默认设置约 2 小时 11 分钟 | **60.7 s 后由服务器自行结束**（自持锁会话最后一条语句起算，含最多 1 s 轮询误差） |
| 需要所有者手动 `pg_terminate_backend` | 是 | **否** |
| 结果 | 通过（暴露风险） | **通过**：在 75 s 上限内释放；head 未变，无遗留事务，锁释放后其他会话立即取得 main 锁 |

断网期间新连接仍在 5.1 s 后明确报 `timeout expired`。其余 S5 演练（备份恢复、会话终止、重启、断电）未受这两项修改影响，本次未重跑。

## 5. 环境问题与处理

- **VirtualBox 运行在 Hyper-V 平台之上**（WSL2 已启用）：带内存的在线快照卡在 `livesnapshotting` 状态 30 分钟以上，内存文件始终为 0 字节，期间虚拟机冻结；结束虚拟机进程后 VirtualBox 回滚了该快照。之后改为关机状态下拍快照（`s1-configured`，瞬间完成）。启动时曾有一次停在 initramfs “Loading essential drivers”，重置后正常；`staging_drills.py` 在 200 s 未就绪时自动重置一次，本次演练中未再触发。
- **笔记本睡眠**会暂停虚拟机和客户端，使长时间测试失效。之后通过应用保持唤醒（仅防止空闲睡眠；合上屏幕仍会睡眠）。
- `db.Session` 超时时的报错写作 “timed out after 0 seconds”，秒数不准确，只影响错误信息。

## 6. 保留与清理

虚拟机上保留了各演练的测试库（名称见各结果文件）与两个快照：`clean-postgres`（配置前）和 `s1-configured`（S1 完成并正常关机后，S4/S5 的重置点）。`E:\agent-db\Snapshots` 中有一个 2 MB 的孤立差分盘 `{81d5f3fc-…}.vdi`，来自被回滚的在线快照。是否清理由用户决定。
