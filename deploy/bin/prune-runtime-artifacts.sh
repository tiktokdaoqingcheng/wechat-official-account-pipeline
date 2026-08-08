#!/usr/bin/env bash
set -euo pipefail

BASE=${WECHAT_AUTOMATION_BASE:-/srv/wechat-official-account-automation}
OUTPUT_RETENTION_DAYS=${OUTPUT_RETENTION_DAYS:-45}
RELEASE_RETENTION_DAYS=${RELEASE_RETENTION_DAYS:-30}
MIN_RELEASES_TO_KEEP=${MIN_RELEASES_TO_KEEP:-8}

if [[ "$BASE" != /srv/wechat-official-account-automation ]]; then
  echo "Refusing unexpected base path: $BASE" >&2
  exit 2
fi

find "$BASE/shared/outputs" -mindepth 1 -maxdepth 1 -type d -mtime "+$OUTPUT_RETENTION_DAYS" -print -exec rm -rf -- {} +

current_release=$(readlink -f "$BASE/current" || true)
mapfile -t releases < <(find "$BASE/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
for index in "${!releases[@]}"; do
  release=${releases[$index]}
  if (( index < MIN_RELEASES_TO_KEEP )); then
    continue
  fi
  if [[ $(readlink -f "$release") == "$current_release" ]]; then
    continue
  fi
  if find "$release" -maxdepth 0 -mtime "+$RELEASE_RETENTION_DAYS" | grep -q .; then
    echo "Removing expired release: $release"
    rm -rf -- "$release"
  fi
done
