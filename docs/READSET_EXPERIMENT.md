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

## 文件与运行

| 文件 | 用途 |
|---|---|
| `experiments/readset/readset.sql` | `xr` schema：`xr.lock_main`、`xr.merge_head`、`xr.merge_validated`；只装入本实验的模板库 |
| `experiments/readset/run_readset.py` | 模板库、75 次运行、每次调用的原始 JSONL（`calls.jsonl`）与 `results.json`（含按上文标准的判定） |
| `experiments/readset/oracle.py` | 陈旧提交、规则违反与无效重新判断的判定，以及采纳标准 |
| `experiments/readset/test_readset_oracle.py` | 单元测试，含构造的陈旧/新鲜历史；不连数据库 |
| `experiments/readset/audit_calls.py` | 只用 `calls.jsonl` 重算每项计数并与 `results.json` 比较；不连数据库 |
| `experiments/readset/audit_sql.py` | 在 `--keep-databases` 保留的运行库中，只用 vc 历史重新推导陈旧提交与规则违反 |

```bash
python -m unittest discover -s experiments/readset -p test_readset_oracle.py -v
python experiments/readset/run_readset.py
python experiments/readset/audit_calls.py results/readset/<run>
python experiments/readset/audit_sql.py <directory of a --keep-databases run>
```

实现说明（2026-10-21，不改变上面的预测与标准）：

- 计时：合入脚本先调用 `xr.lock_main` 取得 main 的锁，它返回取锁时刻（同 `scripts/benchmark_merge.py` 先取锁的做法）；之后合入函数再取锁即为空操作。RS001 回滚后会话设置随之消失，所以由错误的 HINT 携带校验结束时刻。
- 所有组的写入者读取相同的依赖行和 20 行浏览行，各组只在发送的读集上不同。写入者以 `agentdb_client` 身份连接，各用一个 `db.Session`。
- 默认输出目录为 `results/readset/<前缀>/`。每次运行的库在核验记录后删除（`--keep-databases` 则保留），模板库保留。
- 正式运行之前，2026-10-21 做过一次冒烟运行（种子 1，共 15 次），只用于检查脚本本身，15 次核验全部通过。它不计入实验，输出没有放进仓库。`--seeds`、`--workloads`、`--arms` 选出的子集在结果中标记为 `complete_schedule=false`。

结果追加到本文；上面的预测与标准在运行后不修改。

## 结果（2026-10-21，75 次）

运行 `readset_df002d50`：UTC 10:49:36 开始，10:53:53 结束（4 分 17 秒）。PostgreSQL 16.15（Ubuntu WSL，unix socket，READ COMMITTED，fsync 与 synchronous_commit 开启），客户端 Windows 11（26200）、Python 3.10.11、22 个逻辑 CPU。模板历史：3 个 changeset、4,010 条行变更、1 个路径。

原始结果：[`results/readset/readset_df002d50/results.json`](../results/readset/readset_df002d50/results.json)，含每个写入者的提案、读集、带服务端时间戳的每次合入尝试，以及运行时各实验源文件的 SHA-256。逐字记录全部 SQL 的 `calls.jsonl`（7.7 MB）只留在本机，已加入 .gitignore；下文所有计数与耗时都能从 results.json 重算。模板库 `agentdb_test_readset_df002d50_tpl` 保留，75 个运行库在核验后已删除。

`complete_schedule` 为 true。75 次运行的核验全部通过；没有失败的写入者，没有锁超时，也没有因时钟不一致而被排除在计时之外的合入调用。

### 预测与实测

下表按种子 1–5 列出实测值；预测见上文“预测”一节。

| 负载 | 指标 | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---|---|---|---|---|---|
| W1 | 40001 重交 | 45 ×5 | 0 | 0 | 0 | 0 |
| W1 | 重新判断 | 0 | 45 ×5，全部无效 | 0 | 0 | 6,4,5,7,6，全部无效 |
| W2 | 40001 重交 | 45 ×5 | 0 | 0 | 0 | 0 |
| W2 | 规则违反 = 陈旧提交 | 5 ×5 | 0 | 0 | 3,3,2,2,3（平均 2.6，预测 2.5） | 0 |
| W2 | 重新判断（有效） | 0 | 25 ×5（各 5 次有效） | 5 ×5（全部有效） | 2,2,3,3,2（= 5 − 违反数） | 5 ×5（全部有效） |
| W3 | 40001 重交 | 45,45,42,45,45 | 0 | 0 | 0 | 0 |
| W3 | 规则违反 = 陈旧提交 | 2 ×5 | 0 | 0 | 0,0,2,1,2（平均 1.0，预测 1） | 0 |
| W3 | 重新判断（有效） | 0 | 31,31,37,33,34（各 2 次有效） | 2 ×5（全部有效） | 2,2,0,1,0（= 2 − 违反数） | 3,4,3,9,8（各 2 次有效） |

逐次预测在 5 次运行中全部成立；两个平均值实测为 2.6 和 1.0。每次 W2、W3 运行中，从 vc 历史判定的陈旧提交数都等于从最终数据得出的规则违反数，这是两个独立的测量。W3 种子 3 的 42 在上界之内：写入者在两次合入之后才重读 head，两次合入只让它被拒一次。

### 采纳标准

| 标准 | 结果 |
|---|---|
| 1 安全 | 通过：rs_declared 与 rs_tracked 在全部 20 次 W2/W3 运行中陈旧提交与规则违反均为 0；head_blind 在全部 10 次中与预测一致（5 和 2） |
| 2 成本 | 通过：rs_declared 在每次 W1 运行中重新判断为 0，在每次 W3 运行中恰为 2 次，没有无效的 |
| 3 开销 | 按登记的定义通过：读集组全部校验的合并中位数 16.3 ms，`vc.merge` 的合并中位数 37.5 ms；见下面的说明 |
| 4 完整 | 通过：75 次运行核验全部通过，没有失败的写入者 |

**关于第 3 条的说明。** 登记的标准比较读集组全部校验的合并中位数，而其中 39%（495 次中的 193 次）读集为空：rs_declared 与 rs_undercite 中的 W1 写入者，以及唯一的依赖被漏报的写入者。这些校验跳过了比较，中位只有 1.6 ms。读集非空时（302 次），校验中位为 22.0 ms，约为 `vc.merge` 中位数的 59%。直接测量的结果相近：读集非空的成功合入持有 main 锁（取锁 → COMMIT 前）的中位为 58.9 ms，head 组为 38.7 ms，约长 52%。按这些数字该标准仍然成立，但余量远小于合并中位数所显示的。校验耗时与键的数量无关（1–2 个键中位 22.8 ms，20 个以上 21.6 ms），因为 `vc.diff_rows` 回放两个版本的整张表；最慢的一次校验为 199.6 ms（rs_tracked）。

### 耗时

| | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---:|---:|---:|---:|---:|
| 校验中位（ms） | 0.24 | 0.21 | 2.7 | 1.75 | 21.6 |
| 校验 p90 / 最大（ms） | 0.34 / 0.46 | 0.32 / 1.15 | 25.4 / 38.0 | 23.7 / 34.8 | 29.0 / 199.6 |
| `vc.merge` 中位（ms） | 38.6 | 37.9 | 38.2 | 38.7 | 36.2 |
| 合入阶段中位（ms）：W1 / W2 / W3 | 456 / 536 / 517 | 801 / 479 / 734 | 408 / 451 / 473 | 408 / 470 / 447 | 642 / 455 / 646 |
| main 被锁定的时间占比（中位）：W1 / W2 / W3 | 0.803 / 0.819 / 0.805 | 0.463 / 0.467 / 0.521 | 1.000 / 0.994 / 0.997 | 0.999 / 0.998 / 0.999 | 0.973 / 0.995 / 0.995 |

head 组的“校验”只是记录一次时间戳，并不校验。合入阶段耗时包含重新判断，而这里没有模型，重新判断几乎瞬间完成。W3 rs_tracked 的一次运行（种子 5）耗时 1,672 ms，锁等待最长 536 ms，按实测保留。耗时是本机 WSL 上的测量，只在本实验内看比例。

### 结论

- 盲重试（现在的脚本客户端）在每次运行中都放过了每个套装的第二笔销售：W2 的 25 个套装与 W3 的 10 个套装全部违反规则。
- 每次 40001 都重新判断是安全的，但大多白费：W1 的 225 次全部无效，W2 的 125 次中 100 次、W3 的 166 次中 156 次没有改变任何东西。
- 声明读集与重新判断一样安全，且没有无效的重新判断（W1 为 0；W2 的 25 次与 W3 的 10 次全部有效），W1 的合入阶段也最短（中位 408 ms，对比 456 与 801 ms）。
- 保护效果完全取决于读集。按 0.5 的概率漏报，放过了 W2 中 25 个套装冲突里的 13 个、W3 中 10 个里的 5 个。记录全部读取是安全的，但浏览行与其他写入者的写入重叠时会多拒：5 个种子合计，W1 中 28 次、W3 中 17 次无效的重新判断。

### 重复运行与独立核查（2026-10-21）

为检验上述结果是否可靠，按登记的安排完整重跑了一次，并新增两项既不使用脚本自身计数、也不使用 `oracle.py` 的核查（`audit_calls.py`、`audit_sql.py`）。

**重复运行。** 运行 `readset_038dfeaa`，UTC 11:08:58–11:12:57（约 4 分钟），同一机器与设置。`readset.sql`、`run_readset.py`、`oracle.py` 的 SHA-256 与第一次相同；只有本文不同，因为其中已加入上面的结果。原始结果：[`results/readset/readset_038dfeaa/results.json`](../results/readset/readset_038dfeaa/results.json)，其 `calls.jsonl` 同样不纳入 git。75 次运行核验全部通过，没有失败的写入者，没有锁超时；有 10 次合入调用（s2-W1-rs_undercite 7 次，s4-W2-rs_tracked 3 次）因服务端与客户端时钟相差超过 10 ms 而不计入耗时。逐次预测在 5 次运行中再次全部成立，四条标准全部通过（合并中位数：校验 17.1 ms，`vc.merge` 36.8 ms）。

75 次运行中有 54 次的计数与第一次完全相同。由种子决定的读集与浏览行两次一致；其余 21 次只在取决于线程时序的计数上不同：

| | 第一次 | 重复运行 |
|---|---|---|
| W1 head_blind：40001 重交 | 45 ×5 | 36,45,45,45,45 |
| W3 head_blind：40001 重交 | 45,45,42,45,45 | 45 ×5 |
| W1 rs_tracked：重新判断（全部无效） | 6,4,5,7,6 | 5,7,5,7,8 |
| W3 head_rethink：重新判断（各 2 次有效） | 31,31,37,33,34 | 35,36,33,31,33 |
| W3 rs_tracked：重新判断（各 2 次有效） | 3,4,3,9,8 | 5,4,5,5,5 |
| W2 rs_undercite：规则违反 = 陈旧提交 | 3,3,2,2,3（25 个中 13 个） | 4,4,3,1,2（25 个中 14 个） |
| W3 rs_undercite：规则违反 = 陈旧提交 | 0,0,2,1,2（10 个中 5 个） | 0,0,1,1,1（10 个中 3 个） |
| 读集非空时的校验中位 | 22.0 ms（n = 302） | 21.7 ms（n = 303） |
| 最慢的一次校验 | 199.6 ms | 37.1 ms |
| 合入阶段中位：W1 head_blind / head_rethink / rs_declared | 456 / 801 / 408 ms | 518 / 779 / 395 ms |

标准所依据的格子两次完全相同：head_blind 在每次 W2、W3 运行中都放过 5 个和 2 个违反；head_rethink、rs_declared、rs_tracked 一个也没有；rs_declared 在 W1、W2、W3 中的重新判断为 0、5、2 次，没有无效的。两次合计，漏报放过了 70 个冲突中的 35 个，与漏报概率 0.5 一致；具体放过哪一个，取决于哪个写入者第二个合入，由时序决定。第一次运行中 199.6 ms 的校验和 1,672 ms 的合入阶段没有再出现。读集非空的成功合入持有 main 锁的中位为 57.1 ms，head 组为 38.8 ms，约长 47%（第一次为 52%）。合入阶段中位耗时两次之间最多相差 13.5%（W1 head_blind，456 → 518 ms）。W1、W3 中各组由快到慢的顺序不变；W2 中 rs_declared 与 rs_tracked 互换了位置（第一次 451 对 455 ms，重复运行 453 对 443 ms），这两组在 W2 中的行为本来相同。

**按原始记录重算**（`audit_calls.py`）。只用 `calls.jsonl` 中记录的 SQL 文本与返回码（定义见脚本），重建 `results.json` 中的每项计数。两次运行（分别记录 5,942 和 5,944 次调用）的全部 75 次运行、每次 8 项计数完全一致。这检查的是脚本自身的计数；`calls.jsonl` 只留在本机，所以只能在本机重跑。

**用 SQL 重新推导**（`audit_sql.py`）。第三次较小的运行（`readset_a3702d78`：种子 1，W2 与 W3，全部五组，`--keep-databases`，UTC 11:13:40–11:14:23；`complete_schedule` 为 false，不计入实验，输出未放入仓库）保留了各运行库。在每个库中只用 vc 历史重新计算陈旧提交：对合入 main 的每笔套装销售，比较同套装另一件商品在合入提交的第一父提交处与在来源提交的父提交处（即做出决定的版本）的值；规则违反直接从 `app.products` 读取。这既不用脚本记录的读取值，也不用 `oracle.py`。10 次运行全部与 `results.json` 一致；套装合计在违反规则时为 90，在一个卖家放弃时为 105，W3 中无人销售的套装为 120。该子集自己的判定显示成本一项为 false，只是因为它不含第 2 条标准需要的 W1 运行。这 10 个运行库与模板库保留在本机服务器上。

重复运行与两项核查都与上面的结果一致；结论与第 3 条标准的说明不变。

### 后续（尚未开始）

各条标准均已满足；按本文，读集校验成为版本引擎的候选改动，须作为单独的行为变更，带自己的测试与迁移。在此之前，先把校验改为增量方式：借助 `vc.row_change(table_id, pk, changeset)` 索引，只检查基线与 head 之间改过读键的提交，使校验耗时不再随表的大小增长，然后重新测量。真实 Agent 漏报的频率仍未知，属于第二阶段，需要单独的协议，并经用户同意使用模型额度。

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

## Files and running

| File | Purpose |
|---|---|
| `experiments/readset/readset.sql` | `xr` schema: `xr.lock_main`, `xr.merge_head`, `xr.merge_validated`; installed only into this experiment's template database |
| `experiments/readset/run_readset.py` | template database, 75 runs, raw JSONL of every call (`calls.jsonl`) and `results.json` (including the verdict on the criteria above) |
| `experiments/readset/oracle.py` | checks for stale commits, rule violations and wasted rethinks, and the adoption criteria |
| `experiments/readset/test_readset_oracle.py` | unit tests, including the constructed stale/fresh history; no database |
| `experiments/readset/audit_calls.py` | rebuilds every count from `calls.jsonl` alone and compares it with `results.json`; no database |
| `experiments/readset/audit_sql.py` | re-derives stale commits and rule violations from vc history alone, in run databases kept with `--keep-databases` |

```bash
python -m unittest discover -s experiments/readset -p test_readset_oracle.py -v
python experiments/readset/run_readset.py
python experiments/readset/audit_calls.py results/readset/<run>
python experiments/readset/audit_sql.py <directory of a --keep-databases run>
```

Implementation notes (2026-10-21; the predictions and criteria above are unchanged):

- Timing: the merge script first calls `xr.lock_main`, which takes main's lock and returns the time it was granted (as `scripts/benchmark_merge.py` takes the lock first); the merge functions then take it again as a no-op. An RS001 rolls back and its session settings go with it, so the error's HINT carries the time validation ended.
- In every arm the writers read the same dependency rows and the same 20 browsed rows; the arms differ only in the read set they send. Writers connect as `agentdb_client`, each with its own `db.Session`.
- The default output directory is `results/readset/<prefix>/`. Each run database is dropped once its checks are recorded (kept with `--keep-databases`); the template is kept.
- Before the registered run, a smoke run was made on 2026-10-21 (seed 1, 15 runs) only to check the harness itself; all 15 runs passed their checks. It is not counted, and its output was not added to the repository. Subsets chosen with `--seeds`, `--workloads` or `--arms` are recorded as `complete_schedule=false`.

Results will be appended to this document; the predictions and criteria above are not changed after running.

## Results (2026-10-21, 75 runs)

Run `readset_df002d50`: started 10:49:36 UTC, finished 10:53:53 UTC (4 min 17 s). PostgreSQL 16.15 (Ubuntu WSL, unix socket, READ COMMITTED, fsync and synchronous_commit on); client Windows 11 (26200), Python 3.10.11, 22 logical CPUs. Template history: 3 changesets, 4,010 row changes, 1 path.

Raw results: [`results/readset/readset_df002d50/results.json`](../results/readset/readset_df002d50/results.json), with every writer's proposals, read set, each merge attempt with its server timestamps, and the SHA-256 of the experiment sources as run. The verbatim SQL log of every call, `calls.jsonl` (7.7 MB), stays on this machine and is git-ignored; every count and timing below can be recomputed from results.json. The template database `agentdb_test_readset_df002d50_tpl` is kept; the 75 run databases were dropped after their checks.

`complete_schedule` is true. All 75 runs passed their checks; no writer failed, no lock timeout occurred, and no merge call was excluded from timing because of clock disagreement.

### Predictions vs. observed

Observed values for seeds 1–5; the predictions are in the "Predictions" section above.

| Workload | Metric | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---|---|---|---|---|---|
| W1 | 40001 resubmits | 45 ×5 | 0 | 0 | 0 | 0 |
| W1 | rethinks | 0 | 45 ×5, all wasted | 0 | 0 | 6,4,5,7,6, all wasted |
| W2 | 40001 resubmits | 45 ×5 | 0 | 0 | 0 | 0 |
| W2 | rule violations = stale commits | 5 ×5 | 0 | 0 | 3,3,2,2,3 (mean 2.6; predicted 2.5) | 0 |
| W2 | rethinks (useful) | 0 | 25 ×5 (5 useful each) | 5 ×5 (all useful) | 2,2,3,3,2 (= 5 − violations) | 5 ×5 (all useful) |
| W3 | 40001 resubmits | 45,45,42,45,45 | 0 | 0 | 0 | 0 |
| W3 | rule violations = stale commits | 2 ×5 | 0 | 0 | 0,0,2,1,2 (mean 1.0; predicted 1) | 0 |
| W3 | rethinks (useful) | 0 | 31,31,37,33,34 (2 useful each) | 2 ×5 (all useful) | 2,2,0,1,0 (= 2 − violations) | 3,4,3,9,8 (2 useful each) |

Every per-run prediction held in all 5 runs; the two averages came out at 2.6 and 1.0. In every W2 and W3 run the stale commits found from vc history equalled the rule violations found in the final rows, two independent measures. The 42 in W3 seed 3 is within the bound: a writer that re-read head only after two merges was refused once for both.

### Adoption criteria

| Criterion | Result |
|---|---|
| 1 Safety | Passed: rs_declared and rs_tracked had 0 stale commits and 0 rule violations in all 20 W2/W3 runs; head_blind matched the prediction (5 and 2) in all 10 |
| 2 Cost | Passed: rs_declared had 0 rethinks in every W1 run, and exactly 2, none wasted, in every W3 run |
| 3 Overhead | Passed as registered: pooled median of all read-set-arm validations 16.3 ms, pooled median `vc.merge` 37.5 ms; see the caveat below |
| 4 Integrity | Passed: all 75 runs passed their checks; no writer failed |

**Caveat on criterion 3.** The registered criterion compares pooled medians over every validation in the read-set arms, and 39% of them (193 of 495) had an empty read set: the W1 writers in rs_declared and rs_undercite, and writers whose only dependency was omitted. Those validations skipped the comparison and took 1.6 ms median. With a non-empty read set (302 validations), validation took 22.0 ms median, about 59% of the `vc.merge` median. A direct measurement agrees: a successful merge with a non-empty read set held main's lock (lock acquired → before COMMIT) for 58.9 ms median, against 38.7 ms in the head arms, about 52% longer. The criterion still holds on these numbers, but with much less margin than the pooled median suggests. Validation time does not depend on the number of keys (22.8 ms median with 1–2 keys, 21.6 ms with 20 or more), because `vc.diff_rows` replays both versions of the whole table; the slowest validation took 199.6 ms (rs_tracked).

### Timing

| | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---:|---:|---:|---:|---:|
| Validation, median (ms) | 0.24 | 0.21 | 2.7 | 1.75 | 21.6 |
| Validation, p90 / max (ms) | 0.34 / 0.46 | 0.32 / 1.15 | 25.4 / 38.0 | 23.7 / 34.8 | 29.0 / 199.6 |
| `vc.merge`, median (ms) | 38.6 | 37.9 | 38.2 | 38.7 | 36.2 |
| Merge phase, median (ms): W1 / W2 / W3 | 456 / 536 / 517 | 801 / 479 / 734 | 408 / 451 / 473 | 408 / 470 / 447 | 642 / 455 / 646 |
| Share of the phase main is locked (median): W1 / W2 / W3 | 0.803 / 0.819 / 0.805 | 0.463 / 0.467 / 0.521 | 1.000 / 0.994 / 0.997 | 0.999 / 0.998 / 0.999 | 0.973 / 0.995 / 0.995 |

The head arms' "validation" only records a timestamp; nothing is validated. Merge-phase times include rethinks, which are nearly instant here because there is no model. One W3 rs_tracked run (seed 5) took 1,672 ms, with lock waits up to 536 ms; it is kept as measured. Timings are local WSL measurements; compare ratios within this experiment.

### What the results show

- Blind retry (today's scripted client) let the second sale of every bundle through in every run: all 25 W2 bundles and all 10 W3 bundles broke the rule.
- Rethinking on every 40001 was safe but mostly wasted: all 225 rethinks in W1, 100 of 125 in W2 and 156 of 166 in W3 changed nothing.
- A declared read set was as safe as rethinking, with no wasted rethinks (0 in W1; all 25 in W2 and all 10 in W3 useful), and had the shortest W1 merge phase (408 ms median, against 456 and 801 ms).
- The protection is exactly as good as the read set. Omitting each dependency with probability 0.5 let 13 of 25 W2 bundle conflicts and 5 of 10 W3 conflicts through. Recording every read was safe but over-rejected whenever browsed rows overlapped another writer's writes: 28 wasted rethinks in W1 and 17 in W3 across the five seeds.

### Replication and independent checks (2026-10-21)

To check that the results above are reliable, the registered schedule was run a second time, and two checks were added that use neither the harness's own counters nor `oracle.py` (`audit_calls.py`, `audit_sql.py`).

**Replication.** Run `readset_038dfeaa`, 11:08:58–11:12:57 UTC (about 4 minutes), on the same machine and settings. The SHA-256 of `readset.sql`, `run_readset.py` and `oracle.py` match the first run; only this document differed, because the results above had been added to it. Raw results: [`results/readset/readset_038dfeaa/results.json`](../results/readset/readset_038dfeaa/results.json); its `calls.jsonl` is git-ignored like the first. All 75 runs passed their checks, with no failed writer and no lock timeout; 10 merge calls (7 in s2-W1-rs_undercite, 3 in s4-W2-rs_tracked) were left out of the timing because the server and client clocks disagreed by more than 10 ms. Every per-run prediction held again in all 5 runs, and all four criteria passed (pooled medians: validation 17.1 ms, `vc.merge` 36.8 ms).

54 of the 75 runs produced exactly the same counts as in the first run. The seeded read sets and browsed rows were identical; the other 21 runs differ only in counts that depend on thread timing:

| | First run | Replication |
|---|---|---|
| W1 head_blind: 40001 resubmits | 45 ×5 | 36,45,45,45,45 |
| W3 head_blind: 40001 resubmits | 45,45,42,45,45 | 45 ×5 |
| W1 rs_tracked: rethinks (all wasted) | 6,4,5,7,6 | 5,7,5,7,8 |
| W3 head_rethink: rethinks (2 useful each) | 31,31,37,33,34 | 35,36,33,31,33 |
| W3 rs_tracked: rethinks (2 useful each) | 3,4,3,9,8 | 5,4,5,5,5 |
| W2 rs_undercite: rule violations = stale commits | 3,3,2,2,3 (13 of 25) | 4,4,3,1,2 (14 of 25) |
| W3 rs_undercite: rule violations = stale commits | 0,0,2,1,2 (5 of 10) | 0,0,1,1,1 (3 of 10) |
| Validation with a non-empty read set, median | 22.0 ms (n = 302) | 21.7 ms (n = 303) |
| Slowest validation | 199.6 ms | 37.1 ms |
| Merge phase, median: W1 head_blind / head_rethink / rs_declared | 456 / 801 / 408 ms | 518 / 779 / 395 ms |

The cells the criteria rest on were identical in both runs: head_blind let 5 and 2 violations through in every W2 and W3 run; head_rethink, rs_declared and rs_tracked let none through; rs_declared needed 0, 5 and 2 rethinks in W1, W2 and W3, none wasted. Over both runs, under-citation let 35 of 70 conflicts through, the omission rate of 0.5; which ones get through depends on which writer merges second, and timing decides that. The first run's 199.6 ms validation and 1,672 ms merge phase did not recur. Successful merges with a non-empty read set held main's lock for 57.1 ms median, against 38.8 ms in the head arms, about 47% longer (52% in the first run). Merge-phase medians moved by up to 13.5% between the runs (W1 head_blind, 456 → 518 ms). The arms kept the same order from fastest to slowest in W1 and W3; in W2, rs_declared and rs_tracked swapped places (451 vs 455 ms in the first run, 453 vs 443 ms in the replication), two arms that behave identically in W2.

**Recount from the raw log** (`audit_calls.py`). Every count in `results.json` was rebuilt from `calls.jsonl` alone, using only the logged SQL text and return codes (definitions in the script). For both runs (5,942 and 5,944 logged calls), all 8 counts matched in all 75 runs. This checks the harness's own bookkeeping; `calls.jsonl` stays on this machine, so the recount can only be rerun here.

**Re-derivation by SQL** (`audit_sql.py`). A third, smaller run (`readset_a3702d78`: seed 1, W2 and W3, all five arms, `--keep-databases`, 11:13:40–11:14:23 UTC; `complete_schedule` false, not counted, output not added to the repository) kept its run databases. In each, stale commits were recomputed from vc history alone: for every bundle sale merged into main, the other product of the bundle was compared between the merge's first parent and the parent of the source commit, the version the sale was decided on; rule violations were read directly from `app.products`. This uses neither the harness's recorded reads nor `oracle.py`. All 10 runs matched `results.json`; bundle totals were 90 where the rule was broken, 105 where one seller held back, and 120 for the W3 bundles nobody sells from. That subset's own criteria output shows cost as false only because it has no W1 runs, which criterion 2 needs. Its 10 run databases and template are kept on the local server.

The replication and both checks agree with the results above; the conclusions and the caveat on criterion 3 stand.

### Next steps (not started)

All criteria passed, so by this document read-set validation becomes a candidate change to the version engine, to be made as a separate behavior change with its own tests and migration. Before that, validation should become incremental: check only the commits between base and head that changed a read key, using the `vc.row_change(table_id, pk, changeset)` index, so its cost no longer grows with table size; then measure again. How often real Agents under-report their dependencies is still unknown; that is phase 2, which needs its own protocol and the user's approval to use model quota.
