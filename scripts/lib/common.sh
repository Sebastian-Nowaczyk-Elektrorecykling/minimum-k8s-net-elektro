#!/usr/bin/env bash
# Sourced by entrypoints; no host changes here.
REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
log() { printf '\n[elektro] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
require_root() { [[ $EUID == 0 ]] || die 'Run with sudo/root.'; }
require_debian() {
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ $ID == debian && $VERSION_ID == 13 ]] || die 'This script requires Debian 13.'
  [[ -d /run/systemd/system ]] || die 'A booted systemd host is required.'
}
load_config() {
  local settings
  settings=$(python3 "$REPO_ROOT/scripts/config.py" env) || die 'Invalid cluster configuration.'
  # Only shlex-quoted data emitted by our validator is evaluated.
  eval "$settings"
}
download() { curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 "$1" -o "$2"; }
arch() {
  case $(uname -m) in
    x86_64) printf amd64 ;;
    aarch64) printf arm64 ;;
    *) die 'Only amd64 and arm64 are supported.' ;;
  esac
}
install_helm() {
  if command -v helm >/dev/null && [[ $(helm version --template '{{.Version}}') == "$HELM_VERSION" ]]; then return; fi
  local tmp archive
  tmp=$(mktemp -d)
  archive="helm-${HELM_VERSION}-linux-$(arch).tar.gz"
  download "https://get.helm.sh/$archive" "$tmp/$archive"
  download "https://get.helm.sh/$archive.sha256sum" "$tmp/checksums"
  (cd "$tmp" && sha256sum --check checksums)
  tar --no-same-owner -xzf "$tmp/$archive" -C "$tmp"
  install -m 0755 "$tmp/linux-$(arch)/helm" /usr/local/bin/helm
  rm -rf -- "$tmp"
}
kubectl() { /usr/local/bin/k3s kubectl "$@"; }

install_gateway_api() {
  local gateway_applied gateway_resource
  local -a gateway_crds=()
  gateway_applied=$(kubectl apply --server-side -k "$REPO_ROOT/infrastructure/gateway-api" -o name) || return
  # kubectl wait accepts resource names, not -k. The bundle also contains
  # admission policies, which do not expose a CRD Established condition.
  while IFS= read -r gateway_resource; do
    case "$gateway_resource" in
      customresourcedefinition.apiextensions.k8s.io/*) gateway_crds+=("$gateway_resource") ;;
    esac
  done <<< "$gateway_applied"
  ((${#gateway_crds[@]} > 0)) || die 'Gateway API bundle did not contain any CRDs.'
  # Wait for every applied CRD before Cilium performs API discovery.
  kubectl wait --for=condition=Established --timeout=120s "${gateway_crds[@]}"
}
