中文 | [English](#english-version)

# 十个分支合入 main：耗时分解

日期：2026-10-17。**仅为本机合成负载**：现有 Ubuntu WSL PostgreSQL 上 10 个脚本写入者，不是大模型 Agent，不代表生产容量，未访问任何云服务。这次测量时，`vc.merge` 与版本引擎都没有修改，也没有实现任何修复；之后的实现与复测见“实现长连接后的复测”和“批量解析目标状态后的复测”两节。

入口：[`scripts/benchmark_merge.py`](../scripts/benchmark_merge.py)。原始结果：[`results/merge-benchmark-results.json`](../results/merge-benchmark-results.json)，含每次调用的客户端耗时与服务端时间戳。用 `python scripts/benchmark_merge.py --reanalyze` 可在不连数据库的情况下，从原始调用重算派生字段。

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

`scripts/benchmark_merge.py` 的常驻会话模式改用 `db.Session` 后，在新模板 `agentdb_test_mergebench_05e9004d_tpl` 上复测（各 3 次，[原始结果](../results/merge-benchmark-session-results.json)）：

| 模式 | 阶段耗时 | 40001 重试 | main 锁被持有的时间占比 | 交接间隔（中位） |
|---|---:|---:|---:|---:|
| spawn_concurrent（对照） | 15.33 s (14.15–15.49) | 45 | 7.1 % | 1,374 ms |
| `db.Session`，同时开始 | 1.16 s (0.99–1.19) | 45（一次为 44） | 93 % | 7.7 ms |
| `db.Session`，逐个合入 | 0.92 s (0.87–1.13) | 0 | 96 % | 4.1 ms |

`review_local.py` 的写入者现在默认各用一个 Session，`--client spawn` 恢复原做法。一次完整运行（库 `agentdb_test_review_0fbedc9103`）中，十个分支全部合入，核验全部通过：建分支阶段 0.39 s（原 4.97 s），合入阶段 1.19 s（原 9.47 s），端到端 2.30 s（原 14.93 s），陈旧 head 重试仍为 45 次。打开 10 个会话另需 2.07 s，在计时开始之前完成。原纪录保存在 `results/local-review-results.2026-10-17-spawn.json`。

`agent_lab.py` 每次工具调用都是一个新的 Python 进程，无法跨调用保持 Session。可选的常驻网关 `agent_lab_gateway.py` 替 Agent 持有 Session，默认关闭，尚未用于任何真实 GPT 实验，见 [agent_lab 常驻网关](AGENT_LAB_GATEWAY.md)。

## 批量解析目标状态后的复测（2026-10-18）

第 2 项建议已经实现。`vc.merge` 对每张已登记表只调用一次 `vc.rows_at(ctx.base, t.id)`，与 `diff_rows` 的结果左连接，得到全部改动行的目标状态；该表没有改动时，这次调用由一次性过滤条件跳过。循环按 `diff_rows` 的原输出顺序（WITH ORDINALITY）逐行处理，判定不变：与来源相同的行跳过，否则目标状态不等于共同祖先时报 P0001；写 main 仍逐行 `_apply_main`，写分支时仍以目标状态作为 before_row 暂存；文件冲突、父边与提交信息均未改动。新安装已包含（`sql/001_core.sql`）；旧安装执行 `python db.py install-merge-batch`，即 `sql/004_merge_batch.sql`，以相同定义 CREATE OR REPLACE，并保留所有者、授权与 search_path。

核验（均为本机专用测试库）：

- 改动前先跑基线：`run_tests.py` 18 项与 Session 检查、`review_local.py` 全部通过，用的是旧版 `vc.merge`。
- `run_tests.py` 新增 `merge_resolves_target_state_per_changed_row`，共 13 个合入情形：与来源相同的行被跳过、两边插入相同行、合入分支时 before_row 取目标状态，以及修改/修改、删除/修改、修改/删除、插入/插入、合入分支时冲突、多行冲突时报告哪一行。原 18 项断言未改。
- [`scripts/check_merge_migration.py`](../scripts/check_merge_migration.py) 用提交 8aeb3d7 的旧 `001_core.sql` 建库（`agentdb_test_mergemig_502ef6643a`），先跑 18 项断言和上述情形，并提交一次合入。迁移连续执行两次后：业务数据与历史表的哈希不变；函数所有者、SECURITY DEFINER 与 ACL 不变，search_path 末尾加上 `pg_temp`（2026-10-21 起 004 的定义自带，此前不变）；13 个情形的规范化输出（行变更、父边、错误码与报错的键）与旧函数逐项相同；18 项断言再次通过；`agentdb_client` 仍可合入。脚本同时确认 001 与 004 中的函数体一致。结果见 [`results/merge-batch-migration-results.json`](../results/merge-batch-migration-results.json)：该文件为 2026-10-21 在 004 的定义加入 `pg_temp` 后的重跑（库 `agentdb_test_mergemig_118789d112`），结论除 search_path 外与首次运行及提交前复核的重跑（库 `agentdb_test_mergemig_21ad82e33c`）逐项相同，此前的结果文件已被覆盖。另在一个旧版测试库上通过命令行入口 `db.py install-merge-batch` 执行过迁移。
- 改动后 `run_tests.py`（库 `agentdb_test_1ab8001efa3d`）与 `review_local.py`（库 `agentdb_test_review_16e28e9e61`）全部通过。在 `review_local.py` 中，十个分支合入阶段 0.49 s，端到端 1.79 s，陈旧 head 重试 45 次。同一天改动前用旧函数跑的基线为 1.29 s 和 2.65 s。

`python scripts/benchmark_merge.py --runs 3 --modes persistent_concurrent persistent_serialized --output results/merge-benchmark-batched-results.json` 的结果见[原始结果](../results/merge-benchmark-batched-results.json)。模板库为 `agentdb_test_mergebench_bb4c8a05_tpl`，历史结构与前两次相同；所有运行和 profile 的核验均通过。下表中“改动前”取自上文：SQL 一列来自第一次 profile，其余各列来自 Session 复测。

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

历史长度对合入与提交的影响及其处理（`sql/005_bounded_history.sql`）见 [历史增长](HISTORY_GROWTH.md)。

---

<a id="english-version"></a>

# Ten Branches Merging into main: Timing Breakdown

Date: 2026-10-17. **Local synthetic load only**: 10 scripted writers on the existing Ubuntu WSL PostgreSQL, not large-language-model Agents; it does not represent production capacity, and no cloud services were accessed. At the time of this measurement, neither `vc.merge` nor the version engine had been modified, and no fix had been implemented; the later implementation and re-measurements are in the two sections "Re-measurement after implementing long-lived connections" and "Re-measurement after batch-resolving the target state".

Entry point: [`scripts/benchmark_merge.py`](../scripts/benchmark_merge.py). Raw results: [`results/merge-benchmark-results.json`](../results/merge-benchmark-results.json), including the client time and server timestamps of every call. `python scripts/benchmark_merge.py --reanalyze` recomputes the derived fields from the raw calls without connecting to the database.

## Problem

[Section 4 of the local review record](LOCAL_REVIEW_RESULTS.md) measured: the phase of ten writers merging into main took 9.47 s, with a median single merge of 7.0 s and 45 stale-head (40001) retries. We first need to find out where the time goes, before choosing between a merge queue/backoff, optimizing `vc.merge`, and reducing client overhead.

## Method

- Only one template database is used, `agentdb_test_mergebench_758f297d_tpl`: installation, the 4,000-row seed, the original 18 assertions (the same as `review_local.py` before merge_all), then 10 branches created from the same baseline, each changing 10 rows and writing 1 code file. Each run clones a new database with `CREATE DATABASE … TEMPLATE`, so all runs have the same starting history and revisions. Template history: 12 changesets, 4,100 row changes, 11 paths.
- Each mode is run 3 times, interleaved by round, so that WSL warm-up or drift does not concentrate on one mode. After each run it is verified that there are 4,000 rows, 100 rows changed, 3,900 rows unchanged, 10 files, all 10 source commits reachable, the pinned baseline unchanged, and no leftover work transactions. All 15 runs and 3 profiles passed.
- Timing modes:

| Mode | Client | Scheduling |
|---|---|---|
| `baseline` | The original `review_local.py` merge code (each call starts wsl.exe + psql, role `agentdb_client`), not instrumented | 10 writers start at the same time, all with the baseline as expected |
| `spawn_concurrent` | Same as above, instrumented | Same as above |
| `spawn_serialized` | Same as above, instrumented | Merge one at a time, with expected set to the previous merge result (ideal queue, no 40001) |
| `persistent_concurrent` | One resident psql session per writer, instrumented | Start at the same time, retry rules unchanged |
| `persistent_serialized` | Resident sessions, instrumented | Merge one at a time |
| profile | `persistent_serialized` + `track_functions='all'` on that test database, with EXPLAIN ANALYZE additionally measuring the component queries | Merge one at a time |

- Instrumentation: in the same psql script, first `SELECT … FROM vc.branch WHERE name='main' FOR UPDATE` takes the main row lock, then the unmodified `vc.merge` is called, recording `clock_timestamp()` between the steps. Since this transaction already holds the lock, `begin_change`'s own FOR UPDATE no longer waits, and the expected check stays as it was. Locking `vc.branch` directly needs owner privileges, so the instrumented writers connect as the owner without SET ROLE; `vc.merge` itself is SECURITY DEFINER, so its internal execution identity is unchanged. Instrumented results match the original client: phase median 14.7 s vs 13.6 s, single-merge median 9.8 s for both, 45 retries for both.
- Breakdown (the parts do not overlap): client overhead = call wall-clock − server time from start to the last timestamp, i.e. process startup, connection and exit; lock wait = start → locked; SQL = locked → merged (the body of `vc.merge`); commit = merged → committed; retry cost = all the time of failed attempts (with their client overhead and lock wait listed separately) + the calls re-reading head after 40001.

## Results (median of 3 runs, min–max in parentheses)

### Merge-phase wall clock

| Mode | Phase time | Median single merge | 40001 retries | Share of time the main lock is held | Gap from one commit to the next lock acquisition (median) |
|---|---:|---:|---:|---:|---:|
| baseline | 13.61 s (13.24–15.75) | 9.84 s | 45 | — | — |
| spawn_concurrent | 14.71 s (14.20–15.73) | 9.80 s | 45 | 6.2 % | 1,604 ms |
| spawn_serialized | 2.82 s (2.59–2.89) | 0.27 s | 0 | 29 % (2 usable runs) | 202 ms |
| persistent_concurrent | 0.92 s (0.90–0.93) | 0.50 s | 45 | 93 % | 4.7 ms |
| persistent_serialized | 0.89 s (0.87–0.91) | 0.09 s | 0 | 97 % | 2.5 ms |

45 = 10×9/2 is deterministic: all writers use the baseline as expected, and in each round only the one that gets the lock succeeds, while the rest receive 40001 immediately after getting the lock.

### Writer time totals (spawn_concurrent, summed over the 10 writers, about 91 s)

| Part | Total | Share |
|---|---:|---:|
| Process/connection overhead of the final attempts | 6.54 s | 7 % |
| Process/connection overhead of failed attempts | 43.03 s | 47 % |
| Re-reading head after 40001 (almost all process startup) | 40.11 s | 44 % |
| Lock wait (final 0.01 s + failed 0.40 s) | 0.41 s | 0.4 % |
| `vc.merge` SQL | 0.81 s | 0.9 % |
| COMMIT | 0.03 s | < 0.1 % |

In all three runs, client process overhead accounted for 98.6–98.8 % of writer time. From the server's point of view, the main lock was held for only 0.86 s out of 13.6 s: after each commit, a loser first has to start a process to re-read head and then start another to retry, about two startups of roughly 1.6 s, during which the lock sits idle. Probes in the same run show that starting a single `SELECT 1` takes a median of 272 ms, starting 10 at the same time a median of 2,037 ms; a resident session round trip is 0.16 ms.

After switching to resident sessions, even with still 45 retries, the phase time dropped from 14.7 s to 0.92 s. The main lock is then held 93 % of the time; the writers' time is mainly queuing for the lock (4.06 s; this is concurrent writers waiting while others execute SQL, not on the critical path). The 45 re-reads took only 34 ms in total.

### Inside `vc.merge` (profile, 3 runs, 10 merges each)

The SQL per merge is about 87 ms (`vc.merge` totals 835 / 869 / 892 ms).

| Function | Calls | Total time | Share of `vc.merge` |
|---|---:|---:|---:|
| `vc.rows_at` (all) | 140 | 605–653 ms | 71–73 % |
| Of which: resolving the target state for each changed row (`SELECT … FROM vc.rows_at(ctx.base) WHERE pk=…`, 10 times per merge) | 100 | 453–496 ms | 54–56 % |
| Of which: inside `vc.diff_rows` (2 registered tables × 2 times) | 40 | 144–157 ms | about 17 % |
| `vc.diff_rows` itself | 20 | 63–65 ms | 7 % |
| `vc._apply_main` + triggers | 100 | 69–73 ms | 8 % |
| `finish_change` / `begin_change` | 10 / 10 | 20–33 / 14–19 ms | 4 % |
| `_file_state` (11 paths × 3) | 230 | 8–13 ms | 1 % |
| `_ancestors`, `merge_base` | 30 / 10 | about 10 / 8 ms | 2 % |

EXPLAIN ANALYZE (5 measurements each in the template state and in the state before the 10th merge): single-key `rows_at` 6.5–9.8 ms, each time resolving the complete 4,000 rows and then filtering; fetching all 10 changed keys at once (including diff_rows) 22–27 ms, while diff_rows alone is 19–21 ms, i.e. batch resolution is about 3–7 ms. From this it is estimated that batch resolution could reduce the SQL per merge from about 87 ms to about 40 ms. **This is an extrapolation; it has not been implemented or measured.** This cost grows as "number of changed rows × number of table rows", so the more rows or the larger a single change, the bigger the gap.

## Recommendations

1. **First reduce client process overhead**: each Agent uses a resident connection or a connection pool, instead of starting `wsl.exe + psql` for every call. This accounts for 98.6 % of writer time, and is why the main lock is idle 94 % of the time. Changing only this, without changing scheduling or touching the SQL, the phase time drops from 14.7 s to 0.92 s (about 16×).
2. **Then batch-resolve the target state inside `vc.merge`**: once the client overhead is removed, the main lock is held 93–97 % of the time, and the critical path is the SQL. More than half of it is the 10 per-row `rows_at` calls. The phase time is estimated to drop from about 0.9 s to about 0.45 s, with the benefit growing with the number of table rows and changed rows. The change must keep the conflict decision semantics and be covered by the original 18 assertions and the merge-all verification.
3. **No merge queue or backoff needed for now**: with resident sessions, the ideal queue is only 2 % faster than 45 retries (0.89 s vs 0.92 s), and the re-reads total 34 ms. The lock is already 93 % busy, so backoff would only add idle time. On a client that still starts a process per call, a queue could reduce 14.7 s to 2.8 s, but it only replaces the expensive retries without addressing the root cause, and is less effective than resident connections. When there are clearly more than 10 writers, the number of retries grows as N(N−1)/2, and it should be measured again then.

## Re-measurement after implementing long-lived connections

Recommendation 1 has been implemented as `db.Session` (see [README](../README.md)). `vc.merge` and the version engine are still unmodified, and recommendations 2 and 3 are not yet implemented. Each Session call keeps the semantics of a single process: stop at the first error, roll back an unfinished transaction, reset session state and psql variables. This costs about 1 ms more per call: in the probe the `SELECT 1` round trip median is 1.08 ms, while the bare pipe in the prototype was 0.16 ms. A new check in `run_tests.py` verifies these semantics item by item and compares with the output of a single process.

After the resident session modes of `scripts/benchmark_merge.py` were switched to `db.Session`, the measurement was repeated on a new template `agentdb_test_mergebench_05e9004d_tpl` (3 times each, [raw results](../results/merge-benchmark-session-results.json)):

| Mode | Phase time | 40001 retries | Share of time the main lock is held | Hand-off gap (median) |
|---|---:|---:|---:|---:|
| spawn_concurrent (control) | 15.33 s (14.15–15.49) | 45 | 7.1 % | 1,374 ms |
| `db.Session`, starting at the same time | 1.16 s (0.99–1.19) | 45 (44 once) | 93 % | 7.7 ms |
| `db.Session`, merging one at a time | 0.92 s (0.87–1.13) | 0 | 96 % | 4.1 ms |

The writers of `review_local.py` now each use a Session by default, and `--client spawn` restores the original approach. In one complete run (database `agentdb_test_review_0fbedc9103`), all ten branches were merged and all verifications passed: branch creation phase 0.39 s (originally 4.97 s), merge phase 1.19 s (originally 9.47 s), end to end 2.30 s (originally 14.93 s), still 45 stale-head retries. Opening the 10 sessions takes another 2.07 s, done before timing starts. The original record is saved in `results/local-review-results.2026-10-17-spawn.json`.

Each tool call of `agent_lab.py` is a new Python process and cannot keep a Session across calls. The optional resident gateway `agent_lab_gateway.py` holds Sessions for the Agents; it is off by default and has not yet been used for any real GPT experiment; see [agent_lab resident gateway](AGENT_LAB_GATEWAY.md).

## Re-measurement after batch-resolving the target state (2026-10-18)

Recommendation 2 has been implemented. `vc.merge` calls `vc.rows_at(ctx.base, t.id)` only once per registered table and left-joins it with the result of `diff_rows` to get the target state of all changed rows; when the table has no changes, this call is skipped by a one-time filter condition. The loop processes rows one by one in the original output order of `diff_rows` (WITH ORDINALITY), with the decisions unchanged: a row identical to the source is skipped, otherwise P0001 is raised if the target state differs from the common ancestor; writing to main is still per-row `_apply_main`, and writing to a branch still stages the target state as before_row; file conflicts, parent edges and commit information are unchanged. New installations already include it (`sql/001_core.sql`); old installations run `python db.py install-merge-batch`, i.e. `sql/004_merge_batch.sql`, which does CREATE OR REPLACE with the same definition and keeps the owner, grants and search_path.

Verification (all on dedicated local test databases):

- Before the change, the baseline was run first: `run_tests.py`'s 18 items and the Session check, and `review_local.py`, all passed, using the old `vc.merge`.
- `run_tests.py` adds `merge_resolves_target_state_per_changed_row`, with 13 merge cases in total: rows identical to the source are skipped, identical rows inserted on both sides, before_row taking the target state when merging into a branch, as well as modify/modify, delete/modify, modify/delete, insert/insert, conflict when merging into a branch, and which row is reported when multiple rows conflict. The original 18 assertions are unchanged.
- [`scripts/check_merge_migration.py`](../scripts/check_merge_migration.py) builds a database with the old `001_core.sql` from commit 8aeb3d7 (`agentdb_test_mergemig_502ef6643a`), first runs the 18 assertions and the cases above, and commits one merge. After running the migration twice in a row: the hashes of business data and history tables are unchanged; function owner, SECURITY DEFINER and ACL are unchanged, and search_path gains a trailing `pg_temp` (004's definition includes it since 2026-10-21; before that it was unchanged); the normalized outputs of the 13 cases (row changes, parent edges, error codes and the keys in errors) are identical item by item to the old function; the 18 assertions pass again; `agentdb_client` can still merge. The script also confirms that the function bodies in 001 and 004 are identical. Results are in [`results/merge-batch-migration-results.json`](../results/merge-batch-migration-results.json): this file is a rerun on 2026-10-21 after 004's definition gained `pg_temp` (database `agentdb_test_mergemig_118789d112`), with conclusions identical item by item, except for search_path, to the first run and to the pre-commit review rerun (database `agentdb_test_mergemig_21ad82e33c`); the earlier result files were overwritten. In addition, the migration was run once on an old-version test database through the command-line entry point `db.py install-merge-batch`.
- After the change, `run_tests.py` (database `agentdb_test_1ab8001efa3d`) and `review_local.py` (database `agentdb_test_review_16e28e9e61`) all passed. In `review_local.py`, the ten-branch merge phase took 0.49 s, end to end 1.79 s, with 45 stale-head retries. The baseline run with the old function on the same day before the change was 1.29 s and 2.65 s.

The results of `python scripts/benchmark_merge.py --runs 3 --modes persistent_concurrent persistent_serialized --output results/merge-benchmark-batched-results.json` are in the [raw results](../results/merge-benchmark-batched-results.json). The template database is `agentdb_test_mergebench_bb4c8a05_tpl`, with the same history structure as the previous two; verification of all runs and profiles passed. In the table below, "before the change" is taken from above: the SQL row comes from the first profile, and the other rows from the Session re-measurement.

| Metric | Before the change | After batch resolution |
|---|---:|---:|
| `vc.merge` SQL per merge (profile, median of 30 merges) | about 87 ms (84–89) | **42.8 ms** (37.6–59.1) |
| `vc.rows_at` calls / total in the profile (10 merges per run) | 140 calls / 605–653 ms | 50 calls / 186–195 ms |
| `db.Session` starting at the same time: phase time | 1.16 s (0.99–1.19) | 0.84 s (0.83–0.88) |
| Same: total writer `vc.merge` SQL | 986 ms | 508 ms |
| Same: share of time the main lock is held | 93 % | 77 % |
| `db.Session` merging one at a time: phase time | 0.92 s (0.87–1.13) | 0.66 s (0.54–0.79) |
| Same: median single merge | — | 64 ms |
| 40001 retries (starting at the same time) | 45 | 45 |

The SQL per merge dropped to about 43 ms, consistent with the earlier estimate of about 40 ms. `rows_at` is called 5 times per merge: 2 tables × 2 times inside `diff_rows`, plus 1 time for the target state of products; the orders table has no changes, so it is skipped. The remaining SQL time is mainly `diff_rows` (about 19 ms per merge) and per-row `_apply_main` (about 5 ms). The phase time dropped only about 28 %, not by half: as the SQL got shorter, the share of client round trips, COMMIT and lock hand-off on the critical path rose, and lock occupancy also dropped to 77–83 %. Optimizing further would require `diff_rows` to replay the whole table less (for example combining the baseline and source rows_at into one during a merge), or reducing round trips per merge; neither is implemented.

These results come from a different time period, and timing on WSL fluctuates considerably (see the next section); ratios are more reliable than absolute values. The load is the same as the previous two: 4,000 rows, 10 branches, each branch changing 10 rows and writing 1 file. Target state resolution used to grow as "number of changed rows × number of table rows", and now each table is replayed only once. With more rows or larger single changes the gap would be larger, but this was not measured.

## Environment and caveats

- PostgreSQL 16.15 (Ubuntu 24.04, WSL 2.6.3.0, kernel 6.6.87.2-1), unix socket; `shared_buffers` 128 MB, `synchronous_commit=on`, `fsync=on`, READ COMMITTED. The client is Windows 11 (26200), Python 3.10.11, 22 logical CPUs. The service was not started/stopped or replaced, and the `agentdb` project database was not written to.
- Process startup time fluctuates a lot at different times: in a smoke run earlier the same day, the median for 10 concurrent process starts was about 0.6 s, and 2.0 s in the formal runs; the baseline is therefore also slower (13.6 s) than the 9.47 s recorded in section 4. Conclusions are about ratios, not absolute values.
- Server timestamps come from the WSL guest clock and wall-clock times from Windows `perf_counter`; the two usually differ by about ±2 ms, and in one run the deviation reached about 4 %. So client overhead with resident sessions is below the resolution and shows up as close to 0 or even slightly negative; the actual round trip should be taken as the directly measured 0.16 ms. In the 1st run of spawn_serialized a clock step of about −0.95 s occurred: the 1 affected call is recorded as `clock_step_unattributed`, and that run's lock timeline is not included in the summary.
- The template contains only the history produced by the seed and the original 18 assertions, not the side branches created by `review_local.py`'s maintenance checks. Branch proposals are created one at a time, whereas in the original experiment they were created concurrently. With deeper history and more paths, the lookback costs of `_file_state` and `rows_at` would change.
- The phase time of the resident session modes is measured from after all sessions are established and does not include establishing them: each session takes a median of about 150–165 ms to establish, about 1.6 s in total for 10 established in sequence (see `session_open_ms` in the result JSON). This corresponds to connection-pool usage where a connection is established once and then reused; if each Agent merges only once and then disconnects, this cost cannot be left out.
- `track_functions` itself has overhead: the profile phase took 0.92 s, and an equivalent run without it 0.89 s. The EXPLAIN of the component queries was run outside `vc.merge` as equivalent read-only queries.
- The test databases kept have the prefix `agentdb_test_mergebench_758f297d_` (template, 15 runs, 3 profiles); the list is in `databases_retained` in the result JSON. One smoke run's `agentdb_test_mergebench_827453e5_*` is also kept; it is not counted in the results.

For the effect of history length on merges and commits and how it is handled (`sql/005_bounded_history.sql`), see [History growth](HISTORY_GROWTH.md).
