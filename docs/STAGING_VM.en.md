English | [中文](STAGING_VM.md)

# Local Virtual Machine as the Staging PostgreSQL (Option B)

2026-10-18. The user chose to use a virtual machine on this Windows 11 laptop for [S1](PRODUCTION_TEST_PLAN.en.md): no cloud costs, and the cloud hold is not lifted. The PostgreSQL in the VM is completely separate from the existing WSL development database; the client is the native Windows psql (the local WSL's mirrored network cannot see the host-only adapter, see section 2), connecting over the virtual network, TLS `verify-full` and the dedicated role `agentdb_staging`. For the connection rules see [Staging connection](STAGING.en.md).

Limitation: the client and server share one machine's CPU and disk, so S1 is used only to prove correctness and the connection method, and its timings are not a performance conclusion (S4's timings must also note this).

## Agreed values

The following values are used below; if changed, the three places (certificate, server script, client configuration) must stay consistent.

| Item | Value |
|---|---|
| VM adapter 2 (host-only network) | Static `192.168.56.10/24`, `192.168.56.1` on the Windows side |
| Certificate hostname | `agentdb-staging.test` (written only into the certificate SAN; the client connects by IP, so the hosts file does not need changing) |
| PostgreSQL | 16.x shipped with Ubuntu 24.04, cluster `16/main`, port 5432 |
| Client network range | `192.168.56.0/24` |

## 1. Create the VM (user action)

1. Install VirtualBox 7.x: run `winget install --id Oracle.VirtualBox -e` in a Windows terminal and accept the administrator and driver prompts, or download it from the [official site](https://www.virtualbox.org/wiki/Downloads) (Windows 11 Home has no Hyper-V Manager; the free VMware Workstation Pro also works). The network drops briefly during installation while the host-only adapter is added; afterwards `VBoxManage list hostonlyifs` should show `192.168.56.1`. When WSL2 is enabled, VirtualBox runs on top of the Hyper-V platform, which is slower but usable; do not turn off WSL for this. Version 7.2.20 was installed on this machine on 2026-10-18.
2. Download the [Ubuntu Server 24.04 LTS](https://ubuntu.com/download/server) ISO and create a new VM: 2 vCPU, 2 GB memory and a 20 GB disk are enough (the test data is only 4,000 rows).
3. Adapters: adapter 1 is NAT (for installing packages); adapter 2 is "Host-only Network", choosing `VirtualBox Host-Only Ethernet Adapter` (default range 192.168.56.0/24, DHCP starts from .100, so .10 will not conflict).
4. When installing Ubuntu, tick OpenSSH server; choose any username, written below as `<vm-user>`.
5. In the VM, give adapter 2 a static address. First find its name with `ip -br link` (usually `enp0s8`), and write `/etc/netplan/60-hostonly.yaml`:

   ```yaml
   network:
     version: 2
     ethernets:
       enp0s8:
         addresses: [192.168.56.10/24]
   ```

   Then `sudo chmod 600 /etc/netplan/60-hostonly.yaml && sudo netplan apply`.
6. Install PostgreSQL: `sudo apt update && sudo apt install -y postgresql` (16.x on 24.04).
7. Take a snapshot of the VM in VirtualBox (for example "clean-postgres"). If configuration goes wrong later, or when doing the S5 failure drills, you can return here.

## 2. Check whether WSL can reach the VM (user action)

The local WSL uses mirrored networking mode (`networkingMode=mirrored` in `.wslconfig`). This mode mirrors the Windows adapters only when WSL starts, so a WSL that was already running before VirtualBox was installed cannot see the new host-only adapter: `ip route get 192.168.56.10` shows the default gateway via the ordinary network, not the 192.168.56.0/24 range. After installing VirtualBox, run `wsl --shutdown` once, and the next time WSL starts it mirrors again. This ends all WSL processes and terminals; the development PostgreSQL is set to start at boot in systemd and restarts along with WSL. After the restart, `wsl --distribution Ubuntu --exec ip -br -4 addr` should show `192.168.56.1`. **Measured on this machine on 2026-10-18: after the restart only the main adapter was still mirrored, the host-only adapter did not appear, and routing still went through the default gateway** (the development PostgreSQL recovered within 1 second). So this machine uses the native Windows psql approach below instead.

When using the WSL client, run the check below in a Windows terminal, and continue only if it outputs `reachable`:

```console
wsl --distribution Ubuntu --exec bash -c "timeout 3 bash -c '</dev/tcp/192.168.56.10/22' && echo reachable"
```

When using the native Windows client, connect directly to the SSH port in PowerShell, and continue only if it outputs `True`:

```console
$c = New-Object Net.Sockets.TcpClient; $c.ConnectAsync('192.168.56.10', 22).Wait(3000) -and $c.Connected; $c.Close()
```

Do not use `Test-NetConnection` or `Find-NetRoute` to decide: measured on this machine on 2026-10-18, they picked the WLAN source address for 192.168.56.10 and reported port 22 as unreachable, while a direct TCP connection had already received the VM's SSH banner (`SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20`), and the MAC for that address in the neighbor table matches the VM's adapter 2.

If it is still unreachable after restarting WSL (mirrored mode does not necessarily mirror all virtual adapters), use the native Windows psql as the client instead: install only the PostgreSQL client, and **do not install the Windows server** (in mirrored mode Windows and WSL share ports, so it would conflict with 5432 in WSL). The EDB installer can install only the command-line tools with `--mode unattended --disable-components server,pgAdmin,stackbuilder`; after installation confirm that `C:\Program Files\PostgreSQL\<版本>\bin\psql.exe` exists (`<版本>` = the version) and that no new `postgresql-x64-*` Windows service was added. In the client configuration leave `wsl_distribution` empty, set `psql` to the path of `psql.exe`, change `sslrootcert` to a Windows path (copy `ca.pem` to the Windows side), and use `%APPDATA%\postgresql\pgpass.conf` as the password file. This way traffic goes directly from Windows into the host-only adapter.

## 3. Generate certificates (in WSL, on the client side)

The CA private key stays on the client and is not put on the server:

```console
wsl --distribution Ubuntu --exec bash deploy/staging/make_certs.sh --name agentdb-staging.test --ip 192.168.56.10 --out /home/<wsl-user>/.agentdb-staging
```

Run it in the project root (WSL maps the current Windows directory to `/mnt/f/...`). The script refuses to overwrite an existing directory, and at the end checks the certificate chain with `openssl verify` and prints the SAN. Outputs: `ca.pem` (the client's sslrootcert), `ca.key` (stays only in this directory), and `server.crt` and `server.key` (copied to the VM).

The local client is the native Windows psql (see section 2), so copy the CA certificate (public content, not the private key) to the Windows side:

```console
wsl --distribution Ubuntu --exec bash -c "mkdir -p /mnt/c/Users/lzt/.agentdb-staging && cp ~/.agentdb-staging/ca.pem /mnt/c/Users/lzt/.agentdb-staging/ca.pem"
```

The private keys `ca.key` and `server.key` stay in WSL's ext4 file system (permissions 0600) and are not copied to `/mnt/c`, which does not keep Unix permissions.

## 4. Configure PostgreSQL in the VM

Copy the four files to the VM (run in WSL):

```console
scp /home/<wsl-user>/.agentdb-staging/server.crt /home/<wsl-user>/.agentdb-staging/server.key \
    deploy/staging/configure_postgres.sh deploy/staging/create_staging_role.sql <vm-user>@192.168.56.10:~/
```

In the VM:

```console
sudo bash configure_postgres.sh --cert server.crt --key server.key \
  --listen 192.168.56.10 --client-cidr 192.168.56.0/24 --role-sql create_staging_role.sql
sudo -u postgres psql -X -d postgres -c '\password agentdb_staging'
shred -u server.key
```

What [configure_postgres.sh](../deploy/staging/configure_postgres.sh) changes: installs the certificate pair (private key 0600, owned by postgres); writes `conf.d/agentdb-staging.conf` (`listen_addresses = 'localhost,192.168.56.10'`, `ssl = on`, TLS 1.2 and above, scram-sha-256, and the timeouts for vanished clients: `idle_in_transaction_session_timeout` 60 s, TCP keepalive 60 s / 10 s / 6 probes, `tcp_user_timeout` 120 s, `client_connection_check_interval` 10 s; for the basis of the values see [S4/S5 section 4](STAGING_S4_S5.en.md#4-risk-sessions-of-vanished-clients-hold-locks-for-a-long-time)); backs up and then appends one line `hostssl all agentdb_staging 192.168.56.0/24 scram-sha-256` to `pg_hba.conf`; when ufw is enabled, allows only that range to 5432; restarts the cluster and confirms that `ssl` is on and that every item in the `conf.d` file has taken effect (no errors in `pg_file_settings` and not overridden by a later file); runs the role script. It does not set a password. Deleting `conf.d/agentdb-staging.conf` and that pg_hba line undoes it, or simply return to the snapshot.

## 4a. Add the vanished-client settings to an already configured VM (user action)

VMs configured before 2026-10-18 (including the snapshot `s1-configured`) do not yet have the timeouts above in their `conf.d` file, so a vanished client can hold the main lock for about 2 hours 11 minutes (see [S4/S5 section 4](STAGING_S4_S5.en.md#4-risk-sessions-of-vanished-clients-hold-locks-for-a-long-time)). The script can be run repeatedly, and running it once more adds them; without `--cert`/`--key` it keeps the already installed certificate pair (section 4 already `shred`ded the `server.key` in the home directory). The existing line in `pg_hba.conf` is not appended again, ufw skips rules that already exist, the role script can be run repeatedly, and the password does not change.

1. Copy the new scripts to the VM. The local WSL cannot reach the host-only network (section 2), so run this in Windows PowerShell:

   ```console
   scp F:6-10-14\https-www-anthropic-com-engineering-redesigninggentdb-postgres\deploy\staging\configure_postgres.sh F:6-10-14\https-www-anthropic-com-engineering-redesigninggentdb-postgres\deploy\staging\create_staging_role.sql <vm-user>@192.168.56.10:~/
   ```

   (Translator's note: in the original Chinese document this command contains corrupted characters where the path segments `\2026` and `\agentdb` should be; the intended paths are `F:\2026-10-14\https-www-anthropic-com-engineering-redesigning\agentdb-postgres\deploy\staging\configure_postgres.sh` and `...\deploy\staging\create_staging_role.sql`. The command is reproduced here exactly as in the original.)

   In an environment using the WSL client where WSL can reach the VM, you can instead run `scp deploy/staging/configure_postgres.sh deploy/staging/create_staging_role.sql <vm-user>@192.168.56.10:~/` in WSL.

2. Run it in the VM. **It restarts PostgreSQL and drops all connections, so do not run it while a drill is in progress**:

   ```console
   sudo bash configure_postgres.sh --listen 192.168.56.10 --client-cidr 192.168.56.0/24 --role-sql create_staging_role.sql
   ```

   The last line should be `Vanished clients: agentdb_staging idle_in_transaction_session_timeout=60s; TCP keepalive 60 s, then 6 probes 10 s apart.` (this timeout is set only on the `agentdb_staging` role); if a setting has not taken effect, the script exits with the error `settings in … not in effect`.

3. On Windows, check over TLS as `agentdb_staging` (the Windows psql only recognizes options placed before the connection string, so `-XAtc` goes first; keepalive values are shown only on TCP connections and are 0 when viewed inside the VM over the Unix socket; paths in the connection string use forward slashes, because libpq treats backslashes as escapes):

   ```console
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -XAtc "SELECT name, setting, unit FROM pg_settings WHERE name ~ '^(idle_in_transaction|tcp_|client_connection)' ORDER BY 1" "host=192.168.56.10 user=agentdb_staging dbname=postgres sslmode=verify-full sslrootcert=C:/Users/lzt/.agentdb-staging/ca.pem"
   ```

   You should see `client_connection_check_interval|10000|ms`, `idle_in_transaction_session_timeout|60000|ms`, `tcp_keepalives_count|6|`, `tcp_keepalives_idle|60|s`, `tcp_keepalives_interval|10|s`, `tcp_user_timeout|120000|ms`.

4. Run the network-cut drill, and judge it by the pass criteria in [S4/S5 section 4](STAGING_S4_S5.en.md#pass-criteria-for-the-network-cut-drill).

To undo: delete the lines from "Bounded detection of vanished clients" downwards in `conf.d/agentdb-staging.conf` and then `sudo systemctl restart postgresql@16-main`, or return to the snapshot `s1-configured`. Whether to take another powered-off snapshot as a new drill reset point after the settings take effect is up to the user.

## 5. Client configuration (native Windows psql)

1. **Install only the PostgreSQL 16 client** (the user runs this in a Windows terminal; an administrator confirmation pops up):

   ```console
   winget install --id PostgreSQL.PostgreSQL.16 -e --source winget --override "--mode unattended --unattendedmodeui minimal --disable-components server,pgAdmin,stackbuilder"
   ```

   winget downloads EDB's official installer `postgresql-16.15-5-windows-x64.exe` (`--override` replaces its default arguments), keeping only the command-line tools, installed to `C:\Program Files\PostgreSQL\16`. No server is installed, so no superuser password is needed, and no Windows service is registered or port 5432 taken. After installation the following should hold: `"C:\Program Files\PostgreSQL\16\bin\psql.exe" --version` prints 16.x; `Get-Service postgresql*` returns nothing. `bin` will still contain the `postgres.exe` program file, but no `data` directory or service is created, and nothing listens on 5432. This machine was installed this way on 2026-10-18 (16.15); for the verification results see the last section of [Staging connection](STAGING.en.md).
2. **Password**: write it into `%APPDATA%\postgresql\pgpass.conf` (create the directory and file if they do not exist), as one line `192.168.56.10:5432:*:agentdb_staging:<口令>` (`<口令>` = the password); or add `--password` on each run for hidden input.
3. **Connection configuration** `connection.staging.local.json` (ignored by `.gitignore`; already created on this machine with the content below):

   ```json
   {
     "host": "192.168.56.10",
     "port": 5432,
     "user": "agentdb_staging",
     "database": "agentdb_staging",
     "maintenance_database": "postgres",
     "psql": "C:\\Program Files\\PostgreSQL\\16\\bin\\psql.exe",
     "wsl_distribution": "",
     "wsl_user": "",
     "sslmode": "verify-full",
     "sslrootcert": "C:\\Users\\lzt\\.agentdb-staging\\ca.pem"
   }
   ```

   Leaving `wsl_distribution` empty uses the native Windows psql, so traffic goes directly from Windows into the host-only adapter; `db.py` hands `sslmode` and `sslrootcert` to psql as `PGSSLMODE` and `PGSSLROOTCERT`. The development `connection.local.json` (WSL transport, Unix socket) is unchanged.

## 6. Run S1

Run the four commands in [Staging connection](STAGING.en.md#s1-verification-commands). `db.py check` should show `"ssl": true`, `"ssl_version": "TLSv1.3"` (or 1.2), `"sslmode": "verify-full"`, `"user": "agentdb_staging"`. When all four commands pass, S1 is complete; save the results separately as a staging record, apart from the local records.

## What has been verified locally

Without a VM the scripts can only be partly checked; see the last section of [Staging connection](STAGING.en.md). `configure_postgres.sh` modifies the server configuration and was not run on the existing WSL PostgreSQL; its first real run is in the VM.
