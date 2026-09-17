#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=lib/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"
require_root
require_debian

log 'Disabling automatic, lid, and sleep-key suspend; the power button will suspend the node.'
install -D -m 0644 "$REPO_ROOT/config/host/logind.conf" \
  /etc/systemd/logind.conf.d/90-elektro-node-power.conf
install -D -m 0644 "$REPO_ROOT/config/host/sleep.conf" \
  /etc/systemd/sleep.conf.d/90-elektro-node-power.conf
# No extra daemon is needed on a headless host. If polkit is installed now or
# later (for a desktop), it automatically loads this rule.
install -D -m 0644 "$REPO_ROOT/config/host/10-elektro-node-power.rules" \
  /etc/polkit-1/rules.d/10-elektro-node-power.rules

# Undo blanket suspend masks, which would also disable the power button.
systemctl unmask sleep.target suspend.target systemd-suspend.service
systemctl unmask --runtime sleep.target suspend.target systemd-suspend.service
# Debian 13's logind supports reload: do not restart it or disrupt sessions.
systemctl reload systemd-logind.service

# A policy reload cannot revoke an inhibitor already held by a desktop.
inhibitors=$(systemd-inhibit --list --no-pager --no-legend)
if [[ $inhibitors == *handle-power-key* || $inhibitors == *handle-suspend-key* ||
      $inhibitors == *handle-hibernate-key* || $inhibitors == *handle-lid-switch* ]]; then
  log 'An existing session still owns power/lid handling. Log out of local desktop sessions or reboot during maintenance to release it; see docs/operations.md#node-power-policy.'
fi
log 'Power policy installed. A short power-button press suspends; lid, idle, and sleep/hibernate keys are ignored.'
