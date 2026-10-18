English | [中文](STAGING.md)

# Staging PostgreSQL Connection (S1)

2026-10-18. Connection instructions for phase S1 of the [Production test plan](PRODUCTION_TEST_PLAN.en.md). The user has chosen a local virtual machine as the staging host; for the step-by-step operations see [Local VM steps](STAGING_VM.en.md); this document gives the general rules independent of the host form. **There is no staging host yet**: the code and scripts below have been verified locally as far as they can be (see the last section), and real S1 results can only be produced after the user provides a host. Cloud services are still not verified against real cloud services, and this phase does not involve object storage.

## Connection rules

`db.py` handles hosts in three cases:

| Host | Requirement |
|---|---|
| `127.0.0.1` / `localhost` / `::1` | Same as before; `sslmode` can optionally be configured |
| Unix socket directory starting with `/` | Native Linux, or the WSL transport on Windows |
| Other (remote) | Must be a single hostname or IP, with `sslmode` set to `verify-full` and `sslrootcert` pointing to the CA file that issued the server certificate; otherwise the connection is refused |

`verify-full` checks both the certificate chain and the hostname, so the configured hostname must appear in the server certificate's CN or SAN. TLS settings are taken only from the configuration file or the command line (`--sslmode`, `--sslrootcert`); inherited `PGSSLMODE`, `PGSSLROOTCERT`, `PGREQUIRESSL` and `PGSSLNEGOTIATION` are always cleared, so that the shell environment cannot quietly lower the verification level. When `PGHOST` points to a remote host, the same rules apply. When using the WSL transport, `sslrootcert`, like `psql`, is a path inside the distribution.

The output of `python db.py check` now includes `ssl`, `ssl_version` and `sslmode`; a remote connection that does not use TLS raises an error directly (which cannot happen under `verify-full` anyway).

## Preparation steps

The following are carried out by the staging server's administrator; this project's scripts do not modify the server configuration.

1. **Server certificate**: a certificate issued by a CA with the hostname written into the SAN; in `postgresql.conf` set `ssl = on`, `ssl_min_protocol_version = 'TLSv1.2'` or higher, and make `listen_addresses` include an address reachable by the client. Give the CA certificate (not the private key) to the client.
2. **pg_hba.conf**: open TLS connections only for the dedicated role, filling in the network range according to the actual client:

   ```text
   hostssl  all  agentdb_staging  10.0.0.0/24  scram-sha-256
   ```

   The database column is `all`, because each suite creates new `agentdb_test_*` databases every time.
3. **Dedicated role**: as a superuser, run [create_staging_role.sql](../deploy/staging/create_staging_role.sql), then set the password interactively in psql:

   ```console
   psql -X -v ON_ERROR_STOP=1 -d postgres -f deploy/staging/create_staging_role.sql
   psql -X -d postgres -c "\password agentdb_staging"
   ```

   `agentdb_staging` is not a superuser and cannot create roles; it has only `CREATEDB`; on `agentdb_client` and `agentdb_manager` it has only SET privilege and does not inherit their privileges. The script creates these two NOLOGIN roles in advance, because `install` creates them only when they do not exist, and creating roles requires CREATEROLE. The script can be run repeatedly.
4. **Client password**: write it into the client's PostgreSQL password file (Linux `~/.pgpass`, permissions 0600; native Windows client `%APPDATA%\postgresql\pgpass.conf`), or add `--password` on each run for hidden input. Do not write it into JSON, command arguments or chat.
5. **Client configuration**: copy [connection.staging.example.json](../connection.staging.example.json) to `connection.staging.local.json` (ignored by `.gitignore`), and fill in the hostname and the CA file path.
6. **Timeouts for vanished clients** (server): by default PostgreSQL takes about 2 hours 11 minutes to notice a client that vanished mid-transaction, and during that time the locks it holds (for example main's branch lock) remain in effect. Set `ALTER ROLE agentdb_staging SET idle_in_transaction_session_timeout = '60s'` for the dedicated role (already included in the [role script](../deploy/staging/create_staging_role.sql); it applies only to that role, and administrator sessions keep the default), and in `postgresql.conf` or its `conf.d` set:

   ```text
   tcp_keepalives_idle = '60s'
   tcp_keepalives_interval = '10s'
   tcp_keepalives_count = 6
   tcp_user_timeout = '120s'
   client_connection_check_interval = '10s'
   ```

   This way a vanished session idle inside a transaction is ended within 60 s, a connection to a silent peer is closed within about 120 s, and a running statement is cancelled within about 10 s after the client leaves (within about 130 s for a silent peer). All clients send one version transaction as one script, so the legitimate idle time inside a transaction is about one round trip, and the longest intentional pause (the lock-holding session in the network-cut drill) is about 4 s. For the basis of the values see [S4/S5 section 4](STAGING_S4_S5.en.md#4-risk-sessions-of-vanished-clients-hold-locks-for-a-long-time); on the local VM they are written by [configure_postgres.sh](../deploy/staging/configure_postgres.sh), and an already configured VM adds them following [section 4a](STAGING_VM.en.md#4a-add-the-vanished-client-settings-to-an-already-configured-vm-user-action).

On the client side: the ten-writer merge in `review_local.py` and `agent_lab.py merge` set `lock_timeout = '10s'` inside the merge transaction. When the main lock cannot be obtained in time, `lock_timeout` (SQLSTATE 55P03) is reported, which, like `stale_head` (40001), means main has not changed and the same proposal can be retried; it is not treated as `merge_conflict` (P0001). The server's `lock_timeout` stays at the default 0.

## S1 verification commands

```console
python db.py check --config connection.staging.local.json
python run_tests.py --config connection.staging.local.json
.\.venv-cloud\Scripts\python.exe review_local.py --config connection.staging.local.json
python check_merge_migration.py --config connection.staging.local.json
```

For the pass criteria see the S1 row of the plan: `check` shows `"ssl": true`, `"sslmode": "verify-full"`; the three suites all pass as `agentdb_staging`. The result files note the environment as "staging PostgreSQL" and are kept separately from the local records. The user decides when to clean up the test databases each suite keeps.

## What has been verified locally (2026-10-18)

There is no staging host on this machine, so the following checks can only prove the code paths and the scripts themselves; they cannot replace S1:

- **TLS path (real WSL PostgreSQL 16.15, over TCP 127.0.0.1)**: the server uses the Ubuntu self-signed certificate `LAPTOP-D0IU4SUK.localdomain`; TCP login requires a password, which was not set or requested this time. With this certificate as the CA and `verify-ca`, the TLS handshake and certificate verification pass, and the connection stops at the authentication step (`fe_sendauth: no password supplied`); with an unrelated CA it reports `certificate verify failed`; `verify-full` to `127.0.0.1` reports a certificate hostname mismatch. A remote hostname without TLS settings is refused directly by `db.py`. The default Unix socket connection is unchanged. The success path of `verify-full` needs a server listening under the certificate's hostname; the local service listens only on `localhost`, so it was not covered.
- **Settings checks**: [tests/test_db_connection.py](../tests/test_db_connection.py) has 10 items in total and needs no database: remote hosts must use `verify-full`, verification modes must have a CA file, host lists and connection strings are rejected, inherited TLS variables are cleared, TLS fields in the configuration file, and the WSL transport passes the TLS variables into the distribution.
- **Role script**: in the local cluster, the script was run twice in a row and then the role attributes and memberships were checked (not a superuser, only CREATEDB, SET rather than inheritance for both roles), confirming that it cannot create roles and can SET ROLE to both roles, then everything was ROLLBACKed; after the rollback `agentdb_staging` does not exist in the cluster.
- **Non-superuser installation**: in the newly created dedicated database `agentdb_test_staginginstall_7cac3bcb`, within one transaction, the role script was run, CREATE privilege on that database was granted, and after SET ROLE to `agentdb_staging` all of the 001–004 SQL was installed: the three schemas and `vc.merge` are all owned by that role, and `agentdb_client` can read head; then ROLLBACK, so neither the role nor the objects were kept, and the empty database is kept for inspection.

- **VM scripts (in WSL, not involving any PostgreSQL)**: both scripts pass syntax checks; the server certificate chain generated by [make_certs.sh](../deploy/staging/make_certs.sh) verifies, with SAN `DNS:agentdb-staging.test, IP:192.168.56.10`, limited to server authentication, private key 0600, and it refuses to overwrite an existing directory and invalid IPs/hostnames. Real TLS handshakes with `openssl s_server` / `s_client`: with the correct CA, both the IP and the hostname verify; a wrong IP reports an address mismatch, and a wrong CA reports that the issuer cannot be found, consistent with `verify-full`'s decisions. For [configure_postgres.sh](../deploy/staging/configure_postgres.sh) only the arguments and the up-front refusal "must be run as root" were checked, along with the public key comparison and `-checkip` decision it uses; it modifies the server configuration, and was not run on the existing WSL PostgreSQL.

- **Native Windows psql client (2026-10-18)**: EDB's PostgreSQL 16.15 installer was installed via winget, with the server, pgAdmin and Stack Builder components disabled, and the installer hash check passed. `bin` still contains the `postgres.exe` program file, but there is no data directory, no Windows service, and no process listening on 5432 on the Windows side, so there is no running server. Through `db.py` (with `wsl_distribution` left empty), Windows psql connected to the local WSL server (`127.0.0.1:5432` in mirrored mode), with results matching the WSL client: the correct CA with `verify-ca` reaches the authentication step (`fe_sendauth: no password supplied`); an unrelated CA reports `certificate verify failed`; `verify-full` to `127.0.0.1` reports a hostname mismatch. When connecting to `192.168.56.10` with `connection.staging.local.json`, `db.py` accepts the remote configuration and hands the connection to psql, which, since there is no VM yet, reports `timeout expired` after the 5-second connection timeout. Windows' neighbor table shows that the ARP requests for this address are sent from the VirtualBox host-only adapter (state Incomplete, i.e. no host has answered yet), showing that the routing on the Windows side is correct. (`Find-NetRoute` reports the default route via WLAN, which does not match the packets actually sent, and is not relied on.)

Not yet covered: logging in as `agentdb_staging` and running the three suites in full (needs a persistent role and password), the remote network, and certificates issued by a real CA. These belong to S1 itself.
