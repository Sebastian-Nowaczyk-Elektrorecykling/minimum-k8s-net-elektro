#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
kubectl get nodes -o wide
kubectl -n flux-system get gitrepositories,kustomizations
kubectl get helmreleases -A
kubectl get gateway,httproute -A
kubectl get certificates -A
kubectl -n kube-system exec daemonset/cilium -- cilium-dbg status --verbose
kubectl get nodes -l elektro.internal/edge=true -o wide
kubectl -n kube-system get deployment hubble-relay hubble-ui
kubectl -n kube-system get services -l k8s-app=hubble
kubectl -n lan-system get service lan-dns
