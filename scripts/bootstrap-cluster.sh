#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
require_root; require_debian; load_config
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
[[ -f $KUBECONFIG ]] || die 'Run this on an installed k3s server.'
python3 "$REPO_ROOT/scripts/config.py" check
git_cmd=(git -c "safe.directory=$REPO_ROOT" -C "$REPO_ROOT")
[[ -z $("${git_cmd[@]}" status --porcelain) ]] || die 'Commit and push all changes before bootstrap: Flux must use exactly this configuration.'
remote_sha=$("${git_cmd[@]}" ls-remote "$GIT_URL" "refs/heads/$GIT_BRANCH" | awk '{print $1}')
[[ -n $remote_sha && $remote_sha == "$("${git_cmd[@]}" rev-parse HEAD)" ]] || die "Checkout must match $GIT_URL branch $GIT_BRANCH. Merge/push before bootstrap."
kubectl get --raw=/readyz >/dev/null
install_helm

log 'Installing Gateway API CRDs before starting Cilium.'
install_gateway_api
# On retries after successful adoption, Flux is the sole Helm release manager.
if kubectl -n kube-system get helmrelease cilium >/dev/null 2>&1; then
  log 'Cilium HelmRelease already exists; leaving release reconciliation to Flux.'
else
  helm repo add cilium https://helm.cilium.io --force-update
  helm repo update cilium
  helm upgrade --install cilium cilium/cilium --namespace kube-system \
    --version "$CILIUM_VERSION" --values "$REPO_ROOT/infrastructure/cilium/values.yaml" \
    --wait --timeout 15m
fi
kubectl -n kube-system rollout status daemonset/cilium --timeout=300s
kubectl -n kube-system rollout status deployment/coredns --timeout=300s

log 'Creating the private CA outside Git.'
"$REPO_ROOT/scripts/initialize-secrets.sh"
log 'Installing Flux controllers, then its Git source and reconciliation graph.'
kubectl apply --server-side -k "$REPO_ROOT/infrastructure/flux"
kubectl -n flux-system wait deployment --all --for=condition=Available --timeout=300s
kubectl apply -f "$REPO_ROOT/clusters/lan/cluster-settings.yaml"
kubectl apply -f "$REPO_ROOT/clusters/lan/flux-system/source.yaml"
kubectl apply -f "$REPO_ROOT/clusters/lan/flux-system/sync.yaml"
# The Git source fetch is independent of cluster DNS exposure. All DNS upstreams
# are explicit so DHCP's use of this node for DNS cannot form a recursion loop.
kubectl -n flux-system wait gitrepository/flux-system --for=condition=Ready --timeout=300s
kubectl -n flux-system wait kustomization/flux-system --for=condition=Ready --timeout=600s
for component in cilium dns pki gateway admin apps; do
  kubectl -n flux-system wait "kustomization/$component" --for=condition=Ready --timeout=900s
done
kubectl -n kube-system wait helmrelease/cilium --for=condition=Ready --timeout=300s
"$REPO_ROOT/scripts/check-gateway.sh"
log 'Flux now owns the manifests and will adopt the existing cilium/kube-system Helm release.'
log "Check scripts/status.sh, then point LAN DNS clients or the router at $API_IP."
log 'The CA is saved under /etc/elektro/secrets (root-only). Back it up securely.'
log 'Hubble UI has no login during bootstrap. See docs/hubble-sso.md for connecting an SSO proxy later.'
