中文 | [English](#english-version)

# 十轮真实 GPT 实验：预先固定的协议

2026-10-17。运行前记录，旧单轮实验不计入十轮样本。模型采用本机既有配置 `gpt-6-astra` / `xhigh`；保存每个新 Agent 的 thread/start 原始返回与模型改派事件。该标识不等于服务商未公开的权重快照。原单轮模型未记录，不能事后补认。

每轮创建现有 WSL PostgreSQL 上的独立测试数据库，4,000 行相同商品、两个全新 GPT 上下文。先由库存 Agent 准备提案，再由价格 Agent 从共同基线准备并合入提案，最后让库存 Agent 合入并处理冲突。每轮只有一个 Agent 正在操作该库，最多并行两轮。这个控制顺序重现旧实验的合入条件，不是随机并发压力测试。

三段提示词及工具 schema 写入 protocol.json，十轮不变。协调器允许准备阶段写分支，阻止提前合入；其余阶段不自动修复 Agent 决策。工具通过现有 agent_lab.py 接入，同一角色可访问 price、stock 两类操作，不强制将 Agent 限制于其正确字段。读代码接口从一开始提供；此前人工新增接口的过程不重复注入。完整系统提示使用 Codex app-server 默认指令加保存的实验 developerInstructions；不声称是与旧聊天完全相同的实验环境。

每轮每个 Agent 最多 40 次数据库工具调用，每个阶段最多 900 秒。所有已注册轮次保留，包括工具错误、模型错误、超时和未完成；不通过替换失败轮次提高成功率。模型不可用或额度中断属于运行失败，单独列明，不能解释成模型推理错误。不会切换模型、购买额度或访问云存储。

成功要求：三段模型 turn 均完成；两个 Agent 各自合入自己的任务；最终商品价格 11、库存 95，两份政策常量正确；其他 3,999 行、固定基线保持原值；没有遗留工作事务；全过程没有覆盖对方已发布的字段或代码，也没有修改对方分支。每次写操作返回后直接核查数据库，包括失败请求，避免漏掉覆盖后又恢复的情况。检测器自身包含“先覆盖、后恢复”反例测试。政策仅核验语法和常量，不执行生成代码。

raw/server.jsonl 保存 app-server 标准输出的原始行，raw/client.jsonl 保存实际发送的 JSONL 字节；raw/tool_calls.jsonl 原样保留 item/tool/call 请求和发回模型的响应。未经总结或重写，无输出截断。transport_timing.jsonl 单独提供接收时刻，不修改原始报文。数据库旁路核验记录另存 database_audit.jsonl。原始模型记录可能含模型内部的事件类型，报告只分析工具、消息、用量与结果，不将推理文字当作事实证明。

每轮报告工具请求数、回应数、每个 Agent 的累计 input/cached-input/output/reasoning-output/total tokens 和精确返回模型标识。累计用量只取每个 thread 的最后一次通知，再对两个 thread 求和；缓存输入及推理输出作为子项单列，不重复相加。缺失用量记为 null，不用文本长度估算。主成功率以十轮计划为分母，另列完成轮数、错误类别、95% Wilson 区间及覆盖事件。

这些 JSONL 是用户明确要求保存的本地实验材料，不是正式管理任务的持久状态库。云存储与 ECS 仍未经真实验证，不创建云资源或变更现有业务库。

接口依据：[OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server)；实际字段以本机 `codex-cli 0.163.0-alpha.10.2` 生成的协议 schema 和运行返回为准。

---

<a id="english-version"></a>

# Ten-Round Real GPT Experiment: Protocol Fixed in Advance

2026-10-17. Recorded before running; the old single-round experiment is not counted in the ten-round sample. The model uses this machine's existing configuration `gpt-6-astra` / `xhigh`; the raw thread/start return and model reassignment events of each new Agent are saved. This identifier is not the same as the weight snapshot the provider has not disclosed. The model of the original single round was not recorded and cannot be retroactively identified.

Each round creates a separate test database on the existing WSL PostgreSQL, with the same 4,000 product rows and two brand-new GPT contexts. First the stock Agent prepares a proposal, then the price Agent prepares and merges a proposal from the common baseline, and finally the stock Agent merges and handles the conflict. In each round only one Agent operates on that database at a time, and at most two rounds run in parallel. This controlled order reproduces the merge conditions of the old experiment; it is not a random concurrency stress test.

The three-part prompts and the tool schema are written into protocol.json and do not change across the ten rounds. The coordinator allows writing branches in the preparation phase and blocks premature merges; in the other phases it does not automatically fix Agent decisions. Tools are connected through the existing agent_lab.py; the same role can access both kinds of operations, price and stock, and Agents are not forcibly restricted to their correct field. The code-reading interface is provided from the start; the earlier process of manually adding that interface is not re-injected. The full system prompt uses the Codex app-server's default instructions plus the saved experiment developerInstructions; it is not claimed to be exactly the same experimental environment as the old chat.

In each round each Agent may make at most 40 database tool calls, and each phase lasts at most 900 seconds. All registered rounds are kept, including tool errors, model errors, timeouts and incomplete rounds; the success rate is not raised by replacing failed rounds. Model unavailability or quota interruptions are run failures, listed separately, and cannot be interpreted as model reasoning errors. Models are not switched, quota is not purchased, and cloud storage is not accessed.

Success requires: all three model turns complete; both Agents merge their own tasks; the final product price is 11 and stock 95, with both policy constants correct; the other 3,999 rows and the pinned baseline keep their original values; no leftover work transactions; and throughout the process neither overwrote the other's published fields or code, nor modified the other's branch. After each write operation returns, the database is checked directly, including failed requests, to avoid missing cases of overwrite followed by restoration. The detector itself includes an "overwrite first, restore later" counter-example test. Policies are verified only for syntax and constants; generated code is not executed.

raw/server.jsonl saves the raw lines of the app-server's standard output, and raw/client.jsonl the JSONL bytes actually sent; raw/tool_calls.jsonl keeps the item/tool/call requests and the responses sent back to the model verbatim. Nothing is summarized or rewritten, and output is not truncated. transport_timing.jsonl provides receive times separately, without modifying the raw messages. The side-channel database verification records are saved separately in database_audit.jsonl. The raw model records may contain the model's internal event types; the report analyzes only tools, messages, usage and results, and does not treat reasoning text as proof of fact.

Each round reports the number of tool requests and responses, each Agent's cumulative input/cached-input/output/reasoning-output/total tokens, and the exact returned model identifier. Cumulative usage takes only each thread's last notification, then sums over the two threads; cached input and reasoning output are listed separately as sub-items and not added again. Missing usage is recorded as null and not estimated from text length. The main success rate uses the ten planned rounds as the denominator, with the number of completed rounds, error categories, the 95% Wilson interval and overwrite events listed separately.

These JSONL files are local experimental material the user explicitly asked to save, not the persistent state store for formal management tasks. Cloud storage and ECS are still not verified against real cloud services; no cloud resources are created and the existing business database is not changed.

Interface basis: [OpenAI App Server documentation](https://learn.chatgpt.com/docs/app-server); the actual fields follow the protocol schema generated by the local `codex-cli 0.163.0-alpha.10.2` and the run returns.
