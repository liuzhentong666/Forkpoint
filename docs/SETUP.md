中文 | [English](#english-version)

# 安装、连接与升级

快速开始见 [README](../README.zh-CN.md)。本文说明连接配置、升级已有安装和测试入口。

## 用 Docker 试用

在项目根目录运行 `docker compose up`。[compose.yaml](../compose.yaml) 启动 PostgreSQL 16，用 `db.py` 把 Forkpoint 安装到 `agentdb_demo` 库，再运行 [examples/two_agents.sql](../examples/two_agents.sql)。演示结束后 PostgreSQL 继续运行，可用 `docker compose exec db psql -U postgres -d agentdb_demo` 查看；`docker compose down` 删除容器。

演示只用于本机试用：口令固定，不向主机发布端口，数据保存在内存中，每次启动都从空库开始。需要在脚本或 CI 中判断成败时，运行 `docker compose up --build --exit-code-from demo`，示例结果不符时退出码非零。

## 连接 PostgreSQL

`db.py` 不会初始化、启动、停止或重新配置 PostgreSQL，只连接已有的服务。默认以 `postgres` 用户连接 `127.0.0.1:5432`，维护库为 `postgres`，项目库为 `agentdb`。

需要修改时，把 `connection.example.json` 复制为 `connection.local.json`（已加入忽略列表）。参数优先级：命令行 > `PGHOST` / `PGPORT` / `PGUSER` / `PGDATABASE` / `AGENTDB_PSQL` 环境变量 > 配置文件 > 默认值。

- **Linux / macOS 原生 PostgreSQL**：保持 `wsl_distribution`、`wsl_user` 为空；`psql` 为空时从 `PATH` 查找。
- **Windows 原生 PostgreSQL**：`host` 使用 `127.0.0.1`，`wsl_*` 置空；`psql` 为空时依次查找 `PATH` 和 `C:\Program Files\PostgreSQL\*\bin\psql.exe`。
- **WSL 中的 PostgreSQL**：Windows 上的 Python 通过 `wsl.exe` 调用 WSL 内的 psql，并使用 WSL 内已有的本地认证。作者的开发机使用以下配置（Ubuntu WSL 中的 PostgreSQL 16，集群 `16/main`）：

  ```json
  {
    "host": "/var/run/postgresql",
    "port": 5432,
    "user": "postgres",
    "database": "agentdb",
    "maintenance_database": "postgres",
    "psql": "/usr/bin/psql",
    "wsl_distribution": "Ubuntu",
    "wsl_user": "postgres"
  }
  ```

  直接进入该数据库：

  ```console
  wsl --distribution Ubuntu --user postgres --exec /usr/bin/psql -h /var/run/postgresql -p 5432 -U postgres -d agentdb
  ```

`python db.py check` 连接维护库，输出服务器版本和实际使用的连接信息。

### 远程连接与 TLS

远程（非回环）PostgreSQL 只接受 `sslmode` 为 `verify-full` 且指定 `sslrootcert` 的连接。TLS 设置只取自配置文件或 `--sslmode` / `--sslrootcert`，继承的 `PGSSL*` 环境变量会被清除。预发布接入步骤与专用登录角色见 [预发布 PostgreSQL 接入](STAGING.md)，整体阶段与通过条件见 [生产环境测试计划（草案）](PRODUCTION_TEST_PLAN.md)。

### 密码

需要密码时，在自己的终端运行 `python db.py check --password`，输入会被隐藏且不保存。长期配置可使用 PostgreSQL 的[密码文件](https://www.postgresql.org/docs/current/libpq-pgpass.html)：Windows 原生客户端使用 `%APPDATA%\postgresql\pgpass.conf`，WSL 客户端使用对应 Linux 用户的 `~/.pgpass`。密码不写进 JSON 或命令参数。

### 每次调用一个进程，或复用会话

`Connection.execute()` 每次调用启动一个 psql 进程（经 WSL 时在作者机器上约 0.2–2 s）。需要连续调用的客户端可以改用 `connection.session()`：它保持一个 psql 进程，提供同样的 `execute()` / `sql()`，每次调用约 1–2 ms。

每次调用的行为与单独起一个进程相同：遇到第一个错误即停止（返回码非零，错误写在 stderr）；未结束的事务回滚；服务端会话状态（设置、角色、临时表、咨询锁）和 psql 变量在下一次调用前重置。一个 Session 一次只执行一个调用，每个线程或写入者各用一个。耗时对比见 [合入耗时分解](MERGE_BENCHMARK.md)。

## 安装

```console
python db.py install
```

安装命令先创建配置中的项目库（若尚不存在；可用 `--database` 指定其他库名），再在一个事务中按顺序安装 `sql/` 中的十个文件（001–010）：版本引擎、示例业务表、受限维护角色、批量合入、有界历史遍历与快照策略、只读历史健康检查、search_path 加固、读集合入、与会话设置无关的行文档，以及不存在的分支报 P0002。它会拒绝已有项目 schema 或用户表的目标库。项目库初始没有业务数据。

安装包含示例表 `app.products`、`app.orders`，以及 NOLOGIN 角色 `agentdb_client` 和 `agentdb_manager`。角色是整个 PostgreSQL 实例共享的；已存在时只授予本项目的库内权限，不修改其角色属性。

## 升级已有安装

已有安装按以下顺序升级，每条命令都可重复执行，不改业务记录或版本历史：

| 顺序 | 命令 | 内容 | 核验脚本 |
|---|---|---|---|
| — | `python db.py install-manager` | 新增 `vc.snapshot_status` 与受限维护角色 `agentdb_manager`；常规维护可加 `--role agentdb_manager`，修改系统规则仍由原管理员身份承担；该角色不会自动分配给其他登录用户 | — |
| 004 | `python db.py install-merge-batch` | `vc.merge` 每张表只调用一次 `vc.rows_at`；已安装 005 的库会拒绝执行，以免还原 005 的 `vc.merge` | `scripts/check_merge_migration.py` |
| 005 | `python db.py install-bounded-history` | 历史遍历不再随全部历史长度变慢，新增只读快照策略 `vc.snapshot_due`（见 [历史增长](HISTORY_GROWTH.md)） | `scripts/check_history_migration.py` |
| 006 | `python db.py install-history-health` | 新增只读的 `vc.history_health(branch)`，仅授权给 `agentdb_manager` | `scripts/check_history_health.py` |
| 007 | `python db.py install-search-path-hardening` | `vc` 中所有设置了 search_path 的函数末尾加上 `pg_temp`；用于升级此前的安装，上面各条迁移的定义已自带 `pg_temp`，重跑它们不会撤销 | `scripts/check_search_path_migration.py` |
| 008 | `python db.py install-read-set-merge` | 新增 `vc.merge_validated`：按决定读过的行校验合入，这些行变过则报 RS001 拒绝（写偏斜），源分支写过的行也按读校验（2026-10-22 起；已安装的库重跑本条即更新）；读键先改写为历史记录的形式（如 `"3204"` 按整数 3204），无法转换的键报 22023，未知的源版本或分支报 P0002；读集代替 head 检查，空读集表示决定没有读任何行；不改动已有函数；回滚方法见该文件开头 | `run_tests.py`（读集合入的一组测试） |
| 009 | `python db.py install-canonical-row-docs` | 历史中的行文档与主键不再随写入会话的设置变化：捕获触发器与 `vc.merge_validated` 以固定的 TimeZone（UTC）、IntervalStyle、extra_float_digits、bytea_output 生成行文档；已存历史中有按其他设置写下的行时整体中止、不做改动，须先修复那段历史；执行时以 SHARE 锁等待进行中的版本化事务结束，期间新的版本化写入等它完成 | `run_tests.py`（`row_documents_do_not_depend_on_session_settings`）、`scripts/check_search_path_migration.py` |
| 010 | `python db.py install-unknown-branch-code` | `vc.begin_change` 遇到不存在的分支报 P0002（no_data_found），不再是与合入冲突相同的 P0001，`agent_lab.py` 据此把这种提案报为 JSON `not_found`；其余不变（签名、函数体其余部分、属主、授权、search_path） | `run_tests.py`（`begin_change_on_a_missing_branch_is_not_found`）、`scripts/check_search_path_migration.py` |

核验脚本都在新建的旧版测试库上执行迁移并逐项比较，不触碰项目库。

## 运行测试

```console
python run_tests.py
```

测试每次新建 `agentdb_test_<随机后缀>` 专用数据库，在其中安装 SQL、生成 4,000 行，执行集成测试和 10 连接并发验证，不写入项目库。结果保存到 `test-results-local.json`（不纳入版本库），测试库保留供检查。连接账户需要创建数据库、安装对象以及切换到 `agentdb_client` 角色的权限。

状态服务与存储适配器的单元测试需要先安装 `requirements-storage-tests.txt`：

```console
python -m unittest discover -s tests -v
```

基准测试、迁移检查和预发布演练在 `scripts/` 中，原始结果在 `results/` 中。按改动范围应运行哪些检查，见 [AGENTS.md](../AGENTS.md) 的“验证入口与交付要求”。

## 管理 Agent 状态服务（云端，未经验证）

> **云服务状态：未经真实验证。** 阿里云 OSS、GCS、S3 均未完成真实云端读写或联调，ECS 状态服务未部署，云端部署暂缓。本地测试结果不能作为云存储已验证或部署可用的依据。

设计目标是把管理任务的最新状态、事件、规则版本与证据保存到配置选择的云端对象存储（GCS / 阿里云 OSS / S3），常驻进程只保留有界内存缓存。适配代码已编写，任务入口、条件发布、命令去重、分页历史和带预算的上下文 API 目前只有本地实现与测试。PostgreSQL 继续负责代码与业务数据的版本，管理状态不写入 PostgreSQL。

OSS 适配器每个任务最多发布 524,288 次，达到后需显式建立后继任务；这不是实测的云端容量，也还没有透明的日志轮转。

- [多后端设计](STORAGE_BACKENDS.md)：OSS 条件追加发布、GCS/S3 条件写入、兼容与容量边界
- [原始设计](CLOUD_STATE_DESIGN.md)：有界状态、幂等命令与跨系统恢复
- [运行与 API](CLOUD_STATE_RUNBOOK.md)：GCS 配置、常驻服务、上下文读取和 PostgreSQL 维护示例
- [管理 Agent 协议](MANAGER_AGENT_PROTOCOL.md)：可固定为云端规则对象的 superuser Agent 操作约定
- [多后端本地测试](STORAGE_BACKENDS_VALIDATION.md)：内存服务模型和 SDK 测试替身的结果，不含真实云存储
- [ECS 部署草案（暂缓）](ALIYUN_ECS_DEPLOYMENT.md)：保留供以后参考；[部署脚本本地检查](ALIYUN_ECS_VALIDATION.md)不代表已部署或已完成云端验证

---

<a id="english-version"></a>

# Setup, Connections and Upgrades

For the quick start see the [README](../README.md). This document covers connection settings, upgrading an existing install, and the test entry points.

## Trying it with Docker

Run `docker compose up` in the project root. [compose.yaml](../compose.yaml) starts PostgreSQL 16, installs Forkpoint into the `agentdb_demo` database with `db.py`, then runs [examples/two_agents.sql](../examples/two_agents.sql). PostgreSQL keeps running after the demo; look around with `docker compose exec db psql -U postgres -d agentdb_demo`, and remove it with `docker compose down`.

The demo is for trying Forkpoint on your own machine only: the password is fixed, no port is published to the host, and the data lives in memory, so every start begins from an empty server. To get a pass/fail result in a script or CI, run `docker compose up --build --exit-code-from demo`; it exits non-zero if the example's outcome is wrong.

## Connecting to PostgreSQL

`db.py` never initializes, starts, stops or reconfigures PostgreSQL; it only connects to a server that is already running. By default it connects to `127.0.0.1:5432` as `postgres`, with `postgres` as the maintenance database and `agentdb` as the project database.

To change that, copy `connection.example.json` to `connection.local.json` (it is ignored by git). Precedence: command line > the `PGHOST` / `PGPORT` / `PGUSER` / `PGDATABASE` / `AGENTDB_PSQL` environment variables > the config file > defaults.

- **Native PostgreSQL on Linux / macOS**: leave `wsl_distribution` and `wsl_user` empty; an empty `psql` means it is looked up on `PATH`.
- **Native PostgreSQL on Windows**: use `127.0.0.1` as `host` and leave the `wsl_*` settings empty; an empty `psql` is looked up on `PATH`, then in `C:\Program Files\PostgreSQL\*\bin\psql.exe`.
- **PostgreSQL inside WSL**: Python on Windows calls the psql inside WSL through `wsl.exe` and uses WSL's existing local authentication. The author's development machine uses this configuration (PostgreSQL 16 in Ubuntu WSL, cluster `16/main`):

  ```json
  {
    "host": "/var/run/postgresql",
    "port": 5432,
    "user": "postgres",
    "database": "agentdb",
    "maintenance_database": "postgres",
    "psql": "/usr/bin/psql",
    "wsl_distribution": "Ubuntu",
    "wsl_user": "postgres"
  }
  ```

  To open that database directly:

  ```console
  wsl --distribution Ubuntu --user postgres --exec /usr/bin/psql -h /var/run/postgresql -p 5432 -U postgres -d agentdb
  ```

`python db.py check` connects to the maintenance database and prints the server version and the connection details actually used.

### Remote connections and TLS

A remote (non-loopback) PostgreSQL is only accepted with `sslmode` set to `verify-full` and `sslrootcert` given. TLS settings come only from the config file or `--sslmode` / `--sslrootcert`; inherited `PGSSL*` environment variables are cleared. For the staging connection steps and the dedicated login role see [Staging PostgreSQL connection](STAGING.md); for the overall phases and pass criteria see the [production test plan (draft)](PRODUCTION_TEST_PLAN.md).

### Passwords

When a password is needed, run `python db.py check --password` in your own terminal; the input is hidden and not saved. For a lasting setup use PostgreSQL's [password file](https://www.postgresql.org/docs/current/libpq-pgpass.html): `%APPDATA%\postgresql\pgpass.conf` for the native Windows client, or the matching Linux user's `~/.pgpass` for the WSL client. Passwords are never written into JSON or command arguments.

### One process per call, or a reused session

Each `Connection.execute()` call starts a psql process (about 0.2–2 s on the author's machine when going through WSL). Clients that make many calls can use `connection.session()` instead: it keeps one psql process and offers the same `execute()` / `sql()`, at about 1–2 ms per call.

Each call behaves the same as a separate process: it stops at the first error (non-zero return code, error on stderr); an unfinished transaction is rolled back; server-side session state (settings, role, temporary tables, advisory locks) and psql variables are reset before the next call. A Session runs one call at a time; use one per thread or writer. For the timing comparison see the [merge timing breakdown](MERGE_BENCHMARK.md).

## Installing

```console
python db.py install
```

The install command first creates the configured project database if it doesn't exist yet (use `--database` for another name), then installs the ten files in `sql/` (001–010) in order in one transaction: the version engine, the demo business tables, the restricted maintenance role, batched merge, bounded history traversal with the snapshot policy, the read-only history health check, the search_path hardening, read-set merges, row documents independent of session settings, and P0002 for a missing branch. It refuses a target database that already has project schemas or user tables. The project database starts with no business data.

The install includes the demo tables `app.products` and `app.orders` and the NOLOGIN roles `agentdb_client` and `agentdb_manager`. Roles are shared by the whole PostgreSQL instance; if one already exists, only this project's in-database privileges are granted and its role attributes are left alone.

## Upgrading an existing install

Upgrade an existing install in this order. Each command can be rerun and changes no business records or version history:

| Order | Command | What it adds | Check script |
|---|---|---|---|
| — | `python db.py install-manager` | `vc.snapshot_status` and the restricted maintenance role `agentdb_manager`; routine maintenance can add `--role agentdb_manager`, while changing system rules stays with the original administrator; the role is not granted to other login users automatically | — |
| 004 | `python db.py install-merge-batch` | `vc.merge` calls `vc.rows_at` once per table; refuses to run on a database that already has 005, so 005's `vc.merge` is not reverted | `scripts/check_merge_migration.py` |
| 005 | `python db.py install-bounded-history` | History walks no longer slow down with total history length, plus the read-only snapshot policy `vc.snapshot_due` (see [History growth](HISTORY_GROWTH.md)) | `scripts/check_history_migration.py` |
| 006 | `python db.py install-history-health` | The read-only `vc.history_health(branch)`, granted only to `agentdb_manager` | `scripts/check_history_health.py` |
| 007 | `python db.py install-search-path-hardening` | `pg_temp` at the end of the search_path of every function in `vc` that sets one; upgrades earlier installations, and the definitions in the migrations above already include `pg_temp`, so rerunning them does not undo it | `scripts/check_search_path_migration.py` |
| 008 | `python db.py install-read-set-merge` | `vc.merge_validated`: a merge checked against the rows its decision read, refused with RS001 when they changed (write skew), with the rows the source wrote checked as read too (since 2026-10-22; rerun it on an existing install to update); read keys are first rewritten to the form history records (`"3204"` as the integer 3204), a key that cannot be converted raises 22023, and an unknown source revision or branch raises P0002; the read set replaces the head check, and an empty one declares that the decision read nothing; no existing function changes; the rollback is at the top of the file | `run_tests.py` (its read-set merge tests) |
| 009 | `python db.py install-canonical-row-docs` | row documents and primary keys in history no longer depend on the settings of the session that wrote them: the capture triggers and `vc.merge_validated` render rows with fixed TimeZone (UTC), IntervalStyle, extra_float_digits and bytea_output; stops without changing anything if stored history holds rows written under other settings, which then has to be repaired first; while it runs, SHARE locks make it wait for open versioned transactions to finish, and new versioned writes wait for it | `run_tests.py` (`row_documents_do_not_depend_on_session_settings`), `scripts/check_search_path_migration.py` |
| 010 | `python db.py install-unknown-branch-code` | `vc.begin_change` reports a branch that does not exist as P0002 (no_data_found) instead of P0001, which is also a merge conflict, so `agent_lab.py` reports such a proposal as JSON `not_found`; nothing else changes (signature, the rest of the body, owner, grants, search_path) | `run_tests.py` (`begin_change_on_a_missing_branch_is_not_found`), `scripts/check_search_path_migration.py` |

Each check script migrates a freshly created old-version test database and compares the results item by item; none of them touches the project database.

## Running the tests

```console
python run_tests.py
```

Each run creates a dedicated `agentdb_test_<random suffix>` database, installs the SQL, generates 4,000 rows, and runs the integration tests and a 10-connection concurrency check; it never writes to the project database. Results go to `test-results-local.json` (not committed), and the test database is kept for inspection. The connecting account needs permission to create databases, install objects and switch to the `agentdb_client` role.

The unit tests for the state service and storage adapters need `requirements-storage-tests.txt` installed first:

```console
python -m unittest discover -s tests -v
```

Benchmarks, migration checks and staging drills are in `scripts/`, and their raw results in `results/`. Which checks to run for which kind of change is listed in [AGENTS.md](../AGENTS.md) under "Verification entry points and delivery requirements".

## Manager Agent state service (cloud, not verified)

> **Cloud service status: not verified against real cloud services.** None of Alibaba Cloud OSS, GCS or S3 has completed real cloud reads/writes or integration testing, the ECS state service has not been deployed, and cloud deployment is on hold. Local test results are not evidence that cloud storage works or that a deployment is usable.

The design keeps the latest state, events, rule versions and evidence of management tasks in the cloud object storage chosen by configuration (GCS / Alibaba Cloud OSS / S3), with the long-running process holding only a bounded in-memory cache. The adapter code is written; the task entry point, conditional publishing, command deduplication, paginated history and the budgeted context API have only local implementations and tests so far. PostgreSQL keeps versioning code and business data; management state is not written to PostgreSQL.

The OSS adapter allows at most 524,288 publishes per task, after which a successor task must be created explicitly. This is not a measured cloud capacity, and there is no transparent log rotation yet.

- [Multi-backend design](STORAGE_BACKENDS.md): OSS conditional-append publishing, GCS/S3 conditional writes, compatibility and capacity limits
- [Original design](CLOUD_STATE_DESIGN.md): bounded state, idempotent commands and cross-system recovery
- [Operation and API](CLOUD_STATE_RUNBOOK.md): GCS configuration, the long-running service, context reads and PostgreSQL maintenance examples
- [Manager Agent protocol](MANAGER_AGENT_PROTOCOL.md): operating conventions for the superuser Agent, which can be pinned as a cloud rule object
- [Multi-backend local tests](STORAGE_BACKENDS_VALIDATION.md): results from the in-memory service model and SDK test doubles, with no real cloud storage
- [ECS deployment draft (on hold)](ALIYUN_ECS_DEPLOYMENT.md): kept for later reference; the [local check of the deployment scripts](ALIYUN_ECS_VALIDATION.md) does not mean anything was deployed or verified in the cloud
