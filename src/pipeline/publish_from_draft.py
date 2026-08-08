from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from src.config_loader import load_layered_yaml
from src.integrations.wechat_client import WeChatApiError, client_from_env
from src.pipeline.package_manifest import verify_frozen_package
from src.review.publish_guard import (
    already_published_today,
    append_audit_event,
    load_manual_confirmation,
    record_publish_intent,
    record_publish_lock,
    record_published_links,
    retry_call,
    update_publish_intent,
)
from src.review.publish_policy import PublishDecision, decide_publish_action
from src.runtime_guard import config_fingerprint, require_wechat_write_access


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
FINAL_PUBLISH_STATUSES = {0, 2, 3, 4, 5, 6}
PUBLISH_STATUS_LABELS = {
    0: "success",
    1: "publishing",
    2: "original_fail",
    3: "failed",
    4: "audit_refused",
    5: "deleted_after_success",
    6: "system_banned_after_success",
}
MASS_SEND_STATUS_LABELS = {
    "SEND_SUCCESS": "success",
    "SENDING": "sending",
    "SEND_FAIL": "failed",
    "DELETE": "deleted",
}
FINAL_MASS_SEND_STATUSES = {"SEND_SUCCESS", "SEND_FAIL", "DELETE"}
PUBLISH_CHANNEL_FREEPUBLISH = "freepublish"
PUBLISH_CHANNEL_MASS_SEND_ALL = "mass_send_all"
PUBLISH_CHANNELS = {PUBLISH_CHANNEL_FREEPUBLISH, PUBLISH_CHANNEL_MASS_SEND_ALL}


class AmbiguousPublishSubmissionError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely publish a WeChat draft by media_id.")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--review", required=True)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--artifact-manifest", default="")
    parser.add_argument("--confirmation-file", default="")
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--poll-attempts", type=int, default=6)
    parser.add_argument("--poll-interval-seconds", type=float, default=10)
    parser.add_argument("--retry-count", type=int, default=None)
    parser.add_argument("--retry-delay-seconds", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = datetime.now(SHANGHAI_TZ)
    result = publish_draft(
        env_path=args.env,
        schedule_path=args.schedule,
        review_path=args.review,
        media_id=args.media_id,
        title=args.title,
        artifact_manifest_path=args.artifact_manifest,
        confirmation_file=args.confirmation_file,
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        poll=args.poll,
        poll_attempts=args.poll_attempts,
        poll_interval_seconds=args.poll_interval_seconds,
        retry_count=args.retry_count,
        retry_delay_seconds=args.retry_delay_seconds,
        now=now,
    )

    _print_result(result, args.json)
    return 0 if result["status"] in {"ok", "submitted", "publishing", "sending"} else 1


def publish_draft(
    *,
    env_path: str | Path = ".env",
    schedule_path: str | Path = "config/schedule.yaml",
    review_path: str | Path,
    media_id: str,
    title: str = "",
    artifact_manifest_path: str | Path = "",
    article_titles: list[str] | None = None,
    confirmation_file: str | Path = "",
    state_dir: str | Path = "data/publish-state",
    output_dir: str | Path | None = None,
    poll: bool = False,
    poll_attempts: int = 6,
    poll_interval_seconds: float = 10,
    retry_count: int | None = None,
    retry_delay_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    output_dir = Path(output_dir or f"outputs/{current:%Y-%m-%d}-publish")
    output_dir.mkdir(parents=True, exist_ok=True)

    schedule = load_layered_yaml(schedule_path)
    review = json.loads(Path(review_path).read_text(encoding="utf-8-sig"))
    publish_channel = _publish_channel(schedule)
    retry_count, retry_delay_seconds = _retry_settings(
        schedule,
        retry_count=retry_count,
        retry_delay_seconds=retry_delay_seconds,
    )

    result: dict[str, Any] = {
        "status": "unknown",
        "created_at": current.isoformat(),
        "published": False,
        "title": title,
        "article_titles": article_titles or ([title] if title else []),
        "media_id": media_id,
        "artifact_manifest": str(artifact_manifest_path),
        "publish_channel": publish_channel,
        "review": {
            "path": str(Path(review_path)),
            "risk_level": review.get("risk_level"),
        },
        "state_dir": str(Path(state_dir)),
        "output_dir": str(output_dir),
        "retry": {
            "retry_count": retry_count,
            "retry_delay_seconds": retry_delay_seconds,
        },
        "poll": {
            "enabled": bool(poll),
            "attempts": max(1, poll_attempts),
            "interval_seconds": max(0.0, poll_interval_seconds),
        },
        "effective_config": {
            "fingerprint": config_fingerprint(schedule),
            "version": str(schedule.get("runtime", {}).get("config_version", ""))
            if isinstance(schedule.get("runtime", {}), dict)
            else "",
        },
    }

    try:
        decision = decide_publish_action(
            schedule,
            review,
            already_published_today=already_published_today(state_dir, now=current),
        )
        result["decision"] = asdict(decision)
        permission = evaluate_publish_permission(
            decision,
            confirmation_file=str(confirmation_file),
        )
        result["permission"] = permission

        if not permission["ok"]:
            result["status"] = "blocked"
            append_audit_event(
                "publish_blocked",
                {
                    "title": title,
                    "media_id": media_id,
                    "publish_channel": publish_channel,
                    "decision": result["decision"],
                    "permission": permission,
                },
                state_dir,
                status="blocked",
                now=current,
            )
            _write_result(output_dir, result)
            return result

        runtime_identity = require_wechat_write_access(schedule, operation="publish WeChat draft")
        result["runtime"] = runtime_identity.public_dict()
        publishing = schedule.get("publishing", {}) if isinstance(schedule.get("publishing", {}), dict) else {}
        if bool(publishing.get("require_frozen_package", runtime_identity.environment == "production")):
            if not str(artifact_manifest_path).strip():
                raise ValueError("Frozen content package manifest is required for production publish.")
            manifest_file = Path(artifact_manifest_path)
            manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
            package_path = str(manifest_payload.get("package", "")).strip()
            frozen = verify_frozen_package(
                package_path,
                manifest_path=manifest_file,
                expected_config_fingerprint=result["effective_config"]["fingerprint"],
                expected_config_version=result["effective_config"]["version"],
            )
            result["artifact_verification"] = frozen
            if not frozen["ok"]:
                raise ValueError("Frozen content package verification failed: " + "; ".join(frozen["errors"]))
        client = client_from_env(env_path)
        token = retry_call(
            "get_access_token",
            client.get_access_token,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
            retry_if=_retryable_wechat_read_error,
        )
        submit_schedule = _schedule_with_stable_mass_send_clientmsgid(
            schedule,
            now=current,
            media_id=media_id,
            artifact_fingerprint=str(result.get("artifact_verification", {}).get("artifact_fingerprint", "")),
        )
        submit_options = _mass_send_options(submit_schedule)
        intent_path = record_publish_intent(
            {
                "title": title,
                "media_id": media_id,
                "publish_channel": publish_channel,
                "artifact_fingerprint": result.get("artifact_verification", {}).get("artifact_fingerprint", ""),
                "clientmsgid": submit_options["clientmsgid"] if publish_channel == PUBLISH_CHANNEL_MASS_SEND_ALL else "",
            },
            state_dir,
            now=current,
        )
        result["publish_intent"] = str(intent_path)
        submitted = submit_publish_request(
            client,
            token.value,
            media_id,
            schedule=submit_schedule,
            publish_channel=publish_channel,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
            now=current,
        )
        publish_id = str(submitted.get("publish_id", "")).strip()
        msg_id = str(submitted.get("msg_id", "")).strip()
        msg_data_id = str(submitted.get("msg_data_id", "")).strip()

        result["submitted"] = {
            "ok": True,
            "publish_channel": publish_channel,
            "payload": submitted,
        }
        if publish_id:
            result["submitted"]["publish_id"] = publish_id
        if msg_id:
            result["submitted"]["msg_id"] = msg_id
        if msg_data_id:
            result["submitted"]["msg_data_id"] = msg_data_id
        lock_path = record_publish_lock(
            {
                "title": title,
                "media_id": media_id,
                "publish_channel": publish_channel,
                "publish_id": publish_id,
                "msg_id": msg_id,
                "msg_data_id": msg_data_id,
                "submitted_at": current.isoformat(),
                "permission": permission,
                "artifact_fingerprint": result.get("artifact_verification", {}).get("artifact_fingerprint", ""),
            },
            state_dir,
            now=current,
        )
        result["publish_lock"] = str(lock_path)
        update_publish_intent(
            intent_path,
            status="submitted",
            payload={
                "publish_id": publish_id,
                "msg_id": msg_id,
                "msg_data_id": msg_data_id,
            },
            now=current,
        )
        append_audit_event(
            "publish_submitted",
            {
                "title": title,
                "media_id": media_id,
                "publish_channel": publish_channel,
                "publish_id": publish_id,
                "msg_id": msg_id,
                "msg_data_id": msg_data_id,
                "permission": permission,
            },
            state_dir,
            status="ok",
            now=current,
        )

        if poll:
            result["status_checks"] = poll_publish_request_status(
                client,
                token.value,
                submitted,
                publish_channel=publish_channel,
                retry_count=retry_count,
                retry_delay_seconds=retry_delay_seconds,
                state_dir=state_dir,
                attempts=max(1, poll_attempts),
                interval_seconds=max(0.0, poll_interval_seconds),
            )
            final_status = result["status_checks"][-1]["normalized_status"]
            result["published"] = final_status == "success"
            result["status"] = "ok" if final_status == "success" else final_status
            if result["published"] and publish_channel == PUBLISH_CHANNEL_FREEPUBLISH:
                result["published_articles"] = extract_published_articles(
                    result["status_checks"],
                    article_titles=result["article_titles"],
                )
                if result["published_articles"]:
                    links_path = record_published_links(
                        {
                            "title": title,
                            "media_id": media_id,
                            "publish_channel": publish_channel,
                            "publish_id": publish_id,
                            "articles": result["published_articles"],
                            "published_at": current.isoformat(),
                        },
                        state_dir,
                        now=current,
                    )
                    result["published_links_path"] = str(links_path)
                    (output_dir / "published-links.json").write_text(
                        json.dumps(result["published_articles"], ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
        else:
            result["status"] = "submitted"

    except AmbiguousPublishSubmissionError as exc:
        result["status"] = "ambiguous_submission"
        result["error"] = str(exc)
        if result.get("publish_intent"):
            update_publish_intent(
                result["publish_intent"],
                status="ambiguous",
                payload={"error": str(exc)},
                now=current,
            )
        append_audit_event(
            "publish_submission_ambiguous",
            {
                "title": title,
                "media_id": media_id,
                "publish_channel": result.get("publish_channel", ""),
                "error": str(exc),
            },
            state_dir,
            status="blocked",
            now=current,
        )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        if result.get("publish_intent"):
            update_publish_intent(
                result["publish_intent"],
                status="failed",
                payload={"error": str(exc)},
                now=current,
            )
        if isinstance(exc, WeChatApiError):
            result["payload"] = exc.payload
        append_audit_event(
            "publish_failed",
            {
                "title": title,
                "media_id": media_id,
                "publish_channel": result.get("publish_channel", ""),
                "error": str(exc),
                "payload": result.get("payload", {}),
            },
            state_dir,
            status="failed",
            now=current,
        )

    _write_result(output_dir, result)
    return result


def evaluate_publish_permission(
    decision: PublishDecision,
    *,
    confirmation_file: str = "",
) -> dict[str, Any]:
    if decision.action == "publish" and decision.allowed:
        return {
            "ok": True,
            "mode": "auto_publish_low_risk",
            "reason": decision.reason,
        }

    if decision.action == "await_manual_confirm":
        if not confirmation_file:
            return {
                "ok": False,
                "mode": "manual_confirm",
                "reason": "Manual confirmation file is required before publish.",
            }
        confirmation = load_manual_confirmation(confirmation_file)
        return {
            "ok": confirmation.ok,
            "mode": "manual_confirm",
            "reason": confirmation.reason,
            "confirmation": asdict(confirmation),
        }

    return {
        "ok": False,
        "mode": "blocked",
        "reason": f"Publish policy action is {decision.action}; publish submission is not allowed.",
    }


def submit_publish_request(
    client: Any,
    access_token: str,
    media_id: str,
    *,
    schedule: dict[str, Any],
    publish_channel: str,
    retry_count: int,
    retry_delay_seconds: float,
    state_dir: str | Path,
    now: datetime,
) -> dict[str, Any]:
    if publish_channel == PUBLISH_CHANNEL_FREEPUBLISH:
        try:
            submitted = client.submit_free_publish(access_token, media_id)
        except requests.RequestException as exc:
            raise AmbiguousPublishSubmissionError("Free-publish submission returned an ambiguous network result.") from exc
        except WeChatApiError as exc:
            if _ambiguous_wechat_write_error(exc):
                raise AmbiguousPublishSubmissionError("Free-publish submission returned an ambiguous response.") from exc
            raise
        publish_id = str(submitted.get("publish_id", "")).strip()
        if not publish_id:
            raise WeChatApiError("submit_free_publish response is missing publish_id", submitted)
        return submitted

    if publish_channel == PUBLISH_CHANNEL_MASS_SEND_ALL:
        options = _mass_send_options(schedule)
        try:
            submitted = client.send_mass_mpnews_to_all(
                access_token,
                media_id,
                send_ignore_reprint=options["send_ignore_reprint"],
                clientmsgid=options["clientmsgid"],
            )
        except requests.RequestException as exc:
            raise AmbiguousPublishSubmissionError("Mass-send submission returned an ambiguous network result.") from exc
        except WeChatApiError as exc:
            if _ambiguous_wechat_write_error(exc):
                raise AmbiguousPublishSubmissionError("Mass-send submission returned an ambiguous response.") from exc
            raise
        msg_id = str(submitted.get("msg_id", "")).strip()
        if not msg_id:
            raise WeChatApiError("send_mass_mpnews_to_all response is missing msg_id", submitted)
        if options["clientmsgid"]:
            submitted["clientmsgid"] = options["clientmsgid"]
        submitted["send_ignore_reprint"] = 1 if options["send_ignore_reprint"] else 0
        return submitted

    raise ValueError(f"Unsupported publish channel: {publish_channel}")


def poll_publish_request_status(
    client: Any,
    access_token: str,
    submitted: dict[str, Any],
    *,
    publish_channel: str,
    retry_count: int,
    retry_delay_seconds: float,
    state_dir: str | Path,
    attempts: int,
    interval_seconds: float,
    sleep: Any = time.sleep,
) -> list[dict[str, Any]]:
    if publish_channel == PUBLISH_CHANNEL_FREEPUBLISH:
        publish_id = str(submitted.get("publish_id", "")).strip()
        if not publish_id:
            raise WeChatApiError("freepublish status polling requires publish_id", submitted)
        return poll_publish_status(
            client,
            access_token,
            publish_id,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
            attempts=attempts,
            interval_seconds=interval_seconds,
            sleep=sleep,
        )

    if publish_channel == PUBLISH_CHANNEL_MASS_SEND_ALL:
        msg_id = str(submitted.get("msg_id", "")).strip()
        if not msg_id:
            raise WeChatApiError("mass send status polling requires msg_id", submitted)
        return poll_mass_send_status(
            client,
            access_token,
            msg_id,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
            attempts=attempts,
            interval_seconds=interval_seconds,
            sleep=sleep,
        )

    raise ValueError(f"Unsupported publish channel: {publish_channel}")


def poll_publish_status(
    client: Any,
    access_token: str,
    publish_id: str,
    *,
    retry_count: int,
    retry_delay_seconds: float,
    state_dir: str | Path,
    attempts: int,
    interval_seconds: float,
    sleep: Any = time.sleep,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    for attempt in range(1, attempts + 1):
        payload = retry_call(
            "get_free_publish_status",
            lambda: client.get_free_publish_status(access_token, publish_id),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
            retry_if=_retryable_wechat_read_error,
        )
        normalized = normalize_publish_status(payload)
        check = {
            "attempt": attempt,
            "payload": payload,
            **normalized,
        }
        checks.append(check)
        append_audit_event(
            "publish_status_checked",
            {
                "publish_id": publish_id,
                "attempt": attempt,
                "status": normalized,
            },
            state_dir,
            status="ok",
        )
        if normalized["raw_status"] in FINAL_PUBLISH_STATUSES:
            break
        if attempt < attempts and interval_seconds:
            sleep(interval_seconds)

    return checks


def poll_mass_send_status(
    client: Any,
    access_token: str,
    msg_id: str,
    *,
    retry_count: int,
    retry_delay_seconds: float,
    state_dir: str | Path,
    attempts: int,
    interval_seconds: float,
    sleep: Any = time.sleep,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    for attempt in range(1, attempts + 1):
        payload = retry_call(
            "get_mass_send_status",
            lambda: client.get_mass_send_status(access_token, msg_id),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
            retry_if=_retryable_wechat_read_error,
        )
        normalized = normalize_mass_send_status(payload)
        check = {
            "attempt": attempt,
            "payload": payload,
            **normalized,
        }
        checks.append(check)
        append_audit_event(
            "mass_send_status_checked",
            {
                "msg_id": msg_id,
                "attempt": attempt,
                "status": normalized,
            },
            state_dir,
            status="ok",
        )
        if normalized["raw_status"] in FINAL_MASS_SEND_STATUSES:
            break
        if attempt < attempts and interval_seconds:
            sleep(interval_seconds)

    return checks


def normalize_publish_status(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("publish_status")
    try:
        raw_status = int(raw)
    except (TypeError, ValueError):
        return {
            "raw_status": raw,
            "normalized_status": "unknown",
            "final": False,
        }

    return {
        "raw_status": raw_status,
        "normalized_status": PUBLISH_STATUS_LABELS.get(raw_status, "unknown"),
        "final": raw_status in FINAL_PUBLISH_STATUSES,
    }


def normalize_mass_send_status(payload: dict[str, Any]) -> dict[str, Any]:
    raw_status = str(payload.get("msg_status", "")).strip()
    if not raw_status:
        return {
            "raw_status": raw_status,
            "normalized_status": "unknown",
            "final": False,
        }
    return {
        "raw_status": raw_status,
        "normalized_status": MASS_SEND_STATUS_LABELS.get(raw_status, "unknown"),
        "final": raw_status in FINAL_MASS_SEND_STATUSES,
    }


def extract_published_articles(
    status_checks: list[dict[str, Any]],
    *,
    article_titles: list[str] | None = None,
) -> list[dict[str, str]]:
    titles = article_titles or []
    payload = _final_payload(status_checks)
    items = _article_items(payload)
    articles: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        url = str(item.get("article_url") or item.get("content_url") or item.get("url") or "").strip()
        article_id = str(item.get("article_id") or item.get("aid") or "").strip()
        key = url or article_id
        if not key or key in seen:
            continue
        seen.add(key)
        title = str(item.get("title") or (titles[index] if index < len(titles) else "")).strip()
        articles.append(
            {
                "index": str(index),
                "title": title,
                "article_id": article_id,
                "url": url,
            }
        )
    if not articles:
        article_id = str(payload.get("article_id", "")).strip()
        url = str(payload.get("article_url") or payload.get("content_url") or "").strip()
        if article_id or url:
            articles.append(
                {
                    "index": "0",
                    "title": titles[0] if titles else "",
                    "article_id": article_id,
                    "url": url,
                }
            )
    return articles


def _final_payload(status_checks: list[dict[str, Any]]) -> dict[str, Any]:
    if not status_checks:
        return {}
    payload = status_checks[-1].get("payload", {})
    return payload if isinstance(payload, dict) else {}


def _article_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    detail = payload.get("article_detail", {})
    if isinstance(detail, dict):
        for key in ("item", "items", "articles"):
            value = detail.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(key in detail for key in ("article_url", "content_url", "url", "article_id")):
            return [detail]
    if isinstance(detail, list):
        return [item for item in detail if isinstance(item, dict)]
    articles = payload.get("articles")
    if isinstance(articles, list):
        return [item for item in articles if isinstance(item, dict)]
    return []


def _retry_settings(
    schedule: dict[str, Any],
    *,
    retry_count: int | None,
    retry_delay_seconds: float | None,
) -> tuple[int, float]:
    publishing = schedule.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}

    configured_count = int(publishing.get("retry_count", 0) or 0)
    configured_delay = float(publishing.get("retry_delay_minutes", 0) or 0) * 60
    return (
        min(2, max(0, retry_count if retry_count is not None else configured_count)),
        max(0.0, retry_delay_seconds if retry_delay_seconds is not None else configured_delay),
    )


def _publish_channel(schedule: dict[str, Any]) -> str:
    publishing = schedule.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}
    channel = str(publishing.get("publish_channel", PUBLISH_CHANNEL_FREEPUBLISH)).strip()
    if not channel:
        return PUBLISH_CHANNEL_FREEPUBLISH
    if channel not in PUBLISH_CHANNELS:
        raise ValueError(f"Unsupported publishing.publish_channel: {channel}")
    return channel


def _mass_send_options(schedule: dict[str, Any]) -> dict[str, Any]:
    publishing = schedule.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}
    clientmsgid = str(publishing.get("mass_send_clientmsgid", "")).strip()
    return {
        "send_ignore_reprint": bool(publishing.get("mass_send_ignore_reprint", False)),
        "clientmsgid": clientmsgid,
    }


def _schedule_with_stable_mass_send_clientmsgid(
    schedule: dict[str, Any],
    *,
    now: datetime,
    media_id: str,
    artifact_fingerprint: str,
) -> dict[str, Any]:
    updated = dict(schedule)
    publishing = dict(schedule.get("publishing", {})) if isinstance(schedule.get("publishing", {}), dict) else {}
    if not str(publishing.get("mass_send_clientmsgid", "")).strip():
        identity = f"{now:%Y-%m-%d}:{media_id}:{artifact_fingerprint}"
        publishing["mass_send_clientmsgid"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    updated["publishing"] = publishing
    return updated


def _retryable_wechat_read_error(exc: Exception) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    if not isinstance(exc, WeChatApiError):
        return False
    try:
        errcode = int(exc.payload.get("errcode", 0) or 0)
    except (TypeError, ValueError):
        errcode = 0
    return errcode == -1


def _ambiguous_wechat_write_error(exc: WeChatApiError) -> bool:
    return "non-JSON response" in str(exc) or ("HTTP" in str(exc) and "errcode" not in exc.payload)


def _write_result(output_dir: Path, result: dict[str, Any]) -> None:
    (output_dir / "publish-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"Status: {result['status']}")
    if result.get("submitted"):
        print(f"Channel: {result['submitted'].get('publish_channel', result.get('publish_channel', ''))}")
        if result["submitted"].get("publish_id"):
            print(f"Publish ID: {result['submitted'].get('publish_id')}")
        if result["submitted"].get("msg_id"):
            print(f"Mass Msg ID: {result['submitted'].get('msg_id')}")
        if result["submitted"].get("msg_data_id"):
            print(f"Mass Msg Data ID: {result['submitted'].get('msg_data_id')}")
    if result.get("permission") and not result["permission"].get("ok"):
        print(f"Blocked: {result['permission'].get('reason')}")
    if result.get("error"):
        print(f"Error: {result['error']}")
    print(f"Result file: {Path(result['output_dir']) / 'publish-result.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
