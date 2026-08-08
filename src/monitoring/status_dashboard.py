from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.monitoring.publish_watchdog import evaluate_publish_outcome


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def build_operations_status(
    *,
    output_root: str | Path,
    state_dir: str | Path,
    current_release: str | Path = "",
    config_fingerprint: str = "",
    config_version: str = "",
    now: datetime | None = None,
    days: int = 7,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    rows = []
    for offset in range(max(1, days)):
        target = current.date() - timedelta(days=offset)
        outcome = evaluate_publish_outcome(
            target,
            output_root=output_root,
            state_dir=state_dir,
            now=current,
        )
        rows.append(_status_row(outcome))

    latest = rows[0] if rows else {}
    last_published = next((row["date"] for row in rows if row.get("outcome") == "published"), "")
    release_path = Path(current_release) if str(current_release).strip() else Path.cwd()
    try:
        release = release_path.resolve().name
    except Exception:
        release = str(release_path)
    return {
        "generated_at": current.isoformat(),
        "current_release": release,
        "config_version": config_version,
        "config_fingerprint": config_fingerprint,
        "last_published_date": last_published,
        "notification_status": latest.get("notification_status", "unknown"),
        "latest": latest,
        "runs": rows,
    }


def _status_row(outcome: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(outcome.get("output_dir", ""))) if outcome.get("output_dir") else None
    package = _read_json(run_dir / "content-package" / "content-package.json") if run_dir else {}
    daily = _read_json(run_dir / "daily-run-result.json") if run_dir else {}
    primary = _article_metrics(run_dir, "primary") if run_dir else {}
    secondary = _article_metrics(run_dir, "secondary") if run_dir else {}
    model_statuses = _model_statuses(package)
    return {
        "date": str(outcome.get("target_date", "")),
        "outcome": str(outcome.get("business_outcome", "unknown")),
        "status": str(outcome.get("status", "unknown")),
        "stage": str(outcome.get("stage", "")),
        "reason": str(outcome.get("reason", "")),
        "publish_channel": str(outcome.get("publish_channel", "")),
        "final_publish_status": str(outcome.get("final_publish_status", "")),
        "msg_id": str(outcome.get("msg_id", "")),
        "msg_data_id": str(outcome.get("msg_data_id", "")),
        "notification_status": str(outcome.get("notification_status", "")),
        "copy_quality_ok": package.get("copy_quality", {}).get("ok") if isinstance(package.get("copy_quality"), dict) else None,
        "package_risk_level": str(package.get("package_risk_level", "")),
        "model_statuses": model_statuses,
        "model_calls": _model_call_metrics(package),
        "stage_durations": _stage_durations(daily),
        "execution_budget": daily.get("execution_budget", {}),
        "resumed_stages": daily.get("resumed_stages", []),
        "source_health": _source_health(daily),
        "primary": primary,
        "secondary": secondary,
    }


def _article_metrics(run_dir: Path, role: str) -> dict[str, Any]:
    article = _read_json(run_dir / "content-package" / role / "article.json")
    items = article.get("news_items", []) if isinstance(article.get("news_items", []), list) else []
    images = article.get("inline_images", []) if isinstance(article.get("inline_images", []), list) else []
    sources = sorted({str(item.get("source_name", "")).strip() for item in items if isinstance(item, dict) and str(item.get("source_name", "")).strip()})
    final_review = article.get("final_ai_review", {}) if isinstance(article.get("final_ai_review", {}), dict) else {}
    replenishment = article.get("news_replenishment", {}) if isinstance(article.get("news_replenishment", {}), dict) else {}
    recovery = article.get("final_review_recovery", {}) if isinstance(article.get("final_review_recovery", {}), dict) else {}
    return {
        "title": str(article.get("title", "")),
        "news_items": len(items),
        "sources": len(sources),
        "source_names": sources,
        "inline_images": len(images),
        "image_provider": str(article.get("image_provider", "")),
        "final_review_status": str(final_review.get("status", "")),
        "final_review_model": str(final_review.get("model", "")),
        "final_review_role": str(final_review.get("reviewer_role", "")),
        "final_review_fallback": bool(final_review.get("fallback_used", False)),
        "recovery_cycles": len(recovery.get("cycles", [])) if isinstance(recovery.get("cycles", []), list) else 0,
        "replenished_items": sum(
            int(run.get("added", 0) or 0)
            for run in replenishment.get("runs", [])
            if isinstance(run, dict)
        ) if isinstance(replenishment.get("runs", []), list) else 0,
        "remaining_reserve": _remaining_reserve(replenishment),
    }


def _model_statuses(package: dict[str, Any]) -> dict[str, dict[str, str]]:
    articles = package.get("text_model_generation", {}).get("articles", {})
    if not isinstance(articles, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for article_role, data in articles.items():
        roles = data.get("roles", {}) if isinstance(data, dict) else {}
        if not isinstance(roles, dict):
            continue
        result[str(article_role)] = {
            str(role): str(value.get("status", ""))
            for role, value in roles.items()
            if isinstance(value, dict)
        }
        final_review = data.get("final_review", {}) if isinstance(data, dict) else {}
        if isinstance(final_review, dict) and final_review:
            reviewer = str(final_review.get("reviewer_role", ""))
            status = str(final_review.get("status", ""))
            result[str(article_role)]["final_review"] = f"{status}:{reviewer}" if reviewer else status
    return result


def _model_call_metrics(package: dict[str, Any]) -> dict[str, Any]:
    generation = package.get("text_model_generation", {}) if isinstance(package.get("text_model_generation", {}), dict) else {}
    calls = generation.get("calls", []) if isinstance(generation.get("calls", []), list) else []
    calls = [call for call in calls if isinstance(call, dict)]
    by_role: dict[str, dict[str, float | int]] = {}
    total_tokens = 0
    for call in calls:
        role = str(call.get("role", "unknown"))
        metrics = by_role.setdefault(role, {"calls": 0, "failures": 0, "duration_seconds": 0.0, "tokens": 0})
        metrics["calls"] = int(metrics["calls"]) + 1
        metrics["duration_seconds"] = round(float(metrics["duration_seconds"]) + float(call.get("duration_seconds", 0) or 0), 3)
        if str(call.get("outcome", "")) != "ok":
            metrics["failures"] = int(metrics["failures"]) + 1
        usage = call.get("usage", {}) if isinstance(call.get("usage", {}), dict) else {}
        tokens = int(usage.get("total_tokens", 0) or 0)
        metrics["tokens"] = int(metrics["tokens"]) + tokens
        total_tokens += tokens
    return {
        "total": len(calls),
        "failures": sum(1 for call in calls if str(call.get("outcome", "")) != "ok"),
        "duration_seconds": round(sum(float(call.get("duration_seconds", 0) or 0) for call in calls), 3),
        "total_tokens": total_tokens,
        "by_role": by_role,
    }


def _stage_durations(daily: dict[str, Any]) -> dict[str, float]:
    stages = daily.get("stages", []) if isinstance(daily.get("stages", []), list) else []
    return {
        str(stage.get("name", "")): float(stage.get("duration_seconds", 0) or 0)
        for stage in stages
        if isinstance(stage, dict) and str(stage.get("name", ""))
    }


def _source_health(daily: dict[str, Any]) -> dict[str, Any]:
    stages = daily.get("stages", []) if isinstance(daily.get("stages", []), list) else []
    content = next(
        (stage for stage in stages if isinstance(stage, dict) and stage.get("name") == "content_package"),
        {},
    )
    result: dict[str, Any] = {}
    for key in ("news_fetch", "manufacturing_news_fetch"):
        value = content.get(key, {}) if isinstance(content, dict) else {}
        diagnostics = value.get("diagnostics", []) if isinstance(value, dict) else []
        diagnostics = [item for item in diagnostics if isinstance(item, dict)] if isinstance(diagnostics, list) else []
        result[key] = {
            "status": str(value.get("status", "")) if isinstance(value, dict) else "",
            "successful_sources": sum(1 for item in diagnostics if item.get("status") == "ok" and item.get("matched_items")),
            "failed_sources": sum(1 for item in diagnostics if item.get("status") == "failed"),
            "cache_hits": sum(1 for item in diagnostics if item.get("cache_hit")),
        }
    return result


def _remaining_reserve(replenishment: dict[str, Any]) -> int:
    runs = replenishment.get("runs", []) if isinstance(replenishment.get("runs", []), list) else []
    for run in reversed(runs):
        if isinstance(run, dict) and "remaining_reserve" in run:
            return int(run.get("remaining_reserve", 0) or 0)
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
