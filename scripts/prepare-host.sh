#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
require_root
require_debian
gpu=${GPU_VENDOR:-auto}
case "$gpu" in auto|none|nvidia|amd|intel) ;; *) die 'GPU_VENDOR must be auto, none, nvidia, amd, or intel.' ;; esac
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg python3 jq git sudo openssh-client \
  openssl etcd-client iproute2 iptables nftables conntrack socat ethtool \
  pciutils kmod util-linux apparmor apparmor-utils chrony \
  open-iscsi nfs-common cryptsetup dmsetup xfsprogs e2fsprogs
load_config
"$REPO_ROOT/scripts/configure-power.sh"

log 'Preparing kernel, time synchronization, swap, and Longhorn V1 host prerequisites.'
cat > /etc/modules-load.d/elektro-k3s.conf <<'EOF'
overlay
br_netfilter
vxlan
xt_socket
xt_TPROXY
xt_mark
xt_CT
iscsi_tcp
dm_crypt
nfs
EOF
while read -r module; do modprobe "$module"; done < /etc/modules-load.d/elektro-k3s.conf
cat > /etc/sysctl.d/90-elektro-k3s.conf <<'EOF'
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 1048576
EOF
sysctl --load=/etc/sysctl.d/90-elektro-k3s.conf
if [[ ! -e /etc/fstab.pre-elektro ]]; then cp -a /etc/fstab /etc/fstab.pre-elektro; fi
sed -i -E '/^[[:space:]]*#/! { /[[:space:]]swap[[:space:]]/s/^/# disabled by elektro: /; }' /etc/fstab
swapoff -a
cat > /etc/systemd/system/elektro-disable-swap.service <<'EOF'
[Unit]
Description=Disable swap before k3s
After=swap.target
Before=k3s.service k3s-agent.service
[Service]
Type=oneshot
ExecStart=/sbin/swapoff -a
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now elektro-disable-swap.service chrony.service iscsid.service
install -d -m 0755 /var/lib/longhorn
if systemctl is-active --quiet multipathd; then
  log 'multipathd is active: exclude Longhorn devices before deploying Longhorn; see docs/operations.md.'
fi

# PCI display-class devices only: do not mistake an AMD CPU/chipset for a GPU.
gpu_pci=$(lspci -Dn | awk '$2 ~ /^03[0-9a-f][0-9a-f]:$/ { print $3 }')
if [[ $gpu == auto ]]; then
  if [[ $gpu_pci == *10de:* ]]; then gpu=nvidia
  elif [[ $gpu_pci == *1002:* ]]; then gpu=amd
  elif [[ $gpu_pci == *8086:* ]]; then gpu=intel
  else gpu=none; fi
fi
if [[ $gpu != none ]]; then
  # A separate, signed source adds only the missing Debian components. Existing
  # mirrors and main entries are preserved (works with .list and Deb822 sources).
  cat > /etc/apt/sources.list.d/elektro-gpu.sources <<'EOF'
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie trixie-updates
Components: contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: https://security.debian.org/debian-security
Suites: trixie-security
Components: contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
  apt-get update
fi
case "$gpu" in
  nvidia)
    [[ $(arch) == amd64 ]] || die 'Debian packaged NVIDIA driver path is amd64; provision an appropriate ARM driver manually and use GPU_VENDOR=none.'
    # NVIDIA_DRIVER_PACKAGE allows a hardware-appropriate Debian driver, e.g.
    # nvidia-open-kernel-dkms where supported. Do not install a host CUDA SDK.
    apt-get install -y --no-install-recommends "linux-headers-$(uname -r)" \
      "${NVIDIA_DRIVER_PACKAGE:-nvidia-driver}" firmware-misc-nonfree
    tmp=$(mktemp -d)
    download https://nvidia.github.io/libnvidia-container/gpgkey "$tmp/key"
    gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg "$tmp/key"
    download https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list "$tmp/repo"
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      "$tmp/repo" > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    rm -rf -- "$tmp"
    apt-get update
    apt-get install -y --no-install-recommends \
      "nvidia-container-toolkit=$NVIDIA_TOOLKIT_VERSION" \
      "nvidia-container-toolkit-base=$NVIDIA_TOOLKIT_VERSION" \
      "libnvidia-container-tools=$NVIDIA_TOOLKIT_VERSION" \
      "libnvidia-container1=$NVIDIA_TOOLKIT_VERSION"
    # K3s detects /usr/bin/nvidia-container-runtime itself; never write to a
    # standalone /etc/containerd/config.toml or install another containerd.
    if ! nvidia-smi >/dev/null 2>&1; then
      die 'NVIDIA packages installed. Reboot, enroll the DKMS MOK if Secure Boot requires it, verify nvidia-smi, then rerun this script.'
    fi
    ;;
  amd)
    apt-get install -y --no-install-recommends firmware-amd-graphics
    modprobe amdgpu
    [[ -e /dev/kfd ]] || die 'AMD firmware installed; reboot and check GPU/kernel ROCm support before continuing.'
    ;;
  intel)
    apt-get install -y --no-install-recommends firmware-intel-graphics
    [[ -d /dev/dri ]] || die 'Intel firmware installed; reboot and verify /dev/dri before continuing.'
    ;;
esac
log "Host ready (GPU vendor: $gpu). No Longhorn controller, GPU operator, or device plugin has been installed."
