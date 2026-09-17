#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
require_root; load_config
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
umask 077
secret_dir=/etc/elektro/secrets
install -d -m 0700 "$secret_dir"
tmp=$(mktemp -d "$secret_dir/.ca-XXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
for ns in cert-manager gateway-system; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done
# Only NotFound means absent. Network/RBAC errors must preserve the local CA.
existing=$(kubectl -n cert-manager get secret internal-ca --ignore-not-found -o json)
if [[ -n $existing ]]; then
  jq -er '.data["tls.crt"]' <<< "$existing" | base64 -d > "$tmp/ca.crt"
  jq -er '.data["tls.key"]' <<< "$existing" | base64 -d > "$tmp/ca.key"
else
  if [[ -s $secret_dir/ca.key && -s $secret_dir/ca.crt ]]; then
    cp "$secret_dir/ca.key" "$secret_dir/ca.crt" "$tmp/"
  elif [[ -e $secret_dir/ca.key || -e $secret_dir/ca.crt ]]; then
    die 'Incomplete local CA backup; restore both ca.key and ca.crt instead of generating a new trust root.'
  else
    openssl req -x509 -newkey rsa:4096 -sha256 -nodes -days 3650 \
      -subj "/CN=$CLUSTER_NAME internal root CA" \
      -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
      -addext 'keyUsage=critical,keyCertSign,cRLSign' \
      -keyout "$tmp/ca.key" -out "$tmp/ca.crt"
  fi
fi
openssl x509 -in "$tmp/ca.crt" -checkend 0 -noout >/dev/null || die 'CA certificate has expired or is invalid.'
openssl x509 -in "$tmp/ca.crt" -pubkey -noout > "$tmp/cert.pub"
openssl pkey -in "$tmp/ca.key" -pubout > "$tmp/key.pub"
cmp -s "$tmp/cert.pub" "$tmp/key.pub" || die 'CA certificate and key do not match; existing backups were preserved.'
if [[ -z $existing ]]; then
  kubectl -n cert-manager create secret tls internal-ca \
    --cert="$tmp/ca.crt" --key="$tmp/ca.key"
fi
chmod 0600 "$tmp/ca.crt" "$tmp/ca.key"
mv -f "$tmp/ca.crt" "$secret_dir/ca.crt"
mv -f "$tmp/ca.key" "$secret_dir/ca.key"
log 'CA preserved. Distribute ca.crt to client trust stores; never distribute ca.key.'
