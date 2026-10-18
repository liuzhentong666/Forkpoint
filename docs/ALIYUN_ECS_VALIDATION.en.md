English | [中文](ALIYUN_ECS_VALIDATION.md)

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

Bucket permissions and configuration, real reads/writes and conditional appends, ECS identity and long-term renewal, dependency installation on Ubuntu, systemd start/stop, restart recovery, SSH tunnels and long-running behavior are all not verified in a real environment. For the deployment draft see [the ECS plan on hold](ALIYUN_ECS_DEPLOYMENT.en.md).
