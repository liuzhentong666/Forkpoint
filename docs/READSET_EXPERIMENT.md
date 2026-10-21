中文 | [English](#english-version)

# 读集校验与陈旧 head 检查：实验设计

状态（2026-10-21）：设计稿，在编写任何代码之前固定。本文内容尚未实现，也未运行。**仅为本机合成负载**：脚本写入者，不是大模型 Agent；不代表生产容量，不访问任何云服务。

## 问题

目前合入 main 必须给出确切的当前 head（`p_expected`），否则 `vc.begin_change` 报 40001。10 个写入者从同一基线出发时，即使改动的行互不相交，也会产生约 45 = 10×9/2 次拒绝（[合入耗时分解](MERGE_BENCHMARK.md) 中一次运行为 44；`review_local.py` 的 `merge_all`）。

收到 40001 之后怎么做，取决于客户端：

- **盲重试**：`review_local.py` 重读 head 后原样重交同一提案；`agent_lab.py` 的注释也说明 stale_head 后同一提案可以再试。这样很便宜，但 head 检查此时没有多保护任何东西：能拦下的只有 `vc.merge` 的三方行/文件冲突和原生约束。如果 Agent 的决定依赖它读过但没有写的行（写偏斜），这个决定可能基于已变化的数据合入。[README 的限制一节](../README.md#limitations)已列出这一点：跨行规则不会自动推断。
- **重新判断**：Agent 重读自己依赖的数据并重新计算。这是安全的，但对大模型 Agent 来说，每次 40001 都要多花一轮，包括由无关合入引起的那些。

读集校验：提案列出其决定所依赖的行；只有当其中某行自提案的基线以来在 main 上发生变化时，合入才被拒绝。要回答：

1. 它能否去掉无关紧要的拒绝（W1）？
2. 它能否仍然拦下要紧的冲突（W2），而盲重试会放过这些冲突？
3. 在持有 main 锁期间，校验要花多少时间？
4. 结果在多大程度上取决于读集的来源：由 Agent 声明（可能漏报），还是自动记录（可能多报）？

## 原型（仅限实验，不修改 `vc.*`）

`experiments/readset/readset.sql` 只安装到本实验的测试库（schema `xr`），`db.py install` 不会安装它。

```text
xr.merge_validated(p_target text, p_source uuid, p_reads jsonb, p_author text) RETURNS uuid
  1. head := p_target 在 vc.branch 中的 head，FOR UPDATE   -- 与 vc.begin_change 一样取得 main 的锁
  2. base := vc.merge_base(head, p_source)
  3. 若 base <> head：对 p_reads 中的每张表，出现在 vc.diff_rows(base, head, 表) 中的读键即为读冲突
  4. 有读冲突 -> RAISE SQLSTATE 'RS001'（read_conflict）；DETAIL 为 JSON，列出表、键、基线值与当前值，
     写入者据此重新计算，无需重读全部数据
  5. 否则 RETURN vc.merge(p_target, p_source, head, p_author)
```

第 5 步以刚锁定的 head 调用未修改的 `vc.merge`，陈旧 head 检查必然通过，现有的写写行/文件冲突检查与原生约束原样生效。校验按值比较（`diff_rows` 比较的是行内容），所以被改后又改回原值的行不算冲突；决定只依赖读到的值，这正是需要的。

- `p_reads` 格式：`{"<table_id>": [{"id": 1}, …]}`，只含按主键的点读。读集只放读过但没有写的键；写过的键交给 `vc.merge`，冲突时仍报 P0001。
- 计时：函数在取得锁后和校验结束后各记录一次 `clock_timestamp()`（写入会话设置，COMMIT 前读回）。两个 head 组改为调用薄包装 `xr.merge_head(p_target, p_source, p_expected, p_author)`：同样先取锁并记时，再以原 `p_expected` 调用 `vc.merge`，陈旧 head 语义不变。这样各组的计时方式相同。
- 两个函数均为 SECURITY DEFINER，属主为数据库所有者，仅向 `agentdb_client` 授予 EXECUTE，`search_path=pg_catalog,vc,xr,pg_temp`。
- 前提：每个提案是从 main 分出的分支上的一次变更（所有工作负载都满足），因此合入基线就是写入者读到的版本。

## 实验组

| 组 | 合入调用 | 40001 之后 | RS001 / P0001 之后 | 读集 |
|---|---|---|---|---|
| `head_blind` | `xr.merge_head` | 重读 head，重交同一提案 | 重新判断 | — |
| `head_rethink` | `xr.merge_head` | 重新判断 | 重新判断 | — |
| `rs_declared` | `xr.merge_validated` | 不会发生 | 重新判断 | 决定实际用到的行 |
| `rs_undercite` | `xr.merge_validated` | 不会发生 | 重新判断 | 同 `rs_declared`，但每个写入者按 0.5 的概率（由种子决定）漏报每个依赖行 |
| `rs_tracked` | `xr.merge_validated` | 不会发生 | 重新判断 | 写入者读过的全部行，包括 20 行它不使用的浏览行 |

- **重新判断**：放弃原提案，从当前 main 新建分支，重读依赖行，重新计算，然后重新提交或放弃。
- 55P03（lock_timeout）在所有组都是原样重交。上限与 `review_local.py` 相同：每个写入者最多 30 次非锁超时尝试、60 次锁超时；用尽上限的写入者记为失败，保留在结果中。
- 浏览行（`rs_tracked`）：每个写入者 20 行，用本次运行的种子从第 1–1,000 行中抽取，排除自己写的行；重新判断时重读同一组浏览行。

## 工作负载

每次运行 10 个写入者，各用一个 `db.Session`，与 `merge_all` 一样在屏障后同时提案，再在屏障后同时合入。

- **W1 互不相交**：即现有 `merge_all`：写入者 i 把第 10i+1…10i+10 行的价格设为 100+i，并写入 `src/agent_i.py`。写入者之间没有依赖。声明读集为空（它只读自己写的行）。
- **W2 写偏斜**：5 个套装，每个含两件商品（第 3001–3010 行，库存各 60，套装合计 120）。规则由写入者检查，没有对应的数据库约束：套装总库存必须保持 ≥ 100。写入者 2b 与 2b+1 各想从套装 b 的不同商品中卖出 15 件；各自读取两件商品，只有卖出后套装合计仍 ≥ 100 才卖。单独卖出都允许（105），同时卖出则违反规则（90）。正确结果是每个套装恰好一个卖出、另一个放弃。声明读集：同套装的另一件商品。
- **W3 混合**：写入者 0–5 同 W1，写入者 6–9 组成两个 W2 套装（第 3001–3004 行）。

模板库（整个实验一个）：安装，按 `review_local.seed` 写入 4,000 行种子，再用一次种子提交把第 3001–3010 行库存设为 60，并物化快照。每次运行用 `CREATE DATABASE … TEMPLATE` 克隆新库（同 `scripts/benchmark_merge.py`），所有运行的起始历史相同。模板历史与合入基准不同，绝对耗时不与 [合入耗时分解](MERGE_BENCHMARK.md) 比较，只在本实验内比较各组。

## 运行安排

5 组 × 3 种工作负载 × 5 个种子（1–5）= 75 次运行。按轮次交错：对每个种子、每种工作负载，各组顺序按种子轮换，避免 WSL 漂移集中在某一组。种子决定浏览行与漏报，不决定线程时序；哪个写入者先合入会变化，并被记录。

## 测量

每次调用保存原始 JSONL：发送的 SQL、返回码、SQLSTATE、标准输出/错误、客户端时间戳、服务端取锁与 COMMIT 前的时间戳。

每个写入者：

- 结果：合入 / 放弃 / 失败
- 重交次数：同一提案再次发送（`head_blind` 的 40001，以及任何组的 55P03）
- 重新判断次数：迫使写入者重新计算的拒绝
- **无效的重新判断**：重新计算后的提案与上一次完全相同（相同的行改动和文件）。这些花了一轮，什么也没改变。
- 发送的读集大小

每次运行：

- **陈旧提交**（独立判定）：对每个已合入的合入提交，取其第一父提交（合入前的 main），把写入者**真实**依赖的每一行在那里的值与写入者使用的值比较；任一不同，即该决定基于已变化的数据合入。判定使用工作负载的真实依赖，不使用该组的读集；用 `vc.rows_at` 读取，不经过原型。
- **规则违反**：最终 main 上总库存低于 100 的套装数（W2、W3）。
- 沿用 `merge_all` 的核验：4,000 行、未改动的行保持原值、每个已合入写入者的文件存在且其提交可达、基线不变、无遗留 `vc.work_tx`。
- 时间：合入阶段墙钟；每次合入持有 main 锁的时间（取锁 → COMMIT 前），拆为校验（第 1–4 步）与 `vc.merge`；阶段内 main 被锁定的时间占比。

陈旧提交判定器有自己的单元测试：用构造的历史，各含一个已知陈旧和一个已知新鲜的提交，做法同 GPT 实验的覆盖检测器。

## 预测（运行前写定）

| 负载 | 指标 | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---|---|---|---|---|---|
| 全部 | 40001 重交 | ≤ 45 | 0 | 0 | 0 | 0 |
| W1 | 重新判断 | 0 | ≤ 45，全部无效 | 0 | 0 | > 0，全部无效 |
| W2 | 规则违反 = 陈旧提交 | 5 | 0 | 0 | 平均 2.5 | 0 |
| W2 | 重新判断 | 0 | ≤ 25，恰有 5 次有效 | 5，全部有效 | 5 − 违反数 | 5，全部有效 |
| W3 | 规则违反 = 陈旧提交 | 2 | 0 | 0 | 平均 1 | 0 |
| W3 | 重新判断 | 0 | ≤ 43，恰有 2 次有效 | 2，全部有效 | 2 − 违反数 | ≥ 2，多出的均无效 |

上界的来历：写入者每次被拒，都对应它上次读取之后至少一次合入，所以被拒次数不超过排在它前面的合入数。W1 为 9+8+…+1 = 45；W2 中每次合入还让同套装的另一个写入者放弃，为 9+7+5+3+1 = 25；W3 取决于两个套装在合入顺序中的位置，最多 43。W2 的 `rs_tracked` 与 `rs_declared` 相同，因为 W2 没有人写第 1–1,000 行。

这些计数大多是设计本身的直接推论。实验检验原型是否按设计工作，并测量设计推不出来的部分：校验耗时，以及并发下实际的重新判断次数。

## 采纳标准（现在决定）

只有全部满足，读集校验才成为版本引擎的候选改动（按 AGENTS.md，作为单独的、有自己测试与迁移的行为变更）：

1. **安全**：`rs_declared` 与 `rs_tracked` 在每次 W2、W3 运行中陈旧提交与规则违反都为 0；且 `head_blind` 的违反数与预测一致（说明判定器能看到它应看到的问题）。
2. **成本**：`rs_declared` 在 W1 每次运行的重新判断为 0，在 W3 不超过有效次数（2），即没有无效的重新判断。
3. **开销**：校验（第 1–4 步）的中位耗时不超过同批运行中 `vc.merge` 的中位耗时，即每次合入持有 main 锁的时间最多翻倍。
4. 每次运行的 `merge_all` 核验全部通过；用尽上限的失败写入者如实报告，读集组中出现任何失败，在查明原因前不采纳。

若第 1 条不满足，说明原型有误。若第 3 条不满足，先把校验改为增量方式再考虑：只检查基线与 head 之间、改过读键的提交（利用 `vc.row_change(table_id, pk, changeset)` 索引），而不是每次回放整表。所有运行都报告，包括失败。

## 本实验回答不了的问题

- 没有大模型：这里的重新判断瞬间完成，换成 Agent 每次都是一轮。结果是次数，不换算成 token 或分钟。
- 真实 Agent 漏报的频率。0.5 是为展示机制而设的假设，不是估计，需要第二阶段。
- 只有点读。基于查询的决定（“库存低于 10 的全部商品”）还依赖之后可能插入的行（幻读），按键的读集拦不住这些。
- 每个分支一次变更；多提交分支、中途合入过 main 的分支不在范围内。
- WSL 上的耗时随时段波动（见 [合入耗时分解](MERGE_BENCHMARK.md)）；只在本实验内比较各组，看比例不看绝对值。

## 第二阶段（仅为提纲；需要单独的协议，并经用户同意使用模型额度）

通过 `agent_lab.py` 让真实 Agent 参与：合入工具要求提供 `relied_on`（键列表），并记录 Agent 的每一次读取。逐轮比较声明读集与记录读集，测量漏报与多报，再用测得的行为重跑 W2、W3。遵循 [GPT 重复实验](GPT_REPEAT_EXPERIMENT.md) 的预登记规则。

## 计划文件

| 文件 | 用途 |
|---|---|
| `experiments/readset/readset.sql` | `xr` schema、`xr.merge_validated`、`xr.merge_head`；只装入测试库 |
| `experiments/readset/run_readset.py` | 模板库、75 次运行、原始 JSONL、结果 JSON |
| `experiments/readset/oracle.py` | 从 vc 历史判定陈旧提交与规则违反 |
| `experiments/readset/test_readset_oracle.py` | 单元测试，含构造的陈旧/新鲜历史 |
| `results/readset/` | 原始结果 |

结果追加到本文；上面的预测与标准在运行后不修改。

---

<a id="english-version"></a>

# Read-Set Validation vs. the Stale-Head Check: Experiment Design

Status (2026-10-21): design, fixed before any code is written. Nothing here has been implemented or run. **Local synthetic load only**: scripted writers, not large-language-model Agents; it does not represent production capacity, and no cloud services are accessed.

## Question

Today a merge into main must name the exact current head (`p_expected`), or `vc.begin_change` raises 40001. When 10 writers start from the same base, this costs about 45 = 10×9/2 rejections even when the rows they change are disjoint (44 in one run of the [merge timing breakdown](MERGE_BENCHMARK.md); `merge_all` in `review_local.py`).

What happens after a 40001 depends on the client:

- **Blind retry**: `review_local.py` re-reads head and resubmits the same proposal unchanged; the comment in `agent_lab.py` also says the same proposal may be tried again after stale_head. This is cheap, but then the head check protects nothing extra: only `vc.merge`'s three-way row/file conflicts and native constraints stop anything. If an Agent's decision depends on a row it read but did not write (write skew), that decision can be merged on data that has changed. The [README's limitations](../README.md#limitations) already list this: cross-row rules aren't inferred.
- **Rethink**: the Agent re-reads what it relied on and recomputes. This is safe, but for an LLM Agent every 40001 then costs an extra turn, including the ones caused by unrelated merges.

Read-set validation: a proposal lists the rows its decision depended on, and the merge is rejected only if one of those rows changed on main since the proposal's base. Questions:

1. Does it remove the rejections that don't matter (W1)?
2. Does it still catch the conflicts that do matter (W2), which blind retry lets through?
3. How long does validation take while main's lock is held?
4. How much do the results depend on where the read set comes from: declared by the Agent (may under-report) or recorded automatically (may over-report)?

## Prototype (experiment only; `vc.*` is not modified)

`experiments/readset/readset.sql` is installed only into this experiment's test databases (schema `xr`); `db.py install` does not install it.

```text
xr.merge_validated(p_target text, p_source uuid, p_reads jsonb, p_author text) RETURNS uuid
  1. head := p_target's head in vc.branch, FOR UPDATE   -- takes main's lock, as vc.begin_change does
  2. base := vc.merge_base(head, p_source)
  3. if base <> head: for each table in p_reads, every read key that appears in
     vc.diff_rows(base, head, table) is a read conflict
  4. any read conflict -> RAISE SQLSTATE 'RS001' (read_conflict); DETAIL is JSON listing table, key,
     value at base and value now, so the writer can recompute without re-reading everything
  5. otherwise RETURN vc.merge(p_target, p_source, head, p_author)
```

Step 5 calls the unmodified `vc.merge` with the head it just locked, so the stale-head check always passes and the existing write-write row/file checks and native constraints apply unchanged. Validation compares values (`diff_rows` compares row contents), so a row changed and then changed back is not a conflict; a decision depends only on the values it read, which is what is needed.

- `p_reads` format: `{"<table_id>": [{"id": 1}, …]}`, point reads by primary key only. The read set holds only keys that were read but not written; written keys are left to `vc.merge`, which still reports P0001 on a conflict.
- Timing: the function records `clock_timestamp()` once after taking the lock and once after validation (into session settings, read back before COMMIT). The two head arms call a thin wrapper instead, `xr.merge_head(p_target, p_source, p_expected, p_author)`: it also takes the lock and records the time first, then calls `vc.merge` with the original `p_expected`, so the stale-head semantics are unchanged. All arms are timed the same way.
- Both functions are SECURITY DEFINER, owned by the database owner, with EXECUTE granted only to `agentdb_client`, and `search_path=pg_catalog,vc,xr,pg_temp`.
- Assumption: each proposal is one change on a branch forked from main (true for every workload), so the merge base is the version the writer read.

## Arms

| Arm | Merge call | After 40001 | After RS001 / P0001 | Read set |
|---|---|---|---|---|
| `head_blind` | `xr.merge_head` | re-read head, resubmit the same proposal | rethink | — |
| `head_rethink` | `xr.merge_head` | rethink | rethink | — |
| `rs_declared` | `xr.merge_validated` | cannot happen | rethink | the rows the decision actually used |
| `rs_undercite` | `xr.merge_validated` | cannot happen | rethink | as `rs_declared`, but each writer omits each dependency row with probability 0.5 (set by the seed) |
| `rs_tracked` | `xr.merge_validated` | cannot happen | rethink | every row the writer read, including 20 browsed rows it doesn't use |

- **Rethink**: drop the proposal, fork a new branch from the current main, re-read the dependency rows, recompute, then propose again or abstain.
- 55P03 (lock_timeout) is a resubmit in every arm. Bounds as in `review_local.py`: at most 30 non-lock-timeout attempts and 60 lock timeouts per writer; a writer that exhausts them counts as failed and stays in the results.
- Browsed rows (`rs_tracked`): 20 per writer, drawn with the run's seed from rows 1–1,000, excluding the writer's own written rows; a rethink re-reads the same browsed rows.

## Workloads

Every run has 10 writers, each with its own `db.Session`; as in `merge_all`, they propose together behind a barrier, then merge together behind a barrier.

- **W1 disjoint**: the existing `merge_all`: writer i sets the price of rows 10i+1…10i+10 to 100+i and writes `src/agent_i.py`. No writer depends on another. Declared read set: empty (it reads only rows it writes).
- **W2 write skew**: 5 bundles of two products each (rows 3001–3010, stock 60 each, 120 per bundle). A rule checked by the writers, with no matching database constraint: a bundle's total stock must stay ≥ 100. Writers 2b and 2b+1 each want to sell 15 units of a different product in bundle b; each reads both products and sells only if the bundle total stays ≥ 100 afterwards. Either sale alone is allowed (105); both together break the rule (90). The correct outcome is exactly one sale per bundle, with the other writer abstaining. Declared read set: the other product in the bundle.
- **W3 mixed**: writers 0–5 as in W1; writers 6–9 form two W2 bundles (rows 3001–3004).

Template database (one for the whole experiment): install, write the 4,000-row seed as `review_local.seed` does, set the stock of rows 3001–3010 to 60 in one more seed commit, and materialize a snapshot. Each run clones a new database with `CREATE DATABASE … TEMPLATE` (as `scripts/benchmark_merge.py` does), so every run starts from the same history. The template history differs from the merge benchmark's, so absolute times are not compared with the [merge timing breakdown](MERGE_BENCHMARK.md); arms are compared only within this experiment.

## Schedule

5 arms × 3 workloads × 5 seeds (1–5) = 75 runs, interleaved by round: for each seed and each workload, the order of the arms is rotated by the seed, so WSL drift does not concentrate on one arm. The seed fixes the browsed rows and the omissions, not thread timing; which writer merges first varies and is recorded.

## Measurements

Every call is saved as raw JSONL: the SQL sent, return code, SQLSTATE, stdout/stderr, client timestamps, and server timestamps at lock acquisition and before COMMIT.

Per writer:

- outcome: merged / abstained / failed
- resubmits: the same proposal sent again (40001 in `head_blind`, and 55P03 in any arm)
- rethinks: rejections that made the writer recompute
- **wasted rethinks**: rethinks whose recomputed proposal is identical to the previous one (same row changes and files). These cost a turn and changed nothing.
- size of the read set sent

Per run:

- **stale commits** (independent check): for each merge commit that landed, take its first parent (main just before the merge) and compare each row the writer **truly** depended on there with the value the writer used; any difference means the decision was merged on data that had changed. The check uses the workload's ground-truth dependencies, not the arm's read set, and reads with `vc.rows_at`, not through the prototype.
- **rule violations**: the number of bundles whose total stock on the final main is below 100 (W2, W3).
- the `merge_all` verification, carried over: 4,000 rows, unchanged rows keep their values, each merged writer's file is present and its commit reachable, base unchanged, no leftover `vc.work_tx`.
- time: merge-phase wall clock; time each merge holds main's lock (lock acquired → before COMMIT), split into validation (steps 1–4) and `vc.merge`; share of the phase during which main is locked.

The stale-commit checker gets its own unit test on a constructed history containing one known stale and one known fresh commit, as the GPT experiment's overwrite detector does.

## Predictions (written before running)

| Workload | Metric | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---|---|---|---|---|---|
| all | 40001 resubmits | ≤ 45 | 0 | 0 | 0 | 0 |
| W1 | rethinks | 0 | ≤ 45, all wasted | 0 | 0 | > 0, all wasted |
| W2 | rule violations = stale commits | 5 | 0 | 0 | 2.5 on average | 0 |
| W2 | rethinks | 0 | ≤ 25, exactly 5 useful | 5, all useful | 5 − violations | 5, all useful |
| W3 | rule violations = stale commits | 2 | 0 | 0 | 1 on average | 0 |
| W3 | rethinks | 0 | ≤ 43, exactly 2 useful | 2, all useful | 2 − violations | ≥ 2, any extra are wasted |

Where the upper bounds come from: each time a writer is rejected, at least one merge has landed since its last read, so its rejections cannot exceed the number of merges ahead of it. In W1 that is 9+8+…+1 = 45; in W2 each merge also makes the other writer in its bundle abstain, giving 9+7+5+3+1 = 25; in W3 it depends on where the two bundles fall in the merge order, at most 43. `rs_tracked` equals `rs_declared` in W2 because nobody writes rows 1–1,000 there.

Most of these counts follow directly from the design. The experiment checks that the prototype behaves as designed, and measures what the design cannot predict: validation time, and the actual number of rethinks under concurrency.

## Adoption criteria (decided now)

Read-set validation becomes a candidate change to the version engine (per AGENTS.md, a separate behavior change with its own tests and migration) only if all of these hold:

1. **Safety**: `rs_declared` and `rs_tracked` have 0 stale commits and 0 rule violations in every W2 and W3 run, and `head_blind`'s violations match the prediction (showing the checker sees what it should).
2. **Cost**: `rs_declared` has 0 rethinks in every W1 run, and in W3 no more than the useful ones (2), i.e. no wasted rethinks.
3. **Overhead**: the median validation time (steps 1–4) is at most the median `vc.merge` time in the same runs, i.e. it at most doubles how long each merge holds main's lock.
4. Every run's `merge_all` verification passes; writers that exhaust their bounds are reported as failures, and any failure in a read-set arm blocks adoption until it is explained.

If criterion 1 fails, the prototype is wrong. If criterion 3 fails, validation is first made incremental before reconsidering: check only the commits between base and head that changed a read key (using the `vc.row_change(table_id, pk, changeset)` index), instead of replaying the whole table each time. All runs are reported, including failures.

## What this experiment cannot tell us

- No LLM: rethinks are instant here, while for an Agent each one is a turn. The result is a count; it is not converted to tokens or minutes.
- How often real Agents under-report. 0.5 is an assumption chosen to show the mechanism, not an estimate; that needs phase 2.
- Point reads only. A decision based on a query ("all products with stock below 10") also depends on rows that may be inserted later (phantoms); key-based read sets don't catch those.
- One change per branch; multi-commit branches and branches that merged main in between are out of scope.
- Timing on WSL varies with the time of day (see the [merge timing breakdown](MERGE_BENCHMARK.md)); arms are compared only within this experiment, and ratios matter more than absolute values.

## Phase 2 (outline only; needs its own protocol and the user's approval to use model quota)

Real Agents take part through `agent_lab.py`: the merge tool requires `relied_on` (a list of keys), and every read the Agent makes is recorded. Compare declared and recorded read sets turn by turn to measure under- and over-reporting, then rerun W2 and W3 with the measured behavior. Follows the pre-registration rules of the [GPT repeat experiment](GPT_REPEAT_EXPERIMENT.md).

## Planned files

| File | Purpose |
|---|---|
| `experiments/readset/readset.sql` | `xr` schema, `xr.merge_validated`, `xr.merge_head`; installed only into test databases |
| `experiments/readset/run_readset.py` | template database, 75 runs, raw JSONL, results JSON |
| `experiments/readset/oracle.py` | stale-commit and rule-violation checks from vc history |
| `experiments/readset/test_readset_oracle.py` | unit tests, including the constructed stale/fresh history |
| `results/readset/` | raw results |

Results will be appended to this document; the predictions and criteria above are not changed after running.
