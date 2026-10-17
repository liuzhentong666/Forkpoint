# AgentDB PostgreSQL：代码与业务表的联合版本管理

> **云服务状态（2026-10-17）：未经真实验证。** 阿里云 OSS、GCS、S3 均未完成真实云端读写或联调，ECS 状态服务未部署。云端部署暂缓，当前只开展本地开发和模拟测试；本地测试结果不能作为云存储已验证或部署可用的依据。

目标规模：第一阶段单张业务表约 **4,000 行、10 个 Agent 并发写入**。

这个方向采用你提供的 PostgreSQL 计划：代码对象、changeset 和业务记录历史保存在同一个 PostgreSQL 数据库中，普通 main 表与分支视图共存。核心逻辑由 SQL / PL/pgSQL 实现，没有 SQLite 依赖。

它是构建面向 Agent 的数据库系统的第一阶段：先验证版本、隔离、事务与合入语义。PostgreSQL 负责事务、恢复和原生约束；本项目实现代码与数据的版本模型。当前仍是单节点实现，不声称已经替代分布式 Spanner。

技术博客：[让 Agent 的工作可以提交、分支和接手：AgentDB 的第一阶段实现](docs/TECHNICAL_BLOG.md)，介绍当前架构、关键实现、验证结果与下一阶段问题。

本机审查补充：[维护恢复、受限角色与多 Agent 实验](docs/LOCAL_REVIEW_RESULTS.md)。包括旧维护者的发布冲突、两个中断窗口、非默认前缀配置路径，以及十个分支全部合入。所有云存储仍未经真实验证。

真实 GPT 重复实验：[十轮结果、逐 Agent 用量与原始 JSONL](docs/GPT_REPEAT_EXPERIMENT.md)。本组记录 10 轮、完成 8 轮、成功 8 轮，保留所有失败样本；原始日志在本机项目的 `experiments/gpt_repeat/results/2026-10-17/`，不随源码自动提交。

现有安装使用新版快照维护器前，先由数据库所有者执行 `python db.py install-manager`，新增 `vc.snapshot_status` 与 `agentdb_manager`，不改业务记录或版本历史。常规维护可加 `--role agentdb_manager`，只使用快照检查/物化权限；修改系统规则仍由原管理员身份承担。该 NOLOGIN 角色不自动分配给其他登录用户。

`vc.merge` 现在每张表只调用一次 `vc.rows_at` 解析全部改动行的目标状态，冲突规则与历史不变（见 [合入耗时分解](docs/MERGE_BENCHMARK.md)）。新安装已包含；现有安装由数据库所有者执行 `python db.py install-merge-batch`（`sql/004_merge_batch.sql`，可重复执行，只替换函数，保留授权，不改业务记录或版本历史）。`python check_merge_migration.py` 在新建的旧版测试库上核验这一迁移。

## 可替换云存储的管理 Agent 状态服务

设计目标是将管理任务的最新状态、事件、规则版本与证据保存到配置选择的云端对象存储（GCS / 阿里云 OSS / S3），常驻进程只保留有界内存缓存。相关适配代码已编写，但尚无真实云服务运行结果。任务入口、条件发布、命令去重、分页历史和带预算的上下文 API 目前仅有本地实现与测试。

- [多后端设计](docs/STORAGE_BACKENDS.md)：OSS 条件追加发布、GCS/S3 条件写入、兼容与容量边界。
- [原始设计](docs/CLOUD_STATE_DESIGN.md)：有界状态、幂等命令与跨系统恢复。
- [运行与 API](docs/CLOUD_STATE_RUNBOOK.md)：GCS 配置、常驻服务、上下文读取和 PostgreSQL 维护示例。
- [管理 Agent 协议](docs/MANAGER_AGENT_PROTOCOL.md)：可固定为云端规则对象的 superuser Agent 操作约定。
- [多后端本地测试](docs/STORAGE_BACKENDS_VALIDATION.md)：内存服务模型和 SDK 测试替身的结果，不包含真实云存储。
- [ECS 部署草案（暂缓）](docs/ALIYUN_ECS_DEPLOYMENT.md)：保留配置与脚本供以后参考；[部署脚本本地检查](docs/ALIYUN_ECS_VALIDATION.md)不代表已部署或已完成云端验证。

用户已报告创建杭州 OSS 桶 `agentdb-dev-goudan`，但本项目未核验该桶的配置、权限和真实读写；也没有 ECS。当前不安排云资源采购、云端联调或部署。保留的部署草案不绑定自定义域名。OSS 适配器代码设置每任务 524,288 次发布的上限，达到后需显式建立后继任务；这不是实测云端容量，且尚无透明日志轮转。PostgreSQL 继续负责代码与业务数据版本，管理状态不写入 ops schema。各后端依赖文件与 ECS 配置保留供后续开发使用。

## 当前工作流程

```text
Agent 选择固定版本
  → 创建分支（新增指针，不复制业务表）
  → BEGIN + begin_change(branch, expected_head)
  → SQL 修改分支视图 + write_file 修改代码
  → finish_change() + COMMIT
  → 比较差异
  → merge(main, source_revision, expected_head)
  → 冲突检查 + PostgreSQL 原生约束检查
  → 一个事务发布代码、历史和 main 业务表
```

- `app.products` / `app.orders`：真正的 PostgreSQL 业务表，代表 main。
- `agent.products` / `agent.orders`：按选定版本读取的视图，分支写入由 `INSTEAD OF` 触发器捕获。
- `vc.*`：历史、版本指针、不可变内容、暂存修改和可重建快照。

创建分支不会复制业务行或代码内容；仍会写入一条分支记录，并有索引/事务开销。“O(1)”只适合描述不随数据行数增长的逻辑复制工作，不能当成严格延迟保证。

## 使用现有的本地 PostgreSQL

当前连接已配置为 **Ubuntu WSL 中现有的 PostgreSQL 16.15**：

| 配置 | 当前值 |
|---|---|
| WSL 发行版 | Ubuntu |
| 已有集群 | 16/main |
| 客户端 | Ubuntu 的 `/usr/bin/psql` |
| 连接 | Unix socket `/var/run/postgresql`，端口 5432 |
| 本地系统账户 / 数据库账户 | postgres / postgres |
| 持久项目数据库 | agentdb |

Windows Python 通过 WSL 调用已经安装的 psql，并使用现有的本地认证设置。连接参数在 `connection.local.json`，该文件已加入忽略列表。项目脚本不会初始化、启动、停止或重新配置 PostgreSQL；旧 `.runtime` 目录不再被使用。

`Connection.execute()` 每次调用都启动一个 `wsl.exe` + psql 进程，本机约需 0.2–2 s。需要连续调用的客户端可改用 `connection.session()`：它保持一个 psql 进程，提供同样的 `execute()` / `sql()`，本机每次调用约 1–2 ms。每次调用的行为与单独起一个进程相同：遇到第一个错误即停止（返回码非零，错误写在 stderr）；未结束的事务回滚；服务端会话状态（设置、角色、临时表、咨询锁）和 psql 变量在下一次调用前重置。一个 Session 一次只执行一个调用，每个线程或写入者各用一个。合入耗时对比见 [合入耗时分解](docs/MERGE_BENCHMARK.md)。

需要 Python 3.10+，不需要第三方 Python 包。在本项目目录执行：

```console
python db.py check
```

`check` 连接维护库，输出真实版本和连接信息。当前 `agentdb` 项目库已安装；在另一套环境首次安装时执行：

```console
python db.py install
```

安装命令先创建配置中的项目库（若尚不存在），再在一个事务中安装三个 SQL 文件（版本引擎、示例业务表、受限维护角色）。它会拒绝已有项目 schema 或用户表的目标库。项目库初始为空业务数据，不导入测试用的 4,000 条商品。

安装包含 `app.products`、`app.orders` 示例表和 `agentdb_client` NOLOGIN 角色。角色是整个 PostgreSQL 实例共享的；若已存在，只授予本项目的库内权限，不修改其角色属性。当前通过本机 postgres 管理账户安装和测试；Agent 的权限示例仍使用 `agentdb_client`。

进入项目数据库：

```console
wsl --distribution Ubuntu --user postgres --exec /usr/bin/psql -h /var/run/postgresql -p 5432 -U postgres -d agentdb
```

### 更换本机连接配置

新环境可将 `connection.example.json` 复制为 `connection.local.json`。Windows 原生 PostgreSQL 使用 `127.0.0.1`，将 `wsl_distribution`、`wsl_user` 置空，并填写已安装的 psql 路径或将其加入 PATH。原生 Linux 也可直接使用本地 psql。

参数优先级：命令行 > `PGHOST` / `PGPORT` / `PGUSER` / `PGDATABASE` / `AGENTDB_PSQL` 环境变量 > 配置文件 > 默认值。

远程（非回环）PostgreSQL 只接受 `sslmode` 为 `verify-full` 且指定 `sslrootcert` 的连接；TLS 设置只取自配置文件或 `--sslmode` / `--sslrootcert`，继承的 `PGSSL*` 环境变量会被清除。预发布接入步骤与专用登录角色见 [预发布 PostgreSQL 接入](docs/STAGING.md)，整体阶段与通过条件见 [生产环境测试计划（草案）](docs/PRODUCTION_TEST_PLAN.md)。

需要密码时，可在自己终端运行 `python db.py check --password`，隐藏输入且不保存。长期配置也支持 PostgreSQL 的 [密码文件](https://www.postgresql.org/docs/current/libpq-pgpass.html)：Windows 原生客户端使用 `%APPDATA%\postgresql\pgpass.conf`；WSL 客户端使用对应 Linux 用户的 `~/.pgpass`。密码不写进 JSON 或命令参数。

## 运行集成测试

```console
python run_tests.py
```

测试使用同一个现有 PostgreSQL 服务，每次新建 `agentdb_test_<随机后缀>` 专用数据库，在其中安装 SQL、生成 4,000 行并执行集成测试和 10 连接并发验证。测试不写入持久项目库 `agentdb`。

结果保存到 `test-results-local.json`。测试库保留供检查，服务继续运行。连接账户需要创建数据库、安装对象及切换 `agentdb_client` 角色的权限；当前本机 postgres 账户满足这些条件。

## SQL 使用示例

以下使用 psql 的 `\gset` 保存查询结果。每个代码块对应一个完整事务或操作。

初始化 main 的代码和业务数据：

```sql
BEGIN;
SELECT vc.begin_change('main', vc.head(), 'developer', 'initial data');
INSERT INTO app.products VALUES (1, 'book-1', 'Book', 100, 10);
SELECT vc.write_file('src/pricing.py', convert_to('DISCOUNT = 0', 'UTF8'));
SELECT vc.finish_change() AS initial_revision \gset
COMMIT;
```

创建隔离分支并联合修改代码与数据：

```sql
SELECT vc.fork('agent-pricing', :'initial_revision');

BEGIN;
SELECT vc.begin_change('agent-pricing', :'initial_revision', 'agent-pricing', 'pricing experiment');
UPDATE agent.products SET price = 90 WHERE id = 1;
SELECT vc.write_file('src/pricing.py', convert_to('DISCOUNT = 10', 'UTF8'));
SELECT vc.finish_change() AS proposed_revision \gset
COMMIT;
```

读取固定版本：

```sql
BEGIN;
SELECT vc.pin(:'initial_revision');
SELECT * FROM agent.products WHERE id = 1; -- 仍然是 100
COMMIT;

SELECT * FROM vc.diff_rows(:'initial_revision', :'proposed_revision', vc.table_id('app.products'));
SELECT * FROM vc.diff_files(:'initial_revision', :'proposed_revision');
SELECT * FROM vc.history(:'proposed_revision');
```

合入 main：

```sql
BEGIN;
SELECT vc.merge('main', :'proposed_revision', :'initial_revision', 'reviewed-agent');
COMMIT;
```

如果 main 已经前进，`expected_head` 检查会失败。调用方应重新读取并决定是否重新比较/合入，不能无条件覆盖。

`finish_change()` 只在当前事务内建立完整版本；对其他连接的可见性与业务数据一样，等到真正 COMMIT 才发生。忘记调用它时，延迟约束触发器会阻止 COMMIT。

## 已实现的关键保护

- 普通 main 表的 DML 必须处于版本化写事务中；未经登记的写入被拒绝。
- 同一行多次更新只保留最初 `before` 与最终 `after`；插入后删除会抵消。
- 分支读取固定基线；main 继续更新不会改变旧分支。
- 分支增量覆盖基线，包含显式删除标记和复合主键支持。
- 代码与行变化在同一事务、同一个 changeset 下发布。
- main 合入保留第一父提交和来源提交，按整文件/整行检测冲突。
- 合入 main 时执行普通 SQL，由真实 UNIQUE、FK、CHECK、NOT NULL 约束检查结果。
- 普通客户端不能直接写历史表，也不能向已发布的 changeset 追加行。
- 已发布历史不允许 UPDATE、DELETE、TRUNCATE；数据库所有者/超级用户仍是可信管理员。
- 快照缓存以 `(提交 ID, 逻辑表 ID)` 标识，不以不断变化的分支名标识。

## 当前限制

1. **固定 schema**：尚未实现 DDL 分支、迁移合并、历史 schema 重建或 event trigger 捕获。
2. **整行冲突**：不同列的并发修改也会报告同一行冲突，尚未自动逐列合并。
3. **业务规则**：原生约束能检查其覆盖的规则；例如“订单总量不超过库存”的跨行规则仍需专门实现，不能由 diff 自动推断。
4. **分支允许部分无效状态**：除主键/类型检查外，分支可能暂时违反唯一键、外键等约束，合入 main 才由真实表验证。
5. **读取成本**：分支视图可能解析/扫描整张逻辑表；逻辑快照不等于普通表的索引查询性能。`materialize` 只减少历史回放，不让 JSON 视图自动获得原表索引。
6. **合入成本**：当前逐行取目标状态、枚举路径并遍历祖先，需进一步优化；不适合直接推广到大规模历史或大型合并。
7. **兼容范围**：注册空的普通表，先版本化导入；不支持 generated/identity 列、任意扩展类型、其他业务触发器的普遍兼容性或现有数据库无停机接入。
8. **同一分支写入串行**：分支头行锁保证一致性；不同分支可并发，但全部合入同一个 main 仍会争用。
9. **本地单租户实验**：没有完整身份认证、租户隔离、资源配额、审计身份绑定、版本保留/回收、跨节点复制与灾难恢复方案。
10. **测试数据发布边界**：本示例可将分支数据合入本地 main；真实开发中，不应把实验产生的订单直接合入生产，应发布经验证的迁移方案。

设计取舍和对原计划的修订见 [DESIGN_REVIEW.md](DESIGN_REVIEW.md)。
