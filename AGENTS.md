中文 | [English](#english-version)

# Forkpoint：Agent 开发与维护约定

适用范围：本目录及子目录。更新日期：2026-10-18。

**当前云服务状态：未经真实验证，云端部署暂缓。** OSS、GCS、S3 均无真实联调结果，ECS 尚未部署。用户当前不安排云资源采购或部署；只继续本地开发与模拟测试，不自动采购、部署或运行会访问真实云服务的测试。不得将 SDK 测试替身、内存服务模型、本机 HTTP 或本机 PostgreSQL 的结果表述为云存储已验证。

本文件是任务入口与架构约束，不是完整操作历史。先读本文，再按任务读取对应模块和文档，避免每次把整个项目装入上下文。用户当前明确要求优先于本文；本文不改变运行环境的权限边界。

## 来源与采用方式

- [Anthropic：Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control)：2026-10-16 重新读取，页面标注 2026-10-15 更新。采用其架构原则，按本项目的数据库和管理状态场景落实。
- 模块重构框架（作者的本地文档，不在本仓库中）：采用分阶段、行为保持、可验证和可回退的重构流程。
- 当前实现与用户已确定的约束见 [README](README.md)、[版本引擎设计](docs/DESIGN_REVIEW.md)、[云端状态设计](docs/CLOUD_STATE_DESIGN.md) 和 [管理 Agent 协议](docs/MANAGER_AGENT_PROTOCOL.md)。以下具体接口、容量与事务要求来自本项目。

## Scion 原则的提炼

面向在线客户端；服务端承担复杂计算，客户端保持轻量；完整能力通过 API 提供；组件可独立扩容；提交增量化并减少全局协调；工作副本按需建立。不可变内容支持复用，派生索引可重建，缓存分层隔离故障。衡量实际负载，避免用全历史扫描代替索引，也避免为假想问题堆叠复杂性。

这些是设计方向。Scion 的规模、性能数据和内部实现不是本项目已经具备的能力；其权限与审批产品选择不直接移植到本项目。

## 项目定位与当前边界

- 第一阶段负载为单表约 4,000 行、10 个并发 Agent。目标是检验 Agent 的分支、固定版本、联合提交、维护与恢复流程；不能因数据库曾面向人类开发者，就推断 PostgreSQL 或 Spanner 已无法承载需求。
- 代码及业务数据版本由现有 Ubuntu WSL PostgreSQL 管理。使用 `connection.local.json` 和 `db.py`；不要替换为 SQLite、临时 PostgreSQL 实例或旧 `.runtime` 中的服务。
- 管理任务的正式持久状态设计为使用满足项目协议的云端对象存储；GCS、阿里云 OSS 和 S3 是候选适配器，尚未完成真实云端接入；运行时使用有界内存缓存。不要因云端工作暂缓而改为 PostgreSQL `ops` schema、本地恢复文件或磁盘状态库。源码、配置和本地测试报告仍可保存在项目目录。
- GCS、OSS、S3 适配代码、ECS 配置及 HTTP 维护客户端已编写。用户报告已创建杭州 OSS 桶 `<your-oss-bucket>`，但未核验其配置、权限或真实读写，且尚无 ECS。MemoryStore 仅用于测试。部署文档只是 [暂缓执行的草案](docs/ALIYUN_ECS_DEPLOYMENT.md)，不是已完成能力的证明。
- 当前版本引擎仍是单节点 PostgreSQL，分支头发布使用锁；多区域缓存、数据分片、虚拟文件系统和统一版本服务 RPC 均不是已完成能力。新增这些能力必须有负载依据和明确迁移方案。

## 必须保持的设计约束

### 服务端语义与按需读取

版本判断、冲突处理、幂等发布与恢复判定属于服务端领域逻辑。Agent 客户端提交意图、固定版本和必要输入，消费结构化结果；不要让每个 Agent 复制完整历史或自行重写一套合并规则。

优先通过固定 revision、task_id、对象哈希和分页游标定位数据。`vc.diff_files`、`vc.diff_rows` 等操作不应因新增客户端包装而强制检出整个代码仓库。真正需要文件的工具只物化所需路径；文件虚拟化属于后续能力。

控制请求与大对象传输分开设计：状态与指针保持小，证据按引用获取。新增缓存层沿用稳定对象契约，明确容量、超时、退避与回源上限，避免下游故障把全部负载直接转移给上游。未出现相应负载前，不为形式上的“分布式”预建多层服务。

### 版本事实、派生数据与发布点

- 封口后的 changeset、代码内容和历史记录不可追加修改；工作事务先暂存，`finish_change()` 再封口。管理 Agent 的 superuser 能力不把破坏历史一致性变成正常写入方式。
- 分支读取固定版本，不叠加实时 main。状态回放沿第一父链；共同祖先查询使用全部父边，不能混用。
- 快照、缓存和查询索引应标明输入版本、完成条件与重建方式。派生数据不能成为唯一事实来源。任务 head 及其发布的命令去重索引涉及协议语义，不能当作普通缓存随意丢弃。
- PG 内代码与业务记录的联合变更、原生约束校验和版本发布保持同一事务；失败整体回滚。云端状态发布与 PG 执行不共享事务。
- 所有后端先写不可变对象，再通过 HeadStore 条件发布任务入口。GCS 使用 generation，S3 使用 ETag 条件写，OSS 使用固定记录的条件追加；存储标记只作相等条件，缓存排序使用应用层 version。重复命令先查已发布结果；相同 command_id、相同请求返回原结果，复用 ID 改变内容则拒绝。发生冲突后重新判断意图，不能只改 expected_version 覆盖。
- 内容哈希依赖稳定的编码规则。压缩、分块或序列化调整涉及对象格式兼容，必须明确哈希所针对的字节、版本识别与旧对象读取方式；不能当作文件移动式重构。

### 增长成本与上下文预算

新增写路径应说明成本取决于变更量、表行数还是历史长度；先测量写放大、锁竞争、读取量与恢复成本，再决定更换存储引擎或分片。避免引入跨任务的全局写锁；现有分支锁与快照协调只能在正确性得到替代保障后调整。

管理状态当前上限为 32 KiB，每次发布保存完整的有界检查点。常规冷恢复读取任务入口和当前状态两个逻辑对象，不扫描 bucket、不回放整个事件链；OSS 每次只读取追加文件尾部的固定记录；这是管理上下文的约束，不是所有 SQL 历史查询的性能承诺。逻辑读取数、SDK 调用数和实际 HTTP 请求数分别计量。

上下文默认上限为 8,192 UTF-8 字节。必要字段保留，可选内容超限时返回省略标记与引用；禁止静默丢失未决事项或用摘要取代证据。精确 token 预算须指定编码器。缓存同时限制条目数和字节数；重要操作前读取 fresh 状态。持续在线不等于上下文永久存在，也不等于网络和进程永不失效。

OSS 第一版每个任务最多发布 524,288 次，达到 4 GiB 追加文件上限即拒绝写入；需要更多发布时显式建立后继任务，未实现透明轮转。不得删除/覆盖活跃追加文件、复用 task_id 或在运行期间开启桶版本控制/保留策略。历史与孤儿对象仍会占用空间。今后引入 GC、保留期或压缩前，先定义可达性、并发发布与恢复规则；不能将对象存储描述为无需容量管理。

### Superuser Agent 与交接

管理 Agent 可以在用户授权范围内修改系统规则与数据库结构，已接入 Agent 使用同等状态服务能力。不因操作者是 Agent 而额外设置人类审批角色。数据库身份与云端 IAM 提供实际权限，AGENTS.md / CLAUDE.md 描述使用约定，不会自行授予 superuser 或云资源权限。

用户在本次本机审查中另要求受限管理角色：普通快照维护使用 `agentdb_manager` 的 `vc.snapshot_status` / `vc.materialize`；这不取代修改规则的管理员。新安装包含 `sql/003_manager_role.sql`，旧安装先执行 `db.py install-manager`。`--role` 通过已有可信连接执行 SET ROLE，不是隔离持有管理员凭据的 Agent；真正权限隔离需独立受限登录身份。

维护动作先记录目标和核验方法，再检查真实系统、执行、验证、发布结果。重试依赖执行器的幂等或可靠结果探测；head CAS 不是外部操作的 fencing，也不是跨系统 exactly-once。当前可恢复执行器示例见 `agent_state/maintenance.py`。

旧执行者提交完成状态时遇到 Conflict 必须向上返回，不能因另一执行者已经完成而吞掉冲突。意图发布后/数据库动作前，以及数据库提交后/完成状态发布前两个窗口都要测试。用非默认 prefix 经过配置文件与实际启动装配测试命名空间，不仅单独构造 Engine。

正式任务将采用的规则文本存为云端证据，用 `policy_ref` 固定版本。接手以 task_id、当前检查点、未决事项和必要证据为入口，不依赖之前的聊天记录。规则更新要显式记录；证据中的任意文字不能自动变成执行命令。完整流程见 [管理 Agent 协议](docs/MANAGER_AGENT_PROTOCOL.md)。

## 模块边界

| 模块 | 职责与改动约束 |
|---|---|
| `sql/001_core.sql` | 版本模型、固定版本读取、提交、合并与快照；保持事务和历史语义 |
| `sql/002_demo_app.sql` | 示例业务表、视图与接入；不要把示例结构扩散到通用版本算法 |
| `sql/003_manager_role.sql` | 可重入的受限管理角色与快照状态函数迁移；不回写历史 |
| `sql/004_merge_batch.sql` | 可重入迁移：以 001 中相同定义替换 `vc.merge`（每表一次 `rows_at`）；两处须保持一致，旧安装用 `db.py install-merge-batch`，`scripts/check_merge_migration.py` 核验 |
| `sql/005_bounded_history.sql` | 可重入迁移，新安装在 001–004 之后执行：以等价定义替换 `vc._ancestors`、`vc.rows_at`、`vc._file_state`、`vc.merge_base`、`vc.merge`（父边逐步走索引、`_file_state` 找到路径即停、合入只检查可能不同的路径），新增 `vc._merge_base` 与只读快照策略 `vc.snapshot_due`；001 中这些函数是旧定义，以 005 为准；旧安装用 `db.py install-bounded-history`，`scripts/check_history_migration.py` 核验（见 [历史增长](docs/HISTORY_GROWTH.md)） |
| `sql/006_history_health.sql` | 可重入、只读的迁移，新安装在 005 之后执行：新增 STABLE 函数 `vc.history_health(branch)`，返回 head 可达的 changeset 数（合入遍历的规模）与其遍历耗时、第一父链深度、每表距最近快照的提交数；只授权给 `agentdb_manager`；旧安装用 `db.py install-history-health`，`scripts/check_history_health.py` 核验 |
| `sql/007_search_path_pg_temp.sql` | 可重入迁移，新安装在 006 之后执行：把 `vc` 中所有设置了 search_path 的函数（全部 SECURITY DEFINER 函数及 `register_table`、`_immutable`）的 search_path 改为 `pg_catalog, vc, pg_temp`（PostgreSQL 手册“安全编写 SECURITY DEFINER 函数”的做法），不改函数体、属主与授权；遇到意外的 search_path 则整体中止。001–006 的函数定义本身已把 `pg_temp` 列在最后，重跑这些迁移不会撤销加固，007 用于升级此前的安装；旧安装用 `db.py install-search-path-hardening`，`scripts/check_search_path_migration.py` 核验，`scripts/check_agent_boundaries.py` 在新安装上检查 |
| `sql/008_read_set_merge.sql` | 可重入迁移，新安装最后执行：只新增 `vc.merge_validated`（读冲突报 RS001）与内部的 `vc._rows_at_keys`，不改已有函数；旧安装用 `db.py install-read-set-merge`，测试在 `run_tests.py` 的 `read_set_merge`（含两个会话的竞态），耗时用 `scripts/benchmark_read_set_merge.py` 测量；回滚方法写在该文件开头 |
| `db.py` | 连接既有 PostgreSQL；不承担版本算法，不隐式启停数据库。`Session` 复用一个 psql 进程，每次调用的语义须与 `Connection.execute` 单次进程一致 |
| `agent_state/storage.py` | 公共对象/错误契约、原有 GCS 适配与内存测试替身 |
| `agent_state/heads.py`、`oss.py`、`s3.py` | 条件发布接口与提供商适配；不能以普通覆盖代替原子条件操作 |
| `agent_state/config.py`、`notifications.py` | 后端选择、配置校验与通知转换；SDK 按需加载 |
| `agent_state/objects.py` | 稳定编码、内容寻址、不可变对象与命令索引 |
| `agent_state/engine.py` | 状态发布、版本竞争、幂等与历史语义 |
| `agent_state/runtime.py` | 有界缓存、通知失效与上下文预算 |
| `agent_state/server.py` | HTTP、认证、启动装配与通知接入；不复制领域规则 |
| `agent_state/client.py` | 本机维护执行器访问远程状态 API；无持久缓存，不隐式重试或改写命令 |
| `agent_state/maintenance.py` | 通过状态接口和数据库连接执行可探测、可恢复的维护；`run_due` 按快照策略 `vc.snapshot_due` 逐个目标调用 `run`；`check_history` 读取 `vc.history_health`，按任务 policy_ref 中的阈值（默认 20,000 个可达 changeset、合入中位 500 ms）发布事实，越线时以 uncertainty 与 next_action 报告需要代数索引（见 [管理 Agent 协议](docs/MANAGER_AGENT_PROTOCOL.md#历史增长检查)） |
| `deploy/` | ECS 打包、首次安装与 systemd；显式文件清单，排除本机连接及凭据配置 |
| `deploy/staging/` | 预发布 PostgreSQL 的专用登录角色脚本（非超级用户，仅 CREATEDB，对两个 NOLOGIN 角色只有 SET 权限）；由服务器管理员执行，口令交互设置，不写入文件（见 [预发布接入](docs/STAGING.md)） |
| `review_local.py`、`agent_lab.py` | 仅限现有本机 PostgreSQL 的专用测试库；分别记录脚本并发与真实 GPT 行为 |
| `agent_lab_gateway.py` | 可选的回环常驻进程，为 `agent_lab.py` 的工作动作复用 `db.Session`；默认关闭，未运行时 `agent_lab.py` 行为不变；不重试，不并入 `agent_state/server.py`（见 [设计](docs/AGENT_LAB_GATEWAY.md)） |
| `experiments/gpt_repeat/` | 真实 GPT 重复实验、原始 RPC JSONL、独立 SQL 核验及离线统计；不修改生产版本算法，不把预期冲突或服务中断隐藏为成功 |
| `experiments/readset/` | 读集校验实验：`xr` 原型只装入测试库，不修改 `vc.*`；见 [设计与结果](docs/READSET_EXPERIMENT.md) |
| `scripts/` | 基准、迁移核验与预发布演练；在项目根目录以 `python scripts/<name>.py` 运行，脚本自行把根目录加入 `sys.path`；纳入版本库的结果默认写入 `results/` |
| `results/`、`examples/` | 文档中数字对应的原始结果（`results/staging/` 为预发布虚拟机）；可直接用 psql 运行的 SQL 示例 |

以下文件留在根目录，不要移入 `scripts/`：`db.py`、`run_tests.py` 是主入口；`experiments/gpt_repeat/run_gpt_repeats.py` 按根目录路径调用并记录 `agent_lab.py`、`review_local.py` 的哈希；`agent_lab.py` 按模块名导入 `agent_lab_gateway`；ECS 部署包与 `install.sh` 按根目录路径使用 `smoke_cloud.py` 和 `requirements-*.txt`。

核心依赖方向为 `runtime → engine → objects / HeadStore → 存储适配器`。网络、时钟、数据库与存储通过明确边界接入，便于替换和故障注入。SDK 集成留在适配器或启动装配层；新增薄层必须解决实际耦合，不为每个函数制造接口。

公共契约包括 SQL 函数、HTTP 请求/响应与错误、配置项、对象编码/哈希、head 协议、游标、命令重试语义和缓存新鲜度。对这些内容的改变属于行为或兼容性变更，即使函数签名没变。

## 模块重构流程

采用来源文档的核心规则：**永远不要同时改变行为和结构。** 功能或 bug 修复与纯结构调整分为独立、可核验的步骤；用户已要求的修复继续完成，不借“保持旧行为”遗漏修复目标。

| 阶段 | 实际动作 | 通过条件 |
|---|---|---|
| 0：目标与基线 | 写明动机、公共接口、非目标、验收指标和回退方法；用 `rg` 找调用方并运行相关现有套件 | 相关套件稳定全绿；已有失败先定位并与结构改动分开处理 |
| 1：安全网 | 盘点公共行为覆盖缺口；补特征化/契约测试，必要时保留黄金样本；覆盖错误、并发、时钟及顺序 | 用例稳定且确能发现行为破坏；对关键路径可做小范围变异或故障注入 |
| 2：接缝 | 提取外部依赖，注入时钟/存储/连接；必要时建立薄 facade；分离副作用与纯计算 | 改动小，相关完整套件通过，外部行为保持 |
| 3：机械移动 | 每步只做重命名、提取、移动或已证实的死代码删除 | 旧契约断言无需更改；每个独立步骤保持可用、可回退 |
| 4：结构调整 | 按风险选择渐进替换、并行实现或抽象后的开关切换；用录制输入做新旧差异比较 | 契约与相关集成套件通过；性能目标与基线可比 |
| 5：迁移调用方 | 小批切换，保留必要兼容入口；观察错误、延迟和冲突率 | 每批集成验证通过；实际部署有条件时先进行预发布或金丝雀验证 |
| 6：收尾 | 确认无调用后删除旧实现、开关与临时适配；更新文档 | 相关完整套件通过，`rg` 无残留引用，兼容与恢复路径有结论 |

补充执行规则：

- 不为通过重构而修改原有公共行为断言。若契约确需改变，显式拆出行为变更及兼容/迁移步骤；已有用户授权范围内直接推进，不重复要求确认。
- 测试以公共行为、边界与故障恢复为主，不按函数数量机械增加测试。新增内部算法有独立风险时再补有价值的单元测试；纯文档改动检查内容与引用即可。
- 影子执行有副作用的写路径时，要隔离目标或只做可验证的干运行，不能把同一真实维护动作执行两遍来“比较”。
- 持久格式迁移必须考虑旧读者、旧规则和旧数据；回退代码不等于回退数据。回退方法必须在改变格式前确定。
- 提交应小到可用一句话说明目的。意外失败时优先缩小变更；只撤销自己本次改动，保留用户已有工作。设置阶段检查点，重新评估收益，不做无边界重写。

## 验证入口与交付要求

协议与限制以 [多后端设计](docs/STORAGE_BACKENDS.md) 为准。全套适配器本地测试需安装 `requirements-storage-tests.txt`；单个后端运行只需其依赖。本地记录见 [多后端本地测试](docs/STORAGE_BACKENDS_VALIDATION.md)，其中不含真实云端结果。

在项目根目录按改动范围选择检查；完成相关检查后，只有新改动、失败或未解风险才扩展/重复测试。

| 范围 | 命令与条件 |
|---|---|
| PostgreSQL 版本引擎 | `python run_tests.py`：连接现有服务并创建专用测试数据库；不向项目业务库导入测试数据 |
| Docker 演示 | `docker compose up --build --exit-code-from demo`：在内存中的 PostgreSQL 16 上用 `db.py` 安装，再运行 `examples/two_agents.sql`；结果不符时退出码非零。这是给访客的演示，不取代上面基于现有 WSL PostgreSQL 的开发与测试；需要 Docker Desktop 正在运行 |
| 管理状态与 HTTP | `.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests -v` |
| agent_lab 网关 | 单元测试含在上一行中（基于黄金样本，不连数据库）；`python scripts/compare_agent_lab_gateway.py` 新建两个 `agentdb_test_gpt_*` 库，对比逐次起进程与经网关两条路径的输出和耗时 |
| 恢复读取结构 | `.\.venv-cloud\Scripts\python.exe scripts/benchmark_cloud_state.py`：内存后端，检查历史增长时的读取量 |
| 维护与 PG 接手 | `.\.venv-cloud\Scripts\python.exe scripts/integration_cloud_postgres.py --database agentdb_test_EXISTING`：替换为已安装示例的测试库 |
| OSS 读取结构 | `.\.venv-cloud\Scripts\python.exe scripts/benchmark_oss_protocol.py`：使用内存 OSS 服务模型，不代表云端性能 |
| 本机审查与全部合入 | `.\.venv-cloud\Scripts\python.exe review_local.py`：新建专用测试库；18 项原断言、受限角色、两个中断窗口和十个分支最终全部合入，以及 main 锁被占用时合入报告 `lock_timeout`；计时包含 COMMIT；写入者默认每人复用一个长连接，`--client spawn` 恢复为每次调用启动进程 |
| 历史增长迁移 | `.\.venv-cloud\Scripts\python.exe scripts/check_history_migration.py`：新建旧版（0bf7309）测试库，迁移两次后逐项比较读取与合入结果、授权和新安装定义，并经 `SnapshotMaintenance.run_due`（MemoryStore）执行快照策略 |
| 历史健康检查 | `.\.venv-cloud\Scripts\python.exe scripts/check_history_health.py`：新建 006 之前（d5c68b0）的测试库并写入分支、合入与快照历史，迁移两次后核对数据、读取与合入结果及已有函数不变，`vc.history_health` 为 STABLE、仅 `agentdb_manager` 可执行、与新安装一致，各分支数值与独立查询及 `vc.snapshot_due` 相符，再经 `SnapshotMaintenance.check_history`（MemoryStore）发布一次、重跑不发布；单元测试见 `tests/test_cloud_state.py` 的 `HistoryHealthTests` |
| search_path 加固迁移 | `.\.venv-cloud\Scripts\python.exe scripts/check_search_path_migration.py`：新建 007 之前（a7e60d9）的测试库，迁移两次后核对数据与历史不变、vc 中每个函数除设置了 search_path 的函数的 search_path 外属性不变、与新安装一致 |
| Agent 边界对抗检查 | `.\.venv-cloud\Scripts\python.exe scripts/check_agent_boundaries.py`：经 `agent_lab.py init` 新建 `agentdb_test_gpt_*`，审计 `agentdb_client` 可执行的函数、可访问的表、可建对象的 schema 与设置了 search_path 的函数（含全部 SECURITY DEFINER 函数）的 search_path；以 `agentdb_client` 自写 SQL 尝试目录审计看不到的操作（停用捕获触发器、关闭会话触发器、未开启变更就写分支视图）、经授权函数破坏版本历史，并向 `agent_lab.py` 传入应被拒绝的参数（须由对应检查以参数错误拒绝：退出码 2、无 Python traceback）；最后核对 main 不变。越界记为 GAP 并以退出码 1 结束；连接参数只接受 `--config` |
| 预发布虚拟机 S4/S5 | `.\.venv-cloud\Scripts\python.exe scripts/staging_drills.py growth|backup|restart|crash|netcut|kill --config connection.staging.local.json`：只对 [预发布虚拟机](docs/STAGING_VM.md) 运行；restart/crash/netcut 会通过 VBoxManage 断电、重启或断网，切勿指向重要数据库；结果见 [S4/S5](docs/STAGING_S4_S5.md) |
| 读集校验实验 | 单元测试、契约测试、变异检查、实验与两项核查的命令见 [设计文档](docs/READSET_EXPERIMENT.md)“文件与运行”一节 |
| 真实云端（未执行，当前暂缓） | `smoke_cloud.py` 仅保留为以后联调入口；当前不运行，不据此宣称任何提供商已通过验证 |

真实模型实验遵循预登记协议：记录接口返回的精确模型 ID、推理强度、运行器版本、提示词与源文件哈希；不把未暴露的权重快照猜成确定版本。固定样本数并保留失败，不以新轮次替换失败轮次。保存未经摘要或截断的工具请求/响应 JSONL，逐 Agent 保存累计用量最后值；缓存输入与推理输出是子项，缺失用量保持未知。每次实际写入后独立核查已发布内容和对方分支，发生覆盖后又恢复仍计为失败。区分受控恢复提示、脚本压力测试和模型自主行为；云端状态仍未经真实验证。

现有记录见 [真实 GPT 重复实验](docs/GPT_REPEAT_EXPERIMENT.md)。在项目根目录用 `python -m unittest discover -s experiments/gpt_repeat -p test_repeat_scoring.py -v` 检查计数与覆盖判定；用 `python experiments/gpt_repeat/analyze_repeat_logs.py experiments/gpt_repeat/results/2026-10-17` 和 `python experiments/gpt_repeat/audit_repeat_logs.py experiments/gpt_repeat/results/2026-10-17` 重新分析本地证据。这些离线命令不调用模型、不写数据库；重新跑模型的命令及额度影响见实验报告，不能为文档修改自动重跑。

涉及并发/恢复的变更至少核验：旧版本竞争、旧命令重试、发布前后中断、缓存失效竞态、固定版本一致性与上下文省略提示。涉及新格式时增加旧数据兼容检查；不要用跑通新建空库代替迁移验证。

交付说明改了什么、为什么、本地检查结果和剩余限制。区分内存模拟、SDK 契约与真实本机 PostgreSQL；各云提供商均须注明未经真实验证，删除任何将本地结果包装为云端成功的表述。容量与延迟结论注明负载和环境。本机记录见 [版本引擎验证](docs/VALIDATION.md) 与 [状态模块本地测试](docs/CLOUD_STATE_VALIDATION.md)，不能当作以后改动已经自动通过的证明。

---

<a id="english-version"></a>

# Forkpoint: Conventions for Agent Development and Maintenance

Scope: this directory and its subdirectories. Last updated: 2026-10-18.

**Current cloud service status: not verified against real cloud services; cloud deployment is on hold.** OSS, GCS and S3 have no real integration results, and ECS has not been deployed. The user is not currently scheduling cloud resource purchases or deployment; only continue local development and simulated testing, and do not automatically purchase, deploy, or run tests that access real cloud services. Results from SDK test doubles, the in-memory service model, local HTTP or the local PostgreSQL must not be described as cloud storage having been verified.

This file is the task entry point and the architectural constraints, not a complete operations history. Read this file first, then read the modules and documents relevant to the task, rather than loading the whole project into context every time. The user's current explicit requests take precedence over this file; this file does not change the permission boundaries of the runtime environment.

## Sources and how they are adopted

- [Anthropic: Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control): re-read on 2026-10-16; the page is marked as updated on 2026-10-15. Its architectural principles are adopted and applied to this project's database and management-state scenarios.
- Module refactoring framework (the author's local document, not in this repository): its staged, behavior-preserving, verifiable and reversible refactoring process is adopted.
- The current implementation and the constraints the user has settled are in [README](README.md), [Version engine design](docs/DESIGN_REVIEW.md), [Cloud state design](docs/CLOUD_STATE_DESIGN.md) and [Manager Agent protocol](docs/MANAGER_AGENT_PROTOCOL.md). The specific interface, capacity and transaction requirements below come from this project.

## Distilled Scion principles

Oriented toward online clients; the server takes on the complex computation and clients stay lightweight; full capability is provided through APIs; components can scale independently; commits are incremental and global coordination is reduced; working copies are built on demand. Immutable content supports reuse, derived indexes are rebuildable, and cache layers isolate failures. Measure the actual load, avoid replacing indexes with full-history scans, and also avoid piling up complexity for hypothetical problems.

These are design directions. Scion's scale, performance data and internal implementation are not capabilities this project already has; its product choices on permissions and approvals are not transplanted directly into this project.

## Project positioning and current boundaries

- The first-phase load is a single table of about 4,000 rows with 10 concurrent Agents. The goal is to test the Agent workflows for branching, pinned versions, joint commits, maintenance and recovery; do not infer that PostgreSQL or Spanner can no longer meet the needs just because the databases were once aimed at human developers.
- Code and business data versions are managed by the existing Ubuntu WSL PostgreSQL. Use `connection.local.json` and `db.py`; do not replace it with SQLite, a temporary PostgreSQL instance, or the service in the old `.runtime`.
- The formal persistent state of management tasks is designed to use cloud object storage that satisfies the project protocol; GCS, Alibaba Cloud OSS and S3 are candidate adapters and have not completed real cloud integration; at runtime a bounded in-memory cache is used. Do not switch to a PostgreSQL `ops` schema, local recovery files or an on-disk state store because cloud work is on hold. Source code, configuration and local test reports can still be kept in the project directory.
- The GCS, OSS and S3 adapter code, the ECS configuration and the HTTP maintenance client have been written. The user reports having created the Hangzhou OSS bucket `<your-oss-bucket>`, but its configuration, permissions or real reads/writes have not been verified, and there is no ECS yet. MemoryStore is for tests only. The deployment document is only a [draft on hold](docs/ALIYUN_ECS_DEPLOYMENT.md), not proof of a completed capability.
- The current version engine is still single-node PostgreSQL, and branch head publishing uses locks; multi-region caching, data sharding, a virtual file system and a unified version-service RPC are not completed capabilities. Adding these capabilities requires a load-based justification and a clear migration plan.

## Design constraints that must be kept

### Server-side semantics and on-demand reads

Version decisions, conflict handling, idempotent publishing and recovery decisions are server-side domain logic. Agent clients submit intent, pinned versions and necessary inputs, and consume structured results; do not make each Agent copy the full history or rewrite its own set of merge rules.

Prefer locating data by pinned revision, task_id, object hash and pagination cursor. Operations such as `vc.diff_files` and `vc.diff_rows` should not be forced to check out the whole code repository because of a new client wrapper. Tools that really need files materialize only the paths they need; file virtualization is a later capability.

Design control requests separately from large-object transfer: state and pointers stay small, and evidence is fetched by reference. New cache layers follow the stable object contract and specify capacity, timeouts, backoff and limits on fallback to the origin, so that a downstream failure does not shift the whole load directly onto the upstream. Until the corresponding load appears, do not pre-build multi-layer services for the sake of being "distributed" in form.

### Version facts, derived data and publishing points

- Sealed changesets, code content and history records cannot be appended to or modified; work transactions are staged first and then sealed by `finish_change()`. The manager Agent's superuser capability does not turn breaking history consistency into a normal way of writing.
- Branches read a pinned version and do not overlay the live main. State replay follows the first-parent chain; common-ancestor queries use all parent edges; the two must not be mixed.
- Snapshots, caches and query indexes should state their input version, completion conditions and how they are rebuilt. Derived data must not become the only source of truth. The task head and the command deduplication index it publishes involve protocol semantics and must not be discarded at will like an ordinary cache.
- In PG, the joint change of code and business records, native constraint checks and version publishing stay in the same transaction; on failure everything rolls back. Cloud state publishing and PG execution do not share a transaction.
- All backends first write immutable objects and then conditionally publish the task entry point through HeadStore. GCS uses generation, S3 uses ETag conditional writes, and OSS uses conditional appends of fixed records; storage markers are used only as equality conditions, and cache ordering uses the application-level version. A repeated command first looks up the published result; the same command_id with the same request returns the original result, while a reused ID with changed content is rejected. After a conflict, re-evaluate the intent; do not just change expected_version and overwrite.
- Content hashes depend on stable encoding rules. Changes to compression, chunking or serialization affect object-format compatibility; they must specify which bytes the hash covers, how versions are identified, and how old objects are read; they must not be treated as a file-move style refactoring.

### Growth cost and context budget

New write paths should state whether their cost depends on the amount changed, the number of table rows or the length of history; first measure write amplification, lock contention, read volume and recovery cost, then decide whether to change the storage engine or shard. Avoid introducing global write locks across tasks; the existing branch locks and snapshot coordination may only be adjusted once correctness is guaranteed by a replacement.

Management state is currently capped at 32 KiB, and each publish saves a complete bounded checkpoint. A routine cold recovery reads two logical objects, the task entry point and the current state; it does not scan the bucket or replay the whole event chain; OSS reads only the fixed record at the tail of the append file each time; this is a constraint on the management context, not a performance promise for all SQL history queries. Logical reads, SDK calls and actual HTTP requests are counted separately.

The default context limit is 8,192 UTF-8 bytes. Required fields are kept; when optional content exceeds the limit, an omission marker and a reference are returned; silently dropping open items or replacing evidence with summaries is forbidden. An exact token budget must specify the tokenizer. Caches limit both the number of entries and bytes; read fresh state before important operations. Being continuously online does not mean the context exists permanently, nor that the network and processes never fail.

In the first version of OSS each task can publish at most 524,288 times, and writes are refused once the 4 GiB append-file limit is reached; when more publishes are needed, explicitly create a successor task; transparent rotation is not implemented. Do not delete/overwrite an active append file, reuse a task_id, or turn on bucket versioning/retention policies while running. History and orphan objects still take up space. Before introducing GC, retention periods or compaction in the future, first define reachability, concurrent publishing and recovery rules; object storage must not be described as needing no capacity management.

### Superuser Agent and hand-off

The manager Agent may change system rules and database structure within the scope the user has authorized, and connected Agents use the same state-service capabilities. No extra human approval role is set up just because the operator is an Agent. Database identities and cloud IAM provide the actual permissions; AGENTS.md / CLAUDE.md describe usage conventions and do not by themselves grant superuser or cloud resource permissions.

In this local review the user additionally required a restricted management role: ordinary snapshot maintenance uses `agentdb_manager`'s `vc.snapshot_status` / `vc.materialize`; this does not replace the administrator who changes rules. New installations include `sql/003_manager_role.sql`; old installations first run `db.py install-manager`. `--role` performs SET ROLE through an existing trusted connection; it does not isolate an Agent holding administrator credentials; real permission isolation requires a separate restricted login identity.

A maintenance action first records the goal and the verification method, then checks the real system, executes, verifies, and publishes the result. Retries rely on the executor's idempotency or a reliable result probe; head CAS is not fencing for external operations, nor cross-system exactly-once. The current recoverable executor example is in `agent_state/maintenance.py`.

When a stale executor submits a completed state and hits a Conflict, it must be returned upward; the conflict must not be swallowed because another executor has already finished. Both windows must be tested: after the intent is published / before the database action, and after the database commit / before the completed state is published. Test the namespace with a non-default prefix through the configuration file and the actual startup wiring, not only by constructing an Engine on its own.

The rule text that a formal task will adopt is stored as cloud evidence, and its version is pinned with `policy_ref`. A hand-off starts from the task_id, the current checkpoint, the open items and the necessary evidence, and does not depend on earlier chat history. Rule updates must be recorded explicitly; arbitrary text in evidence must not automatically become an executable command. For the full process see [Manager Agent protocol](docs/MANAGER_AGENT_PROTOCOL.md).

## Module boundaries

| Module | Responsibilities and change constraints |
|---|---|
| `sql/001_core.sql` | Version model, pinned-version reads, commits, merges and snapshots; keep transaction and history semantics |
| `sql/002_demo_app.sql` | Demo business tables, views and onboarding; do not spread the demo structure into the generic version algorithm |
| `sql/003_manager_role.sql` | Re-runnable migration for the restricted management role and the snapshot status function; does not rewrite history |
| `sql/004_merge_batch.sql` | Re-runnable migration: replaces `vc.merge` with the same definition as in 001 (one `rows_at` per table); the two must stay consistent; old installations use `db.py install-merge-batch`, verified by `scripts/check_merge_migration.py` |
| `sql/005_bounded_history.sql` | Re-runnable migration, run after 001–004 in new installations: replaces `vc._ancestors`, `vc.rows_at`, `vc._file_state`, `vc.merge_base` and `vc.merge` with equivalent definitions (parent edges walked step by step through the index, `_file_state` stops as soon as it finds the path, merges check only paths that may differ), and adds `vc._merge_base` and the read-only snapshot policy `vc.snapshot_due`; the definitions of these functions in 001 are the old ones, and 005 takes precedence; old installations use `db.py install-bounded-history`, verified by `scripts/check_history_migration.py` (see [History growth](docs/HISTORY_GROWTH.md)) |
| `sql/006_history_health.sql` | Re-runnable, read-only migration, run after 005 in new installations: adds the STABLE function `vc.history_health(branch)`, which returns the number of changesets reachable from head (the size of the merge traversal) and its traversal time, the first-parent chain depth, and the number of commits per table since the latest snapshot; granted only to `agentdb_manager`; old installations use `db.py install-history-health`, verified by `scripts/check_history_health.py` |
| `sql/007_search_path_pg_temp.sql` | Re-runnable migration, run in new installations after 006: sets the search_path of every function in `vc` that sets one (all SECURITY DEFINER functions plus `register_table` and `_immutable`) to `pg_catalog, vc, pg_temp` (the PostgreSQL manual's "Writing SECURITY DEFINER Functions Safely"), leaving bodies, owner and grants unchanged; stops without changing anything if it finds an unexpected search_path. The definitions in 001–006 now list `pg_temp` last themselves, so rerunning those migrations no longer undoes it, and 007 upgrades earlier installations; old installations use `db.py install-search-path-hardening`, verified by `scripts/check_search_path_migration.py`, and `scripts/check_agent_boundaries.py` checks it on a new installation |
| `sql/008_read_set_merge.sql` | Re-runnable migration, run last in new installations: only adds `vc.merge_validated` (read conflicts raise RS001) and the internal `vc._rows_at_keys`, changing no existing function; old installations use `db.py install-read-set-merge`, tested by `read_set_merge` in `run_tests.py` (including a two-session race), timed by `scripts/benchmark_read_set_merge.py`; the rollback is written at the top of the file |
| `db.py` | Connects to the existing PostgreSQL; does not carry the version algorithm and does not implicitly start or stop the database. `Session` reuses one psql process, and the semantics of each call must match a single-process `Connection.execute` |
| `agent_state/storage.py` | Common object/error contract, the original GCS adapter and the in-memory test double |
| `agent_state/heads.py`, `oss.py`, `s3.py` | Conditional publishing interface and provider adapters; ordinary overwrites must not replace atomic conditional operations |
| `agent_state/config.py`, `notifications.py` | Backend selection, configuration validation and notification conversion; SDKs are loaded on demand |
| `agent_state/objects.py` | Stable encoding, content addressing, immutable objects and the command index |
| `agent_state/engine.py` | State publishing, version races, idempotency and history semantics |
| `agent_state/runtime.py` | Bounded cache, notification invalidation and context budget |
| `agent_state/server.py` | HTTP, authentication, startup wiring and notification intake; does not duplicate domain rules |
| `agent_state/client.py` | Local maintenance executor's access to the remote state API; no persistent cache, no implicit retries or rewriting of commands |
| `agent_state/maintenance.py` | Performs probeable, recoverable maintenance through the state interface and the database connection; `run_due` calls `run` for each target according to the snapshot policy `vc.snapshot_due`; `check_history` reads `vc.history_health` and publishes facts according to the thresholds in the task's policy_ref (default 20,000 reachable changesets, median merge 500 ms), and when a threshold is crossed reports through uncertainty and next_action that a generation-number index is needed (see [Manager Agent protocol](docs/MANAGER_AGENT_PROTOCOL.md#history-growth-check)) |
| `deploy/` | ECS packaging, first installation and systemd; explicit file list, excluding local connection and credential configuration |
| `deploy/staging/` | Script for the staging PostgreSQL's dedicated login role (not a superuser, only CREATEDB, with only SET privilege on the two NOLOGIN roles); run by the server administrator, password set interactively and not written to files (see [Staging connection](docs/STAGING.md)) |
| `review_local.py`, `agent_lab.py` | Only for dedicated test databases on the existing local PostgreSQL; record scripted concurrency and real GPT behavior respectively |
| `agent_lab_gateway.py` | Optional loopback long-running process that reuses `db.Session` for `agent_lab.py`'s work actions; off by default, and when it is not running `agent_lab.py` behaves unchanged; no retries, not merged into `agent_state/server.py` (see [design](docs/AGENT_LAB_GATEWAY.md)) |
| `experiments/gpt_repeat/` | Real GPT repeat experiment, raw RPC JSONL, independent SQL verification and offline statistics; does not modify the production version algorithm, and does not hide expected conflicts or service interruptions as successes |
| `experiments/readset/` | Read-set validation experiment: the `xr` prototype goes only into test databases and does not modify `vc.*`; see the [design and results](docs/READSET_EXPERIMENT.md) |
| `scripts/` | Benchmarks, migration checks and staging drills; run from the project root as `python scripts/<name>.py`, each script adds the root to `sys.path` itself; committed results go to `results/` by default |
| `results/`, `examples/` | Raw results behind the numbers in the docs (`results/staging/` is the staging VM); SQL examples that run directly with psql |

These files stay in the project root; do not move them into `scripts/`: `db.py` and `run_tests.py` are the main entry points; `experiments/gpt_repeat/run_gpt_repeats.py` calls `agent_lab.py` and hashes it and `review_local.py` by their root paths; `agent_lab.py` imports `agent_lab_gateway` by module name; the ECS bundle and `install.sh` use `smoke_cloud.py` and `requirements-*.txt` at the root.

The core dependency direction is `runtime → engine → objects / HeadStore → storage adapters`. Network, clock, database and storage are connected through explicit boundaries, which makes replacement and fault injection easy. SDK integration stays in the adapter or startup-wiring layer; a new thin layer must solve an actual coupling, not create an interface for every function.

Public contracts include SQL functions, HTTP requests/responses and errors, configuration items, object encoding/hashing, the head protocol, cursors, command retry semantics and cache freshness. Changes to these are behavioral or compatibility changes, even if a function signature does not change.

## Module refactoring process

The core rule of the source document is adopted: **never change behavior and structure at the same time.** Features or bug fixes and pure structural adjustments are split into separate, verifiable steps; fixes the user has asked for are still completed, and "keeping the old behavior" is not used as an excuse to miss the fix goal.

| Phase | Actual actions | Pass criteria |
|---|---|---|
| 0: Goal and baseline | Write down the motivation, public interfaces, non-goals, acceptance metrics and rollback method; use `rg` to find callers and run the relevant existing suites | Relevant suites are stably all green; existing failures are located first and handled separately from structural changes |
| 1: Safety net | Inventory coverage gaps in public behavior; add characterization/contract tests, keeping golden samples when necessary; cover errors, concurrency, clocks and ordering | Cases are stable and actually catch behavior breakage; small-scale mutation or fault injection can be done on critical paths |
| 2: Seams | Extract external dependencies, inject clock/storage/connection; build a thin facade when necessary; separate side effects from pure computation | Small changes, the relevant full suites pass, external behavior is preserved |
| 3: Mechanical moves | Each step only renames, extracts, moves, or deletes proven dead code | Old contract assertions need no changes; each separate step stays usable and reversible |
| 4: Structural adjustment | Choose incremental replacement, parallel implementation, or switching behind an abstraction depending on risk; compare old and new with recorded inputs | Contract and relevant integration suites pass; performance targets are comparable with the baseline |
| 5: Migrate callers | Switch in small batches, keeping necessary compatibility entry points; watch errors, latency and conflict rates | Each batch passes integration validation; when actual deployment is possible, do staging or canary validation first |
| 6: Wrap-up | After confirming there are no callers, delete the old implementation, switches and temporary adapters; update the documents | Relevant full suites pass, `rg` finds no leftover references, and there are conclusions on the compatibility and recovery paths |

Additional execution rules:

- Do not modify existing public behavior assertions to make a refactoring pass. If a contract really needs to change, split out the behavior change and the compatibility/migration steps explicitly; within the scope the user has already authorized, proceed directly without asking for confirmation again.
- Tests focus on public behavior, boundaries and failure recovery, not on mechanically adding tests per function. Add valuable unit tests for new internal algorithms only when they carry their own risk; for documentation-only changes, checking the content and references is enough.
- When shadow-running a write path with side effects, isolate the target or do only a verifiable dry run; do not execute the same real maintenance action twice to "compare".
- Persistent format migrations must consider old readers, old rules and old data; rolling back code is not rolling back data. The rollback method must be settled before the format is changed.
- Commits should be small enough that their purpose can be stated in one sentence. On unexpected failures, prefer shrinking the change; only revert your own changes from this session and keep the user's existing work. Set phase checkpoints, re-evaluate the benefit, and do not do unbounded rewrites.

## Verification entry points and delivery requirements

The protocol and limitations are as defined in [Multi-backend design](docs/STORAGE_BACKENDS.md). The full local adapter test suite requires installing `requirements-storage-tests.txt`; running a single backend needs only its dependencies. Local records are in [Multi-backend local tests](docs/STORAGE_BACKENDS_VALIDATION.md), which contain no real cloud results.

From the project root, choose checks according to the scope of the change; after the relevant checks are done, extend/repeat tests only for new changes, failures or unresolved risks.

| Scope | Command and conditions |
|---|---|
| PostgreSQL version engine | `python run_tests.py`: connects to the existing service and creates a dedicated test database; does not import test data into the project business database |
| Docker demo | `docker compose up --build --exit-code-from demo`: installs with `db.py` on an in-memory PostgreSQL 16, then runs `examples/two_agents.sql`; a wrong result gives a non-zero exit code. This is a demo for visitors and does not replace development and testing on the existing WSL PostgreSQL above; Docker Desktop must be running |
| Management state and HTTP | `.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests -v` |
| agent_lab gateway | Unit tests are included in the previous line (based on golden samples, no database connection); `python scripts/compare_agent_lab_gateway.py` creates two `agentdb_test_gpt_*` databases and compares the output and timing of the spawn-a-process-per-call path and the gateway path |
| Recovery read structure | `.\.venv-cloud\Scripts\python.exe scripts/benchmark_cloud_state.py`: in-memory backend, checks read volume as history grows |
| Maintenance and PG hand-off | `.\.venv-cloud\Scripts\python.exe scripts/integration_cloud_postgres.py --database agentdb_test_EXISTING`: replace with a test database that has the demo installed |
| OSS read structure | `.\.venv-cloud\Scripts\python.exe scripts/benchmark_oss_protocol.py`: uses the in-memory OSS service model, does not represent cloud performance |
| Local review and merging everything | `.\.venv-cloud\Scripts\python.exe review_local.py`: creates a dedicated test database; the 18 original assertions, the restricted role, the two interruption windows and the final merging of all ten branches, plus a merge reporting `lock_timeout` while the main lock is held; timings include COMMIT; by default each writer reuses one long-lived connection, and `--client spawn` restores starting a process per call |
| History growth migration | `.\.venv-cloud\Scripts\python.exe scripts/check_history_migration.py`: creates an old-version (0bf7309) test database, migrates twice, then compares read and merge results, grants and the new-installation definitions item by item, and runs the snapshot policy through `SnapshotMaintenance.run_due` (MemoryStore) |
| History health check | `.\.venv-cloud\Scripts\python.exe scripts/check_history_health.py`: creates a pre-006 (d5c68b0) test database and writes branch, merge and snapshot history, migrates twice, then checks that data, read and merge results and existing functions are unchanged, that `vc.history_health` is STABLE, executable only by `agentdb_manager` and identical to a new installation, and that each branch's values match independent queries and `vc.snapshot_due`; then publishes once through `SnapshotMaintenance.check_history` (MemoryStore), and a rerun does not publish; unit tests are `HistoryHealthTests` in `tests/test_cloud_state.py` |
| search_path hardening migration | `.\.venv-cloud\Scripts\python.exe scripts/check_search_path_migration.py`: creates a pre-007 (a7e60d9) test database, migrates twice, then checks that data and history are unchanged, that every function in vc keeps all attributes except the search_path of the functions that set one, and that the functions equal a new installation's |
| Agent boundary adversarial check | `.\.venv-cloud\Scripts\python.exe scripts/check_agent_boundaries.py`: creates `agentdb_test_gpt_*` through `agent_lab.py init`, audits the functions `agentdb_client` can execute, the tables it can reach, the schemas it can create in and the search_path of every function that sets one (all SECURITY DEFINER functions included); tries with `agentdb_client`'s own SQL what the catalog cannot show (disabling the capture trigger, turning off triggers for the session, writing a branch view with no open change), tries to break version history through the granted functions, and passes `agent_lab.py` arguments it must reject (each by the check under test, as an argument error: exit code 2, no Python traceback); finally checks that main is unchanged. A crossed boundary is a GAP and exits 1; takes only `--config` for the connection |
| Staging VM S4/S5 | `.\.venv-cloud\Scripts\python.exe scripts/staging_drills.py growth|backup|restart|crash|netcut|kill --config connection.staging.local.json`: run only against the [staging VM](docs/STAGING_VM.md); restart/crash/netcut power off, restart or disconnect the network through VBoxManage, so never point it at an important database; results are in [S4/S5](docs/STAGING_S4_S5.md) |
| Read-set experiment | Commands for the unit and contract tests, the mutation check, the experiment and the two audits are in "Files and running" in the [design](docs/READSET_EXPERIMENT.md) |
| Real cloud (not run, currently on hold) | `smoke_cloud.py` is kept only as a future integration entry point; it is not run now, and no provider is claimed to have passed verification on its basis |

Real-model experiments follow a pre-registered protocol: record the exact model ID returned by the interface, reasoning effort, runner version, and hashes of the prompts and source files; do not guess an unexposed weight snapshot as a definite version. Fix the sample size and keep failures; do not replace failed rounds with new ones. Save tool request/response JSONL without summarizing or truncating, and save the last value of each Agent's cumulative usage; cached input and reasoning output are sub-items, and missing usage stays unknown. After each actual write, independently check the published content and the other party's branch; an overwrite that is later restored still counts as a failure. Distinguish controlled recovery prompts, scripted stress tests and autonomous model behavior; cloud state is still not verified against real cloud services.

Existing records are in [Real GPT repeat experiment](docs/GPT_REPEAT_EXPERIMENT.md). From the project root, use `python -m unittest discover -s experiments/gpt_repeat -p test_repeat_scoring.py -v` to check counting and overwrite judgments; use `python experiments/gpt_repeat/analyze_repeat_logs.py experiments/gpt_repeat/results/2026-10-17` and `python experiments/gpt_repeat/audit_repeat_logs.py experiments/gpt_repeat/results/2026-10-17` to re-analyze the local evidence. These offline commands do not call models and do not write to the database; the commands to rerun the models and their quota impact are in the experiment report, and they must not be rerun automatically for documentation changes.

Changes involving concurrency/recovery must at least verify: stale-version races, retries of old commands, interruptions before and after publishing, cache invalidation races, pinned-version consistency and context omission markers. When a new format is involved, add a compatibility check against old data; do not substitute running successfully on a newly created empty database for migration verification.

Delivery notes state what was changed, why, the local check results and the remaining limitations. Distinguish in-memory simulation, SDK contracts and the real local PostgreSQL; each cloud provider must be noted as not verified against real cloud services, and any wording that packages local results as cloud success must be removed. Capacity and latency conclusions must state the load and environment. Local records are in [Version engine validation](docs/VALIDATION.md) and [State module local tests](docs/CLOUD_STATE_VALIDATION.md); they cannot be taken as proof that later changes have automatically passed.
