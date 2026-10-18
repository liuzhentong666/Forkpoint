English | [中文](ALIYUN_ECS_DEPLOYMENT.md)

# Alibaba Cloud ECS State Service Deployment Draft (On Hold)

Status update: 2026-10-17. **Cloud services are not verified against real cloud services; cloud resource purchasing, integration testing and deployment are currently on hold.** The user reports having created the Hangzhou OSS bucket `agentdb-dev-goudan`, but its permissions, configuration or real reads/writes have not been verified; there is no ECS, and no cloud state service has run. The text below is kept for future reference and is not a current execution plan or a record of a completed deployment.

## Deployment design and acceptance

```text
本机 Agent / maintain_snapshot.py ──SSH 隧道──> ECS 127.0.0.1:8844
          │                                   │
          v                                   v
现有 WSL PostgreSQL                 杭州 OSS 内网端点 → agentdb-dev-goudan
代码与业务数据版本                  管理任务状态、历史、规则、证据
```

(The diagram reads: local Agent / `maintain_snapshot.py` ── SSH tunnel ──> ECS 127.0.0.1:8844; the local side goes to the existing WSL PostgreSQL, holding code and business data versions; the ECS side goes to the Hangzhou OSS internal endpoint → `agentdb-dev-goudan`, holding management task state, history, rules and evidence.)

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
.\.venv-cloud\Scripts\python.exe maintain_snapshot.py --state-url http://127.0.0.1:18844 --task snapshot-job-1 --actor admin-agent-1 --revision REVISION_UUID --table-id TABLE_UUID
```

Replace the two UUIDs with an explicitly chosen version/logical table in the existing database. By default `connection.local.json` is used, and no database is created or migrated. The tool stops when a state request fails; when the result is unclear, after the connection is restored take over with the same task target, and it will re-check the real result in PostgreSQL. The HTTP client does not automatically change command_id or expected_version, or replay maintenance actions.

`StateClient` provides only the read/publish that the maintenance executor needs; the other capabilities are still provided by the full HTTP API. Requests and responses have timeouts/size limits, there is no persistent client cache, HTTP redirects are not followed, and the system proxy is not used to forward the loopback tunnel. Only HTTPS or loopback HTTP is accepted; if the service later configures a bearer token, the client reads it from `AGENTDB_STATE_TOKEN` and does not save it to JSON.

## Current deliverables and pending items

For the local scope see [Local check of the deployment scripts](ALIYUN_ECS_VALIDATION.en.md). The real OSS smoke test, RAM role renewal, systemd installation/restart and cross-network SSH access are all not verified in a real environment. No server purchase or cloud execution is currently scheduled; the code and this draft are kept, waiting for cloud work to resume later.
