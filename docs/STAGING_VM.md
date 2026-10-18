[English](STAGING_VM.en.md) | 中文

# 本机虚拟机作为预发布 PostgreSQL（方案 B）

2026-10-18。用户选择在这台 Windows 11 笔记本上用虚拟机承担 [S1](PRODUCTION_TEST_PLAN.md)：不产生云费用，不解除云端暂缓。虚拟机里的 PostgreSQL 与现有 WSL 开发库完全分开；客户端是 Windows 原生 psql（本机 WSL 的 mirrored 网络看不到仅主机网卡，见第 2 节），经虚拟网络、TLS `verify-full` 和专用角色 `agentdb_staging` 连接。连接规则见 [预发布接入](STAGING.md)。

限制：客户端和服务器共用一台机器的 CPU 与磁盘，S1 只用来证明正确性与接入方式，其计时不作为性能结论（S4 的计时也需注明这一点）。

## 约定值

下文使用以下值；若改动，三处（证书、服务器脚本、客户端配置）必须一致。

| 项 | 值 |
|---|---|
| 虚拟机网卡 2（仅主机网络） | 静态 `192.168.56.10/24`，Windows 侧为 `192.168.56.1` |
| 证书主机名 | `agentdb-staging.test`（只写入证书 SAN；客户端按 IP 连接，无需改 hosts 文件） |
| PostgreSQL | Ubuntu 24.04 自带的 16.x，集群 `16/main`，端口 5432 |
| 客户端网段 | `192.168.56.0/24` |

## 1. 建虚拟机（用户操作）

1. 安装 VirtualBox 7.x：在 Windows 终端执行 `winget install --id Oracle.VirtualBox -e` 并接受管理员与驱动提示，或从 [官网](https://www.virtualbox.org/wiki/Downloads) 下载（Windows 11 家庭版没有 Hyper-V 管理器；VMware Workstation Pro 免费版也可）。安装时会短暂断网以添加仅主机网卡；完成后 `VBoxManage list hostonlyifs` 应显示 `192.168.56.1`。WSL2 已启用时，VirtualBox 运行在 Hyper-V 平台之上，速度较慢但可用；不要为此关闭 WSL。2026-10-18 在本机安装的是 7.2.20。
2. 下载 [Ubuntu Server 24.04 LTS](https://ubuntu.com/download/server) ISO，新建虚拟机：2 vCPU、2 GB 内存、20 GB 磁盘足够（测试数据仅 4,000 行）。
3. 网卡：网卡 1 为 NAT（安装软件包用）；网卡 2 为“仅主机网络”，选择 `VirtualBox Host-Only Ethernet Adapter`（默认网段 192.168.56.0/24，DHCP 从 .100 起，.10 不会冲突）。
4. 安装 Ubuntu 时勾选 OpenSSH server；用户名自定，下文记作 `<vm-user>`。
5. 在虚拟机中给网卡 2 设静态地址。先用 `ip -br link` 查名称（通常为 `enp0s8`），写入 `/etc/netplan/60-hostonly.yaml`：

   ```yaml
   network:
     version: 2
     ethernets:
       enp0s8:
         addresses: [192.168.56.10/24]
   ```

   然后 `sudo chmod 600 /etc/netplan/60-hostonly.yaml && sudo netplan apply`。
6. 安装 PostgreSQL：`sudo apt update && sudo apt install -y postgresql`（24.04 为 16.x）。
7. 在 VirtualBox 中给虚拟机拍一个快照（例如“clean-postgres”）。之后配置出错或做 S5 故障演练时可以回到这里。

## 2. 检查 WSL 能否到达虚拟机（用户操作）

本机 WSL 使用 mirrored 网络模式（`.wslconfig` 中 `networkingMode=mirrored`）。这种模式只在 WSL 启动时镜像 Windows 网卡，安装 VirtualBox 之前已经运行的 WSL 看不到新的仅主机网卡：`ip route get 192.168.56.10` 会显示经由普通网络的默认网关，而不是 192.168.56.0/24 网段。安装 VirtualBox 后执行一次 `wsl --shutdown`，下次进入 WSL 时即重新镜像。它会结束所有 WSL 进程和终端；开发用 PostgreSQL 在 systemd 中设为开机启动，会随 WSL 一起重新启动。重启后 `wsl --distribution Ubuntu --exec ip -br -4 addr` 应能看到 `192.168.56.1`。**本机 2026-10-18 实测：重启后仍只镜像了主网卡，仅主机网卡没有出现，路由仍走默认网关**（开发用 PostgreSQL 在 1 秒内恢复）。因此本机改用下文的 Windows 原生 psql 方案。

使用 WSL 客户端时，在 Windows 终端执行下面的检查，输出 `reachable` 才继续：

```console
wsl --distribution Ubuntu --exec bash -c "timeout 3 bash -c '</dev/tcp/192.168.56.10/22' && echo reachable"
```

使用 Windows 原生客户端时，在 PowerShell 中直接连 SSH 端口，输出 `True` 才继续：

```console
$c = New-Object Net.Sockets.TcpClient; $c.ConnectAsync('192.168.56.10', 22).Wait(3000) -and $c.Connected; $c.Close()
```

不要用 `Test-NetConnection` 或 `Find-NetRoute` 判断：本机 2026-10-18 实测，它们为 192.168.56.10 选择了 WLAN 的源地址，报告端口 22 不通，而直接 TCP 连接已拿到虚拟机的 SSH 握手信息（`SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20`），邻居表中该地址的 MAC 与虚拟机网卡 2 一致。

若重启 WSL 后仍不通（mirrored 模式不一定镜像所有虚拟网卡），改用 Windows 原生 psql 作为客户端：只安装 PostgreSQL 客户端，**不要装 Windows 版服务器**（mirrored 模式下 Windows 与 WSL 共用端口，会与 WSL 中的 5432 冲突）。EDB 安装包可用 `--mode unattended --disable-components server,pgAdmin,stackbuilder` 只装命令行工具，安装后确认 `C:\Program Files\PostgreSQL\<版本>\bin\psql.exe` 存在、且没有新增 `postgresql-x64-*` Windows 服务。客户端配置中 `wsl_distribution` 留空、`psql` 填 `psql.exe` 的路径，`sslrootcert` 改为 Windows 路径（把 `ca.pem` 复制到 Windows 侧），口令文件改用 `%APPDATA%\postgresql\pgpass.conf`。这样流量直接从 Windows 进入仅主机网卡。

## 3. 生成证书（在 WSL 中，客户端一侧）

CA 私钥留在客户端，不放到服务器上：

```console
wsl --distribution Ubuntu --exec bash deploy/staging/make_certs.sh --name agentdb-staging.test --ip 192.168.56.10 --out /home/<wsl-user>/.agentdb-staging
```

在项目根目录执行（WSL 会把当前 Windows 目录映射为 `/mnt/f/...`）。脚本拒绝覆盖已有目录，结束时用 `openssl verify` 检查证书链并打印 SAN。产物：`ca.pem`（客户端的 sslrootcert）、`ca.key`（只留在此目录）、`server.crt` 与 `server.key`（复制到虚拟机）。

本机客户端是 Windows 原生 psql（见第 2 节），所以把 CA 证书（公开内容，不是私钥）复制一份到 Windows 侧：

```console
wsl --distribution Ubuntu --exec bash -c "mkdir -p /mnt/c/Users/lzt/.agentdb-staging && cp ~/.agentdb-staging/ca.pem /mnt/c/Users/lzt/.agentdb-staging/ca.pem"
```

私钥 `ca.key` 与 `server.key` 留在 WSL 的 ext4 文件系统中（权限 0600），不复制到 `/mnt/c`，那里不保留 Unix 权限。

## 4. 配置虚拟机中的 PostgreSQL

把四个文件复制到虚拟机（在 WSL 中执行）：

```console
scp /home/<wsl-user>/.agentdb-staging/server.crt /home/<wsl-user>/.agentdb-staging/server.key \
    deploy/staging/configure_postgres.sh deploy/staging/create_staging_role.sql <vm-user>@192.168.56.10:~/
```

在虚拟机中：

```console
sudo bash configure_postgres.sh --cert server.crt --key server.key \
  --listen 192.168.56.10 --client-cidr 192.168.56.0/24 --role-sql create_staging_role.sql
sudo -u postgres psql -X -d postgres -c '\password agentdb_staging'
shred -u server.key
```

[configure_postgres.sh](../deploy/staging/configure_postgres.sh) 的改动：安装证书对（私钥 0600，属 postgres）；写入 `conf.d/agentdb-staging.conf`（`listen_addresses = 'localhost,192.168.56.10'`、`ssl = on`、TLS 1.2 起、scram-sha-256，以及失联客户端的超时：`idle_in_transaction_session_timeout` 60 s、TCP keepalive 60 s / 10 s / 6 次、`tcp_user_timeout` 120 s、`client_connection_check_interval` 10 s，取值依据见 [S4/S5 第 4 节](STAGING_S4_S5.md#4-风险失联客户端的会话长时间持有锁)）；先备份再向 `pg_hba.conf` 追加一行 `hostssl all agentdb_staging 192.168.56.0/24 scram-sha-256`；ufw 启用时只放行该网段到 5432；重启集群，确认 `ssl` 为 on、`conf.d` 文件中的每一项都已生效（`pg_file_settings` 中没有报错或被后面的文件覆盖）；执行角色脚本。它不设置口令。删除 `conf.d/agentdb-staging.conf` 和那一行 pg_hba 即可撤销，或直接回到快照。

## 4a. 给已配置的虚拟机加上失联客户端设置（用户操作）

2026-10-18 之前配置的虚拟机（包括快照 `s1-configured`）的 `conf.d` 文件里还没有上述超时，失联客户端可以占住 main 锁约 2 小时 11 分钟（见 [S4/S5 第 4 节](STAGING_S4_S5.md#4-风险失联客户端的会话长时间持有锁)）。脚本可以重复执行，重新运行一次即可补上；不传 `--cert`/`--key` 时沿用已安装的证书对（第 4 节已 `shred` 掉家目录里的 `server.key`）。`pg_hba.conf` 中已有的那一行不会重复追加，ufw 规则已存在时 ufw 会跳过，角色脚本可重复执行，口令不变。

1. 把新脚本复制到虚拟机。本机 WSL 到达不了仅主机网络（第 2 节），所以在 Windows PowerShell 中执行：

   ```console
   scp F:6-10-14\https-www-anthropic-com-engineering-redesigninggentdb-postgres\deploy\staging\configure_postgres.sh F:6-10-14\https-www-anthropic-com-engineering-redesigninggentdb-postgres\deploy\staging\create_staging_role.sql <vm-user>@192.168.56.10:~/
   ```

   使用 WSL 客户端、且 WSL 能到达虚拟机的环境，可改为在 WSL 中执行 `scp deploy/staging/configure_postgres.sh deploy/staging/create_staging_role.sql <vm-user>@192.168.56.10:~/`。

2. 在虚拟机中执行。**它会重启 PostgreSQL，断开所有连接，不要在演练进行中执行**：

   ```console
   sudo bash configure_postgres.sh --listen 192.168.56.10 --client-cidr 192.168.56.0/24 --role-sql create_staging_role.sql
   ```

   最后一行应为 `Vanished clients: agentdb_staging idle_in_transaction_session_timeout=60s; TCP keepalive 60 s, then 6 probes 10 s apart.`（该超时只设在 `agentdb_staging` 角色上）；若某项设置未生效，脚本以 `settings in … not in effect` 报错退出。

3. 在 Windows 中经 TLS 以 `agentdb_staging` 核对（Windows 版 psql 只认连接串之前的选项，所以 `-XAtc` 放在最前；keepalive 的值只在 TCP 连接上显示，虚拟机内经 Unix socket 查看时为 0；连接串中的路径用正斜杠，libpq 会把反斜杠当作转义）：

   ```console
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -XAtc "SELECT name, setting, unit FROM pg_settings WHERE name ~ '^(idle_in_transaction|tcp_|client_connection)' ORDER BY 1" "host=192.168.56.10 user=agentdb_staging dbname=postgres sslmode=verify-full sslrootcert=C:/Users/lzt/.agentdb-staging/ca.pem"
   ```

   应看到 `client_connection_check_interval|10000|ms`、`idle_in_transaction_session_timeout|60000|ms`、`tcp_keepalives_count|6|`、`tcp_keepalives_idle|60|s`、`tcp_keepalives_interval|10|s`、`tcp_user_timeout|120000|ms`。

4. 运行断网演练，按 [S4/S5 第 4 节](STAGING_S4_S5.md#断网演练的通过条件) 的通过条件判断。

撤销：删去 `conf.d/agentdb-staging.conf` 中 “Bounded detection of vanished clients” 以下各行后 `sudo systemctl restart postgresql@16-main`，或回到快照 `s1-configured`。设置生效后是否另拍一个关机状态的快照作为新的演练重置点，由用户决定。

## 5. 客户端配置（Windows 原生 psql）

1. **只装 PostgreSQL 16 客户端**（用户在 Windows 终端执行；会弹出管理员确认）：

   ```console
   winget install --id PostgreSQL.PostgreSQL.16 -e --source winget --override "--mode unattended --unattendedmodeui minimal --disable-components server,pgAdmin,stackbuilder"
   ```

   winget 下载 EDB 官方安装包 `postgresql-16.15-5-windows-x64.exe`（`--override` 替换其默认参数），只保留命令行工具，安装到 `C:\Program Files\PostgreSQL\16`。不装服务器，因此不需要超级用户口令，也不会注册 Windows 服务或占用 5432。安装后应满足：`"C:\Program Files\PostgreSQL\16\bin\psql.exe" --version` 输出 16.x；`Get-Service postgresql*` 没有结果。`bin` 里仍会有 `postgres.exe` 程序文件，但不会创建 `data` 目录或服务，也不会监听 5432。本机 2026-10-18 已按此安装（16.15），核验结果见 [预发布接入](STAGING.md) 末节。
2. **口令**：写入 `%APPDATA%\postgresql\pgpass.conf`（不存在就新建目录和文件），一行 `192.168.56.10:5432:*:agentdb_staging:<口令>`；或每次运行加 `--password` 隐藏输入。
3. **连接配置** `connection.staging.local.json`（已被 `.gitignore` 忽略；本机已按下列内容建好）：

   ```json
   {
     "host": "192.168.56.10",
     "port": 5432,
     "user": "agentdb_staging",
     "database": "agentdb_staging",
     "maintenance_database": "postgres",
     "psql": "C:\\Program Files\\PostgreSQL\\16\\bin\\psql.exe",
     "wsl_distribution": "",
     "wsl_user": "",
     "sslmode": "verify-full",
     "sslrootcert": "C:\\Users\\lzt\\.agentdb-staging\\ca.pem"
   }
   ```

   `wsl_distribution` 留空即使用 Windows 原生 psql，流量直接从 Windows 进入仅主机网卡；`db.py` 把 `sslmode`、`sslrootcert` 以 `PGSSLMODE`、`PGSSLROOTCERT` 交给 psql。开发用的 `connection.local.json`（WSL 传输、Unix socket）不变。

## 6. 运行 S1

按 [预发布接入](STAGING.md#s1-核验命令) 的四条命令执行。`db.py check` 应显示 `"ssl": true`、`"ssl_version": "TLSv1.3"`（或 1.2）、`"sslmode": "verify-full"`、`"user": "agentdb_staging"`。四条命令全部通过即 S1 完成；把结果另存为预发布记录，与本机记录分开。

## 本机已核验的部分

脚本在没有虚拟机的情况下只能部分检查，见 [预发布接入](STAGING.md) 末节。`configure_postgres.sh` 修改服务器配置，未在现有 WSL PostgreSQL 上执行；它的第一次真实运行就在虚拟机里。
