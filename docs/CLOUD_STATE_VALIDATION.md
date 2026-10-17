# 状态模块本地测试范围（非云端验证）

更新日期：2026-10-17。**云服务未经真实验证，云端部署暂缓。** 本文不包含 GCS、OSS 或 S3 的真实读写、持久性、一致性、性能或部署结果。

本文仅说明本机代码测试的来源与范围；原始本地记录不作为云存储已验证的依据。

## 本地记录的含义

- [cloud-test-results.json](../cloud-test-results.json)：最初状态模块的单元测试记录。使用 MemoryStore、SDK 测试替身及本机 HTTP，未连接真实 GCS。
- [cloud-read-benchmark.json](../cloud-read-benchmark.json)：内存后端中的读取结构实验，计数不是云端实测流量，也不能推导云端延迟、吞吐或费用。
- [cloud-postgres-integration.json](../cloud-postgres-integration.json)：现有 Ubuntu WSL PostgreSQL 与内存状态后端的接手实验。`resumed.verified` 只表示已核验本机数据库快照，不能解释为云存储验证成功。

这些文件保留本地测试事实；文件名中的 cloud 是历史命名，不表示运行地点或真实云连接。后续适配器的本地范围见 [多后端本地测试](STORAGE_BACKENDS_VALIDATION.md)。

## 尚无结果的云端项目

真实桶访问与配置、IAM、条件发布、网络故障恢复、Pub/Sub、跨实例通知、长期运行、容量及费用均无真实验证结果。用户报告已创建 OSS 桶，也不等于本项目已连接或核验该桶。

云端入口与部署代码保留供以后使用，当前不运行云端测试或部署。现有本机 PostgreSQL 测试仍独立记录在 [本机版本引擎验证](../VALIDATION.md)，不扩展为云端结论。
