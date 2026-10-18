#!/usr/bin/env bash
# Configure PostgreSQL on the staging VM for TLS-only remote access by agentdb_staging.
# Run on the VM as root, from a directory holding the copied files (docs/STAGING_VM.md):
#   sudo bash configure_postgres.sh --cert server.crt --key server.key \
#     --listen 192.168.56.10 --client-cidr 192.168.56.0/24 --role-sql create_staging_role.sql
# It installs the certificate pair, writes conf.d/agentdb-staging.conf (TLS and the
# vanished-client timeouts), appends one hostssl line to pg_hba.conf (after a
# timestamped backup), restarts the cluster, opens the port in ufw if ufw is active,
# and runs the role script. It never sets a password. Rerunning replaces the
# certificate pair and the conf.d file only; leave out --cert and --key to keep the
# pair already installed (e.g. to apply new conf.d settings after shred -u server.key):
#   sudo bash configure_postgres.sh --listen 192.168.56.10 --client-cidr 192.168.56.0/24 \
#     --role-sql create_staging_role.sql
set -euo pipefail

die() { echo "configure_postgres.sh: $*" >&2; exit 2; }
version=16 cert="" key="" listen="" cidr="" rolesql=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version|--cert|--key|--listen|--client-cidr|--role-sql)
      [ $# -ge 2 ] || die "$1 needs a value" ;;&
    --version) version="$2"; shift 2 ;;
    --cert) cert="$2"; shift 2 ;;
    --key) key="$2"; shift 2 ;;
    --listen) listen="$2"; shift 2 ;;
    --client-cidr) cidr="$2"; shift 2 ;;
    --role-sql) rolesql="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$listen" ] && [ -n "$cidr" ] && [ -n "$rolesql" ] ||
  die "usage: configure_postgres.sh [--cert FILE --key FILE] --listen IPv4 --client-cidr CIDR --role-sql FILE"
{ [ -n "$cert" ] && [ -n "$key" ]; } || { [ -z "$cert" ] && [ -z "$key" ]; } ||
  die "--cert and --key go together (leave both out to keep the installed pair)"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[[ "$version" =~ ^1[6-9]$ ]] || die "PostgreSQL 16 or later is required"
[[ "$listen" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--listen must be the VM's IPv4 address"
[[ "$cidr" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/([0-9]|[12][0-9]|3[0-2])$ ]] || die "--client-cidr must look like 192.168.56.0/24"
[ "$cidr" != "0.0.0.0/0" ] || die "refusing to open PostgreSQL to every address"

dir="/etc/postgresql/$version/main"
[ -d "$dir" ] || die "cluster $version/main not found (apt install postgresql-$version)"
reuse=""
if [ -z "$cert" ]; then  # keep the pair a previous run installed
  cert="$dir/agentdb-staging.crt" key="$dir/agentdb-staging.key" reuse=1
  [ -r "$cert" ] && [ -r "$key" ] || die "no installed certificate pair in $dir; pass --cert and --key"
fi
for file in "$cert" "$key" "$rolesql"; do [ -r "$file" ] || die "cannot read $file"; done
grep -Eq "^[[:space:]]*include_dir[[:space:]]*=[[:space:]]*'conf\.d'" "$dir/postgresql.conf" ||
  die "$dir/postgresql.conf has no include_dir = 'conf.d'"
ip -4 -o addr show | grep -q " inet $listen/" || die "$listen is not an address of this machine"

# The key must belong to the certificate, and the certificate must name the listen address.
[ "$(openssl x509 -in "$cert" -noout -pubkey)" = "$(openssl pkey -in "$key" -pubout)" ] ||
  die "$key does not match $cert"
openssl x509 -in "$cert" -noout -checkip "$listen" | grep -q "does match" ||
  echo "warning: $cert has no IP:$listen in its SAN; clients must connect by its DNS name" >&2

if [ -z "$reuse" ]; then
  install -o postgres -g postgres -m 0644 "$cert" "$dir/agentdb-staging.crt"
  install -o postgres -g postgres -m 0600 "$key" "$dir/agentdb-staging.key"
fi
install -d -o postgres -g postgres -m 0755 "$dir/conf.d"
cat > "$dir/conf.d/agentdb-staging.conf" <<EOF
# Written by deploy/staging/configure_postgres.sh; remove this file to undo.
listen_addresses = 'localhost,$listen'
ssl = on
ssl_cert_file = '$dir/agentdb-staging.crt'
ssl_key_file = '$dir/agentdb-staging.key'
ssl_min_protocol_version = 'TLSv1.2'
password_encryption = 'scram-sha-256'

# Bounded detection of vanished clients; values justified in docs/STAGING_S4_S5.md
# section 4. Every client sends a versioned transaction as one script, so a live
# client is idle inside it for about one round trip; the longest deliberate gap
# (staging_drills.py netcut's lock holder) is about 4 s. The 60 s
# idle_in_transaction_session_timeout is set on the agentdb_staging role by
# create_staging_role.sql, so admin sessions and other roles keep the default.
# A silent peer on an idle connection: first probe after 60 s, closed after 6 unanswered
# probes 10 s apart (about 120 s); a host that is up but lost the socket answers the first
# probe with a reset. tcp_user_timeout gives the same bound while sent data is unacknowledged.
tcp_keepalives_idle = '60s'
tcp_keepalives_interval = '10s'
tcp_keepalives_count = 6
tcp_user_timeout = '120s'
# While a statement runs, check every 10 s whether the client is still connected.
client_connection_check_interval = '10s'
EOF
chown postgres:postgres "$dir/conf.d/agentdb-staging.conf"
chmod 0644 "$dir/conf.d/agentdb-staging.conf"

# Only TLS connections from the client network, only for the staging role.
line="hostssl all agentdb_staging $cidr scram-sha-256"
if ! grep -Fxq "$line" "$dir/pg_hba.conf"; then
  cp -p "$dir/pg_hba.conf" "$dir/pg_hba.conf.agentdb-backup-$(date +%Y%m%d%H%M%S)"
  printf '\n# agentdb staging (deploy/staging/configure_postgres.sh)\n%s\n' "$line" >> "$dir/pg_hba.conf"
fi

if command -v ufw >/dev/null && ufw status | grep -q "^Status: active"; then
  ufw allow from "$cidr" to "$listen" port 5432 proto tcp
fi

# listen_addresses needs a restart, not a reload.
systemctl restart "postgresql@$version-main"
for _ in $(seq 1 30); do
  sudo -u postgres pg_isready -q -p 5432 && break
  sleep 1
done
sudo -u postgres pg_isready -q -p 5432 || die "PostgreSQL did not come back; see journalctl -u postgresql@$version-main"
[ "$(sudo -u postgres psql -XAtc 'SHOW ssl')" = "on" ] || die "ssl is not on after restart"
# Every line of the conf.d file must be in effect: not rejected, and not overridden by
# a later file (for example ALTER SYSTEM's postgresql.auto.conf).
rejected="$(sudo -u postgres psql -XAtc "SELECT string_agg(name || ': ' || coalesce(error, 'overridden'), ', ')
  FROM pg_file_settings WHERE sourcefile = '$dir/conf.d/agentdb-staging.conf' AND (error IS NOT NULL OR NOT applied)")"
[ -z "$rejected" ] || die "settings in $dir/conf.d/agentdb-staging.conf not in effect: $rejected"
sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -d postgres < "$rolesql"

echo "PostgreSQL $version listens on localhost and $listen with TLS; agentdb_staging may connect from $cidr."
echo "Vanished clients: agentdb_staging $(sudo -u postgres psql -XAtc \
  "SELECT coalesce((SELECT array_to_string(setconfig, ',') FROM pg_db_role_setting
     WHERE setrole = 'agentdb_staging'::regrole AND setdatabase = 0), 'has no role settings')"); \
TCP keepalive 60 s, then 6 probes 10 s apart."
echo "Set its password now (typed, not stored here):"
echo "  sudo -u postgres psql -X -d postgres -c '\\password agentdb_staging'"
