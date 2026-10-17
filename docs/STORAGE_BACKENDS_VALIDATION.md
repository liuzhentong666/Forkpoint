# 存储适配器本地测试范围（非云端验证）

更新日期：2026-10-17。**云服务未经真实验证，云端部署暂缓。** OSS、S3、GCS 的真实联调均未执行；没有可宣称云存储已验证的结果。

## 已有记录属于哪些环境

| 记录 | 实际执行环境 | 不包含的证据 |
|---|---|---|
| [storage-backend-test-results.json](../storage-backend-test-results.json) | 本机内存模型、官方 SDK 类型、捕获传输层及 boto3/botocore Stubber | 真实云服务响应、IAM 和桶配置 |
| [oss-protocol-benchmark.json](../oss-protocol-benchmark.json) | 内存 OSS 协议模型 | 真实 OSS 下载量、时延、吞吐、容量和费用 |
| [cloud-postgres-oss-protocol.json](../cloud-postgres-oss-protocol.json) | 现有 WSL PostgreSQL 与内存 OSS 协议模型 | PostgreSQL 与真实 OSS 的跨系统恢复 |

本地套件检查客户端条件参数、并发模型、响应丢失模拟、错误映射、缓存和配置兼容。SDK 能构造请求不代表真实提供商已接受请求；模型符合预期也不证明真实存储的一致性或可用性。

数据库接手记录中的 `resumed.verified` 仅针对本机 PostgreSQL 快照，`applied=false` 表示接手时未再次物化；不是 OSS 验证标记。原始数据保留为本地开发证据，不能改写成云端成功报告。

## 未完成且当前暂缓

真实 OSS / S3 / GCS 的读写、条件发布、旧命令重试、网络故障恢复、实际权限、保留策略、性能和成本均未验证。用户报告已有杭州 OSS 桶，但本项目未核验该桶；ECS 也未创建或部署。

适配代码及 `smoke_cloud.py` 保留，当前不执行真实云测试。接口和存储布局见 [多后端设计](STORAGE_BACKENDS.md)；不要把设计约束或模拟计数当作实测云端能力。
