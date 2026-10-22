中文 | [English](#english-version)

# 读集校验与陈旧 head 检查：实验设计

状态：2026-10-21 在编写任何代码之前登记（提交 4db8267），同日实现并运行，结果见下文。**仅为本机合成负载**：脚本写入者，不是大模型 Agent；不代表生产容量，不访问任何云服务。

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
| `experiments/readset/test_readset_sql.py` | `xr` 函数的契约测试；自建并在结束时删除 `agentdb_test_xrcontract_*` 库 |
| `experiments/readset/check_mutations.py` | 在临时副本中做单行变异，确认测试会失败；`--sql` 时还变异 `readset.sql`（需要数据库） |

```bash
python -m unittest discover -s experiments/readset -p test_readset_oracle.py -v
python -m unittest discover -s experiments/readset -p test_readset_sql.py -v
python experiments/readset/check_mutations.py --sql
python experiments/readset/run_readset.py
python experiments/readset/audit_calls.py results/readset/<run>
python experiments/readset/audit_sql.py <directory of a --keep-databases run>
```

实现说明（2026-10-21，不改变上面的预测与标准）：

- 计时：合入脚本先调用 `xr.lock_main` 取得 main 的锁，它返回取锁时刻（同 `scripts/benchmark_merge.py` 先取锁的做法）；之后合入函数再取锁即为空操作。RS001 回滚后会话设置随之消失，所以由错误的 HINT 携带校验结束时刻。
- 所有组的写入者读取相同的依赖行和 20 行浏览行，各组只在发送的读集上不同。写入者以 `agentdb_client` 身份连接，各用一个 `db.Session`。
- 默认输出目录为 `results/readset/<前缀>/`。每次运行的库在核验记录后删除（`--keep-databases` 则保留），模板库保留。
- `--seeds`、`--workloads`、`--arms` 选出的子集在结果中标记为 `complete_schedule=false`，不计入实验。

结果追加到本文；上面的预测与标准在运行后不修改。

## 结果（2026-10-21）

所有运行使用同一机器与设置：PostgreSQL 16.15（Ubuntu WSL，unix socket，READ COMMITTED，fsync 与 synchronous_commit 开启），客户端 Windows 11（26200）、Python 3.10.11、22 个逻辑 CPU。模板历史：3 个 changeset、4,010 条行变更、1 个路径。每个 `results.json` 含每个写入者的提案、读集、带服务端时间戳的每次合入尝试，以及运行时各实验源文件的 SHA-256，下文所有计数与耗时都能从中重算；逐字记录全部 SQL 的 `calls.jsonl`（登记运行为 7.7 MB）只留在本机，已加入 .gitignore。

| 运行 | 时间（UTC） | 内容 | 结果 |
|---|---|---|---|
| 冒烟，`readset_8e903d65` | 登记运行之前 | 种子 1，15 次，只检查脚本本身；不计入，输出未放入仓库 | 15 次核验全部通过 |
| 登记运行，[`readset_df002d50`](../results/readset/readset_df002d50/results.json) | 10:49:36–10:53:53 | 登记的安排，75 次；`complete_schedule` 为 true | 75 次核验全部通过；没有失败的写入者，没有锁超时，没有合入调用被排除在计时之外 |
| 重复运行，[`readset_038dfeaa`](../results/readset/readset_038dfeaa/results.json) | 11:08:58–11:12:57 | 同一安排再跑一次，检验可靠性；`readset.sql`、`run_readset.py`、`oracle.py` 的 SHA-256 与登记运行相同，只有本文不同（其中已加入结果） | 75 次核验全部通过；没有失败的写入者，没有锁超时；10 次合入调用（s2-W1-rs_undercite 7 次，s4-W2-rs_tracked 3 次）因服务端与客户端时钟相差超过 10 ms 而不计入耗时 |
| SQL 核查，`readset_a3702d78` | 11:13:40–11:14:23 | 种子 1，W2 与 W3，全部五组，`--keep-databases`；不计入，输出未放入仓库 | 10 次核验全部通过；运行库保留，供 `audit_sql.py` 使用 |

### 实测计数

按种子 1–5 列出；重复运行与登记运行不同时，其值写在“/”之后。预测见上文“预测”一节。

| 负载 | 指标 | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---|---|---|---|---|---|
| W1 | 40001 重交 | 45 ×5 / 36,45,45,45,45 | 0 | 0 | 0 | 0 |
| W1 | 重新判断 | 0 | 45 ×5，全部无效 | 0 | 0 | 6,4,5,7,6 / 5,7,5,7,8，全部无效 |
| W2 | 40001 重交 | 45 ×5 | 0 | 0 | 0 | 0 |
| W2 | 规则违反 = 陈旧提交 | 5 ×5 | 0 | 0 | 3,3,2,2,3（平均 2.6）/ 4,4,3,1,2（平均 2.8）；预测 2.5 | 0 |
| W2 | 重新判断（有效） | 0 | 25 ×5（各 5 次有效） | 5 ×5（全部有效） | 2,2,3,3,2 / 1,1,2,4,3（= 5 − 违反数） | 5 ×5（全部有效） |
| W3 | 40001 重交 | 45,45,42,45,45 / 45 ×5 | 0 | 0 | 0 | 0 |
| W3 | 规则违反 = 陈旧提交 | 2 ×5 | 0 | 0 | 0,0,2,1,2（平均 1.0）/ 0,0,1,1,1（平均 0.6）；预测 1 | 0 |
| W3 | 重新判断（有效） | 0 | 31,31,37,33,34 / 35,36,33,31,33（各 2 次有效） | 2 ×5（全部有效） | 2,2,0,1,0 / 2,2,1,1,1（= 2 − 违反数） | 3,4,3,9,8 / 5,4,5,5,5（各 2 次有效） |

两次运行中，逐次预测在 5 个种子里全部成立。每次 W2、W3 运行中，从 vc 历史判定的陈旧提交数都等于从最终数据得出的规则违反数，这是两个独立的测量。低于 45 的重交次数仍在上界之内：写入者在几次合入之后才重读 head，这几次合入只让它被拒一次。重复运行的 75 次中有 54 次的计数与登记运行完全相同；由种子决定的读集与浏览行两次一致，其余 21 次只在取决于线程时序的计数上不同。

### 采纳标准

两次运行中四条标准全部通过。第 3 条按登记的定义比较合并中位数：校验 16.3 ms 对 `vc.merge` 37.5 ms（登记运行），17.1 对 36.8 ms（重复运行）。

**关于第 3 条的说明。** 合并中位数包含读集为空的校验：登记运行中占 39%（495 次中的 193 次），即 rs_declared 与 rs_undercite 中的 W1 写入者，以及唯一的依赖被漏报的写入者。这些校验跳过了比较，中位只有 1.6 ms。读集非空时，校验中位为 22.0 ms（302 次；重复运行 21.7 ms，303 次），约为 `vc.merge` 中位数的 59%。直接测量的结果相近：读集非空的成功合入持有 main 锁（取锁 → COMMIT 前）的中位为 58.9 ms，head 组为 38.7 ms，约长 52%（重复运行 57.1 对 38.8 ms，约长 47%）。按这些数字该标准仍然成立，但余量远小于合并中位数所显示的。校验耗时与键的数量无关：1–2 个键时中位为 22.8 ms，与 rs_tracked 20 个以上键时（见耗时表）相当，因为 `vc.diff_rows` 回放两个版本的整张表。

### 耗时

除标注外均为登记运行。

| | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---:|---:|---:|---:|---:|
| 校验中位（ms） | 0.24 | 0.21 | 2.7 | 1.75 | 21.6 |
| 校验 p90 / 最大（ms） | 0.34 / 0.46 | 0.32 / 1.15 | 25.4 / 38.0 | 23.7 / 34.8 | 29.0 / 199.6 |
| `vc.merge` 中位（ms） | 38.6 | 37.9 | 38.2 | 38.7 | 36.2 |
| 合入阶段中位（ms）：W1 / W2 / W3 | 456 / 536 / 517 | 801 / 479 / 734 | 408 / 451 / 473 | 408 / 470 / 447 | 642 / 455 / 646 |
| 重复运行的合入阶段中位（ms）：W1 / W2 / W3 | 518 / 533 / 533 | 779 / 532 / 792 | 395 / 453 / 438 | 404 / 479 / 436 | 688 / 443 / 686 |
| main 被锁定的时间占比（中位）：W1 / W2 / W3 | 0.803 / 0.819 / 0.805 | 0.463 / 0.467 / 0.521 | 1.000 / 0.994 / 0.997 | 0.999 / 0.998 / 0.999 | 0.973 / 0.995 / 0.995 |

head 组的“校验”只是记录一次时间戳，并不校验。合入阶段耗时包含重新判断，而这里没有模型，重新判断几乎瞬间完成。登记运行的两个离群值按实测保留：最慢的那次校验，以及 W3 rs_tracked 一次运行（种子 5）1,672 ms 的合入阶段（锁等待最长 536 ms）；两者都没有在重复运行中再出现，重复运行最慢的校验为 37.1 ms。合入阶段中位耗时两次之间最多相差 13.5%（W1 head_blind）。W1、W3 中各组由快到慢的顺序不变；W2 中行为本来相同的 rs_declared 与 rs_tracked 互换了位置。耗时是本机 WSL 上的测量，只在本实验内看比例。

### 独立核查

两项核查既不使用脚本自身的计数，也不使用 `oracle.py`：

- **按原始记录重算**（`audit_calls.py`）：两次完整运行（分别记录 5,942 和 5,944 次调用）的全部 75 次运行、每次 8 项计数都与 `results.json` 一致。这检查的是脚本自身的计数；`calls.jsonl` 只留在本机，所以只能在本机重跑。
- **用 SQL 重新推导**（`audit_sql.py`）：只用 vc 历史，比较同套装另一件商品在合入提交的第一父提交处与在做出决定的版本处的值；SQL 核查运行的 10 个库全部与 `results.json` 一致。套装合计在违反规则时为 90，在一个卖家放弃时为 105，W3 中无人销售的套装为 120。该运行自己的判定显示成本一项为 false，只是因为它不含第 2 条标准需要的 W1 运行。它的 10 个运行库与模板库保留在本机服务器上。

### 结论

- 盲重试（现在的脚本客户端）在每次运行中都放过了每个套装的第二笔销售。
- 每次 40001 都重新判断是安全的，但大多白费：W1 的重新判断全部无效，W2、W3 的大多数也没有改变任何东西。
- 声明读集与重新判断一样安全，没有无效的重新判断，W1 的合入阶段也最短。
- 保护效果完全取决于读集。按 0.5 的概率漏报，两次合计放过了 70 个冲突中的 35 个；具体放过哪一个，取决于哪个写入者第二个合入，由时序决定。记录全部读取是安全的，但浏览行与其他写入者的写入重叠时会多拒。
- 重复运行与两项核查都与上述结果一致；第 3 条标准的说明不变。

### 后续

已于 2026-10-21 完成：迁移 008 把读集校验作为 `vc.merge_validated` 加入版本引擎，按键增量查找代替整表回放，并修复下面已知问题中的第 1、2 条（见 [安装与升级](SETUP.md)）。之后的审查又修复了三处：键的值必须与历史记录的主键完全一致（如整数 id 写成 `"3204"` 原先会被当作未变而放行，现报 22023）；合并基为 NULL 时仍做校验；`run_tests.py` 新增两个会话的竞态用例，覆盖下面的第 3 条（把 `FOR UPDATE` 去掉的变异会被它拦下，第二个合入改报 40001）。实现的耗时用 `scripts/benchmark_read_set_merge.py` 重新测量（[结果](../results/read-set-merge-benchmark.json)）：4,000 行、快照后 10–1,000 个提交、1–100 个读键时，校验本身中位 1.192–16.159 ms，为同次 `vc.merge` 的 2–28%；整个 `merge_validated`（含 2026-10-22 加入的写过行校验）比 `vc.merge` 至多多 22.5 ms。测量中发现 `_rows_at_keys` 的默认行数估计（1,000 行）使校验查询的估算代价超过 `jit_above_cost`，每次调用都被 JIT 编译，空读集也多出约 10 ms；已用 `ROWS 10` 修正。下面计划中的增量方式没有实现：校验分别回放基线与 head 的第一父链，只查 head 约占一半，按上述耗时不值得为此增加复杂度。2026-10-22 的审查又发现：实验约定读集不含写过的行，而 `vc.merge` 把目标已有相同结果的行当作已应用跳过，于是两个分支各把最后一件库存从 1 改为 0、各记一笔订单时，两笔都能合入；现在 `merge_validated` 自动把源分支写过的行按读校验，这种情况报 RS001（`run_tests.py` 的 `two_sales_of_the_last_unit_refuse_the_second`）；上述竞态用例也改为先确认第二个合入确实在等锁、再让第一个提交。以下为当时写下的计划。

各条标准均已满足；按本文，读集校验成为版本引擎的候选改动，须作为单独的行为变更，带自己的测试与迁移。在此之前，先把校验改为增量方式：借助 `vc.row_change(table_id, pk, changeset)` 索引，只检查基线与 head 之间改过读键的提交，使校验耗时不再随表的大小增长，然后重新测量。真实 Agent 漏报的频率仍未知，属于第二阶段，需要单独的协议，并经用户同意使用模型额度。

## 测试记录与已知问题（2026-10-21）

对照模块重构框架（作者的本地文档，不在本仓库中）补做的检查：

- **基线**：在 a32e61f 上运行现有套件，全部通过：`run_tests.py` 21 项（含原 18 项断言，以及十写入者竞争中 1 个成功、9 个陈旧 head），`tests/` 79 项，`experiments/gpt_repeat` 7 项，`review_local.py` 全部检查（十个分支全部合入、45 次陈旧 head 重试、锁超时与两个中断窗口）。本实验的两次提交没有改动这些套件涉及的任何文件，所以这也就是改动之前的基线。
- **变异检查**（`check_mutations.py`）：第一次用 30 处单行变异检查原有的 17 个单元测试，只拦下 13 处，未达到框架第一阶段的门槛。补上边界值、判定标准、时钟检查、原始记录重算的测试，以及一个黄金样本测试（用已登记运行的 `results.json` 核对 750 个写入者由种子决定的读集与浏览行）之后，31 个单元测试拦下全部 30 处。`--sql` 再对 `readset.sql` 做 5 处变异，13 个契约测试全部拦下。合计 35 处，0 处存活。
- **契约测试**（`test_readset_sql.py`，13 项）：在专用库中以 `agentdb_client` 身份记录 `xr` 函数的现有行为：首个合入不做校验；读键变化报 RS001，DETAIL 含基线值与当前值，main 不动；读键未变时即使 main 已前进也能合入；改后又改回不算冲突；空读集跳过校验；写写冲突仍由 `vc.merge` 报 P0001；读冲突先于写冲突报告；`xr.merge_head` 保留 40001；未知分支被拒；排队超过 lock_timeout 报 55P03 且 main 不变；三个函数都是 SECURITY DEFINER、search_path 固定、只有 `agentdb_client` 可执行。

已知问题。这里只记录，不在本次修改；若读集校验进入版本引擎，须在那次单独的改动中先修复：

1. 格式错误的 `p_reads`（不是对象、为 NULL、值不是数组）报 P0001，而 P0001 也是 `vc.merge` 行/文件冲突的错误码，调用方无法仅凭 SQLSTATE 区分输入错误与冲突，`run_readset.py` 会把它当作冲突去重新判断。应改用 22023（invalid_parameter_value）。
2. `p_reads` 中不存在的表 id 在 main 未前进时被静默忽略（基线等于 head，什么也不校验），main 前进后才报 P0001 “Unknown revision or table”；不是 uuid 的键报 22P02。应在开头就按 `vc.tracked_table` 校验表 id。
3. 先取锁、后校验的顺序没有契约测试覆盖：`vc.merge` 本身也会取同一把锁，只有并发竞态才会暴露。目前的证据是实验中 W1 rs_declared 的重新判断为 0。

以上三条已在 008 中处理：1、2 由开头的 22023 校验修复，3 由 `run_tests.py` 的 `crossing_read_sets_at_the_same_time_refuse_the_second` 覆盖。

## 回滚方案

本实验没有修改现有代码、数据格式或项目数据库，各部分可以单独撤销：

| 部分 | 撤销方法 | 核对 |
|---|---|---|
| 实验提交（4db8267–176572b） | `git revert` 对应提交；被改动的现有文件只有 `.gitignore`（新增 4 行）与 `AGENTS.md`（新增表格行与说明） | `git diff 8537cbb 176572b -- sql db.py agent_lab.py agent_lab_gateway.py review_local.py run_tests.py agent_state tests scripts` 为空（2026-10-21 已核对）；之后的 008 提交改动了这些路径，见最后一行 |
| `experiments/readset/` | `git revert`，或删除该目录；目录外没有代码导入它 | 在该目录、本文与 `results/` 之外，只有 `AGENTS.md` 与 `.gitignore` 提到 readset（2026-10-21 已核对） |
| `xr` schema | 用 `DROP DATABASE … WITH (FORCE)` 删除 `agentdb_test_readset_*` 测试库；契约测试的库在测试结束时已删除 | 项目库 `agentdb` 中没有 `xr` schema（2026-10-21 已核对） |
| 结果 | 删除 `results/readset/<运行>/` | — |
| 版本引擎（迁移 008） | 008 只新增函数、不改 `vc.merge`，无数据需要恢复，经它完成的合入是普通的 `vc.merge` 提交。以数据库所有者执行 `BEGIN; DROP FUNCTION vc.merge_validated(text,uuid,jsonb,text); DROP FUNCTION vc._rows_at_keys(uuid,uuid,jsonb[]); COMMIT;`，再撤销 008 的提交（b1b9505 及之后） | 2026-10-21 在新安装（`agentdb_test_rsback_4c2886203a`）上执行：vc 函数由 34 个变为 32 个，其余 32 个属性不变；重新执行 008 后 34 个与新安装一致 |

---

<a id="english-version"></a>

# Read-Set Validation vs. the Stale-Head Check: Experiment Design

Status: registered on 2026-10-21 before any code was written (commit 4db8267), then implemented and run the same day; results below. **Local synthetic load only**: scripted writers, not large-language-model Agents; it does not represent production capacity, and no cloud services are accessed.

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
| `experiments/readset/test_readset_sql.py` | contract tests for the `xr` functions; builds an `agentdb_test_xrcontract_*` database and drops it at the end |
| `experiments/readset/check_mutations.py` | one-line mutations in a temporary copy, to confirm the tests fail; `--sql` also mutates `readset.sql` (needs the database) |

```bash
python -m unittest discover -s experiments/readset -p test_readset_oracle.py -v
python -m unittest discover -s experiments/readset -p test_readset_sql.py -v
python experiments/readset/check_mutations.py --sql
python experiments/readset/run_readset.py
python experiments/readset/audit_calls.py results/readset/<run>
python experiments/readset/audit_sql.py <directory of a --keep-databases run>
```

Implementation notes (2026-10-21; the predictions and criteria above are unchanged):

- Timing: the merge script first calls `xr.lock_main`, which takes main's lock and returns the time it was granted (as `scripts/benchmark_merge.py` takes the lock first); the merge functions then take it again as a no-op. An RS001 rolls back and its session settings go with it, so the error's HINT carries the time validation ended.
- In every arm the writers read the same dependency rows and the same 20 browsed rows; the arms differ only in the read set they send. Writers connect as `agentdb_client`, each with its own `db.Session`.
- The default output directory is `results/readset/<prefix>/`. Each run database is dropped once its checks are recorded (kept with `--keep-databases`); the template is kept.
- Subsets chosen with `--seeds`, `--workloads` or `--arms` are recorded as `complete_schedule=false` and not counted.

Results will be appended to this document; the predictions and criteria above are not changed after running.

## Results (2026-10-21)

All runs used the same machine and settings: PostgreSQL 16.15 (Ubuntu WSL, unix socket, READ COMMITTED, fsync and synchronous_commit on); client Windows 11 (26200), Python 3.10.11, 22 logical CPUs. Template history: 3 changesets, 4,010 row changes, 1 path. Each `results.json` holds every writer's proposals, read set, each merge attempt with its server timestamps, and the SHA-256 of the experiment sources as run, so every count and timing below can be recomputed from it; the verbatim SQL log `calls.jsonl` (7.7 MB for the registered run) stays on this machine and is git-ignored.

| Run | Time (UTC) | What | Outcome |
|---|---|---|---|
| smoke, `readset_8e903d65` | before the registered run | seed 1, 15 runs, only to check the harness; not counted, output not in the repository | all 15 passed their checks |
| registered, [`readset_df002d50`](../results/readset/readset_df002d50/results.json) | 10:49:36–10:53:53 | the registered schedule, 75 runs; `complete_schedule` true | all 75 passed their checks; no failed writer, no lock timeout, no merge call left out of timing |
| replication, [`readset_038dfeaa`](../results/readset/readset_038dfeaa/results.json) | 11:08:58–11:12:57 | the same schedule again, to check reliability; same SHA-256 of `readset.sql`, `run_readset.py` and `oracle.py`, only this document differed (results had been added) | all 75 passed their checks; no failed writer, no lock timeout; 10 merge calls (7 in s2-W1-rs_undercite, 3 in s4-W2-rs_tracked) left out of timing because the server and client clocks disagreed by more than 10 ms |
| SQL audit, `readset_a3702d78` | 11:13:40–11:14:23 | seed 1, W2 and W3, all five arms, `--keep-databases`; not counted, output not in the repository | all 10 passed their checks; run databases kept for `audit_sql.py` |

### Observed counts

Values for seeds 1–5; where the replication differs from the registered run, its values follow the "/". The predictions are in the "Predictions" section above.

| Workload | Metric | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---|---|---|---|---|---|
| W1 | 40001 resubmits | 45 ×5 / 36,45,45,45,45 | 0 | 0 | 0 | 0 |
| W1 | rethinks | 0 | 45 ×5, all wasted | 0 | 0 | 6,4,5,7,6 / 5,7,5,7,8, all wasted |
| W2 | 40001 resubmits | 45 ×5 | 0 | 0 | 0 | 0 |
| W2 | rule violations = stale commits | 5 ×5 | 0 | 0 | 3,3,2,2,3 (mean 2.6) / 4,4,3,1,2 (mean 2.8); predicted 2.5 | 0 |
| W2 | rethinks (useful) | 0 | 25 ×5 (5 useful each) | 5 ×5 (all useful) | 2,2,3,3,2 / 1,1,2,4,3 (= 5 − violations) | 5 ×5 (all useful) |
| W3 | 40001 resubmits | 45,45,42,45,45 / 45 ×5 | 0 | 0 | 0 | 0 |
| W3 | rule violations = stale commits | 2 ×5 | 0 | 0 | 0,0,2,1,2 (mean 1.0) / 0,0,1,1,1 (mean 0.6); predicted 1 | 0 |
| W3 | rethinks (useful) | 0 | 31,31,37,33,34 / 35,36,33,31,33 (2 useful each) | 2 ×5 (all useful) | 2,2,0,1,0 / 2,2,1,1,1 (= 2 − violations) | 3,4,3,9,8 / 5,4,5,5,5 (2 useful each) |

In both runs, every per-run prediction held in all 5 seeds. In every W2 and W3 run, the stale commits found from vc history equalled the rule violations found in the final rows, two independent measures. Resubmit counts below 45 are within the bound: a writer that re-read head only after several merges was refused once for all of them. 54 of the replication's 75 runs produced exactly the same counts as the registered run; the seeded read sets and browsed rows were identical, and the other 21 differ only in counts that depend on thread timing.

### Adoption criteria

All four criteria passed in both runs. Criterion 3, as registered, compares pooled medians: validation 16.3 ms against `vc.merge` 37.5 ms in the registered run, 17.1 against 36.8 ms in the replication.

**Caveat on criterion 3.** The pooled medians include validations with an empty read set: 39% of the registered run's (193 of 495), from the W1 writers in rs_declared and rs_undercite and from writers whose only dependency was omitted. Those skipped the comparison and took 1.6 ms median. With a non-empty read set, validation took 22.0 ms median over 302 validations (21.7 ms over 303 in the replication), about 59% of the `vc.merge` median. A direct measurement agrees: a successful merge with a non-empty read set held main's lock (lock acquired → before COMMIT) for 58.9 ms median, against 38.7 ms in the head arms, about 52% longer (57.1 against 38.8 ms, about 47%, in the replication). The criterion still holds on these numbers, but with much less margin than the pooled medians suggest. Validation time does not depend on the number of keys: 22.8 ms median with 1–2 keys, about the same as rs_tracked's 20 or more (timing table), because `vc.diff_rows` replays both versions of the whole table.

### Timing

Registered run unless marked.

| | head_blind | head_rethink | rs_declared | rs_undercite | rs_tracked |
|---|---:|---:|---:|---:|---:|
| Validation, median (ms) | 0.24 | 0.21 | 2.7 | 1.75 | 21.6 |
| Validation, p90 / max (ms) | 0.34 / 0.46 | 0.32 / 1.15 | 25.4 / 38.0 | 23.7 / 34.8 | 29.0 / 199.6 |
| `vc.merge`, median (ms) | 38.6 | 37.9 | 38.2 | 38.7 | 36.2 |
| Merge phase, median (ms): W1 / W2 / W3 | 456 / 536 / 517 | 801 / 479 / 734 | 408 / 451 / 473 | 408 / 470 / 447 | 642 / 455 / 646 |
| Merge phase in the replication, median (ms): W1 / W2 / W3 | 518 / 533 / 533 | 779 / 532 / 792 | 395 / 453 / 438 | 404 / 479 / 436 | 688 / 443 / 686 |
| Share of the phase main is locked (median): W1 / W2 / W3 | 0.803 / 0.819 / 0.805 | 0.463 / 0.467 / 0.521 | 1.000 / 0.994 / 0.997 | 0.999 / 0.998 / 0.999 | 0.973 / 0.995 / 0.995 |

The head arms' "validation" only records a timestamp; nothing is validated. Merge-phase times include rethinks, which are nearly instant here because there is no model. Two outliers of the registered run are kept as measured: its slowest validation, and a 1,672 ms W3 rs_tracked merge phase (seed 5, lock waits up to 536 ms); neither recurred in the replication, whose slowest validation took 37.1 ms. Merge-phase medians moved by up to 13.5% between the runs (W1 head_blind). The arms kept the same order from fastest to slowest in W1 and W3; in W2, rs_declared and rs_tracked, which behave identically there, swapped places. Timings are local WSL measurements; compare ratios within this experiment.

### Independent checks

Neither check uses the harness's own counters or `oracle.py`:

- **Recount from the raw log** (`audit_calls.py`): for both full runs (5,942 and 5,944 logged calls), all 8 counts matched `results.json` in all 75 runs. This checks the harness's own bookkeeping; `calls.jsonl` stays on this machine, so the recount can only be rerun here.
- **Re-derivation by SQL** (`audit_sql.py`): from vc history alone, comparing the other product of each bundle at the merge's first parent with its value at the version the sale was decided on, all 10 databases of the SQL audit run matched `results.json`. Bundle totals were 90 where the rule was broken, 105 where one seller held back, and 120 for the W3 bundles nobody sells from. That run's own criteria output shows cost as false only because it has no W1 runs, which criterion 2 needs. Its 10 run databases and template are kept on the local server.

### What the results show

- Blind retry (today's scripted client) let the second sale of every bundle through in every run.
- Rethinking on every 40001 was safe but mostly wasted: every W1 rethink, and most W2 and W3 rethinks, changed nothing.
- A declared read set was as safe as rethinking, with no wasted rethinks, and had the shortest W1 merge phase.
- The protection is exactly as good as the read set. Omitting each dependency with probability 0.5 let 35 of 70 conflicts through over both runs; which ones depends on which writer merges second, and timing decides that. Recording every read was safe but over-rejected whenever browsed rows overlapped another writer's writes.
- The replication and both checks agree with these results; the caveat on criterion 3 stands.

### Next steps

Done on 2026-10-21: migration 008 adds read-set validation to the version engine as `vc.merge_validated`, with per-key lookups instead of whole-table replay, and fixes known issues 1 and 2 below (see [Setup and upgrades](SETUP.md)). A later review fixed three more points: a key's values must equal the primary key exactly as history records it (an integer id written as `"3204"` used to pass as unchanged; it now raises 22023); a NULL merge base is still checked; and `run_tests.py` gained a two-session race that covers known issue 3 below (a mutation that drops `FOR UPDATE` is caught by it: the second merge then reports 40001). The implementation was measured with `scripts/benchmark_read_set_merge.py` ([results](../results/read-set-merge-benchmark.json)): with 4,000 rows, 10–1,000 commits since the snapshot and 1–100 read keys, the check itself takes a median 1.192–16.159 ms, 2–28% of the same `vc.merge`, and the whole `merge_validated`, including the written-row check added on 2026-10-22, takes at most 22.5 ms more than `vc.merge`. The measurement found that `_rows_at_keys`'s default row estimate (1,000) put the check's estimated cost above `jit_above_cost`, so every call was JIT-compiled, about 10 ms more even for an empty read set; `ROWS 10` fixes that. The incremental check planned below was not built: the check replays the first-parent lineage at the base and at the head, the head alone is about half, and at these times it does not justify the added complexity. A review on 2026-10-22 then found that the experiment's convention leaves written rows out of the read set while `vc.merge` skips a row the target already holds with the same result, so two branches that each take the last unit of stock from 1 to 0 and record an order both merged; `merge_validated` now checks the rows the source wrote as read, and that case raises RS001 (`two_sales_of_the_last_unit_refuse_the_second` in `run_tests.py`). The race case above now also confirms that the second merge is waiting on the lock before the first commits. The plan as written before follows.

All criteria passed, so by this document read-set validation becomes a candidate change to the version engine, to be made as a separate behavior change with its own tests and migration. Before that, validation should become incremental: check only the commits between base and head that changed a read key, using the `vc.row_change(table_id, pk, changeset)` index, so its cost no longer grows with table size; then measure again. How often real Agents under-report their dependencies is still unknown; that is phase 2, which needs its own protocol and the user's approval to use model quota.

## Test record and known issues (2026-10-21)

Checks added against the module refactoring framework (the author's local document, not in this repository):

- **Baseline**: the existing suites, run on a32e61f, all pass: `run_tests.py` 21 items (including the original 18 assertions, and 1 winner with 9 stale heads in the ten-writer race), `tests/` 79 tests, `experiments/gpt_repeat` 7 tests, and every `review_local.py` check (all ten branches merged, 45 stale-head retries, the lock timeout and both interruption windows). The experiment's two commits changed none of the files these suites cover, so this is also the baseline from before the change.
- **Mutation check** (`check_mutations.py`): the first pass ran 30 one-line mutations against the original 17 unit tests and only 13 were caught, short of the framework's phase 1 gate. After adding tests for boundary values, the criteria, the clock check and the raw-log recount, plus a golden-master test (the seeded read sets and browsed rows of all 750 writers, checked against the registered run's `results.json`), the 31 unit tests catch all 30. `--sql` adds 5 mutations of `readset.sql`, and the 13 contract tests catch all of them. 35 mutations in total, 0 survived.
- **Contract tests** (`test_readset_sql.py`, 13 tests): run as `agentdb_client` in a dedicated database, they record what the `xr` functions do now: the first merge validates nothing; a changed read key raises RS001 with the base and current values in its DETAIL, and main does not move; an unchanged read key merges even after main has moved; a row changed and changed back is not a conflict; an empty read set skips validation; a write-write conflict is still P0001 from `vc.merge`; a read conflict is reported before a write conflict; `xr.merge_head` keeps 40001; an unknown branch is refused; queuing past lock_timeout gives 55P03 and leaves main unchanged; all three functions are SECURITY DEFINER with a pinned search_path, executable only by `agentdb_client`.

Known issues, recorded but not fixed here; if read-set validation goes into the version engine, they are fixed first, in that separate change:

1. A malformed `p_reads` (not an object, NULL, or a value that is not an array) raises P0001, which is also `vc.merge`'s row/file conflict code, so a caller cannot tell bad input from a conflict by SQLSTATE; `run_readset.py` would treat it as a conflict and rethink. It should use 22023 (invalid_parameter_value).
2. An unknown table id in `p_reads` is ignored silently while main has not moved (base equals head, so nothing is validated), and raises P0001 "Unknown revision or table" once main has moved; a key that is not a uuid raises 22P02. Table ids should be checked against `vc.tracked_table` up front.
3. No contract test covers taking main's lock before validating: `vc.merge` takes the same lock itself, so only a concurrent race would show the difference. The evidence so far is the experiment's 0 rethinks in W1 rs_declared.

All three are handled in 008: 1 and 2 by its up-front 22023 checks, 3 by `crossing_read_sets_at_the_same_time_refuse_the_second` in `run_tests.py`.

## Rollback plan

The experiment changed no existing code, data format or project database, so each part can be undone on its own:

| Part | How to undo | Check |
|---|---|---|
| Experiment commits (4db8267–176572b) | `git revert` them; the only existing files they touch are `.gitignore` (4 added lines) and `AGENTS.md` (added table rows and notes) | `git diff 8537cbb 176572b -- sql db.py agent_lab.py agent_lab_gateway.py review_local.py run_tests.py agent_state tests scripts` is empty (checked 2026-10-21); the later 008 commits do change these paths, see the last row |
| `experiments/readset/` | `git revert`, or delete the directory; no code outside it imports it | outside that directory, this document and `results/`, only `AGENTS.md` and `.gitignore` mention readset (checked 2026-10-21) |
| `xr` schema | drop the `agentdb_test_readset_*` test databases with `DROP DATABASE … WITH (FORCE)`; the contract tests' database is already dropped when they finish | the project database `agentdb` has no `xr` schema (checked 2026-10-21) |
| Results | delete `results/readset/<run>/` | — |
| Version engine (migration 008) | 008 only adds functions and leaves `vc.merge` unchanged, so there is no data to restore, and merges made through it are ordinary `vc.merge` commits. As the database owner run `BEGIN; DROP FUNCTION vc.merge_validated(text,uuid,jsonb,text); DROP FUNCTION vc._rows_at_keys(uuid,uuid,jsonb[]); COMMIT;`, then revert 008's commits (b1b9505 and later) | run on 2026-10-21 on a fresh install (`agentdb_test_rsback_4c2886203a`): the vc functions went from 34 to 32 with the other 32 unchanged; after reapplying 008 all 34 equal a fresh install's |
