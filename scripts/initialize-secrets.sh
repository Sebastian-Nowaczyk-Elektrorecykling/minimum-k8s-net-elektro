#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
require_root; load_config
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
umask 077
secret_dir=/etc/elektro/secrets
install -d -m 0700 "$secret_dir"
for ns in cert-manager administration gateway-system; do
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
if kubectl -n administration get secret admin-credentials >/dev/null 2>&1; then
  for key in username password auth; do
    kubectl -n administration get secret admin-credentials -o "jsonpath={.data.$key}" | base64 -d > "$secret_dir/$key"
  done
else
  if [[ ! -s $secret_dir/password ]]; then
    password=$(openssl rand -hex 24)
    printf %s "$password" > "$secret_dir/password"
  fi
  printf admin > "$secret_dir/username"
  # stdin avoids exposing the password in process arguments. SHA-512 crypt is
  # supported by nginx's crypt(3) verifier in the selected Alpine image.
  hash=$(openssl passwd -6 -stdin < "$secret_dir/password")
  printf 'admin:%s\n' "$hash" > "$secret_dir/auth"
  kubectl -n administration create secret generic admin-credentials \
    --from-file=username="$secret_dir/username" --from-file=password="$secret_dir/password" \
    --from-file=auth="$secret_dir/auth"
fi
chmod 0600 "$secret_dir/"*
log 'Credentials preserved. Distribute ca.crt to client trust stores; never distribute ca.key.'
