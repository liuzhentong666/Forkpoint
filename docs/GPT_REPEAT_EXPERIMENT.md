[English](GPT_REPEAT_EXPERIMENT.en.md) | 中文

# 十轮真实 GPT 实验：结果与原始调用记录

2026-10-17。独立测试库位于已有 WSL PostgreSQL；未运行 Claude，未访问真实云存储。

本次预先登记 **10 轮**，记录 **10 轮**，成功 **8/10（80%）**，模型阶段全部完成的轮数为 **8**。
这里的成功率以全部预登记轮次为分母，包括模型服务中断。对应的 95% Wilson 区间为 **49.0%–94.3%**，仅作为二项模型下的小样本描述；共享账户和服务故障可能相关，不能据此估计模型固有能力或生产可靠性。

模型为运行接口明确返回的 **`gpt-6-astra`**，推理强度 **`xhigh`**；运行器为 `codex-cli 0.163.0-alpha.10.2`。每个 Agent 的返回值及模型改派事件均在原始日志和结果中保留。这是接口报告的精确模型 ID；服务商未暴露底层权重快照。原来的单轮实验没有记录精确型号，不计入本次样本。

观察到已发布字段/代码覆盖：**0/10 轮**；修改另一 Agent 分支：**0/10 轮**。检查覆盖每次实际执行的写操作后的数据库状态，包含失败请求；即使后来修复，先前的覆盖仍记为失败。中途停止的轮次只说明已执行部分未发生覆盖，不证明它们若继续也不会覆盖。

## 每轮结果

| 轮次 | 结果 | 工具调用 | 输入 / 其中缓存 | 输出 / 其中推理 | 总 tokens | 已发布覆盖 |
|---|---|---:|---:|---:|---:|---:|
| [01](../experiments/gpt_repeat/results/2026-10-17/run-01/result.json) | 通过 | 32 | 316,723 / 263,680 | 2,564 / 236 | 319,287 | 0 |
| [02](../experiments/gpt_repeat/results/2026-10-17/run-02/result.json) | 通过 | 31 | 315,721 / 279,808 | 2,692 / 176 | 318,413 | 0 |
| [03](../experiments/gpt_repeat/results/2026-10-17/run-03/result.json) | 运行错误 | 7 | 56,137 / 43,136 | 854 / 318 | 56,991（部分） | 0 |
| [04](../experiments/gpt_repeat/results/2026-10-17/run-04/result.json) | 运行错误 | 1 | 10,257 / 0 | 81 / 0 | 10,338（部分） | 0 |
| [05](../experiments/gpt_repeat/results/2026-10-17/run-05/result.json) | 通过 | 32 | 307,406 / 268,544 | 2,704 / 210 | 310,110 | 0 |
| [06](../experiments/gpt_repeat/results/2026-10-17/run-06/result.json) | 通过 | 31 | 303,866 / 276,480 | 2,649 / 200 | 306,515 | 0 |
| [07](../experiments/gpt_repeat/results/2026-10-17/run-07/result.json) | 通过 | 33 | 323,645 / 273,920 | 2,560 / 67 | 326,205 | 0 |
| [08](../experiments/gpt_repeat/results/2026-10-17/run-08/result.json) | 通过 | 31 | 318,497 / 290,944 | 2,718 / 280 | 321,215 | 0 |
| [09](../experiments/gpt_repeat/results/2026-10-17/run-09/result.json) | 通过 | 32 | 319,639 / 283,008 | 2,891 / 241 | 322,530 | 0 |
| [10](../experiments/gpt_repeat/results/2026-10-17/run-10/result.json) | 通过 | 31 | 316,749 / 267,904 | 2,766 / 226 | 319,515 | 0 |

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

- [预登记协议、提示词、源文件哈希](../experiments/gpt_repeat/results/2026-10-17/protocol.json)
- [机器可读汇总](../experiments/gpt_repeat/results/2026-10-17/summary.json)
- [逐动作差异与部分用量分析](../experiments/gpt_repeat/results/2026-10-17/behavior-analysis.json)
- [原始记录一致性检查](../experiments/gpt_repeat/results/2026-10-17/log-audit.json)
- [文件 SHA-256 清单](../experiments/gpt_repeat/results/2026-10-17/SHA256SUMS.json)

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

- 第 3 轮：`RuntimeError`；完整错误原文见 [result.json](../experiments/gpt_repeat/results/2026-10-17/run-03/result.json) 及该轮原始日志。
- 第 4 轮：`RuntimeError`；完整错误原文见 [result.json](../experiments/gpt_repeat/results/2026-10-17/run-04/result.json) 及该轮原始日志。
