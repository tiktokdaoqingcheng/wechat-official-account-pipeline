#!/usr/bin/env bash
set -euo pipefail

BASE=/srv/wechat-official-account-automation
SOURCE=${1:-$BASE/current}

if [[ $(id -u) -ne 0 ]]; then
  echo "This installer must run as root." >&2
  exit 2
fi
if [[ $(readlink -f "$SOURCE") != "$BASE"/* ]]; then
  echo "Refusing source outside $BASE: $SOURCE" >&2
  exit 2
fi

install -m 0750 "$SOURCE/deploy/bin/run-daily-auto-publish.sh" "$BASE/run-daily-auto-publish.sh"
install -m 0750 "$SOURCE/deploy/bin/run-content-prepare.sh" "$BASE/run-content-prepare.sh"
install -m 0750 "$SOURCE/deploy/bin/prune-runtime-artifacts.sh" "$BASE/prune-runtime-artifacts.sh"
install -m 0644 "$SOURCE/deploy/systemd/"*.service /etc/systemd/system/
install -m 0644 "$SOURCE/deploy/systemd/"*.timer /etc/systemd/system/
install -m 0644 "$SOURCE/deploy/logrotate/wechat-official-account-automation" /etc/logrotate.d/wechat-official-account-automation
systemctl daemon-reload
systemctl enable wechat-content-prepare.timer wechat-official-account-automation.timer wechat-publish-watchdog.timer wechat-runtime-maintenance.timer

echo "Assets installed. Timers were enabled but not restarted and the daily service was not started."
