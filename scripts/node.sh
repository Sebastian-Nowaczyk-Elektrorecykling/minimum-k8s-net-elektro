#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
usage() {
  cat <<'EOF'
Usage: node.sh controller|worker|hybrid --interface IFACE [--node-ip IP]
               [--token-file FILE | --bootstrap] [--skip-host-preparation]
Joining servers need the server token; workers may use an agent token.
--bootstrap creates the first embedded-etcd server; run bootstrap-cluster.sh next.
Bootstrap defaults to api_ip; joining nodes discover their DHCP address at every
k3s service start. --node-ip opts into a fixed address already assigned by you.
GPU_VENDOR=auto|none|nvidia|amd|intel controls host GPU preparation (default auto).
EOF
}
[[ $# -gt 0 ]] || { usage; exit 1; }
[[ $1 != --help ]] || { usage; exit 0; }
role=$1; shift
case "$role" in worker|controller|hybrid) ;; *) usage; exit 1 ;; esac
bootstrap=false; prepare=true; node_ip=; interface=; token_file=
while (($#)); do
  case "$1" in
    --node-ip) node_ip=${2:?}; shift 2 ;;
    --interface) interface=${2:?}; shift 2 ;;
    --token-file) token_file=${2:?}; shift 2 ;;
    --bootstrap) bootstrap=true; shift ;;
    --skip-host-preparation) prepare=false; shift ;;
    --help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done
require_root; require_debian
[[ -n $interface ]] || die 'Specify --interface explicitly.'
if [[ $bootstrap == false ]]; then
  [[ -n $token_file && -s $token_file ]] || die 'Joining a node requires a nonempty --token-file.'
else
  [[ -z $token_file ]] || die '--bootstrap and --token-file are mutually exclusive.'
fi
state=/etc/elektro/node-role
if [[ -e /etc/rancher/k3s/config.yaml || -e /var/lib/rancher/k3s/server/db ]]; then
  [[ -f $state && $(cat "$state") == "$role:$bootstrap" ]] || die 'Existing or differently configured k3s installation; do not repurpose a node in place.'
fi
if [[ $prepare == true ]]; then "$REPO_ROOT/scripts/prepare-host.sh"; fi
load_config
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
if [[ $bootstrap == true && -z $node_ip ]]; then node_ip=$API_IP; fi
dynamic=false; [[ -n $node_ip ]] || dynamic=true
python3 - "$interface" "$LAN_CIDR" "$API_IP" "$node_ip" "$bootstrap" > "$tmp/network.json" <<'PY'
import json, sys
print(json.dumps(dict(interface=sys.argv[1], lan_cidr=sys.argv[2], api_ip=sys.argv[3],
                     fixed_ip=sys.argv[4] or None, edge=sys.argv[5] == 'true'), indent=2))
PY
node_ip=$(python3 "$REPO_ROOT/scripts/refresh-node-ip.py" detect --settings "$tmp/network.json")
if [[ $bootstrap == true && ! -f /etc/rancher/k3s/config.yaml ]]; then
  python3 - "$API_IP" <<'PY'
import socket, sys
for host, port, kind in [(sys.argv[1],53,socket.SOCK_DGRAM), (sys.argv[1],53,socket.SOCK_STREAM),
                         ('0.0.0.0',80,socket.SOCK_STREAM), ('0.0.0.0',443,socket.SOCK_STREAM)]:
    with socket.socket(socket.AF_INET, kind) as sock:
        try:
            sock.bind((host, port))
        except OSError as error:
            sys.exit(f'Free {host}:{port} for cluster DNS/gateway before bootstrap: {error}')
PY
fi
args=(node --role "$role" --node-ip "$node_ip" --interface "$interface")
[[ $bootstrap == false ]] || args+=(--bootstrap)
python3 "$REPO_ROOT/scripts/config.py" "${args[@]}" > "$tmp/config.yaml"
install -d -m 0700 /etc/rancher/k3s /etc/elektro
if [[ -f /etc/elektro/node-network.json ]] && ! cmp -s "$tmp/network.json" /etc/elektro/node-network.json; then
  die 'Existing interface/address discovery settings differ; reconfigure the node deliberately.'
fi
if [[ -f /etc/rancher/k3s/config.yaml ]]; then
  python3 - /etc/rancher/k3s/config.yaml "$tmp/config.yaml" "$dynamic" <<'PY'
import json, sys
with open(sys.argv[1]) as stream: old = json.load(stream)
with open(sys.argv[2]) as stream: new = json.load(stream)
if sys.argv[3] == 'true': old['node-ip'] = new['node-ip']
if old != new:
    sys.exit('Existing node configuration differs. Use a controlled reconfiguration procedure.')
PY
fi
if [[ $bootstrap == false ]]; then
  install -m 0600 "$token_file" /etc/rancher/k3s/join.token
fi
install -m 0600 "$tmp/config.yaml" /etc/rancher/k3s/config.yaml
for dns in $UPSTREAM_DNS; do printf 'nameserver %s\n' "$dns"; done > /etc/rancher/k3s/upstream-resolv.conf
printf '%s:%s\n' "$role" "$bootstrap" > "$state"
mode=server; [[ $role != worker ]] || mode=agent
service=k3s; [[ $role != worker ]] || service=k3s-agent
install -m 0600 "$tmp/network.json" /etc/elektro/node-network.json
install -m 0700 "$REPO_ROOT/scripts/refresh-node-ip.py" /etc/elektro/refresh-node-ip.py
install -d -m 0755 "/etc/systemd/system/$service.service.d"
cat > "/etc/systemd/system/$service.service.d/20-elektro-node-ip.conf" <<'EOF'
[Unit]
Wants=network-online.target
After=network-online.target

[Service]
ExecStartPre=/usr/bin/python3 /etc/elektro/refresh-node-ip.py refresh
EOF
systemctl daemon-reload
# Download the installer from the same immutable k3s release tag. The upstream
# installer checks the release binary against its published SHA256 checksum.
download "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION}/install.sh" "$tmp/install.sh"
INSTALL_K3S_VERSION=$K3S_VERSION INSTALL_K3S_EXEC=$mode INSTALL_K3S_FORCE_RESTART=true sh "$tmp/install.sh"
if command -v nvidia-container-runtime >/dev/null; then
  grep -q nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml || die 'k3s did not detect the NVIDIA runtime.'
fi
log "Installed $role. Bootstrap node is expected to be NotReady until Cilium is installed."
