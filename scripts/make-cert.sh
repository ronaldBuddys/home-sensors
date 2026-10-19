#!/usr/bin/env bash
# Issue a TLS certificate from a small private CA kept in data/tls/ (git-ignored).
#   ./scripts/make-cert.sh web 192.0.2.10 myhost.local     -> data/tls/web.{crt,key}
#   ./scripts/make-cert.sh postgres 192.0.2.20             -> data/tls/postgres.{crt,key}
# The CA is created on first use (data/tls/ca.{crt,key}). Install ca.crt on the devices that
# open the dashboard (browsers then trust it) and use it as the Postgres sslrootcert.
# Every name/IP clients use to connect must be listed (verify-full checks it).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $# -ge 2 ]] || { sed -n '2,7p' "$0"; exit 2; }
name=$1; shift
[[ "$name" =~ ^[a-z0-9_-]+$ ]] || { echo "name must be [a-z0-9_-]+" >&2; exit 2; }
umask 077
dir=data/tls
mkdir -p "$dir"

if [[ ! -f "$dir/ca.key" ]]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
    -subj "/CN=Home sensors local CA" -keyout "$dir/ca.key" -out "$dir/ca.crt" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
  echo "created CA $dir/ca.crt"
fi

san=""
for h in "$@"; do
  if [[ "$h" =~ ^[0-9.]+$ || "$h" == *:* ]]; then san+="IP:$h,"; else san+="DNS:$h,"; fi
done
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$1" \
  -keyout "$dir/$name.key" -out "$dir/$name.csr"
# 825 days: the longest validity Apple devices accept for a private-CA server certificate
openssl x509 -req -in "$dir/$name.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" -CAcreateserial \
  -days 825 -out "$dir/$name.crt" \
  -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "${san%,}")
rm -f "$dir/$name.csr"
echo "issued $dir/$name.crt for ${san%,} (key: $dir/$name.key)"
