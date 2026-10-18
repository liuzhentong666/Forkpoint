English | [中文](STORAGE_BACKENDS_VALIDATION.md)

# Scope of the Storage Adapters' Local Tests (Not Cloud Verification)

Last updated: 2026-10-17. **Cloud services are not verified against real cloud services, and cloud deployment is on hold.** No real integration testing of OSS, S3 or GCS has been run; there are no results that could be claimed as cloud storage having been verified.

## Which environments the existing records belong to

| Record | Actual execution environment | Evidence not included |
|---|---|---|
| [storage-backend-test-results.json](../storage-backend-test-results.json) | Local in-memory model, official SDK types, captured transport layer and boto3/botocore Stubber | Real cloud service responses, IAM and bucket configuration |
| [oss-protocol-benchmark.json](../oss-protocol-benchmark.json) | In-memory OSS protocol model | Real OSS download volume, latency, throughput, capacity and cost |
| [cloud-postgres-oss-protocol.json](../cloud-postgres-oss-protocol.json) | Existing WSL PostgreSQL and the in-memory OSS protocol model | Cross-system recovery between PostgreSQL and a real OSS |

The local suite checks client conditional parameters, the concurrency model, simulated response loss, error mapping, caching and configuration compatibility. The SDK being able to construct a request does not mean the real provider has accepted it; the model behaving as expected also does not prove the consistency or availability of real storage.

`resumed.verified` in the database hand-off record refers only to the local PostgreSQL snapshot, and `applied=false` means no re-materialization happened during the hand-off; it is not an OSS verification marker. The raw data is kept as local development evidence and must not be rewritten into a cloud success report.

## Not completed and currently on hold

Real OSS / S3 / GCS reads/writes, conditional publishing, retries of old commands, network failure recovery, actual permissions, retention policies, performance and cost are all not verified. The user reports having a Hangzhou OSS bucket, but this project has not verified that bucket; no ECS has been created or deployed either.

The adapter code and `smoke_cloud.py` are kept, and no real cloud tests are currently run. For the interfaces and storage layout see [Multi-backend design](STORAGE_BACKENDS.en.md); do not take design constraints or simulated counts as measured cloud capability.
