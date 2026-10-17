#!/usr/bin/env bash
# Configure PostgreSQL on the staging VM for TLS-only remote access by agentdb_staging.
# Run on the VM as root, from a directory holding the copied files (docs/STAGING_VM.md):
#   sudo bash configure_postgres.sh --cert server.crt --key server.key \
#     --listen 192.168.56.10 --client-cidr 192.168.56.0/24 --role-sql create_staging_role.sql
# It installs the certificate pair, writes conf.d/agentdb-staging.conf, appends one
# hostssl line to pg_hba.conf (after a timestamped backup), restarts the cluster,
# opens the port in ufw if ufw is active, and runs the role script. It never sets
# a password. Rerunning replaces the certificate pair and the conf.d file only.
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
[ -n "$cert" ] && [ -n "$key" ] && [ -n "$listen" ] && [ -n "$cidr" ] && [ -n "$rolesql" ] ||
  die "usage: configure_postgres.sh --cert FILE --key FILE --listen IPv4 --client-cidr CIDR --role-sql FILE"
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
[[ "$version" =~ ^1[6-9]$ ]] || die "PostgreSQL 16 or later is required"
[[ "$listen" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--listen must be the VM's IPv4 address"
[[ "$cidr" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/([0-9]|[12][0-9]|3[0-2])$ ]] || die "--client-cidr must look like 192.168.56.0/24"
[ "$cidr" != "0.0.0.0/0" ] || die "refusing to open PostgreSQL to every address"
for file in "$cert" "$key" "$rolesql"; do [ -r "$file" ] || die "cannot read $file"; done

dir="/etc/postgresql/$version/main"
[ -d "$dir" ] || die "cluster $version/main not found (apt install postgresql-$version)"
grep -Eq "^[[:space:]]*include_dir[[:space:]]*=[[:space:]]*'conf\.d'" "$dir/postgresql.conf" ||
  die "$dir/postgresql.conf has no include_dir = 'conf.d'"
ip -4 -o addr show | grep -q " inet $listen/" || die "$listen is not an address of this machine"

# The key must belong to the certificate, and the certificate must name the listen address.
[ "$(openssl x509 -in "$cert" -noout -pubkey)" = "$(openssl pkey -in "$key" -pubout)" ] ||
  die "$key does not match $cert"
openssl x509 -in "$cert" -noout -checkip "$listen" | grep -q "does match" ||
  echo "warning: $cert has no IP:$listen in its SAN; clients must connect by its DNS name" >&2

install -o postgres -g postgres -m 0644 "$cert" "$dir/agentdb-staging.crt"
install -o postgres -g postgres -m 0600 "$key" "$dir/agentdb-staging.key"
install -d -o postgres -g postgres -m 0755 "$dir/conf.d"
cat > "$dir/conf.d/agentdb-staging.conf" <<EOF
# Written by deploy/staging/configure_postgres.sh; remove this file to undo.
listen_addresses = 'localhost,$listen'
ssl = on
ssl_cert_file = '$dir/agentdb-staging.crt'
ssl_key_file = '$dir/agentdb-staging.key'
ssl_min_protocol_version = 'TLSv1.2'
password_encryption = 'scram-sha-256'
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
sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -d postgres < "$rolesql"

echo "PostgreSQL $version listens on localhost and $listen with TLS; agentdb_staging may connect from $cidr."
echo "Set its password now (typed, not stored here):"
echo "  sudo -u postgres psql -X -d postgres -c '\\password agentdb_staging'"
