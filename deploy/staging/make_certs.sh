#!/usr/bin/env bash
# Private CA and server certificate for the staging PostgreSQL VM (docs/STAGING_VM.md).
# Run on the client side (WSL or any Linux with OpenSSL 1.1.1+), not on the VM:
#   bash deploy/staging/make_certs.sh --name agentdb-staging.test --ip 192.168.56.10 --out ~/.agentdb-staging
# Copy only server.crt and server.key to the VM; ca.key stays in --out, and
# ca.pem is the sslrootcert for clients. Refuses to overwrite an existing --out.
set -euo pipefail

die() { echo "make_certs.sh: $*" >&2; exit 2; }
name="" ip="" out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) [ $# -ge 2 ] || die "--name needs a value"; name="$2"; shift 2 ;;
    --ip) [ $# -ge 2 ] || die "--ip needs a value"; ip="$2"; shift 2 ;;
    --out) [ $# -ge 2 ] || die "--out needs a value"; out="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$name" ] && [ -n "$out" ] || die "usage: make_certs.sh --name HOST [--ip IPv4] --out DIR"
[[ "$name" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || die "invalid host name: $name"
if [ -n "$ip" ]; then
  [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || die "invalid IPv4 address: $ip"
  for octet in "${BASH_REMATCH[@]:1}"; do [ "$octet" -le 255 ] || die "invalid IPv4 address: $ip"; done
fi
[ ! -e "$out" ] || { echo "make_certs.sh: $out already exists; refusing to overwrite keys" >&2; exit 1; }

umask 077
mkdir -p "$out"
cd "$out"
san="DNS:$name"
[ -z "$ip" ] || san="$san,IP:$ip"

# CA: two years, may only sign end-entity certificates.
openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -sha256 -days 730 \
  -subj "/CN=AgentDB staging CA" -keyout ca.key -out ca.pem \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

# Server: 397 days; verify-full matches the host name or IP against the SAN.
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -sha256 \
  -subj "/CN=$name" -keyout server.key -out server.csr 2>/dev/null
printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" \
  "extendedKeyUsage=serverAuth" "subjectAltName=$san" > server.ext
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -sha256 -days 397 \
  -extfile server.ext -out server.crt 2>/dev/null
rm -f server.csr server.ext
chmod 644 ca.pem server.crt
chmod 600 ca.key server.key

openssl verify -CAfile ca.pem server.crt
echo "Subject alternative names: $san"
echo "Client sslrootcert: $(pwd)/ca.pem"
echo "Copy to the VM: $(pwd)/server.crt and $(pwd)/server.key (keep ca.key here)"
