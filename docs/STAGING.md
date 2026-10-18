中文 | [English](#english-version)

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
6. **失联客户端的超时**（服务器）：PostgreSQL 默认要约 2 小时 11 分钟才发现事务中失联的客户端，期间它持有的锁（例如 main 的分支锁）一直有效。为专用角色设置 `ALTER ROLE agentdb_staging SET idle_in_transaction_session_timeout = '60s'`（[角色脚本](../deploy/staging/create_staging_role.sql) 已包含；只作用于该角色，管理员会话保持默认），并在 `postgresql.conf` 或其 `conf.d` 中设置：

   ```text
   tcp_keepalives_idle = '60s'
   tcp_keepalives_interval = '10s'
   tcp_keepalives_count = 6
   tcp_user_timeout = '120s'
   client_connection_check_interval = '10s'
   ```

   这样事务内空闲的失联会话 60 s 内被结束，静默对端的连接约 120 s 内被关闭，执行中的语句在客户端离开后约 10 s 内（静默对端约 130 s 内）被取消。所有客户端都把一个版本事务作为一个脚本发送，事务内的合法空闲约为一次往返，最长的有意停顿（断网演练的持锁会话）约 4 s。取值依据见 [S4/S5 第 4 节](STAGING_S4_S5.md#4-风险失联客户端的会话长时间持有锁)；本机虚拟机由 [configure_postgres.sh](../deploy/staging/configure_postgres.sh) 写入，已配置的虚拟机按 [4a 节](STAGING_VM.md#4a-给已配置的虚拟机加上失联客户端设置用户操作) 补上。

客户端一侧：`review_local.py` 的十写入者合入与 `agent_lab.py merge` 在合入事务内设置 `lock_timeout = '10s'`。等不到 main 锁时报告 `lock_timeout`（SQLSTATE 55P03），与 `stale_head`（40001）一样表示 main 未改变、同一提案可以重试，不当作 `merge_conflict`（P0001）。服务器的 `lock_timeout` 保持默认 0。

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

---

<a id="english-version"></a>

# Staging PostgreSQL Connection (S1)

2026-10-18. Connection instructions for phase S1 of the [Production test plan](PRODUCTION_TEST_PLAN.md). The user has chosen a local virtual machine as the staging host; for the step-by-step operations see [Local VM steps](STAGING_VM.md); this document gives the general rules independent of the host form. **There is no staging host yet**: the code and scripts below have been verified locally as far as they can be (see the last section), and real S1 results can only be produced after the user provides a host. Cloud services are still not verified against real cloud services, and this phase does not involve object storage.

## Connection rules

`db.py` handles hosts in three cases:

| Host | Requirement |
|---|---|
| `127.0.0.1` / `localhost` / `::1` | Same as before; `sslmode` can optionally be configured |
| Unix socket directory starting with `/` | Native Linux, or the WSL transport on Windows |
| Other (remote) | Must be a single hostname or IP, with `sslmode` set to `verify-full` and `sslrootcert` pointing to the CA file that issued the server certificate; otherwise the connection is refused |

`verify-full` checks both the certificate chain and the hostname, so the configured hostname must appear in the server certificate's CN or SAN. TLS settings are taken only from the configuration file or the command line (`--sslmode`, `--sslrootcert`); inherited `PGSSLMODE`, `PGSSLROOTCERT`, `PGREQUIRESSL` and `PGSSLNEGOTIATION` are always cleared, so that the shell environment cannot quietly lower the verification level. When `PGHOST` points to a remote host, the same rules apply. When using the WSL transport, `sslrootcert`, like `psql`, is a path inside the distribution.

The output of `python db.py check` now includes `ssl`, `ssl_version` and `sslmode`; a remote connection that does not use TLS raises an error directly (which cannot happen under `verify-full` anyway).

## Preparation steps

The following are carried out by the staging server's administrator; this project's scripts do not modify the server configuration.

1. **Server certificate**: a certificate issued by a CA with the hostname written into the SAN; in `postgresql.conf` set `ssl = on`, `ssl_min_protocol_version = 'TLSv1.2'` or higher, and make `listen_addresses` include an address reachable by the client. Give the CA certificate (not the private key) to the client.
2. **pg_hba.conf**: open TLS connections only for the dedicated role, filling in the network range according to the actual client:

   ```text
   hostssl  all  agentdb_staging  10.0.0.0/24  scram-sha-256
   ```

   The database column is `all`, because each suite creates new `agentdb_test_*` databases every time.
3. **Dedicated role**: as a superuser, run [create_staging_role.sql](../deploy/staging/create_staging_role.sql), then set the password interactively in psql:

   ```console
   psql -X -v ON_ERROR_STOP=1 -d postgres -f deploy/staging/create_staging_role.sql
   psql -X -d postgres -c "\password agentdb_staging"
   ```

   `agentdb_staging` is not a superuser and cannot create roles; it has only `CREATEDB`; on `agentdb_client` and `agentdb_manager` it has only SET privilege and does not inherit their privileges. The script creates these two NOLOGIN roles in advance, because `install` creates them only when they do not exist, and creating roles requires CREATEROLE. The script can be run repeatedly.
4. **Client password**: write it into the client's PostgreSQL password file (Linux `~/.pgpass`, permissions 0600; native Windows client `%APPDATA%\postgresql\pgpass.conf`), or add `--password` on each run for hidden input. Do not write it into JSON, command arguments or chat.
5. **Client configuration**: copy [connection.staging.example.json](../connection.staging.example.json) to `connection.staging.local.json` (ignored by `.gitignore`), and fill in the hostname and the CA file path.
6. **Timeouts for vanished clients** (server): by default PostgreSQL takes about 2 hours 11 minutes to notice a client that vanished mid-transaction, and during that time the locks it holds (for example main's branch lock) remain in effect. Set `ALTER ROLE agentdb_staging SET idle_in_transaction_session_timeout = '60s'` for the dedicated role (already included in the [role script](../deploy/staging/create_staging_role.sql); it applies only to that role, and administrator sessions keep the default), and in `postgresql.conf` or its `conf.d` set:

   ```text
   tcp_keepalives_idle = '60s'
   tcp_keepalives_interval = '10s'
   tcp_keepalives_count = 6
   tcp_user_timeout = '120s'
   client_connection_check_interval = '10s'
   ```

   This way a vanished session idle inside a transaction is ended within 60 s, a connection to a silent peer is closed within about 120 s, and a running statement is cancelled within about 10 s after the client leaves (within about 130 s for a silent peer). All clients send one version transaction as one script, so the legitimate idle time inside a transaction is about one round trip, and the longest intentional pause (the lock-holding session in the network-cut drill) is about 4 s. For the basis of the values see [S4/S5 section 4](STAGING_S4_S5.md#4-risk-sessions-of-vanished-clients-hold-locks-for-a-long-time); on the local VM they are written by [configure_postgres.sh](../deploy/staging/configure_postgres.sh), and an already configured VM adds them following [section 4a](STAGING_VM.md#4a-add-the-vanished-client-settings-to-an-already-configured-vm-user-action).

On the client side: the ten-writer merge in `review_local.py` and `agent_lab.py merge` set `lock_timeout = '10s'` inside the merge transaction. When the main lock cannot be obtained in time, `lock_timeout` (SQLSTATE 55P03) is reported, which, like `stale_head` (40001), means main has not changed and the same proposal can be retried; it is not treated as `merge_conflict` (P0001). The server's `lock_timeout` stays at the default 0.

## S1 verification commands

```console
python db.py check --config connection.staging.local.json
python run_tests.py --config connection.staging.local.json
.\.venv-cloud\Scripts\python.exe review_local.py --config connection.staging.local.json
python check_merge_migration.py --config connection.staging.local.json
```

For the pass criteria see the S1 row of the plan: `check` shows `"ssl": true`, `"sslmode": "verify-full"`; the three suites all pass as `agentdb_staging`. The result files note the environment as "staging PostgreSQL" and are kept separately from the local records. The user decides when to clean up the test databases each suite keeps.

## What has been verified locally (2026-10-18)

There is no staging host on this machine, so the following checks can only prove the code paths and the scripts themselves; they cannot replace S1:

- **TLS path (real WSL PostgreSQL 16.15, over TCP 127.0.0.1)**: the server uses the Ubuntu self-signed certificate `LAPTOP-D0IU4SUK.localdomain`; TCP login requires a password, which was not set or requested this time. With this certificate as the CA and `verify-ca`, the TLS handshake and certificate verification pass, and the connection stops at the authentication step (`fe_sendauth: no password supplied`); with an unrelated CA it reports `certificate verify failed`; `verify-full` to `127.0.0.1` reports a certificate hostname mismatch. A remote hostname without TLS settings is refused directly by `db.py`. The default Unix socket connection is unchanged. The success path of `verify-full` needs a server listening under the certificate's hostname; the local service listens only on `localhost`, so it was not covered.
- **Settings checks**: [tests/test_db_connection.py](../tests/test_db_connection.py) has 10 items in total and needs no database: remote hosts must use `verify-full`, verification modes must have a CA file, host lists and connection strings are rejected, inherited TLS variables are cleared, TLS fields in the configuration file, and the WSL transport passes the TLS variables into the distribution.
- **Role script**: in the local cluster, the script was run twice in a row and then the role attributes and memberships were checked (not a superuser, only CREATEDB, SET rather than inheritance for both roles), confirming that it cannot create roles and can SET ROLE to both roles, then everything was ROLLBACKed; after the rollback `agentdb_staging` does not exist in the cluster.
- **Non-superuser installation**: in the newly created dedicated database `agentdb_test_staginginstall_7cac3bcb`, within one transaction, the role script was run, CREATE privilege on that database was granted, and after SET ROLE to `agentdb_staging` all of the 001–004 SQL was installed: the three schemas and `vc.merge` are all owned by that role, and `agentdb_client` can read head; then ROLLBACK, so neither the role nor the objects were kept, and the empty database is kept for inspection.

- **VM scripts (in WSL, not involving any PostgreSQL)**: both scripts pass syntax checks; the server certificate chain generated by [make_certs.sh](../deploy/staging/make_certs.sh) verifies, with SAN `DNS:agentdb-staging.test, IP:192.168.56.10`, limited to server authentication, private key 0600, and it refuses to overwrite an existing directory and invalid IPs/hostnames. Real TLS handshakes with `openssl s_server` / `s_client`: with the correct CA, both the IP and the hostname verify; a wrong IP reports an address mismatch, and a wrong CA reports that the issuer cannot be found, consistent with `verify-full`'s decisions. For [configure_postgres.sh](../deploy/staging/configure_postgres.sh) only the arguments and the up-front refusal "must be run as root" were checked, along with the public key comparison and `-checkip` decision it uses; it modifies the server configuration, and was not run on the existing WSL PostgreSQL.

- **Native Windows psql client (2026-10-18)**: EDB's PostgreSQL 16.15 installer was installed via winget, with the server, pgAdmin and Stack Builder components disabled, and the installer hash check passed. `bin` still contains the `postgres.exe` program file, but there is no data directory, no Windows service, and no process listening on 5432 on the Windows side, so there is no running server. Through `db.py` (with `wsl_distribution` left empty), Windows psql connected to the local WSL server (`127.0.0.1:5432` in mirrored mode), with results matching the WSL client: the correct CA with `verify-ca` reaches the authentication step (`fe_sendauth: no password supplied`); an unrelated CA reports `certificate verify failed`; `verify-full` to `127.0.0.1` reports a hostname mismatch. When connecting to `192.168.56.10` with `connection.staging.local.json`, `db.py` accepts the remote configuration and hands the connection to psql, which, since there is no VM yet, reports `timeout expired` after the 5-second connection timeout. Windows' neighbor table shows that the ARP requests for this address are sent from the VirtualBox host-only adapter (state Incomplete, i.e. no host has answered yet), showing that the routing on the Windows side is correct. (`Find-NetRoute` reports the default route via WLAN, which does not match the packets actually sent, and is not relied on.)

Not yet covered: logging in as `agentdb_staging` and running the three suites in full (needs a persistent role and password), the remote network, and certificates issued by a real CA. These belong to S1 itself.
