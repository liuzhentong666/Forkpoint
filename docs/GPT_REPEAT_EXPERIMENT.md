中文 | [English](#english-version)

# 十轮真实 GPT 实验：结果与原始调用记录

2026-10-17。独立测试库位于已有 WSL PostgreSQL；未运行 Claude，未访问真实云存储。

2026-10-20 又用 Claude 按同一协议重复了十轮，结果单列在[文末](#claude-run-2026-10-20)；从这里到“本组运行错误”是 2026-10-17 的 GPT 记录，内容未改动。

本次预先登记 **10 轮**，记录 **10 轮**，成功 **8/10（80%）**，模型阶段全部完成的轮数为 **8**。
这里的成功率以全部预登记轮次为分母，包括模型服务中断。对应的 95% Wilson 区间为 **49.0%–94.3%**，仅作为二项模型下的小样本描述；共享账户和服务故障可能相关，不能据此估计模型固有能力或生产可靠性。

模型为运行接口明确返回的 **`gpt-6-astra`**，推理强度 **`xhigh`**；运行器为 `codex-cli 0.163.0-alpha.10.2`。每个 Agent 的返回值及模型改派事件均在原始日志和结果中保留。这是接口报告的精确模型 ID；服务商未暴露底层权重快照。原来的单轮实验没有记录精确型号，不计入本次样本。

观察到已发布字段/代码覆盖：**0/10 轮**；修改另一 Agent 分支：**0/10 轮**。检查覆盖每次实际执行的写操作后的数据库状态，包含失败请求；即使后来修复，先前的覆盖仍记为失败。中途停止的轮次只说明已执行部分未发生覆盖，不证明它们若继续也不会覆盖。

## 每轮结果

每轮的完整记录 `experiments/gpt_repeat/results/2026-10-17/run-NN/result.json` 只保存在作者本机，不在仓库中。

| 轮次 | 结果 | 工具调用 | 输入 / 其中缓存 | 输出 / 其中推理 | 总 tokens | 已发布覆盖 |
|---|---|---:|---:|---:|---:|---:|
| 01 | 通过 | 32 | 316,723 / 263,680 | 2,564 / 236 | 319,287 | 0 |
| 02 | 通过 | 31 | 315,721 / 279,808 | 2,692 / 176 | 318,413 | 0 |
| 03 | 运行错误 | 7 | 56,137 / 43,136 | 854 / 318 | 56,991（部分） | 0 |
| 04 | 运行错误 | 1 | 10,257 / 0 | 81 / 0 | 10,338（部分） | 0 |
| 05 | 通过 | 32 | 307,406 / 268,544 | 2,704 / 210 | 310,110 | 0 |
| 06 | 通过 | 31 | 303,866 / 276,480 | 2,649 / 200 | 306,515 | 0 |
| 07 | 通过 | 33 | 323,645 / 273,920 | 2,560 / 67 | 326,205 | 0 |
| 08 | 通过 | 31 | 318,497 / 290,944 | 2,718 / 280 | 321,215 | 0 |
| 09 | 通过 | 32 | 319,639 / 283,008 | 2,891 / 241 | 322,530 | 0 |
| 10 | 通过 | 31 | 316,749 / 267,904 | 2,766 / 226 | 319,515 | 0 |

合计 **261 次工具调用**；两个 Agent 均有用量记录的轮数为 **8/10**。把中断轮次已返回的单 Agent 用量也计入，观察到 **2,611,119 tokens**，其中输入 2,588,640、输出 22,479；缓存输入 2,247,424 与推理输出 1,954 是各自的子项，不再相加。

“部分”表示该轮只取得部分 thread 的累计用量，详见原始通知和逐 Agent 记录；未返回的用量保持未知。summary.json 原有合计只包含两个 thread 均有数据的轮次，behavior-analysis.json 另合计所有已报告用量，两者统计口径不同。

用量来自每个 thread 最后一次累计通知，再对 thread 求和，不对逐次累计事件重复求和。它包含多次请求重发的上下文，不能解释为读取了同等数量的独立数据库内容。不用字符数估算缺失值，也不把这些计数当作账单核对；请求断线或服务端未返回的用量可能无法在客户端获得。启动/重连错误同样保留在原始流中。计时包含本机工具、旁路核验和网络重连，不用于模型速度排名。

## 行为差异与运行失败

真正完成三阶段的轮次有 8 轮，其中 8 轮达成目标；未完成轮次仍保留在主成功率的分母中。完成轮次中，8 轮依次遇到 stale_head / 40001 与 merge_conflict / P0001。这个顺序由提示词明确安排，不能当作 Agent 在没有提示时自行发现恢复策略的证据。

完成轮次每轮工具调用为 31–33 次，均值 31.6 次，中位数 31.5 次。逐动作和逐阶段计数见 behavior-analysis.json；它们可以区分额外核验与重试成本，不应只用最终成功一个数字概括模型行为。

本组的差异主要出现在读取与核验：info 每轮 5–7 次、read-code 9–10 次、diff 5–6 次。八个完成轮次的写入步骤相同：3 次建分支、3 次联合提案、4 次合入请求，其中 2 次合入被预期的冲突检查拒绝。结果一致并不意味着调用路径或资源消耗一致。

收到 usageLimitExceeded 的轮次：3, 4；出现其他数据库工具错误的轮次：无。额度错误属于运行服务返回，不能直接归因于数据库或模型推理。

第 3、4 轮中断后，单独的只读额度查询又返回 ordinaryUsageAllowed=true，并显示一般 Codex 窗口仍有剩余额度；后续轮次继续取得真实工具调用。这与失败 turn 的额度报错不一致，具体原因未确认，不能表述为整个账户额度确定耗尽。查询原始报文另存 diagnostics/quota-status，未改变登录、模型或账户设置。

## 实验条件与判定

每轮两个全新 GPT 上下文和一个独立的 4,000 行测试库。库存 Agent 先从共同基线准备库存 100→95 的提案；价格 Agent 再从同一基线准备并合入价格 10→11 的提案；最后库存 Agent 根据错误和当前状态完成合入。每个任务同时修改对应的 Python 政策文件。最多并行两轮，同一个库内的操作分阶段进行。

三段提示词、工具定义、模型与强度在十轮中保持一致，均预先写入 protocol.json；没有根据某轮结果进行人工补救或替换失败样本。提示词明确安排先用旧 expected 检查冲突，并提示保留其他工作、必要时重新提案，因此仍属于受控、带恢复提示的实验。与原单轮相比，本次用结构化工具包装相同的 agent_lab.py 能力，read-code 从开始即可用，且没有故意注入额度中断。

两个 Agent 的数据库能力相同：都能请求修改 price 或 stock；记录器不强制纠正错误字段。成功要求两个 Agent 均完成自己的合入，最终价格 11、库存 95、两份代码常量正确，其他 3,999 行和基线未改变，没有未完成事务，且全过程未覆盖对方已发布内容或修改对方分支。代码只检查语法和常量，没有执行。

覆盖检测器先通过了“覆盖后又恢复”的反例测试。每次写请求之后的独立 SQL 核验保存在 database_audit.jsonl；离线审计重新计算覆盖事件，核对了全部 82 次实际写请求的快照，并确认工具原文和计数一致。这个观测范围覆盖通过实验工具完成的提交，不声称验证任意 SQL、DDL、外部程序或云存储。

## 原始证据与重新分析

以下文件位于 `experiments/gpt_repeat/results/2026-10-17/`，只保存在作者本机，不在仓库中：

- 预登记协议、提示词、源文件哈希：`protocol.json`
- 机器可读汇总：`summary.json`
- 逐动作差异与部分用量分析：`behavior-analysis.json`
- 原始记录一致性检查：`log-audit.json`
- 文件 SHA-256 清单：`SHA256SUMS.json`

每轮目录都包含以下文件：

| 文件 | 内容 |
|---|---|
| raw/server.jsonl | app-server 标准输出的原始 JSONL 字节，先保存再解析 |
| raw/client.jsonl | 实际发给 app-server 的请求及工具响应字节 |
| raw/tool_calls.jsonl | 原样抽出的工具请求与响应，按 callId / RPC id 关联 |
| raw/transport_timing.jsonl | 单独记录接收时间，不改写原始报文 |
| raw/server.stderr.log | 运行器错误与重连记录 |
| database_audit.jsonl | 每次写操作后的独立数据库状态 |
| result.json | 可重新计算的汇总、模型标识、用量与结果 |

JSONL 中的调用参数与返回内容没有被摘要替换或截断；离线检查确认工具原文逐行出自 server/client 原始流。前置的接口探测和一次回显预检另存 diagnostics/preflight 与 diagnostics/instrumentation-pilot，不计入这十轮的调用数和用量。本机生成的协议 schema 另存 protocol-schema.zip。原始文件按本地实验材料保留，并通过 .gitignore 避免意外提交账号运行元数据；发布源码不等于发布这些日志。

在项目目录复查现有记录，不调用模型、不写数据库：

```console
python experiments/gpt_repeat/audit_repeat_logs.py experiments/gpt_repeat/results/2026-10-17
```

重新进行一组实验需新的输出目录，会使用现有 Codex 登录的模型额度并创建新的专用测试库：

```console
.\.venv-cloud\Scripts\python.exe experiments/gpt_repeat/run_gpt_repeats.py --project . --output experiments/gpt_repeat/results/NEW_RUN --runs 10 --workers 2 --model gpt-6-astra --effort xhigh
```

记录入口依据 [OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server) 与本机版本生成的 schema。**云存储仍未经真实验证，云端部署暂缓。**

## 本组运行错误

- 第 3 轮：`RuntimeError`；完整错误原文见 `experiments/gpt_repeat/results/2026-10-17/run-03/result.json` 及该轮原始日志（均只保存在作者本机）。
- 第 4 轮：`RuntimeError`；完整错误原文见 `experiments/gpt_repeat/results/2026-10-17/run-04/result.json` 及该轮原始日志（均只保存在作者本机）。

<a id="claude-run-2026-10-20"></a>

## 2026-10-20：用 Claude 按同一协议重复十轮

2026-10-20 21:41（UTC+8）登记并开始。测试库仍位于已有的 WSL PostgreSQL 16.15，每轮新建；未访问真实云存储。上面的 GPT 记录保持原样。

本次预先登记 **10 轮**，记录 **10 轮**，成功 **10/10（100%）**，三个模型阶段全部完成的轮数为 **10**。95% Wilson 区间为 **72.2%–100%**，仍只是二项模型下的小样本描述，不能据此估计模型固有能力或生产可靠性。两组的运行器、提示词外壳和引擎代码也不完全相同（见下文），不宜把 8/10 与 10/10 直接当作模型优劣的比较。

模型为运行接口明确返回的 **`claude-opus-5-5`**：每个 Agent 的 init 事件，以及每一次 API 响应的 `message.model`，都是这个 ID，没有出现其他模型。推理强度以 `--effort xhigh` 请求；Claude Code 的 init 事件不回显强度值，因此只记录了请求值。运行器为 Claude Code `2.1.299`，以 headless 方式运行（`claude -p`，stream-json 输入输出）。登录方式为 claude.ai Pro 订阅，没有使用 API key。服务商未暴露底层权重快照。

观察到已发布字段/代码覆盖：**0/10 轮**；修改另一 Agent 分支：**0/10 轮**。检查覆盖全部 95 次实际执行的写操作之后的数据库状态。

### 每轮结果

每轮的完整记录 `experiments/gpt_repeat/results/2026-10-20-claude/run-NN/result.json` 只保存在作者本机，不在仓库中。

| 轮次 | 结果 | 工具调用 | 输入 / 其中缓存 | 输出 / 其中推理 | 总 tokens | 已发布覆盖 |
|---|---|---:|---:|---:|---:|---:|
| 01 | 通过 | 35 | 205,481 / 179,303 | 8,801 / 2,336 | 214,282 | 0 |
| 02 | 通过 | 34 | 174,547 / 151,535 | 7,127 / 873 | 181,674 | 0 |
| 03 | 通过 | 34 | 176,243 / 155,349 | 7,170 / 296 | 183,413 | 0 |
| 04 | 通过 | 33 | 178,270 / 157,288 | 6,947 / 671 | 185,217 | 0 |
| 05 | 通过 | 34 | 177,673 / 156,315 | 7,675 / 998 | 185,348 | 0 |
| 06 | 通过 | 34 | 178,814 / 157,304 | 7,412 / 857 | 186,226 | 0 |
| 07 | 通过 | 34 | 177,654 / 156,494 | 7,280 / 823 | 184,934 | 0 |
| 08 | 通过 | 35 | 186,536 / 163,858 | 8,003 / 1,241 | 194,539 | 0 |
| 09 | 通过 | 34 | 180,477 / 158,792 | 7,778 / 1,249 | 188,255 | 0 |
| 10 | 通过 | 34 | 180,132 / 158,284 | 7,793 / 1,173 | 187,925 | 0 |

合计 **341 次工具调用**；两个 Agent 均有用量记录的轮数为 **10/10**。共 **1,891,813 tokens**，其中输入 1,815,827、输出 75,986。缓存读取 1,594,522 与缓存写入 220,883 是输入的子项，推理（thinking）10,517 是输出的子项，均不再相加。表中“其中缓存”指缓存读取。

用量取每个 Agent 进程最后一次 result 事件中的累计 `modelUsage`，再对两个 Agent 求和。stream 中 assistant 事件附带的 usage 是生成过程中的快照，不计入。Claude 与 GPT 的分词器和系统提示都不同：Claude 每个 Agent 的首次请求为 2,926–2,959 tokens，Codex 约为 10k tokens，所以两组的 token 数不宜直接比较。Claude Code 按标价估算全部十轮约 3.61 美元；这是订阅登录下的估算值，不是账单。计时包含本机工具与旁路核验，不用于模型速度排名。

### 行为与运行情况

十轮中，库存 Agent 第一次合入时都遇到 stale_head / 40001。之后的处理分成两种：

- 01、02、03、05、08 轮：按提示用当前 main 再试一次原提案，得到 merge_conflict / P0001。
- 04、06、07、09、10 轮：先对比当前 main 与原提案，看出原提案会把价格改回 10，因此没有再试，直接从当前 main 新建分支，重新提交库存修改。

两种路径都符合提示词，十轮最终都在新分支上重新表达了库存意图。这个顺序由提示词安排，不能当作 Agent 在没有提示时自行发现恢复策略的证据。

每轮工具调用 33–35 次，均值 34.1 次，中位数 34 次。按动作分：info 9–10 次、read-code 9 次、diff 4–5 次、fork 3 次、propose 3 次、merge 3–4 次、verify 1 次。每轮耗时 143–222 秒。

本组没有运行错误：没有模型错误、超时、工具预算耗尽或阶段闸门拦截。78 次限额事件均为 allowed；Pro 订阅 5 小时窗口的使用率从 0% 升到 29%。该账户不允许超额计费。

### 与 GPT 组的条件差异

- 运行器：用 Claude Code headless 代替 Codex app-server。同一个 agentdb 工具（名称、说明、参数 schema 都相同）通过 MCP stdio 服务器提供，模型看到的工具名是 `mcp__agentdb__agentdb`。内置工具全部关闭（`--tools ""`），只允许这一个 MCP 工具（`--allowedTools`、`--permission-mode dontAsk`、`--strict-mcp-config`）。每个 Agent 是一个独立进程，不保存会话（`--no-session-persistence`）。配置目录只含登录信息，工作目录为空，且不在任何 git 仓库内。
- 提示词：三段提示词和工具定义与 GPT 组逐字节相同，登记时已对照 GPT 组的 protocol.json 校验。附加的实验说明用 `--append-system-prompt` 加在 Claude Code 默认系统提示之后，相对 GPT 组只把“real GPT agent”改为“real Claude agent”。
- 引擎代码：GPT 组之后，agent_lab.py、db.py、review_local.py 与 sql/001 有改动，并新增了 sql/004–006，内容包括合入等锁上限 10 秒、批量合入查询、有界历史和 psql 会话复用。工具动作和错误返回格式未变。GPT 组当时的 agent_lab.py、review_local.py 与 sql/001–003 和提交 276d6b1 一致；当时的 db.py 不在 git 中，无法逐字节复现。
- 用量口径：Claude 单独报告 thinking tokens，列为“其中推理”；缓存写入单独记录，并计入输入。两组都只取每个 Agent 的最后一次累计值。

### 原始证据与复查

以下文件位于 `experiments/gpt_repeat/results/2026-10-20-claude/`，只保存在作者本机，不在仓库中：

- 预登记协议，含提示词、命令行、源文件与运行器哈希：`protocol.json`
- 机器可读汇总：`summary.json`
- 原始记录一致性检查：`log-audit.json`
- 文件 SHA-256 清单：`SHA256SUMS.json`

每轮目录都包含以下文件。`AGENT` 为 gpt-a 或 gpt-b，沿用 agent_lab.py 的角色名。

| 文件 | 内容 |
|---|---|
| `raw/AGENT.stdout.jsonl` | Claude Code 标准输出的原始 stream-json 字节，先保存再解析 |
| `raw/AGENT.stdin.jsonl` | 实际发给 Claude Code 的提示与控制消息 |
| `raw/AGENT.mcp_in.jsonl`、`raw/AGENT.mcp_out.jsonl` | MCP 服务器收到和发出的原始 JSON-RPC 字节 |
| `raw/tool_calls.AGENT.jsonl` | 原样抽出的 tools/call 请求与响应 |
| `raw/AGENT.timing.jsonl` | 单独记录接收时间，不改写原始报文 |
| `raw/AGENT.stderr.log` | Claude Code 的错误输出 |
| `database_audit.jsonl` | 每次写操作后的独立数据库状态 |
| `result.json` | 可重新计算的汇总、模型标识、用量与结果 |

离线审计重新计算了覆盖事件和分支修改，并核对了以下内容，全部通过：

- 95 次写操作后的数据库快照；
- 工具原文逐行出自 MCP 原始流；
- 每次 MCP 调用都对应 stream 中同一个 tool_use id；
- 用量和模型标识。

正式登记前的接口预检另存在 `experiments/gpt_repeat/results/2026-10-20-claude-preflight/`，不计入这十轮：

- pilot-1：MCP 配置路径错误，未启动模型。
- pilot-2：用不连数据库的桩工具跑了两轮短对话，确认模型 ID、工具集合和累计用量口径。它的工作目录位于仓库内，首次请求为 22,228 tokens。

正式十轮改用仓库外的空目录。此前还用脚本代替模型，在真实 WSL PostgreSQL 上完成过两次十轮演练，两次都通过离线审计；这些演练没有调用模型，结果只在临时目录。

在项目目录复查现有记录，不调用模型、不写数据库：

```console
python experiments/gpt_repeat/audit_claude_logs.py experiments/gpt_repeat/results/2026-10-20-claude
```

重新进行一组实验需要满足以下条件，并且会使用该登录的 Claude 用量额度，创建新的专用测试库：

- 新的输出目录；
- 一个只含登录信息的 Claude Code 配置目录；
- 一个仓库外的空工作目录。

```console
.\.venv-cloud\Scripts\python.exe experiments/gpt_repeat/run_claude_repeats.py --project . --claude PATH\TO\claude.exe --config-dir PATH\TO\CONFIG --workdir PATH\TO\EMPTY_DIR --output experiments/gpt_repeat/results/NEW_RUN --gpt-protocol experiments/gpt_repeat/results/2026-10-17/protocol.json --runs 10 --workers 2 --model claude-opus-5-5 --effort xhigh
```

2026-10-24 起，`run_gpt_repeats.TOOL` 的错误列表多列了 `agent_lab.py` 对 fork 和提案的拒绝，与 2026-10-17 的 `protocol.json` 记录的工具说明不再相同；按上面的命令运行时，`run_claude_repeats.py` 会在调用模型之前以 “Prompts or tool differ from the GPT run's protocol.json” 退出。2026-10-20 的 Claude 组用的是旧的说明。

**云存储仍未经真实验证，云端部署暂缓。**

---

<a id="english-version"></a>

# Ten-Round Real GPT Experiment: Results and Raw Call Records

2026-10-17. Separate test databases on the existing WSL PostgreSQL; Claude was not run, and no real cloud storage was accessed.

On 2026-10-20 the same protocol was repeated for ten rounds with Claude; those results are in [their own section at the end](#claude-run-2026-10-20-en). From here to "Run errors in this group" is the 2026-10-17 GPT record, unchanged.

This group pre-registered **10 rounds**, recorded **10 rounds**, with **8/10 (80%)** successful, and **8** rounds in which all model phases completed.
The success rate here uses all pre-registered rounds as the denominator, including model service interruptions. The corresponding 95% Wilson interval is **49.0%–94.3%**, given only as a small-sample description under a binomial model; the shared account and service failures may be correlated, so it cannot be used to estimate the model's intrinsic ability or production reliability.

The model is **`gpt-6-astra`**, as explicitly returned by the running interface, with reasoning effort **`xhigh`**; the runner is `codex-cli 0.163.0-alpha.10.2`. Each Agent's return values and model reassignment events are kept in the raw logs and results. This is the exact model ID reported by the interface; the provider does not expose the underlying weight snapshot. The original single-round experiment did not record the exact model, and is not counted in this sample.

Observed overwrites of published fields/code: **0/10 rounds**; modifications of the other Agent's branch: **0/10 rounds**. The check covers the database state after every write operation actually executed, including failed requests; even if later repaired, an earlier overwrite still counts as a failure. Rounds stopped midway only show that no overwrite happened in the part that was executed; they do not prove that no overwrite would have happened had they continued.

## Results per round

Each round's full record, `experiments/gpt_repeat/results/2026-10-17/run-NN/result.json`, is kept on the author's machine only and is not in the repository.

| Round | Result | Tool calls | Input / of which cached | Output / of which reasoning | Total tokens | Published overwrites |
|---|---|---:|---:|---:|---:|---:|
| 01 | Passed | 32 | 316,723 / 263,680 | 2,564 / 236 | 319,287 | 0 |
| 02 | Passed | 31 | 315,721 / 279,808 | 2,692 / 176 | 318,413 | 0 |
| 03 | Run error | 7 | 56,137 / 43,136 | 854 / 318 | 56,991 (partial) | 0 |
| 04 | Run error | 1 | 10,257 / 0 | 81 / 0 | 10,338 (partial) | 0 |
| 05 | Passed | 32 | 307,406 / 268,544 | 2,704 / 210 | 310,110 | 0 |
| 06 | Passed | 31 | 303,866 / 276,480 | 2,649 / 200 | 306,515 | 0 |
| 07 | Passed | 33 | 323,645 / 273,920 | 2,560 / 67 | 326,205 | 0 |
| 08 | Passed | 31 | 318,497 / 290,944 | 2,718 / 280 | 321,215 | 0 |
| 09 | Passed | 32 | 319,639 / 283,008 | 2,891 / 241 | 322,530 | 0 |
| 10 | Passed | 31 | 316,749 / 267,904 | 2,766 / 226 | 319,515 | 0 |

In total **261 tool calls**; rounds in which both Agents have usage records: **8/10**. Also counting the single-Agent usage already returned in the interrupted rounds, **2,611,119 tokens** were observed, of which input 2,588,640 and output 22,479; cached input 2,247,424 and reasoning output 1,954 are sub-items of these respectively and are not added again.

"Partial" means that round obtained the cumulative usage of only some threads; see the raw notifications and per-Agent records for details; usage that was not returned remains unknown. The original totals in summary.json include only rounds where both threads have data, while behavior-analysis.json separately totals all reported usage; the two use different counting bases.

Usage comes from each thread's last cumulative notification, then summed over threads, without repeatedly summing the per-event cumulative values. It includes context re-sent across multiple requests and cannot be interpreted as having read an equal amount of independent database content. Missing values are not estimated from character counts, and these counts are not treated as a billing reconciliation; usage from requests that disconnected or that the server did not return may not be obtainable on the client. Startup/reconnection errors are likewise kept in the raw streams. Timing includes local tools, side-channel verification and network reconnections, and is not used to rank model speed.

## Behavioral differences and run failures

There were 8 rounds that actually completed all three phases, and all 8 achieved the goal; the incomplete rounds stay in the denominator of the main success rate. Among the completed rounds, all 8 encountered stale_head / 40001 and then merge_conflict / P0001 in order. This order was explicitly arranged by the prompts and cannot be taken as evidence that the Agents discovered the recovery strategy on their own without prompting.

Completed rounds made 31–33 tool calls each, mean 31.6, median 31.5. Per-action and per-phase counts are in behavior-analysis.json; they can distinguish extra verification from retry costs, and model behavior should not be summarized by the single number of final success alone.

In this group the differences appeared mainly in reading and verification: info 5–7 times per round, read-code 9–10 times, diff 5–6 times. The write steps of the eight completed rounds were the same: 3 branch creations, 3 joint proposals, 4 merge requests, of which 2 merges were rejected by the expected conflict checks. Consistent results do not mean the call paths or resource consumption were the same.

Rounds that received usageLimitExceeded: 3, 4; rounds with other database tool errors: none. Quota errors are returned by the running service and cannot be directly attributed to the database or to model reasoning.

After rounds 3 and 4 were interrupted, a separate read-only quota query returned ordinaryUsageAllowed=true and showed that the general Codex window still had remaining quota; later rounds continued to get real tool calls. This is inconsistent with the quota errors of the failed turns; the specific cause has not been confirmed, and it cannot be stated that the whole account's quota was definitely exhausted. The raw messages of the query are saved separately in diagnostics/quota-status; login, model and account settings were not changed.

## Experimental conditions and criteria

Each round has two brand-new GPT contexts and a separate 4,000-row test database. The stock Agent first prepares a proposal for stock 100→95 from the common baseline; the price Agent then prepares and merges a proposal for price 10→11 from the same baseline; finally the stock Agent completes its merge based on the error and the current state. Each task also modifies the corresponding Python policy file. At most two rounds run in parallel, and operations within one database proceed in phases.

The three-part prompts, tool definitions, model and effort were kept the same across the ten rounds, all written in advance into protocol.json; there was no manual rescue based on any round's results and no replacement of failed samples. The prompts explicitly arrange to first check the conflict with the old expected, and hint to keep other work and re-propose when necessary, so this is still a controlled experiment with recovery hints. Compared with the original single round, this time the same agent_lab.py capabilities are wrapped as structured tools, read-code is available from the start, and no quota interruption was deliberately injected.

The two Agents have the same database capabilities: both can request to modify price or stock; the recorder does not force corrections of wrong fields. Success requires both Agents to complete their own merges, a final price of 11 and stock of 95, both code constants correct, the other 3,999 rows and the baseline unchanged, no unfinished transactions, and that throughout the process neither overwrote the other's published content or modified the other's branch. Code is only checked for syntax and constants, not executed.

The overwrite detector first passed a counter-example test of "overwrite and then restore". The independent SQL verification after each write request is saved in database_audit.jsonl; an offline audit recomputed the overwrite events, checked the snapshots of all 82 actual write requests, and confirmed that the tool texts and counts are consistent. This observation scope covers commits made through the experiment tools, and does not claim to verify arbitrary SQL, DDL, external programs or cloud storage.

## Raw evidence and re-analysis

The following files are in `experiments/gpt_repeat/results/2026-10-17/` and are kept on the author's machine only, not in the repository:

- Pre-registered protocol, prompts, source file hashes: `protocol.json`
- Machine-readable summary: `summary.json`
- Per-action differences and partial usage analysis: `behavior-analysis.json`
- Raw record consistency check: `log-audit.json`
- File SHA-256 manifest: `SHA256SUMS.json`

Each round's directory contains the following files:

| File | Content |
|---|---|
| raw/server.jsonl | Raw JSONL bytes of the app-server's standard output, saved first and parsed afterwards |
| raw/client.jsonl | Bytes of the requests and tool responses actually sent to the app-server |
| raw/tool_calls.jsonl | Tool requests and responses extracted verbatim, linked by callId / RPC id |
| raw/transport_timing.jsonl | Receive times recorded separately, without rewriting the raw messages |
| raw/server.stderr.log | Runner errors and reconnection records |
| database_audit.jsonl | Independent database state after each write operation |
| result.json | Recomputable summary, model identifier, usage and results |

Call arguments and returned content in the JSONL have not been replaced by summaries or truncated; an offline check confirmed that the tool texts come line by line from the server/client raw streams. The preceding interface probes and one echo pre-check are saved separately in diagnostics/preflight and diagnostics/instrumentation-pilot, and are not counted in these ten rounds' call counts and usage. The locally generated protocol schema is saved separately as protocol-schema.zip. The raw files are kept as local experimental material, and .gitignore prevents accidentally committing account run metadata; publishing the source code does not mean publishing these logs.

To re-check the existing records from the project directory, without calling models or writing to the database:

```console
python experiments/gpt_repeat/audit_repeat_logs.py experiments/gpt_repeat/results/2026-10-17
```

Running a new group of experiments needs a new output directory, uses the model quota of the existing Codex login, and creates new dedicated test databases:

```console
.\.venv-cloud\Scripts\python.exe experiments/gpt_repeat/run_gpt_repeats.py --project . --output experiments/gpt_repeat/results/NEW_RUN --runs 10 --workers 2 --model gpt-6-astra --effort xhigh
```

The recording entry point is based on the [OpenAI App Server documentation](https://learn.chatgpt.com/docs/app-server) and the schema generated from the local version. **Cloud storage is still not verified against real cloud services, and cloud deployment is on hold.**

## Run errors in this group

- Round 3: `RuntimeError`; for the full error text see `experiments/gpt_repeat/results/2026-10-17/run-03/result.json` and that round's raw logs (both kept on the author's machine only).
- Round 4: `RuntimeError`; for the full error text see `experiments/gpt_repeat/results/2026-10-17/run-04/result.json` and that round's raw logs (both kept on the author's machine only).

<a id="claude-run-2026-10-20-en"></a>

## 2026-10-20: the same protocol repeated for ten rounds with Claude

Registered and started at 21:41 (UTC+8) on 2026-10-20. The test databases were again on the existing WSL PostgreSQL 16.15, with a new one for each round; no real cloud storage was accessed. The GPT record above is unchanged.

This group pre-registered **10 rounds**, recorded **10 rounds**, with **10/10 (100%)** successful, and **10** rounds in which all three model phases completed. The 95% Wilson interval is **72.2%–100%**. This is still only a small-sample description under a binomial model, and it cannot be used to estimate the model's intrinsic ability or production reliability. The two groups also differ in runner, prompt wrapper and engine code (see below), so 8/10 against 10/10 should not be read as a direct comparison of the models.

The model is **`claude-opus-5-5`**, as explicitly returned by the running interface. Each Agent's init event, and `message.model` in every API response, report this ID; no other model appeared. Reasoning effort was requested with `--effort xhigh`; Claude Code's init event does not echo the effort level, so only the requested value is recorded. The runner is Claude Code `2.1.299`, run headless (`claude -p`, stream-json input and output). The login is a claude.ai Pro subscription; no API key was used. The provider does not expose the underlying weight snapshot.

Observed overwrites of published fields/code: **0/10 rounds**; modifications of the other Agent's branch: **0/10 rounds**. The check covers the database state after all 95 write operations actually executed.

### Results per round

Each round's full record, `experiments/gpt_repeat/results/2026-10-20-claude/run-NN/result.json`, is kept on the author's machine only and is not in the repository.

| Round | Result | Tool calls | Input / of which cached | Output / of which reasoning | Total tokens | Published overwrites |
|---|---|---:|---:|---:|---:|---:|
| 01 | Passed | 35 | 205,481 / 179,303 | 8,801 / 2,336 | 214,282 | 0 |
| 02 | Passed | 34 | 174,547 / 151,535 | 7,127 / 873 | 181,674 | 0 |
| 03 | Passed | 34 | 176,243 / 155,349 | 7,170 / 296 | 183,413 | 0 |
| 04 | Passed | 33 | 178,270 / 157,288 | 6,947 / 671 | 185,217 | 0 |
| 05 | Passed | 34 | 177,673 / 156,315 | 7,675 / 998 | 185,348 | 0 |
| 06 | Passed | 34 | 178,814 / 157,304 | 7,412 / 857 | 186,226 | 0 |
| 07 | Passed | 34 | 177,654 / 156,494 | 7,280 / 823 | 184,934 | 0 |
| 08 | Passed | 35 | 186,536 / 163,858 | 8,003 / 1,241 | 194,539 | 0 |
| 09 | Passed | 34 | 180,477 / 158,792 | 7,778 / 1,249 | 188,255 | 0 |
| 10 | Passed | 34 | 180,132 / 158,284 | 7,793 / 1,173 | 187,925 | 0 |

In total **341 tool calls**; rounds in which both Agents have usage records: **10/10**. **1,891,813 tokens** in all, of which input 1,815,827 and output 75,986. Cache reads (1,594,522) and cache writes (220,883) are sub-items of input, and reasoning (thinking, 10,517) is a sub-item of output; none of them is added again. "Of which cached" in the table means cache reads.

Usage is the cumulative `modelUsage` in each Agent process's last result event, summed over the two Agents. The usage attached to assistant events in the stream is a snapshot taken while the response is still being generated, so it is not counted. Claude and GPT differ in tokenizer and system prompt: each Claude Agent's first request was 2,926–2,959 tokens, against about 10k tokens for Codex, so the two groups' token counts should not be compared directly. Claude Code's list-price estimate for all ten rounds is about $3.61; this is an estimate under a subscription login, not a bill. Timing includes local tools and side-channel verification and is not used to rank model speed.

### Behavior and run conditions

In all ten rounds, the stock Agent's first merge attempt hit stale_head / 40001. After that, the rounds split into two paths:

- Rounds 01, 02, 03, 05 and 08: as the prompt suggests, the Agent tried the original proposal once more against the current main and got merge_conflict / P0001.
- Rounds 04, 06, 07, 09 and 10: the Agent first compared the current main with its original proposal, saw that the proposal would set the price back to 10, and did not try that merge. It created a new branch from the current main and resubmitted the stock change there.

Both paths follow the prompt, and in all ten rounds the stock intent was finally re-expressed on a new branch. This order was arranged by the prompts and cannot be taken as evidence that the Agents discovered the recovery strategy on their own without prompting.

Each round made 33–35 tool calls, mean 34.1, median 34. By action: info 9–10, read-code 9, diff 4–5, fork 3, propose 3, merge 3–4, verify 1. Each round took 143–222 seconds.

This group had no run errors: no model errors, timeouts, exhausted tool budgets or phase-gate refusals. All 78 rate-limit events were "allowed"; utilization of the Pro subscription's 5-hour window rose from 0% to 29%. The account does not allow overage billing.

### Differences in conditions from the GPT group

- Runner: Claude Code headless replaces the Codex app-server. The same agentdb tool (same name, description and parameter schema) is served by an MCP stdio server, and the model sees it as `mcp__agentdb__agentdb`. All built-in tools are disabled (`--tools ""`), and only this one MCP tool is allowed (`--allowedTools`, `--permission-mode dontAsk`, `--strict-mcp-config`). Each Agent is a separate process that saves no session (`--no-session-persistence`). The config directory holds only the login, and the working directory is empty and outside any git repository.
- Prompts: the three prompts and the tool definition are byte-identical to the GPT group's, checked against the GPT group's protocol.json at registration. The experiment instructions are appended to Claude Code's default system prompt with `--append-system-prompt`; the only change from the GPT group is "real GPT agent" to "real Claude agent".
- Engine code: since the GPT group ran, agent_lab.py, db.py, review_local.py and sql/001 have changed, and sql/004–006 were added. The changes cover a 10-second cap on waiting for the merge lock, batched merge lookups, bounded history and psql session reuse. The tool actions and the error reply format are unchanged. The GPT group's agent_lab.py, review_local.py and sql/001–003 match commit 276d6b1; its db.py is not in git and cannot be reproduced byte for byte.
- Usage counting: Claude reports thinking tokens separately, and they are listed as "of which reasoning"; cache writes are recorded separately and counted in input. Both groups take only each Agent's last cumulative value.

### Raw evidence and re-checking

The following files are in `experiments/gpt_repeat/results/2026-10-20-claude/` and are kept on the author's machine only, not in the repository:

- Pre-registered protocol, with prompts, command line, and source and runner hashes: `protocol.json`
- Machine-readable summary: `summary.json`
- Raw record consistency check: `log-audit.json`
- File SHA-256 manifest: `SHA256SUMS.json`

Each round's directory contains the following files. `AGENT` is gpt-a or gpt-b, the role names agent_lab.py uses.

| File | Content |
|---|---|
| `raw/AGENT.stdout.jsonl` | Raw stream-json bytes of Claude Code's standard output, saved first and parsed afterwards |
| `raw/AGENT.stdin.jsonl` | Prompts and control messages actually sent to Claude Code |
| `raw/AGENT.mcp_in.jsonl`, `raw/AGENT.mcp_out.jsonl` | Raw JSON-RPC bytes received and sent by the MCP server |
| `raw/tool_calls.AGENT.jsonl` | tools/call requests and responses extracted verbatim |
| `raw/AGENT.timing.jsonl` | Receive times recorded separately, without rewriting the raw messages |
| `raw/AGENT.stderr.log` | Claude Code's error output |
| `database_audit.jsonl` | Independent database state after each write operation |
| `result.json` | Recomputable summary, model identifier, usage and results |

The offline audit recomputed the overwrite events and branch modifications, and checked the following; all of it passed:

- the database snapshots after all 95 write operations;
- that every tool text line comes from the raw MCP streams;
- that every MCP call matches the same tool_use id in the stream;
- the usage and model identifiers.

The interface preflight before registration is kept separately in `experiments/gpt_repeat/results/2026-10-20-claude-preflight/` and is not counted in the ten rounds:

- pilot-1: wrong MCP config path; the model was never started.
- pilot-2: two short turns against a stub tool with no database, confirming the model ID, the tool set and the cumulative usage counting. Its working directory was inside the repository, and its first request was 22,228 tokens.

The ten registered rounds used an empty directory outside the repository instead. Before that, a scripted stand-in for the model completed two ten-round rehearsals on the real WSL PostgreSQL, and both passed the offline audit. These rehearsals called no model, and their results were kept only in a temporary directory.

To re-check the existing records from the project directory, without calling models or writing to the database:

```console
python experiments/gpt_repeat/audit_claude_logs.py experiments/gpt_repeat/results/2026-10-20-claude
```

Running a new group needs the following, and it uses that login's Claude usage quota and creates new dedicated test databases:

- a new output directory;
- a Claude Code config directory that holds only the login;
- an empty working directory outside the repository.

```console
.\.venv-cloud\Scripts\python.exe experiments/gpt_repeat/run_claude_repeats.py --project . --claude PATH\TO\claude.exe --config-dir PATH\TO\CONFIG --workdir PATH\TO\EMPTY_DIR --output experiments/gpt_repeat/results/NEW_RUN --gpt-protocol experiments/gpt_repeat/results/2026-10-17/protocol.json --runs 10 --workers 2 --model claude-opus-5-5 --effort xhigh
```

Since 2026-10-24 the error list in `run_gpt_repeats.TOOL` also names `agent_lab.py`'s fork and propose refusals, so it no longer matches the tool description recorded in the 2026-10-17 `protocol.json`; run as above, `run_claude_repeats.py` exits with "Prompts or tool differ from the GPT run's protocol.json" before calling any model. The 2026-10-20 Claude group used the old description.

**Cloud storage is still not verified against real cloud services, and cloud deployment is on hold.**
