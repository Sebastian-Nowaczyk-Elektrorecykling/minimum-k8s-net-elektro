#!/usr/bin/env bash
# Read-only NVIDIA readiness check and failure diagnostics; no host changes.
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
require_root
check_nvidia_driver
