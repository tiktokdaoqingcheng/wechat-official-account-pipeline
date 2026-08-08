from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from src.config_loader import load_layered_yaml, load_yaml
from src.pipeline.content_package import (
    aggregate_package_review,
    build_content_package,
    build_daily_article_seeds,
    validate_content_package,
)
from src.pipeline.news_seed import NewsFetchCache, generate_news_seed
from src.pipeline.package_manifest import freeze_content_package
from src.pipeline.angle_memory import memory_path
from src.pipeline.topic_engine import select_topic_seed
from src.review.publish_guard import already_published_today, create_manual_confirmation_request
from src.review.publish_policy import decide_publish_action
from src.scheduler.schedule_plan import build_schedule_plan, validate_schedule_plan
from src.runtime_guard import config_fingerprint
from src.runtime_budget import ExecutionBudget


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local dry-run article package.")
    parser.add_argument("--seed", default="")
    parser.add_argument("--topics", default="config/topics.yaml")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--safety", default="config/safety-rules.yaml")
    parser.add_argument("--news-sources", default="config/news-sources.yaml")
    parser.add_argument("--news-seed", default="")
    parser.add_argument("--manufacturing-news-sources", default="config/manufacturing-news-sources.yaml")
    parser.add_argument("--manufacturing-news-seed", default="")
    parser.add_argument("--fetch-news", action="store_true")
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--generate-images", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone(timedelta(hours=8), name="Asia/Shanghai"))
    result = generate_dry_run_package(
        seed_path=args.seed,
        topics_path=args.topics,
        schedule_path=args.schedule,
        safety_path=args.safety,
        news_sources_path=args.news_sources,
        news_seed_path=args.news_seed,
        manufacturing_news_sources_path=args.manufacturing_news_sources,
        manufacturing_news_seed_path=args.manufacturing_news_seed,
        fetch_news=args.fetch_news,
        state_dir=args.state_dir,
        env_path=args.env,
        generate_images=args.generate_images,
        output_dir=args.output_dir,
        now=now,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status: {result['status']}")
        print(f"Title: {result['title']}")
        print(f"Publish action: {result['publish_action']}")
        print(f"Output: {result['output_dir']}")
    return 0


def generate_dry_run_package(
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
    env_path: str | Path = ".env",
    generate_images: bool = False,
    output_dir: str | Path | None = None,
    execution_budget: ExecutionBudget | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone(timedelta(hours=8), name="Asia/Shanghai"))
    if seed_path:
        seed = json.loads(Path(seed_path).read_text(encoding="utf-8-sig"))
        seed_source = str(seed_path)
    else:
        seed = select_topic_seed(topics_path, now=current, memory_path=memory_path(state_dir))
        seed_source = str(topics_path)
    schedule = load_layered_yaml(schedule_path)
    output_path = Path(output_dir or f"outputs/{current:%Y-%m-%d}-dry-run")
    output_path.mkdir(parents=True, exist_ok=True)

    safety = load_yaml(safety_path)
    published_today = already_published_today(state_dir, now=current)
    news_fetch_cache = NewsFetchCache() if fetch_news else None
    primary_seed, primary_seed_source, news_fetch_result = _primary_news_seed(
        news_sources_path=news_sources_path,
        news_seed_path=news_seed_path,
        fetch_news=fetch_news,
        fetch_cache=news_fetch_cache,
        now=current,
    )
    secondary_seed, secondary_seed_source, manufacturing_news_fetch_result = _manufacturing_news_seed(
        news_sources_path=manufacturing_news_sources_path,
        news_seed_path=manufacturing_news_seed_path,
        fetch_news=fetch_news,
        fetch_cache=news_fetch_cache,
        now=current,
    )
    article_seeds = build_daily_article_seeds(
        schedule_config=schedule,
        selected_seed=seed,
        seed_source=seed_source,
        primary_seed=primary_seed,
        primary_seed_source=primary_seed_source,
        secondary_seed=secondary_seed,
        secondary_seed_source=secondary_seed_source,
        now=current,
    )
    manufacturing_news_fetch_result = _effective_manufacturing_news_fetch_result(
        manufacturing_news_fetch_result,
        article_seeds.get("secondary", {}),
    )
    draft_decision = decide_publish_action(
        schedule,
        {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
        already_published_today=published_today,
    )
    package_bundle = build_content_package(
        schedule_config=schedule,
        article_seeds=article_seeds,
        safety_config=safety,
        publish_decision=draft_decision,
        output_dir=output_path,
        seed_source=seed_source,
        already_published_today=published_today,
        env_path=env_path,
        generate_images=generate_images,
        execution_budget=execution_budget,
        now=current,
    )
    package_review = aggregate_package_review(package_bundle["reviews"])
    decision = decide_publish_action(schedule, package_review, already_published_today=published_today)
    package_bundle["package"]["publish_decision"] = asdict(decision)
    Path(package_bundle["files"]["content_package_json"]).write_text(
        json.dumps(package_bundle["package"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    package_review = aggregate_package_review(package_bundle["reviews"])
    package = package_bundle["package"]
    package_errors = validate_content_package(package)
    if package_errors:
        raise ValueError("Invalid content package: " + "; ".join(package_errors))
    article = package_bundle["articles"]["primary"]
    review = package_bundle["reviews"]["primary"]
    secondary_article = package_bundle["articles"]["secondary"]
    secondary_review = package_bundle["reviews"]["secondary"]
    role_files = package_bundle["files"]

    files = {
        "article_json": output_path / "article.json",
        "article_md": output_path / "article.md",
        "article_html": output_path / "article.html",
        "review_json": output_path / "review.json",
        "publish_decision_json": output_path / "publish-decision.json",
        "schedule_plan_json": output_path / "schedule-plan.json",
        "content_package_json": output_path / "content-package.json",
        "cover": output_path / "cover.png",
        "primary_article_json": role_files["primary"]["article_json"],
        "primary_article_md": role_files["primary"]["article_md"],
        "primary_article_html": role_files["primary"]["article_html"],
        "primary_review_json": role_files["primary"]["review_json"],
        "primary_cover": role_files["primary"]["cover"],
        "secondary_article_json": role_files["secondary"]["article_json"],
        "secondary_article_md": role_files["secondary"]["article_md"],
        "secondary_article_html": role_files["secondary"]["article_html"],
        "secondary_review_json": role_files["secondary"]["review_json"],
        "secondary_cover": role_files["secondary"]["cover"],
    }

    if decision.action == "await_manual_confirm":
        files["manual_confirmation"] = create_manual_confirmation_request(
            output_path,
            action="publish",
            title=f"{article['title']} / {secondary_article['title']}",
            decision=asdict(decision),
            review_summary={
                "risk_level": package["package_risk_level"],
                "primary": {
                    "risk_level": review["risk_level"],
                    "flags": review["flags"],
                    "sensitive_hits": review["sensitive_hits"],
                },
                "secondary": {
                    "risk_level": secondary_review["risk_level"],
                    "flags": secondary_review["flags"],
                    "sensitive_hits": secondary_review["sensitive_hits"],
                },
            },
            now=current,
        )

    _copy_text(Path(role_files["primary"]["article_json"]), files["article_json"])
    _copy_text(Path(role_files["primary"]["article_md"]), files["article_md"])
    _copy_text(Path(role_files["primary"]["article_html"]), files["article_html"])
    _copy_text(Path(role_files["primary"]["review_json"]), files["review_json"])
    _copy_binary(Path(role_files["primary"]["cover"]), files["cover"])
    files["publish_decision_json"].write_text(json.dumps(asdict(decision), ensure_ascii=False, indent=2), encoding="utf-8")
    schedule_plan = build_schedule_plan(
        schedule,
        article=article,
        review=review,
        publish_decision=asdict(decision),
        files=files,
        seed_source=seed_source,
        already_published_today=published_today,
        now=current,
    )
    schedule_errors = validate_schedule_plan(schedule_plan)
    if schedule_errors:
        raise ValueError("Invalid schedule plan: " + "; ".join(schedule_errors))
    files["schedule_plan_json"].write_text(
        json.dumps(schedule_plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    runtime = schedule.get("runtime", {}) if isinstance(schedule.get("runtime", {}), dict) else {}
    manifest = freeze_content_package(
        files["content_package_json"],
        config_fingerprint=config_fingerprint(schedule),
        config_version=str(runtime.get("config_version", "")),
        now=current,
    )
    files["content_package_manifest"] = Path(manifest["path"])

    result = {
        "status": "ok",
        "published": False,
        "created_at": current.isoformat(),
        "seed_source": seed_source,
        "primary_seed_source": primary_seed_source,
        "news_fetch": news_fetch_result,
        "secondary_seed_source": secondary_seed_source,
        "manufacturing_news_fetch": manufacturing_news_fetch_result,
        "title": article["title"],
        "secondary_title": secondary_article["title"],
        "article_count": package["article_count"],
        "content_package_schema_version": package["schema_version"],
        "content_package": {
            "primary_article": package["primary_article"],
            "secondary_article": package["secondary_article"],
            "package_risk_level": package["package_risk_level"],
            "wechat_ready": package["wechat_ready"],
        },
        "image_generation": package.get("image_generation", {}),
        "text_model_generation": package.get("text_model_generation", {}),
        "word_count_estimate": article["word_count_estimate"],
        "risk_level": package["package_risk_level"],
        "already_published_today": published_today,
        "publish_action": decision.action,
        "publish_allowed": decision.allowed,
        "output_dir": str(output_path),
        "files": {name: str(path) for name, path in files.items()},
        "artifact_manifest": manifest,
    }
    (output_path / "dry-run-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _primary_news_seed(
    *,
    news_sources_path: str | Path,
    news_seed_path: str | Path,
    fetch_news: bool,
    fetch_cache: NewsFetchCache | None,
    now: datetime,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    if news_seed_path:
        path = Path(news_seed_path)
        seed = json.loads(path.read_text(encoding="utf-8-sig"))
        return seed, str(path), {"status": "loaded", "output_path": str(path)}

    if not fetch_news:
        return None, "", {"status": "disabled"}

    result = generate_news_seed(
        config_path=news_sources_path,
        output_dir="data/news-seeds",
        fetch_cache=fetch_cache,
        now=now,
    )
    seed = result.get("seed") if isinstance(result.get("seed"), dict) else None
    return seed, str(result.get("output_path", "")), {
        "status": result.get("status", "unknown"),
        "target_date": result.get("target_date", ""),
        "item_count": result.get("item_count", 0),
        "output_path": result.get("output_path", ""),
        "source_quality": seed.get("source_quality", {}) if isinstance(seed, dict) else {},
        "diagnostics": result.get("diagnostics", []),
    }


def _manufacturing_news_seed(
    *,
    news_sources_path: str | Path,
    news_seed_path: str | Path,
    fetch_news: bool,
    fetch_cache: NewsFetchCache | None,
    now: datetime,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    if news_seed_path:
        path = Path(news_seed_path)
        seed = json.loads(path.read_text(encoding="utf-8-sig"))
        return seed, str(path), {"status": "loaded", "output_path": str(path)}

    if not fetch_news:
        return None, "", {"status": "disabled"}

    target = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    try:
        config = load_yaml(news_sources_path)
        if str(config.get("target_window", "previous_day")) != "today":
            target = (now.astimezone(timezone(timedelta(hours=8))) - timedelta(days=1)).date().isoformat()
    except Exception:
        target = (now.astimezone(timezone(timedelta(hours=8))) - timedelta(days=1)).date().isoformat()
    output_path = Path("data/news-seeds") / f"{target}-manufacturing-news.json"
    result = generate_news_seed(
        config_path=news_sources_path,
        output_dir="data/news-seeds",
        output_path=output_path,
        fetch_cache=fetch_cache,
        now=now,
    )
    seed = result.get("seed") if isinstance(result.get("seed"), dict) else None
    return seed, str(result.get("output_path", "")), {
        "status": result.get("status", "unknown"),
        "target_date": result.get("target_date", ""),
        "item_count": result.get("item_count", 0),
        "output_path": result.get("output_path", ""),
        "source_quality": seed.get("source_quality", {}) if isinstance(seed, dict) else {},
        "diagnostics": result.get("diagnostics", []),
    }


def _effective_manufacturing_news_fetch_result(fetch_result: dict[str, Any], secondary_seed: dict[str, Any]) -> dict[str, Any]:
    result = dict(fetch_result)
    status = str(secondary_seed.get("news_status", "")).strip()
    if status == "supplemented":
        result["status"] = "supplemented"
        result["item_count"] = len(secondary_seed.get("news_items", [])) if isinstance(secondary_seed.get("news_items", []), list) else result.get("item_count", 0)
        result["supplemental_seed_source"] = str(secondary_seed.get("supplemental_seed_source", "")).strip()
        result["reason"] = "Independent manufacturing sources were supplemented with same-day manufacturing items from the primary news seed."
    return result


def _copy_text(source: Path, destination: Path) -> None:
    destination.write_text(source.read_text(encoding="utf-8-sig"), encoding="utf-8")


def _copy_binary(source: Path, destination: Path) -> None:
    destination.write_bytes(source.read_bytes())

if __name__ == "__main__":
    raise SystemExit(main())
