from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_layered_yaml
from src.integrations.notification_client import notify_daily_run
from src.integrations.wechat_client import client_from_env
from src.pipeline.publish_from_draft import poll_publish_request_status
from src.review.publish_guard import retry_call
from src.runtime_guard import require_wechat_write_access


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch and reconcile the daily WeChat publish outcome.")
    parser.add_argument("--date", default="")
    parser.add_argument("--output-root", default="outputs")
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--expected-by", default="09:30")
    parser.add_argument("--refresh-pending", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = datetime.now(SHANGHAI_TZ)
    target = date.fromisoformat(args.date) if args.date else now.date()
    result = evaluate_publish_outcome(
        target,
        output_root=args.output_root,
        state_dir=args.state_dir,
        expected_by=args.expected_by,
        now=now,
    )
    if args.refresh_pending and _can_refresh_publish_status(result):
        result["status_refresh"] = refresh_pending_publish(
            result,
            env_path=args.env,
            schedule_path=args.schedule,
            state_dir=args.state_dir,
        )
        result = evaluate_publish_outcome(
            target,
            output_root=args.output_root,
            state_dir=args.state_dir,
            expected_by=args.expected_by,
            now=now,
        ) | {"status_refresh": result["status_refresh"]}
    result_path = _write_watchdog_result(result, state_dir=args.state_dir)
    result["result_path"] = str(result_path)
    if args.notify:
        result["notification"] = notify_daily_run(result, env_path=args.env, state_dir=args.state_dir)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Outcome: {result['business_outcome']}")
        print(f"Reason: {result['reason']}")
        print(f"Result: {result_path}")
    return 0 if result["business_outcome"] in {"published", "duplicate_skipped"} else 2


def refresh_pending_publish(
    outcome: dict[str, Any],
    *,
    env_path: str | Path,
    schedule_path: str | Path,
    state_dir: str | Path,
) -> dict[str, Any]:
    publish_path = Path(str(outcome.get("publish_result", "")))
    if not publish_path.exists():
        return {"status": "skipped", "reason": "publish result is missing"}
    try:
        publish = _read_json(publish_path)
        schedule = load_layered_yaml(schedule_path)
        require_wechat_write_access(schedule, operation="refresh WeChat publish status")
        client = client_from_env(env_path)
        publishing = schedule.get("publishing", {}) if isinstance(schedule.get("publishing", {}), dict) else {}
        retry_count = min(1, max(0, int(publishing.get("retry_count", 0) or 0)))
        retry_delay_seconds = min(5.0, max(0.0, float(publishing.get("retry_delay_minutes", 0) or 0) * 60))
        token = retry_call(
            "get_access_token_for_watchdog",
            client.get_access_token,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
        )
        submitted = publish.get("submitted", {}) if isinstance(publish.get("submitted", {}), dict) else {}
        checks = poll_publish_request_status(
            client,
            token.value,
            submitted,
            publish_channel=str(publish.get("publish_channel", "")),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
            attempts=1,
            interval_seconds=0,
        )
        existing_checks = publish.get("status_checks", []) if isinstance(publish.get("status_checks", []), list) else []
        publish["status_checks"] = [*existing_checks, *checks]
        final_status = checks[-1].get("normalized_status", "unknown") if checks else "unknown"
        publish["published"] = final_status == "success"
        publish["status"] = "ok" if publish["published"] else str(final_status)
        _atomic_write_json(publish_path, publish)
        return {"status": "ok", "final_publish_status": final_status, "published": publish["published"]}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def _can_refresh_publish_status(result: dict[str, Any]) -> bool:
    final_status = str(result.get("final_publish_status", "")).strip()
    return bool(result.get("publish_result")) and final_status not in {
        "success",
        "ok",
        "failed",
        "audit_refused",
        "deleted",
        "system_banned_after_success",
        "original_fail",
    }


def evaluate_publish_outcome(
    target_date: date,
    *,
    output_root: str | Path,
    state_dir: str | Path,
    expected_by: str = "09:30",
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    run_dir = _daily_run_dir(Path(output_root), target_date)
    daily_path = run_dir / "daily-run-result.json" if run_dir else Path()
    lock_path = Path(state_dir) / "published" / f"{target_date.isoformat()}.json"
    result: dict[str, Any] = {
        "status": "unknown",
        "business_outcome": "pending",
        "created_at": current.isoformat(),
        "target_date": target_date.isoformat(),
        "output_dir": str(run_dir or ""),
        "daily_result": str(daily_path) if run_dir else "",
        "publish_lock": str(lock_path),
        "expected_by": expected_by,
        "stage": "not_started",
        "reason": "Daily run has not produced a result yet.",
    }

    if not run_dir or not daily_path.exists():
        overdue = current >= _deadline(target_date, expected_by, current.tzinfo or SHANGHAI_TZ)
        result["status"] = "failed" if overdue else "pending"
        result["business_outcome"] = "failed" if overdue else "pending"
        result["reason"] = (
            "Daily run result is missing after the expected publish deadline."
            if overdue
            else "Daily run has not produced a result yet."
        )
        return result

    daily = _read_json(daily_path)
    result["daily_status"] = str(daily.get("status", "unknown"))
    result["next_action"] = str(daily.get("next_action", "unknown"))
    result["publishing_mode"] = str(daily.get("publishing_mode", ""))
    result["publish_channel"] = str(daily.get("publish_channel", ""))
    result["msg_id"] = str(daily.get("msg_id", ""))
    result["msg_data_id"] = str(daily.get("msg_data_id", ""))
    result["runtime"] = daily.get("runtime", {}) if isinstance(daily.get("runtime", {}), dict) else {}
    result["effective_config"] = (
        daily.get("effective_config", {}) if isinstance(daily.get("effective_config", {}), dict) else {}
    )
    result["notification_status"] = str((daily.get("notification") or {}).get("status", ""))

    manual_path = _manual_publish_result_path(run_dir)
    manual_publish = _read_json(manual_path) if manual_path else {}
    if manual_publish:
        submitted = (
            manual_publish.get("submitted", {})
            if isinstance(manual_publish.get("submitted", {}), dict)
            else {}
        )
        final_status = _final_publish_status(manual_publish)
        result["publish_result"] = str(manual_path)
        result["publish_channel"] = str(manual_publish.get("publish_channel", ""))
        result["msg_id"] = str(submitted.get("msg_id", ""))
        result["msg_data_id"] = str(submitted.get("msg_data_id", ""))
        result["final_publish_status"] = final_status
        result["manual_owner_requested"] = bool(manual_publish.get("manual_owner_requested", False))
        if final_status in {"success", "ok"} and lock_path.exists():
            result.update(
                status="ok",
                business_outcome="published",
                stage="manual_wechat_publish",
                published=True,
                reason="Owner-requested manual publish completed and the daily publish lock exists.",
            )
            return result
        if final_status in {"failed", "audit_refused", "deleted", "system_banned_after_success", "original_fail"}:
            result.update(
                status="failed",
                business_outcome="failed",
                stage="manual_wechat_publish",
                published=False,
                reason=f"Owner-requested manual publish returned final status: {final_status}.",
            )
            return result
        if submitted:
            result.update(
                status="pending",
                business_outcome="pending",
                stage="manual_wechat_publish",
                published=False,
                reason="Owner-requested manual publish is still pending confirmation.",
            )
            return result

    if result["next_action"] == "duplicate_skipped":
        if lock_path.exists():
            result.update(
                status="ok",
                business_outcome="duplicate_skipped",
                stage="deduplicated",
                reason=str(daily.get("reason", "Duplicate controller was skipped after today's publish completed.")),
            )
            return result
        overdue = current >= _deadline(target_date, expected_by, current.tzinfo or SHANGHAI_TZ)
        result.update(
            status="failed" if overdue else "pending",
            business_outcome="failed" if overdue else "pending",
            stage="deduplicated",
            reason=(
                "A duplicate controller was skipped, but no daily publish lock exists after the expected deadline."
                if overdue
                else "A duplicate controller was skipped while the active controller is still expected to publish."
            ),
        )
        return result
    if result["daily_status"] == "failed":
        result.update(
            status="failed",
            business_outcome="failed",
            stage=_last_stage(daily),
            reason=str(daily.get("error") or daily.get("reason") or "Daily run failed."),
        )
        return result
    if result["daily_status"] == "blocked" or result["next_action"] in {"blocked", "runtime_guard_blocked", "paused"}:
        result.update(
            status="blocked",
            business_outcome="blocked",
            stage=_last_stage(daily),
            reason=str(daily.get("reason") or "Daily run was blocked."),
        )
        return result

    publish_path = _publish_result_path(daily, run_dir)
    result["publish_result"] = str(publish_path) if publish_path else ""
    publish = _read_json(publish_path) if publish_path and publish_path.exists() else {}
    final_status = _final_publish_status(publish)
    result["final_publish_status"] = final_status
    result["published"] = bool(daily.get("published", False) or publish.get("published", False))

    if result["published"] and lock_path.exists() and final_status in {"success", "ok", ""}:
        result.update(
            status="ok",
            business_outcome="published",
            stage="wechat_publish",
            reason="WeChat publish completed and the daily publish lock exists.",
        )
        return result
    if final_status in {"failed", "audit_refused", "deleted", "system_banned_after_success", "original_fail"}:
        result.update(
            status="failed",
            business_outcome="failed",
            stage="wechat_publish",
            reason=f"WeChat returned final publish status: {final_status}.",
        )
        return result
    if result["next_action"] == "draft_created":
        result.update(
            status="blocked",
            business_outcome="blocked",
            stage="wechat_draft",
            reason="A draft exists, but no publish submission was recorded.",
        )
        return result

    overdue = current >= _deadline(target_date, expected_by, current.tzinfo or SHANGHAI_TZ)
    result.update(
        status="failed" if overdue else "pending",
        business_outcome="failed" if overdue else "pending",
        stage="wechat_publish" if publish else _last_stage(daily),
        reason=(
            "Publish did not reach a confirmed success state before the expected deadline."
            if overdue
            else "Publish is still pending confirmation."
        ),
    )
    return result


def _daily_run_dir(output_root: Path, target_date: date) -> Path | None:
    exact = output_root / f"{target_date.isoformat()}-daily-run"
    if exact.is_dir():
        return exact
    candidates = sorted(output_root.glob(f"{target_date.isoformat()}-daily-run*"), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def _publish_result_path(daily: dict[str, Any], run_dir: Path) -> Path | None:
    configured = str(daily.get("publish_result", "")).strip()
    if configured:
        return Path(configured)
    default = run_dir / "wechat-publish" / "publish-result.json"
    return default if default.exists() else None


def _manual_publish_result_path(run_dir: Path) -> Path | None:
    candidates = [
        path
        for path in run_dir.glob("manual-publish-*/manual-publish-result.json")
        if path.is_file()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _final_publish_status(publish: dict[str, Any]) -> str:
    checks = publish.get("status_checks", [])
    if isinstance(checks, list) and checks:
        last = checks[-1]
        if isinstance(last, dict):
            normalized = str(last.get("normalized_status", "")).strip()
            if normalized:
                return normalized
            payload = last.get("payload", {})
            if isinstance(payload, dict):
                msg_status = str(payload.get("msg_status", "")).strip()
                if msg_status == "SEND_SUCCESS":
                    return "success"
                if msg_status == "SEND_FAIL":
                    return "failed"
    if bool(publish.get("published", False)):
        return "success"
    return str(publish.get("status", "")).strip()


def _last_stage(daily: dict[str, Any]) -> str:
    stages = daily.get("stages", [])
    if isinstance(stages, list) and stages:
        last = stages[-1]
        if isinstance(last, dict):
            return str(last.get("name", "unknown"))
    return "controller"


def _deadline(target_date: date, value: str, tzinfo: timezone) -> datetime:
    hour, minute = (int(part) for part in value.split(":", 1))
    return datetime.combine(target_date, time(hour, minute), tzinfo=tzinfo)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_watchdog_result(result: dict[str, Any], *, state_dir: str | Path) -> Path:
    directory = Path(state_dir) / "watchdog"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{result['target_date']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
