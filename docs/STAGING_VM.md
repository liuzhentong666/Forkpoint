# 本机虚拟机作为预发布 PostgreSQL（方案 B）

2026-10-18。用户选择在这台 Windows 11 笔记本上用虚拟机承担 [S1](PRODUCTION_TEST_PLAN.md)：不产生云费用，不解除云端暂缓。虚拟机里的 PostgreSQL 与现有 WSL 开发库完全分开；客户端仍是 WSL 中的 psql，经虚拟网络、TLS `verify-full` 和专用角色 `agentdb_staging` 连接。连接规则见 [预发布接入](STAGING.md)。

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

1. 安装 [VirtualBox](https://www.virtualbox.org/wiki/Downloads) 7.x（Windows 11 家庭版没有 Hyper-V 管理器；VMware Workstation Pro 免费版也可）。WSL2 已启用时，VirtualBox 运行在 Hyper-V 平台之上，速度较慢但可用；不要为此关闭 WSL。
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

在 Windows 终端执行：

```console
wsl --distribution Ubuntu --exec bash -c "timeout 3 bash -c '</dev/tcp/192.168.56.10/22' && echo reachable"
```

输出 `reachable` 才继续。若不通，可在 `%UserProfile%\.wslconfig` 的 `[wsl2]` 下设置 `networkingMode=mirrored` 后执行 `wsl --shutdown` 再试；这会改变 WSL 的网络模式，由用户决定。

## 3. 生成证书（在 WSL 中，客户端一侧）

CA 私钥留在客户端，不放到服务器上：

```console
wsl --distribution Ubuntu --exec bash deploy/staging/make_certs.sh --name agentdb-staging.test --ip 192.168.56.10 --out /home/<wsl-user>/.agentdb-staging
```

在项目根目录执行（WSL 会把当前 Windows 目录映射为 `/mnt/f/...`）。脚本拒绝覆盖已有目录，结束时用 `openssl verify` 检查证书链并打印 SAN。产物：`ca.pem`（客户端的 sslrootcert）、`ca.key`（只留在此目录）、`server.crt` 与 `server.key`（复制到虚拟机）。

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

[configure_postgres.sh](../deploy/staging/configure_postgres.sh) 的改动：安装证书对（私钥 0600，属 postgres）；写入 `conf.d/agentdb-staging.conf`（`listen_addresses = 'localhost,192.168.56.10'`、`ssl = on`、TLS 1.2 起、scram-sha-256）；先备份再向 `pg_hba.conf` 追加一行 `hostssl all agentdb_staging 192.168.56.0/24 scram-sha-256`；ufw 启用时只放行该网段到 5432；重启集群并确认 `ssl` 为 on；执行角色脚本。它不设置口令。删除 `conf.d/agentdb-staging.conf` 和那一行 pg_hba 即可撤销，或直接回到快照。

## 5. 客户端配置（WSL）

1. 口令写入默认 WSL 用户的 `~/.pgpass`（权限 0600）：`192.168.56.10:5432:*:agentdb_staging:<口令>`。或每次运行加 `--password`。
2. 复制 [connection.staging.example.json](../connection.staging.example.json) 为 `connection.staging.local.json`（已忽略），改为：

   ```json
   {
     "host": "192.168.56.10",
     "port": 5432,
     "user": "agentdb_staging",
     "database": "agentdb_staging",
     "maintenance_database": "postgres",
     "psql": "/usr/bin/psql",
     "wsl_distribution": "Ubuntu",
     "wsl_user": "",
     "sslmode": "verify-full",
     "sslrootcert": "/home/<wsl-user>/.agentdb-staging/ca.pem"
   }
   ```

   `wsl_user` 留空表示默认 WSL 用户，与 `~/.pgpass` 和 `ca.pem` 的所有者一致。

## 6. 运行 S1

按 [预发布接入](STAGING.md#s1-核验命令) 的四条命令执行。`db.py check` 应显示 `"ssl": true`、`"ssl_version": "TLSv1.3"`（或 1.2）、`"sslmode": "verify-full"`、`"user": "agentdb_staging"`。四条命令全部通过即 S1 完成；把结果另存为预发布记录，与本机记录分开。

## 本机已核验的部分

脚本在没有虚拟机的情况下只能部分检查，见 [预发布接入](STAGING.md) 末节。`configure_postgres.sh` 修改服务器配置，未在现有 WSL PostgreSQL 上执行；它的第一次真实运行就在虚拟机里。
