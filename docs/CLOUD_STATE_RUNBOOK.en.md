English | [中文](CLOUD_STATE_RUNBOOK.md)

# Cloud State Service: Running and Connecting with Multiple Backends

**Status update (2026-10-17): cloud services are not verified against real cloud services, and cloud deployment is on hold.** The adapter code for GCS, Alibaba Cloud OSS and S3 has only local test records; the user reports having created an OSS bucket, but there has been no real integration testing, and the long-running service has not been deployed. The cloud commands below are for future reference only and are not currently run. For the full constraints see [Multi-backend design](STORAGE_BACKENDS.en.md); for the local scope see [Local test record](STORAGE_BACKENDS_VALIDATION.en.md).

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

For the specific deployment of Hangzhou ECS + OSS internal network see the [ECS guide](ALIYUN_ECS_DEPLOYMENT.en.md). Once the SSH tunnel is established, this machine can use `maintain_snapshot.py --state-url http://127.0.0.1:18844` plus the same task arguments below to operate on the existing PostgreSQL; use either `--state-url` or `--cloud-config`, and the client does not need OSS credentials. Instance role mode is configured only on ECS with `AGENTDB_OSS_CREDENTIALS=ecs_ram_role`.

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
