English | [中文](CLOUD_STATE_DESIGN.md)

# Cloud State and Context Service for Long-Running Agents

> Status update (2026-10-17): this document is a design and code description; cloud services are not verified against real cloud services, and cloud deployment is on hold. The storage layout and recovery flow below do not mean there are any cloud run results.

Date: 2026-10-16. This document records the original GCS implementation design. It has since been extended to multiple backends (GCS / OSS / S3); for provider selection, the task entry-point protocol and compatibility boundaries, [STORAGE_BACKENDS.en.md](STORAGE_BACKENDS.en.md) takes precedence; this document's principles of bounded state, idempotency and cross-system recovery continue to apply.

## Goals and boundaries

Business versions are still managed by AgentDB on the existing Ubuntu WSL PostgreSQL. The design of the manager Agent state service is: complete operation facts, checkpoints and evidence are written to the selected cloud object storage, active state is kept in runtime memory, and the model gets a size-capped context per task. This cloud flow has not actually run; the design does not use SQLite, local state files or a new PostgreSQL ops schema.

Being online all day means the service runs continuously; the model's context is still limited. Storage volume can grow, but ordinary recovery does not scan the storage bucket or replay the full history. The in-memory cache can be discarded and recovered from cloud state. Source code, connection configuration and test reports are not part of the management task's persistent state.

The first version implements the object storage protocol, the GCS adapter, conditional publishing, idempotent commands, the in-memory cache, the context service, the HTTP interface, notification intake, and a recoverable PostgreSQL snapshot maintenance example. It does not automatically execute arbitrary DDL, database restarts or model inference; these executors will be connected later through the same state protocol. The superuser Agent's database capabilities are not reduced by this service.

## State and object layout

All keys are within the configured bucket/prefix. Task IDs are limited to short strings of letters, digits, underscores and hyphens.

```text
tasks/<task_id>/head.json          唯一可变发布入口
objects/<sha256>                   不可变 JSON：状态、事件、命令索引节点
artifacts/<sha256>                 不可变原始证据
```

(In the layout above: `tasks/<task_id>/head.json` is the only mutable publishing entry point; `objects/<sha256>` holds immutable JSON: states, events and command index nodes; `artifacts/<sha256>` holds immutable raw evidence.)

The head contains the protocol version, the task ID, a monotonically increasing version number, the current state hash, the latest event hash and the command index root hash. The head does not contain an ever-growing history list. The state holds the goal, status, next step, open items, structured facts, the rule object reference and evidence references; the default JSON limit is 32 KiB, and when it is exceeded the task must be split or the details stored as evidence.

Each successful change produces a complete but bounded state checkpoint. So cold recovery needs only two logical object reads, the head and that state; there is no need to replay deltas. The cost is writing one extra small state per publish. One logical read on GCS may involve two network requests, metadata and content at a fixed generation, and reports must not conflate the two.

Each immutable event links to the previous event's hash. The history interface reads only the requested page along the chain, the page size has a hard limit, and a cursor is returned. This is a cost incurred when history is explicitly requested, not part of routine context recovery. No task head or cache entry embeds the complete event chain.

## Publishing, concurrency and idempotency

The client submits command_id, expected_version, the executor, the complete target state and a short event description. The server validates size, format and evidence references, then writes the immutable objects, and finally updates the head conditional on the old head's GCS generation. Task creation uses ifGenerationMatch=0.

The conditional head update is the publishing point for a single task. A failed concurrent writer gets a conflict back; its modification is not automatically overwritten or silently replayed. Objects uploaded but not published are temporarily orphans; this version provides no automatic GC, to avoid mistakenly deleting objects that are still referenced.

Command idempotency cannot rely on remembering only the last command_id. A content-addressed persistent hash dictionary stores command_id → request hash, published version and event reference. The dictionary uses copy-on-write branch nodes/bounded leaf nodes, with a maximum depth of 64 over the SHA-256 hex path. The index root is published in the head together with the state. This way a duplicate command from long ago can still be checked, without scanning the whole history and without accumulating command IDs in a single head.

A repeated submission with the same command_id and the same request returns the original result; the same ID with a different request is rejected. After a publish response is lost, a retry first queries the published command index. There are no transactions across objects; content is written first and the reference published afterwards. A failed GCS request must not be declared a success.

This is not an exactly-once guarantee for external operations. Modifying PostgreSQL and publishing GCS state do not share a transaction. External executors must record the intent, check the real state before executing, and verify after executing; only operations with idempotency or result-checking capability can be recovered automatically. In this version, snapshot maintenance uses a pinned revision/table ID and reuses the idempotency of vc.materialize. The head CAS itself is not a fencing token for executors.

## Resident memory and change notifications

The runtime caches recently active tasks, limited both by number of entries and total bytes. State is returned as a copy, preventing callers from modifying the cache. The default cache refresh period is limited, and after it expires the task head is re-read. A request can explicitly ask for fresh, so that the published version is checked before important operations.

An optional GCS Pub/Sub pull subscription receives object change notifications and only handles head updates under this prefix. Duplicate or old generations do not downgrade the cache; notifications only invalidate state, and the next read checks against GCS. Periodic checks of the active tasks in memory cover missed notifications. The subscription callback does not run models and does not traverse the bucket; the subscription is provided by the operator, and the program does not create cloud resources.

On service restart, not all tasks are scanned; a task is recovered once the scheduler provides its task_id. Global task discovery, task assignment and a cross-node memory service are not within the scope of this version.

## Context interface

The context contains task_id, version, goal, status, next_action, uncertainties, policy_ref and state_ref. Structured facts and evidence references are added according to the budget. Omitted content must be reported, the complete state can be fetched by state_ref, and truncating key facts without notice is forbidden.

A hard budget in UTF-8 bytes is provided. When a tiktoken encoder is optionally configured, a token budget for that encoder is additionally supported; when it is not configured, claiming an exact token count is refused. This count covers only the returned package, not the system prompt of the calling model or other tool messages. When the required fields alone exceed the budget, an explicit error is returned so the caller can split the task or adjust the budget.

Agents can read specific evidence by hash. Evidence objects have upload and download limits, and data larger than this version's interface limit must be chunked; whole reports are not stuffed into the context by default. Rule files can likewise be saved as evidence objects with a content hash, with the state pinning a reference to their version. Rule content is a working basis provided to the manager Agent; the server does not automatically treat stored content as executable instructions.

## Interfaces and execution flow

The HTTP service provides task creation, conditional updates, state reads, context building, paginated history, evidence upload/read, metrics and a notification entry point. By default it listens only on loopback; non-loopback requires an access token to be configured, and external deployment also needs TLS or a controlled entry point. All connected Agents use the same set of capabilities, with no artificial split into developer/approver permissions.

Typical execution: read the pinned rules → create/recover the task → publish the intent → check the actual objects → perform idempotent maintenance → verify → publish the result. When context is lost, only the state and the necessary evidence are re-read; tasks are not recovered from chat history.

The PostgreSQL example continues to use the existing local service in connection.local.json. Cloud credentials use GCS Application Default Credentials and are not written into tasks, code or HTTP request bodies. The service does not implicitly create buckets, modify IAM, start/stop PostgreSQL, or treat the in-memory test backend as cloud persistence.

## Acceptance and observability

Record the number of logical object reads, writes, bytes transferred, CAS conflicts, cache hits, context size, and token counts when configured. Cloud network latency, billing, IAM and actual notification latency can only be reported after real integration testing.

Must test: ten writers competing for the same version can publish only one result; repeated requests (including old requests across later versions) do not re-execute state changes; the same command ID with a different request is rejected; failures before/after publishing can be determined; each published object is complete; content hash verification; cold recovery read volume does not grow with history length; the cache is bounded; stale/duplicate notifications are handled correctly; context budget/omission markers; history pagination; the recovery example for PostgreSQL pinned-version snapshots.

The pure in-memory object storage is only for fault injection and testing and does not write to local disk. Contract tests for the GCS adapter cannot replace integration testing against a real bucket. When the bucket or credentials are missing, record it as not verified; do not report cloud deployment as complete.

## References

- [GCS request preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions)
- [GCS single-object atomicity and consistency](https://docs.cloud.google.com/storage/docs/consistency)
- [GCS notification semantics](https://docs.cloud.google.com/storage/docs/pubsub-notifications)
- [Google Cloud ADC](https://docs.cloud.google.com/docs/authentication/provide-credentials-adc)
