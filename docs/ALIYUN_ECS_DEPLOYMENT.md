中文 | [English](#english-version)

# 阿里云 ECS 状态服务部署草案（暂缓）

状态更新：2026-10-17。**云服务未经真实验证，当前暂缓云资源采购、联调和部署。** 用户报告已创建杭州 OSS 桶 `<your-oss-bucket>`，但未核验其权限、配置或真实读写；没有 ECS，也没有已运行的云端状态服务。下文保留为未来参考，不是当前执行计划或已完成部署的记录。

## 部署设计与验收

```text
本机 Agent / scripts/maintain_snapshot.py ──SSH 隧道──> ECS 127.0.0.1:8844
          │                                   │
          v                                   v
现有 WSL PostgreSQL                 杭州 OSS 内网端点 → <your-oss-bucket>
代码与业务数据版本                  管理任务状态、历史、规则、证据
```

拟议方案中，ECS 只运行状态服务，使用实例 RAM 角色取得临时凭据，配置中不保存 AccessKey；所有接入 Agent 使用相同状态 API 能力。正式状态计划放在 OSS，进程只使用有界内存缓存，系统盘仅保存程序、依赖和配置。这些云端行为及凭据续期尚未在真实 ECS 上检验。

本机通过 SSH 转发访问 API，无需域名、证书或开放公网 8844。服务的无 token 回环监听信任该 ECS 的本机进程及获准 SSH 转发的用户，所以首次部署使用专用实例。以后需要共享公网入口时，再建设 HTTPS、认证及限流入口；当前 Python 标准库 HTTP 服务仅按这种受控接入方式部署。

代码提供 `ecs_ram_role` 凭据模式及维护工具的 `--state-url` 参数，保留 `--cloud-config`。本地 SDK/HTTP 测试只能检查代码与请求构造；真实桶 smoke、常驻启动和重启恢复均未执行，当前暂缓。

回退时停止服务，切回先前程序及配置，再读取同一 OSS 前缀；本次不迁移存储格式。切回尚无 ECS 凭据支持的程序时，必须另外提供它支持的凭据方式。不要通过删桶、清空前缀或回写旧 head 来回退。

## 未来恢复部署时的 ECS 配置参考

以下是本项目开发联调的起步配置，不是性能保证，也不是已下单配置；费用以控制台订单为准。

| 选项 | 建议 |
|---|---|
| 产品与地域 | 云服务器 ECS；华东 1（杭州），`cn-hangzhou` |
| 付费 | 先按量付费联调，观察实际用量后决定长期计费 |
| CPU / 内存 | x86_64，2 vCPU / 2 GiB 起步；价格接近可选 4 GiB；无需 GPU |
| 镜像 | Ubuntu 24.04 LTS 64 位公共镜像，使用其 Python 3.12 |
| 系统盘 | 40 GiB 起步，用于系统、程序及依赖；不额外购买数据库盘 |
| 网络 | VPC + 同地域交换机；允许 SSH 和安装依赖所需的出站网络 |
| 公网 | 为首次 SSH / scp 配置公网 IPv4 或 EIP；业务 OSS 读写走内网 |
| 登录 | SSH 密钥；私钥只留在本机，不发到聊天或项目目录 |
| 安全组入站 | TCP 22 仅允许你的当前公网 IP；不开放 8844、5432 |
| 实例元数据 | 开启元数据访问，使用加固模式 IMDSv2 |

实例地域创建后不能直接改变；[ECS 创建文档](https://help.aliyun.com/zh/ecs/user-guide/create-an-instance-by-using-the-wizard)。同地域 ECS 可使用 OSS 内网端点，不要求桶与 ECS 绑定同一 VPC，也无需绑定自定义域名；[OSS 网络说明](https://www.alibabacloud.com/help/zh/oss/user-guide/access-and-network-overview)。内网不意味着存储和请求免费。

## 将实例角色连接到桶

在 RAM 控制台创建受信任服务为 ECS 的角色，例如 `AgentDBStateEcsRole`。信任策略见 `deploy/ecs/ram-role-trust.json`；将 `deploy/ecs/oss-policy.json` 建为自定义权限策略并授予角色，再把角色绑定到这台 ECS。已有角色的实例须先检查其用途，不能直接替换。操作入口见[实例 RAM 角色文档](https://help.aliyun.com/zh/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance)。

该策略允许读取桶的版本/保留配置，以及读写 `agentdb-state/` 下对象，包含 smoke 随机子前缀。AppendObject 对应 `oss:PutObject`，HeadObject 对应 `oss:GetObject`，映射依据为 [OSS 授权动作说明](https://help.aliyun.com/zh/oss/user-guide/authorization-syntax-and-elements)。它不配置 Agent 等级，也不是数据库 superuser 的权限清单；这是状态服务访问自己数据所需的云权限。若未来管理 Agent 要变更云资源，应单独明确其执行身份与操作范围。

桶需使用标准存储并保持私有；不要开启版本控制（曾开启后暂停也不兼容）、WORM 或对象保留策略，不要给活跃前缀设置自动删除/归档生命周期。部署工具只检查和报告，不替你修改这些配置。服务当前检查版本与保留策略，生命周期需在控制台核对。

内网端点已写入 `deploy/ecs/cloud.ecs.json`：

```text
https://oss-cn-hangzhou-internal.aliyuncs.com
```

`AGENTDB_OSS_CREDENTIALS=ecs_ram_role` 显式启用角色凭据，强制 IMDSv2；可用 `ALIBABA_CLOUD_ECS_METADATA` 指定实际角色名，未指定时由 SDK 查询。每次 OSS 签名从凭据提供器取值，临时凭据的缓存/刷新由官方 Credentials SDK 管理，不能启动时取一次后永久复用。原有默认模式 `environment` 仍使用 OSS SDK 的环境变量凭据。参见[官方 Python 凭据说明](https://help.aliyun.com/zh/sdk/developer-reference/v2-manage-python-access-credentials)。

## 构建、传输、安装

本机项目根目录：

```powershell
.\.venv-cloud\Scripts\python.exe deploy/build_ecs_bundle.py
```

生成 `dist/agentdb-state-ecs.tar.gz` 和 SHA-256 校验文件。构建器只打包明确列出的源码与部署文件，排除数据库连接配置、虚拟环境及任何本地凭据。记录输出哈希；传输后检查相同哈希，再解压。

以下 `agentdb-ecs` 是你自己配置的 SSH Host 别名，指向新实例的公网地址和登录用户，不是真实已有主机名：

```powershell
scp dist/agentdb-state-ecs.tar.gz agentdb-ecs:~/
ssh agentdb-ecs
```

在 ECS 执行：

```bash
sha256sum ~/agentdb-state-ecs.tar.gz  # 与本机构建输出比较
mkdir ~/agentdb-state-install
tar -xzf ~/agentdb-state-ecs.tar.gz -C ~/agentdb-state-install
cd ~/agentdb-state-install/agentdb-state
sudo bash deploy/ecs/install.sh
```

首次安装器校验文件 manifest，创建专用系统用户和 `/opt/agentdb-state` 程序目录，安装 Python 依赖，然后以服务身份运行桶检查、真实 smoke，通过后才注册和启动 systemd。它拒绝覆盖已有安装；安装失败时保留现场，先查看错误再处理，不能靠反复重装跳过失败检查。安装会使用软件源和 PyPI 网络，真实 smoke 会写入随机前缀并保留少量测试对象，产生正常 OSS 请求/存储费用。

若依赖安装成功但桶检查失败，核对地域、角色绑定、RAM 策略、内网解析与桶配置。修复后可在程序目录以 `agentdb-state` 用户执行下面相同命令继续诊断；确认 smoke 成功后再安装 unit 和启动：

```bash
cd /opt/agentdb-state
sudo -u agentdb-state env AGENTDB_OSS_CREDENTIALS=ecs_ram_role \
  .venv/bin/python -m agent_state.server --config deploy/ecs/cloud.ecs.json --check
sudo -u agentdb-state env AGENTDB_OSS_CREDENTIALS=ecs_ram_role \
  .venv/bin/python smoke_cloud.py --config deploy/ecs/cloud.ecs.json
sudo install -m 0644 deploy/ecs/agentdb-state.service /etc/systemd/system/agentdb-state.service
sudo systemctl daemon-reload
sudo systemctl enable --now agentdb-state
```

`--check` 只确认桶配置读取；smoke 才验证真实 put/append、10 个竞争写者、旧命令去重、发布前后中断恢复和有界冷读取。`/health` 仅证明进程在响应，不代表此刻 OSS 可写。启动后检查：

```bash
systemctl status agentdb-state --no-pager
journalctl -u agentdb-state -n 60 --no-pager
curl --fail http://127.0.0.1:8844/health
```

日志用于排障，不作为任务恢复依据。重启后应通过 API 读取之前创建的任务，并核对版本/状态；不要仅以新建任务或 `/health` 作为恢复证据。当前单 ECS 无高可用切换；systemd 能拉起进程，不能消除实例或网络故障。

## 本机 Agent 和 PostgreSQL 接入

在本机单独终端保留 SSH 隧道：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18844:127.0.0.1:8844 agentdb-ecs
```

另一个终端检查服务，并让维护工具只在本机操作已有 PostgreSQL：

```powershell
Invoke-RestMethod http://127.0.0.1:18844/health
.\.venv-cloud\Scripts\python.exe scripts/maintain_snapshot.py --state-url http://127.0.0.1:18844 --task snapshot-job-1 --actor admin-agent-1 --revision REVISION_UUID --table-id TABLE_UUID
```

把两个 UUID 换成现有数据库中明确选定的版本/逻辑表。默认沿用 `connection.local.json`，不创建或迁移数据库。状态请求出错时工具停止；结果不明时恢复连接后使用同一任务目标接手，它会重新检查 PostgreSQL 的真实结果。HTTP 客户端不自动改 command_id、expected_version 或重放维护动作。

`StateClient` 仅提供维护执行器需要的 read/publish；其余能力仍由完整 HTTP API 提供。请求和响应设超时/大小上限，无客户端持久缓存，不跟随 HTTP 重定向，不使用系统代理转发回环隧道。只接受 HTTPS 或回环 HTTP；如服务以后配置 bearer token，客户端从 `AGENTDB_STATE_TOKEN` 读取，不将其保存到 JSON。

## 当前交付与待完成事项

本地范围见 [部署脚本本地检查](ALIYUN_ECS_VALIDATION.md)。真实 OSS smoke、RAM 角色续期、systemd 安装/重启及跨网络 SSH 接入均未经真实验证。当前不安排服务器采购或云端执行；保留代码和本草案，等待以后恢复云端工作。

---

<a id="english-version"></a>

# Alibaba Cloud ECS State Service Deployment Draft (On Hold)

Status update: 2026-10-17. **Cloud services are not verified against real cloud services; cloud resource purchasing, integration testing and deployment are currently on hold.** The user reports having created the Hangzhou OSS bucket `<your-oss-bucket>`, but its permissions, configuration or real reads/writes have not been verified; there is no ECS, and no cloud state service has run. The text below is kept for future reference and is not a current execution plan or a record of a completed deployment.

## Deployment design and acceptance

```text
本机 Agent / scripts/maintain_snapshot.py ──SSH 隧道──> ECS 127.0.0.1:8844
          │                                   │
          v                                   v
现有 WSL PostgreSQL                 杭州 OSS 内网端点 → <your-oss-bucket>
代码与业务数据版本                  管理任务状态、历史、规则、证据
```

(The diagram reads: local Agent / `scripts/maintain_snapshot.py` ── SSH tunnel ──> ECS 127.0.0.1:8844; the local side goes to the existing WSL PostgreSQL, holding code and business data versions; the ECS side goes to the Hangzhou OSS internal endpoint → `<your-oss-bucket>`, holding management task state, history, rules and evidence.)

In the proposed plan, ECS runs only the state service, obtains temporary credentials through the instance RAM role, and no AccessKey is saved in the configuration; all connected Agents use the same state API capabilities. Formal state is planned to be kept in OSS, the process uses only a bounded in-memory cache, and the system disk holds only the program, dependencies and configuration. These cloud behaviors and credential renewal have not been tested on a real ECS.

This machine accesses the API through SSH forwarding, with no need for a domain, a certificate or opening port 8844 to the public internet. The service's token-less loopback listener trusts local processes on that ECS and users permitted to use SSH forwarding, so the first deployment uses a dedicated instance. When a shared public entry point is needed later, HTTPS, authentication and a rate-limited entry point will be built then; the current Python standard-library HTTP service is deployed only in this controlled-access way.

The code provides the `ecs_ram_role` credential mode and the maintenance tool's `--state-url` parameter, keeping `--cloud-config`. Local SDK/HTTP tests can only check the code and request construction; the real bucket smoke test, long-running startup and restart recovery have not been executed and are currently on hold.

To roll back, stop the service, switch back to the previous program and configuration, then read the same OSS prefix; this round does not migrate the storage format. When switching back to a program without ECS credential support, a credential method it supports must be provided separately. Do not roll back by deleting the bucket, emptying the prefix or writing back an old head.

## ECS configuration reference for when deployment resumes

The following is a starting configuration for this project's development integration testing; it is not a performance guarantee, nor a configuration that has been ordered; costs are as shown in the console order.

| Option | Suggestion |
|---|---|
| Product and region | Elastic Compute Service (ECS); China East 1 (Hangzhou), `cn-hangzhou` |
| Billing | Start with pay-as-you-go for integration testing, and decide long-term billing after observing actual usage |
| CPU / memory | x86_64, starting at 2 vCPU / 2 GiB; 4 GiB is an option if the price is close; no GPU needed |
| Image | Ubuntu 24.04 LTS 64-bit public image, using its Python 3.12 |
| System disk | Starting at 40 GiB, for the system, program and dependencies; no extra database disk purchased |
| Network | VPC + vSwitch in the same region; allow SSH and the outbound network needed to install dependencies |
| Public network | Configure a public IPv4 or EIP for the first SSH / scp; business OSS reads/writes go over the internal network |
| Login | SSH key; the private key stays only on this machine and is not sent to chat or the project directory |
| Security group inbound | TCP 22 allowed only from your current public IP; do not open 8844 or 5432 |
| Instance metadata | Enable metadata access, using the hardened mode IMDSv2 |

An instance's region cannot be changed directly after creation; [ECS creation documentation](https://help.aliyun.com/zh/ecs/user-guide/create-an-instance-by-using-the-wizard). An ECS in the same region can use the OSS internal endpoint; the bucket does not need to be bound to the same VPC as the ECS, and no custom domain needs to be bound; [OSS network overview](https://www.alibabacloud.com/help/zh/oss/user-guide/access-and-network-overview). The internal network does not mean storage and requests are free.

## Connecting the instance role to the bucket

In the RAM console, create a role whose trusted service is ECS, for example `AgentDBStateEcsRole`. The trust policy is in `deploy/ecs/ram-role-trust.json`; create `deploy/ecs/oss-policy.json` as a custom permission policy and grant it to the role, then attach the role to this ECS. For an instance that already has a role, first check what that role is used for; it must not be replaced directly. For the steps see the [instance RAM role documentation](https://help.aliyun.com/zh/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance).

The policy allows reading the bucket's versioning/retention configuration, and reading and writing objects under `agentdb-state/`, including the smoke test's random sub-prefixes. AppendObject corresponds to `oss:PutObject` and HeadObject to `oss:GetObject`; the mapping is based on the [OSS authorization actions documentation](https://help.aliyun.com/zh/oss/user-guide/authorization-syntax-and-elements). It does not configure Agent levels, nor is it the permission list of a database superuser; these are the cloud permissions the state service needs to access its own data. If in the future the manager Agent needs to change cloud resources, its execution identity and scope of operations should be defined separately.

The bucket must use standard storage and stay private; do not enable versioning (having enabled it and then suspended it is also incompatible), WORM or object retention policies, and do not set automatic delete/archive lifecycle rules on the active prefix. The deployment tools only check and report, and do not change these configurations for you. The service currently checks versioning and retention policies; lifecycle rules must be checked in the console.

The internal endpoint has been written into `deploy/ecs/cloud.ecs.json`:

```text
https://oss-cn-hangzhou-internal.aliyuncs.com
```

`AGENTDB_OSS_CREDENTIALS=ecs_ram_role` explicitly enables role credentials and enforces IMDSv2; `ALIBABA_CLOUD_ECS_METADATA` can specify the actual role name, and when it is not specified the SDK looks it up. Every OSS signature takes values from the credential provider; caching/refreshing of the temporary credentials is managed by the official Credentials SDK, and they cannot be fetched once at startup and reused forever. The original default mode `environment` still uses the OSS SDK's environment-variable credentials. See the [official Python credentials documentation](https://help.aliyun.com/zh/sdk/developer-reference/v2-manage-python-access-credentials).

## Build, transfer, install

From the local project root:

```powershell
.\.venv-cloud\Scripts\python.exe deploy/build_ecs_bundle.py
```

This produces `dist/agentdb-state-ecs.tar.gz` and a SHA-256 checksum file. The builder packages only the explicitly listed source and deployment files, excluding the database connection configuration, virtual environments and any local credentials. Record the output hash; after transfer, check that the hash is the same, then extract.

The `agentdb-ecs` below is an SSH Host alias you configure yourself, pointing to the new instance's public address and login user; it is not a real existing hostname:

```powershell
scp dist/agentdb-state-ecs.tar.gz agentdb-ecs:~/
ssh agentdb-ecs
```

Run on the ECS:

```bash
sha256sum ~/agentdb-state-ecs.tar.gz  # 与本机构建输出比较
mkdir ~/agentdb-state-install
tar -xzf ~/agentdb-state-ecs.tar.gz -C ~/agentdb-state-install
cd ~/agentdb-state-install/agentdb-state
sudo bash deploy/ecs/install.sh
```

(The comment on the first line means "compare with the local build output".)

The first-time installer verifies the file manifest, creates a dedicated system user and the `/opt/agentdb-state` program directory, installs the Python dependencies, then, as the service identity, runs the bucket check and the real smoke test, and only after they pass registers and starts systemd. It refuses to overwrite an existing installation; when installation fails it keeps the state as it is, so look at the error first before dealing with it; do not rely on reinstalling repeatedly to skip failed checks. Installation uses the package sources and the PyPI network, and the real smoke test writes to a random prefix and keeps a small number of test objects, incurring normal OSS request/storage costs.

If dependency installation succeeds but the bucket check fails, check the region, role attachment, RAM policy, internal DNS resolution and bucket configuration. After fixing, you can continue diagnosing by running the same commands below as the `agentdb-state` user in the program directory; only after confirming the smoke test succeeds, install the unit and start it:

```bash
cd /opt/agentdb-state
sudo -u agentdb-state env AGENTDB_OSS_CREDENTIALS=ecs_ram_role \
  .venv/bin/python -m agent_state.server --config deploy/ecs/cloud.ecs.json --check
sudo -u agentdb-state env AGENTDB_OSS_CREDENTIALS=ecs_ram_role \
  .venv/bin/python smoke_cloud.py --config deploy/ecs/cloud.ecs.json
sudo install -m 0644 deploy/ecs/agentdb-state.service /etc/systemd/system/agentdb-state.service
sudo systemctl daemon-reload
sudo systemctl enable --now agentdb-state
```

`--check` only confirms that the bucket configuration can be read; only the smoke test verifies real put/append, 10 competing writers, deduplication of old commands, recovery from interruptions before and after publishing, and bounded cold reads. `/health` only proves that the process is responding, not that OSS is writable at that moment. After startup, check:

```bash
systemctl status agentdb-state --no-pager
journalctl -u agentdb-state -n 60 --no-pager
curl --fail http://127.0.0.1:8844/health
```

Logs are for troubleshooting and are not a basis for task recovery. After a restart, tasks created earlier should be read through the API and their versions/states checked; do not use only a newly created task or `/health` as evidence of recovery. A single ECS currently has no high-availability failover; systemd can bring the process back up but cannot eliminate instance or network failures.

## Connecting the local Agent and PostgreSQL

Keep the SSH tunnel open in a separate terminal on this machine:

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18844:127.0.0.1:8844 agentdb-ecs
```

In another terminal, check the service, and have the maintenance tool operate only on the existing PostgreSQL on this machine:

```powershell
Invoke-RestMethod http://127.0.0.1:18844/health
.\.venv-cloud\Scripts\python.exe scripts/maintain_snapshot.py --state-url http://127.0.0.1:18844 --task snapshot-job-1 --actor admin-agent-1 --revision REVISION_UUID --table-id TABLE_UUID
```

Replace the two UUIDs with an explicitly chosen version/logical table in the existing database. By default `connection.local.json` is used, and no database is created or migrated. The tool stops when a state request fails; when the result is unclear, after the connection is restored take over with the same task target, and it will re-check the real result in PostgreSQL. The HTTP client does not automatically change command_id or expected_version, or replay maintenance actions.

`StateClient` provides only the read/publish that the maintenance executor needs; the other capabilities are still provided by the full HTTP API. Requests and responses have timeouts/size limits, there is no persistent client cache, HTTP redirects are not followed, and the system proxy is not used to forward the loopback tunnel. Only HTTPS or loopback HTTP is accepted; if the service later configures a bearer token, the client reads it from `AGENTDB_STATE_TOKEN` and does not save it to JSON.

## Current deliverables and pending items

For the local scope see [Local check of the deployment scripts](ALIYUN_ECS_VALIDATION.md). The real OSS smoke test, RAM role renewal, systemd installation/restart and cross-network SSH access are all not verified in a real environment. No server purchase or cloud execution is currently scheduled; the code and this draft are kept, waiting for cloud work to resume later.
