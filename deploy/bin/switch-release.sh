#!/usr/bin/env bash
set -euo pipefail

BASE=/srv/wechat-official-account-automation
TARGET=${1:-}

if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 /srv/wechat-official-account-automation/releases/<release>" >&2
  exit 2
fi
target_real=$(readlink -f "$TARGET")
if [[ "$target_real" != "$BASE/releases/"* || ! -d "$target_real" ]]; then
  echo "Refusing invalid release target: $TARGET" >&2
  exit 2
fi
if systemctl is-active --quiet wechat-official-account-automation.service; then
    echo "Refusing release switch while the daily service is active." >&2
    exit 2
fi
if systemctl is-active --quiet wechat-content-prepare.service; then
    echo "Refusing release switch while the content prepare service is active." >&2
    exit 2
fi

ln -sfn "$target_real" "$BASE/current.next"
mv -Tf "$BASE/current.next" "$BASE/current"
systemctl restart wechat-config-page.service
echo "Current release switched to $target_real. Daily timer was not restarted."
