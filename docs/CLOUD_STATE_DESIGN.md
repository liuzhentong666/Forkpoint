中文 | [English](#english-version)

# 常驻 Agent 的云端状态与上下文服务

> 状态更新（2026-10-17）：本文是设计与代码说明，云服务未经真实验证，云端部署暂缓。下文存储布局和恢复流程不表示已有云端运行结果。

日期：2026-10-16。本文记录最初的 GCS 实现设计。当前已扩展为 GCS / OSS / S3 多后端，提供商选择、任务入口协议及兼容边界以 [STORAGE_BACKENDS.md](STORAGE_BACKENDS.md) 为准；本文的有界状态、幂等和跨系统恢复原则继续适用。

## 目标与边界

业务版本仍由现有 Ubuntu WSL PostgreSQL 的 Forkpoint 管理。管理 Agent 状态服务的设计是：完整操作事实、检查点和证据写入所选云对象存储，活跃状态放在运行时内存，模型按任务取得有大小上限的上下文。此云端流程尚未真实运行，设计不使用 SQLite、本地状态文件或新增 PostgreSQL ops schema。

全天在线指服务持续运行；模型上下文仍有限。存储量可以增长，但普通恢复不扫描存储桶、不回放全历史。内存缓存可以丢弃，由云端状态恢复。源码、连接配置和测试报告不属于管理任务的持久状态。

第一版实现对象存储协议、GCS 适配器、条件发布、幂等命令、内存缓存、上下文服务、HTTP 接口、通知接入和可恢复的 PostgreSQL 快照维护示例。它不自动执行任意 DDL、数据库重启或模型推理；这些执行器以后通过同一状态协议接入。superuser Agent 的数据库能力不因本服务被缩减。

## 状态和对象布局

所有键位于配置的 bucket/prefix 内。任务 ID 限定为短的字母、数字、下划线和连字符。

```text
tasks/<task_id>/head.json          唯一可变发布入口
objects/<sha256>                   不可变 JSON：状态、事件、命令索引节点
artifacts/<sha256>                 不可变原始证据
```

head 包含协议版本、任务 ID、单调递增版本号、当前状态哈希、最新事件哈希、命令索引根哈希。head 不含不断增长的历史列表。状态保存目标、状态、下一步、未决事项、结构化事实、规则对象引用及证据引用；默认 JSON 上限 32 KiB，超出时要求拆分任务或把细节存为证据。

每次成功变更生成一份完整但有界的状态检查点。因此冷恢复只需 head 和该状态两个逻辑对象读取；不需要重放增量。代价是每次发布多写一份小状态。GCS 的一次逻辑读取可能包含元数据和固定 generation 内容两个网络请求，报告不能把两者混为一谈。

每个不可变事件链接前一个事件哈希。历史接口只沿链读取请求的一页，页大小有硬上限；返回游标。它是明确请求历史时的成本，不属于常规上下文恢复。任何任务头和缓存项都不内嵌完整事件链。

## 发布、并发和幂等

客户端提交 command_id、expected_version、执行者、完整目标状态与简短事件说明。服务端验证大小、格式和证据引用，然后写入不可变对象，最后以旧 head 的 GCS generation 为条件更新 head。任务创建使用 ifGenerationMatch=0。

head 条件更新是单任务的发布点。失败的并发写者返回冲突，不自动覆盖或偷偷重放其修改。上传后未发布的对象暂为孤儿；本版不提供自动 GC，避免误删仍被引用的对象。

命令幂等不能只记最后一个 command_id。使用内容寻址的持久哈希字典保存 command_id → 请求哈希、已发布版本和事件引用。字典采用写时复制的分支节点/有界叶节点，SHA-256 十六进制路径最大深度 64。索引根与状态一起在 head 中发布。这样很久以前的重复命令仍可查证，不需要扫描全部历史，也不在单个 head 中积累命令 ID。

同 command_id、同请求重复提交，返回原结果；同 ID 不同请求拒绝。发布响应丢失后，重试先查询已发布命令索引。跨对象没有事务；先写内容、后发布引用。GCS 请求失败不能被宣称为成功。

这不是外部操作的 exactly-once 保证。修改 PostgreSQL 与发布 GCS 状态不共享事务。外部执行器必须记录意图，执行前检查真实状态，执行后核验；只有具备幂等或结果核对能力的操作才能自动恢复。本版快照维护使用固定 revision/table ID，复用 vc.materialize 的幂等性。head CAS 本身不是执行器的 fencing token。

## 常驻内存与变化通知

运行时缓存最近活跃任务，按条目数和总字节数双重限制。状态返回副本，防止调用方修改缓存。默认缓存刷新期限有限，超过后重新读取该任务头。请求可以显式要求 fresh，以便重要操作前核对已发布版本。

可选 GCS Pub/Sub pull 订阅接收对象变更通知，只处理本 prefix 下的 head 更新。重复或旧 generation 不把缓存降级；通知只使状态失效，下一次从 GCS 核对。周期性检查内存中的活跃任务覆盖漏通知情况。订阅回调不运行模型，不遍历 bucket；订阅由操作者提供，程序不创建云资源。

服务重启时不扫描所有任务；调度器给出 task_id 后恢复。全局任务发现、任务分配、跨节点内存服务不在本版范围。

## 上下文接口

上下文包含 task_id、version、goal、status、next_action、uncertainties、policy_ref、state_ref。结构化事实和证据引用按预算添加。被省略内容必须报告，完整状态可按 state_ref 获取，禁止无提示截断关键事实。

提供 UTF-8 字节硬预算。可选配置 tiktoken 编码器时，额外支持该编码器的 token 预算；不配置时拒绝声称精确 token 计数。此计数只覆盖返回包，不包括调用模型的系统提示或其他工具消息。必要字段也超预算时返回明确错误，让调用方拆分任务或调整预算。

Agent 可以按哈希读取具体证据。证据对象有上传和下载上限，数据大于本版接口上限时必须拆块；不把整个报告默认塞进上下文。规则文件同样可保存为有内容哈希的证据对象，状态固定引用其版本。规则内容是提供给管理 Agent 的工作依据；服务端不把存储内容自动当作可执行指令。

## 接口与执行流程

HTTP 服务提供创建任务、条件更新、读取状态、构建上下文、分页历史、上传/读取证据、指标和通知入口。默认只监听 loopback；非 loopback 必须配置访问令牌，外部部署还需 TLS 或受控入口。所有已接入 Agent 使用同一能力集合，没有人为划分开发者/审批者权限。

典型执行：读取固定规则 → 创建/恢复任务 → 发布意图 → 检查实际对象 → 执行幂等维护 → 验证 → 发布结果。上下文丢失只重新读取状态和必要证据，不以聊天记录恢复任务。

PostgreSQL 示例继续使用 connection.local.json 的现有本地服务。云凭据采用 GCS Application Default Credentials，不写进任务、代码或 HTTP 请求体。服务不会隐式创建 bucket、修改 IAM、启停 PostgreSQL 或把内存测试后端当作云端持久化。

## 验收与可观察性

记录逻辑对象读取次数、写入次数、传输字节、CAS 冲突、缓存命中、上下文大小和有配置时的 token 数。云端网络延迟、计费、IAM 与实际通知延迟必须真实联调后才能报告。

必测：十个写者竞争同一版本只能发布一个结果；重复请求（包括跨后续版本的旧请求）不重复执行状态变更；同命令 ID 不同请求拒绝；发布前/后故障可判定；每次发布对象完整；内容哈希校验；冷恢复读取量不随历史长度增长；缓存有界；过期/重复通知正确；上下文预算/省略提示；历史分页；PostgreSQL 固定版本快照的恢复示例。

纯内存对象存储仅供故障注入和测试，不写本地磁盘。GCS 适配器契约测试不能替代真实 bucket 联调。缺少 bucket 或凭据时记录为未验证，不报告云端部署完成。

## 依据

- [GCS 条件请求](https://docs.cloud.google.com/storage/docs/request-preconditions)
- [GCS 单对象原子性与一致性](https://docs.cloud.google.com/storage/docs/consistency)
- [GCS 通知语义](https://docs.cloud.google.com/storage/docs/pubsub-notifications)
- [Google Cloud ADC](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc)

---

<a id="english-version"></a>

# Cloud State and Context Service for Long-Running Agents

> Status update (2026-10-17): this document is a design and code description; cloud services are not verified against real cloud services, and cloud deployment is on hold. The storage layout and recovery flow below do not mean there are any cloud run results.

Date: 2026-10-16. This document records the original GCS implementation design. It has since been extended to multiple backends (GCS / OSS / S3); for provider selection, the task entry-point protocol and compatibility boundaries, [STORAGE_BACKENDS.md](STORAGE_BACKENDS.md) takes precedence; this document's principles of bounded state, idempotency and cross-system recovery continue to apply.

## Goals and boundaries

Business versions are still managed by Forkpoint on the existing Ubuntu WSL PostgreSQL. The design of the manager Agent state service is: complete operation facts, checkpoints and evidence are written to the selected cloud object storage, active state is kept in runtime memory, and the model gets a size-capped context per task. This cloud flow has not actually run; the design does not use SQLite, local state files or a new PostgreSQL ops schema.

Being online all day means the service runs continuously; the model's context is still limited. Storage volume can grow, but ordinary recovery does not scan the storage bucket or replay the full history. The in-memory cache can be discarded and recovered from cloud state. Source code, connection configuration and test reports are not part of the management task's persistent state.

The first version implements the object storage protocol, the GCS adapter, conditional publishing, idempotent commands, the in-memory cache, the context service, the HTTP interface, notification intake, and a recoverable PostgreSQL snapshot maintenance example. It does not automatically execute arbitrary DDL, database restarts or model inference; these executors will be connected later through the same state protocol. The superuser Agent's database capabilities are not reduced by this service.

## State and object layout

All keys are within the configured bucket/prefix. Task IDs are limited to short strings of letters, digits, underscores and hyphens.

```text
tasks/<task_id>/head.json          唯一可变发布入口
objects/<sha256>                   不可变 JSON：状态、事件、命令索引节点
artifacts/<sha256>                 不可变原始证据
```

(In the layout above: `tasks/<task_id>/head.json` is the only mutable publishing entry point; `objects/<sha256>` holds immutable JSON: states, events and command index nodes; `artifacts/<sha256>` holds immutable raw evidence.)

The head contains the protocol version, the task ID, a monotonically increasing version number, the current state hash, the latest event hash and the command index root hash. The head does not contain an ever-growing history list. The state holds the goal, status, next step, open items, structured facts, the rule object reference and evidence references; the default JSON limit is 32 KiB, and when it is exceeded the task must be split or the details stored as evidence.

Each successful change produces a complete but bounded state checkpoint. So cold recovery needs only two logical object reads, the head and that state; there is no need to replay deltas. The cost is writing one extra small state per publish. One logical read on GCS may involve two network requests, metadata and content at a fixed generation, and reports must not conflate the two.

Each immutable event links to the previous event's hash. The history interface reads only the requested page along the chain, the page size has a hard limit, and a cursor is returned. This is a cost incurred when history is explicitly requested, not part of routine context recovery. No task head or cache entry embeds the complete event chain.

## Publishing, concurrency and idempotency

The client submits command_id, expected_version, the executor, the complete target state and a short event description. The server validates size, format and evidence references, then writes the immutable objects, and finally updates the head conditional on the old head's GCS generation. Task creation uses ifGenerationMatch=0.

The conditional head update is the publishing point for a single task. A failed concurrent writer gets a conflict back; its modification is not automatically overwritten or silently replayed. Objects uploaded but not published are temporarily orphans; this version provides no automatic GC, to avoid mistakenly deleting objects that are still referenced.

Command idempotency cannot rely on remembering only the last command_id. A content-addressed persistent hash dictionary stores command_id → request hash, published version and event reference. The dictionary uses copy-on-write branch nodes/bounded leaf nodes, with a maximum depth of 64 over the SHA-256 hex path. The index root is published in the head together with the state. This way a duplicate command from long ago can still be checked, without scanning the whole history and without accumulating command IDs in a single head.

A repeated submission with the same command_id and the same request returns the original result; the same ID with a different request is rejected. After a publish response is lost, a retry first queries the published command index. There are no transactions across objects; content is written first and the reference published afterwards. A failed GCS request must not be declared a success.

This is not an exactly-once guarantee for external operations. Modifying PostgreSQL and publishing GCS state do not share a transaction. External executors must record the intent, check the real state before executing, and verify after executing; only operations with idempotency or result-checking capability can be recovered automatically. In this version, snapshot maintenance uses a pinned revision/table ID and reuses the idempotency of vc.materialize. The head CAS itself is not a fencing token for executors.

## Resident memory and change notifications

The runtime caches recently active tasks, limited both by number of entries and total bytes. State is returned as a copy, preventing callers from modifying the cache. The default cache refresh period is limited, and after it expires the task head is re-read. A request can explicitly ask for fresh, so that the published version is checked before important operations.

An optional GCS Pub/Sub pull subscription receives object change notifications and only handles head updates under this prefix. Duplicate or old generations do not downgrade the cache; notifications only invalidate state, and the next read checks against GCS. Periodic checks of the active tasks in memory cover missed notifications. The subscription callback does not run models and does not traverse the bucket; the subscription is provided by the operator, and the program does not create cloud resources.

On service restart, not all tasks are scanned; a task is recovered once the scheduler provides its task_id. Global task discovery, task assignment and a cross-node memory service are not within the scope of this version.

## Context interface

The context contains task_id, version, goal, status, next_action, uncertainties, policy_ref and state_ref. Structured facts and evidence references are added according to the budget. Omitted content must be reported, the complete state can be fetched by state_ref, and truncating key facts without notice is forbidden.

A hard budget in UTF-8 bytes is provided. When a tiktoken encoder is optionally configured, a token budget for that encoder is additionally supported; when it is not configured, claiming an exact token count is refused. This count covers only the returned package, not the system prompt of the calling model or other tool messages. When the required fields alone exceed the budget, an explicit error is returned so the caller can split the task or adjust the budget.

Agents can read specific evidence by hash. Evidence objects have upload and download limits, and data larger than this version's interface limit must be chunked; whole reports are not stuffed into the context by default. Rule files can likewise be saved as evidence objects with a content hash, with the state pinning a reference to their version. Rule content is a working basis provided to the manager Agent; the server does not automatically treat stored content as executable instructions.

## Interfaces and execution flow

The HTTP service provides task creation, conditional updates, state reads, context building, paginated history, evidence upload/read, metrics and a notification entry point. By default it listens only on loopback; non-loopback requires an access token to be configured, and external deployment also needs TLS or a controlled entry point. All connected Agents use the same set of capabilities, with no artificial split into developer/approver permissions.

Typical execution: read the pinned rules → create/recover the task → publish the intent → check the actual objects → perform idempotent maintenance → verify → publish the result. When context is lost, only the state and the necessary evidence are re-read; tasks are not recovered from chat history.

The PostgreSQL example continues to use the existing local service in connection.local.json. Cloud credentials use GCS Application Default Credentials and are not written into tasks, code or HTTP request bodies. The service does not implicitly create buckets, modify IAM, start/stop PostgreSQL, or treat the in-memory test backend as cloud persistence.

## Acceptance and observability

Record the number of logical object reads, writes, bytes transferred, CAS conflicts, cache hits, context size, and token counts when configured. Cloud network latency, billing, IAM and actual notification latency can only be reported after real integration testing.

Must test: ten writers competing for the same version can publish only one result; repeated requests (including old requests across later versions) do not re-execute state changes; the same command ID with a different request is rejected; failures before/after publishing can be determined; each published object is complete; content hash verification; cold recovery read volume does not grow with history length; the cache is bounded; stale/duplicate notifications are handled correctly; context budget/omission markers; history pagination; the recovery example for PostgreSQL pinned-version snapshots.

The pure in-memory object storage is only for fault injection and testing and does not write to local disk. Contract tests for the GCS adapter cannot replace integration testing against a real bucket. When the bucket or credentials are missing, record it as not verified; do not report cloud deployment as complete.

## References

- [GCS request preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions)
- [GCS single-object atomicity and consistency](https://docs.cloud.google.com/storage/docs/consistency)
- [GCS notification semantics](https://docs.cloud.google.com/storage/docs/pubsub-notifications)
- [Google Cloud ADC](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc)
