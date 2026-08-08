#!/usr/bin/env bash
set -euo pipefail

BASE=/srv/wechat-official-account-automation
LOG_DIR="$BASE/shared/logs"
mkdir -p "$LOG_DIR"
cd "$BASE/current"

set +e
{
  echo "===== $(date -Is) scheduled publish stage start ====="
  "$BASE/venv/bin/python" -m src.scheduler.daily_runner \
    --topics "$BASE/shared/topics.yaml" \
    --schedule "$BASE/shared/schedule.auto_publish.yaml" \
    --safety "$BASE/current/config/safety-rules.yaml" \
    --news-sources "$BASE/shared/news-sources.yaml" \
    --manufacturing-news-sources "$BASE/shared/manufacturing-news-sources.yaml" \
    --env "$BASE/shared/.env" \
    --state-dir "$BASE/shared/data/publish-state" \
    --output-root "$BASE/shared/outputs" \
    --db "$BASE/shared/data/app.sqlite" \
    --stage publish \
    --json
  run_status=$?
  echo "===== $(date -Is) scheduled publish stage end (exit=$run_status) ====="
} >> "$LOG_DIR/daily-run.log" 2>&1
set -e
exit "$run_status"
