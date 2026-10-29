中文 | [English](#english-version)

# 真实 Agent 的读集校验与普通 PostgreSQL 对比：实验设计

状态：2026-10-28 在编写任何运行器代码、运行任何模型之前登记。用户已于同日确定两项待选内容：每轮两个真实 Agent，P 组提示词点明加锁方法。试运行和样本都须经用户同意使用模型额度。仅限本机，不访问任何云服务。

2026-10-28 编写运行器时的补充（问题、预测与判定规则不变）：运行器以超级用户登录，再以 SET ROLE 切换为 `agentdb_client`，`RESET ROLE` 一类语句会让 Agent 离开这个角色，因此两组的工具除 `BEGIN`、`COMMIT`、`ROLLBACK` 外也拒绝切换角色（SET/RESET ROLE、SESSION AUTHORIZATION，以及以 `set_config` 设置它们）。Claude Code 的工具调用超时由每个 Agent 的 MCP 配置中该服务器的 `timeout` 字段设为 600,000 ms，`agentdb_mcp_server.py` 的等待也是 600 秒；在 2.1.308 中这一字段优先于 `MCP_TOOL_TIMEOUT`（默认 1e8 ms），并限制 stdio 服务器的空闲上限（默认 1,800,000 ms），试运行中仍须核实。已在 PostgreSQL 16.15 上核实：F 组分支视图上的 `SELECT … FOR UPDATE` 不报错，但只锁视图本身，不锁 main 的行。设计未规定的细节写在运行器中并记入 protocol.json：追加的系统指令、工具说明、返回给 Agent 的输出至多 8,192 字节、reads 用 SQL 中的表名，以及成功还要求两个 Agent 的回合都完成（与之前的运行框架相同）。

2026-10-28 修改 P 组的次要指标“较晚写入方实际使用的保护”（主要指标、预测与判定规则不变）。登记的定义是：它的第一次 `execute` 是否等待了较早一方的锁，或以序列化错误失败。等锁并不说明它保护了写入：较早一方已锁住 101 与 202（`SELECT … FOR UPDATE`）时，一个不加锁、只在 UPDATE 中检查规则的较晚写入方，在 READ COMMITTED 下按语句的快照（100 + 100）通过检查，再等 202 的锁，较早一方提交后照样更新，两笔销售都落地（170），而旧定义把这一轮记为“等待了锁”。新定义只描述较晚写入方第一次 `execute` 保存下来的 SQL：在第一条写 products 的语句之前，是否有语句带锁定子句（FOR UPDATE、FOR NO KEY UPDATE、FOR SHARE 或 FOR KEY SHARE；写入语句自身中的锁，如在 CTE 中，不计；写入在 DO 块中、看不到时不作判断），以及开头的 SET TRANSACTION 设置的隔离级别（没有则为 READ COMMITTED）。单元测试覆盖四个脚本：正确的保护、上述不加锁的检查、直接 UPDATE，以及 REPEATABLE READ 的变体。F 组的指标（reads 是否列出另一件商品）不变。已在专用测试库（`agentdb_test_plain_*`，PostgreSQL 16.15）上用运行器的暂扣与提交顺序核实：较早一方运行上述保护并保持未提交，直到较晚一方被观察到等锁。较晚一方运行不加锁的检查时，等锁后报告 UPDATE 1，结束时 101 与 202 都为 85（合计 170）；运行同样的保护时也等锁，它的 `FOR UPDATE` 读到 101 为 85，报告 UPDATE 0，合计 185。旧定义把这两轮都记为“等待了锁”。

本文取代[读集校验实验](READSET_EXPERIMENT.md)中第二阶段的提纲。那份提纲在 Forkpoint 内部比较 Agent 声明的读集与它实际的读取，没有回答决定读集合入对 Agent 是否有用的问题：在保护 Agent 的写入方面，它是否胜过普通 PostgreSQL 加上被告知如何保护写入的 Agent。

## 问题

当两个真实 Agent 同时修改共享数据、且各自的决策都依赖对方写入的行时，Forkpoint（先在分支上修改，再由合入检查 Agent 列为已读的行）能否比普通 PostgreSQL（由被告知如何保护写入的 Agent 使用）更常阻止陈旧决策落地？

## 背景

- 第一阶段使用脚本写入者，不涉及模型。读集包含全部依赖行时，没有任何写偏斜冲突漏过；每个依赖行以 0.5 的概率被漏报时，70 个冲突中漏过 35 个。保护效果取决于读集是否完整。
- 迄今的模型实验都只在有版本引擎的条件下运行 Agent：[重复实验](GPT_REPEAT_EXPERIMENT.md)中 GPT 与 Claude 各 10 轮都没有覆盖，但没有不使用引擎的对照组。[上下文存档](CONTEXT_ARCHIVE_EXPERIMENT.md)与[任务状态](TASK_STATE_EXPERIMENT.md)实验都没有发现其功能带来的优势：Opus 5.5 自己的笔记保留了所需的事实，也会自行重读变化过的行。
- 普通 PostgreSQL 本身就能保护这种写入：在与写入相同的事务中，锁住决策读取的行（`SELECT … FOR UPDATE`）并再次检查规则。被告知这样做的 Agent 是否照做、是否做对，就是 Forkpoint 必须胜过的基线。

## 任务

两组相同，使用 4,000 行种子数据（价格 10，库存 100）：

- 商品 101 与 202 作为套装出售：两者库存合计不得低于 180。没有数据库约束检查这一点（即第一阶段 W2 的规则，换成本种子的数字）。
- 两个 Agent 同时开始。gpt-a 被要求出售 15 件商品 101，gpt-b 出售 15 件商品 202，都是仅当售出后套装合计仍不低于 180 时才出售，否则不出售。任一笔单独出售都允许（200 → 185）；两笔都出售则违反规则（170）。
- 冲突：每个 Agent 第一次可能修改共享数据的调用（P 组为第一次 `execute`，F 组为第一次 `merge`）会被暂扣，直到另一个 Agent 的第一次此类调用到达、另一个 Agent 的回合结束，或过了 300 秒。随后运行器先在一个暂不提交的事务中执行较早到达的调用，再开始较晚的调用；较晚的调用一结束，或一被观察到在等锁（每 50 ms 查看一次），运行器就提交较早的调用，最迟在较晚的调用开始 10 秒后提交；较晚的调用随后提交。因此只要两个 Agent 都写入，较晚的写入方总是在较早一方的变更尚未提交时工作，这对它是最不利的时刻。除此之外，两者的节奏由它们自己决定。
- 正确结果是恰好一笔销售落地（套装合计 185），另一个 Agent 不出售。陈旧：两笔都落地（合计 170）。

提示词，两个 Agent、两组都相同，只有商品编号和最后一行不同（以英文发送；`{product}` 对 gpt-a 为 101，对 gpt-b 为 202）：

```text
You are an order agent for a shop database. Products 101 and 202 are sold as a bundle:
their combined stock must never fall below 180. No database constraint checks this.
A customer wants 15 units of product {product}. Sell them by lowering product {product}'s stock by 15,
but only if the bundle still has at least 180 units afterwards; otherwise do not sell.
Change nothing else. Finish by saying whether you sold, and the stock values your decision used.
Other agents and processes change this database at the same time as you, including the stock
of other products. Your change must not rest on values that changed after you read them:
P: in the same execute call as the write, lock the rows your decision depends on
   (SELECT ... FOR UPDATE) and check the rule again before changing anything.
F: when you merge, list in reads every row your decision depends on; the merge is refused
   if one of them changed on main.
```

## 组别

| | P：普通 PostgreSQL | F：Forkpoint |
|---|---|---|
| 数据库 | 新建 `agentdb_test_plain_<10 位十六进制>`：`app.products` 与 `app.orders` 的定义同 `sql/002_demo_app.sql`，但不纳管，同样 4,000 行；没有 `vc` schema | `agent_lab.py init`（`agentdb_test_gpt_<10 位十六进制>`） |
| 角色 | `agentdb_client`，对 `app` 的权限与 F 组相同 | `agentdb_client` |
| `query` | 在实时表上以一个只读事务执行 Agent 的 SQL | 在 Agent 最新的分支上以一个只读事务执行 Agent 的 SQL；调用要求读 main 或 Agent 还没有分支时，读当前 main |
| 修改 | `execute`：以一个读写事务执行 Agent 的 SQL，全部语句成功则提交 | `fork` 从当前 main 建分支（与 agent_lab 的 fork 相同）；`execute` 把 Agent 的 SQL 作为最新分支上的一个变更执行 |
| 落地 | `execute` 的提交 | `merge`：运行器自己执行 agent_lab 的 `merge --reads` 所用的 SQL（迁移 008 的 `vc.merge_validated`），以便控制提交时机；`reads` 必填，可以为空；若 reads 中的行或分支写过的行自分支开始以来在 main 上有变化，则以 `read_conflict` 拒绝，并列出每个变化行在基线与当前的值 |

两组共同的设置：每个 Agent 有自己的 MCP 工具，绑定其名字（gpt-a 或 gpt-b）；不带 schema 的表名在 P 组解析到 `app`，在 F 组解析到 `agent`（分支视图），Agent 在两组写相同的表名；事务由运行器开启和结束，Agent 的 SQL 可以以 `SET TRANSACTION ISOLATION LEVEL …` 开头，但 `BEGIN`、`COMMIT`、`ROLLBACK` 会被拒绝，F 组的 SQL 不得引用 `vc` 或 `app` schema；`lock_timeout` 为 10 秒，与 agent_lab 的合入相同；错误返回其 SQLSTATE；每个 Agent 40 次调用。运行框架沿用 `experiments/task_state/` 的（Claude Code 无界面模式，归档每个请求和响应，每次可能写入的调用之后做一次 SQL 核查），但两个 Agent 的进程同时运行（之前的实验每次只运行一个 Agent 的回合）；运行器使 Claude Code 的工具调用超时长于 300 秒的暂扣（在试运行中核实）。

## 运行安排

- 试运行：每组一轮，使用 `--pilot`，不计入样本，单独报告。
- 样本：20 轮，奇数轮 P、偶数轮 F，每次运行一轮（每轮的两个 Agent 同时运行）；两个 Agent 都用 claude-opus-5-5，推理强度 xhigh，确切版本记入 protocol.json。保留所有轮次，不替换失败轮次，不人工补救。因用量额度中断的轮次保留，并如实标明；与存档实验一样每轮有两个 Agent，而存档实验的第 19、20 轮曾用尽 Pro 订阅的会话额度。

## 测量

- 主要指标（逐轮）：两笔销售是否都落地（结束时 101 与 202 都为 85）。
- 次要指标：成功（恰好一笔销售落地；其余 3,998 行和 `app.orders` 不变；没有未结束的事务；每个响应都来自登记的模型；每个 MCP 请求都有回应）；结果（正确、陈旧或其他：没有销售，或任何其他改动）；冲突是否按设计发生（两个 Agent 都发出了第一次写入调用）以及哪个 Agent 较晚写入；较晚写入方实际使用的保护（P 组：它的第一次 `execute` 是否等待了较早一方的锁，或以序列化错误失败；F 组：它的 reads 是否包含另一件商品）；每个 Agent 收到的拒绝及其后续动作；每个 Agent 的调用次数、token（输入、其中缓存、输出）、耗时与被暂扣的时间。

## 预测（运行前写定）

- P：提示词点明了保护方法，多数较晚写入方会在写入的调用中锁住另一件商品；10 轮中陈旧 0–2 轮。可能的失败方式是不加锁的检查，无论在同一语句中还是在之前的 query 中；较早一方尚未提交的变更会让两者都失效。
- F：10 轮中陈旧 0–1 轮；可能的失败方式是较晚写入方的 reads 漏掉另一件商品。
- F 的每个 Agent 比 P 用更多调用和 token：需要 fork 与 merge，收到 `read_conflict` 后还要新建分支或重新读取。

## 判定规则（现在决定）

- 只有当 P 的陈旧轮次比 F 至少多 3 轮（各 10 轮中），且 F 的成功次数不更低时，才认为 Forkpoint 在本任务上有正确性优势。
- 否则结论为：没有证据表明读集合入比普通 PostgreSQL 上得到指导的 Agent 更能保护这个决策；成本差异按实测报告。
- F 中每一个陈旧轮次都连同两个 Agent 发送的 reads 一起报告，作为声明读集是否可靠的证据。

## 本实验回答不了的问题

- 这类冲突多常发生：暂扣使两次写入在两个 Agent 都写入的每一轮都发生冲突，因此测量的是冲突发生时的保护，而不是它发生的频率。
- 每轮只检验一个保护：较早的写入方没有遇到任何变化，结果只取决于较晚写入方的保护。
- 同一轮的两个写入方使用同一组的工具和提示词；保护所面对的是同类写入方，而不是应用程序或后台管理工具。（在 P 组中，不加锁的 SERIALIZABLE 只有在两个事务都使用它时才起作用。）
- 只有一个任务：两行规则上的写偏斜，按键读取。基于范围查询的决策还可能被新插入的行（幻读）破坏，按键的读集发现不了。
- 无关并发变更（改动决策未使用的行）的成本没有测量；第一阶段已用脚本写入者测过。
- P 组的提示词点明了保护方法；只被告知他人同时写入的 Agent 不在测试范围内。
- 只测 Claude Opus 5.5、推理强度 xhigh；每组 10 轮发现不了小的差异。

## 用户已确定的选择（2026-10-28）

1. 每轮两个真实 Agent，而不是一个 Agent 加一笔脚本化的销售。“任务”一节所述的暂扣，是本稿让两者在每一轮都发生冲突的方法；没有暂扣，是否冲突将取决于每个 Agent 碰巧有多快。
2. P 组提示词点明加锁方法，详细程度与 F 组相同。

## 文件与运行

| 文件 | 用途 |
|---|---|
| `experiments/readset_agents/run_readset_agents.py` | 运行器：两种数据库、每个 Agent 的工具、暂扣与提交顺序、核查、protocol.json 与逐轮记录 |
| `experiments/readset_agents/fake_readset_claude.py` | 替身试运行所用的脚本化替身，不调用模型 |
| `experiments/readset_agents/test_run_readset_agents.py` | 结果判定规则；暂扣与提交顺序（第一次写入被暂扣，直到另一方的到达、另一方的回合结束或过了 300 秒；较晚的调用结束或等锁时提交较早的）；每个工具应拒绝的输入；用两个替身分别以有保护和无保护的方式对两组做替身试运行，两组都必须分别得到正确和陈旧的结果 |

| 内容 | 命令 |
|---|---|
| 测试 | `python -m unittest discover -s experiments/readset_agents -v`：21 项，自建并删除测试库；其中替身试运行四轮（两组各一轮有保护、一轮无保护），约需 1 分钟 |
| 替身试运行（命令行） | `python experiments/readset_agents/run_readset_agents.py --claude experiments/readset_agents/fake_readset_claude.py --config-dir <目录> --workdir <新目录> --output <新目录> --runs-per-group 1 --pilot`：替身默认有保护；加上 `--child-env '{"READSET_STAND_IN_GUARDS": "0"}'` 则无保护 |
| 试运行（须经用户同意使用额度） | `python experiments/readset_agents/run_readset_agents.py --claude <claude.exe> --config-dir <只含登录的 CLAUDE_CONFIG_DIR> --workdir <新目录> --output experiments/readset_agents/results/<日期>-pilot --runs-per-group 1 --pilot` |
| 样本（须经用户同意） | 同上，去掉 `--pilot`，改为 `--runs-per-group 10`，输出到 `experiments/readset_agents/results/<日期>` |

原始记录只保存在作者的机器上：`experiments/readset_agents/results/` 不纳入版本库（见该目录的 `.gitignore`）。每轮的测试库（`agentdb_test_plain_*` 或 `agentdb_test_gpt_*`）保留以便核查。

## 试运行（2026-10-29，不计入样本）

经用户同意，在 commit bff680f 上以 `--pilot` 每组运行一轮：Claude Code 2.1.308，两个 Agent 都用 claude-opus-5-5，推理强度 xhigh。原始记录在 `experiments/readset_agents/results/2026-10-29-pilot`（不在版本库中）；测试库为 `agentdb_test_plain_6c9b51e27c`（P）与 `agentdb_test_gpt_0509d9d754`（F）。

| | P | F |
|---|---|---|
| 结果 | 成功，正确：202 售出（85），101 未售（100） | 成功，正确：101 售出（85），202 未售（100） |
| 较早 / 较晚写入方 | gpt-b / gpt-a | gpt-a / gpt-b |
| 冲突 | 按设计发生：gpt-b 的写入被暂扣 5.6 秒，直到 gpt-a 的写入到达；观察到 gpt-a 等锁时提交 gpt-b | 按设计发生：gpt-a 的 merge 被暂扣 0.8 秒；观察到 gpt-b 的 merge 等锁时提交 gpt-a |
| 较晚写入方的保护 | 在带条件的 UPDATE 之前对 101 与 202 执行 `SELECT … FOR UPDATE`，READ COMMITTED；等锁后读到 202 为 85，没有修改 | reads 列出 101 与 202；merge 因 101 被拒（`read_conflict`，RS001） |
| 工具调用（gpt-a / gpt-b） | 4（2 / 2） | 12（5 / 7） |
| token：输入（其中缓存读取）/ 输出 | 20,775（12,494）/ 2,620 | 48,627（39,336）/ 2,345 |
| Claude Code 报告的费用 | 0.12 美元 | 0.13 美元 |
| 耗时 | 25 秒 | 39 秒 |

两轮中每个 MCP 请求都有回应，每个响应都来自 claude-opus-5-5，没有核查错误或未结束的事务，其余 3,998 行不变，`app.orders` 为空；运行后对两个测试库的独立查询结果相同。

观察：
- P 组两个 Agent 都在一次 `execute` 中使用了提示词点明的保护：先对两行 `SELECT … FOR UPDATE`，再执行在 WHERE 中重新检查套装合计的 UPDATE；gpt-a 还显式设置了 READ COMMITTED。
- F 组两个 Agent 都把分支上的 UPDATE 限定为 `stock = 100`（它们读到的值），并在 reads 中列出两件商品。gpt-b 收到 `read_conflict` 后新建分支、读到 101 为 85、决定不出售，然后合入这个没有改动、reads 列出两件商品的分支，用来确认它读到的值仍是 main 的当前值；合入被接受，main 的 revision 不变。
- 与预测对照：两组都没有陈旧轮次（预测 P 为 0–2、F 为 0–1）；F 的调用次数和输入 token 更多（预测如此）。每组一轮不能据此对判定规则作出任何结论。

## 结果（2026-10-29）

经用户同意，在 commit be4aef1 上运行登记的样本：20 轮，奇数轮 P、偶数轮 F，设置与试运行相同。原始记录在 `experiments/readset_agents/results/2026-10-29`（不在版本库中）；20 个测试库保留。运行器退出码为 0；没有轮次失败或被替换，也没有遇到用量额度。

| | P | F |
|---|---|---|
| 陈旧轮次（主要指标） | 0 / 10 | 0 / 10 |
| 成功 | 10 / 10（Wilson 95% 区间 0.72–1.00） | 10 / 10（0.72–1.00） |
| 冲突按设计发生（两方都写入、较晚一方在较早一方未提交时工作、较早一方在较晚一方等锁时提交） | 10 / 10 | 10 / 10 |
| 较晚写入方的保护 | 10 / 10 在写入前加锁，READ COMMITTED | 10 / 10 的 reads 列出另一件商品，都收到 `read_conflict` |
| 每轮工具调用（中位数，范围） | 4（4–5） | 12（11–14） |
| 每轮输入 token（中位数）/ 输出 | 20,515 / 2,701 | 49,094 / 2,320 |
| 全部 token：输入（其中缓存读取）/ 输出 | 209,844（175,651）/ 26,792 | 488,287（447,989）/ 23,128 |
| Claude Code 报告的费用：合计（每轮中位数） | 0.84 美元（0.085） | 0.87 美元（0.088） |
| 每轮耗时（中位数，范围） | 27.7 秒（22.3–30.7） | 43.1 秒（37.2–53.4） |
| 暂扣时间（范围） | 0.16–6.34 秒 | 0.92–7.98 秒 |

20 轮中每个 MCP 请求都有回应，每个响应都来自 claude-opus-5-5，没有核查错误或未结束的事务；其余 3,998 行不变，`app.orders` 为空。运行后对 20 个测试库的独立查询与记录一致：每轮恰好一笔销售，合计 185。

判定规则：P 的陈旧轮次（0）没有比 F（0）多 3 轮，因此结论为：**没有证据表明读集合入比普通 PostgreSQL 上得到指导的 Agent 更能保护这个决策。** 实测的成本差异：F 每轮约用 3 倍的工具调用、2.4 倍的输入 token、1.6 倍的耗时；因为多出的输入大多是缓存读取，报告的费用只高约 4%。

观察：
- P 组每个较晚写入方都只用了一次 query 和一次 execute：在同一个 execute 中先对两行 `SELECT … FOR UPDATE`，再执行带条件的 UPDATE；等锁后它的检查看到另一笔销售，没有修改。
- F 组每个较晚写入方在 `read_conflict` 后都新建分支并重新读取；其中 4 轮（2、6、10、20）还合入了没有改动的分支，用来确认读到的值。
- 与预测对照：三项预测都成立（P 陈旧 0，预测 0–2；F 陈旧 0，预测 0–1；F 用更多调用和 token）。

## 回滚

所有内容将位于 `experiments/readset_agents/` 与本文中；`vc.*`、`agent_lab.py` 和项目数据库都不改动。删除它们即恢复原状。

---

<a id="english-version"></a>

# Read Sets with Real Agents vs. Plain PostgreSQL: Experiment Design

Status: registered on 2026-10-28, before any runner code was written or any model was run. The user settled its two open choices the same day: two real agents per round, and a P prompt that names the locking technique. The pilot and the sample need the user's approval for model quota. Local only; no cloud service is accessed.

Added on 2026-10-28 while writing the runner (the question, predictions and decision rule are unchanged): the runner logs in as a superuser and then sets the role to `agentdb_client`, so `RESET ROLE` and the like would take an agent out of that role; besides `BEGIN`, `COMMIT` and `ROLLBACK`, both groups' tools therefore refuse role changes (SET/RESET ROLE and SESSION AUTHORIZATION, and `set_config` on them). Claude Code's tool-call timeout is set to 600,000 ms through the `timeout` field of each agent's MCP server config, and `agentdb_mcp_server.py` waits 600 s too; in 2.1.308 that field takes precedence over `MCP_TOOL_TIMEOUT` (default 1e8 ms) and also caps the idle limit for stdio servers (default 1,800,000 ms); the pilot still checks it. Checked on PostgreSQL 16.15: `SELECT … FOR UPDATE` on an F branch view runs without error but locks only the view, not main's rows. Details the design left open are in the runner and recorded in protocol.json: the appended system instructions, the tool texts, at most 8,192 bytes of output back to the agent, reads named by the tables the SQL uses, and that success also needs both agents' turns to complete (as in the earlier harnesses).

Changed on 2026-10-28: P's secondary measure of the guard the later writer used (the primary measure, the predictions and the decision rule are unchanged). The registered definition is whether its first execute waited for the earlier one's lock or failed with a serialization error. Waiting for a lock does not show that the agent guarded its write: once the earlier writer has locked 101 and 202 (`SELECT … FOR UPDATE`), a later writer that checks the rule only inside its UPDATE, without a lock, passes the check on the statement's snapshot (100 + 100) under READ COMMITTED, waits for 202's lock, and updates anyway when the earlier one commits; both sales land (170), and the old definition records the round as "waited for a lock". The new definition only describes the SQL saved for the later writer's first execute: whether a statement before the first one that writes products has a locking clause (FOR UPDATE, FOR NO KEY UPDATE, FOR SHARE or FOR KEY SHARE; a lock in the writing statement itself, as in a CTE, does not count, and a write inside a DO block, which this cannot see, gives no answer), and the isolation level its leading SET TRANSACTION sets (READ COMMITTED if none). Unit tests cover four scripts: the correct guard, the unlocked check above, a blind UPDATE, and a REPEATABLE READ variant. F's measure (whether reads named the other product) is unchanged. Confirmed on a dedicated test database (`agentdb_test_plain_*`, PostgreSQL 16.15) with the runner's hold and commit order, the earlier writer running that guard and staying open until the later one was seen waiting. A later writer with the unlocked check waited, reported UPDATE 1, and left 101 and 202 both at 85 (bundle 170); one running the same guard waited too, its `FOR UPDATE` read 101 at 85, and it reported UPDATE 0 (bundle 185). The old definition records both rounds as "waited for a lock".

This replaces the phase 2 outline in the [read-set experiment](READSET_EXPERIMENT.md). That outline compared, within Forkpoint, the read sets agents declare with the reads they make. It did not ask the question that decides whether the read-set merge helps agents at all: does it protect their writes better than plain PostgreSQL in the hands of agents told how to guard their writes?

## Question

When two real agents change shared data at the same time, and each one's decision depends on a row the other one writes, does Forkpoint (a branch, then a merge that checks the rows the agent lists as read) stop the stale decision more often than plain PostgreSQL used by agents that have been told how to guard their writes?

## Background

- Phase 1 used scripted writers and no model. With every dependency in the read set, no write-skew conflict got through; with each dependency left out at probability 0.5, 35 of 70 got through. The protection is only as good as the read set.
- The model experiments so far ran agents with the engine only: no overwrite in 10 GPT and 10 Claude rounds of the [repeat experiments](GPT_REPEAT_EXPERIMENT.md), but no group worked without the engine. The [context archive](CONTEXT_ARCHIVE_EXPERIMENT.md) and [task state](TASK_STATE_EXPERIMENT.md) experiments found no advantage for their features: Opus 5.5's own notes kept the facts it needed, and it re-read the changed row by itself.
- Plain PostgreSQL can guard such a write: in the same transaction as the write, lock the rows the decision read (`SELECT … FOR UPDATE`) and check the rule again. Whether agents told to do this do it, and do it correctly, is the baseline Forkpoint has to beat.

## Task

The same in both groups, on the 4,000-row seed (price 10, stock 100):

- Products 101 and 202 are sold as a bundle: their combined stock must not fall below 180. No database constraint checks this (phase 1's W2 rule, with this seed's numbers).
- Two agents start at the same time. gpt-a is asked to sell 15 units of product 101 and gpt-b 15 units of product 202, each only if the bundle still has at least 180 afterwards, and otherwise not to sell. Either sale alone is allowed (200 → 185); both together break the rule (170).
- The collision: each agent's first call that can change shared data (P: its first `execute`; F: its first `merge`) is held until the other agent's first such call arrives, the other agent's turn ends, or 300 s pass. The runner then runs the earlier call in a transaction it does not commit yet, starts the later call, and commits the earlier one as soon as the later call finishes or is seen waiting for a lock (checked every 50 ms), and at most 10 s after the later call starts; the later call commits after that. So whenever both agents write, the later writer works while the earlier one's change is in flight, the worst moment for it. Otherwise each agent keeps its own pace.
- The correct outcome is that exactly one sale lands (bundle 185) and the other agent does not sell. Stale: both sales land (170).

The prompt, the same for both agents and both groups except for the product and the last line (sent in English; `{product}` is 101 for gpt-a and 202 for gpt-b):

```text
You are an order agent for a shop database. Products 101 and 202 are sold as a bundle:
their combined stock must never fall below 180. No database constraint checks this.
A customer wants 15 units of product {product}. Sell them by lowering product {product}'s stock by 15,
but only if the bundle still has at least 180 units afterwards; otherwise do not sell.
Change nothing else. Finish by saying whether you sold, and the stock values your decision used.
Other agents and processes change this database at the same time as you, including the stock
of other products. Your change must not rest on values that changed after you read them:
P: in the same execute call as the write, lock the rows your decision depends on
   (SELECT ... FOR UPDATE) and check the rule again before changing anything.
F: when you merge, list in reads every row your decision depends on; the merge is refused
   if one of them changed on main.
```

## Groups

| | P: plain PostgreSQL | F: Forkpoint |
|---|---|---|
| Database | a new `agentdb_test_plain_<10 hex>`: `app.products` and `app.orders` defined as in `sql/002_demo_app.sql` but not enrolled, the same 4,000 rows; no `vc` schema | `agent_lab.py init` (`agentdb_test_gpt_<10 hex>`) |
| Role | `agentdb_client`, with the same privileges on `app` as in F | `agentdb_client` |
| `query` | the agent's SQL in one read-only transaction on the live tables | the agent's SQL in one read-only transaction on its newest branch; on current main if the call asks for main or the agent has no branch yet |
| Changing | `execute`: the agent's SQL in one read-write transaction, committed if every statement succeeds | `fork` starts a branch from current main, as agent_lab's fork does; `execute` runs the agent's SQL as one change on the newest branch |
| Landing | the `execute` commit | `merge`: the runner itself issues the SQL of agent_lab's `merge --reads` (`vc.merge_validated`, migration 008), so that it controls the commit; `reads` is required and may be empty; refused with `read_conflict`, listing each changed row's value at the base and now, if a row in reads or a row the branch wrote changed on main since the branch started |

Common to both groups: each agent has its own MCP tool, bound to its name (gpt-a or gpt-b); table names without a schema resolve to `app` in P and to `agent` (the branch views) in F, so the agents write the same table names in both; the runner opens and closes each transaction, so an agent's SQL may begin with `SET TRANSACTION ISOLATION LEVEL …` but `BEGIN`, `COMMIT` and `ROLLBACK` are refused, and F's SQL may not name the `vc` or `app` schemas; `lock_timeout` is 10 s, as for agent_lab's merges; errors return their SQLSTATE; 40 calls per agent. The harness is `experiments/task_state/`'s (Claude Code headless, every request and response archived, an SQL audit after every call that can write), except that both agents' processes run at once (the earlier experiments ran one agent's turn at a time); the runner keeps Claude Code's tool-call timeout above the 300 s hold (checked in the pilot).

## Schedule

- Pilot: one round per group with `--pilot`, outside the sample, reported separately.
- Sample: 20 rounds, odd rounds P and even rounds F, one round at a time (each round runs its two agents at once); claude-opus-5-5 at xhigh effort for both agents, exact versions recorded in protocol.json. All rounds are kept, none is replaced, and nothing is rescued by hand. A round stopped by a usage limit is kept and reported as such; like the archive experiment, every round has two agents, and that experiment's rounds 19 and 20 hit the Pro subscription's session limit.

## Measurements

- Primary, per round: whether both sales landed (101 and 202 both at 85 at the end).
- Secondary: success (exactly one sale landed; the other 3,998 rows and `app.orders` unchanged; no open transaction; every response from the registered model; every MCP request answered); the outcome (correct, stale, or other: no sale, or any other change); whether the collision happened as designed (both agents made a first write call), and which agent wrote later; the later writer's guard (P: whether its first `execute` waited for the earlier one's lock or failed with a serialization error; F: whether its reads named the other product); the refusals each agent got and what it did next; per agent, calls, tokens (input, of which cached, and output), wall time and time held.

## Predictions (written before running)

- P: with the guard named in the prompt, most later writers lock the other product in their write call; 0–2 stale rounds in 10. The likely failure is a check without a lock, in the same statement or in an earlier query; the earlier writer's change in flight defeats both.
- F: 0–1 stale rounds in 10; the likely failure is a later writer that leaves the other product out of reads.
- Each F agent takes more calls and tokens than a P agent: fork and merge, and after `read_conflict` a new branch or a fresh read.

## Decision rule (decided now)

- Forkpoint shows a correctness advantage on this task only if P has a stale round in at least 3 more of its 10 rounds than F, and F's success count is not lower.
- Otherwise the conclusion is: no evidence that the read-set merge protects this decision better than guided agents on plain PostgreSQL; the cost difference is reported as measured.
- Every stale round in F is reported with the reads both agents sent, as evidence on whether declared read sets can be relied on.

## What this experiment cannot tell us

- How often such collisions happen: the hold makes the two writes collide in every round where both agents write, so this measures protection when a collision happens, not how often one does.
- Each round tests one guard: the earlier writer meets no change, so the outcome depends only on the later writer's guard.
- Both writers in a round use the same group's tool and prompt, so a guard is tested against a writer of the same kind, not against an application or an admin tool. (In P, SERIALIZABLE without locks holds only when both transactions use it.)
- One task: write skew on a two-row rule, with reads by key. A decision based on a range query can also be broken by inserted rows (phantoms), which key-based read sets miss.
- The cost of concurrent changes that don't matter (to rows the decision doesn't use) is not measured; phase 1 covered that with scripted writers.
- P's prompt names the guard; agents told only that others write at the same time are not tested.
- Claude Opus 5.5 at xhigh only; 10 rounds per group cannot detect small differences.

## Choices settled by the user (2026-10-28)

1. Two real agents per round, rather than one agent and a scripted sale. The hold described under "Task" is this draft's way of making the two collide in every round; without it, whether they collide would depend on how fast each agent happens to be.
2. P's prompt names the locking technique, at the same level of detail as F's.

## Files and running

| File | Purpose |
|---|---|
| `experiments/readset_agents/run_readset_agents.py` | the runner: both kinds of database, each agent's tool, the hold and the commit order, the audits, protocol.json and the round records |
| `experiments/readset_agents/fake_readset_claude.py` | a scripted stand-in for dry runs; calls no model |
| `experiments/readset_agents/test_run_readset_agents.py` | the outcome rule; the hold and the commit order (a first write held until the other's arrives, the other's turn ends or 300 s pass; the earlier committed when the later finishes or waits); what each tool refuses; a dry run of each group with two stand-ins, guarding and not guarding, which must give the correct and the stale outcome in both groups |

| What | Command |
|---|---|
| Tests | `python -m unittest discover -s experiments/readset_agents -v`: 21 tests; makes and drops its test databases; the stand-in dry run in it (four rounds: each group once guarding and once not) takes about a minute |
| Stand-in dry run (command line) | `python experiments/readset_agents/run_readset_agents.py --claude experiments/readset_agents/fake_readset_claude.py --config-dir <dir> --workdir <new dir> --output <new dir> --runs-per-group 1 --pilot`: the stand-ins guard by default; add `--child-env '{"READSET_STAND_IN_GUARDS": "0"}'` for stand-ins that do not |
| Pilot (needs the user's approval for quota) | `python experiments/readset_agents/run_readset_agents.py --claude <claude.exe> --config-dir <CLAUDE_CONFIG_DIR holding only the login> --workdir <new dir> --output experiments/readset_agents/results/<date>-pilot --runs-per-group 1 --pilot` |
| Sample (needs the user's approval) | as above without `--pilot`, with `--runs-per-group 10`, output to `experiments/readset_agents/results/<date>` |

Raw records stay on the author's machine only: `experiments/readset_agents/results/` is not committed (see the folder's `.gitignore`). Each round's test database (`agentdb_test_plain_*` or `agentdb_test_gpt_*`) is kept for checking.

## Pilot (2026-10-29, not part of the sample)

With the user's approval, one round of each group was run with `--pilot` at commit bff680f: Claude Code 2.1.308, claude-opus-5-5 at xhigh effort for both agents. Raw records are in `experiments/readset_agents/results/2026-10-29-pilot` (not in the repository); the test databases are `agentdb_test_plain_6c9b51e27c` (P) and `agentdb_test_gpt_0509d9d754` (F).

| | P | F |
|---|---|---|
| Result | success, correct: 202 sold (85), 101 not sold (100) | success, correct: 101 sold (85), 202 not sold (100) |
| Earlier / later writer | gpt-b / gpt-a | gpt-a / gpt-b |
| Collision | as designed: gpt-b's write held 5.6 s until gpt-a's arrived; gpt-b committed when gpt-a was seen waiting for a lock | as designed: gpt-a's merge held 0.8 s; gpt-a committed when gpt-b's merge was seen waiting |
| Later writer's guard | `SELECT … FOR UPDATE` on 101 and 202 before the conditional UPDATE, READ COMMITTED; after the wait it read 202 at 85 and changed nothing | reads named 101 and 202; the merge was refused on 101 (`read_conflict`, RS001) |
| Tool calls (gpt-a / gpt-b) | 4 (2 / 2) | 12 (5 / 7) |
| Tokens: input (of which cache read) / output | 20,775 (12,494) / 2,620 | 48,627 (39,336) / 2,345 |
| Cost reported by Claude Code | $0.12 | $0.13 |
| Wall time | 25 s | 39 s |

In both rounds every MCP request was answered, every response came from claude-opus-5-5, and there was no audit error or open transaction; the other 3,998 rows were unchanged and `app.orders` was empty. An independent query of both test databases after the run agreed.

Observations:
- In P both agents used the guard the prompt names, in one `execute`: `SELECT … FOR UPDATE` on both rows, then an UPDATE whose WHERE checks the bundle again. gpt-a also set READ COMMITTED explicitly.
- In F both agents made their branch UPDATE conditional on `stock = 100`, the value they had read, and listed both products in reads. After `read_conflict`, gpt-b forked a new branch, read 101 at 85 and decided not to sell; it then merged that branch, which held no change, with both products as reads, to confirm the values it read were still main's. The merge was accepted and main's revision did not change.
- Against the predictions: no stale round in either group (predicted 0–2 for P, 0–1 for F); F took more calls and input tokens, as predicted. One round per group says nothing about the decision rule.

## Results (2026-10-29)

With the user's approval, the registered sample was run at commit be4aef1: 20 rounds, odd rounds P and even rounds F, with the pilot's settings. Raw records are in `experiments/readset_agents/results/2026-10-29` (not in the repository); the 20 test databases are kept. The runner exited 0; no round failed or was replaced, and no usage limit was hit.

| | P | F |
|---|---|---|
| Stale rounds (primary) | 0 / 10 | 0 / 10 |
| Success | 10 / 10 (Wilson 95% interval 0.72–1.00) | 10 / 10 (0.72–1.00) |
| Collision as designed (both wrote; the later one worked while the earlier change was in flight; the earlier one committed when the later was seen waiting) | 10 / 10 | 10 / 10 |
| Later writer's guard | 10 / 10 locked before writing, READ COMMITTED | 10 / 10 named the other product in reads; all got `read_conflict` |
| Tool calls per round (median, range) | 4 (4–5) | 12 (11–14) |
| Input / output tokens per round (median) | 20,515 / 2,701 | 49,094 / 2,320 |
| All tokens: input (of which cache read) / output | 209,844 (175,651) / 26,792 | 488,287 (447,989) / 23,128 |
| Cost reported by Claude Code: total (median per round) | $0.84 ($0.085) | $0.87 ($0.088) |
| Wall time per round (median, range) | 27.7 s (22.3–30.7) | 43.1 s (37.2–53.4) |
| Time held (range) | 0.16–6.34 s | 0.92–7.98 s |

In all 20 rounds every MCP request was answered, every response came from claude-opus-5-5, and there was no audit error or open transaction; the other 3,998 rows were unchanged and `app.orders` was empty. An independent query of the 20 test databases after the run agreed with the records: exactly one sale in every round, bundle 185.

Decision rule: P's stale rounds (0) are not at least 3 more than F's (0), so the conclusion is: **no evidence that the read-set merge protects this decision better than guided agents on plain PostgreSQL.** The measured cost difference: per round, F used about 3 times the tool calls, 2.4 times the input tokens and 1.6 times the wall time; since most of the extra input was cache reads, the reported cost was only about 4% higher.

Observations:
- Every later writer in P used one query and one execute: `SELECT … FOR UPDATE` on both rows, then the conditional UPDATE, in the same execute. After the wait its check saw the other sale and it changed nothing.
- Every later writer in F forked a new branch and read again after `read_conflict`; in 4 rounds (2, 6, 10, 20) it also merged that branch with no change in it to confirm the values it had read.
- All three predictions held (P 0 stale, predicted 0–2; F 0 stale, predicted 0–1; F used more calls and tokens).

## Rollback

Everything will be in `experiments/readset_agents/` and this document; `vc.*`, `agent_lab.py` and the project database are not changed. Removing them restores the previous state.
