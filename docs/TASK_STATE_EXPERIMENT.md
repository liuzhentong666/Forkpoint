中文 | [English](#english-version)

# 可核查的任务状态：实验设计

状态：2026-10-24 在任何模型运行之前登记。尚未运行任何模型。真实轮次须经用户同意使用模型额度；先各跑一轮试运行（pilot），单独报告，不计入样本。下文的检查和替身试运行都不调用模型。任务状态保存在运行器进程内的 MemoryStore 中（云端工作暂缓，正式持久化另行决定）；仅限本机，不访问任何云服务。

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

- `test_task_state.py`（8 项）：行在 main 上改变后事实变为过期、改回后恢复新鲜、改动其他行不影响；没有读取记录或写错键为未知；重新设置或删除事实时替换或删除其读取记录；重试不重复执行；错误的检查点被拒绝；恢复包在预算内并统计被省略的过期事实；旧记录照常恢复。
- `test_run_state_trials.py`（7 项）：结果判定；两组工具相同且没有 verify；`read-product` 的参数检查；只有 F 组标记事实；以及在本进程内用替身各跑一轮、把重置上限降到 4,000 token 的试运行：每个 Agent 重置 1–3 次，交接包含当前阶段指令，只有 F 组的交接带标记。
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
| 测试 | `python -m unittest discover -s experiments/task_state -v`：15 项，自建并删除测试库；其中替身试运行约需 2–3 分钟 |
| 替身试运行（命令行） | `python experiments/task_state/run_state_trials.py --claude experiments/task_state/fake_state_claude.py --config-dir <目录> --workdir <新目录> --output <新目录> --runs-per-setup 1 --pilot`（真实上限下不会重置） |
| 试运行（须经用户同意使用额度） | `python experiments/task_state/run_state_trials.py --claude <claude.exe> --config-dir <只含登录的 CLAUDE_CONFIG_DIR> --workdir <新目录> --output experiments/task_state/results/<日期>-pilot --runs-per-setup 1 --pilot` |
| 样本（须经用户同意） | 同上，去掉 `--pilot`，改为 `--runs-per-setup 10`，输出到 `experiments/task_state/results/<日期>` |

原始记录只保存在作者的机器上：`experiments/task_state/results/` 不纳入版本库。每轮的测试库 `agentdb_test_gpt_*` 保留以便核查。

## 回滚

全部内容在 `experiments/task_state/` 和本文件中；`vc.*`、`agent_state` 与项目数据库都没有改动。删除它们即恢复原状。

---

<a id="english-version"></a>

# Checkable Task State: Experiment Design

Status: registered on 2026-10-24, before any model run. No model has run. Real rounds need the user's approval for model quota; a pilot round of each setup comes first and is reported separately, outside the sample. The checks and the stand-in dry run below call no model. Task state is kept in a MemoryStore inside the runner process (cloud work is on hold; durable storage will be decided separately). Local only; no cloud service is accessed.

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

- `test_task_state.py` (8 tests): a fact turns stale when its row changes on main, fresh again when the row changes back, and is unaffected by other rows; no recorded reads or a wrong key is unknown; setting or dropping a fact replaces or removes its reads; a retry is not applied twice; bad checkpoints are refused; the resume packet stays within its budget and counts stale facts it left out; an older record still resumes.
- `test_run_state_trials.py` (7 tests): the outcome rule; the same tool in both setups, without verify; `read-product`'s argument checks; only setup F marks facts; and an in-process dry run of one round per setup with the stand-in and the reset limit lowered to 4,000 tokens: each agent resets 1–3 times, every handoff carries the stage's instructions, and only setup F's carry marks.
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
| Tests | `python -m unittest discover -s experiments/task_state -v`: 15 tests, building and dropping their own test databases; the stand-in dry run takes about 2–3 minutes of it |
| Stand-in dry run (command line) | `python experiments/task_state/run_state_trials.py --claude experiments/task_state/fake_state_claude.py --config-dir <dir> --workdir <new dir> --output <new dir> --runs-per-setup 1 --pilot` (no resets at the real limit) |
| Pilot (needs the user's approval for quota) | `python experiments/task_state/run_state_trials.py --claude <claude.exe> --config-dir <CLAUDE_CONFIG_DIR holding only the login> --workdir <new dir> --output experiments/task_state/results/<date>-pilot --runs-per-setup 1 --pilot` |
| Sample (needs the user's approval) | The same without `--pilot`, with `--runs-per-setup 10` and output to `experiments/task_state/results/<date>` |

Raw records stay on the author's machine only: `experiments/task_state/results/` is not in the repository. Each round's `agentdb_test_gpt_*` test database is kept for checking.

## Rollback

Everything is in `experiments/task_state/` and this document; `vc.*`, `agent_state` and the project database are unchanged. Removing them restores the previous state.
