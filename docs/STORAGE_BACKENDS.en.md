English | [中文](STORAGE_BACKENDS.md)

# Replaceable Cloud Storage and the OSS Publishing Protocol

Last updated: 2026-10-17. **Cloud services are not verified against real cloud services, and cloud deployment is on hold.** Alibaba Cloud OSS is the domestic (China) adaptation target; the user reports having a Hangzhou OSS bucket, but the project has not verified its configuration, permissions or real reads/writes. This document only describes the adapter code and the protocol design; SDK test doubles and the in-memory service model do not constitute cloud verification.

## Boundaries and compatibility

Business versions still connect to the existing WSL PostgreSQL. This round only changes the storage adapters for management state, and does not modify the SQL version model, state object encoding, content hashes, command idempotency rules or the HTTP submission format.

The two capabilities are separated: immutable object storage is responsible for creating and reading content by key; HeadStore is responsible for reading and conditionally publishing the task entry point. Engine accepts a separate HeadStore; the original MemoryStore/GCSStore use an ordinary CAS wrapper, and old configurations that omit provider still select GCS.

The version marker returned by storage is an opaque value that is only handed back to the same backend for equality checks. HTTP read results keep the historical field name generation; GCS still returns an integer, and new backends may return a string; client submissions continue to use the application-level expected_version. Cache ordering uses the task's own version and does not compare ETag magnitudes. Task IDs do not support reuse after deletion, nor restoring an old head by bypassing the protocol.

## OSS interface reference and protocol design

The official PutObject documentation does not provide If-Match conditional overwrite, so we cannot pretend to be compatible with GCS generation CAS. AppendObject accepts position, and returns 409 when the position does not match the existing object length; this plan uses this conditional append mechanism to publish the task entry point. See [limits on conditional parameters](https://help.aliyun.com/zh/oss/user-guide/0017-00000245), [the append interface](https://help.aliyun.com/zh/oss/developer-reference/appendobject), [range reads](https://help.aliyun.com/zh/oss/developer-reference/getobject). The record format and recovery protocol below are this project's design; cloud verification can only be claimed as complete after passing tests against a real OSS.

Physical layout:

```text
objects/<sha256>                  原有不可变 JSON 对象
artifacts/<sha256>                原有不可变证据
tasks/<id>/head.json.append-v1    固定长度追加记录
```

(In the layout above: `objects/<sha256>` holds the existing immutable JSON objects, `artifacts/<sha256>` the existing immutable evidence, and `tasks/<id>/head.json.append-v1` the fixed-length append records.)

Each head record is a fixed 8,192 bytes, containing a format identifier, the payload length, a SHA-256 checksum, at most 4,096 bytes of head JSON, and zero padding. State and history are still kept in ordinary immutable objects; the append file only holds the small entry point.

A read first gets the append object's length, then range-reads the last complete record at that position; it does not download the whole append file, nor scan the bucket. Concurrent writers continuing to append do not change the record already selected. The returned position marker contains the end offset of that record; a later publish uses this offset to execute AppendObject. A successful non-empty append is the publishing point; competitors with old offsets fail. Creation starts at offset 0.

Publishing still first writes the complete state, events and command index, and appends the entry point last. When a response is lost, the new end position must not be substituted into a retry of the old request: re-enter Engine with the original command_id, and let the published index decide whether it completed. The SDK may also only retry the same position; the high-level append_file interface that automatically catches up to the position must not be used.

A read returns some complete version as of the time of the read; important write operations are ultimately decided as new or stale by the server-side conditional append. Misaligned lengths, wrong formats, checksum failures, non-Appendable objects and abnormal range responses are rejected directly, without falling back to an ordinary overwrite.

The first version sets a 4 GiB limit on each task's append file, i.e. at most 524,288 publishes. Before the limit is reached, the normal read volume does not change; when the limit is reached, new publishes are rejected and the old version is kept, without overwriting, deleting or pretending to succeed. When more publishes are needed, explicitly create a successor task and record the reference. Transparent log rotation is not within the scope of this round, and unlimited appends per task cannot be promised. Lifecycle rules must not delete/overwrite an entry point in use or the objects reachable from it.

The first adaptation is limited to ordinary OSS buckets: versioning has never been enabled, and no retention policy is in effect. The reason is that immutable object creation uses x-oss-forbid-overwrite, and in a bucket with versioning enabled or suspended this condition cannot provide this project's creation protection. The program checks for and rejects unsupported configurations, and does not modify the bucket configuration. After the check, operations staff must also not change these conditions on an active prefix. See [PutObject](https://help.aliyun.com/zh/oss/developer-reference/putobject).

This protocol targets writers that follow the project interface; it does not defend against external programs holding account permissions deleting/replacing the append file. Such behavior breaks the protocol's preconditions; the superuser Agent needs to maintain data through explicit migrations rather than bypassing the publishing entry point.

## S3 and GCS

GCS keeps generation conditional writes. S3 uses If-None-Match: * to create and If-Match: ETag to update; ETag is only a version condition and does not replace the project's SHA-256 content check. Only endpoints tested to satisfy conditional writes and read consistency are certified; an "S3 compatible" label is not treated as proof. See [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).

All backends check the size before and after downloading, and close the response stream. Errors must distinguish not-found, version races, permission/network failures and corruption; network uncertainty is left to idempotent recovery and must not be reported as an ordinary "not found". Each deployment selects only one authoritative backend, with no automatic dual writes or cross-cloud switching.

## Notifications and dependencies

Periodic reconciliation of active tasks and fresh reads are the general mechanism. Google Pub/Sub is only an optional acceleration for GCS; OSS/S3 do not depend on Google credentials or SDKs. General notifications only invalidate the task cache and re-read the authoritative state; tasks are not updated directly from message content. The old GCS notification entry point is kept for compatibility.

## Local test scope and the cloud steps on hold

1. The original 21 tests pass first, as the behavior baseline.
2. On the simulated OSS service, use the actual SDK request/response model: verify 10-writer contention, creation contention, lost responses, deduplication of old requests after later commits, detection of corruption/short reads, the length limit, and tail reads of large histories.
3. After splitting the interfaces, the original suite continues to pass; add tests for opaque markers, slow reads and cache freshness.
4. Each adapter checks the conditions, ranges, error mapping and size limits in SDK requests; the factory and CLI check the provider, region and credential boundaries.
5. The real cloud step has not been executed and is currently on hold; only when it resumes in the future will the key scenarios be checked under a random test prefix. Local tests cannot replace this step.

Test doubles only prove the client protocol and request construction; they cannot prove the consistency, network performance, cost or permission configuration of a real OSS. Results must be recorded separately.
