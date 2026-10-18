中文 | [English](#english-version)

# 云端状态服务：多后端运行与接入

**状态更新（2026-10-17）：云服务未经真实验证，云端部署暂缓。** GCS、阿里云 OSS、S3 的适配代码仅有本地测试记录；用户报告已建 OSS 桶，但尚未真实联调，也未部署常驻服务。下文云端命令仅供未来参考，当前不执行。完整约束见 [多后端设计](STORAGE_BACKENDS.md)，本地范围见 [本地测试记录](STORAGE_BACKENDS_VALIDATION.md)。

## 环境与依赖

长期部署建议 Python 3.11+。本机已有 `.venv-cloud`，此次 Python 3.10.11 通过测试，但 Google SDK 对该版本已发出停止支持提醒。

新环境创建虚拟环境后，只安装采用的后端依赖：

```powershell
python -m venv .venv-cloud
# 阿里云 OSS
.\.venv-cloud\Scripts\python.exe -m pip install -r requirements-oss.txt
# S3 用户改用 requirements-s3.txt；GCS 用户改用 requirements-cloud.txt
```

服务核心只依赖标准库。选择 OSS 不需要 Google 账户、Google SDK 或 Pub/Sub。可选精确分词另装 `tiktoken>=0.9,<1`；默认字节预算不需要分词器。开发者运行全部适配器测试时安装 `requirements-storage-tests.txt`。

## OSS 配置与凭据

复制 `cloud.oss.example.json` 为 `cloud.local.json`，填写已有 bucket 和真实 region，例如 `cn-hangzhou`。不指定 endpoint 时由官方 SDK 解析地域端点；需要内网端点时填写同地域的 HTTPS URL。`prefix` 为项目专用前缀，不使用其他应用正在写入的对象路径。

本版通过官方 SDK 环境凭据提供器读取 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` 和可选 `OSS_SESSION_TOKEN`。在进程环境或部署平台的凭据注入机制中配置；不要把值放到聊天、JSON、源码或任务状态中。使用其他身份提供器时可向 `OSSStore` 注入已配置的官方 SDK client。

本版要求 bucket 从未启用版本控制，并且未配置 BucketWorm / ObjectWorm。启动会调用 GetBucketVersioning、GetBucketWorm、GetBucketObjectWormConfiguration 检查；身份需要这些读取权限和对象读写权限。程序不修改桶设置。不要在服务运行期间开启这些功能，也不要设置删除活跃前缀的生命周期规则。

```powershell
.\.venv-cloud\Scripts\python.exe -m agent_state.server --config cloud.local.json --check
```

`--check` 只验证访问与配置，不能替代并发发布验证。真实桶准备好后运行：

```powershell
.\.venv-cloud\Scripts\python.exe smoke_cloud.py --config cloud.local.json
```

该命令在 `prefix/smoke-<随机ID>` 下创建对象，验证十个并发写者、旧命令重试、发布前后中断和恢复读取；保留对象供检查，不创建桶、不更改 IAM，也不连接 PostgreSQL。中断由客户端注入，报告不代表真实机房故障演练。

OSS 每条任务入口记录固定 8 KiB，单任务文件最多 4 GiB / 524,288 次发布。到达上限会拒绝写入；调用方需显式创建后继任务并保存关联。当前不支持透明日志轮转、任务 ID 删除后复用或覆盖追加文件。

## S3 与 GCS 配置

S3 用户复制 `cloud.s3.example.json`，设置 bucket、region；示例 region 为北京 `cn-north-1`，按实际部署调整。使用 boto3 的凭据链，JSON 不接收密钥。可配置 HTTPS endpoint，但自定义 S3 兼容产品只有通过真实条件写入测试后才能列为已验证；不支持条件写入的端点不回退到普通覆盖。

GCS 用户使用 `cloud.example.json`，通过 [ADC](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc) 提供身份。旧配置不含 provider 时继续选择 GCS。`project` 与 `subscription` 仅适用于 GCS；OSS/S3 配置这些字段会报错。旧 `smoke_gcs.py --bucket ...` 入口仍保留，新验证优先使用统一 `smoke_cloud.py`。

每个部署选择一个权威后端。修改 provider、bucket 或 prefix 会指向另一个命名空间，不会自动迁移旧数据。已有部署迁移应单独冻结写入、复制并核验所有引用、建立目标入口并验证恢复；本版没有自动跨云迁移工具，也不会双写两个云。

## 常驻服务与通知

```powershell
.\.venv-cloud\Scripts\python.exe -m agent_state.server --config cloud.local.json
```

默认监听 `127.0.0.1:8844`。运行时只缓存有限活跃任务，每 30 秒核对这些任务，不扫描 bucket。调度器在服务重启后提供 task_id，即可恢复指定任务。

配置 `AGENTDB_STATE_TOKEN` 可启用共享 bearer 令牌。所有已接入 Agent 使用同等能力；actor 是调用者提供的标签，不是独立身份认证。非 loopback 监听必须配置令牌，外部部署配合 TLS 入口。标准库 HTTP 服务用于受控开发环境，生产部署还需要进程托管、连接限流与生产级入口；本服务本身不运行模型。

GCS 可选配置已有 Pub/Sub pull subscription。OSS/S3 默认通过周期核对和 `fresh=true` 工作，不需要消息服务。可信事件转发器也可以向 `POST /v1/notifications` 发送统一格式：

```json
{
  "event": {
    "kind": "changed",
    "task_id": "snapshot-job-1",
    "bucket": "YOUR_BUCKET",
    "prefix": "agentdb-state"
  }
}
```

kind 可为 changed 或 deleted；bucket/prefix 必须匹配当前部署。消息仅使缓存失效，不携带可直接写入的任务状态。重复消息最多造成额外核对，不会降低已发布版本。旧 GCS attributes 格式保留兼容；本版不自动订阅或解析 OSS/S3 的原生通知，转发器需显式转换。

## API 与上下文

| 方法与路径 | 用途 |
|---|---|
| `POST /v1/tasks/<id>` | 创建，提供 command_id、actor、完整 state、可选 event |
| `PUT /v1/tasks/<id>` | 更新，另外提供 expected_version |
| `GET /v1/tasks/<id>?fresh=true` | 读取当前状态，fresh 绕过任务缓存 |
| `POST /v1/tasks/<id>/context` | 按 max_bytes / 可选 max_tokens、fact_keys、fresh 构建上下文 |
| `GET /v1/tasks/<id>/history?limit=20&cursor=...` | 从新到旧分页读取历史 |
| `POST /v1/artifacts` | 以 JSON base64 字段上传证据 |
| `GET /v1/artifacts/<hash>` | 获取证据，返回 base64 |
| `GET /v1/objects/<hash>` | 获取指定不可变 JSON 对象 |
| `POST /v1/notifications` | 缓存失效通知 |
| `GET /metrics`、`GET /health` | 请求/缓存指标及进程状态 |

创建请求示例：

```json
{
  "command_id": "prepare-001",
  "actor": "admin-agent-1",
  "state": {
    "goal": "检查固定版本快照",
    "status": "running",
    "next_action": "查询 PostgreSQL 的实际结果",
    "uncertainties": ["前次操作响应丢失"],
    "facts": {},
    "policy_ref": null,
    "evidence_refs": []
  },
  "event": "开始核验"
}
```

状态上限 32 KiB，证据单块上限 1 MiB。更新使用完整状态，不是隐式 patch。网络结果不明时以完全相同的 command_id、actor 与请求重试；409 后读取最新状态重新判断，不只改 expected_version。去重结果可能是较早版本，调用方需要当前状态时另外读取。

读取结果中的兼容字段 generation 是后端不透明标记：GCS 返回整数，OSS/S3 返回字符串。不要对它排序、加一或用于跨云比较；提交用任务的 expected_version。原有 head JSON、不可变对象编码和 SHA-256 ID 保持不变，OSS 仅改变入口的物理存放方式。

上下文默认上限为完整 JSON 的 8,192 UTF-8 字节；必要字段超限报错，可选事实与证据省略时明确返回数量及 state_ref。配置 tokenizer（例如 cl100k_base）后可同时指定 token 上限；usage 的内容统计不包括 usage 自身，硬预算检查覆盖整个响应。模型其他消息由调用方另行计入。编码器首次使用可能下载公开词表，这不是任务状态持久化。

## PostgreSQL 维护与测试

杭州 ECS + OSS 内网的具体部署见 [ECS 指南](ALIYUN_ECS_DEPLOYMENT.md)。SSH 隧道建立后，本机可用 `maintain_snapshot.py --state-url http://127.0.0.1:18844` 加下列相同任务参数操作现有 PostgreSQL；`--state-url` 与 `--cloud-config` 二选一，客户端不需要 OSS 凭据。实例角色模式仅在 ECS 配置 `AGENTDB_OSS_CREDENTIALS=ecs_ram_role`。

正式维护入口使用选定云后端和现有 `connection.local.json`：

以下仍为暂缓执行的云端示例。新版维护器需要先在现有数据库执行 `python db.py install-manager`；使用受限快照身份时加 `--role agentdb_manager`。其 `vc.snapshot_status` 只返回目标和快照是否存在，维护器不再直接查询历史表。

```powershell
.\.venv-cloud\Scripts\python.exe maintain_snapshot.py --cloud-config cloud.local.json --task snapshot-job-1 --actor admin-agent-1 --revision REVISION_UUID --table-id TABLE_UUID
```

先记录意图，再检查/创建/核验固定版本快照，最后发布结果。接手者会检查实际数据库结果。云端发布与 PG 不共享事务；其他 DDL 或执行器需要自己的幂等、协调与恢复设计。

如果旧维护者停顿期间另一维护者完成，旧维护者醒来后的发布冲突会原样返回。调用者可以重新读取任务，但不能把该次冲突当成自己的成功发布。该规则不阻止任意外部动作的重复执行；数据库快照仍依赖自身幂等性。

开发验证入口：

```powershell
.\.venv-cloud\Scripts\python.exe -m pip install -r requirements-storage-tests.txt
.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests -v
.\.venv-cloud\Scripts\python.exe benchmark_oss_protocol.py
.\.venv-cloud\Scripts\python.exe integration_cloud_postgres.py --database agentdb_test_EXISTING --test-backend oss-protocol
```

前两种本地测试不访问云。最后一项使用现有 PostgreSQL 测试库与内存 OSS 服务模型，会保留测试分支；不启停服务，不创建 ops schema。内存模拟、SDK 契约、真实数据库与真实云验证必须分别报告。

---

<a id="english-version"></a>

# Cloud State Service: Running and Connecting with Multiple Backends

**Status update (2026-10-17): cloud services are not verified against real cloud services, and cloud deployment is on hold.** The adapter code for GCS, Alibaba Cloud OSS and S3 has only local test records; the user reports having created an OSS bucket, but there has been no real integration testing, and the long-running service has not been deployed. The cloud commands below are for future reference only and are not currently run. For the full constraints see [Multi-backend design](STORAGE_BACKENDS.md); for the local scope see [Local test record](STORAGE_BACKENDS_VALIDATION.md).

## Environment and dependencies

Python 3.11+ is recommended for long-term deployment. This machine already has `.venv-cloud`; this time Python 3.10.11 passed the tests, but the Google SDK has already issued an end-of-support notice for that version.

In a new environment, after creating the virtual environment, install only the dependencies of the backend you adopt:

```powershell
python -m venv .venv-cloud
# 阿里云 OSS
.\.venv-cloud\Scripts\python.exe -m pip install -r requirements-oss.txt
# S3 用户改用 requirements-s3.txt；GCS 用户改用 requirements-cloud.txt
```

(The comments say: "Alibaba Cloud OSS"; "S3 users use requirements-s3.txt instead; GCS users use requirements-cloud.txt instead".)

The service core depends only on the standard library. Choosing OSS does not require a Google account, the Google SDK or Pub/Sub. For optional exact tokenization, additionally install `tiktoken>=0.9,<1`; the default byte budget does not need a tokenizer. Developers running all adapter tests install `requirements-storage-tests.txt`.

## OSS configuration and credentials

Copy `cloud.oss.example.json` to `cloud.local.json`, and fill in an existing bucket and the real region, for example `cn-hangzhou`. When no endpoint is specified, the official SDK resolves the regional endpoint; when an internal endpoint is needed, fill in an HTTPS URL in the same region. `prefix` is a project-specific prefix; do not use object paths that other applications are writing to.

This version reads `OSS_ACCESS_KEY_ID`, `OSS_ACCESS_KEY_SECRET` and the optional `OSS_SESSION_TOKEN` through the official SDK's environment credential provider. Configure them in the process environment or in the deployment platform's credential injection mechanism; do not put the values in chat, JSON, source code or task state. When using another identity provider, a configured official SDK client can be injected into `OSSStore`.

This version requires that the bucket has never had versioning enabled and has no BucketWorm / ObjectWorm configured. Startup calls GetBucketVersioning, GetBucketWorm and GetBucketObjectWormConfiguration to check; the identity needs these read permissions plus object read/write permissions. The program does not modify bucket settings. Do not enable these features while the service is running, and do not set lifecycle rules that delete the active prefix.

```powershell
.\.venv-cloud\Scripts\python.exe -m agent_state.server --config cloud.local.json --check
```

`--check` only verifies access and configuration and cannot replace concurrent publishing verification. Once a real bucket is ready, run:

```powershell
.\.venv-cloud\Scripts\python.exe smoke_cloud.py --config cloud.local.json
```

This command creates objects under `prefix/smoke-<random ID>` and verifies ten concurrent writers, retries of old commands, interruptions before and after publishing, and recovery reads; the objects are kept for inspection; it does not create buckets, change IAM, or connect to PostgreSQL. Interruptions are injected by the client, and the report does not represent a real data-center failure drill.

Each OSS task entry record is a fixed 8 KiB, and a single task file allows at most 4 GiB / 524,288 publishes. Reaching the limit refuses writes; the caller must explicitly create a successor task and save the association. Transparent log rotation, reusing a task ID after deletion, and overwriting the append file are currently not supported.

## S3 and GCS configuration

S3 users copy `cloud.s3.example.json` and set bucket and region; the example region is Beijing `cn-north-1`, adjust according to the actual deployment. boto3's credential chain is used, and the JSON does not accept keys. An HTTPS endpoint can be configured, but a custom S3-compatible product can only be listed as verified after passing real conditional write tests; endpoints that do not support conditional writes do not fall back to ordinary overwrites.

GCS users use `cloud.example.json` and provide identity through [ADC](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc). Old configurations without provider continue to select GCS. `project` and `subscription` apply only to GCS; configuring these fields for OSS/S3 is an error. The old `smoke_gcs.py --bucket ...` entry point is still kept, but new verification should preferably use the unified `smoke_cloud.py`.

Each deployment selects one authoritative backend. Changing provider, bucket or prefix points to another namespace and does not automatically migrate old data. Migrating an existing deployment should separately freeze writes, copy and verify all references, build the target entry point and verify recovery; this version has no automatic cross-cloud migration tool, and does not dual-write to two clouds.

## Long-running service and notifications

```powershell
.\.venv-cloud\Scripts\python.exe -m agent_state.server --config cloud.local.json
```

It listens on `127.0.0.1:8844` by default. At runtime it caches only a limited number of active tasks, checks these tasks every 30 seconds, and does not scan the bucket. After a service restart, the scheduler provides a task_id and the specified task can be recovered.

Configuring `AGENTDB_STATE_TOKEN` enables a shared bearer token. All connected Agents use the same capabilities; actor is a label supplied by the caller, not independent authentication. Non-loopback listening requires a token to be configured, and external deployment pairs it with a TLS entry point. The standard-library HTTP service is for controlled development environments; production deployment also needs process supervision, connection rate limiting and a production-grade entry point; the service itself does not run models.

GCS can optionally be configured with an existing Pub/Sub pull subscription. OSS/S3 work by default through periodic reconciliation and `fresh=true`, without needing a messaging service. A trusted event forwarder can also send a unified format to `POST /v1/notifications`:

```json
{
  "event": {
    "kind": "changed",
    "task_id": "snapshot-job-1",
    "bucket": "YOUR_BUCKET",
    "prefix": "agentdb-state"
  }
}
```

kind can be changed or deleted; bucket/prefix must match the current deployment. Messages only invalidate the cache and do not carry task state that can be written directly. Duplicate messages cause at most an extra check and never lower a published version. The old GCS attributes format is kept for compatibility; this version does not automatically subscribe to or parse OSS/S3 native notifications, so the forwarder must convert them explicitly.

## API and context

| Method and path | Purpose |
|---|---|
| `POST /v1/tasks/<id>` | Create; provide command_id, actor, the complete state, and an optional event |
| `PUT /v1/tasks/<id>` | Update; additionally provide expected_version |
| `GET /v1/tasks/<id>?fresh=true` | Read the current state; fresh bypasses the task cache |
| `POST /v1/tasks/<id>/context` | Build the context according to max_bytes / optional max_tokens, fact_keys, fresh |
| `GET /v1/tasks/<id>/history?limit=20&cursor=...` | Read history page by page, newest to oldest |
| `POST /v1/artifacts` | Upload evidence as a JSON base64 field |
| `GET /v1/artifacts/<hash>` | Get evidence, returned as base64 |
| `GET /v1/objects/<hash>` | Get the specified immutable JSON object |
| `POST /v1/notifications` | Cache invalidation notification |
| `GET /metrics`, `GET /health` | Request/cache metrics and process status |

Example create request:

```json
{
  "command_id": "prepare-001",
  "actor": "admin-agent-1",
  "state": {
    "goal": "检查固定版本快照",
    "status": "running",
    "next_action": "查询 PostgreSQL 的实际结果",
    "uncertainties": ["前次操作响应丢失"],
    "facts": {},
    "policy_ref": null,
    "evidence_refs": []
  },
  "event": "开始核验"
}
```

(The Chinese example values mean: goal "check the pinned-version snapshot", next_action "query the actual result in PostgreSQL", uncertainties ["the previous operation's response was lost"], event "start verification".)

The state limit is 32 KiB, and the limit for a single evidence chunk is 1 MiB. Updates use the complete state, not an implicit patch. When the network result is unclear, retry with exactly the same command_id, actor and request; after a 409, read the latest state and re-evaluate, rather than only changing expected_version. A deduplicated result may be an earlier version; callers that need the current state read it separately.

The compatibility field generation in read results is an opaque backend marker: GCS returns an integer, OSS/S3 return a string. Do not sort it, increment it or use it for cross-cloud comparisons; submissions use the task's expected_version. The original head JSON, the immutable object encoding and the SHA-256 IDs are unchanged; OSS only changes how the entry point is physically stored.

The default context limit is 8,192 UTF-8 bytes of complete JSON; when required fields exceed the limit an error is raised, and when optional facts and evidence are omitted the counts and state_ref are returned explicitly. After configuring a tokenizer (for example cl100k_base), a token limit can be specified as well; usage's content statistics exclude usage itself, and the hard budget check covers the whole response. Other messages to the model are counted separately by the caller. The encoder may download a public vocabulary on first use; this is not task state persistence.

## PostgreSQL maintenance and testing

For the specific deployment of Hangzhou ECS + OSS internal network see the [ECS guide](ALIYUN_ECS_DEPLOYMENT.md). Once the SSH tunnel is established, this machine can use `maintain_snapshot.py --state-url http://127.0.0.1:18844` plus the same task arguments below to operate on the existing PostgreSQL; use either `--state-url` or `--cloud-config`, and the client does not need OSS credentials. Instance role mode is configured only on ECS with `AGENTDB_OSS_CREDENTIALS=ecs_ram_role`.

The formal maintenance entry point uses the selected cloud backend and the existing `connection.local.json`:

The following is still a cloud example whose execution is on hold. The new maintainer requires first running `python db.py install-manager` on the existing database; when using the restricted snapshot identity add `--role agentdb_manager`. Its `vc.snapshot_status` returns only the target and whether the snapshot exists, and the maintainer no longer queries the history tables directly.

```powershell
.\.venv-cloud\Scripts\python.exe maintain_snapshot.py --cloud-config cloud.local.json --task snapshot-job-1 --actor admin-agent-1 --revision REVISION_UUID --table-id TABLE_UUID
```

It first records the intent, then checks/creates/verifies the pinned-version snapshot, and finally publishes the result. A taker-over checks the actual database result. Cloud publishing and PG do not share a transaction; other DDL or executors need their own idempotency, coordination and recovery design.

If another maintainer completes while an old maintainer is stalled, the publishing conflict the old maintainer gets after waking up is returned as is. The caller can re-read the task, but cannot treat that conflict as its own successful publish. This rule does not prevent repeated execution of arbitrary external actions; database snapshots still rely on their own idempotency.

Development verification entry points:

```powershell
.\.venv-cloud\Scripts\python.exe -m pip install -r requirements-storage-tests.txt
.\.venv-cloud\Scripts\python.exe -m unittest discover -s tests -v
.\.venv-cloud\Scripts\python.exe benchmark_oss_protocol.py
.\.venv-cloud\Scripts\python.exe integration_cloud_postgres.py --database agentdb_test_EXISTING --test-backend oss-protocol
```

The first two local tests do not access the cloud. The last one uses an existing PostgreSQL test database and the in-memory OSS service model, and keeps the test branches; it does not start/stop the service and does not create an ops schema. In-memory simulation, SDK contracts, real database and real cloud verification must be reported separately.
