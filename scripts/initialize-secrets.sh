#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
require_root; load_config
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
umask 077
secret_dir=/etc/elektro/secrets
install -d -m 0700 "$secret_dir"
for ns in cert-manager gateway-system; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done
if kubectl -n cert-manager get secret internal-ca >/dev/null 2>&1; then
  kubectl -n cert-manager get secret internal-ca -o jsonpath='{.data.tls\.crt}' | base64 -d > "$secret_dir/ca.crt"
  kubectl -n cert-manager get secret internal-ca -o jsonpath='{.data.tls\.key}' | base64 -d > "$secret_dir/ca.key"
else
  if [[ ! -s $secret_dir/ca.key || ! -s $secret_dir/ca.crt ]]; then
    openssl req -x509 -newkey rsa:4096 -sha256 -nodes -days 3650 \
      -subj "/CN=$CLUSTER_NAME internal root CA" \
      -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
      -addext 'keyUsage=critical,keyCertSign,cRLSign' \
      -keyout "$secret_dir/ca.key" -out "$secret_dir/ca.crt"
  fi
  kubectl -n cert-manager create secret tls internal-ca \
    --cert="$secret_dir/ca.crt" --key="$secret_dir/ca.key"
fi
chmod 0600 "$secret_dir/ca.crt" "$secret_dir/ca.key"
log 'CA preserved. Distribute ca.crt to client trust stores; never distribute ca.key.'
