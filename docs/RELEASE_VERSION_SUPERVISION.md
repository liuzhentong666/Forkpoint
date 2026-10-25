中文 | [English](#english-version)

<a id="chinese-version"></a>

# 提交版本与运行版本监管 Agent

日期：2026-10-25；同日按设计审查修订。状态：用户已确定采用此方向；以下是待实现设计，未启动监管进程、模型任务或部署。云端状态后端仍未经真实验证。

目标：用户能知道指定环境是否正在运行预期发布；出现偏离时，由监管 Agent 根据可核验证据诊断，并在既有授权和策略范围内调用现有发布工具修复，最后验证实际结果。它属于数据库与发布维护，通用编程任务编排保持现状；监管范围本身待定（D16）。入口文档是[低成本接入](LOW_COST_ADOPTION_PLAN.md#chinese-version)（决策 D1–D9、门槛 G0–G4），配套见[结构迁移](SCHEMA_EVOLUTION_PLAN.md#chinese-version)。

## 1. 监管对象和保证

比较链条为：已提交源码 → 可追溯构建产物 → 指定环境的目标发布 → 实际实例 → 数据库结构/必要数据兼容性 → 健康与功能验证。

最新提交可以尚未发布。每个环境有独立、明确设置的目标发布；监管器不因仓库 head 前进就自动部署。版本标签只是显示名称，应以不可变源码引用和构建产物摘要判定身份。同一源码可能因依赖、配置或构建过程不同得到不同产物；一个看似相同的版本号不是充分证据。

这条链与现有工具大量重叠：部署平台已经跟踪目标版本与运行版本及升级期限，下文 R1 验收样例中与期限有关的部分，Kubernetes Deployment 的 `progressDeadlineSeconds` 已经会报告。只有 Forkpoint 掌握的是结构兼容与带版本的参考数据两环。监管器自建链条中的哪些部分由 D16 决定。

提供检测、解释、受控修复及事后核验，不能仅靠增加一个 Agent 就保证分布式应用原子切换，也不能由版本一致推出功能正确。

## 2. 需要记录的事实

以下字段是拟议契约，尚无对应现成 API。

| 记录 | 必要字段 |
|---|---|
| 不可变发布清单 | `release_id`、`source_ref`（Git SHA 或 Forkpoint revision，权威来源按 D7）、`artifact_digest`、构建记录引用、依赖/工具链引用、`config_ref`、所需结构/迁移集合（来源按 D15）、必要的固定数据引用、验证证据、恢复方案引用。配置只存安全引用/指纹，不含密钥。 |
| 环境目标 | `environment_id`、`desired_release_id`、`desired_generation`、部署对象范围、允许的旧/新版本集合、升级期限、健康和覆盖标准、策略引用。目标变化递增 generation。 |
| 运行观察 | 实例身份、实际产物摘要或可验证构建身份、进程启动时间、实际配置引用、流量/角色信息、观察来源和时间、失效时间、实际数据库身份与结构、健康结果。 |
| 执行动作 | 命令 ID、目标 generation、前提、执行器操作 ID、必要的 fencing 标记、开始/完成证据、核验方法、结果未知的原因及下一步。 |

源码权威只选一种（D7），另一种仅作对应引用，不建立两个独立"最新版本"。普通订单等动态数据持续变化，不要求当前数据库 head 永远等于发布时 revision；比较的是结构兼容和发布确实依赖的固定数据。若确实要求固定数据，必须注明范围、版本和更新政策。

在零代码改动下，应用所需结构唯一的自动来源是构建时迁移工具的版本表；它记录已执行了哪些迁移，而不是代码实际读取什么（D15）。

## 3. 取证和状态判定

优先复用容器/进程管理器、构建系统、现有版本端点和数据库目录。版本端点没有可靠构建关联时，仅是自报信息；证据不足记为未知。部署期望字段不能替代实例实际状态。容器场景以摘要识别产物，标签可能变化。[镜像身份依据](https://kubernetes.io/docs/concepts/containers/images/)

| 状态 | 条件 | 动作 |
|---|---|---|
| `unknown` | 实例不可达、观察超时、缺少构建对应、部署清单不完整 | 查证；不得报告一致，不盲目重启或重发动作。 |
| `converging` | 升级期限内，运行版本在允许集合内，覆盖/健康条件符合策略 | 跟踪进度，不把正常新旧共存报为失败。 |
| `aligned` | 最新目标 generation；全部要求覆盖的运行单元和产物、配置均已核验，结构兼容核验到 D15 所能提供的程度；健康/冒烟达到标准 | 记录带时间和范围的成功证据；观察过期后重新检查。若只有目录证据，此状态只表示"所需迁移已执行"，并按此措辞报告。 |
| `drifted` | 出现未允许版本、配置/结构偏离，或期限到仍未达到目标 | 生成差异和修复意图，在策略允许时执行。 |
| `unhealthy` | 版本可一致，但健康或功能检查失败 | 进入故障诊断；不把版本一致作为成功。 |
| `blocked` | 修复缺少执行能力、可靠结果探测、兼容回退或所需权限 | 明确阻碍和最小下一步，保留现场证据。 |

默认部署清单覆盖所有应服务的实例；采样只提供采样结论。范围应纳入后台 worker、定时任务及有独立生命周期的服务。前端/CDN/客户端缓存无法全面观察时单独标出覆盖缺口。使用既有滚动升级机制和状态，不开发新的容器调度器。[滚动发布参考](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)

## 4. Agent 与确定性执行器的分工

探针和状态比较用确定性代码；只在有意义的偏离、故障或策略需要解释时调用 Agent。Agent 消费短事实包和必要证据，提出诊断及动作；执行器校验目标、权限、前提和幂等信息。正常稳定状态不逐轮调用模型，不重复通知。部署事件触发检查，辅以有上限的周期核验、退避、抖动和回源限流；频率由用户环境的恢复目标确定。

沿用[管理 Agent 协议](MANAGER_AGENT_PROTOCOL.md)。已有授权范围内自动执行，不增加每次操作的人类审批角色。权限不足或需要改变既定策略时才报告所需动作。可维护的典型动作包括：重试已确认可重试的发布、替换已确认偏离的实例、暂停失败升级、恢复已验证兼容的旧产物。禁止仅因版本号不一致而盲目回滚数据库。

状态服务遵循现有有界检查点、证据引用和幂等命令协议；正式持久状态仍采用已设计的云端对象存储，当前暂缓。不得另建 PostgreSQL `ops` schema、本地文件或 SQLite 作为唯一正式任务状态。本地阶段用 MemoryStore 测试协议；它不能证明监管进程退出后的恢复，而 R2 的验收需要这一点，因此在 C09 之前 R2 如何进行由 D17 决定。数据库迁移凭据是数据库事实，与任务检查点分开。

## 5. 修复与中断窗口

顺序：fresh 读取目标 → 收集实际状态 → 持久记录意图与核验方式 → 执行器再次检查 generation/前提 → 执行 → 独立核验 → 发布结果。

| 情形 | 恢复规则 |
|---|---|
| 已记录意图，尚未执行 | 查询外部操作 ID/真实状态，确认未执行后按同一命令恢复。 |
| 动作可能完成，回复丢失 | 先查询发布平台和实例，不以超时等同失败；无法判定则保持未知。 |
| 实例已切换，状态发布失败 | 现场核验后补记结果，不再部署一次。 |
| 目标切换或两个监管者竞争 | 执行器支持排他协调和 generation/fencing 校验；旧命令拒绝或取消，已发生的动作重新对账。任务 CAS 不等于外部 fencing。 |
| 新代码要求尚未完成的结构迁移 | 按兼容清单等待或停止流量切换，不能仅修正代码版本号。 |
| 代码需要回滚但数据已改变 | 验证旧代码对当前结构/数据的兼容；否则前向修复。不得恢复旧数据库快照而无视之后业务写入。 |

如果发布平台不支持 fencing，必须给出可验证的串行控制与取消边界；做不到就只检测或限制自动动作，不能承诺互斥执行。该设计提供可恢复的协调流程，不声称跨数据库、对象存储和部署平台 exactly-once。

## 6. 待用户决定的事项

格式与 D1–D9 相同（见[低成本接入](LOW_COST_ADOPTION_PLAN.md#chinese-version)第 4 节）。

| ID | 决策 | 选项及后果 | 阻塞 | 决定前需要 |
|---|---|---|---|---|
| D15 | 应用所需结构的来源 | (a) 每个迁移由作者声明（处于扩展还是收缩阶段、支持哪些发布）：可靠，但每个迁移都增加人工。(b) 在验证环境（S3、D14）中对候选结构运行应用自己的测试。(c) 两者都用。(d) 只看目录：只能说明"迁移已执行"，不能说明兼容。 | R1 的 `aligned`、结构计划的首个验收样例 | D1、D14 |
| D16 | 监管范围 | (a) 第 1 节的完整链条，每种部署平台各写适配器。(b) 只做兼容门：目标/运行状态取自平台（例如 Kubernetes Deployment 状态条件、Argo CD、Flux），在其上增加结构与参考数据检查。(c) 先做 (b)，再按证据逐个平台扩展。 | R0–R3 | D1（部署平台）、D8 |
| D17 | 云端工作暂缓期间的监管状态 | (a) 等待 C09：R2 自动修复和 A2 维护接管均受阻。(b) 允许仅用于开发测试的本地持久后端（需要修改 AGENTS.md 中禁止以本地文件、SQLite 或 PostgreSQL `ops` schema 作为唯一正式状态的规则）。(c) 在 C09 之前只检测并提出修复建议，不自动修复。 | R2、A2 | 无 |

## 7. 交付阶段、门槛与验收

| 阶段 | 交付 | 必须通过的验收 | 需要 |
|---|---|---|---|
| R0 版本对应 | 清单格式、源码到产物绑定、环境目标、至少一种现有部署方式的只读探针 | 同标签不同产物、同源码不同构建均可区分；缺证据报告 unknown。 | D1、D7、D16、G7 |
| R1 偏离检测 | 实例清单、结构兼容检查、状态机、超时与通知去重 | 目标 v12、实际 2 个 v12/1 个 v11：升级期内为 converging，期限后 drifted，全部核验后 aligned；实例失联不能 aligned。 | D15 |
| R2 受控修复 | 既有发布工具适配、幂等查询、目标代际校验、恢复执行器 | 覆盖回复丢失、重复命令、目标被改、两个监管者、执行后检查点失败；不重复不可重试操作。 | D17、G8 |
| R3 正式运行 | 经单独安排的真实状态后端、实际进程/主机恢复、监控与运行成本 | 检测时延、恢复耗时、覆盖率、误报漏报、模型/存储开销和未决动作都有实测报告。 | C09 |

| 门槛 | 在此之前 | 检查 |
|---|---|---|
| G7 真实部署方式 | R0 视为交付 | 只读探测试点项目的一种真实部署方式（来自 D1）。本项目目前没有已部署的平台（ECS 暂缓）；只对模拟部署 API 测试时，验证的只是状态机本身。 |
| G8 自动修复 | R2 自动执行任何修复 | D17 已决定；第 5 节的中断窗口测试在状态能跨监管进程重启保留的条件下通过；否则 R2 只提出修复建议。 |

R0/R1 可先以本地服务或模拟部署 API 开发。样例中的版本号用于测试，不代表真实发布。Agent 输出只作为诊断输入，最终完成判定依赖探针和验证证据。

---

<a id="english-version"></a>

[中文](#chinese-version) | English

# Agent for Supervising Committed and Running Versions

Date: October 25, 2026; revised the same day after a design review. Status: the user has chosen this direction; the following design is not implemented. No supervisor process, model task, or deployment was started. The cloud state backend remains unverified against real services.

Goal: establish whether a selected environment runs its intended release. On drift, the supervisor agent diagnoses from verifiable evidence, invokes existing deployment tools within established authorization and policy, and verifies the outcome. This is database/release maintenance; general coding orchestration remains unchanged. The supervisor's scope itself is open (D16). The entry document is [low-cost adoption](LOW_COST_ADOPTION_PLAN.md#english-version) (decisions D1–D9, gates G0–G4); the related plan is [schema evolution](SCHEMA_EVOLUTION_PLAN.md#english-version).

## 1. Scope and guarantees

The chain is: committed source → traceable build artifact → environment's desired release → actual instances → database schema/required-data compatibility → health and functional verification.

The latest commit may not be intended for deployment. Each environment has an explicitly selected desired release; advancing repository head does not trigger deployment by itself. Version labels are display names. Identify releases by immutable source references and artifact digests. The same source may produce different artifacts because of dependencies, configuration, or build differences; an apparently matching version number is insufficient evidence.

The chain overlaps existing tools: deployment platforms already track desired versus running versions and rollout deadlines, and Kubernetes Deployment's `progressDeadlineSeconds` already reports the deadline part of the R1 acceptance case below. The links only Forkpoint knows are schema compatibility and versioned reference data. D16 decides which parts of the chain the supervisor builds itself.

The system provides detection, explanation, controlled repair, and verification. Adding an agent does not make a distributed application switch atomically, and matching versions do not prove functional correctness.

## 2. Facts to record

These fields are a proposed contract, not an existing API.

| Record | Required fields |
|---|---|
| Immutable release manifest | `release_id`, `source_ref` (Git SHA or Forkpoint revision; the authority follows D7), `artifact_digest`, build evidence, dependencies/toolchain references, `config_ref`, required schemas/migrations (source per D15), necessary fixed-data references, validation evidence, and recovery-plan reference. Store safe configuration references/fingerprints, not secrets. |
| Environment target | `environment_id`, `desired_release_id`, `desired_generation`, deployment scope, allowed old/new release set, rollout deadline, health/coverage criteria, and policy reference. Increment generation when the target changes. |
| Runtime observation | Instance identity, actual artifact digest or verifiable build identity, process start time, actual configuration reference, traffic/role, observation source/time/expiry, actual database identity/schema, and health results. |
| Execution action | Command ID, target generation, preconditions, executor operation ID, required fencing token, start/completion evidence, verification method, reasons an outcome is unknown, and next action. |

Choose one authority for source code (D7); the other version system supplies a mapping, not an independent latest version. Dynamic business data such as orders keeps changing: database head need not remain at the release's revision. Compare schema compatibility and the fixed data the release actually requires. Fixed-data requirements must specify scope, version, and update policy.

Under zero code changes, the only automatic source of an application's schema requirements is the migration tool's version table at build time, which records which migrations were applied, not what the code actually reads (D15).

## 3. Evidence and state classification

Reuse container/process managers, build systems, existing version endpoints, and database catalogs. A version endpoint without a reliable build association is self-reported information only; insufficient evidence means unknown. Desired deployment fields cannot substitute for observed runtime state. Container artifacts should be identified by digest because tags can change. [Image identity reference](https://kubernetes.io/docs/concepts/containers/images/)

| State | Condition | Action |
|---|---|---|
| `unknown` | Unreachable instance, expired observation, missing build mapping, or incomplete deployment inventory | Investigate; do not report alignment or blindly restart/reissue actions. |
| `converging` | Within rollout deadline, observed releases are allowed, and coverage/health meet policy | Track progress without treating expected coexistence as failure. |
| `aligned` | Current target generation; all required units, artifacts, and configuration verified; schema compatibility verified to the extent D15 provides; health/smoke criteria met | Record time- and scope-qualified evidence; recheck after observations expire. With catalog evidence only, this state means "required migrations applied" and is reported in those words. |
| `drifted` | Unallowed version, configuration/schema drift, or missed convergence deadline | Produce a difference report and repair intent; execute where policy permits. |
| `unhealthy` | Versions may match, but health or functional checks fail | Diagnose the failure; version equality is not success. |
| `blocked` | Repair lacks execution capability, reliable result probing, compatible rollback, or required privileges | State the blocker and smallest next step; preserve evidence. |

By default, inventory all units expected to serve. Sampling supports conclusions only about the sample. Include background workers, scheduled jobs, and independently deployed services. Report gaps where frontend/CDN/client caches cannot be fully observed. Use existing rolling-deployment mechanisms and status rather than building a new container scheduler. [Deployment reference](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)

## 4. Agent and deterministic executor responsibilities

Probes and comparisons use deterministic code. Invoke the agent for meaningful drift, failures, or policy interpretation. It receives a short fact packet plus evidence and proposes a diagnosis/action; the executor checks target, privileges, preconditions, and idempotency. Stable healthy operation neither calls the model every cycle nor repeatedly notifies. Deployment events trigger checks, supplemented by bounded periodic verification, backoff, jitter, and upstream rate limits. Frequency follows the environment's recovery objectives.

Follow the existing [manager-agent protocol](MANAGER_AGENT_PROTOCOL.md#english-version). Execute automatically within existing authorization without adding a human approval role for every action. Report required action when privileges are missing or policy must change. Typical permitted repairs include retrying a confirmed retryable deployment, replacing a confirmed drifting instance, pausing a failed rollout, or restoring a verified compatible artifact. Never blindly roll back a database solely because version numbers differ.

State follows the existing bounded-checkpoint, evidence-reference, and idempotent-command protocol. The intended durable backend remains cloud object storage, currently on hold. Do not introduce a PostgreSQL `ops` schema, local file, or SQLite as the sole formal task state. Use MemoryStore for local protocol tests. It cannot show recovery after the supervisor process exits, which R2's acceptance requires, so until C09 D17 decides how R2 proceeds. Migration receipts are database facts, separate from task checkpoints.

## 5. Repair and interruption windows

Sequence: read the target fresh → inspect actual state → durably record intent and verification → executor rechecks generation/preconditions → execute → independently verify → publish results.

| Situation | Recovery rule |
|---|---|
| Intent recorded before execution | Query the external operation ID/state; resume the same command after confirming it has not executed. |
| Action may have completed but reply was lost | Query deployment and instances first; timeout does not imply failure. Preserve unknown if the outcome cannot be established. |
| Instances switched but state publication failed | Verify reality and record completion without deploying again. |
| Desired target changes or supervisors compete | Executors enforce coordination and generation/fencing checks; reject/cancel stale commands and reconcile already executed actions. Task CAS is not external fencing. |
| New code requires an unfinished migration | Wait or stop traffic switching according to compatibility requirements; changing the code version alone is insufficient. |
| Code rollback is needed after data changes | Verify old code against current schema/data, otherwise repair forward. Do not restore an old database snapshot while ignoring subsequent business writes. |

Where a deployment platform lacks fencing, specify verifiable serialized control and cancellation boundaries. If these cannot be established, restrict automatic actions or provide detection only; do not promise mutual exclusion. This is a recoverable coordination workflow, not cross-system exactly-once execution across databases, object storage, and deployment platforms.

## 6. Decisions left to the user

Same format as D1–D9 (section 4 of [low-cost adoption](LOW_COST_ADOPTION_PLAN.md#english-version)).

| ID | Decision | Options and consequences | Blocks | Settle with |
|---|---|---|---|---|
| D15 | Source of an application's schema requirements | (a) Declared per migration by its author (expand or contract phase, and which releases it supports): reliable, but adds manual work to every migration. (b) The application's own tests run against the candidate schema in a validation environment (S3, D14). (c) Both. (d) Catalog only: can say "migrations applied", not "compatible". | R1 `aligned`, the schema plan's first acceptance scenario | D1, D14 |
| D16 | Supervisor scope | (a) The full chain of section 1, with an adapter per deployment platform. (b) A compatibility gate only: take desired/running state from the platform (for example Kubernetes Deployment conditions, Argo CD, Flux) and add the schema and reference-data checks. (c) Start with (b) and extend platform by platform on evidence. | R0–R3 | D1 (deployment platform), D8 |
| D17 | Supervisor state while cloud work is on hold | (a) Wait for C09: R2 automatic repair and A2 maintenance delegation are blocked. (b) Allow a durable local backend for development tests only (changes the AGENTS.md rule that forbids local files, SQLite, or a PostgreSQL `ops` schema as the sole formal state). (c) Until C09, detect and propose repairs only; no automatic repair. | R2, A2 | None |

## 7. Delivery stages, gates, and acceptance

| Stage | Deliverable | Required acceptance | Needs |
|---|---|---|---|
| R0 Version mapping | Manifest format, source/artifact binding, environment target, read-only probe for at least one existing deployment method | Distinguish identical tags with different artifacts and identical source with different builds; missing evidence returns unknown. | D1, D7, D16, G7 |
| R1 Drift detection | Instance inventory, schema compatibility, state machine, timeouts, notification deduplication | Target v12, two v12 instances and one v11: converging during allowed rollout, drifted after deadline, aligned after complete verification. An unreachable instance cannot yield aligned. | D15 |
| R2 Controlled repair | Deployment adapters, idempotent queries, generation checks, recovery executor | Cover lost replies, duplicate commands, changed targets, competing supervisors, and post-execution checkpoint failure; never repeat nonretryable operations. | D17, G8 |
| R3 Formal operation | Separately scheduled real state backend, actual process/host recovery, monitoring and operating cost | Measured detection/recovery time, coverage, false positives/negatives, model/storage cost, and unresolved actions. | C09 |

| Gate | Before | Checks |
|---|---|---|
| G7 Real deployment method | R0 counts as delivered | Probe one real deployment method of the pilot project (from D1), read-only. This project has no deployed platform yet (ECS is on hold); tests against a simulated deployment API only check the state machine itself. |
| G8 Automatic repair | R2 executing any repair automatically | D17 is decided, and the interruption-window tests of section 5 pass with state that survives a supervisor process restart; otherwise R2 only proposes repairs. |

R0/R1 can first be developed against local services or simulated deployment APIs. Example version numbers are test fixtures, not real releases. Agent output is diagnostic input; probes and verification evidence determine completion.
