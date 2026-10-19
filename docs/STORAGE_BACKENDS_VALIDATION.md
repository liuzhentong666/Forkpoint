中文 | [English](#english-version)

# 存储适配器本地测试范围（非云端验证）

更新日期：2026-10-17。**云服务未经真实验证，云端部署暂缓。** OSS、S3、GCS 的真实联调均未执行；没有可宣称云存储已验证的结果。

## 已有记录属于哪些环境

| 记录 | 实际执行环境 | 不包含的证据 |
|---|---|---|
| [storage-backend-test-results.json](../results/storage-backend-test-results.json) | 本机内存模型、官方 SDK 类型、捕获传输层及 boto3/botocore Stubber | 真实云服务响应、IAM 和桶配置 |
| [oss-protocol-benchmark.json](../results/oss-protocol-benchmark.json) | 内存 OSS 协议模型 | 真实 OSS 下载量、时延、吞吐、容量和费用 |
| [cloud-postgres-oss-protocol.json](../results/cloud-postgres-oss-protocol.json) | 现有 WSL PostgreSQL 与内存 OSS 协议模型 | PostgreSQL 与真实 OSS 的跨系统恢复 |

本地套件检查客户端条件参数、并发模型、响应丢失模拟、错误映射、缓存和配置兼容。SDK 能构造请求不代表真实提供商已接受请求；模型符合预期也不证明真实存储的一致性或可用性。

数据库接手记录中的 `resumed.verified` 仅针对本机 PostgreSQL 快照，`applied=false` 表示接手时未再次物化；不是 OSS 验证标记。原始数据保留为本地开发证据，不能改写成云端成功报告。

## 未完成且当前暂缓

真实 OSS / S3 / GCS 的读写、条件发布、旧命令重试、网络故障恢复、实际权限、保留策略、性能和成本均未验证。用户报告已有杭州 OSS 桶，但本项目未核验该桶；ECS 也未创建或部署。

适配代码及 `smoke_cloud.py` 保留，当前不执行真实云测试。接口和存储布局见 [多后端设计](STORAGE_BACKENDS.md)；不要把设计约束或模拟计数当作实测云端能力。

---

<a id="english-version"></a>

# Scope of the Storage Adapters' Local Tests (Not Cloud Verification)

Last updated: 2026-10-17. **Cloud services are not verified against real cloud services, and cloud deployment is on hold.** No real integration testing of OSS, S3 or GCS has been run; there are no results that could be claimed as cloud storage having been verified.

## Which environments the existing records belong to

| Record | Actual execution environment | Evidence not included |
|---|---|---|
| [storage-backend-test-results.json](../results/storage-backend-test-results.json) | Local in-memory model, official SDK types, captured transport layer and boto3/botocore Stubber | Real cloud service responses, IAM and bucket configuration |
| [oss-protocol-benchmark.json](../results/oss-protocol-benchmark.json) | In-memory OSS protocol model | Real OSS download volume, latency, throughput, capacity and cost |
| [cloud-postgres-oss-protocol.json](../results/cloud-postgres-oss-protocol.json) | Existing WSL PostgreSQL and the in-memory OSS protocol model | Cross-system recovery between PostgreSQL and a real OSS |

The local suite checks client conditional parameters, the concurrency model, simulated response loss, error mapping, caching and configuration compatibility. The SDK being able to construct a request does not mean the real provider has accepted it; the model behaving as expected also does not prove the consistency or availability of real storage.

`resumed.verified` in the database hand-off record refers only to the local PostgreSQL snapshot, and `applied=false` means no re-materialization happened during the hand-off; it is not an OSS verification marker. The raw data is kept as local development evidence and must not be rewritten into a cloud success report.

## Not completed and currently on hold

Real OSS / S3 / GCS reads/writes, conditional publishing, retries of old commands, network failure recovery, actual permissions, retention policies, performance and cost are all not verified. The user reports having a Hangzhou OSS bucket, but this project has not verified that bucket; no ECS has been created or deployed either.

The adapter code and `smoke_cloud.py` are kept, and no real cloud tests are currently run. For the interfaces and storage layout see [Multi-backend design](STORAGE_BACKENDS.md); do not take design constraints or simulated counts as measured cloud capability.
