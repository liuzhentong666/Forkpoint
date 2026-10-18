English | [中文](STAGING_S1_RESULTS.md)

# S1 Results: Staging PostgreSQL (Local Virtual Machine)

2026-10-18. Record of the S1 phase of the [Production test plan](PRODUCTION_TEST_PLAN.en.md). **S1 passed.** Cloud services are still not verified against real cloud services, and this phase does not involve object storage; the following are results on a local virtual machine, not production or cloud conclusions.

## Environment

| Item | Value |
|---|---|
| Host | Local VirtualBox 7.2.20 virtual machine `agent-db`: 2 vCPU, 2 GB memory, 20 GB dynamic disk; Ubuntu Server 24.04.5 (the ISO's SHA-256 was checked against the official SHA256SUMS) |
| Server | PostgreSQL 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1), configured by [configure_postgres.sh](../deploy/staging/configure_postgres.sh): listens on `localhost,192.168.56.10`, TLS 1.2 and above, `pg_hba` only allows `agentdb_staging` to log in via `hostssl` from 192.168.56.0/24 with scram-sha-256 |
| Certificates | A private P-256 CA generated in WSL by [make_certs.sh](../deploy/staging/make_certs.sh); the server certificate's SAN is `DNS:agentdb-staging.test, IP:192.168.56.10`; the CA private key never left WSL |
| Network | VirtualBox host-only network, 192.168.56.1 on the Windows side; WSL mirrored mode cannot see this adapter, so the client is the native Windows psql |
| Client | Native Windows psql 16.15 (client components only); `db.py` reads `connection.staging.local.json`: `sslmode=verify-full`, `sslrootcert` is that CA; the password comes from `%APPDATA%\postgresql\pgpass.conf` |
| Login identity | `agentdb_staging`: not a superuser, cannot create roles, only CREATEDB, with only SET privilege on `agentdb_client` and `agentdb_manager` ([role script](../deploy/staging/create_staging_role.sql)) |
| Code | On top of commit 90f2dbf, with no code changes |

## Results

| Check | Result | Raw record |
|---|---|---|
| `db.py check` | `ssl: true`, `TLSv1.3`, `sslmode: verify-full`, `user: agentdb_staging`, server 16.15 | [db-check.json](../staging-results/db-check.json) |
| `run_tests.py` | The original 18 assertions, the Session equivalence check and the 13 batched-merge cases all passed; 10 separate branches, 10 succeeded; competing on the same old head, 1 succeeded and 9 were rejected as stale versions. Test database `agentdb_test_273d7423471f` | [run-tests.json](../staging-results/run-tests.json) |
| `review_local.py` | All passed: the restricted management role rejected 7 kinds of operations with 42501; the two interruption windows (hand-off after intent `applied=true`, hand-off after commit `applied=false`) and the stale-executor conflict; all ten branches merged, verifying 100 rows changed, 3,900 rows unchanged, 10 files, 10 source commits reachable, baseline unchanged, no leftover transactions. Test database `agentdb_test_review_12b899a534` | [review-local.json](../staging-results/review-local.json) |
| `check_merge_migration.py` | After building a database with the old `001_core.sql` from commit 8aeb3d7, migrated twice in a row: data and history hashes unchanged, function attributes unchanged, the outputs of the 13 merge cases identical item by item, the 18 assertions all passed before and after the migration, and `agentdb_client` could still merge. Test database `agentdb_test_mergemig_ef8b856aa0` | [merge-batch-migration.json](../staging-results/merge-batch-migration.json) |

All of the above was done by the non-superuser `agentdb_staging` over a remote TLS connection, meeting the plan's pass criteria for S1. The `verify-full` success path, which locally could only be verified up to the authentication step, is actually covered here.

## Timing (for reference only)

In `review_local.py` the ten-branch merge phase took 941 ms, end to end 1,804 ms, with 45 stale-head retries (each writer using one long-lived connection). The client and server share the CPU and disk of the same laptop and go through a virtual network adapter and TLS; these numbers only show that the workflow works, are not a performance conclusion, and are not directly compared with the local WSL results.

## Problems found and handled along the way

- After a reboot the WSL mirrored network still did not mirror the VirtualBox host-only adapter, so the native Windows psql was used instead; the guide has been updated.
- `Test-NetConnection` / `Find-NetRoute` picked the WLAN source address for 192.168.56.10 and falsely reported the port as unreachable; the guide now uses a direct TCP connection check instead.
- When setting the role password, the password was once written directly into a `psql -c` command (and subsequently appeared in the chat, the VM's shell history and the server error log); it was changed to setting a different password with the interactive `\password`, and the original string is no longer used.

## What is kept and next steps

- Three test databases are kept on the VM (see the table above), as well as the snapshot `clean-postgres` (the state before configuration). Whether to clean them up is up to the user.
- The local original result files `test-results-local.json` and `local-review-results.json` were backed up before the run and restored after it; the staging results are saved separately in `staging-results/`.
- Not covered: certificates issued by a real CA, cross-machine networking, a production-shaped host. The next phase, S2 (real object storage), needs the user to lift the cloud hold first; S4 (load and growth) and S5 (failure drills) can continue on this VM, and the snapshot makes resetting easy.
