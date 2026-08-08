from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_layered_yaml


SCHEDULE_SCHEMA_VERSION = "schedule.v1"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


STAGE_DEFINITIONS = [
    ("topic", "topic_time", "select_topic", "选题"),
    ("article", "draft_time", "generate_article", "生成文章草稿"),
    ("cover", "image_time", "prepare_cover", "生成或选择封面"),
    ("review", "review_time", "review_article", "审核文章"),
    ("draft_upload", "draft_upload_time", "create_draft", "创建微信草稿"),
    ("publish", "publish_time", "publish_or_hold", "发布或等待确认"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a schedule.v1 plan from generated package files.")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--article", required=True)
    parser.add_argument("--review", required=True)
    parser.add_argument("--decision", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    schedule = load_layered_yaml(args.schedule)
    article = json.loads(Path(args.article).read_text(encoding="utf-8-sig"))
    review = json.loads(Path(args.review).read_text(encoding="utf-8-sig"))
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8-sig"))
    plan = build_schedule_plan(
        schedule,
        article=article,
        review=review,
        publish_decision=decision,
        files={
            "article_json": args.article,
            "review_json": args.review,
            "publish_decision_json": args.decision,
        },
    )
    errors = validate_schedule_plan(plan)
    if errors:
        raise ValueError("Invalid schedule plan: " + "; ".join(errors))

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(f"Date: {plan['date']}")
        print(f"Mode: {plan['publishing_mode']}")
        print(f"Next action: {plan['next_action']}")
        if args.output:
            print(f"Output: {args.output}")
    return 0


def build_schedule_plan(
    schedule_config: dict[str, Any],
    *,
    article: dict[str, Any],
    review: dict[str, Any],
    publish_decision: dict[str, Any],
    files: dict[str, Any],
    seed_source: str = "",
    already_published_today: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    publishing = schedule_config.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}
    daily_run = schedule_config.get("daily_run", {})
    if not isinstance(daily_run, dict):
        daily_run = {}

    mode = str(publishing.get("mode", "dry_run"))
    decision_payload = _decision_payload(publish_decision)
    next_action, blocker = _next_action_and_blocker(
        mode,
        decision_payload,
        review,
        already_published_today=already_published_today,
    )

    artifacts = _artifact_map(files)
    return {
        "schema_version": SCHEDULE_SCHEMA_VERSION,
        "generated_at": current.isoformat(),
        "date": current.date().isoformat(),
        "timezone": str(schedule_config.get("timezone", "Asia/Shanghai")),
        "daily_run_enabled": bool(daily_run.get("enabled", True)),
        "publishing_mode": mode,
        "next_action": next_action,
        "blocker": blocker,
        "article": {
            "schema_version": str(article.get("schema_version", "")),
            "title": str(article.get("title", "")),
            "topic": str(article.get("topic", "")),
            "column": str(article.get("column", "")),
            "word_count_estimate": int(article.get("word_count_estimate", 0) or 0),
        },
        "content_package": _content_package_summary(artifacts),
        "review": {
            "schema_version": str(review.get("schema_version", "")),
            "risk_level": str(review.get("risk_level", "")),
            "wechat_ready": bool(review.get("wechat_ready", False)),
            "flags": review.get("flags", {}) if isinstance(review.get("flags", {}), dict) else {},
        },
        "publish_decision": decision_payload,
        "artifacts": artifacts,
        "timeline": _timeline(
            daily_run,
            current=current,
            artifacts=artifacts,
            seed_source=seed_source,
            next_action=next_action,
            review=review,
            decision=decision_payload,
        ),
    }


def validate_schedule_plan(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if plan.get("schema_version") != SCHEDULE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEDULE_SCHEMA_VERSION}")
    for field in ("generated_at", "date", "timezone", "publishing_mode", "next_action"):
        if not str(plan.get(field, "")).strip():
            errors.append(f"{field} is required")
    if not isinstance(plan.get("daily_run_enabled"), bool):
        errors.append("daily_run_enabled must be a boolean")
    if not isinstance(plan.get("article"), dict):
        errors.append("article must be an object")
    elif not str(plan["article"].get("title", "")).strip():
        errors.append("article.title is required")
    if not isinstance(plan.get("review"), dict):
        errors.append("review must be an object")
    elif str(plan["review"].get("risk_level", "")) not in {"low", "medium", "high"}:
        errors.append("review.risk_level must be low, medium, or high")
    if not isinstance(plan.get("publish_decision"), dict):
        errors.append("publish_decision must be an object")
    if not isinstance(plan.get("artifacts"), dict):
        errors.append("artifacts must be an object")
    timeline = plan.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        errors.append("timeline must be a non-empty list")
    else:
        seen = set()
        for index, stage in enumerate(timeline):
            if not isinstance(stage, dict):
                errors.append(f"timeline[{index}] must be an object")
                continue
            name = str(stage.get("stage", ""))
            if not name:
                errors.append(f"timeline[{index}].stage is required")
            if name in seen:
                errors.append(f"timeline[{index}].stage is duplicated")
            seen.add(name)
            if str(stage.get("status", "")) not in {"completed", "pending", "blocked", "skipped"}:
                errors.append(f"timeline[{index}].status is invalid")
    return errors


def _timeline(
    daily_run: dict[str, Any],
    *,
    current: datetime,
    artifacts: dict[str, str],
    seed_source: str,
    next_action: str,
    review: dict[str, Any],
    decision: dict[str, Any],
) -> list[dict[str, Any]]:
    completed_artifacts = {
        "topic": [seed_source] if seed_source else [],
        "article": [artifacts.get("article_json", ""), artifacts.get("article_md", "")],
        "cover": [artifacts.get("cover", "")],
        "review": [artifacts.get("review_json", "")],
        "draft_upload": [],
        "publish": [artifacts.get("manual_confirmation", "")],
    }
    timeline = []
    for stage_name, time_key, action, label in STAGE_DEFINITIONS:
        status = _stage_status(stage_name, next_action, review, decision)
        artifact_values = [value for value in completed_artifacts.get(stage_name, []) if value]
        timeline.append(
            {
                "stage": stage_name,
                "label": label,
                "action": action,
                "scheduled_time": str(daily_run.get(time_key, "")),
                "scheduled_at": _scheduled_at(current, str(daily_run.get(time_key, ""))),
                "status": status,
                "artifacts": artifact_values,
            }
        )
    return timeline


def _stage_status(
    stage_name: str,
    next_action: str,
    review: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    if stage_name in {"topic", "article", "cover", "review"}:
        return "completed"
    if stage_name == "draft_upload":
        if next_action in {"create_draft", "await_manual_confirmation", "ready_to_publish"}:
            return "pending"
        if next_action == "blocked":
            return "blocked"
        return "skipped"
    if stage_name == "publish":
        if next_action == "ready_to_publish":
            return "pending"
        if next_action == "await_manual_confirmation":
            return "pending"
        if next_action == "blocked":
            return "blocked"
        if decision.get("action") == "dry_run" or str(review.get("risk_level", "")) != "low":
            return "skipped"
    return "pending"


def _scheduled_at(current: datetime, hhmm: str) -> str:
    if not hhmm:
        return ""
    parts = hhmm.split(":")
    if len(parts) != 2:
        return ""
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return ""
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return ""
    return current.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def _decision_payload(decision: Any) -> dict[str, Any]:
    if hasattr(decision, "__dataclass_fields__"):
        return asdict(decision)
    if isinstance(decision, dict):
        return decision
    return {}


def _artifact_map(files: dict[str, Any]) -> dict[str, str]:
    return {name: str(path) for name, path in files.items() if path}


def _content_package_summary(artifacts: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": "content_package.v1" if artifacts.get("content_package_json") else "",
        "article_count": 2 if artifacts.get("secondary_article_json") else 1,
        "primary_article_json": artifacts.get("primary_article_json", artifacts.get("article_json", "")),
        "secondary_article_json": artifacts.get("secondary_article_json", ""),
        "content_package_json": artifacts.get("content_package_json", ""),
    }


def _next_action_and_blocker(
    mode: str,
    decision: dict[str, Any],
    review: dict[str, Any],
    *,
    already_published_today: bool,
) -> tuple[str, str]:
    if already_published_today:
        return "blocked", "A publish lock already exists for today."
    action = str(decision.get("action", ""))
    allowed = bool(decision.get("allowed", False))
    risk = str(review.get("risk_level", ""))

    if mode == "dry_run":
        return "none", "dry_run mode only generates local outputs."
    if mode == "draft_only":
        if action == "reject":
            return "blocked", str(decision.get("reason", "High-risk content rejected."))
        return "create_draft", ""
    if mode == "manual_confirm":
        if action == "await_manual_confirm":
            return "await_manual_confirmation", ""
        return "blocked", str(decision.get("reason", "Manual confirmation is blocked by policy."))
    if mode == "auto_publish_low_risk":
        if action == "publish" and allowed and risk == "low":
            return "ready_to_publish", ""
        return "blocked", str(decision.get("reason", "Automatic publish is blocked by policy."))
    return "blocked", f"Unsupported publishing mode: {mode}"


if __name__ == "__main__":
    raise SystemExit(main())
