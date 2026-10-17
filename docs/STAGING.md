# 预发布 PostgreSQL 接入（S1）

2026-10-18。[生产环境测试计划](PRODUCTION_TEST_PLAN.md) 第 S1 阶段的接入说明。用户已选择本机虚拟机作为预发布主机，逐步操作见 [本机虚拟机步骤](STAGING_VM.md)；本文是与主机形态无关的通用规则。**尚无预发布主机**：下文的代码与脚本已在本机核验到能核验的程度（见末节），真正的 S1 结果要在用户提供主机后才能产生。云服务仍未经真实验证，本阶段不涉及对象存储。

## 连接规则

`db.py` 对主机分三种情况处理：

| 主机 | 要求 |
|---|---|
| `127.0.0.1` / `localhost` / `::1` | 与原来相同；可选配置 `sslmode` |
| `/` 开头的 Unix socket 目录 | 原生 Linux，或 Windows 上的 WSL 传输 |
| 其他（远程） | 必须是单个主机名或 IP，且 `sslmode` 为 `verify-full`、`sslrootcert` 指向签发服务器证书的 CA 文件；否则拒绝连接 |

`verify-full` 同时校验证书链和主机名，因此配置的主机名必须出现在服务器证书的 CN 或 SAN 中。TLS 设置只取自配置文件或命令行（`--sslmode`、`--sslrootcert`）；继承来的 `PGSSLMODE`、`PGSSLROOTCERT`、`PGREQUIRESSL`、`PGSSLNEGOTIATION` 一律清除，避免 shell 环境悄悄降低校验级别。`PGHOST` 指向远程主机时同样受上述规则约束。使用 WSL 传输时，`sslrootcert` 与 `psql` 一样是发行版内部的路径。

`python db.py check` 的输出新增 `ssl`、`ssl_version` 和 `sslmode`；远程连接若未使用 TLS 会直接报错（`verify-full` 下本不会发生）。

## 准备步骤

以下由预发布服务器的管理员执行，本项目脚本不会修改服务器配置。

1. **服务器证书**：由 CA 签发、主机名写入 SAN 的证书；`postgresql.conf` 中 `ssl = on`，`ssl_min_protocol_version = 'TLSv1.2'` 或更高，并让 `listen_addresses` 包含客户端可达的地址。把 CA 证书（不是私钥）交给客户端。
2. **pg_hba.conf**：只为专用角色开放 TLS 连接，网段按实际客户端填写：

   ```text
   hostssl  all  agentdb_staging  10.0.0.0/24  scram-sha-256
   ```

   数据库一栏为 `all`，因为各套件每次都会新建 `agentdb_test_*` 库。
3. **专用角色**：以超级用户执行 [create_staging_role.sql](../deploy/staging/create_staging_role.sql)，再在 psql 中交互设置口令：

   ```console
   psql -X -v ON_ERROR_STOP=1 -d postgres -f deploy/staging/create_staging_role.sql
   psql -X -d postgres -c "\password agentdb_staging"
   ```

   `agentdb_staging` 不是超级用户，不能建角色，只有 `CREATEDB`；对 `agentdb_client`、`agentdb_manager` 只有 SET 权限、不继承其权限。脚本会预先建立这两个 NOLOGIN 角色，因为 `install` 只在它们不存在时才建，而建角色需要 CREATEROLE。脚本可重复执行。
4. **客户端口令**：写入客户端的 PostgreSQL 密码文件（Linux `~/.pgpass`，权限 0600；Windows 原生客户端 `%APPDATA%\postgresql\pgpass.conf`），或每次运行时加 `--password` 隐藏输入。不要写进 JSON、命令参数或聊天。
5. **客户端配置**：复制 [connection.staging.example.json](../connection.staging.example.json) 为 `connection.staging.local.json`（已被 `.gitignore` 忽略），填写主机名与 CA 文件路径。

## S1 核验命令

```console
python db.py check --config connection.staging.local.json
python run_tests.py --config connection.staging.local.json
.\.venv-cloud\Scripts\python.exe review_local.py --config connection.staging.local.json
python check_merge_migration.py --config connection.staging.local.json
```

通过条件见计划的 S1 一行：`check` 显示 `"ssl": true`、`"sslmode": "verify-full"`；三个套件以 `agentdb_staging` 身份全部通过。结果文件注明环境为“预发布 PostgreSQL”，与本机记录分开保存。各套件保留的测试库由用户决定何时清理。

## 本机已核验的部分（2026-10-18）

本机没有预发布主机，以下检查只能证明代码路径与脚本本身，不能代替 S1：

- **TLS 路径（真实 WSL PostgreSQL 16.15，经 TCP 127.0.0.1）**：服务器使用 Ubuntu 自签名证书 `LAPTOP-D0IU4SUK.localdomain`，TCP 登录需要口令，本次不设置、不索取口令。以该证书为 CA、`verify-ca` 时，TLS 握手与证书校验通过，连接停在认证步骤（`fe_sendauth: no password supplied`）；换成无关 CA 时报 `certificate verify failed`；`verify-full` 连 `127.0.0.1` 时报证书主机名不匹配。远程主机名不带 TLS 设置时由 `db.py` 直接拒绝。默认的 Unix socket 连接不变。`verify-full` 的成功路径需要一个按证书主机名监听的服务器，本机服务只监听 `localhost`，因此未覆盖。
- **设置检查**：[tests/test_db_connection.py](../tests/test_db_connection.py) 共 10 项，不需要数据库：远程主机必须 `verify-full`、验证模式必须有 CA 文件、拒绝多主机列表与连接串、清除继承的 TLS 变量、配置文件中的 TLS 字段、WSL 传输把 TLS 变量传进发行版。
- **角色脚本**：在本机集群中把脚本连续执行两次后检查角色属性与成员关系（非超级用户、仅 CREATEDB、两个角色均为 SET 而非继承），确认其不能建角色、可以 SET ROLE 到两个角色，然后整体 ROLLBACK；回滚后集群中不存在 `agentdb_staging`。
- **非超级用户安装**：在新建的专用库 `agentdb_test_staginginstall_7cac3bcb` 中，同一事务内执行角色脚本、授予该库 CREATE 权限、SET ROLE 为 `agentdb_staging` 后安装 001–004 全部 SQL：三个 schema 与 `vc.merge` 均归该角色所有，`agentdb_client` 可读取 head；随后 ROLLBACK，角色与对象均未保留，空库保留供检查。

- **虚拟机脚本（WSL 中，不涉及任何 PostgreSQL）**：两个脚本语法检查通过；[make_certs.sh](../deploy/staging/make_certs.sh) 生成的服务器证书链校验通过，SAN 为 `DNS:agentdb-staging.test, IP:192.168.56.10`，仅限服务器认证，私钥 0600，且拒绝覆盖已有目录和非法 IP/主机名。用 `openssl s_server` / `s_client` 做真实 TLS 握手：正确 CA 配 IP 或主机名均校验通过，错误 IP 报地址不匹配，错误 CA 报无法找到签发者，与 `verify-full` 的判定一致。[configure_postgres.sh](../deploy/staging/configure_postgres.sh) 只检查了参数与“必须以 root 运行”的前置拒绝，以及它使用的公钥比对和 `-checkip` 判断；它会修改服务器配置，未在现有 WSL PostgreSQL 上执行。

- **Windows 原生 psql 客户端（2026-10-18）**：经 winget 安装 EDB 的 PostgreSQL 16.15 安装包，禁用 server、pgAdmin、Stack Builder 组件，安装器哈希校验通过。`bin` 中仍带有 `postgres.exe` 程序文件，但没有数据目录、没有 Windows 服务、Windows 侧没有进程监听 5432，因此没有运行中的服务器。经 `db.py`（`wsl_distribution` 留空）用 Windows psql 连本机 WSL 服务器（mirrored 模式下的 `127.0.0.1:5432`），结果与 WSL 客户端一致：正确 CA 配 `verify-ca` 到达认证步骤（`fe_sendauth: no password supplied`）；无关 CA 报 `certificate verify failed`；`verify-full` 连 `127.0.0.1` 报主机名不匹配。用 `connection.staging.local.json` 连 `192.168.56.10` 时，`db.py` 接受该远程配置并把连接交给 psql，因尚无虚拟机在 5 秒连接超时后报 `timeout expired`。Windows 的邻居表显示该地址的 ARP 请求发自 VirtualBox 仅主机网卡（状态 Incomplete，即暂无主机应答），说明 Windows 侧路由正确。（`Find-NetRoute` 报告的是经 WLAN 的默认路由，与实际发包不一致，不以其为准。）

尚未覆盖：以 `agentdb_staging` 登录后完整运行三个套件（需要持久角色与口令）、远程网络与真实 CA 签发的证书。这些属于 S1 本身。
