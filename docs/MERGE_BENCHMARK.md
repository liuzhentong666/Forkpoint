# 十个分支合入 main：耗时分解

日期：2026-10-17。**仅为本机合成负载**：现有 Ubuntu WSL PostgreSQL 上 10 个脚本写入者，不是大模型 Agent，不代表生产容量，未访问任何云服务。这次测量时，`vc.merge` 与版本引擎都没有修改，也没有实现任何修复；之后的实现与复测见“实现长连接后的复测”和“批量解析目标状态后的复测”两节。

入口：[`benchmark_merge.py`](../benchmark_merge.py)。原始结果：[`merge-benchmark-results.json`](../merge-benchmark-results.json)，含每次调用的客户端耗时与服务端时间戳。用 `python benchmark_merge.py --reanalyze` 可在不连数据库的情况下，从原始调用重算派生字段。

## 问题

[本机审查记录第 4 节](LOCAL_REVIEW_RESULTS.md)测得：十个写入者合入 main 的阶段耗时 9.47 s，单次合入中位数 7.0 s，陈旧 head（40001）重试 45 次。需要先弄清时间花在哪里，再在合入队列/退避、优化 `vc.merge` 与降低客户端开销之间做选择。

## 方法

- 只使用一个模板库 `agentdb_test_mergebench_758f297d_tpl`：安装、4,000 行种子、原有 18 项断言（与 `review_local.py` 在 merge_all 前相同），然后从同一基线建立 10 个分支，每个分支改 10 行、写 1 个代码文件。每次运行都用 `CREATE DATABASE … TEMPLATE` 克隆一个新库，因此所有运行的起始历史和 revision 都相同。模板历史：12 个 changeset，4,100 条行变更，11 个路径。
- 每种模式运行 3 次，按轮次交错执行，避免 WSL 预热或漂移集中落在某一模式上。每次运行后核验 4,000 行、100 行改动、3,900 行未变、10 个文件、10 个来源提交均可达、固定基线不变、无遗留工作事务。15 次运行和 3 次 profile 全部通过。
- 计时模式：

| 模式 | 客户端 | 调度 |
|---|---|---|
| `baseline` | 原 `review_local.py` 合入代码（每次调用都起 wsl.exe + psql，角色 `agentdb_client`），不插桩 | 10 个写入者同时开始，均以基线为 expected |
| `spawn_concurrent` | 同上，加插桩 | 同上 |
| `spawn_serialized` | 同上，加插桩 | 逐个合入，expected 使用上一个合入结果（理想队列，无 40001） |
| `persistent_concurrent` | 每个写入者一个常驻 psql 会话，加插桩 | 同时开始，重试规则不变 |
| `persistent_serialized` | 常驻会话，加插桩 | 逐个合入 |
| profile | `persistent_serialized` + 该测试库 `track_functions='all'`，另用 EXPLAIN ANALYZE 测组成查询 | 逐个合入 |

- 插桩：同一个 psql 脚本里，先 `SELECT … FROM vc.branch WHERE name='main' FOR UPDATE` 拿到 main 行锁，再调用未修改的 `vc.merge`，并在各步之间记录 `clock_timestamp()`。由于本事务已持有该锁，`begin_change` 自己的 FOR UPDATE 不再等待，expected 检查保持原样。直接锁 `vc.branch` 需要 owner 权限，所以插桩写入者以 owner 身份连接，不执行 SET ROLE；`vc.merge` 本身是 SECURITY DEFINER，内部执行身份不变。插桩与原客户端结果一致：阶段中位数 14.7 s 对 13.6 s，单次中位数均为 9.8 s，均为 45 次重试。
- 分解（各部分互不重叠）：客户端开销 = 调用墙钟 − 服务端从 start 到最后时间戳的耗时，即进程启动、连接与退出；锁等待 = start → locked；SQL = locked → merged（`vc.merge` 本体）；提交 = merged → committed；重试成本 = 失败尝试的全部时间（其客户端开销与锁等待分开列出）+ 40001 后重读 head 的调用。

## 结果（3 次运行的中位数，括号内为最小–最大）

### 合入阶段墙钟

| 模式 | 阶段耗时 | 单次合入中位数 | 40001 重试 | main 锁被持有的时间占比 | 前一次提交到下一次拿锁的间隔（中位） |
|---|---:|---:|---:|---:|---:|
| baseline | 13.61 s (13.24–15.75) | 9.84 s | 45 | — | — |
| spawn_concurrent | 14.71 s (14.20–15.73) | 9.80 s | 45 | 6.2 % | 1,604 ms |
| spawn_serialized | 2.82 s (2.59–2.89) | 0.27 s | 0 | 29 %（2 次可用运行） | 202 ms |
| persistent_concurrent | 0.92 s (0.90–0.93) | 0.50 s | 45 | 93 % | 4.7 ms |
| persistent_serialized | 0.89 s (0.87–0.91) | 0.09 s | 0 | 97 % | 2.5 ms |

45 = 10×9/2 是确定值：所有写入者都以基线为 expected，每一轮只有拿到锁的那一个成功，其余在拿锁后立即收到 40001。

### 写入者耗时合计（spawn_concurrent，10 个写入者相加，约 91 s）

| 部分 | 合计 | 占比 |
|---|---:|---:|
| 最终尝试的进程/连接开销 | 6.54 s | 7 % |
| 失败尝试的进程/连接开销 | 43.03 s | 47 % |
| 40001 后重读 head（几乎全部是进程启动） | 40.11 s | 44 % |
| 锁等待（最终 0.01 s + 失败 0.40 s） | 0.41 s | 0.4 % |
| `vc.merge` SQL | 0.81 s | 0.9 % |
| COMMIT | 0.03 s | < 0.1 % |

三次运行中，客户端进程开销都占写入者时间的 98.6–98.8 %。从服务端看，main 锁在 13.6 s 内只被持有 0.86 s：每次提交之后，失败者需要先起一个进程重读 head，再起一个进程重试，大约两次启动约 1.6 s，锁在这段时间里空闲。同一次运行的探针显示，单独起一次 `SELECT 1` 中位 272 ms，10 个同时起中位 2,037 ms；常驻会话往返 0.16 ms。

换成常驻会话后，即使重试仍是 45 次，阶段耗时也从 14.7 s 降到 0.92 s。此时 main 锁被持有 93 % 的时间；写入者的时间主要是排队等锁（4.06 s，这是并发写入者在别人执行 SQL 时的等待，不在关键路径上）。45 次重读合计只花 34 ms。

### `vc.merge` 内部（profile，3 次运行，每次 10 次合入）

每次合入的 SQL 约 87 ms（`vc.merge` 合计 835 / 869 / 892 ms）。

| 函数 | 调用 | 合计耗时 | 占 `vc.merge` |
|---|---:|---:|---:|
| `vc.rows_at`（全部） | 140 | 605–653 ms | 71–73 % |
| 其中：为每个改动行解析目标状态（`SELECT … FROM vc.rows_at(ctx.base) WHERE pk=…`，每次合入 10 次） | 100 | 453–496 ms | 54–56 % |
| 其中：`vc.diff_rows` 内部（2 张已登记表 × 2 次） | 40 | 144–157 ms | 约 17 % |
| `vc.diff_rows` 自身 | 20 | 63–65 ms | 7 % |
| `vc._apply_main` + 触发器 | 100 | 69–73 ms | 8 % |
| `finish_change` / `begin_change` | 10 / 10 | 20–33 / 14–19 ms | 4 % |
| `_file_state`（11 个路径 × 3） | 230 | 8–13 ms | 1 % |
| `_ancestors`、`merge_base` | 30 / 10 | 约 10 / 8 ms | 2 % |

EXPLAIN ANALYZE（模板状态和第 10 次合入前状态各测 5 次）：单键 `rows_at` 6.5–9.8 ms，每次都要解析完整的 4,000 行再过滤；一次性取全部 10 个改动键（含 diff_rows）22–27 ms，而 diff_rows 单独为 19–21 ms，即批量解析约 3–7 ms。由此估算，批量解析可以把每次合入的 SQL 从约 87 ms 降到约 40 ms。**这是推算，没有实现，也没有测量。** 这部分成本按「改动行数 × 表行数」增长，行数或单次改动越多，差距越大。

## 建议

1. **先降低客户端进程开销**：每个 Agent 使用常驻连接或连接池，不再每次调用都起 `wsl.exe + psql`。这一项占写入者时间的 98.6 %，也是 main 锁 94 % 时间空闲的原因。只改这一点，不改调度，也不动 SQL，阶段耗时就从 14.7 s 降到 0.92 s（约 16 倍）。
2. **然后在 `vc.merge` 里批量解析目标状态**：客户端开销去掉后，main 锁被持有 93–97 % 的时间，关键路径就是 SQL。其中一半以上是 10 次逐行 `rows_at`。估计阶段耗时可从约 0.9 s 降到约 0.45 s，且收益随表行数和改动行数增长。修改时必须保留冲突判定语义，并用原有 18 项断言和 merge-all 核验覆盖。
3. **暂不需要合入队列或退避**：常驻会话下，理想队列只比 45 次重试快 2 %（0.89 s 对 0.92 s），重读合计 34 ms。锁已经 93 % 繁忙，退避只会增加空闲时间。在仍然每次起进程的客户端上，队列能把 14.7 s 降到 2.8 s，但只是替代价比较高的重试，没有解决根因，效果也不如常驻连接。写入者明显多于 10 个时重试次数按 N(N−1)/2 增长，届时再重新测量。

## 实现长连接后的复测

第 1 项建议已经实现，即 `db.Session`（见 [README](../README.md)）。`vc.merge` 与版本引擎仍未修改，第 2、3 项尚未实现。Session 每次调用都保持单次进程的语义：遇错即停、回滚未结束的事务、重置会话状态与 psql 变量。为此每次调用约多花 1 ms，探针中 `SELECT 1` 往返中位为 1.08 ms，而原型中的裸管道为 0.16 ms。`run_tests.py` 新增的检查逐项核对这些语义，并与单次进程的输出比较。

`benchmark_merge.py` 的常驻会话模式改用 `db.Session` 后，在新模板 `agentdb_test_mergebench_05e9004d_tpl` 上复测（各 3 次，[原始结果](../merge-benchmark-session-results.json)）：

| 模式 | 阶段耗时 | 40001 重试 | main 锁被持有的时间占比 | 交接间隔（中位） |
|---|---:|---:|---:|---:|
| spawn_concurrent（对照） | 15.33 s (14.15–15.49) | 45 | 7.1 % | 1,374 ms |
| `db.Session`，同时开始 | 1.16 s (0.99–1.19) | 45（一次为 44） | 93 % | 7.7 ms |
| `db.Session`，逐个合入 | 0.92 s (0.87–1.13) | 0 | 96 % | 4.1 ms |

`review_local.py` 的写入者现在默认各用一个 Session，`--client spawn` 恢复原做法。一次完整运行（库 `agentdb_test_review_0fbedc9103`）中，十个分支全部合入，核验全部通过：建分支阶段 0.39 s（原 4.97 s），合入阶段 1.19 s（原 9.47 s），端到端 2.30 s（原 14.93 s），陈旧 head 重试仍为 45 次。打开 10 个会话另需 2.07 s，在计时开始之前完成。原纪录保存在 `local-review-results.2026-10-17-spawn.json`。

`agent_lab.py` 每次工具调用都是一个新的 Python 进程，无法跨调用保持 Session。可选的常驻网关 `agent_lab_gateway.py` 替 Agent 持有 Session，默认关闭，尚未用于任何真实 GPT 实验，见 [agent_lab 常驻网关](AGENT_LAB_GATEWAY.md)。

## 批量解析目标状态后的复测（2026-10-18）

第 2 项建议已经实现。`vc.merge` 对每张已登记表只调用一次 `vc.rows_at(ctx.base, t.id)`，与 `diff_rows` 的结果左连接，得到全部改动行的目标状态；该表没有改动时，这次调用由一次性过滤条件跳过。循环按 `diff_rows` 的原输出顺序（WITH ORDINALITY）逐行处理，判定不变：与来源相同的行跳过，否则目标状态不等于共同祖先时报 P0001；写 main 仍逐行 `_apply_main`，写分支时仍以目标状态作为 before_row 暂存；文件冲突、父边与提交信息均未改动。新安装已包含（`sql/001_core.sql`）；旧安装执行 `python db.py install-merge-batch`，即 `sql/004_merge_batch.sql`，以相同定义 CREATE OR REPLACE，并保留所有者、授权与 search_path。

核验（均为本机专用测试库）：

- 改动前先跑基线：`run_tests.py` 18 项与 Session 检查、`review_local.py` 全部通过，用的是旧版 `vc.merge`。
- `run_tests.py` 新增 `merge_resolves_target_state_per_changed_row`，共 13 个合入情形：与来源相同的行被跳过、两边插入相同行、合入分支时 before_row 取目标状态，以及修改/修改、删除/修改、修改/删除、插入/插入、合入分支时冲突、多行冲突时报告哪一行。原 18 项断言未改。
- [`check_merge_migration.py`](../check_merge_migration.py) 用提交 8aeb3d7 的旧 `001_core.sql` 建库（`agentdb_test_mergemig_502ef6643a`），先跑 18 项断言和上述情形，并提交一次合入。迁移连续执行两次后：业务数据与历史表的哈希不变；函数所有者、SECURITY DEFINER、配置与 ACL 不变；13 个情形的规范化输出（行变更、父边、错误码与报错的键）与旧函数逐项相同；18 项断言再次通过；`agentdb_client` 仍可合入。脚本同时确认 001 与 004 中的函数体一致。结果见 [`merge-batch-migration-results.json`](../merge-batch-migration-results.json)：该文件为提交前复核时的重跑（库 `agentdb_test_mergemig_21ad82e33c`），结论与首次运行逐项相同，首次运行的结果文件已被覆盖。另在一个旧版测试库上通过命令行入口 `db.py install-merge-batch` 执行过迁移。
- 改动后 `run_tests.py`（库 `agentdb_test_1ab8001efa3d`）与 `review_local.py`（库 `agentdb_test_review_16e28e9e61`）全部通过。在 `review_local.py` 中，十个分支合入阶段 0.49 s，端到端 1.79 s，陈旧 head 重试 45 次。同一天改动前用旧函数跑的基线为 1.29 s 和 2.65 s。

`python benchmark_merge.py --runs 3 --modes persistent_concurrent persistent_serialized --output merge-benchmark-batched-results.json` 的结果见[原始结果](../merge-benchmark-batched-results.json)。模板库为 `agentdb_test_mergebench_bb4c8a05_tpl`，历史结构与前两次相同；所有运行和 profile 的核验均通过。下表中“改动前”取自上文：SQL 一列来自第一次 profile，其余各列来自 Session 复测。

| 指标 | 改动前 | 批量解析后 |
|---|---:|---:|
| 每次合入的 `vc.merge` SQL（profile，30 次合入的中位数） | 约 87 ms（84–89） | **42.8 ms**（37.6–59.1） |
| profile 中 `vc.rows_at` 调用 / 合计（每次运行 10 次合入） | 140 次 / 605–653 ms | 50 次 / 186–195 ms |
| `db.Session` 同时开始：阶段耗时 | 1.16 s (0.99–1.19) | 0.84 s (0.83–0.88) |
| 同上：写入者 `vc.merge` SQL 合计 | 986 ms | 508 ms |
| 同上：main 锁被持有的时间占比 | 93 % | 77 % |
| `db.Session` 逐个合入：阶段耗时 | 0.92 s (0.87–1.13) | 0.66 s (0.54–0.79) |
| 同上：单次合入中位数 | — | 64 ms |
| 40001 重试（同时开始） | 45 | 45 |

每次合入的 SQL 降到约 43 ms，与先前约 40 ms 的估算相符。`rows_at` 每次合入调用 5 次：`diff_rows` 内部为 2 张表 × 2 次，再加 products 的目标状态 1 次；orders 表没有改动，所以跳过。剩下的 SQL 时间主要是 `diff_rows`（约 19 ms/次合入）与逐行 `_apply_main`（约 5 ms）。阶段耗时只降了约 28 %，没有减半：SQL 缩短后，客户端往返、COMMIT 和锁交接在关键路径上的占比上升，锁占用比也降到 77–83 %。再往下优化，就要让 `diff_rows` 少回放全表（例如合入时把基线与来源的 rows_at 合并成一次），或者减少每次合入的往返；这两项都未实现。

本次结果来自另一个时段，WSL 上的时间波动较大（见下节）；看比例比看绝对值更可靠。负载与前两次相同：4,000 行，10 个分支，每个分支改 10 行、写 1 个文件。目标状态解析原先按“改动行数 × 表行数”增长，现在每张表只回放一次。行数更多或单次改动更多时，差距会更大，但这一点没有测量。

## 环境与注意事项

- PostgreSQL 16.15（Ubuntu 24.04，WSL 2.6.3.0，内核 6.6.87.2-1），unix socket；`shared_buffers` 128 MB，`synchronous_commit=on`，`fsync=on`，READ COMMITTED。客户端为 Windows 11（26200），Python 3.10.11，22 个逻辑 CPU。服务未被启停或替换，`agentdb` 项目库未被写入。
- 进程启动耗时在不同时段波动很大：同一天较早的冒烟运行中，10 个并发起进程的中位约 0.6 s，正式运行时为 2.0 s；baseline 也因此比第 4 节记录的 9.47 s 慢（13.6 s）。结论看比例，不看绝对值。
- 服务端时间戳来自 WSL 客户机时钟，墙钟来自 Windows `perf_counter`，两者通常相差约 ±2 ms，某次运行偏差达约 4 %。因此常驻会话下的客户端开销低于分辨率，表现为接近 0 甚至略负，实际往返以直接测得的 0.16 ms 为准。spawn_serialized 第 1 次运行中发生过约 −0.95 s 的时钟跳变：受影响的 1 次调用记为 `clock_step_unattributed`，该次运行的锁时间线不纳入汇总。
- 模板只包含种子和原有 18 项断言产生的历史，不含 `review_local.py` 维护检查建立的侧分支。分支提案逐个建立，而原实验是并发建立。历史更深、路径更多时，`_file_state` 与 `rows_at` 回溯的成本会变化。
- 常驻会话模式的阶段耗时从所有会话建立之后开始计时，不含建立会话：每个会话建立中位约 150–165 ms，10 个依次建立合计约 1.6 s（见结果 JSON 的 `session_open_ms`）。这对应连接建立一次、之后反复复用的连接池用法；若每个 Agent 只合入一次就断开，这笔开销不能省略。
- `track_functions` 本身有开销：profile 阶段 0.92 s，同等的未开启运行为 0.89 s。组成查询的 EXPLAIN 是在 `vc.merge` 外部以只读方式执行的等价查询。
- 保留的测试库前缀为 `agentdb_test_mergebench_758f297d_`（模板、15 次运行、3 次 profile），名单见结果 JSON 的 `databases_retained`。另外保留了一次冒烟运行的 `agentdb_test_mergebench_827453e5_*`，它不计入结果。
