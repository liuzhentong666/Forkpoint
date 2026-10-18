中文 | [English](#english-version)

# 可替换云存储与 OSS 发布协议

更新日期：2026-10-17。**云服务未经真实验证，云端部署暂缓。** 阿里云 OSS 是国内适配目标；用户报告已有杭州 OSS 桶，但项目未核验配置、权限或真实读写。本文仅说明适配代码与协议设计，SDK 测试替身和内存服务模型不构成云端验证。

## 边界与兼容

业务版本仍连接现有 WSL PostgreSQL。本轮只改变管理状态的存储适配，不修改 SQL 版本模型、状态对象编码、内容哈希、命令幂等规则或 HTTP 提交格式。

把两种能力分开：不可变对象存储负责按键创建与读取内容；HeadStore 负责读取和条件发布任务入口。Engine 接受独立的 HeadStore，原有 MemoryStore/GCSStore 使用普通 CAS 包装，旧配置省略 provider 时仍选择 GCS。

存储返回的版本标记是不透明值，只交回相同后端进行相等性检查。HTTP 读取结果保留历史字段名 generation，GCS 仍返回整数，新后端可返回字符串；客户端提交继续使用应用层 expected_version。缓存排序使用任务自己的 version，不比较 ETag 大小。任务 ID 不支持删除后复用，也不支持绕过协议恢复旧 head。

## OSS 接口参考与协议设计

官方 PutObject 文档不提供 If-Match 条件覆盖，不能假装兼容 GCS generation CAS。AppendObject 接受 position，位置与现有对象长度不一致时返回 409；本方案用这一条件追加机制发布任务入口。参考 [条件参数限制](https://help.aliyun.com/zh/oss/user-guide/0017-00000245)、[追加接口](https://help.aliyun.com/zh/oss/developer-reference/appendobject)、[范围读取](https://help.aliyun.com/zh/oss/developer-reference/getobject)。以下记录格式与恢复协议是本项目设计，需通过真实 OSS 测试后才可声称云端验证完成。

物理布局：

```text
objects/<sha256>                  原有不可变 JSON 对象
artifacts/<sha256>                原有不可变证据
tasks/<id>/head.json.append-v1    固定长度追加记录
```

每条 head 记录固定 8,192 字节，包含格式标识、有效载荷长度、SHA-256 校验、最多 4,096 字节的 head JSON 和零填充。状态及历史仍放在普通不可变对象中，追加文件只保存小入口。

读取先取得追加对象长度，再范围读取该位置的最后一条完整记录；不下载整个追加文件，也不扫描 bucket。并发写者继续追加不会改变已经选定的记录。返回的位置标记包含该记录结束偏移；后续发布用此偏移执行 AppendObject。一条成功的非空追加即发布点；竞争者的旧偏移失败。创建从偏移 0 开始。

发布仍然先写完整状态、事件与命令索引，最后追加入口。响应丢失时不得把新的末尾位置代入旧请求重试：用原 command_id 重新进入 Engine，由已发布索引判断是否完成。SDK 也只能重试同一个 position；不得使用自动追赶位置的高级 append_file 接口。

读取返回的是读取期间某个完整版本；重要写操作最终由服务端条件追加判断新旧。错位长度、错误格式、校验失败、非 Appendable 对象和异常范围响应直接拒绝，不回退到普通覆盖。

第一版对每个任务追加文件设置 4 GiB 上限，即最多 524,288 次发布。到达上限前正常读取量不变；达到上限时拒绝新发布并保留旧版本，不进行覆盖、删除或伪装成功。需要更多发布时显式建立后继任务并记录引用。透明日志轮转不在本轮实现范围，不能承诺单任务无限追加。禁止生命周期规则删除/覆盖正在使用的入口及其可达对象。

首次适配限定普通 OSS bucket：版本控制从未开启、没有正在生效的保留策略。原因是不可变对象创建采用 x-oss-forbid-overwrite，该条件在开启或暂停版本控制的桶中不能承担本项目的创建保护。程序检查并拒绝不支持的配置，不会修改 bucket 配置。检查后运维也不得在活跃前缀上改变这些条件。参考 [PutObject](https://help.aliyun.com/zh/oss/developer-reference/putobject)。

该协议针对遵守项目接口的写者，不防御持有账户权限的外部程序删除/替换追加文件。此类行为破坏协议前提；superuser Agent 需要通过显式迁移维护数据，而不是绕过发布入口。

## S3 与 GCS

GCS 保留 generation 条件写入。S3 用 If-None-Match: * 创建、If-Match: ETag 更新；ETag 只是版本条件，不代替项目 SHA-256 内容校验。仅认证经测试满足条件写入与读取一致性的端点，不把“S3 兼容”标签视为证明。参考 [S3 条件写入](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)。

所有后端在下载前后检查大小，并关闭响应流。错误必须区分不存在、版本竞争、权限/网络失败与损坏；网络不确定性保留给幂等恢复，不能报告为普通的“未找到”。每次部署只选择一个权威后端，不自动双写或跨云切换。

## 通知与依赖

周期核对活跃任务及 fresh 读取是通用机制。Google Pub/Sub 仅作为 GCS 的可选加速；OSS/S3 不依赖 Google 凭据或 SDK。通用通知只使任务缓存失效，重新读取权威状态，不从消息内容直接更新任务。旧 GCS 通知入口保留兼容。

## 本地测试范围与暂缓的云端步骤

1. 原有 21 项测试先通过，作为行为基线。
2. 在 OSS 模拟服务上使用实际 SDK 请求/响应模型：验证 10 写者竞争、创建竞争、丢失响应、后续提交后的旧请求去重、损坏/短读检测、长度上限及大历史尾部读取。
3. 拆分接口后继续通过原有套件；增加不透明标记、慢读取与缓存新鲜度测试。
4. 每个适配器检查 SDK 请求中的条件、范围、错误映射与大小限制；工厂与 CLI 检查 provider、地域和凭据边界。
5. 真实云端步骤尚未执行，当前暂缓；将来恢复时才在随机测试前缀检查关键场景。本地测试不能代替该步骤。

测试替身只证明客户端协议和请求构造，不能证明真实 OSS 的一致性、网络性能、费用或权限配置。结果必须分别记录。

---

<a id="english-version"></a>

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
