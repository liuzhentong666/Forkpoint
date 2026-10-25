中文 | [English](#english-version)

# 可核查的任务状态：实验设计

状态：2026-10-24 在任何模型运行之前登记。同日经用户同意完成了试运行（见下文“试运行”，不计入样本），两组都落地了正确的 55；样本没有运行，用户依据试运行决定不采纳新鲜度标记（见下文“结论”）。真实轮次须经用户同意使用模型额度。下文的检查和替身试运行都不调用模型。任务状态保存在运行器进程内的 MemoryStore 中（云端工作暂缓，正式持久化另行决定）；仅限本机，不访问任何云服务。

2026-10-24 试运行之后的改动：工具说明的错误列表加上了 `agent_lab.py` 对 fork 和提案的拒绝（`stale_branch_head`、附该分支 head 的 `branch_exists`、`not_found`），工具说明因此长了 116 字节；这些失败此前是 traceback，运行器记为 `helper_failed`。上面的试运行用的是旧的说明和旧的行为，再运行会在这两点上不同。

2026-10-25 审查后修正了运行器的三处：成功判定改为核对 main 上最终的价格 11 与 `PRICE = 11`，与登记的定义一致（此前 gpt-a 曾经落地过就算，之后改价也不影响）；恢复包只为显示出来的事实留出新鲜度标记的位置，事实与其标记一并省略（此前为全部事实预留，事实多时 F 组报预算不足，U 组仍能恢复）；重置时已保存的状态恢复失败，不再当作“尚未保存”交给新进程，本轮停止并作为失败保留。试运行的两轮都不受影响：最终价格 11、`PRICE = 11.0`，每次重置都有已保存的状态，没有恢复失败。

## 问题

Agent 在任务中途多次重置后，从检查点恢复。如果检查点中的每个事实都标明它读自哪个版本的哪些行，并在恢复时标出“新鲜 / 已过期 / 未知”（F 组），是否比没有这些标记的同样检查点（U 组）更少依据过期事实做出决定？

## 背景

- [上下文存档实验](CONTEXT_ARCHIVE_EXPERIMENT.md) 没有发现原样存档优于模型自己的说明；对本机 3 次真实压缩的检查也没有发现可测的损失，可能因为状态都写在提交、文档和记忆文件中。
- 写下来的记录只在它仍然正确时有用。重置之后 Agent 会相信记录；记录依据的数据若已被别人改动，它不会知道。
- Forkpoint 的合入检查已在写入时发现这类问题：别人改过的行会引起冲突。但 Agent 读了却不写的行不受这一检查保护（例如依据另一件商品的库存来决定）。本实验只针对这种情形。

## 原型（仅限实验，不修改 `vc.*` 与 `agent_state`）

- `experiments/task_state/task_state.py`：任务状态仍是 `agent_state` 引擎中的记录（目标、状态、下一步、未决事项、事实、证据引用），引擎不变。检查点可为事实附上 `based_on: {revision, rows}`，保存为普通证据对象；旧记录照常读取，其事实为“未知”。
- 新鲜度：把这些行在该版本与当前 main 上的行文档逐一比较（`vc._rows_at_keys`，008）：都相同为新鲜；有变化或已删除为已过期（最多列出 3 行的前后值）；没有读取记录或该版本上找不到某行为未知（防止写错键的事实被当作“未变”）。
- 恢复包：项目已有的有预算上下文包（`Runtime.context`），F 组为其中每个事实加上标记，并统计因预算省略的过期事实；整个包不超过 8 KB。U 组的包相同，只是没有标记。
- 同一检查点的重试返回第一次的结果，即使状态已经前进（按检查点内容的摘要识别）。

## 任务

- 每轮准备：`agent_lab.py init`（4,000 行，价格 10，库存 100）。
- 规则：商品 101 由商品 202 补货，101 的库存应等于 202 当前库存减 5。
- gpt-b 准备阶段（`PREPARE_S`）：在基线上读 101 和 202，按规则提出 101 的库存（基线上为 95）及 `STOCK` 常量，不要合入；结束前做检查点：提案版本、原始 expected main，以及库存值所依据的每个事实及其读取的版本和行。
- gpt-a（`RUN_A`）：与 2026-10-17/20 的实验相同，把 101 的价格提高 10% 并合入。
- 仓库更新：gpt-a 阶段之后，运行器在 main 上提交，把 202 的库存改为 60。
- gpt-b 恢复阶段（`LAND_S`）：先恢复保存的任务状态，再按规则把 101 的库存变更合入 main。正确结果为 55；依据过期事实则为 95。
- 两组的工具相同：`agent_lab.py` 的动作去掉 `verify`（它检查旧任务的 95），加上 `read-product`、`checkpoint`、`resume`，由运行器应答。每个 Agent 60 次调用。
- 成功：两个阶段完成；main 上 101 的库存为 55 且 `STOCK = 55`，价格 11 且 `PRICE = 11`；202 为 60；其余行与基线不变；没有未结束的事务、已发布的覆盖或对方分支的改动；所有回复来自登记的模型；所有 MCP 请求都有回复；没有存档错误。

## 实验组与重置

- U：`resume` 不带新鲜度标记。
- F：`resume` 为每个事实标出新鲜、已过期或未知。
- 两组相同：某次请求的输入第一次超过 10,000 token 时，在下一个工具调用边界重置该 Agent；新进程收到当前阶段的指令和它的恢复包（`HEADER_S`）。每个 Agent 每轮最多重置 3 次，第 3 次之后不再触发。重置期间的调用被拒绝（`context_switch`），记录在案，不计入预算。

## 运行安排

- 试运行：用 `--pilot` 每组各跑一轮，不计入样本，单独报告。
- 样本：20 轮，奇数轮 U、偶数轮 F，两轮并行；claude-opus-5-5，推理强度 xhigh，Claude Code 2.1.308；确切版本记录在 protocol.json。保留所有轮次，不替换失败的轮次，不人工补救。

## 测量

- 主要指标（每轮）：gpt-b 是否把过期决定合入 main，即结束时 101 的库存为 95。
- 次要指标：是否成功；结果为正确、过期或其他；仓库更新之后 gpt-b 重读 202 的次数；每个 Agent 的重置次数；重置后的调用和 token；恢复包大小。

## 检查与替身试运行（不调用模型）

- `test_task_state.py`（9 项）：行在 main 上改变后事实变为过期、改回后恢复新鲜、改动其他行不影响；没有读取记录或写错键为未知；重新设置或删除事实时替换或删除其读取记录；重试不重复执行；错误的检查点被拒绝；恢复包在预算内并统计被省略的过期事实；全部标记放不下时，事实与其标记一并省略，仍能恢复；旧记录照常恢复。
- `test_run_state_trials.py`（10 项）：结果判定；成功判定要求最终价格 11 与 `PRICE = 11`；重置时没有保存的状态照实交接，已保存的状态恢复失败则本轮停止；两组工具相同且没有 verify；`read-product` 的参数检查；只有 F 组标记事实；以及在本进程内用替身各跑一轮、把重置上限降到 4,000 token 的试运行：gpt-b 重置 1–3 次，交接包含当前阶段指令，只有 F 组的交接带标记。
- 替身是脚本：只有恢复包标出过期时才重读 202，所以 U 组得到 95、F 组得到 55。这只说明流程可行，不预测模型的行为。在真实的 10,000 token 上限下替身不会重置：2026-10-24 的命令行替身试运行中，gpt-b 的请求最多为 8,943（U）和 9,500（F）token，结果为 U 落地 95、F 落地 55；2026-10-20 的真实 gpt-b 在相近任务上达到 16.6–19.7K token。

## 预测（运行前写定）

- 真实 gpt-b 每轮大约重置 1–2 次，多在恢复阶段（依据上面的记录峰值）。
- F 组的过期合入少于 U 组。U 组的比例不预测：模型可能自己重读 202。

## 采纳标准（现在决定）

只有当 F 组 10 轮中的过期合入至少比 U 组少 3 轮，且成功率不低于 U 组时，才在后续工作中保留新鲜度标记。否则结论为：在这个任务上没有证据支持新鲜度标记。

## 本实验回答不了的问题

- 任务是为让过期事实起作用而设计的：仓库更新是脚本，且只改 Agent 读而不写的行。
- 写入时的过期由 Forkpoint 的合入检查处理；本实验不比较两者。
- 只用 Claude；每组 10 轮发现不了较小的差异；10,000 token 是缩小后的窗口。
- 替身试运行是脚本化的。

## 文件与运行

| 内容 | 命令 |
|---|---|
| 测试 | `python -m unittest discover -s experiments/task_state -v`：19 项，自建并删除测试库；其中替身试运行约需 2–3 分钟 |
| 替身试运行（命令行） | `python experiments/task_state/run_state_trials.py --claude experiments/task_state/fake_state_claude.py --config-dir <目录> --workdir <新目录> --output <新目录> --runs-per-setup 1 --pilot`（真实上限下不会重置） |
| 试运行（须经用户同意使用额度） | `python experiments/task_state/run_state_trials.py --claude <claude.exe> --config-dir <只含登录的 CLAUDE_CONFIG_DIR> --workdir <新目录> --output experiments/task_state/results/<日期>-pilot --runs-per-setup 1 --pilot` |
| 样本（须经用户同意） | 同上，去掉 `--pilot`，改为 `--runs-per-setup 10`，输出到 `experiments/task_state/results/<日期>` |

原始记录只保存在作者的机器上：`experiments/task_state/results/` 不纳入版本库。每轮的测试库 `agentdb_test_gpt_*` 保留以便核查。

## 试运行（2026-10-24，不计入样本）

经用户同意，在提交 4a40fe8 上用 `--pilot` 每组各跑一轮：Claude Code 2.1.308，claude-opus-5-5，推理强度 xhigh。原始记录在 `experiments/task_state/results/2026-10-24-pilot`（不纳入版本库）；测试库为 `agentdb_test_gpt_a7067ae4fa`（U）和 `agentdb_test_gpt_134b30fb20`（F）。

| | U | F |
|---|---|---|
| 结果 | 成功；101 的库存为 55（正确） | 成功；101 的库存为 55（正确） |
| gpt-b 重置 | 2 次，都在恢复阶段；触发时请求 11,093 和 11,176 token | 3 次（上限），都在恢复阶段；触发时请求 11,546、10,150 和 10,136 token |
| 交接大小 | 2,723 和 1,770 字节 | 每次 4,429 字节 |
| 交接中的标记 | 无 | 每次 8 个事实：5 个已过期，3 个未知，0 个新鲜 |
| 仓库更新后 gpt-b 读 202 | 3 次；第一次在第 1 次重置之后、重新提案之前 | 4 次；第一次在第 1 次重置之后、重新提案之前 |
| 工具调用（gpt-b / gpt-a） | 52（37 / 15） | 64（49 / 15） |
| 出错的调用 | 2：gpt-b 的第一次检查点被拒绝；gpt-a 一次 resume 时尚无状态 | 4：同左两项，另有 gpt-b 两次 fork 失败 |
| 全部 token：输入（其中缓存读取）/ 输出 | 213,478（177,214）/ 13,585 | 324,647（281,017）/ 16,383 |
| gpt-b 第 1 次重置后的 token：输入（其中缓存读取）/ 输出 | 94,644（77,873）/ 5,370 | 203,273（177,710）/ 7,980 |
| 按标价估算的单轮费用 | $0.60 | $0.73 |
| 用时 | 285 秒 | 335 秒 |

两轮中每个 MCP 请求都有回复并被记录，每个回复都来自 claude-opus-5-5；没有已发布的覆盖、对方分支的改动、审计错误或未结束的事务；202 为 60，其余行与基线不变。

观察：
- 两轮的 gpt-b 都自己计划了重读。两轮中 gpt-b 第一个成功的检查点（在仓库更新之前）都已在下一步和未决事项中写明：合入前在当前 main 上重读 202，若有变化就按新值重新提案。重置后的进程从交接中得到这一计划，所以 U 组没有任何标记也重读了 202。因此本次试运行中，标记没有可以纠正的过期决定。
- 重置都在恢复阶段：准备阶段结束时 gpt-b 的请求已超过 10,000 token（11,901 和 12,415），所以第一次重置发生在恢复阶段的第一个工具调用边界。gpt-a 没有重置：它唯一超过 10,000 的请求（F 组，10,222）是最后的回答，之后没有工具调用。F 组第 3 次重置之后，最后一个进程的请求最多达到 17,383 token。
- 按行的规则，每个标记都正确：5 个已过期的事实读自 101（被 gpt-a 的价格合入改动）或 202（被仓库更新改动），3 个未知的事实没有记录读取的行。与库存决定有关的只有读自 202 的 2 个；另外 3 个读自 101，其中 2 个是基线上的值。
- F 组的交接较大（标记带有变化行的前后值），重置更多，token 也更多；每组只有一轮，不能据此判断原因。
- F 组的 gpt-b 在恢复阶段的几次重置之间没有做检查点，后来的进程不知道前一个进程已建的分支：两次 fork 因分支名已存在而失败（`agent_lab.py` 以 Python traceback 报告这一 23505 错误，而不是 JSON 拒绝），提案也重做了一次。
- 两轮中 gpt-b 的第一次检查点都被拒绝，因为它给关于代码或版本的事实附上了不含商品行的 based_on；拒绝信息说明了原因，下一次调用去掉这些 based_on 后成功。gpt-a 未被要求也调用了 checkpoint（2 次和 3 次）和 resume（1 次，尚无状态）。

对照预测：
- “每轮重置约 1–2 次，多在恢复阶段”：U 组符合；F 组为 3 次；全部在恢复阶段。
- “F 组的过期合入少于 U 组”：无法检验，两组都没有过期合入。

决定：用户于 2026-10-24 要求暂缓样本；样本尚未登记或运行。每组一轮的试运行不能用来判断采纳标准；若以后运行样本，按本文登记的设计进行。从这次试运行看，在现在的提示下模型不需要标记也会计划重读，这个任务可能区分不出两组；能让标记起作用的任务需要另行设计并重新登记。

## 结论（2026-10-24）

新鲜度标记不予采纳。这是用户依据试运行作出的决定；登记的样本没有运行，所以这不是采纳标准给出的结果。

- 没有收益：两组的 gpt-b 都在第一个检查点中自己计划了重读 202，标记没有可以纠正的过期决定。
- 标记有确定的成本：每次交接多 1.7–2.7 KB（4,429 字节对 2,723 和 1,770 字节），所以 F 组每个新进程的第一个请求多 830–1,210 token（4,922 对 4,090 和 3,716），更快到达重置上限。
- 试运行中 F 组花费更多：多 114K token、49 秒和 $0.14。F 组多重置一次，并重做了 fork 和提案；较大的交接可能是部分原因，但每组只有一轮，测不出差别中有多少来自标记。作为参照，上下文存档样本（同一运行器与模型，任务不同）中同一组完成的各轮之间最多相差 105K token、53 秒和 $0.06（A，9 轮）以及 137K token、62 秒和 $0.09（B，9 轮）；第 19–20 轮因 Pro 会话额度中断，不计在内。这次的 token 差别在 B 组的离散之内、略高于 A 组，用时差别在两组的离散之内，费用差别超出了两组。
- 检查点与恢复没有被检验：U 组同样有检查点，本实验没有与无检查点的做法比较。两组的 Agent 都在 2–3 次重置后落地了正确结果，且没有覆盖；这只说明机制可用，不说明它优于其他做法。
- 与上下文存档实验相同的规律：两项功能提供的信息，claude-opus-5-5（xhigh）没有它们也会自己得到。存档输给了模型自己的说明（保留了全部 14 个值），标记输给了模型自己计划的重读。这只对这两个任务以及这一模型和推理强度成立。
- 尚未检验的推测（不是结果）：模型自己做不到的是强制执行。Forkpoint 的合入检查拒绝写入别人改过的行，与 Agent 是否谨慎无关。本次试运行中它没有拒绝任何调用：两组的 gpt-b 都没有尝试合入旧提案，它们在检查点中已预计旧提案会与价格改动冲突。
- 产品上不加入新鲜度标记。原型留在 `experiments/task_state/` 作为记录；`vc.*` 与 `agent_state` 没有改动。

## 回滚

全部内容在 `experiments/task_state/` 和本文件中；`vc.*`、`agent_state` 与项目数据库都没有改动。删除它们即恢复原状。

---

<a id="english-version"></a>

# Checkable Task State: Experiment Design

Status: registered on 2026-10-24, before any model run. The pilot (see "Pilot" below; not part of the sample) ran the same day with the user's approval, and both setups landed the correct 55; the sample was not run, and on the pilot the user decided not to adopt freshness marks (see "Conclusion" below). Real rounds need the user's approval for model quota. The checks and the stand-in dry run below call no model. Task state is kept in a MemoryStore inside the runner process (cloud work is on hold; durable storage will be decided separately). Local only; no cloud service is accessed.

Changes after the 2026-10-24 pilot: the tool description's error list now names `agent_lab.py`'s fork and propose refusals (`stale_branch_head`, `branch_exists` with the branch's head, `not_found`), which makes the description 116 bytes longer; those failures used to be tracebacks, recorded by the runner as `helper_failed`. The pilot above used the old description and the old behaviour, so another run differs in both.

After a review on 2026-10-25, three runner defects were fixed. The success rule now checks main's final price 11 and `PRICE = 11`, as the registered definition says (before, gpt-a having landed once was enough, even if the price changed later). The resume packet makes room for the freshness marks of the facts it shows only, leaving facts out together with their marks (before, it reserved room for every fact's mark, so with many facts setup F reported an exhausted budget where setup U still resumed). A saved state that fails to resume at a reset is no longer handed to the new process as "nothing saved yet": the round stops and is kept as a failure. Neither pilot round was affected: both ended at price 11 with `PRICE = 11.0`, every reset had a saved state, and no resume failed.

## Question

When an agent is reset several times mid-task and continues from its checkpoints, does marking each checkpointed fact fresh, stale or unknown, from the revision and rows it was read from (setup F), make it decide on stale facts less often than the same checkpoints without those marks (setup U)?

## Background

- The [context archive experiment](CONTEXT_ARCHIVE_EXPERIMENT.md) found no advantage for a word-for-word archive over the model's own note, and a check of 3 real compactions on this machine found no measurable loss, probably because the state was written in commits, docs and memory files.
- A written record helps only while it is still right. After a reset the agent trusts it; if the data it was based on has since been changed by someone else, the agent does not know.
- Forkpoint's merge checks already catch this at write time: a row someone else changed causes a conflict. Rows an agent reads but does not write have no such protection (for example, deciding from another product's stock). This experiment is only about that case.

## Prototype (experiment only; `vc.*` and `agent_state` are not modified)

- `experiments/task_state/task_state.py`: task state stays a record in `agent_state`'s engine (goal, status, next action, open items, facts, evidence references), and the engine is unchanged. A checkpoint can attach `based_on: {revision, rows}` to a fact, stored as an ordinary evidence object; older records load as before, and their facts are unknown.
- Freshness: each of those rows is compared between that revision and current main (`vc._rows_at_keys`, 008): all the same is fresh; one changed or deleted is stale (with up to 3 rows before and after); no recorded reads, or a row not found at that revision, is unknown (so a fact with a wrong key is never taken as unchanged).
- Resume packet: the project's existing size-limited context packet (`Runtime.context`); in setup F each fact in it carries a mark, and stale facts left out by the budget are counted; the whole packet stays within 8 KB. Setup U's packet is the same without marks.
- A retried checkpoint returns its first result even after the state has moved on (it is recognised by a digest of the checkpoint itself).

## Task

- Each round's setup: `agent_lab.py init` (4,000 rows, price 10, stock 100).
- The rule: product 101 is restocked from product 202, so 101's stock must equal 202's current stock minus 5.
- gpt-b's preparation (`PREPARE_S`): at the baseline, read 101 and 202 and propose 101's stock by the rule (95 at the baseline) with a matching `STOCK` constant, without merging; before finishing, checkpoint the proposal revision, the original expected main, and each fact the stock value depends on, with the revision and rows it was read from.
- gpt-a (`RUN_A`): as in the 2026-10-17 and 2026-10-20 experiments, raise 101's price by 10% and merge.
- Warehouse update: after gpt-a's stage the runner commits product 202's stock 60 on main.
- gpt-b's recovery (`LAND_S`): resume the saved task state first, then land 101's stock change on main by the rule. The right result is 55; a decision on the stale fact gives 95.
- Both setups get the same tool: `agent_lab.py`'s actions without `verify` (which checks the old task's 95), plus `read-product`, `checkpoint` and `resume`, answered by the runner. 60 calls per agent.
- Success: both stages complete; main has 101 at stock 55 with `STOCK = 55` and price 11 with `PRICE = 11`; 202 at 60; every other row and the baseline unchanged; no open transaction, published overwrite or edit of the other agent's branch; every response from the registered model; every MCP request answered; no archive error.

## Setups and resets

- U: `resume` without freshness marks.
- F: `resume` marks each fact fresh, stale or unknown.
- Common to both: the first time a request's input exceeds 10,000 tokens, the agent is reset at the next tool-call boundary; its new process gets the current stage's instructions and its resume packet (`HEADER_S`). At most 3 resets per agent and round, with no trigger after the third. Calls during a reset are refused (`context_switch`), recorded and not counted against the budget.

## Schedule

- Pilot: one round of each setup with `--pilot`, outside the sample, reported separately.
- Sample: 20 rounds, odd rounds U and even rounds F, two at a time; claude-opus-5-5 at xhigh effort, Claude Code 2.1.308; exact versions recorded in protocol.json. All rounds are kept, no failed round is replaced, and nothing is rescued by hand.

## Measurements

- Primary, per round: whether gpt-b landed the stale decision, that is, product 101's stock is 95 on main at the end.
- Secondary: success; the outcome (correct, stale or other); gpt-b's reads of product 202 after the warehouse update; resets per agent; calls and tokens after the resets; resume packet sizes.

## Checks and stand-in dry run (no model calls)

- `test_task_state.py` (9 tests): a fact turns stale when its row changes on main, fresh again when the row changes back, and is unaffected by other rows; no recorded reads or a wrong key is unknown; setting or dropping a fact replaces or removes its reads; a retry is not applied twice; bad checkpoints are refused; the resume packet stays within its budget and counts stale facts it left out; when the marks of all facts would not fit, facts are left out together with their marks and the state still resumes; an older record still resumes.
- `test_run_state_trials.py` (10 tests): the outcome rule; the success rule's final price 11 with `PRICE = 11`; at a reset, nothing saved is handed over as such, and a saved state that fails to resume stops the round; the same tool in both setups, without verify; `read-product`'s argument checks; only setup F marks facts; and an in-process dry run of one round per setup with the stand-in and the reset limit lowered to 4,000 tokens: gpt-b resets 1–3 times, every handoff carries the stage's instructions, and only setup F's carry marks.
- The stand-in is a script: it re-reads 202 only when the resume packet marks the fact stale, so setup U lands 95 and setup F 55. This shows the path works and predicts nothing about a model. With the real 10,000-token limit the stand-in never resets: in the command-line stand-in dry run on 2026-10-24, gpt-b's requests peaked at 8,943 (U) and 9,500 (F) tokens, and U landed 95 and F 55; the real gpt-b reached 16.6–19.7K tokens on a similar task on 2026-10-20.

## Predictions (written before running)

- The real gpt-b resets about 1–2 times per round, mostly in its recovery (from the recorded peaks above).
- F lands fewer stale decisions than U. No prediction is made for U's share: the model may re-read 202 on its own.

## Adoption rule (decided now)

Freshness marks are kept for further work only if F lands a stale decision in at least 3 fewer of its 10 rounds than U, and F's success rate is not lower. Otherwise the conclusion is: no evidence for freshness marks on this task.

## What this experiment cannot tell us

- The task was designed for a stale fact to matter: the warehouse update is scripted, and it changes only a row the agent reads but does not write.
- Staleness at write time is handled by Forkpoint's merge checks; this experiment does not compare the two.
- Claude only; 10 rounds per setup cannot detect small differences; 10,000 tokens is a scaled-down window.
- The stand-in dry run is scripted.

## Files and running

| Purpose | Command |
|---|---|
| Tests | `python -m unittest discover -s experiments/task_state -v`: 19 tests, building and dropping their own test databases; the stand-in dry run takes about 2–3 minutes of it |
| Stand-in dry run (command line) | `python experiments/task_state/run_state_trials.py --claude experiments/task_state/fake_state_claude.py --config-dir <dir> --workdir <new dir> --output <new dir> --runs-per-setup 1 --pilot` (no resets at the real limit) |
| Pilot (needs the user's approval for quota) | `python experiments/task_state/run_state_trials.py --claude <claude.exe> --config-dir <CLAUDE_CONFIG_DIR holding only the login> --workdir <new dir> --output experiments/task_state/results/<date>-pilot --runs-per-setup 1 --pilot` |
| Sample (needs the user's approval) | The same without `--pilot`, with `--runs-per-setup 10` and output to `experiments/task_state/results/<date>` |

Raw records stay on the author's machine only: `experiments/task_state/results/` is not in the repository. Each round's `agentdb_test_gpt_*` test database is kept for checking.

## Pilot (2026-10-24, not part of the sample)

With the user's approval, one round of each setup was run with `--pilot` at commit 4a40fe8: Claude Code 2.1.308, claude-opus-5-5 at xhigh effort. Raw records are in `experiments/task_state/results/2026-10-24-pilot` (not in the repository); the test databases are `agentdb_test_gpt_a7067ae4fa` (U) and `agentdb_test_gpt_134b30fb20` (F).

| | U | F |
|---|---|---|
| Result | success; product 101 at stock 55 (correct) | success; product 101 at stock 55 (correct) |
| gpt-b resets | 2, both in the recovery; requests at the trigger 11,093 and 11,176 tokens | 3 (the cap), all in the recovery; requests at the trigger 11,546, 10,150 and 10,136 tokens |
| Handoff size | 2,723 and 1,770 bytes | 4,429 bytes each time |
| Marks in the handoff | none | each time 8 facts: 5 stale, 3 unknown, 0 fresh |
| gpt-b's reads of 202 after the warehouse update | 3; the first after reset 1, before re-proposing | 4; the first after reset 1, before re-proposing |
| Tool calls (gpt-b / gpt-a) | 52 (37 / 15) | 64 (49 / 15) |
| Calls with an error | 2: gpt-b's first checkpoint refused; one resume by gpt-a with no state yet | 4: the same two, plus two failed forks by gpt-b |
| All tokens: input (of which cache read) / output | 213,478 (177,214) / 13,585 | 324,647 (281,017) / 16,383 |
| gpt-b tokens after reset 1: input (of which cache read) / output | 94,644 (77,873) / 5,370 | 203,273 (177,710) / 7,980 |
| List-price estimate for the round | $0.60 | $0.73 |
| Wall time | 285 s | 335 s |

In both rounds every MCP request was answered and recorded, every response came from claude-opus-5-5, and there was no published overwrite, peer branch edit, audit error or open transaction; product 202 was at 60, and every other row and the baseline were unchanged.

Observations:
- Both gpt-b agents planned the re-read themselves. In both rounds, gpt-b's first successful checkpoint, before the warehouse update, already set its next action and open items to re-read 202 at current main before merging, and to re-propose with the new value if it had changed. Each process after a reset got that plan in its handoff, so setup U re-read 202 without any mark. The marks therefore had no stale decision to correct in this pilot.
- All resets came in the recovery: by the end of the preparation gpt-b's requests were already over 10,000 tokens (11,901 and 12,415), so the first reset came at the first tool-call boundary of the recovery. gpt-a never reset: its only request over 10,000 (10,222, in setup F) was its final answer, with no tool call after it. After setup F's third reset, the last process's requests reached 17,383 tokens.
- Every mark was right by the row rule: the 5 stale facts were read from product 101 (changed by gpt-a's price merge) or 202 (changed by the warehouse update), and the 3 unknown facts had no recorded rows. Only the 2 read from 202 bore on the stock decision; the other 3 were read from 101, 2 of them baseline values.
- Setup F's handoffs were larger (the marks carry the changed rows before and after), and it reset more and used more tokens; with one round per setup this cannot say why.
- In setup F gpt-b did not checkpoint between its resets in the recovery, so a later process did not know the branch an earlier one had made: two forks failed on a branch name that already existed (`agent_lab.py` reported the 23505 error as a Python traceback, not a JSON refusal), and one proposal was made again.
- In both rounds gpt-b's first checkpoint was refused because it attached based_on without product rows to facts about code or a revision; the refusal said why, and the next call, without those based_on, succeeded. gpt-a, not asked to, also called checkpoint (2 and 3 times) and resume (once, with no state yet).

Against the predictions:
- "About 1–2 resets per round, mostly in the recovery": met in U; F had 3; all were in the recovery.
- "F lands fewer stale decisions than U": cannot be checked; neither setup landed one.

Decision: on 2026-10-24 the user put the sample on hold; it has not been registered or run. A pilot of one round per setup does not decide the adoption rule; if the sample runs later, it runs as registered here. On this pilot, with the current prompts the model plans the re-read without marks, so this task may not tell the two setups apart; a task where marks could matter needs its own design and a new registration.

## Conclusion (2026-10-24)

Freshness marks are not adopted. The user decided this on the pilot; the registered sample was not run, so this is not an outcome of the adoption rule.

- No benefit: in both setups gpt-b planned the re-read of 202 in its first checkpoint, so the marks had no stale decision to correct.
- The marks have a fixed cost: each handoff was 1.7–2.7 KB larger (4,429 bytes against 2,723 and 1,770), so each new process in setup F started with a first request 830–1,210 tokens larger (4,922 against 4,090 and 3,716) and reached the reset limit sooner.
- Setup F cost more in the pilot: 114K tokens, 49 s and $0.14 more. It reset once more and made a fork and a proposal again; the larger handoffs may explain part of that, but with one round per setup, how much of the gap the marks caused cannot be measured. For scale, in the context archive sample (same runner and model, a different task) the completed rounds of one setup differed by at most 105K tokens, 53 s and $0.06 (A, 9 rounds) and 137K tokens, 62 s and $0.09 (B, 9 rounds); rounds 19–20, cut short by the Pro session limit, are left out. The pilot's token gap is within B's spread and a little above A's, its time gap is within both, and its cost gap is beyond both.
- Checkpoint and resume were not tested: setup U had checkpoints too, and this experiment made no comparison with going without them. In both setups the agent landed the correct result after 2–3 resets, with no overwrite; that shows the mechanism works, not that it beats other ways.
- The same pattern as the context archive experiment: both features supplied information claude-opus-5-5 at xhigh effort works out by itself without them. The archive lost to the model's own note (which kept all 14 values), and the marks lost to the model's own planned re-read. This holds only for these two tasks and this model and effort.
- An untested guess, not a result: what the model cannot do by itself is enforcement. Forkpoint's merge checks refuse a write to a row someone else changed, however careful the agent is. They refused no call in this pilot: neither gpt-b tried to merge its old proposal, and both had noted in their checkpoint that it would conflict with the price change.
- In the product, freshness marks are not added. The prototype stays in `experiments/task_state/` as a record; `vc.*` and `agent_state` are unchanged.

## Rollback

Everything is in `experiments/task_state/` and this document; `vc.*`, `agent_state` and the project database are unchanged. Removing them restores the previous state.
