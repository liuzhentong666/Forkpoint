English | [中文](CLOUD_STATE_VALIDATION.md)

# Scope of the State Module's Local Tests (Not Cloud Verification)

Last updated: 2026-10-17. **Cloud services are not verified against real cloud services, and cloud deployment is on hold.** This document contains no results of real reads/writes, durability, consistency, performance or deployment for GCS, OSS or S3.

This document only describes the source and scope of the local code tests; the raw local records are not evidence that cloud storage has been verified.

## What the local records mean

- [cloud-test-results.json](../cloud-test-results.json): the unit test record of the original state module. Uses MemoryStore, SDK test doubles and local HTTP; no real GCS was connected.
- [cloud-read-benchmark.json](../cloud-read-benchmark.json): a read-structure experiment in the in-memory backend; the counts are not measured cloud traffic, and cloud latency, throughput or cost cannot be derived from them.
- [cloud-postgres-integration.json](../cloud-postgres-integration.json): a hand-off experiment between the existing Ubuntu WSL PostgreSQL and the in-memory state backend. `resumed.verified` only means the local database snapshot was verified, and cannot be interpreted as successful cloud storage verification.

These files keep the facts of the local tests; "cloud" in the file names is historical naming and does not indicate where they ran or a real cloud connection. The local scope of the later adapters is in [Multi-backend local tests](STORAGE_BACKENDS_VALIDATION.en.md).

## Cloud items with no results yet

Real bucket access and configuration, IAM, conditional publishing, network failure recovery, Pub/Sub, cross-instance notifications, long-running operation, capacity and cost all have no real verification results. The user reporting that an OSS bucket has been created does not mean this project has connected to or verified that bucket.

The cloud entry points and deployment code are kept for later use; no cloud tests or deployments are currently run. The existing local PostgreSQL tests are still recorded separately in [Local version engine validation](../VALIDATION.en.md) and are not extended into cloud conclusions.
