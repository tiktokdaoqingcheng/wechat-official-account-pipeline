from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.integrations.wechat_client import load_env_file
from src.review.publish_guard import append_audit_event, redact_sensitive


DEFAULT_TIMEOUT_SECONDS = 12
SUCCESS_STATUSES = {"ok"}
FAILURE_STATUSES = {"failed"}
BLOCKED_STATUSES = {"blocked"}


@dataclass(frozen=True)
class NotificationConfig:
    webhook_url: str = ""
    webhook_type: str = "generic"
    token: str = ""
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    on_success: bool = True
    on_failure: bool = True
    on_blocked: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url.strip())


def load_notification_config(env_path: str | Path = ".env") -> NotificationConfig:
    values = {**load_env_file(env_path), **os.environ}
    return NotificationConfig(
        webhook_url=str(values.get("NOTIFICATION_WEBHOOK_URL", "")).strip(),
        webhook_type=str(values.get("NOTIFICATION_WEBHOOK_TYPE", "generic")).strip().lower() or "generic",
        token=str(values.get("NOTIFICATION_WEBHOOK_TOKEN", "")).strip(),
        timeout_seconds=int(values.get("NOTIFICATION_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS),
        on_success=_env_bool(values.get("NOTIFICATION_ON_SUCCESS", "true")),
        on_failure=_env_bool(values.get("NOTIFICATION_ON_FAILURE", "true")),
        on_blocked=_env_bool(values.get("NOTIFICATION_ON_BLOCKED", "true")),
    )


def notify_daily_run(
    result: dict[str, Any],
    *,
    env_path: str | Path = ".env",
    state_dir: str | Path = "data/publish-state",
) -> dict[str, Any]:
    config = load_notification_config(env_path)
    if not config.enabled:
        return {"status": "disabled", "reason": "NOTIFICATION_WEBHOOK_URL is not configured."}

    status = str(result.get("status", "unknown"))
    if str(result.get("business_outcome", "")).strip() == "prepared":
        return {"status": "skipped", "reason": "Prepare-stage success is not a terminal publish notification."}
    if status in SUCCESS_STATUSES and not config.on_success:
        return {"status": "skipped", "reason": "success notifications are disabled."}
    if status in FAILURE_STATUSES and not config.on_failure:
        return {"status": "skipped", "reason": "failure notifications are disabled."}
    if status in BLOCKED_STATUSES and not config.on_blocked:
        return {"status": "skipped", "reason": "blocked notifications are disabled."}

    fingerprint = _notification_fingerprint(result)
    notification_path = _notification_state_path(result, state_dir)
    if _already_notified(notification_path, fingerprint):
        return {
            "status": "duplicate_skipped",
            "reason": "The same terminal notification was already delivered.",
        }

    payload = _payload_for(config.webhook_type, result)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    try:
        response = requests.post(
            config.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=max(1, config.timeout_seconds),
        )
        response.raise_for_status()
        notice_result = {
            "status": "ok",
            "webhook_type": config.webhook_type,
            "status_code": response.status_code,
        }
        _record_notification(notification_path, fingerprint, result)
    except Exception as exc:
        notice_result = {
            "status": "failed",
            "webhook_type": config.webhook_type,
            "error": str(exc),
        }

    append_audit_event(
        "daily_run_notification",
        notice_result,
        state_dir,
        status="ok" if notice_result["status"] == "ok" else "failed",
    )
    return redact_sensitive(notice_result)


def _notification_fingerprint(result: dict[str, Any]) -> str:
    payload = {
        "status": result.get("status"),
        "business_outcome": result.get("business_outcome"),
        "next_action": result.get("next_action"),
        "publish_id": result.get("publish_id"),
        "msg_id": result.get("msg_id"),
        "msg_data_id": result.get("msg_data_id"),
        "reason": result.get("reason"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _notification_state_path(result: dict[str, Any], state_dir: str | Path) -> Path:
    target_date = str(result.get("target_date", "")).strip()
    if not target_date:
        target_date = str(result.get("created_at", ""))[:10]
    if not _is_full_date(target_date):
        target_date = "unknown-date"
    return Path(state_dir) / "notifications" / f"{target_date}.json"


def _already_notified(path: Path, fingerprint: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    fingerprints = payload.get("fingerprints", []) if isinstance(payload, dict) else []
    return fingerprint in fingerprints if isinstance(fingerprints, list) else False


def _record_notification(path: Path, fingerprint: str, result: dict[str, Any]) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    fingerprints = payload.get("fingerprints", []) if isinstance(payload, dict) else []
    fingerprints = [str(value) for value in fingerprints if str(value)] if isinstance(fingerprints, list) else []
    if fingerprint not in fingerprints:
        fingerprints.append(fingerprint)
    updated = {
        "target_date": path.stem,
        "fingerprints": fingerprints[-20:],
        "last_status": str(result.get("status", "")),
        "last_business_outcome": str(result.get("business_outcome", "")),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _is_full_date(value: str) -> bool:
    if len(value) != 10:
        return False
    return value[:4].isdigit() and value[4] == "-" and value[5:7].isdigit() and value[7] == "-" and value[8:].isdigit()


def _payload_for(webhook_type: str, result: dict[str, Any]) -> dict[str, Any]:
    text = _message_text(result)
    title = _message_title(result)
    if webhook_type == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if webhook_type == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    if webhook_type in {"wecom", "wechat_work", "enterprise_wechat"}:
        return {"msgtype": "markdown", "markdown": {"content": text}}
    return {
        "event": "wechat_official_account_daily_run",
        "status": result.get("status"),
        "business_outcome": result.get("business_outcome"),
        "next_action": result.get("next_action"),
        "title": _title(result),
        "publish_channel": result.get("publish_channel"),
        "publish_id": result.get("publish_id"),
        "msg_id": result.get("msg_id"),
        "msg_data_id": result.get("msg_data_id"),
        "published": result.get("published"),
        "published_articles": result.get("published_articles", []),
        "output_dir": result.get("output_dir"),
        "reason": result.get("reason", ""),
    }


def _message_title(result: dict[str, Any]) -> str:
    outcome = str(result.get("business_outcome") or result.get("status", "unknown"))
    return f"WeChat automation: {outcome}"


def _message_text(result: dict[str, Any]) -> str:
    lines = [
        _message_title(result),
        f"Status: {result.get('status', 'unknown')}",
        f"Outcome: {result.get('business_outcome', 'unknown')}",
        f"Next: {result.get('next_action', 'unknown')}",
    ]
    title = _title(result)
    if title:
        lines.append(f"Title: {title}")
    if result.get("reason"):
        lines.append(f"Reason: {result.get('reason')}")
    if result.get("publish_channel"):
        lines.append(f"Channel: {result.get('publish_channel')}")
    if result.get("publish_id"):
        lines.append(f"Publish ID: {result.get('publish_id')}")
    if result.get("msg_id"):
        lines.append(f"Mass Msg ID: {result.get('msg_id')}")
    if result.get("msg_data_id"):
        lines.append(f"Mass Msg Data ID: {result.get('msg_data_id')}")
    links = _published_links(result)
    if links:
        lines.append("Published links:")
        lines.extend(f"- {link}" for link in links)
    if result.get("output_dir"):
        lines.append(f"Output: {result.get('output_dir')}")
    return "\n".join(lines)


def _title(result: dict[str, Any]) -> str:
    stages = result.get("stages", [])
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict) or stage.get("name") != "content_package":
                continue
            title = str(stage.get("title", "")).strip()
            secondary = str(stage.get("secondary_title", "")).strip()
            if title and secondary:
                return f"{title} / {secondary}"
            return title
    return str(result.get("title", "")).strip()


def _published_links(result: dict[str, Any]) -> list[str]:
    articles = result.get("published_articles", [])
    if not isinstance(articles, list):
        return []
    links = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        url = str(article.get("url", "")).strip()
        if url:
            links.append(url)
    return links


def _env_bool(value: Any) -> bool:
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
