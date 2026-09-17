#!/usr/bin/env bash
# Read-only Gateway API acceptance checks. Run after Flux reconciliation.
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
load_config
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
kubectl get ingress,ingressclass -A -o json > "$tmp/ingress.json"
kubectl -n kube-system get configmap cilium-config -o json > "$tmp/cilium.json"
kubectl get gatewayclass cilium -o json > "$tmp/class.json"
kubectl -n gateway-system get gateway internal -o json > "$tmp/gateway.json"
kubectl get httproute -A -o json > "$tmp/routes.json"
kubectl get nodes -l elektro.internal/edge=true -o json > "$tmp/edge.json"
python3 "$REPO_ROOT/scripts/check-gateway.py" "$tmp" --api-ip "$API_IP"
