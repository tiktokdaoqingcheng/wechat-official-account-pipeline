from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_layered_yaml
from src.integrations.notification_client import notify_daily_run
from src.monitoring.outcomes import apply_business_outcome
from src.pipeline.angle_memory import record_angles
from src.pipeline.create_draft_from_package import create_draft_from_package
from src.pipeline.daily_dry_run import generate_dry_run_package
from src.pipeline.package_manifest import verify_frozen_package
from src.pipeline.publish_from_draft import publish_draft
from src.pipeline.run_archive import write_run_archive
from src.review.publish_guard import append_audit_event
from src.review.publish_guard import already_published_today
from src.run_lease import acquire_daily_run_lease
from src.run_checkpoint import checkpoint_stage, load_run_checkpoint, record_run_stage
from src.runtime_budget import ExecutionBudget
from src.runtime_guard import config_fingerprint, resolve_runtime_identity
from src.storage.local_db import record_daily_run


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class DailyStageBlocked(RuntimeError):
    def __init__(self, next_action: str, message: str):
        super().__init__(message)
        self.next_action = next_action


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the daily WeChat automation controller.")
    parser.add_argument("--seed", default="")
    parser.add_argument("--topics", default="config/topics.yaml")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--safety", default="config/safety-rules.yaml")
    parser.add_argument("--news-sources", default="config/news-sources.yaml")
    parser.add_argument("--news-seed", default="")
    parser.add_argument("--manufacturing-news-sources", default="config/manufacturing-news-sources.yaml")
    parser.add_argument("--manufacturing-news-seed", default="")
    parser.add_argument("--skip-fetch-news", action="store_true")
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--output-root", default="outputs")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--db", default="data/app.sqlite")
    parser.add_argument("--stage", choices=("all", "prepare", "publish"), default="all")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_daily(
        seed_path=args.seed,
        topics_path=args.topics,
        schedule_path=args.schedule,
        safety_path=args.safety,
        news_sources_path=args.news_sources,
        news_seed_path=args.news_seed,
        manufacturing_news_sources_path=args.manufacturing_news_sources,
        manufacturing_news_seed_path=args.manufacturing_news_seed,
        fetch_news=not args.skip_fetch_news,
        state_dir=args.state_dir,
        output_root=args.output_root,
        env_path=args.env,
        db_path=args.db,
        run_stage=args.stage,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status: {result['status']}")
        print(f"Mode: {result['publishing_mode']}")
        print(f"Next action: {result['next_action']}")
        print(f"Run output: {result['output_dir']}")

    if result["status"] == "ok":
        return 0
    if result["status"] == "blocked":
        return 2
    return 1


def run_daily(
    *,
    seed_path: str | Path = "",
    topics_path: str | Path = "config/topics.yaml",
    schedule_path: str | Path = "config/schedule.yaml",
    safety_path: str | Path = "config/safety-rules.yaml",
    news_sources_path: str | Path = "config/news-sources.yaml",
    news_seed_path: str | Path = "",
    manufacturing_news_sources_path: str | Path = "config/manufacturing-news-sources.yaml",
    manufacturing_news_seed_path: str | Path = "",
    fetch_news: bool = False,
    state_dir: str | Path = "data/publish-state",
    output_root: str | Path = "outputs",
    env_path: str | Path = ".env",
    db_path: str | Path = "data/app.sqlite",
    run_stage: str = "all",
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    run_dir = Path(output_root) / f"{current:%Y-%m-%d}-daily-run"

    schedule = load_layered_yaml(schedule_path)
    daily_run = schedule.get("daily_run", {})
    if not isinstance(daily_run, dict):
        daily_run = {}
    publishing = schedule.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}
    mode = str(publishing.get("mode", "dry_run"))
    normalized_stage = run_stage if run_stage in {"all", "prepare", "publish"} else "all"
    runtime_identity = resolve_runtime_identity(schedule)
    effective_config_fingerprint = config_fingerprint(schedule)
    execution_budget = ExecutionBudget(
        _positive_number(daily_run.get("max_runtime_seconds", 840), default=840),
        publish_reserve_seconds=_positive_number(
            daily_run.get("publish_reserve_seconds", 120),
            default=120,
            allow_zero=True,
        ),
    )

    result: dict[str, Any] = {
        "status": "unknown",
        "created_at": current.isoformat(),
        "publishing_mode": mode,
        "run_stage": normalized_stage,
        "seed": str(seed_path),
        "topics": str(topics_path),
        "schedule": str(schedule_path),
        "safety": str(safety_path),
        "news_sources": str(news_sources_path),
        "news_seed": str(news_seed_path),
        "manufacturing_news_sources": str(manufacturing_news_sources_path),
        "manufacturing_news_seed": str(manufacturing_news_seed_path),
        "fetch_news": fetch_news,
        "state_dir": str(state_dir),
        "env": str(env_path),
        "db": str(db_path),
        "output_dir": str(run_dir),
        "stages": [],
        "next_action": "unknown",
        "runtime": runtime_identity.public_dict(),
        "effective_config": {
            "version": runtime_identity.config_version,
            "fingerprint": effective_config_fingerprint,
            "layers": schedule.get("config_layers", {}),
        },
        "execution_budget": execution_budget.snapshot(),
        "resumed_stages": [],
    }

    runtime_guard_reasons = list(runtime_identity.reasons)
    if not runtime_identity.allow_external_providers:
        runtime_guard_reasons.append("runtime.allow_external_providers is false")
    if mode != "dry_run" and runtime_guard_reasons:
        run_dir.mkdir(parents=True, exist_ok=True)
        result["status"] = "blocked"
        result["next_action"] = "runtime_guard_blocked"
        result["reason"] = "Runtime identity does not allow external provider or WeChat write operations."
        result["runtime_guard_reasons"] = runtime_guard_reasons
        append_audit_event(
            "daily_runner_runtime_guard_blocked",
            {
                "output_dir": str(run_dir),
                "publishing_mode": mode,
                "runtime": runtime_identity.public_dict(),
            },
            state_dir,
            status="blocked",
            now=current,
        )
        _finalize_result(
            result,
            run_dir=run_dir,
            output_root=output_root,
            env_path=env_path,
            state_dir=state_dir,
            db_path=db_path,
            now=current,
        )
        return result

    if not bool(daily_run.get("enabled", True)) or bool(daily_run.get("paused", False)):
        run_dir.mkdir(parents=True, exist_ok=True)
        result["status"] = "blocked"
        result["next_action"] = "paused"
        result["reason"] = "Daily run is paused or disabled in config/schedule.yaml."
        append_audit_event(
            "daily_runner_paused",
            {
                "output_dir": str(run_dir),
                "enabled": bool(daily_run.get("enabled", True)),
                "paused": bool(daily_run.get("paused", False)),
            },
            state_dir,
            status="blocked",
            now=current,
        )
        _finalize_result(
            result,
            run_dir=run_dir,
            output_root=output_root,
            env_path=env_path,
            state_dir=state_dir,
            db_path=db_path,
            now=current,
        )
        return result

    if already_published_today(state_dir, now=current):
        run_dir = Path(output_root) / f"{current:%Y-%m-%d}-daily-run-duplicate-{current:%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        result["output_dir"] = str(run_dir)
        result["status"] = "ok"
        result["next_action"] = "duplicate_skipped"
        result["reason"] = "A publish lock already exists for today; generation and external provider calls were skipped."
        _finalize_result(
            result,
            run_dir=run_dir,
            output_root=output_root,
            env_path=env_path,
            state_dir=state_dir,
            db_path=db_path,
            now=current,
        )
        return result

    lease = acquire_daily_run_lease(
        state_dir,
        now=current,
        instance_id=runtime_identity.instance_id,
    )
    if not lease.acquired:
        run_dir = Path(output_root) / f"{current:%Y-%m-%d}-daily-run-duplicate-{current:%H%M%S}"
        run_dir.mkdir(parents=True, exist_ok=True)
        result["output_dir"] = str(run_dir)
        result["status"] = "ok"
        result["next_action"] = "duplicate_skipped"
        result["reason"] = "Another daily controller already holds today's run lease."
        result["run_lease"] = {
            "path": str(lease.path),
            "existing_instance_id": str((lease.existing or {}).get("instance_id", "")),
            "existing_created_at": str((lease.existing or {}).get("created_at", "")),
        }
        _finalize_result(
            result,
            run_dir=run_dir,
            output_root=output_root,
            env_path=env_path,
            state_dir=state_dir,
            db_path=db_path,
            now=current,
        )
        return result

    run_dir.mkdir(parents=True, exist_ok=True)
    result["run_lease"] = {"path": str(lease.path), "acquired": True}
    checkpoint = load_run_checkpoint(
        run_dir,
        config_fingerprint=effective_config_fingerprint,
        config_version=runtime_identity.config_version,
    )
    result["checkpoint"] = str(run_dir / "run-state.json")
    result["prepare_budget"] = _prepare_budget_public_state(
        checkpoint_stage(checkpoint, "prepare_budget"),
        daily_run,
    )
    active_stage = "content_package"

    try:
        dry_run_dir = run_dir / "content-package"
        stage_started = time.perf_counter()
        dry_run = _resumable_content_package(
            checkpoint,
            mode=mode,
            expected_config_fingerprint=effective_config_fingerprint,
            expected_config_version=runtime_identity.config_version,
        )
        resumed_content = bool(dry_run)
        if resumed_content:
            result["resumed_stages"].append("content_package")
        elif normalized_stage == "publish":
            raise DailyStageBlocked(
                "prepare_required",
                "Publish stage requires a completed frozen content package from the prepare stage.",
            )
        else:
            prepare_state = _start_prepare_attempt(
                checkpoint_stage(checkpoint, "prepare_budget"),
                daily_run,
            )
            if not prepare_state["allowed"]:
                raise DailyStageBlocked(
                    str(prepare_state["next_action"]),
                    str(prepare_state["reason"]),
                )
            execution_budget.set_provider_call_limit(int(prepare_state["provider_call_limit"]))
            prepare_checkpoint = _record_checkpoint_safely(
                run_dir,
                stage="prepare_budget",
                data={
                    "status": "running",
                    "attempts_started": prepare_state["attempts_started"],
                    "provider_calls_cumulative": prepare_state["provider_calls_cumulative"],
                    "active_call_reservation": prepare_state["provider_call_limit"],
                    "max_prepare_attempts_per_day": prepare_state["max_prepare_attempts_per_day"],
                    "max_text_model_calls_per_attempt": prepare_state["max_text_model_calls_per_attempt"],
                    "max_text_model_calls_per_day": prepare_state["max_text_model_calls_per_day"],
                },
                config_fingerprint=effective_config_fingerprint,
                config_version=runtime_identity.config_version,
                now=current,
            )
            if not prepare_checkpoint:
                raise DailyStageBlocked(
                    "prepare_checkpoint_failed",
                    "Prepare was stopped because its paid-call budget could not be checkpointed safely.",
                )
            checkpoint = prepare_checkpoint
            generation_completed = False
            generation_error = ""
            attempt_archive = ""
            try:
                dry_run = generate_dry_run_package(
                    seed_path=seed_path,
                    topics_path=topics_path,
                    schedule_path=schedule_path,
                    safety_path=safety_path,
                    news_sources_path=news_sources_path,
                    news_seed_path=news_seed_path,
                    manufacturing_news_sources_path=manufacturing_news_sources_path,
                    manufacturing_news_seed_path=manufacturing_news_seed_path,
                    fetch_news=fetch_news,
                    state_dir=state_dir,
                    env_path=env_path,
                    generate_images=(
                        mode in {"draft_only", "manual_confirm", "auto_publish_low_risk"}
                        and execution_budget.can_start_optional(90)
                    ),
                    output_dir=dry_run_dir,
                    execution_budget=execution_budget,
                    now=current,
                )
                generation_completed = True
                attempt_archive = _archive_prepare_attempt(
                    run_dir=run_dir,
                    package_dir=dry_run_dir,
                    attempt_number=int(prepare_state["attempts_started"]),
                    dry_run=dry_run,
                    now=current,
                )
            except Exception as exc:
                generation_error = str(exc)
                raise
            finally:
                provider_calls_used = execution_budget.provider_calls_used()
                completed_prepare = _record_checkpoint_safely(
                    run_dir,
                    stage="prepare_budget",
                    data={
                        "status": "completed" if generation_completed else "failed",
                        "attempts_started": prepare_state["attempts_started"],
                        "provider_calls_cumulative": (
                            int(prepare_state["provider_calls_cumulative"]) + provider_calls_used
                        ),
                        "provider_calls_last_attempt": provider_calls_used,
                        "active_call_reservation": 0,
                        "max_prepare_attempts_per_day": prepare_state["max_prepare_attempts_per_day"],
                        "max_text_model_calls_per_attempt": prepare_state["max_text_model_calls_per_attempt"],
                        "max_text_model_calls_per_day": prepare_state["max_text_model_calls_per_day"],
                        "attempt_archive": attempt_archive,
                        "error": generation_error,
                    },
                    config_fingerprint=effective_config_fingerprint,
                    config_version=runtime_identity.config_version,
                    now=current,
                )
                if completed_prepare:
                    checkpoint = completed_prepare
                result["prepare_budget"] = _prepare_budget_public_state(
                    checkpoint_stage(checkpoint, "prepare_budget"),
                    daily_run,
                )
            checkpoint = _record_checkpoint_safely(
                run_dir,
                stage="content_package",
                data={
                    "status": "completed",
                    "dry_run_result": str(Path(dry_run["output_dir"]) / "dry-run-result.json"),
                    "content_package": dry_run.get("files", {}).get("content_package_json", ""),
                    "artifact_manifest": dry_run.get("files", {}).get("content_package_manifest", ""),
                    "publish_allowed": bool(dry_run.get("publish_allowed", False)),
                    "retry_eligible": _content_package_retry_eligible(dry_run),
                    "attempt_archive": attempt_archive,
                },
                config_fingerprint=effective_config_fingerprint,
                config_version=runtime_identity.config_version,
                now=current,
            ) or checkpoint
        content_duration = round(time.perf_counter() - stage_started, 3)
        result["stages"].append(
            {
                "name": "content_package",
                "status": "resumed" if resumed_content else dry_run["status"],
                "resumed": resumed_content,
                "duration_seconds": content_duration,
                "output_dir": dry_run["output_dir"],
                "title": dry_run["title"],
                "secondary_title": dry_run.get("secondary_title", ""),
                "article_count": dry_run.get("article_count", 1),
                "risk_level": dry_run["risk_level"],
                "publish_action": dry_run["publish_action"],
                "schedule_plan": dry_run.get("files", {}).get("schedule_plan_json", ""),
                "news_fetch": dry_run.get("news_fetch", {}),
                "manufacturing_news_fetch": dry_run.get("manufacturing_news_fetch", {}),
                "image_generation": dry_run.get("image_generation", {}),
            }
        )
        text_model_generation = (
            dry_run.get("text_model_generation", {})
            if isinstance(dry_run.get("text_model_generation", {}), dict)
            else {}
        )
        result["model_calls"] = (
            text_model_generation.get("calls", [])
            if isinstance(text_model_generation.get("calls", []), list)
            else []
        )

        result.update(_next_step_for_mode(mode, dry_run, schedule))
        if mode in {"draft_only", "auto_publish_low_risk"} and result.get("next_action") in {
            "create_draft",
            "ready_to_publish",
        }:
            active_stage = "wechat_draft"
            stage_started = time.perf_counter()
            draft_checkpoint = checkpoint_stage(checkpoint, "wechat_draft")
            resumed_draft = bool(
                draft_checkpoint.get("status") == "completed"
                and str(draft_checkpoint.get("draft_media_id", "")).strip()
            )
            if resumed_draft:
                draft_result = {
                    "status": "ok",
                    "output_dir": str(draft_checkpoint.get("output_dir", run_dir / "wechat-draft")),
                    "article_count": int(draft_checkpoint.get("article_count", dry_run.get("article_count", 0)) or 0),
                    "draft": {"media_id": str(draft_checkpoint.get("draft_media_id", ""))},
                }
                result["resumed_stages"].append("wechat_draft")
            elif normalized_stage == "publish":
                raise DailyStageBlocked(
                    "draft_not_ready",
                    "Publish stage requires a completed WeChat draft checkpoint and will not create one at publish time.",
                )
            else:
                draft_result = create_draft_from_package(
                    package_path=dry_run.get("files", {}).get("content_package_json", ""),
                    env_path=env_path,
                    schedule_path=schedule_path,
                    output_dir=run_dir / "wechat-draft",
                    state_dir=state_dir,
                    now=current,
                )
                checkpoint = _record_checkpoint_safely(
                    run_dir,
                    stage="wechat_draft",
                    data={
                        "status": "completed" if draft_result["status"] == "ok" else "failed",
                        "output_dir": draft_result["output_dir"],
                        "article_count": draft_result.get("article_count", 0),
                        "draft_media_id": draft_result.get("draft", {}).get("media_id", ""),
                        "error": draft_result.get("error", ""),
                    },
                    config_fingerprint=effective_config_fingerprint,
                    config_version=runtime_identity.config_version,
                    now=current,
                ) or checkpoint
            draft_duration = round(time.perf_counter() - stage_started, 3)
            result["stages"].append(
                {
                    "name": "wechat_draft",
                    "status": "resumed" if resumed_draft else draft_result["status"],
                    "resumed": resumed_draft,
                    "duration_seconds": draft_duration,
                    "output_dir": draft_result["output_dir"],
                    "article_count": draft_result.get("article_count", 0),
                    "draft_media_id": draft_result.get("draft", {}).get("media_id", ""),
                }
            )
            result["draft_result"] = str(Path(draft_result["output_dir"]) / "create-draft-result.json")
            result["artifact_manifest"] = dry_run.get("files", {}).get("content_package_manifest", "")
            if draft_result["status"] == "ok":
                result["next_action"] = (
                    "prepared_for_publish"
                    if normalized_stage == "prepare" and mode == "auto_publish_low_risk"
                    else "draft_created"
                )
                result["draft_media_id"] = draft_result.get("draft", {}).get("media_id", "")
                result["reason"] = (
                    "Prepare stage created and froze the WeChat draft for the scheduled publish stage."
                    if result["next_action"] == "prepared_for_publish"
                    else "draft_only mode created a WeChat draft and did not publish."
                )
            else:
                result["next_action"] = "blocked"
                result["reason"] = f"WeChat draft creation failed: {draft_result.get('error', 'unknown error')}"
            if (
                mode == "auto_publish_low_risk"
                and normalized_stage in {"all", "publish"}
                and result.get("next_action") == "draft_created"
                and result.get("draft_media_id")
            ):
                active_stage = "wechat_publish"
                stage_started = time.perf_counter()
                publish_checkpoint = checkpoint_stage(checkpoint, "wechat_publish")
                resumed_publish = bool(
                    publish_checkpoint.get("status") in {"submitted", "publishing", "sending", "published"}
                    and (
                        str(publish_checkpoint.get("publish_id", "")).strip()
                        or str(publish_checkpoint.get("msg_id", "")).strip()
                    )
                )
                if resumed_publish:
                    publish_result = _publish_result_from_checkpoint(publish_checkpoint, run_dir=run_dir)
                    result["resumed_stages"].append("wechat_publish")
                else:
                    publish_result = publish_draft(
                        env_path=env_path,
                        schedule_path=schedule_path,
                        review_path=dry_run.get("files", {}).get("review_json", ""),
                        media_id=str(result["draft_media_id"]),
                        title=str(dry_run.get("title", "")),
                        artifact_manifest_path=result.get("artifact_manifest", ""),
                        article_titles=[
                            title
                            for title in (str(dry_run.get("title", "")), str(dry_run.get("secondary_title", "")))
                            if title
                        ],
                        state_dir=state_dir,
                        output_dir=run_dir / "wechat-publish",
                        poll=True,
                        now=current,
                    )
                    submitted = publish_result.get("submitted", {})
                    checkpoint_status = "published" if publish_result.get("published") else str(publish_result.get("status", "failed"))
                    checkpoint = _record_checkpoint_safely(
                        run_dir,
                        stage="wechat_publish",
                        data={
                            "status": checkpoint_status,
                            "output_dir": publish_result["output_dir"],
                            "publish_channel": publish_result.get("publish_channel", ""),
                            "publish_id": submitted.get("publish_id", ""),
                            "msg_id": submitted.get("msg_id", ""),
                            "msg_data_id": submitted.get("msg_data_id", ""),
                            "published": bool(publish_result.get("published", False)),
                            "published_articles": publish_result.get("published_articles", []),
                            "error": publish_result.get("error", ""),
                        },
                        config_fingerprint=effective_config_fingerprint,
                        config_version=runtime_identity.config_version,
                        now=current,
                    ) or checkpoint
                publish_duration = round(time.perf_counter() - stage_started, 3)
                result["stages"].append(
                    {
                        "name": "wechat_publish",
                        "status": "resumed" if resumed_publish else publish_result["status"],
                        "resumed": resumed_publish,
                        "duration_seconds": publish_duration,
                        "output_dir": publish_result["output_dir"],
                        "publish_channel": publish_result.get("publish_channel", ""),
                        "publish_id": publish_result.get("submitted", {}).get("publish_id", ""),
                        "msg_id": publish_result.get("submitted", {}).get("msg_id", ""),
                        "msg_data_id": publish_result.get("submitted", {}).get("msg_data_id", ""),
                        "published": bool(publish_result.get("published", False)),
                        "published_articles": publish_result.get("published_articles", []),
                    }
                )
                result["publish_result"] = str(Path(publish_result["output_dir"]) / "publish-result.json")
                if publish_result["status"] in {"ok", "submitted", "publishing", "sending"}:
                    submitted = publish_result.get("submitted", {})
                    result["next_action"] = "publish_submitted"
                    result["publish_channel"] = publish_result.get("publish_channel", "")
                    result["publish_id"] = submitted.get("publish_id", "")
                    result["msg_id"] = submitted.get("msg_id", "")
                    result["msg_data_id"] = submitted.get("msg_data_id", "")
                    result["published"] = bool(publish_result.get("published", False))
                    result["published_articles"] = publish_result.get("published_articles", [])
                    result["reason"] = (
                        "auto_publish_low_risk mode created a draft and submitted it "
                        f"via {result['publish_channel'] or 'configured publish channel'}."
                    )
                elif publish_result["status"] == "ambiguous_submission":
                    result["next_action"] = "ambiguous_submission"
                    result["reason"] = "WeChat submission result is ambiguous; automatic resubmission is forbidden."
                else:
                    result["next_action"] = "blocked"
                    result["reason"] = f"WeChat publish failed: {publish_result.get('error', publish_result['status'])}"
        result["schedule_plan"] = dry_run.get("files", {}).get("schedule_plan_json", "")
        if result["next_action"] == "ambiguous_submission":
            result["status"] = "blocked"
        elif result["next_action"] == "blocked" and mode == "auto_publish_low_risk":
            result["status"] = "failed" if result.get("publish_result") else "blocked"
        else:
            result["status"] = "ok" if result["next_action"] != "blocked" else "blocked"
        result["execution_budget"] = execution_budget.snapshot()
        append_audit_event(
            "daily_runner_completed",
            {
                "output_dir": str(run_dir),
                "publishing_mode": mode,
                "next_action": result["next_action"],
                "title": dry_run["title"],
            },
            state_dir,
            status=result["status"],
            now=current,
        )
    except DailyStageBlocked as exc:
        result["status"] = "blocked"
        result["next_action"] = exc.next_action
        result["reason"] = str(exc)
        result["execution_budget"] = execution_budget.snapshot()
        append_audit_event(
            "daily_runner_stage_not_ready",
            {
                "output_dir": str(run_dir),
                "run_stage": normalized_stage,
                "next_action": exc.next_action,
                "reason": str(exc),
            },
            state_dir,
            status="blocked",
            now=current,
        )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        result["execution_budget"] = execution_budget.snapshot()
        _record_checkpoint_safely(
            run_dir,
            stage=active_stage,
            data={"status": "failed", "error": str(exc)},
            config_fingerprint=effective_config_fingerprint,
            config_version=runtime_identity.config_version,
            now=current,
        )
        append_audit_event(
            "daily_runner_failed",
            {
                "output_dir": str(run_dir),
                "error": str(exc),
            },
            state_dir,
            status="failed",
            now=current,
        )

    try:
        _finalize_result(
            result,
            run_dir=run_dir,
            output_root=output_root,
            env_path=env_path,
            state_dir=state_dir,
            db_path=db_path,
            now=current,
        )
    finally:
        lease.release()
    return result


def _next_step_for_mode(mode: str, dry_run: dict[str, Any], schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    files = dry_run.get("files", {})
    review_path = files.get("review_json", "")
    manual_confirmation = files.get("manual_confirmation", "")

    if dry_run.get("already_published_today"):
        return {
            "next_action": "blocked",
            "reason": "A publish lock already exists for today.",
        }

    if mode == "dry_run":
        return {
            "next_action": "none",
            "reason": "dry_run mode generated local outputs only.",
        }

    if mode == "draft_only":
        return {
            "next_action": "create_draft",
            "reason": "draft_only mode should create a WeChat draft after reviewing the package.",
            "required_inputs": {
                "review": review_path,
            },
        }

    if mode == "manual_confirm":
        return {
            "next_action": "await_manual_confirmation",
            "reason": "manual_confirm mode requires an approved confirmation file before publish.",
            "required_inputs": {
                "review": review_path,
                "manual_confirmation": manual_confirmation,
            },
        }

    if mode == "auto_publish_low_risk":
        source_gate = _source_publish_gate(dry_run)
        if not source_gate["ok"]:
            return {
                "next_action": "blocked",
                "reason": source_gate["reason"],
                "source_gate": source_gate,
            }
        image_gate = _image_publish_gate(schedule or {}, dry_run)
        if not image_gate["ok"]:
            return {
                "next_action": "blocked",
                "reason": image_gate["reason"],
                "image_gate": image_gate,
            }
        if dry_run.get("publish_action") == "publish" and dry_run.get("publish_allowed"):
            return {
                "next_action": "ready_to_publish",
                "reason": "Low-risk package passed policy gates; use publish-from-draft after draft media_id is available.",
                "required_inputs": {
                    "review": review_path,
                    "draft_media_id": "",
                },
            }
        return {
            "next_action": "blocked",
            "reason": "auto_publish_low_risk mode is enabled, but policy did not allow publish.",
            "publish_action": dry_run.get("publish_action"),
        }

    return {
        "next_action": "blocked",
        "reason": f"Unsupported publishing mode: {mode}",
    }


def _source_publish_gate(dry_run: dict[str, Any]) -> dict[str, Any]:
    statuses: dict[str, str] = {}
    for key in ("news_fetch", "manufacturing_news_fetch"):
        value = dry_run.get(key, {})
        if not isinstance(value, dict):
            continue
        status = str(value.get("status", "")).strip()
        if status:
            statuses[key] = status
        if status and status not in {"ok", "loaded", "supplemented"}:
            return {
                "ok": False,
                "status": status,
                "source": key,
                "reason": f"Automatic publish blocked because news source {key} status is {status}.",
            }
    if statuses:
        return {"ok": True, "statuses": statuses}
    return {
        "ok": True,
    }


def _image_publish_gate(schedule: dict[str, Any], dry_run: dict[str, Any]) -> dict[str, Any]:
    publishing = schedule.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}
    allow_fallback = bool(publishing.get("allow_image_fallback_for_publish", True))
    fallbacks = _image_fallbacks(dry_run.get("image_generation", {}))
    if allow_fallback or not fallbacks:
        return {"ok": True, "allow_image_fallback_for_publish": allow_fallback, "fallbacks": fallbacks}
    return {
        "ok": False,
        "allow_image_fallback_for_publish": allow_fallback,
        "fallbacks": fallbacks,
        "reason": "Automatic publish blocked because image generation used local fallback.",
    }


def _image_fallbacks(value: Any, *, path: str = "") -> list[dict[str, str]]:
    fallbacks: list[dict[str, str]] = []
    if isinstance(value, dict):
        provider = str(value.get("provider", "")).strip()
        if provider == "local_fallback":
            fallbacks.append(
                {
                    "path": path,
                    "status": str(value.get("status", "")),
                    "reason": str(value.get("reason", "")),
                }
            )
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            fallbacks.extend(_image_fallbacks(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            fallbacks.extend(_image_fallbacks(item, path=f"{path}[{index}]"))
    return fallbacks


def _resumable_content_package(
    checkpoint: dict[str, Any],
    *,
    mode: str,
    expected_config_fingerprint: str,
    expected_config_version: str,
) -> dict[str, Any]:
    stage = checkpoint_stage(checkpoint, "content_package")
    if stage.get("status") != "completed":
        return {}
    if (
        mode == "auto_publish_low_risk"
        and not bool(stage.get("publish_allowed", False))
        and bool(stage.get("retry_eligible", True))
    ):
        return {}
    result_path = Path(str(stage.get("dry_run_result", "")))
    if not result_path.exists():
        return {}
    try:
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(result, dict):
        return {}
    files = result.get("files", {}) if isinstance(result.get("files", {}), dict) else {}
    package_path = str(files.get("content_package_json", "")).strip()
    manifest_path = str(files.get("content_package_manifest", "")).strip()
    if not package_path or not manifest_path:
        return {}
    frozen = verify_frozen_package(
        package_path,
        manifest_path=manifest_path,
        expected_config_fingerprint=expected_config_fingerprint,
        expected_config_version=expected_config_version,
    )
    if not frozen["ok"]:
        return {}
    result["resume_verification"] = frozen
    return result


def _content_package_retry_eligible(dry_run: dict[str, Any]) -> bool:
    if bool(dry_run.get("publish_allowed", False)):
        return False
    healthy_source_statuses = {"ok", "loaded", "supplemented"}
    for field in ("news_fetch", "manufacturing_news_fetch"):
        value = dry_run.get(field, {})
        if not isinstance(value, dict):
            continue
        status = str(value.get("status", "")).strip()
        if status and status not in healthy_source_statuses:
            return True
    transient_markers = (
        "text_model_error",
        "unavailable",
        "circuit_open",
        "timeout",
    )
    generation = dry_run.get("text_model_generation", {})
    return any(
        marker in value.lower()
        for value in _nested_string_values(generation)
        for marker in transient_markers
    )


def _nested_string_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _nested_string_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _nested_string_values(child)]
    return [value] if isinstance(value, str) else []


def _archive_prepare_attempt(
    *,
    run_dir: Path,
    package_dir: Path,
    attempt_number: int,
    dry_run: dict[str, Any],
    now: datetime,
) -> str:
    archive_dir = run_dir / "prepare-attempts" / f"attempt-{max(1, attempt_number):02d}"
    evidence_files = (
        "dry-run-result.json",
        "content-package.json",
        "schedule-plan.json",
        "review.json",
        "publish-decision.json",
        "primary/article.json",
        "primary/review.json",
        "primary/article.html",
        "secondary/article.json",
        "secondary/review.json",
        "secondary/article.html",
    )
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        copied = []
        for relative in evidence_files:
            source = package_dir / relative
            if not source.is_file():
                continue
            destination = archive_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative)
        model_generation = dry_run.get("text_model_generation", {})
        calls = model_generation.get("calls", []) if isinstance(model_generation, dict) else []
        summary = {
            "schema_version": "prepare_attempt_evidence.v1",
            "created_at": now.isoformat(),
            "attempt": max(1, attempt_number),
            "status": str(dry_run.get("status", "")),
            "risk_level": str(dry_run.get("risk_level", "")),
            "publish_action": str(dry_run.get("publish_action", "")),
            "publish_allowed": bool(dry_run.get("publish_allowed", False)),
            "retry_eligible": _content_package_retry_eligible(dry_run),
            "title": str(dry_run.get("title", "")),
            "secondary_title": str(dry_run.get("secondary_title", "")),
            "model_call_count": len(calls) if isinstance(calls, list) else 0,
            "files": copied,
        }
        (archive_dir / "attempt-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(archive_dir)
    except OSError:
        return ""


def _record_checkpoint_safely(
    run_dir: Path,
    *,
    stage: str,
    data: dict[str, Any],
    config_fingerprint: str,
    config_version: str,
    now: datetime,
) -> dict[str, Any]:
    try:
        return record_run_stage(
            run_dir,
            stage=stage,
            data=data,
            config_fingerprint=config_fingerprint,
            config_version=config_version,
            now=now,
        )
    except Exception:
        return {}


def _publish_result_from_checkpoint(stage: dict[str, Any], *, run_dir: Path) -> dict[str, Any]:
    published = bool(stage.get("published", False) or stage.get("status") == "published")
    status = "ok" if published else str(stage.get("status", "submitted"))
    return {
        "status": status,
        "output_dir": str(stage.get("output_dir", run_dir / "wechat-publish")),
        "publish_channel": str(stage.get("publish_channel", "")),
        "submitted": {
            "publish_id": str(stage.get("publish_id", "")),
            "msg_id": str(stage.get("msg_id", "")),
            "msg_data_id": str(stage.get("msg_data_id", "")),
        },
        "published": published,
        "published_articles": stage.get("published_articles", []),
    }


def _positive_number(value: Any, *, default: float, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    minimum = 0.0 if allow_zero else 1.0
    return max(minimum, parsed)


def _bounded_positive_int(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed))


def _prepare_budget_limits(daily_run: dict[str, Any]) -> dict[str, int]:
    return {
        "max_prepare_attempts_per_day": _bounded_positive_int(
            daily_run.get("max_prepare_attempts_per_day", 3),
            default=3,
            maximum=3,
        ),
        "max_text_model_calls_per_attempt": _bounded_positive_int(
            daily_run.get("max_text_model_calls_per_attempt", 28),
            default=28,
            maximum=30,
        ),
        "max_text_model_calls_per_day": _bounded_positive_int(
            daily_run.get("max_text_model_calls_per_day", 36),
            default=36,
            maximum=50,
        ),
    }


def _accounted_prepare_usage(stage: dict[str, Any]) -> tuple[int, int]:
    try:
        attempts = max(0, int(stage.get("attempts_started", 0) or 0))
    except (TypeError, ValueError):
        attempts = 0
    try:
        provider_calls = max(0, int(stage.get("provider_calls_cumulative", 0) or 0))
    except (TypeError, ValueError):
        provider_calls = 0
    if str(stage.get("status", "")) == "running":
        try:
            provider_calls += max(0, int(stage.get("active_call_reservation", 0) or 0))
        except (TypeError, ValueError):
            pass
    return attempts, provider_calls


def _start_prepare_attempt(stage: dict[str, Any], daily_run: dict[str, Any]) -> dict[str, Any]:
    limits = _prepare_budget_limits(daily_run)
    attempts, provider_calls = _accounted_prepare_usage(stage)
    if attempts >= limits["max_prepare_attempts_per_day"]:
        return {
            **limits,
            "allowed": False,
            "next_action": "prepare_attempt_limit_reached",
            "reason": "Prepare was stopped after three attempts today; no more provider calls will be made.",
            "attempts_started": attempts,
            "provider_calls_cumulative": provider_calls,
            "provider_call_limit": 0,
        }
    remaining_daily_calls = limits["max_text_model_calls_per_day"] - provider_calls
    if remaining_daily_calls <= 0:
        return {
            **limits,
            "allowed": False,
            "next_action": "prepare_call_budget_exhausted",
            "reason": "Prepare was stopped because today's paid text-model call budget is exhausted.",
            "attempts_started": attempts,
            "provider_calls_cumulative": provider_calls,
            "provider_call_limit": 0,
        }
    return {
        **limits,
        "allowed": True,
        "next_action": "prepare_allowed",
        "reason": "",
        "attempts_started": attempts + 1,
        "provider_calls_cumulative": provider_calls,
        "provider_call_limit": min(
            limits["max_text_model_calls_per_attempt"],
            remaining_daily_calls,
        ),
    }


def _prepare_budget_public_state(stage: dict[str, Any], daily_run: dict[str, Any]) -> dict[str, Any]:
    limits = _prepare_budget_limits(daily_run)
    attempts, provider_calls = _accounted_prepare_usage(stage)
    return {
        **limits,
        "status": str(stage.get("status", "not_started")),
        "attempts_started": attempts,
        "provider_calls_cumulative": provider_calls,
        "attempts_remaining": max(0, limits["max_prepare_attempts_per_day"] - attempts),
        "provider_calls_remaining_today": max(0, limits["max_text_model_calls_per_day"] - provider_calls),
    }


def _record_recent_angles(
    result: dict[str, Any],
    *,
    state_dir: str | Path,
    now: datetime,
) -> None:
    stages = result.get("stages", [])
    if not isinstance(stages, list):
        return
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("name") != "content_package":
            continue
        record_angles(
            state_dir,
            [
                {
                    "role": "primary",
                    "title": stage.get("title", ""),
                    "topic": "昨天 AI 界发生了什么",
                    "source": stage.get("output_dir", ""),
                },
                {
                    "role": "secondary",
                    "title": stage.get("secondary_title", ""),
                    "topic": stage.get("secondary_title", ""),
                    "source": stage.get("output_dir", ""),
                },
            ],
            now=now,
        )
        return


def _finalize_result(
    result: dict[str, Any],
    *,
    run_dir: Path,
    output_root: str | Path,
    env_path: str | Path,
    state_dir: str | Path,
    db_path: str | Path,
    now: datetime,
) -> None:
    apply_business_outcome(result)
    if result.get("business_outcome") == "published":
        _record_recent_angles(result, state_dir=state_dir, now=now)
        result["recent_angles_recorded"] = True
    _write_daily_result(run_dir, result)
    try:
        archive = write_run_archive(result, run_dir=run_dir, output_root=output_root, now=now)
        result["archive"] = archive
        _write_daily_result(run_dir, result)
    except Exception as exc:
        result["archive_error"] = str(exc)
        append_audit_event(
            "daily_runner_archive_failed",
            {"output_dir": str(run_dir), "error": str(exc)},
            state_dir,
            status="failed",
            now=now,
        )
        _write_daily_result(run_dir, result)

    try:
        run_id = record_daily_run(result, db_path)
        result["db_run_id"] = run_id
        _write_daily_result(run_dir, result)
    except Exception as exc:
        result["db_error"] = str(exc)
        append_audit_event(
            "daily_runner_db_record_failed",
            {
                "output_dir": str(run_dir),
                "error": str(exc),
            },
            state_dir,
            status="failed",
            now=now,
        )
        _write_daily_result(run_dir, result)

    try:
        result["notification"] = notify_daily_run(result, env_path=env_path, state_dir=state_dir)
    except Exception as exc:
        result["notification"] = {"status": "failed", "error": str(exc)}
        append_audit_event(
            "daily_runner_notification_failed",
            {
                "output_dir": str(run_dir),
                "error": str(exc),
            },
            state_dir,
            status="failed",
            now=now,
        )
    _write_daily_result(run_dir, result)


def _write_daily_result(run_dir: Path, result: dict[str, Any]) -> None:
    (run_dir / "daily-run-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
