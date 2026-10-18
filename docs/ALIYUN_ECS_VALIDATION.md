中文 | [English](#english-version)

# 部署脚本本地检查范围（未部署 ECS）

更新日期：2026-10-17。**云服务未经真实验证，云端部署暂缓。** 没有 ECS 实例，状态服务未在阿里云运行，也未连接真实 OSS 完成测试。本文仅说明本地代码与脚本检查的范围。

## 本地工作能说明什么

现有测试代码位于 `tests/test_ecs_deployment.py`。其运行对象是本机回环 HTTP、内存状态后端、凭据测试替身和本地归档，不是真实 ECS、IMDS 或 OSS。

- HTTP 测试检查客户端请求、错误映射、并发模型及模拟的响应丢失。
- 凭据测试检查如何向官方 SDK 取值；未取得或刷新真实 ECS 临时凭据。
- 维护接手测试使用数据库替身；不构成真实云与 PostgreSQL 的联调。
- 归档和脚本检查仅涉及文件清单、哈希、入口加载与 Bash 语法，不能证明服务器上能够安装或常驻运行。

这些本地工作不作为云服务验证结论。代码、测试和部署草案保留，当前不采购、安装或启动云资源。

## 真实环境中的未验证项

桶权限与配置、真实读写与条件追加、ECS 身份与长期续期、Ubuntu 上依赖安装、systemd 启停、重启恢复、SSH 隧道和长期运行表现，均未经真实验证。部署草案见 [暂缓的 ECS 方案](ALIYUN_ECS_DEPLOYMENT.md)。

---

<a id="english-version"></a>

# Scope of the Local Check of the Deployment Scripts (ECS Not Deployed)

Last updated: 2026-10-17. **Cloud services are not verified against real cloud services, and cloud deployment is on hold.** There is no ECS instance, the state service has not run on Alibaba Cloud, and no test has been completed against a real OSS. This document only describes the scope of the local code and script checks.

## What the local work can show

The existing test code is in `tests/test_ecs_deployment.py`. What it runs against is local loopback HTTP, the in-memory state backend, credential test doubles and local archives, not a real ECS, IMDS or OSS.

- The HTTP tests check client requests, error mapping, the concurrency model and simulated response loss.
- The credential tests check how values are obtained from the official SDK; no real ECS temporary credentials were obtained or refreshed.
- The maintenance hand-off test uses a database double; it does not constitute integration testing between a real cloud and PostgreSQL.
- The archive and script checks only cover the file list, hashes, entry-point loading and Bash syntax, and cannot prove that installation or long-running operation works on a server.

This local work is not taken as a conclusion that cloud services are verified. The code, tests and deployment draft are kept; no cloud resources are currently being purchased, installed or started.

## Items not verified in a real environment

Bucket permissions and configuration, real reads/writes and conditional appends, ECS identity and long-term renewal, dependency installation on Ubuntu, systemd start/stop, restart recovery, SSH tunnels and long-running behavior are all not verified in a real environment. For the deployment draft see [the ECS plan on hold](ALIYUN_ECS_DEPLOYMENT.md).
