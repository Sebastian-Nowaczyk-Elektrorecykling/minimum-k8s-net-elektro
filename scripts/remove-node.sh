#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() {
  cat <<'EOF'
Usage: remove-node.sh --node NAME --ssh USER@IP [--yes]
                      [--delete-emptydir-data] [--destroy-last-server]
Run on a surviving controller with root and cluster-admin kubeconfig.
Without --yes this prints the plan after read-only safety checks.
SSH must reach the selected node and permit sudo -n (or connect as root).
All node types are supported. Longhorn replicas must be evacuated first.
Last-server destruction additionally requires every other node to be removed.
EOF
}
node=; target=; execute=false; emptydir=false; destroy=false
while (($#)); do
  case "$1" in
    --node) node=${2:?}; shift 2 ;;
    --ssh) target=${2:?}; shift 2 ;;
    --yes) execute=true; shift ;;
    --delete-emptydir-data) emptydir=true; shift ;;
    --destroy-last-server) destroy=true; shift ;;
    --help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[[ $node =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || die 'Specify a valid --node name.'
[[ $target =~ ^[a-zA-Z0-9_.-]+@[a-zA-Z0-9.:-]+$ ]] || die 'Specify --ssh USER@IP (no shell options).'
load_config
export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
node_json=$(kubectl get node "$node" -o json)
internal_ip=$(jq -r '.status.addresses[] | select(.type == "InternalIP") | .address' <<< "$node_json" | head -1)
is_server=$(jq -r '.metadata.labels | has("node-role.kubernetes.io/etcd")' <<< "$node_json")
if [[ $destroy == false ]]; then
  local_ips=$(ip -j -4 addr show)
  if jq -e --arg ip "$internal_ip" 'any(.[].addr_info[]; .local == $ip)' <<< "$local_ips" >/dev/null; then
    die 'Run routine removal from a different surviving controller; stopping this host would cut off the API/etcd checks.'
  fi
fi
if [[ $internal_ip == "$API_IP" && $destroy == false ]]; then
  die 'This node owns the API, DNS and web gateway. Migrate the shared edge endpoint first; see docs/operations.md.'
fi
all_nodes=$(kubectl get nodes -o json)
etcd=(etcdctl --endpoints=https://127.0.0.1:2379
  --cacert=/var/lib/rancher/k3s/server/tls/etcd/server-ca.crt
  --cert=/var/lib/rancher/k3s/server/tls/etcd/client.crt
  --key=/var/lib/rancher/k3s/server/tls/etcd/client.key)
if [[ $destroy == true ]]; then
  [[ $is_server == true && $(jq '.items | length' <<< "$all_nodes") == 1 ]] || die '--destroy-last-server is only valid for the final node of a cluster.'
elif [[ $is_server == true ]]; then
  # Conservative guard: all registered etcd servers must be Ready. Two-member
  # clusters need a third server added first; never remove a quorum member from
  # a degraded cluster through this routine maintenance path.
  jq -e --arg node "$node" '
    [.items[] | select(.metadata.labels | has("node-role.kubernetes.io/etcd"))] as $servers |
    ($servers | length) as $n |
    ($n >= 3) and
    ([$servers[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))] | length) == $n and
    ([$servers[] | select(.metadata.name != $node)] | length) >= (($n / 2 | floor) + 1)
  ' <<< "$all_nodes" >/dev/null || die 'Etcd safety check failed: add/repair servers first (minimum three healthy members before routine removal).'
  command -v etcdctl >/dev/null || die 'Install Debian etcd-client on the administering server.'
  "${etcd[@]}" endpoint health --cluster
  members=$("${etcd[@]}" member list -w json)
  registered=$(jq '[.items[] | select(.metadata.labels | has("node-role.kubernetes.io/etcd"))] | length' <<< "$all_nodes")
  jq -e --argjson n "$registered" --arg peer "https://$internal_ip:2380" \
    '(.members | length) == $n and any(.members[]; any(.peerURLs[]; . == $peer)) and all(.members[]; .isLearner != true)' \
    <<< "$members" >/dev/null || die 'Actual etcd membership differs from ready server nodes; inspect etcd before removal.'
fi

# Do not proceed if API discovery fails: fail closed rather than treating an
# unreachable Longhorn installation as an absent one.
crds=$(kubectl get crd -o json)
if jq -e '.items[] | select(.metadata.name == "nodes.longhorn.io")' <<< "$crds" >/dev/null; then
  lh_nodes=$(kubectl -n longhorn-system get nodes.longhorn.io -o json)
  if jq -e --arg node "$node" '.items[] | select(.metadata.name == $node)' <<< "$lh_nodes" >/dev/null; then
    jq -e --arg node "$node" '.items[] | select(.metadata.name == $node) | .spec.allowScheduling == false' \
      <<< "$lh_nodes" >/dev/null || die 'Disable Longhorn scheduling and request replica eviction on this node first.'
    replicas=$(kubectl -n longhorn-system get replicas.longhorn.io -o json)
    jq -e --arg node "$node" '[.items[] | select(.spec.nodeID == $node)] | length == 0' \
      <<< "$replicas" >/dev/null || die 'Longhorn replicas remain on this node. Wait for safe eviction before retrying.'
    volumes=$(kubectl -n longhorn-system get volumes.longhorn.io -o json)
    jq -e --arg node "$node" '[.items[] | select(.spec.nodeID == $node or .status.currentNodeID == $node)] | length == 0' \
      <<< "$volumes" >/dev/null || die 'Longhorn volumes are still attached here. Migrate workloads and detach them before retrying.'
  fi
fi
pv=$(kubectl get pv -o json)
jq -n --argjson node "$node_json" --argjson volumes "$pv" '{node: $node, volumes: $volumes}' |
  python3 "$REPO_ROOT/scripts/check-local-volumes.py"
log "Plan: drain $node ($internal_ip), stop k3s via $target, remove its cluster objects, then uninstall locally. Longhorn data and host prerequisites remain."
[[ $execute == true ]] || { log 'Read-only plan complete. Use --yes to execute.'; exit 0; }
require_root
# Verify SSH identity before draining; prevent deleting one node and stopping
# another because of a mistyped host. Never disable SSH host key verification.
remote_ips=$(ssh -o BatchMode=yes -- "$target" 'ip -j -4 addr show')
jq -e --arg ip "$internal_ip" 'any(.[].addr_info[]; .local == $ip)' <<< "$remote_ips" >/dev/null || die 'SSH target does not own the Kubernetes node IP.'
ssh -o BatchMode=yes -- "$target" 'sudo -n true'
if [[ $is_server == true ]]; then
  # This runs on the administering server, which must itself use embedded etcd.
  /usr/local/bin/k3s etcd-snapshot save --name "before-removing-$node-$(date +%s)"
  log 'Save the etcd snapshot AND /var/lib/rancher/k3s/server/token off-host before dismantling the final server.'
  if [[ $destroy == true ]]; then
    [[ ${ETCD_BACKUP_CONFIRMED:-no} == yes ]] || die 'For final-server destruction, first back up the snapshot and server token off-host, then set ETCD_BACKUP_CONFIRMED=yes.'
  fi
fi
drain=(drain "$node" --ignore-daemonsets --timeout=15m)
[[ $emptydir == false ]] || drain+=(--delete-emptydir-data)
# PDBs and unmanaged pods are respected; no --force or --disable-eviction.
kubectl "${drain[@]}"
ssh -o BatchMode=yes -- "$target" 'sudo -n sh -c "systemctl stop k3s.service 2>/dev/null || systemctl stop k3s-agent.service"'
if [[ $destroy == false ]]; then
  # K3s's embedded-etcd controller removes membership in response to Node deletion.
  kubectl delete node "$node" --wait=true --timeout=120s
  if [[ $is_server == true ]]; then
    deadline=$((SECONDS + 120))
    while :; do
      members=$("${etcd[@]}" member list -w json)
      if jq -e --arg peer "https://$internal_ip:2380" \
        'all(.members[]; all(.peerURLs[]; . != $peer))' <<< "$members" >/dev/null; then break; fi
      (( SECONDS < deadline )) || die 'K3s has not removed the etcd member. Node is stopped; preserve its data and repair membership manually.'
      sleep 3
    done
    "${etcd[@]}" endpoint health --cluster
  fi
  if kubectl -n kube-system get secret "$node.node-password.k3s" >/dev/null 2>&1; then
    kubectl -n kube-system delete secret "$node.node-password.k3s"
  fi
  if jq -e '.items[] | select(.metadata.name == "nodes.longhorn.io")' <<< "$crds" >/dev/null; then
    kubectl -n longhorn-system delete nodes.longhorn.io "$node" --ignore-not-found --timeout=120s
  fi
fi
ssh -o BatchMode=yes -- "$target" 'sudo -n bash -s' <<'REMOTE'
set -Eeuo pipefail
# K3s's documented Cilium cleanup must precede its iptables cleanup. Do not
# flush the host firewall or delete unrelated links or persistent storage.
for link in cilium_host cilium_net cilium_vxlan; do
  if ip link show "$link" >/dev/null 2>&1; then ip link delete "$link"; fi
done
if command -v iptables-save >/dev/null; then
  iptables-save | sed '/CILIUM/d' | iptables-restore
fi
if command -v ip6tables-save >/dev/null; then
  ip6tables-save | sed '/CILIUM/d' | ip6tables-restore
fi
if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
  /usr/local/bin/k3s-uninstall.sh
elif [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then
  /usr/local/bin/k3s-agent-uninstall.sh
else
  echo 'No k3s uninstall script found; inspect the host manually.' >&2
  exit 1
fi
rm -f /etc/elektro/node-role /etc/elektro/node-network.json /etc/elektro/refresh-node-ip.py
rm -f /etc/systemd/system/k3s.service.d/20-elektro-node-ip.conf \
  /etc/systemd/system/k3s-agent.service.d/20-elektro-node-ip.conf
systemctl daemon-reload
echo 'Uninstalled k3s. Reboot before rejoining to clear residual Cilium BPF state.'
REMOTE
log 'Removal complete. Reboot the removed host before repurposing; /var/lib/longhorn is preserved.'
