中文 | [English](#english-version)

# S1 结果：预发布 PostgreSQL（本机虚拟机）

2026-10-18。[生产环境测试计划](PRODUCTION_TEST_PLAN.md) S1 阶段的记录。**S1 通过。** 云服务仍未经真实验证，本阶段不涉及对象存储；以下是本机虚拟机上的结果，不是生产或云端结论。

## 环境

| 项 | 值 |
|---|---|
| 主机 | 本机 VirtualBox 7.2.20 虚拟机 `agent-db`：2 vCPU、2 GB 内存、20 GB 动态磁盘；Ubuntu Server 24.04.5（ISO 的 SHA-256 已对照官方 SHA256SUMS 校验）|
| 服务器 | PostgreSQL 16.15（Ubuntu 16.15-0ubuntu0.24.04.1），由 [configure_postgres.sh](../deploy/staging/configure_postgres.sh) 配置：监听 `localhost,192.168.56.10`，TLS 1.2 起，`pg_hba` 只允许 `agentdb_staging` 经 `hostssl` 从 192.168.56.0/24 以 scram-sha-256 登录 |
| 证书 | [make_certs.sh](../deploy/staging/make_certs.sh) 在 WSL 中生成的私有 P-256 CA；服务器证书 SAN 为 `DNS:agentdb-staging.test, IP:192.168.56.10`；CA 私钥未离开 WSL |
| 网络 | VirtualBox 仅主机网络，Windows 侧 192.168.56.1；WSL mirrored 模式看不到该网卡，因此客户端为 Windows 原生 psql |
| 客户端 | Windows 原生 psql 16.15（仅客户端组件），`db.py` 读取 `connection.staging.local.json`：`sslmode=verify-full`，`sslrootcert` 为该 CA；口令来自 `%APPDATA%\postgresql\pgpass.conf` |
| 登录身份 | `agentdb_staging`：非超级用户、不能建角色、仅 CREATEDB，对 `agentdb_client`、`agentdb_manager` 只有 SET 权限（[角色脚本](../deploy/staging/create_staging_role.sql)） |
| 代码 | 提交 90f2dbf 之上，未改动代码 |

## 结果

| 检查 | 结果 | 原始记录 |
|---|---|---|
| `db.py check` | `ssl: true`，`TLSv1.3`，`sslmode: verify-full`，`user: agentdb_staging`，服务器 16.15 | [db-check.json](../results/staging/db-check.json) |
| `run_tests.py` | 原有 18 项断言、Session 等价检查、批量合入 13 个情形全部通过；10 个独立分支 10 个成功，同一旧 head 竞争 1 成功、9 个陈旧版本拒绝。测试库 `agentdb_test_273d7423471f` | [run-tests.json](../results/staging/run-tests.json) |
| `review_local.py` | 全部通过：受限管理角色以 42501 拒绝 7 类操作；两个中断窗口（意图后接手 `applied=true`，提交后接手 `applied=false`）与旧执行者冲突；十个分支全部合入，核验 100 行改动、3,900 行未变、10 个文件、10 个来源提交可达、基线不变、无遗留事务。测试库 `agentdb_test_review_12b899a534` | [review-local.json](../results/staging/review-local.json) |
| `scripts/check_merge_migration.py` | 用提交 8aeb3d7 的旧 `001_core.sql` 建库后连续迁移两次：数据与历史哈希不变、函数属性不变、13 个合入情形输出逐项相同、迁移前后 18 项断言均通过、`agentdb_client` 仍可合入。测试库 `agentdb_test_mergemig_ef8b856aa0` | [merge-batch-migration.json](../results/staging/merge-batch-migration.json) |

以上全部以非超级用户 `agentdb_staging` 经 TLS 远程连接完成，满足计划中 S1 的通过条件。此前只能在本机核验到认证步骤为止的 `verify-full` 成功路径，在此得到真实覆盖。

## 计时（仅供参考）

`review_local.py` 的十分支合入阶段 941 ms，端到端 1,804 ms，陈旧 head 重试 45 次（写入者各用一个长连接）。客户端与服务器共用同一台笔记本的 CPU 与磁盘，且经虚拟网卡与 TLS，这些数字只说明流程可用，不作为性能结论，也不与本机 WSL 结果直接比较。

## 过程中发现并已处理的问题

- WSL mirrored 网络在重启后仍不镜像 VirtualBox 仅主机网卡，改用 Windows 原生 psql；指南已更新。
- `Test-NetConnection` / `Find-NetRoute` 对 192.168.56.10 选择了 WLAN 源地址并误报端口不通；指南改用直接 TCP 连接检查。
- 设置角色口令时，曾把口令直接写进 `psql -c` 命令（随后出现在聊天、虚拟机 shell 历史与服务器错误日志中）；已改用交互式 `\password` 设置另一个口令，原字符串不再使用。

## 保留与后续

- 虚拟机上保留三个测试库（见上表），以及快照 `clean-postgres`（配置前状态）。是否清理由用户决定。
- 本机原有结果文件 `test-results-local.json`、`local-review-results.json` 在运行前备份、运行后恢复；预发布结果另存于 `results/staging/`。
- 未覆盖：真实 CA 签发的证书、跨机器网络、生产形态的主机。下一阶段 S2（真实对象存储）需要用户先解除云端暂缓；S4（负载与增长）和 S5（故障演练）可以在此虚拟机上继续，快照便于重置。

---

<a id="english-version"></a>

# S1 Results: Staging PostgreSQL (Local Virtual Machine)

2026-10-18. Record of the S1 phase of the [Production test plan](PRODUCTION_TEST_PLAN.md). **S1 passed.** Cloud services are still not verified against real cloud services, and this phase does not involve object storage; the following are results on a local virtual machine, not production or cloud conclusions.

## Environment

| Item | Value |
|---|---|
| Host | Local VirtualBox 7.2.20 virtual machine `agent-db`: 2 vCPU, 2 GB memory, 20 GB dynamic disk; Ubuntu Server 24.04.5 (the ISO's SHA-256 was checked against the official SHA256SUMS) |
| Server | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1), configured by [configure_postgres.sh](../deploy/staging/configure_postgres.sh): listens on `localhost,192.168.56.10`, TLS 1.2 and above, `pg_hba` only allows `agentdb_staging` to log in via `hostssl` from 192.168.56.0/24 with scram-sha-256 |
| Certificates | A private P-256 CA generated in WSL by [make_certs.sh](../deploy/staging/make_certs.sh); the server certificate's SAN is `DNS:agentdb-staging.test, IP:192.168.56.10`; the CA private key never left WSL |
| Network | VirtualBox host-only network, 192.168.56.1 on the Windows side; WSL mirrored mode cannot see this adapter, so the client is the native Windows psql |
| Client | Native Windows psql 16.15 (client components only); `db.py` reads `connection.staging.local.json`: `sslmode=verify-full`, `sslrootcert` is that CA; the password comes from `%APPDATA%\postgresql\pgpass.conf` |
| Login identity | `agentdb_staging`: not a superuser, cannot create roles, only CREATEDB, with only SET privilege on `agentdb_client` and `agentdb_manager` ([role script](../deploy/staging/create_staging_role.sql)) |
| Code | On top of commit 90f2dbf, with no code changes |

## Results

| Check | Result | Raw record |
|---|---|---|
| `db.py check` | `ssl: true`, `TLSv1.3`, `sslmode: verify-full`, `user: agentdb_staging`, server 16.15 | [db-check.json](../results/staging/db-check.json) |
| `run_tests.py` | The original 18 assertions, the Session equivalence check and the 13 batched-merge cases all passed; 10 separate branches, 10 succeeded; competing on the same old head, 1 succeeded and 9 were rejected as stale versions. Test database `agentdb_test_273d7423471f` | [run-tests.json](../results/staging/run-tests.json) |
| `review_local.py` | All passed: the restricted management role rejected 7 kinds of operations with 42501; the two interruption windows (hand-off after intent `applied=true`, hand-off after commit `applied=false`) and the stale-executor conflict; all ten branches merged, verifying 100 rows changed, 3,900 rows unchanged, 10 files, 10 source commits reachable, baseline unchanged, no leftover transactions. Test database `agentdb_test_review_12b899a534` | [review-local.json](../results/staging/review-local.json) |
| `scripts/check_merge_migration.py` | After building a database with the old `001_core.sql` from commit 8aeb3d7, migrated twice in a row: data and history hashes unchanged, function attributes unchanged, the outputs of the 13 merge cases identical item by item, the 18 assertions all passed before and after the migration, and `agentdb_client` could still merge. Test database `agentdb_test_mergemig_ef8b856aa0` | [merge-batch-migration.json](../results/staging/merge-batch-migration.json) |

All of the above was done by the non-superuser `agentdb_staging` over a remote TLS connection, meeting the plan's pass criteria for S1. The `verify-full` success path, which locally could only be verified up to the authentication step, is actually covered here.

## Timing (for reference only)

In `review_local.py` the ten-branch merge phase took 941 ms, end to end 1,804 ms, with 45 stale-head retries (each writer using one long-lived connection). The client and server share the CPU and disk of the same laptop and go through a virtual network adapter and TLS; these numbers only show that the workflow works, are not a performance conclusion, and are not directly compared with the local WSL results.

## Problems found and handled along the way

- After a reboot the WSL mirrored network still did not mirror the VirtualBox host-only adapter, so the native Windows psql was used instead; the guide has been updated.
- `Test-NetConnection` / `Find-NetRoute` picked the WLAN source address for 192.168.56.10 and falsely reported the port as unreachable; the guide now uses a direct TCP connection check instead.
- When setting the role password, the password was once written directly into a `psql -c` command (and subsequently appeared in the chat, the VM's shell history and the server error log); it was changed to setting a different password with the interactive `\password`, and the original string is no longer used.

## What is kept and next steps

- Three test databases are kept on the VM (see the table above), as well as the snapshot `clean-postgres` (the state before configuration). Whether to clean them up is up to the user.
- The local original result files `test-results-local.json` and `local-review-results.json` were backed up before the run and restored after it; the staging results are saved separately in `results/staging/`.
- Not covered: certificates issued by a real CA, cross-machine networking, a production-shaped host. The next phase, S2 (real object storage), needs the user to lift the cloud hold first; S4 (load and growth) and S5 (failure drills) can continue on this VM, and the snapshot makes resetting easy.
