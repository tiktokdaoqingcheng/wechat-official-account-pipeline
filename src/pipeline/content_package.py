from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from src.integrations.openai_image_client import OpenAIImageClient, OpenAIImageError, load_openai_image_config
from src.pipeline.briefing_cover import write_weekly_briefing_cover
from src.pipeline.copy_quality import (
    apply_copy_quality_to_review,
    evaluate_package_copy_quality,
    final_review_issue_blocks_publish,
    sanitize_article_copy,
)
from src.pipeline.fact_cards import attach_fact_cards
from src.pipeline.content_engine import (
    build_article,
    news_title_element,
    render_markdown,
    render_wechat_html,
    validate_article,
)
from src.pipeline.news_seed import _same_news_event, blocked_by_wechat_platform_risk
from src.pipeline.test_assets import write_article_illustration_png, write_test_cover_png
from src.pipeline.text_model_enhancer import (
    enhance_article_with_text_models,
    generate_final_article_title,
    prepare_text_model_context,
    review_final_article_deterministically,
    review_final_article_with_text_model,
    text_model_package_status,
)
from src.review.publish_policy import PublishDecision
from src.review.review_engine import review_article, validate_review
from src.runtime_budget import ExecutionBudget


CONTENT_PACKAGE_SCHEMA_VERSION = "content_package.v1"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_ARTICLE_ROLES = ("primary", "secondary")
MAX_INLINE_IMAGES_PER_ARTICLE = 2
SOURCE_IMAGE_TIMEOUT_SECONDS = 10
SOURCE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
SOURCE_IMAGE_USER_AGENT = "wechat-official-account-automation/0.1"


def build_daily_article_seeds(
    *,
    schedule_config: dict[str, Any],
    selected_seed: dict[str, Any],
    seed_source: str,
    primary_seed: dict[str, Any] | None = None,
    primary_seed_source: str = "",
    secondary_seed: dict[str, Any] | None = None,
    secondary_seed_source: str = "",
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    current = now or datetime.now(SHANGHAI_TZ)
    package_config = schedule_config.get("daily_content_package", {})
    if not isinstance(package_config, dict):
        package_config = {}

    roles = _article_roles(schedule_config)
    primary_config = _article_config(package_config, "primary")
    secondary_config = _article_config(package_config, "secondary")

    seeds = {
        "primary": _primary_seed(primary_config, current=current, news_seed=primary_seed, seed_source=primary_seed_source),
    }
    if "secondary" in roles:
        seeds["secondary"] = _secondary_seed(
            secondary_config,
            selected_seed=selected_seed,
            seed_source=seed_source,
            primary_seed=primary_seed,
            primary_seed_source=primary_seed_source,
            secondary_seed=secondary_seed,
            secondary_seed_source=secondary_seed_source,
        )
    return seeds


def build_content_package(
    *,
    schedule_config: dict[str, Any],
    article_seeds: dict[str, dict[str, Any]],
    safety_config: dict[str, Any],
    publish_decision: PublishDecision,
    output_dir: str | Path,
    seed_source: str,
    already_published_today: bool,
    env_path: str | Path = ".env",
    generate_images: bool = False,
    execution_budget: ExecutionBudget | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    output_path = Path(output_dir)
    articles: dict[str, dict[str, Any]] = {}
    reviews: dict[str, dict[str, Any]] = {}
    files: dict[str, Any] = {}
    role_dirs: dict[str, Path] = {}
    image_client, image_provider_status = _openai_image_client(env_path, enabled=generate_images)
    text_model_context = prepare_text_model_context(env_path, execution_budget=execution_budget)
    roles = _article_roles(schedule_config)
    final_review_config = (
        schedule_config.get("final_review", {})
        if isinstance(schedule_config.get("final_review", {}), dict)
        else {}
    )
    minimum_items_by_role: dict[str, int] = {}
    target_items_by_role: dict[str, int] = {}
    replenishment_runs_by_role: dict[str, list[dict[str, Any]]] = {}

    # Complete the first writing pass for every article before spending calls on
    # review. This prevents the primary article from exhausting the shared model
    # budget before the secondary article has usable copy.
    for role in roles:
        role_dir = output_path / role
        role_dir.mkdir(parents=True, exist_ok=True)
        configured_minimum = _minimum_recovery_items(final_review_config, role)
        minimum_items = configured_minimum
        target_items = max(_target_news_item_count(article_seeds[role]), minimum_items)
        replenishment_runs: list[dict[str, Any]] = []
        article = attach_fact_cards(build_article(article_seeds[role]))
        article = enhance_article_with_text_models(article, text_model_context)
        article = _prepare_article_for_render(article, role=role)
        article, replenishment = _replenish_article_from_reserve(
            article,
            role=role,
            target_news_items=minimum_items,
            text_model_context=text_model_context,
            execution_budget=execution_budget,
        )
        replenishment_runs.append(replenishment)
        if replenishment["changed"]:
            article = _prepare_article_for_render(article, role=role)

        articles[role] = article
        role_dirs[role] = role_dir
        minimum_items_by_role[role] = minimum_items
        target_items_by_role[role] = target_items
        replenishment_runs_by_role[role] = replenishment_runs

    for role in roles:
        article = articles[role]
        minimum_items = minimum_items_by_role[role]
        target_items = target_items_by_role[role]
        replenishment_runs = replenishment_runs_by_role[role]
        article = review_final_article_with_text_model(
            article,
            text_model_context,
            visible_html=render_wechat_html(article),
            mode=str(final_review_config.get("mode", "shadow")),
        )
        if bool(final_review_config.get("auto_repair", False)):
            article, recovery, cycle_replenishments = _run_final_review_recovery_cycles(
                article,
                role=role,
                min_news_items=minimum_items,
                target_news_items=minimum_items,
                final_review_config=final_review_config,
                text_model_context=text_model_context,
                execution_budget=execution_budget,
            )
            replenishment_runs.extend(cycle_replenishments)
            article["final_review_recovery"] = recovery
        article = _finalize_article_title(article, text_model_context=text_model_context)
        article = _strip_replenishment_metadata(article)
        article["news_replenishment"] = {
            "status": "applied" if any(run.get("changed") for run in replenishment_runs) else "not_needed",
            "target_news_items": target_items,
            "minimum_news_items": minimum_items,
            "final_news_items": _article_news_count(article),
            "below_minimum": _article_news_count(article) < minimum_items,
            "runs": replenishment_runs,
        }
        article_errors = validate_article(article)
        if article_errors:
            raise ValueError(f"Invalid {role} article: " + "; ".join(article_errors))

        review = review_article(article, safety_config)
        review_errors = validate_review(review)
        if review_errors:
            raise ValueError(f"Invalid {role} review: " + "; ".join(review_errors))

        articles[role] = article
        reviews[role] = review

    copy_quality = evaluate_package_copy_quality(articles, text_model_context)
    for role in roles:
        role_quality = copy_quality.get("articles", {}).get(role, {})
        if isinstance(role_quality, dict):
            reviews[role] = apply_copy_quality_to_review(reviews[role], role_quality)

    image_content_ready = bool(
        copy_quality.get("ok", False)
        and all(
            str(reviews[role].get("risk_level", "")) == "low"
            and bool(reviews[role].get("wechat_ready", False))
            for role in roles
        )
    )
    image_runtime_ready = execution_budget is None or execution_budget.can_start_optional(90)
    paid_image_generation_allowed = image_content_ready and image_runtime_ready
    effective_image_client = image_client if paid_image_generation_allowed else None
    effective_image_provider_status = image_provider_status
    if generate_images and image_client is not None and not paid_image_generation_allowed:
        skip_reason = (
            "Paid image generation was skipped because the text package was not publish-ready."
            if not image_content_ready
            else "Paid image generation was skipped because the remaining runtime budget is too low."
        )
        effective_image_provider_status = {
            "status": (
                "skipped_content_quality_gate"
                if not image_content_ready
                else "skipped_runtime_budget"
            ),
            "provider": "local_fallback",
            "reason": skip_reason,
            "configured_provider": image_provider_status.get("provider", "openai_image_api"),
            "model": image_provider_status.get("model", ""),
            "api_base": image_provider_status.get("api_base", ""),
        }

    for role in roles:
        article = articles[role]
        role_dir = role_dirs[role]
        cover_path, illustration_path, inline_image_paths, image_generation = _write_article_images(
            article,
            role=role,
            role_dir=role_dir,
            image_client=effective_image_client,
            provider_status=effective_image_provider_status,
            now=current,
        )
        article["image_generation"] = image_generation
        article_errors = validate_article(article)
        if article_errors:
            raise ValueError(f"Invalid {role} article after image preparation: " + "; ".join(article_errors))
        role_files = {
            "article_json": role_dir / "article.json",
            "article_md": role_dir / "article.md",
            "article_html": role_dir / "article.html",
            "review_json": role_dir / "review.json",
            "cover": cover_path,
            "illustration": illustration_path,
            "inline_images": inline_image_paths,
        }
        role_files["article_json"].write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
        role_files["article_md"].write_text(render_markdown(article), encoding="utf-8")
        role_files["article_html"].write_text(render_wechat_html(article), encoding="utf-8")
        role_files["review_json"].write_text(
            json.dumps(reviews[role], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        files[role] = _role_files_for_package(role_files)

    package_risk = _aggregate_risk([reviews[role]["risk_level"] for role in roles])
    package = {
        "schema_version": CONTENT_PACKAGE_SCHEMA_VERSION,
        "created_at": current.isoformat(),
        "article_count": len(roles),
        "article_roles": list(roles),
        "primary_article": _article_summary(articles["primary"], reviews["primary"]),
        "secondary_article": _article_summary(articles["secondary"], reviews["secondary"])
        if "secondary" in roles
        else {},
        "package_risk_level": package_risk,
        "wechat_ready": all(bool(reviews[role]["wechat_ready"]) for role in roles),
        "already_published_today": already_published_today,
        "publish_decision": asdict(publish_decision),
        "seed_source": seed_source,
        "image_generation": {
            "enabled": generate_images,
            "content_ready": image_content_ready,
            "runtime_ready": image_runtime_ready,
            "paid_generation_allowed": paid_image_generation_allowed,
            "provider_status": effective_image_provider_status,
            **{role: articles[role].get("image_generation", {}) for role in roles},
        },
        "text_model_generation": text_model_package_status(text_model_context, articles),
        "copy_quality": copy_quality,
        "news_replenishment": {
            role: articles[role].get("news_replenishment", {})
            for role in roles
        },
        "schedule": {
            "publishing_mode": str(schedule_config.get("publishing", {}).get("mode", "dry_run"))
            if isinstance(schedule_config.get("publishing", {}), dict)
            else "dry_run",
            "article_count": int(
                schedule_config.get("daily_content_package", {}).get("article_count", len(roles))
                if isinstance(schedule_config.get("daily_content_package", {}), dict)
                else len(roles)
            ),
        },
        "files": files,
    }
    if "tertiary" in roles:
        package["tertiary_article"] = _article_summary(articles["tertiary"], reviews["tertiary"])
    package_path = output_path / "content-package.json"
    package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    files["content_package_json"] = str(package_path)
    package["files"] = files
    package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "package": package,
        "articles": articles,
        "reviews": reviews,
        "files": files,
    }


def validate_content_package(package: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if package.get("schema_version") != CONTENT_PACKAGE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {CONTENT_PACKAGE_SCHEMA_VERSION}")
    article_count = int(package.get("article_count", 0) or 0)
    roles = _package_roles(package)
    if article_count != len(roles) or article_count not in {1, 2}:
        errors.append("article_count must match 1-2 article_roles")
    for role in roles:
        field = f"{role}_article"
        value = package.get(field)
        if not isinstance(value, dict):
            errors.append(f"{field} must be an object")
            continue
        if not str(value.get("title", "")).strip():
            errors.append(f"{field}.title is required")
        if str(value.get("risk_level", "")) not in {"low", "medium", "high"}:
            errors.append(f"{field}.risk_level must be low, medium, or high")
    if str(package.get("package_risk_level", "")) not in {"low", "medium", "high"}:
        errors.append("package_risk_level must be low, medium, or high")
    if not isinstance(package.get("wechat_ready"), bool):
        errors.append("wechat_ready must be a boolean")
    files = package.get("files")
    if not isinstance(files, dict):
        errors.append("files must be an object")
    else:
        for role in roles:
            if not isinstance(files.get(role), dict):
                errors.append(f"files.{role} must be an object")
    return errors


def aggregate_package_review(reviews: dict[str, dict[str, Any]]) -> dict[str, Any]:
    roles = [role for role in MAX_ARTICLE_ROLES if isinstance(reviews.get(role), dict)]
    risk_level = _aggregate_risk([str(reviews[role].get("risk_level", "")) for role in roles])
    review_flags = [
        review.get("flags", {}) if isinstance(review.get("flags", {}), dict) else {}
        for review in reviews.values()
        if isinstance(review, dict)
    ]
    flags = {
        "fact_uncertain": any(bool(item.get("fact_uncertain")) for item in review_flags),
        "sensitive_topic": any(bool(item.get("sensitive_topic")) for item in review_flags),
        "copyright_unclear": any(bool(item.get("copyright_unclear")) for item in review_flags),
    }
    return {
        "schema_version": "package_review.v1",
        "risk_level": risk_level,
        "wechat_ready": all(bool(reviews[role].get("wechat_ready")) for role in roles),
        "flags": flags,
        "checks": [
            {
                "name": f"{role}_article",
                "ok": str(reviews[role].get("risk_level", "")) == "low" and bool(reviews[role].get("wechat_ready")),
                "severity": str(reviews[role].get("risk_level", "unknown")),
                "details": {
                    "title": reviews[role].get("title", ""),
                    "risk_level": reviews[role].get("risk_level", ""),
                },
            }
            for role in roles
        ],
        "sensitive_hits": _combined_list(*(reviews[role].get("sensitive_hits", []) for role in roles)),
        "blocked_claim_hits": _combined_list(*(reviews[role].get("blocked_claim_hits", []) for role in roles)),
    }


def _article_config(package_config: dict[str, Any], role: str) -> dict[str, Any]:
    key = f"{role}_article"
    value = package_config.get(key, {})
    return value if isinstance(value, dict) else {}


def _article_roles(schedule_config: dict[str, Any]) -> tuple[str, ...]:
    package_config = schedule_config.get("daily_content_package", {})
    if not isinstance(package_config, dict):
        package_config = {}
    try:
        count = int(package_config.get("article_count", 2) or 2)
    except (TypeError, ValueError):
        count = 2
    count = min(max(count, 1), len(MAX_ARTICLE_ROLES))
    return MAX_ARTICLE_ROLES[:count]


def _package_roles(package: dict[str, Any]) -> tuple[str, ...]:
    roles = package.get("article_roles")
    if isinstance(roles, list):
        normalized = tuple(role for role in MAX_ARTICLE_ROLES if role in {str(item) for item in roles})
        if normalized:
            return normalized
    try:
        count = int(package.get("article_count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    if count:
        return MAX_ARTICLE_ROLES[: min(max(count, 1), len(MAX_ARTICLE_ROLES))]
    return ("primary", "secondary")


def _role_label(role: str) -> str:
    if role == "primary":
        return "主文章"
    return "智能制造日报"


def _primary_seed(
    config: dict[str, Any],
    *,
    current: datetime,
    news_seed: dict[str, Any] | None,
    seed_source: str,
) -> dict[str, Any]:
    if news_seed and isinstance(news_seed.get("news_items"), list) and news_seed.get("news_items"):
        seed = dict(news_seed)
        raw_items = [item for item in news_seed.get("news_items", []) if isinstance(item, dict)]
        raw_reserve = [item for item in news_seed.get("reserve_news_items", []) if isinstance(item, dict)]
        target_count = len(raw_items)
        ranked_pool = _dedupe_news_items([*raw_items, *raw_reserve])
        removed_items = [item for item in ranked_pool if blocked_by_wechat_platform_risk(item)]
        safe_pool = [item for item in ranked_pool if not blocked_by_wechat_platform_risk(item)]
        filtered_items = safe_pool[:target_count]
        reserve_items = safe_pool[target_count:]
        seed["news_items"] = filtered_items
        seed["reserve_news_items"] = reserve_items
        seed["target_news_item_count"] = target_count
        configured_sources = seed.get("sources", []) if isinstance(seed.get("sources", []), list) else []
        seed["sources"] = _sources_for_selected_items(filtered_items, configured_sources)
        if removed_items:
            seed["wechat_platform_risk_replacements"] = [
                {
                    "title": str(item.get("title", "")).strip(),
                    "source_name": str(item.get("source_name", "")).strip(),
                    "reason": "wechat_platform_sensitive_combo",
                }
                for item in removed_items
            ]
            if not filtered_items:
                seed["news_status"] = "blocked_by_wechat_platform_risk"
                seed["sources"] = [
                    {
                        "name": "待补充：微信平台风险新闻已自动替换",
                        "url": str(seed_source or "data/news-seeds"),
                        "summary": "当天候选新闻均命中诈骗、犯罪团伙、受害者或大规模短信等微信平台高风险组合，需要补充更安全的公开新闻源。",
                    }
                ]
        seed["topic"] = str(config.get("topic") or seed.get("topic") or "昨天 AI 界发生了什么")
        seed["column"] = str(config.get("column") or seed.get("column") or "AI 昨日速览")
        seed["audience"] = str(seed.get("audience") or "对 AI 感兴趣但不熟悉技术细节的读者")
        seed["topic_reason"] = str(
            seed.get("topic_reason") or config.get("purpose") or "每日主文章固定复盘昨天 AI 行业重点变化。"
        )
        seed["seed_source"] = seed_source
        return seed

    previous_day = (current - timedelta(days=1)).date().isoformat()
    topic = str(config.get("topic") or "昨天 AI 界发生了什么")
    return {
        "topic": topic,
        "column": str(config.get("column") or "AI 昨日速览"),
        "audience": "对 AI 感兴趣但不熟悉技术细节的读者",
        "topic_reason": str(config.get("purpose") or "每日主文章固定复盘昨天 AI 行业重点变化。"),
        "risk_tags": ["requires_external_sources", "previous_day_ai_news"],
        "sources": [
            {
                "name": "待补充：昨日 AI 新闻源",
                "url": f"data/news-seeds/{previous_day}-ai-news.json",
                "summary": "主文章需要接入前一天可核验 AI 新闻源；当前为结构化占位来源。",
            }
        ],
    }


def _secondary_seed(
    config: dict[str, Any],
    *,
    selected_seed: dict[str, Any],
    seed_source: str,
    primary_seed: dict[str, Any] | None = None,
    primary_seed_source: str = "",
    secondary_seed: dict[str, Any] | None = None,
    secondary_seed_source: str = "",
) -> dict[str, Any]:
    primary_manufacturing_items = _manufacturing_items_from_primary(primary_seed)
    if isinstance(secondary_seed, dict) and isinstance(secondary_seed.get("news_items"), list) and secondary_seed.get("news_items"):
        return _smart_manufacturing_seed(
            config,
            manufacturing_seed=secondary_seed,
            seed_source=secondary_seed_source,
            supplemental_items=primary_manufacturing_items,
            supplemental_source=primary_seed_source,
        )

    if primary_manufacturing_items:
        fallback_seed = dict(primary_seed or {})
        fallback_seed["news_items"] = primary_manufacturing_items
        fallback_seed["sources"] = [_source_from_news_item(item) for item in primary_manufacturing_items]
        return _smart_manufacturing_seed(
            config,
            manufacturing_seed=fallback_seed,
            seed_source=primary_seed_source or (primary_seed.get("seed_source", "") if isinstance(primary_seed, dict) else ""),
            fallback_from_primary=True,
        )

    previous_day = str((datetime.now(SHANGHAI_TZ) - timedelta(days=1)).date().isoformat())
    return {
        "topic": "智能制造日报",
        "column": str(config.get("column") or "智能制造日报"),
        "audience": str(config.get("audience") or "关注智能制造、机器人、工业AI和产业落地的读者"),
        "digest": "每日智能制造产业资讯速递。",
        "topic_reason": str(config.get("purpose") or "第二篇固定为智能制造产业日报，当前等待独立制造业新闻源。"),
        "source_policy": "smart_manufacturing_daily",
        "risk_tags": ["requires_external_sources", "smart_manufacturing_news"],
        "target_date": previous_day,
        "news_status": "no_items",
        "sources": [
            {
                "name": "待补充：智能制造日报新闻源",
                "url": str(secondary_seed_source or seed_source or "config/manufacturing-news-sources.yaml"),
                "summary": "第二篇需要独立抓取智能制造、机器人、工业AI、低空经济和产业落地新闻；当前没有可用条目。",
            }
        ],
    }


def _smart_manufacturing_seed(
    config: dict[str, Any],
    *,
    manufacturing_seed: dict[str, Any],
    seed_source: str,
    fallback_from_primary: bool = False,
    supplemental_items: list[dict[str, Any]] | None = None,
    supplemental_source: str = "",
) -> dict[str, Any]:
    news_items = [item for item in manufacturing_seed.get("news_items", []) if isinstance(item, dict)]
    reserve_items = [item for item in manufacturing_seed.get("reserve_news_items", []) if isinstance(item, dict)]
    target_count = len(news_items)
    ranked_items = _dedupe_news_items(_rank_manufacturing_news_items([*news_items, *reserve_items]))
    removed_items = [item for item in ranked_items if blocked_by_wechat_platform_risk(item)]
    safe_ranked_items = [item for item in ranked_items if not blocked_by_wechat_platform_risk(item)]
    primary_reference_items = [item for item in (supplemental_items or []) if isinstance(item, dict)]
    safe_ranked_items = _prioritize_items_not_in_reference(safe_ranked_items, primary_reference_items)
    selected_items = safe_ranked_items[:target_count]
    reserve_items = safe_ranked_items[target_count:]
    raw_status = str(manufacturing_seed.get("news_status", "")).strip() or "ok"
    supplement = [
        item
        for item in _rank_manufacturing_news_items(supplemental_items or [])
        if not blocked_by_wechat_platform_risk(item)
    ]
    used_supplement = False
    used_supplement_for_selection = False
    if supplement:
        if _needs_manufacturing_supplement(selected_items, raw_status):
            combined = _rank_manufacturing_news_items(
                _dedupe_news_items([*selected_items, *reserve_items, *supplement])
            )
            combined = _prioritize_items_not_in_reference(combined, primary_reference_items)
            selected_items = combined[: max(target_count, min(4, len(combined)))]
            reserve_items = combined[len(selected_items) :]
            used_supplement = True
            used_supplement_for_selection = True
        else:
            selected_keys = {_news_item_identity(item) for item in selected_items}
            reserve_items = [
                item
                for item in _rank_manufacturing_news_items(
                    _dedupe_news_items([*reserve_items, *supplement])
                )
                if _news_item_identity(item) not in selected_keys
            ]
            used_supplement = bool(reserve_items)
    sources = manufacturing_seed.get("sources", [])
    if not isinstance(sources, list):
        sources = []
    sources = _sources_for_selected_items(selected_items, sources)
    topic_reason = "第二篇固定为智能制造产业日报，按公开来源整理机器人、智能工厂、低空经济、工业AI和制造业项目进展。"
    if fallback_from_primary:
        topic_reason += " 当前复用主新闻池中的制造业相关条目，后续应优先使用独立智能制造新闻源。"
    elif used_supplement_for_selection:
        topic_reason += " 当前独立智能制造新闻源命中较少，已补入同日主新闻池中的制造业相关公开新闻。"
    elif used_supplement:
        topic_reason += " 已保留同日主新闻池中的制造业相关条目作为自动补位候选。"
    news_status = raw_status
    if used_supplement and raw_status not in {"ok", "loaded"} and _source_diversity(selected_items) >= 2:
        news_status = "supplemented"
    seed = {
        "topic": "智能制造日报",
        "column": str(config.get("column") or "智能制造日报"),
        "audience": str(config.get("audience") or "关注智能制造、机器人、工业AI和产业落地的读者"),
        "digest": "每日智能制造产业资讯速递。",
        "topic_reason": topic_reason,
        "source_policy": "smart_manufacturing_daily",
        "risk_tags": ["requires_external_sources", "smart_manufacturing_news"],
        "target_date": str(manufacturing_seed.get("target_date", "")).strip(),
        "news_status": news_status,
        "news_items": selected_items,
        "reserve_news_items": reserve_items,
        "target_news_item_count": max(target_count, len(selected_items)),
        "sources": sources,
        "seed_source": seed_source,
        "supplemental_seed_source": supplemental_source if used_supplement else "",
    }
    if removed_items:
        seed["wechat_platform_risk_replacements"] = [
            {
                "title": str(item.get("title", "")).strip(),
                "source_name": str(item.get("source_name", "")).strip(),
                "reason": "wechat_platform_sensitive_combo",
            }
            for item in removed_items
        ]
        if not selected_items:
            seed["news_status"] = "blocked_by_wechat_platform_risk"
            seed["sources"] = [
                {
                    "name": "待补充：智能制造日报风险新闻已自动替换",
                    "url": str(seed_source or "config/manufacturing-news-sources.yaml"),
                    "summary": "当天智能制造候选新闻命中网络攻击、机密泄露、暗网或勒索软件等微信平台高风险组合，需要补充更安全的产业新闻源。",
                }
            ]
    return seed


def _manufacturing_items_from_primary(primary_seed: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(primary_seed, dict):
        return []
    news_items = primary_seed.get("news_items", [])
    reserve_items = primary_seed.get("reserve_news_items", [])
    pool = [
        item
        for group in (news_items, reserve_items)
        if isinstance(group, list)
        for item in group
        if isinstance(item, dict)
    ]
    return [item for item in _dedupe_news_items(pool) if _is_manufacturing_news_item(item)]


def _needs_manufacturing_supplement(items: list[dict[str, Any]], status: str) -> bool:
    if status not in {"ok", "loaded"}:
        return True
    return len(items) < 4 or _source_diversity(items) < 2


def _source_diversity(items: list[dict[str, Any]]) -> int:
    return len({str(item.get("source_name", "")).strip().lower() for item in items if str(item.get("source_name", "")).strip()})


def _dedupe_news_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    seen_topics: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("url") or item.get("title", "")).strip().lower()
        topic_keys = _duplicate_topic_keys(item)
        if not key and not topic_keys:
            continue
        if (
            (key and key in seen)
            or any(topic_key in seen_topics for topic_key in topic_keys)
            or any(_same_news_event(item, existing) for existing in deduped)
        ):
            continue
        if key:
            seen.add(key)
        seen_topics.update(topic_keys)
        deduped.append(item)
    return deduped


def _prioritize_items_not_in_reference(
    items: list[dict[str, Any]],
    reference_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not reference_items:
        return items
    reference_keys = {_news_item_identity(item) for item in reference_items}
    unique_items: list[dict[str, Any]] = []
    overlapping_items: list[dict[str, Any]] = []
    for item in items:
        identity = _news_item_identity(item)
        overlaps = identity in reference_keys or any(
            _same_news_event(item, reference) for reference in reference_items
        )
        (overlapping_items if overlaps else unique_items).append(item)
    return [*unique_items, *overlapping_items]


def _duplicate_topic_keys(item: dict[str, Any]) -> list[str]:
    combined = _combined_news_text(item)
    entity_groups = (
        ("northstar_industrial", ("northstar industrial", "北辰工业")),
        ("orion_robotics", ("orion robotics", "星云机器人")),
        ("harbor_motors", ("harbor motors", "远航汽车")),
        ("riverlight_energy", ("riverlight energy", "江灯能源")),
    )
    topic_groups = (
        ("robotics", ("robot", "robotics", "humanoid", "embodied", "机器人", "人形", "具身")),
        ("ai_factory", ("ai factory", "smart factory", "智能工厂", "工厂", "工业ai", "工业 ai")),
        ("low_altitude", ("evtol", "low-altitude", "low altitude", "低空", "飞行器")),
        ("satellite", ("satellite", "space", "卫星", "航天")),
        ("auto", ("electric vehicle", "smart driving", "autonomous driving", "汽车", "自动驾驶")),
    )
    entities = [
        entity
        for entity, aliases in entity_groups
        if any(alias in combined for alias in aliases)
    ]
    topics = [
        topic
        for topic, aliases in topic_groups
        if any(alias in combined for alias in aliases)
    ]
    return [f"{entity}:{topic}" for entity in entities for topic in topics]


def _sources_for_selected_items(selected_items: list[dict[str, Any]], configured_sources: list[Any]) -> list[dict[str, str]]:
    configured_by_url: dict[str, dict[str, str]] = {}
    configured_by_name: dict[str, dict[str, str]] = {}
    for source in configured_sources:
        if not isinstance(source, dict):
            continue
        normalized = {
            "name": str(source.get("name", "")).strip(),
            "url": str(source.get("url", "")).strip(),
            "summary": str(source.get("summary", "")).strip(),
        }
        url_key = normalized["url"].lower()
        name_key = normalized["name"].lower()
        if url_key:
            configured_by_url[url_key] = normalized
        if name_key:
            configured_by_name[name_key] = normalized

    selected_sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in selected_items:
        fallback = _source_from_news_item(item)
        url_key = str(item.get("url") or item.get("source_url") or "").strip().lower()
        source_name = str(item.get("source_name", "")).strip().lower()
        source = configured_by_url.get(url_key)
        if source is None and source_name:
            source = configured_by_name.get(source_name)
        source = dict(source or fallback)
        key = str(source.get("url") or source.get("name", "")).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        selected_sources.append(source)
    return selected_sources


def _prepare_article_for_render(article: dict[str, Any], *, role: str) -> dict[str, Any]:
    original_news_count = len(article.get("news_items", [])) if isinstance(article.get("news_items", []), list) else 0
    updated = sanitize_article_copy(article)
    news_items = updated.get("news_items", [])
    sanitized_news_count = len(news_items) if isinstance(news_items, list) else 0
    if sanitized_news_count != original_news_count:
        rebuilt = build_article(_article_seed_for_recovery(updated, cleared_fields={"title", "digest"}))
        for key in ("text_model_generation", "package_role", "package_role_label", "candidate_preflight"):
            if key in article:
                rebuilt[key] = article[key]
        rebuilt["fact_cards"] = _fact_card_digest(rebuilt.get("news_items", []))
        updated = sanitize_article_copy(rebuilt)
        news_items = updated.get("news_items", [])
    if isinstance(news_items, list):
        selected_items = [item for item in news_items if isinstance(item, dict)]
        sources = updated.get("sources", []) if isinstance(updated.get("sources", []), list) else []
        updated["sources"] = _sources_for_selected_items(selected_items, sources)
    updated["package_role"] = role
    updated["package_role_label"] = _role_label(role)
    updated["illustration_prompt"] = _illustration_prompt(updated)
    updated["illustration_alt"] = _illustration_alt(updated)
    updated["illustration_placeholder"] = f"{{{{ARTICLE_ILLUSTRATION_{role.upper()}}}}}"
    updated["inline_images"] = _inline_image_specs(updated, role=role)
    return updated


def _finalize_article_title(article: dict[str, Any], *, text_model_context: dict[str, Any]) -> dict[str, Any]:
    updated = dict(article)
    deterministic = build_article(_article_seed_for_recovery(updated, cleared_fields={"title"}))
    updated["title"] = str(deterministic.get("title", "")).strip()
    model_title = generate_final_article_title(updated, text_model_context)
    if model_title:
        updated["title"] = model_title
    return sanitize_article_copy(updated)


def _minimum_recovery_items(final_review_config: dict[str, Any], role: str) -> int:
    configured = final_review_config.get("min_news_items", {})
    defaults = {"primary": 5, "secondary": 3}
    value = configured.get(role, defaults.get(role, 1)) if isinstance(configured, dict) else defaults.get(role, 1)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return defaults.get(role, 1)


def _target_news_item_count(seed: dict[str, Any]) -> int:
    news_items = seed.get("news_items", [])
    default = len(news_items) if isinstance(news_items, list) else 0
    try:
        return max(default, int(seed.get("target_news_item_count", default) or default))
    except (TypeError, ValueError):
        return default


def _article_news_count(article: dict[str, Any]) -> int:
    news_items = article.get("news_items", [])
    return len(news_items) if isinstance(news_items, list) else 0


def _replenish_article_from_reserve(
    article: dict[str, Any],
    *,
    role: str,
    target_news_items: int,
    text_model_context: dict[str, Any],
    execution_budget: ExecutionBudget | None = None,
    max_batches: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_items = [item for item in article.get("news_items", []) if isinstance(item, dict)]
    reserve_items = [item for item in article.get("reserve_news_items", []) if isinstance(item, dict)]
    missing = max(0, target_news_items - len(current_items))
    report: dict[str, Any] = {
        "role": role,
        "status": "not_needed" if missing == 0 else "attempted",
        "changed": False,
        "requested": missing,
        "attempted": 0,
        "added": 0,
        "remaining_reserve": len(reserve_items),
        "added_items": [],
        "model_generation": {},
        "batches": [],
        "budget_exhausted": False,
    }
    if missing == 0 or not reserve_items:
        if missing and not reserve_items:
            report["status"] = "reserve_exhausted"
        return article, report

    existing_keys = {_news_item_identity(item) for item in current_items}
    reserve_work = [dict(item) for item in reserve_items]
    additions: list[dict[str, Any]] = []
    for batch_index in range(max(1, int(max_batches))):
        missing = max(0, target_news_items - len(current_items) - len(additions))
        if missing == 0 or not reserve_work:
            break
        if execution_budget is not None and not execution_budget.can_start_optional(30):
            report["budget_exhausted"] = True
            break

        active_keys = existing_keys | {_news_item_identity(item) for item in additions}
        candidates = [
            item
            for item in reserve_work
            if _news_item_identity(item) not in active_keys and not blocked_by_wechat_platform_risk(item)
        ]
        if not candidates:
            break
        ready_candidates = [
            item
            for item in candidates
            if str(item.get("detail_heading", "")).strip()
            and str(item.get("reader_summary", "")).strip()
        ][:missing]
        ready_keys = {_news_item_identity(item) for item in ready_candidates}
        raw_candidates = [item for item in candidates if _news_item_identity(item) not in ready_keys]
        raw_batch = raw_candidates[: max(2, missing * 2)]
        report["attempted"] += len(raw_batch)

        enhanced_candidates: list[dict[str, Any]] = []
        model_generation: dict[str, Any] = {}
        if raw_batch:
            candidate_seed = _article_seed_for_recovery(article, cleared_fields={"title", "digest"})
            candidate_seed["news_items"] = raw_batch
            candidate_seed["reserve_news_items"] = []
            candidate_seed["sources"] = [_source_from_news_item(item) for item in raw_batch]
            candidate_article = attach_fact_cards(build_article(candidate_seed))
            candidate_article = enhance_article_with_text_models(candidate_article, text_model_context)
            candidate_article = sanitize_article_copy(candidate_article)
            model_generation = candidate_article.get("text_model_generation", {})
            report["model_generation"] = model_generation
            enhanced_candidates = [
                item
                for item in candidate_article.get("news_items", [])
                if isinstance(item, dict)
                and str(item.get("detail_heading", "")).strip()
                and str(item.get("reader_summary", "")).strip()
                and not blocked_by_wechat_platform_risk(item)
            ]

        publishable_candidates = [*ready_candidates, *enhanced_candidates]
        combined = _dedupe_news_items([*current_items, *additions, *publishable_candidates])
        batch_additions = [
            _without_replenishment_metadata(item)
            for item in combined
            if _news_item_identity(item) not in active_keys
        ][:missing]
        additions.extend(batch_additions)
        added_keys = {_news_item_identity(item) for item in batch_additions}
        attempted_keys = {_news_item_identity(item) for item in raw_batch}
        publishable_by_key = {_news_item_identity(item): item for item in publishable_candidates}
        next_reserve: list[dict[str, Any]] = []
        deferred_failed: list[dict[str, Any]] = []
        for item in reserve_work:
            key = _news_item_identity(item)
            if key in added_keys:
                continue
            if key in publishable_by_key:
                next_reserve.append(publishable_by_key[key])
                continue
            if key in attempted_keys:
                failures = int(item.get("_replenishment_failures", 0) or 0) + 1
                if failures < 2:
                    deferred_failed.append({**item, "_replenishment_failures": failures})
                continue
            next_reserve.append(item)
        reserve_work = [*next_reserve, *deferred_failed]
        report["batches"].append(
            {
                "batch": batch_index + 1,
                "requested": missing,
                "attempted": len(raw_batch),
                "added": len(batch_additions),
                "remaining_reserve": len(reserve_work),
                "model_generation": model_generation,
            }
        )

    if not additions:
        updated = dict(article)
        updated["reserve_news_items"] = reserve_work
        report["status"] = "budget_exhausted" if report["budget_exhausted"] else "candidates_not_publishable"
        report["remaining_reserve"] = len(reserve_work)
        return updated, report

    added_keys = {_news_item_identity(item) for item in additions}
    updated = dict(article)
    updated["news_items"] = [*current_items, *additions]
    updated["reserve_news_items"] = [item for item in reserve_work if _news_item_identity(item) not in added_keys]
    updated.pop("title", None)
    updated.pop("digest", None)
    updated["sources"] = _sources_for_selected_items(updated["news_items"], article.get("sources", []))
    rebuilt = build_article(_article_seed_for_recovery(updated, cleared_fields={"title", "digest"}))
    for key in ("text_model_generation", "package_role", "package_role_label", "candidate_preflight"):
        if key in article:
            rebuilt[key] = article[key]
    rebuilt["fact_cards"] = _fact_card_digest(rebuilt.get("news_items", []))
    report["status"] = "applied"
    report["changed"] = True
    report["added"] = len(additions)
    report["remaining_reserve"] = len(rebuilt.get("reserve_news_items", []))
    report["added_items"] = [
        {
            "title": str(item.get("title") or item.get("detail_heading") or "").strip(),
            "source_name": str(item.get("source_name", "")).strip(),
        }
        for item in additions
    ]
    return rebuilt, report


def _run_final_review_recovery_cycles(
    article: dict[str, Any],
    *,
    role: str,
    min_news_items: int,
    target_news_items: int,
    final_review_config: dict[str, Any],
    text_model_context: dict[str, Any],
    execution_budget: ExecutionBudget | None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    max_cycles = _bounded_int(final_review_config.get("max_recovery_cycles", 3), default=3, minimum=1, maximum=3)
    minimum_cycle_seconds = _bounded_int(
        final_review_config.get("minimum_cycle_budget_seconds", 45),
        default=45,
        minimum=0,
        maximum=300,
    )
    initial_review = article.get("final_ai_review", {}) if isinstance(article.get("final_ai_review", {}), dict) else {}
    initial_issues = initial_review.get("issues", []) if isinstance(initial_review.get("issues", []), list) else []
    aggregate: dict[str, Any] = {
        "status": "not_needed",
        "role": role,
        "changed": False,
        "before_risk_level": str(initial_review.get("risk_level", "unknown")),
        "before_issue_count": len([issue for issue in initial_issues if isinstance(issue, dict)]),
        "min_news_items": min_news_items,
        "max_cycles": max_cycles,
        "cycles": [],
        "actions": [],
        "unresolved_issues": [],
        "dropped_items": [],
        "below_minimum": False,
        "budget_exhausted": False,
    }
    replenishments: list[dict[str, Any]] = []
    seen_issue_keys: set[tuple[str, str]] = set()

    for cycle_index in range(max_cycles):
        review = article.get("final_ai_review", {}) if isinstance(article.get("final_ai_review", {}), dict) else {}
        issues = review.get("issues", []) if isinstance(review.get("issues", []), list) else []
        blocking_issues = _blocking_recovery_issues(
            [issue for issue in issues if isinstance(issue, dict)]
        )
        needs_replenishment = _article_news_count(article) < min_news_items
        if not blocking_issues and not needs_replenishment:
            break
        if execution_budget is not None and not execution_budget.can_start_optional(minimum_cycle_seconds):
            aggregate["budget_exhausted"] = True
            break

        issue_keys = _review_issue_keys(blocking_issues, article.get("news_items", []))
        article, cycle = _recover_article_from_final_review(
            article,
            role=role,
            min_news_items=min_news_items,
            force_drop_issue_keys=issue_keys & seen_issue_keys,
        )
        seen_issue_keys.update(issue_keys)
        if cycle["changed"]:
            article = _prepare_article_for_render(article, role=role)
        article, replenishment = _replenish_article_from_reserve(
            article,
            role=role,
            target_news_items=min_news_items,
            text_model_context=text_model_context,
            execution_budget=execution_budget,
        )
        replenishments.append(replenishment)
        cycle["cycle"] = cycle_index + 1
        cycle["replenishment"] = replenishment
        aggregate["cycles"].append(cycle)
        aggregate["actions"].extend(cycle.get("actions", []))
        aggregate["dropped_items"].extend(cycle.get("dropped_items", []))
        visible_changed = bool(cycle.get("changed") or replenishment.get("changed"))
        aggregate["changed"] = bool(aggregate["changed"] or visible_changed)
        if not visible_changed:
            if replenishment.get("budget_exhausted"):
                aggregate["budget_exhausted"] = True
            break

        article = _prepare_article_for_render(article, role=role)
        article = review_final_article_deterministically(
            article,
            mode=str(final_review_config.get("mode", "shadow")),
        )

    after_review = article.get("final_ai_review", {}) if isinstance(article.get("final_ai_review", {}), dict) else {}
    after_issues = after_review.get("issues", []) if isinstance(after_review.get("issues", []), list) else []
    after_issues = [issue for issue in after_issues if isinstance(issue, dict)]
    blocking_after_issues = _blocking_recovery_issues(after_issues)
    aggregate["after_risk_level"] = str(after_review.get("risk_level", "unknown"))
    aggregate["after_issue_count"] = len(after_issues)
    aggregate["unresolved_issues"] = after_issues
    aggregate["below_minimum"] = _article_news_count(article) < min_news_items
    if aggregate["cycles"] or aggregate["before_issue_count"]:
        aggregate["status"] = (
            "resolved"
            if not blocking_after_issues and not aggregate["below_minimum"]
            else "partial"
        )
    if aggregate["budget_exhausted"] and (blocking_after_issues or aggregate["below_minimum"]):
        aggregate["status"] = "budget_exhausted"
    return article, aggregate, replenishments


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _news_item_identity(item: dict[str, Any]) -> str:
    return str(item.get("url") or item.get("source_url") or item.get("title", "")).strip().lower()


def _without_replenishment_metadata(item: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(item)
    cleaned.pop("_replenishment_failures", None)
    return cleaned


def _strip_replenishment_metadata(article: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(article)
    for field in ("news_items", "reserve_news_items"):
        items = cleaned.get(field, [])
        if isinstance(items, list):
            cleaned[field] = [
                _without_replenishment_metadata(item)
                for item in items
                if isinstance(item, dict)
            ]
    return cleaned


def _recover_article_from_final_review(
    article: dict[str, Any],
    *,
    role: str,
    min_news_items: int,
    force_drop_issue_keys: set[tuple[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    review = article.get("final_ai_review", {})
    issues = review.get("issues", []) if isinstance(review, dict) else []
    valid_issues = [issue for issue in issues if isinstance(issue, dict)] if isinstance(issues, list) else []
    report: dict[str, Any] = {
        "status": "not_needed" if not valid_issues else "attempted",
        "role": role,
        "changed": False,
        "before_risk_level": str(review.get("risk_level", "unknown")) if isinstance(review, dict) else "unknown",
        "before_issue_count": len(valid_issues),
        "min_news_items": min_news_items,
        "actions": [],
        "unresolved_issues": [],
        "dropped_items": [],
        "below_minimum": False,
    }
    if not valid_issues:
        return article, report

    updated = dict(article)
    news_items = [dict(item) for item in article.get("news_items", []) if isinstance(item, dict)]
    drop_indexes: set[int] = set()
    article_fields_cleared: set[str] = set()
    forced_drop_keys = force_drop_issue_keys or set()

    for issue in valid_issues:
        issue_type = str(issue.get("type", "")).strip()
        action = str(issue.get("action", "keep")).strip()
        severity = str(issue.get("severity", "low")).strip().lower()
        evidence = str(issue.get("evidence", "")).strip()
        reason = str(issue.get("reason", "")).strip()
        if severity in {"medium", "high"} and any(
            phrase in reason for phrase in ("与栏目无关", "与智能制造无关", "分类错位", "明显跑题", "不属于该栏目")
        ):
            issue_type = "off_topic"
            action = "drop_item"
        if severity == "low" and action == "keep":
            continue
        try:
            news_index = int(issue.get("news_index", 0) or 0)
        except (TypeError, ValueError):
            news_index = 0

        if 1 <= news_index <= len(news_items):
            item = news_items[news_index - 1]
            issue_key = (_news_item_identity(item), issue_type)
            normalized_issue = {**issue, "type": issue_type, "action": action, "severity": severity}
            if not final_review_issue_blocks_publish(normalized_issue) and issue_key not in forced_drop_keys:
                continue
            if (
                issue_type == "platform_risk"
                or final_review_issue_blocks_publish(normalized_issue)
                or issue_key in forced_drop_keys
            ):
                drop_indexes.add(news_index - 1)
                continue
            cleared = _clear_issue_fields(item, issue_type=issue_type, evidence=evidence)
            if cleared:
                report["actions"].extend(
                    {
                        "action": "clear_field",
                        "news_index": news_index,
                        "field": field,
                        "issue_type": issue_type,
                    }
                    for field in cleared
                )
                report["changed"] = True
                continue
            report["unresolved_issues"].append(issue)
            continue

        normalized_issue = {**issue, "type": issue_type, "action": action, "severity": severity}
        if not final_review_issue_blocks_publish(normalized_issue):
            continue
        cleared_article_field = False
        for field in ("title", "digest"):
            if evidence and _evidence_in_text(evidence, str(updated.get(field, ""))):
                updated.pop(field, None)
                article_fields_cleared.add(field)
                report["actions"].append(
                    {"action": "clear_article_field", "field": field, "issue_type": issue_type}
                )
                report["changed"] = True
                cleared_article_field = True
        if not cleared_article_field and issue_type not in {"source_outside_fact"}:
            report["unresolved_issues"].append(issue)

    if drop_indexes:
        retained = [item for index, item in enumerate(news_items) if index not in drop_indexes]
        dropped = [news_items[index] for index in sorted(drop_indexes)]
        news_items = retained
        report["dropped_items"] = [
            {
                "title": str(item.get("title") or item.get("detail_heading") or "").strip(),
                "source_name": str(item.get("source_name", "")).strip(),
            }
            for item in dropped
        ]
        report["actions"].append({"action": "drop_items", "count": len(dropped)})
        report["changed"] = True
        updated.pop("title", None)
        updated.pop("digest", None)
        article_fields_cleared.update({"title", "digest"})
        if len(news_items) < min_news_items:
            report["below_minimum"] = True

    if not report["changed"]:
        return article, report

    updated["news_items"] = news_items
    sources = updated.get("sources", []) if isinstance(updated.get("sources", []), list) else []
    updated["sources"] = _sources_for_selected_items(news_items, sources)
    rebuilt = build_article(_article_seed_for_recovery(updated, cleared_fields=article_fields_cleared))
    for key in ("text_model_generation", "package_role", "package_role_label", "candidate_preflight"):
        if key in article:
            rebuilt[key] = article[key]
    rebuilt["fact_cards"] = _fact_card_digest(rebuilt.get("news_items", []))
    report["status"] = "applied"
    return rebuilt, report


def _review_issue_keys(issues: list[Any], news_items: Any) -> set[tuple[str, str]]:
    items = [item for item in news_items if isinstance(item, dict)] if isinstance(news_items, list) else []
    keys: set[tuple[str, str]] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        try:
            news_index = int(issue.get("news_index", 0) or 0)
        except (TypeError, ValueError):
            continue
        issue_type = str(issue.get("type", "")).strip()
        if 1 <= news_index <= len(items) and issue_type:
            keys.add((_news_item_identity(items[news_index - 1]), issue_type))
    return keys


def _blocking_recovery_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [issue for issue in issues if final_review_issue_blocks_publish(issue)]


def _clear_issue_fields(item: dict[str, Any], *, issue_type: str, evidence: str) -> list[str]:
    recoverable_types = {
        "source_outside_fact",
        "numeric_drift",
        "ai_cliche",
        "repetition",
        "promotion",
        "byline",
        "english_fragment",
        "truncation",
    }
    if issue_type not in recoverable_types:
        return []
    hits = [
        field
        for field in ("detail_heading", "reader_summary")
        if evidence and _evidence_in_text(evidence, str(item.get(field, "")))
    ]
    if issue_type in {"ai_cliche", "repetition"} and "reader_summary" in hits:
        hits = ["reader_summary"]
    if not hits and issue_type in {"truncation", "english_fragment", "byline", "promotion"}:
        hits = ["detail_heading", "reader_summary"]
    for field in hits:
        item.pop(field, None)
    return hits


def _evidence_in_text(evidence: str, value: str) -> bool:
    normalized_evidence = re.sub(r"\s+", "", evidence).lower()
    normalized_value = re.sub(r"\s+", "", value).lower()
    return bool(normalized_evidence and normalized_evidence in normalized_value)


def _article_seed_for_recovery(article: dict[str, Any], *, cleared_fields: set[str]) -> dict[str, Any]:
    keys = (
        "title",
        "digest",
        "topic",
        "column",
        "audience",
        "topic_reason",
        "source_policy",
        "risk_tags",
        "sources",
        "target_date",
        "news_items",
        "reserve_news_items",
        "target_news_item_count",
        "news_status",
        "wechat_platform_risk_replacements",
    )
    return {key: article[key] for key in keys if key in article and key not in cleared_fields}


def _fact_card_digest(news_items: Any) -> list[dict[str, Any]]:
    digest = []
    for index, item in enumerate(news_items if isinstance(news_items, list) else [], start=1):
        if not isinstance(item, dict):
            continue
        card = item.get("fact_card", {}) if isinstance(item.get("fact_card", {}), dict) else {}
        digest.append(
            {
                "index": index,
                "subject": str(card.get("subject", "")),
                "action": str(card.get("action", "")),
                "confidence": str(card.get("confidence", "")),
                "source_name": str(card.get("source_name") or item.get("source_name", "")),
            }
        )
    return digest


def _rank_manufacturing_news_items(news_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        (_manufacturing_news_score(item), index, item)
        for index, item in enumerate(news_items)
        if _is_manufacturing_news_item(item) and not _weak_manufacturing_news_item(item)
    ]
    if not candidates:
        candidates = [
            (_manufacturing_news_score(item), index, item)
            for index, item in enumerate(news_items)
            if _is_manufacturing_news_item(item)
        ]
    ranked = [item for _score, _index, item in sorted(candidates, key=lambda row: (-row[0], row[1]))]
    return ranked or news_items[:8]


def _is_manufacturing_news_item(item: dict[str, Any]) -> bool:
    combined = _combined_news_text(item)
    keywords = (
        "smart manufacturing",
        "manufacturing",
        "industrial",
        "ai factory",
        "factory",
        "robot",
        "robotics",
        "humanoid",
        "embodied intelligence",
        "battery output",
        "smart driving",
        "electric vehicle",
        "production line",
        "factory output",
        "manufacturing output",
        "evtol",
        "satellite",
        "autonomous driving",
        "gpu",
        "chip",
        "semiconductor",
        "compute infrastructure",
        "compute stack",
        "data center",
        "智能制造",
        "智能工厂",
        "工业ai",
        "工业 ai",
        "工业",
        "制造",
        "工厂",
        "产线",
        "机器人",
        "人形",
        "具身",
        "自动化",
        "低空",
        "卫星",
        "自动驾驶",
        "量产",
        "总部",
        "落户",
    )
    return any(keyword in combined for keyword in keywords)


def _weak_manufacturing_news_item(item: dict[str, Any]) -> bool:
    combined = _combined_news_text(item)
    weak_terms = (
        "早报",
        "晚报",
        "晨间简报",
        "股价",
        "荐股",
        "概念股",
        "涨停",
        "研报",
        "世界杯",
        "比赛用球",
        "体育装备",
        "球迷",
        "运动品牌",
        "演员",
        "电视剧",
        "网络剧",
        "影视",
        "明星效应",
        "粉丝经济",
    )
    return any(term in combined for term in weak_terms)


def _manufacturing_news_score(item: dict[str, Any]) -> int:
    combined = _combined_news_text(item)
    score = 0
    title = str(item.get("title", "")).strip()
    summary = str(item.get("summary", "")).strip()
    if title and not _mostly_ascii(title):
        score += 2
    elif _mostly_ascii(title) and _mostly_ascii(summary):
        score -= 3
    boosts = [
        (("ai factory",), 7),
        (("ai工厂",), 7),
        (("智能工厂",), 7),
        (("机器人",), 6),
        (("robot",), 6),
        (("embodied intelligence",), 6),
        (("battery output",), 4),
        (("smart driving",), 4),
        (("electric vehicle",), 3),
        (("人形",), 6),
        (("具身",), 5),
        (("evtol",), 6),
        (("低空",), 6),
        (("卫星",), 5),
        (("satellite",), 5),
        (("工业ai",), 5),
        (("工业 ai",), 5),
        (("制造",), 4),
        (("工厂",), 4),
        (("量产",), 4),
        (("交付",), 4),
        (("总部",), 3),
        (("落户",), 3),
        (("融资",), 2),
        (("获投",), 2),
    ]
    for keywords, value in boosts:
        if all(keyword in combined for keyword in keywords):
            score += value
    if any(keyword in combined for keyword in ("项目", "客户", "订单", "产线", "供应链")):
        score += 2
    return score


def _source_from_news_item(item: dict[str, Any]) -> dict[str, str]:
    title = str(item.get("title", "")).strip()
    source_name = str(item.get("source_name", "")).strip()
    published_at = str(item.get("published_at", "")).strip()
    summary = _strip_leading_news_byline(str(item.get("summary", "")).strip())
    return {
        "name": f"{source_name}：{title}" if source_name else title,
        "url": str(item.get("url", "")).strip(),
        "summary": f"{published_at}；{summary}" if published_at else summary,
    }


def _strip_leading_news_byline(summary: str) -> str:
    value = summary.strip()
    dateline = r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+\d{2}:\d{2})?|(?:\d{1,2}月\d{1,2}日))\s*[；;，,、]\s*"
    surname = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元顾孟平黄穆萧尹姚邵汪祁毛米贝明伏成戴宋庞熊纪舒屈项祝董梁杜阮蓝闵季麻强贾路危江童颜郭梅林钟徐邱骆高夏蔡田胡凌霍虞万柯管卢莫房解应宗丁宣邓杭洪包左石崔龚程邢裴陆翁荀惠曲封储段富焦巴谷车侯全班秋仲伊宫宁仇甘祖武符刘景龙叶白蒲赖卓池乔闻党谭劳申冉雍桂牛边尚农温庄柴瞿阎慕连习艾鱼古易戈廖终居衡步都耿满弘匡寇广沃利越师巩聂晁辛阚简饶曾关相查游权盖益桓公"
    compound_surname = "欧阳|司马|上官|诸葛|东方|令狐|夏侯|尉迟|皇甫|公孙|长孙|慕容|司徒|司空"
    name_token = rf"(?!(?:作者|文|记者|编辑)(?:\s|[|丨｜:：]|$))(?:(?:{compound_surname})[\u4e00-\u9fff]{{1,2}}|[{surname}][\u4e00-\u9fff]{{1,2}}|[A-Za-z·.\-]{{2,24}})"
    byline = (
        rf"^(?:作者|文|记者|编辑)\s*[|丨｜:：]\s*{name_token}"
        rf"(?:\s+{name_token}){{0,3}}"
    )
    for _ in range(8):
        stripped = re.sub(dateline, "", value).strip(" 　，,。；;")
        stripped = re.sub(byline, "", stripped).strip(" 　，,。；;")
        if stripped == value:
            break
        value = stripped
    return value


def _secondary_news_seed(config: dict[str, Any], *, primary_seed: dict[str, Any]) -> dict[str, Any]:
    news_items = [item for item in primary_seed.get("news_items", []) if isinstance(item, dict)]
    item = _secondary_news_item(news_items)
    raw_title = str(item.get("title", "")).strip() or "今日科技新闻延展"
    title = _secondary_news_title(item)
    source_name = str(item.get("source_name", "")).strip()
    url = str(item.get("url", "")).strip()
    summary = str(item.get("summary", "")).strip() or raw_title
    source_label = f"{source_name}：{raw_title}" if source_name else raw_title
    return {
        "topic": title,
        "title": title,
        "column": str(config.get("news_extension_column") or "科技新闻延展"),
        "audience": str(config.get("audience") or "对科技新闻感兴趣、但不想被术语淹没的读者"),
        "digest": "从今天早报中挑出一条值得展开的新闻，讲清它为什么重要，以及和普通读者有什么关系。",
        "topic_reason": "副文章承接当天真实新闻池，不再默认生成说明文或格式解释。",
        "source_policy": "single_news_extension",
        "risk_tags": ["requires_external_sources", "news_item_extension"],
        "target_date": str(primary_seed.get("target_date", "")).strip(),
        "news_status": str(primary_seed.get("news_status", "")).strip(),
        "news_items": [item],
        "sources": [
            {
                "name": source_label,
                "url": url,
                "summary": summary,
            }
        ],
    }


def _secondary_news_item(news_items: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        (_secondary_news_score(item), index, item)
        for index, item in enumerate(news_items)
        if not _weak_secondary_news_item(item)
    ]
    if candidates:
        return sorted(candidates, key=lambda row: (-row[0], row[1]))[0][2]
    return news_items[1] if len(news_items) > 1 else news_items[0]


def _secondary_news_score(item: dict[str, Any]) -> int:
    combined = _combined_news_text(item)
    score = 0
    boosts = [
        (("试点", "工厂"), 4),
        (("pilot", "factory"), 4),
        (("量产", "机器人"), 5),
        (("deploy", "robot"), 4),
        (("支付", "商户"), 4),
        (("open source", "agent"), 3),
        (("开源", "智能体"), 3),
        (("汽车", "智能体"), 3),
    ]
    for keywords, value in boosts:
        if all(keyword in combined for keyword in keywords):
            score += value
    if any(keyword in combined for keyword in ("产品", "工具", "支付", "硬件", "机器人", "物流", "出海", "商户", "智能体", "agent")):
        score += 2
    if any(keyword in combined for keyword in ("融资", "获投", "并购", "收购", "客户", "成本", "价格")):
        score += 1
    if any(keyword in combined for keyword in ("economic research exchange", "confidential", "s-1", "sec", "vision for the future")):
        score -= 3
    return score


def _weak_secondary_news_item(item: dict[str, Any]) -> bool:
    combined = _combined_news_text(item)
    weak_terms = ("公募基金", "私募基金", "基金业协会", "通道空转", "概念炒作")
    if any(term in combined for term in weak_terms):
        return True
    event_terms = ("aicon", "确认出席", "applications officially close", "startup battlefield", "大赛", "正式启动")
    if any(term in combined for term in event_terms):
        return True
    title = str(item.get("title", "")).strip().lower()
    summary = str(item.get("summary", "")).strip().lower()
    if title and summary and title == summary and len(title) < 80:
        return True
    if "built to benefit everyone" in combined and "vision for the future" in combined:
        return True
    return False


def _secondary_news_title(item: dict[str, Any]) -> str:
    combined = _combined_news_text(item)
    patterns = [
        (("智能制造", "质检"), "智能制造质检落地"),
        (("量产", "机器人"), "机器人进入量产"),
        (("open source", "agent"), "开源智能体工具"),
        (("开源", "智能体"), "开源智能体工具"),
        (("海外", "支付"), "跨境支付进入AI工作流"),
        (("汽车", "智能体"), "汽车智能体进入产品流程"),
        (("物流", "机器人"), "物流机器人进入交付"),
    ]
    for keywords, title in patterns:
        if all(keyword in combined for keyword in keywords):
            return f"{_compact_news_title(title, max_length=18)}，为什么值得单独看"
    element = news_title_element(item)
    if _mostly_ascii(element):
        element = "科技新闻延展"
    return f"{_compact_news_title(element, max_length=18)}，为什么值得单独看"


def _combined_news_text(item: dict[str, Any]) -> str:
    return f"{item.get('title', '')} {item.get('summary', '')} {item.get('source_name', '')}".lower()


def _tertiary_seed(
    config: dict[str, Any],
    *,
    selected_seed: dict[str, Any],
    seed_source: str,
    primary_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    news_items = [item for item in (primary_seed or {}).get("news_items", []) if isinstance(item, dict)]
    if news_items:
        return {
            "topic": str(config.get("topic") or "把重复任务交给 AI 前，先写清这四行"),
            "title": str(config.get("title") or "把重复任务交给 AI 前，先写清这四行"),
            "column": str(config.get("column") or "AI 工具箱"),
            "audience": str(config.get("audience") or "想把 AI 用进日常工作的新手读者"),
            "digest": str(
                config.get("digest")
                or "给一个可复制的四行任务模板，先把小流程跑顺，再谈自动化。"
            ),
            "topic_reason": "第三篇副文章不再延展新闻本身，而是提供轻实用、可收藏的 AI 任务模板。",
            "source_policy": "daily_practical_takeaway",
            "risk_tags": ["practical_takeaway", "workflow_template"],
            "target_date": str((primary_seed or {}).get("target_date", "")).strip(),
            "news_status": str((primary_seed or {}).get("news_status", "")).strip(),
            "news_items": news_items[:6],
            "sources": _tertiary_toolbox_sources(seed_source),
        }

    topic = str(config.get("topic") or selected_seed.get("topic") or "今天用 AI 先省下一步")
    sources = selected_seed.get("sources", [])
    if not isinstance(sources, list) or not sources:
        sources = [
            {
                "name": "本地选题配置",
                "url": seed_source,
                "summary": "第三篇副文章使用本地选题配置生成，作为工具箱式实践文章。",
            }
        ]
    return {
        "topic": topic,
        "title": str(config.get("title") or f"{_compact_news_title(topic, max_length=18)}，今天先做一个小动作"),
        "column": str(config.get("column") or "AI 工具箱"),
        "audience": str(selected_seed.get("audience") or "想把 AI 用进日常工作的新手读者"),
        "digest": str(config.get("digest") or "把一个重复任务拆清楚，比追十个新工具更有用。"),
        "topic_reason": str(config.get("purpose") or "第三篇副文章提供轻实用、可收藏的行动建议。"),
        "source_policy": "local_topic_explainer",
        "risk_tags": selected_seed.get("risk_tags", []) if isinstance(selected_seed.get("risk_tags", []), list) else [],
        "sources": sources,
    }


def _tertiary_toolbox_sources(seed_source: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "AI 工具箱模板",
            "url": seed_source,
            "summary": "第三篇副文章使用固定工具箱结构生成，重点是提供可复制的 AI 任务拆解模板。",
        }
    ]


def _mostly_ascii(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return ascii_count / len(value) > 0.8


def _compact_news_title(title: str, *, max_length: int = 46) -> str:
    value = " ".join(title.split())
    if len(value) <= max_length:
        return value
    return value[: max_length - 1] + "..."


def _aggregate_risk(levels: list[str]) -> str:
    if "high" in levels:
        return "high"
    if "medium" in levels:
        return "medium"
    return "low"


def _combined_list(*values: Any) -> list[Any]:
    combined: list[Any] = []
    for value in values:
        if isinstance(value, list):
            combined.extend(value)
    return combined


def _role_files_for_package(role_files: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in role_files.items():
        if isinstance(value, list):
            result[name] = [str(item) for item in value]
        else:
            result[name] = str(value)
    return result


def _article_summary(article: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(article.get("schema_version", "")),
        "role": str(article.get("package_role", "")),
        "role_label": str(article.get("package_role_label", "")),
        "title": str(article.get("title", "")),
        "topic": str(article.get("topic", "")),
        "column": str(article.get("column", "")),
        "word_count_estimate": int(article.get("word_count_estimate", 0) or 0),
        "news_item_count": _article_news_count(article),
        "reserve_item_count": len(article.get("reserve_news_items", []))
        if isinstance(article.get("reserve_news_items", []), list)
        else 0,
        "risk_level": str(review.get("risk_level", "")),
        "wechat_ready": bool(review.get("wechat_ready", False)),
        "image_provider": _image_provider_name(article),
    }


def _illustration_prompt(article: dict[str, Any]) -> str:
    role = str(article.get("package_role_label", "文章"))
    topic = str(article.get("topic", "AI 自动化"))
    return f"{role}正文配图，主题：{topic}。现代清爽的信息流插图，包含文档、节点、流程线和 AI 辅助分析感。"


def _illustration_alt(article: dict[str, Any]) -> str:
    topic = str(article.get("topic", "AI 主题"))
    return f"{topic}的流程化信息插图"


def _openai_image_client(
    env_path: str | Path,
    *,
    enabled: bool,
) -> tuple[OpenAIImageClient | None, dict[str, Any]]:
    if not enabled:
        return None, {
            "status": "disabled",
            "provider": "local_fallback",
            "reason": "Image API generation is disabled for this run.",
        }

    config = load_openai_image_config(env_path)
    if not config.configured:
        return None, {
            "status": "missing_api_key",
            "provider": "local_fallback",
            "reason": "OPENAI_API_KEY is not configured.",
            "model": config.model,
            "api_base": config.api_base,
        }

    try:
        return OpenAIImageClient(config), {
            "status": "configured",
            "provider": "openai_image_api",
            "model": config.model,
            "size": config.size,
            "quality": config.quality,
            "output_format": config.output_format,
            "api_base": config.api_base,
        }
    except OpenAIImageError as exc:
        return None, {
            "status": "unavailable",
            "provider": "local_fallback",
            "reason": str(exc),
            "payload": exc.payload,
            "model": config.model,
            "api_base": config.api_base,
        }


def _write_article_images(
    article: dict[str, Any],
    *,
    role: str,
    role_dir: Path,
    image_client: OpenAIImageClient | None,
    provider_status: dict[str, Any],
    now: datetime,
) -> tuple[Path, Path, list[Path], dict[str, Any]]:
    cover_path = role_dir / "cover.png"
    illustration_path = role_dir / "illustration.png"
    cover_prompt = _cover_image_prompt(article)
    illustration_prompt = _article_image_prompt(article)

    if role == "primary" and _uses_weekly_briefing_cover(article):
        cover_result = write_weekly_briefing_cover(cover_path, now=now)
        cover_result["prompt"] = cover_prompt
        cover_result["role"] = role
    else:
        cover_fallback = (
            (lambda path: write_article_illustration_png(path, role="tertiary-cover", width=900, height=500))
            if role == "tertiary"
            else (lambda path: write_test_cover_png(path))
        )
        cover_result = _write_image_asset(
            output_path=cover_path,
            fallback_writer=cover_fallback,
            image_client=image_client,
            prompt=cover_prompt,
            fallback_role=role,
            provider_status=provider_status,
        )
    illustration_result = _write_image_asset(
        output_path=illustration_path,
        fallback_writer=lambda path: write_article_illustration_png(path, role=role),
        image_client=image_client,
        prompt=illustration_prompt,
        fallback_role=role,
        provider_status=provider_status,
    )
    inline_results = []
    inline_paths = []
    inline_images = article.get("inline_images", [])
    if isinstance(inline_images, list):
        for index, image in enumerate(inline_images, start=1):
            if not isinstance(image, dict):
                continue
            source_url = str(image.get("source_url", "")).strip()
            source_ext = _source_image_extension(source_url) if source_url else ".png"
            inline_path = role_dir / f"inline-{index}{source_ext}"
            if source_url:
                source_result = _write_source_inline_image(
                    url=source_url,
                    output_path=inline_path,
                    alt=str(image.get("alt", "")),
                )
                if source_result.get("status") == "downloaded":
                    downloaded_path = Path(str(source_result.get("path", "") or inline_path))
                    inline_paths.append(downloaded_path)
                    inline_results.append(source_result)
                    continue
                inline_path = role_dir / f"inline-{index}.png"
                image["caption"] = "AI 生成配图 · 辅助理解新闻脉络"
            prompt = str(image.get("prompt", "")).strip() or _article_image_prompt(article)
            result = _write_image_asset(
                output_path=inline_path,
                fallback_writer=lambda path, inline_role=f"{role}-inline": write_article_illustration_png(
                    path,
                    role=inline_role,
                ),
                image_client=image_client,
                prompt=prompt,
                fallback_role=f"{role}-inline",
                provider_status=provider_status,
            )
            if source_url:
                result["source_image_attempt"] = source_result
            inline_paths.append(inline_path)
            inline_results.append(result)
    return cover_path, illustration_path, inline_paths, {
        "cover": cover_result,
        "illustration": illustration_result,
        "inline_images": inline_results,
    }


def _write_image_asset(
    *,
    output_path: Path,
    fallback_writer: Any,
    image_client: OpenAIImageClient | None,
    prompt: str,
    fallback_role: str,
    provider_status: dict[str, Any],
) -> dict[str, Any]:
    if image_client is not None:
        try:
            result = image_client.generate_image(prompt=prompt, output_path=output_path)
            result["prompt"] = prompt
            return result
        except Exception as exc:
            fallback_writer(output_path)
            payload = exc.payload if isinstance(exc, OpenAIImageError) else {}
            return {
                "provider": "local_fallback",
                "status": "fallback_after_openai_error",
                "path": str(output_path),
                "prompt": prompt,
                "role": fallback_role,
                "reason": str(exc),
                "payload": payload,
            }

    fallback_writer(output_path)
    return {
        "provider": "local_fallback",
        "status": str(provider_status.get("status", "fallback")),
        "path": str(output_path),
        "prompt": prompt,
        "role": fallback_role,
        "reason": str(provider_status.get("reason", "OpenAI image generation is unavailable.")),
    }


def _write_source_inline_image(*, url: str, output_path: Path, alt: str) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    actual_path = output_path
    webp_source = _source_image_is_webp(url, "")
    try:
        response = requests.get(
            url,
            headers={"User-Agent": SOURCE_IMAGE_USER_AGENT},
            timeout=SOURCE_IMAGE_TIMEOUT_SECONDS,
            stream=True,
        )
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        webp_source = _source_image_is_webp(url, content_type)
        allowed_content_types = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}
        if content_type and content_type not in allowed_content_types:
            return {
                "provider": "source_image",
                "status": "skipped_non_image",
                "url": url,
                "path": str(output_path),
                "reason": f"Content-Type is {content_type}",
            }
        content_ext = _source_image_extension_for_content_type(content_type)
        if webp_source:
            actual_path = output_path.with_suffix(".source.webp")
        elif actual_path.suffix.lower() == ".webp":
            actual_path = actual_path.with_suffix(".jpg")
        if content_ext and actual_path.suffix.lower() != content_ext:
            actual_path = actual_path.with_suffix(content_ext)
        total = 0
        head = bytearray()
        with actual_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > SOURCE_IMAGE_MAX_BYTES:
                    handle.close()
                    actual_path.unlink(missing_ok=True)
                    return {
                        "provider": "source_image",
                        "status": "skipped_too_large",
                        "url": url,
                        "path": str(actual_path),
                        "reason": f"Image exceeds {SOURCE_IMAGE_MAX_BYTES} bytes",
                    }
                if len(head) < 16:
                    head.extend(chunk[: 16 - len(head)])
                handle.write(chunk)
        if total == 0:
            actual_path.unlink(missing_ok=True)
            return {
                "provider": "source_image",
                "status": "skipped_empty",
                "url": url,
                "path": str(actual_path),
                "reason": "Downloaded image is empty",
            }
        if not _downloaded_image_signature_ok(bytes(head), content_type=content_type, path=actual_path):
            actual_path.unlink(missing_ok=True)
            return {
                "provider": "source_image",
                "status": "skipped_invalid_image",
                "url": url,
                "path": str(actual_path),
                "reason": "Downloaded content does not look like a supported image",
            }
        if webp_source:
            converted_path = output_path.with_suffix(".jpg")
            conversion = _convert_webp_to_jpeg(actual_path, converted_path)
            actual_path.unlink(missing_ok=True)
            if conversion.get("status") != "converted":
                return {
                    "provider": "source_image",
                    "status": "fallback_after_source_image_error",
                    "url": url,
                    "path": str(converted_path),
                    "reason": str(conversion.get("reason", "Could not convert WebP source image")),
                    "source_format": "webp",
                    "source_bytes": total,
                }
            return {
                "provider": "source_image",
                "status": "downloaded",
                "url": url,
                "path": str(converted_path),
                "bytes": int(conversion.get("bytes", 0)),
                "source_bytes": total,
                "alt": alt,
                "source_format": "webp",
                "converted_to": "jpeg",
            }
        return {
            "provider": "source_image",
            "status": "downloaded",
            "url": url,
            "path": str(actual_path),
            "bytes": total,
            "alt": alt,
        }
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        actual_path.unlink(missing_ok=True)
        return {
            "provider": "source_image",
            "status": "fallback_after_source_image_error",
            "url": url,
            "path": str(output_path),
            "reason": str(exc),
        }


def _convert_webp_to_jpeg(source_path: Path, output_path: Path) -> dict[str, Any]:
    try:
        from PIL import Image
    except Exception as exc:
        return {"status": "unavailable", "reason": f"Pillow is unavailable: {exc}"}

    try:
        with Image.open(source_path) as image:
            image.load()
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.getchannel("A"))
                rgb_image = canvas
            else:
                rgb_image = image.convert("RGB")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            rgb_image.save(output_path, format="JPEG", quality=88, optimize=True)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        return {"status": "failed", "reason": str(exc)}

    return {"status": "converted", "path": str(output_path), "bytes": output_path.stat().st_size}


def _source_image_extension(url: str) -> str:
    if not _is_source_image_url(url):
        return ""
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if suffix == ".webp":
        return ".jpg"
    return ".jpg"


def _source_image_extension_for_content_type(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(content_type, "")


def _downloaded_image_signature_ok(payload: bytes, *, content_type: str, path: Path) -> bool:
    if payload.startswith(b"\xff\xd8\xff"):
        return True
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return True
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return True
    # Tests and a few tiny CDN placeholders may be valid upload responses but too
    # small for a full signature; only allow that when the server declared a type.
    return bool(content_type and path.suffix.lower() in {".jpg", ".png", ".gif", ".webp"})


def _source_image_is_webp(url: str, content_type: str) -> bool:
    if content_type == "image/webp":
        return True
    return Path(urlparse(url).path).suffix.lower() == ".webp"


def _is_source_image_url(url: str) -> bool:
    value = url.strip()
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    suffix = Path(parsed.path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return True
    if suffix:
        return False
    lowered = value.lower()
    return "mmbiz.qpic.cn" in lowered or "/image/" in lowered or "/images/" in lowered


def _uses_weekly_briefing_cover(article: dict[str, Any]) -> bool:
    if str(article.get("package_role", "")).strip() != "primary":
        return False
    news_items = article.get("news_items", [])
    return bool(str(article.get("target_date", "")).strip()) and isinstance(news_items, list) and bool(news_items)


def _is_smart_manufacturing_daily(article: dict[str, Any]) -> bool:
    return str(article.get("source_policy", "")).strip() == "smart_manufacturing_daily"


def _uses_briefing_inline_images(article: dict[str, Any], *, role: str) -> bool:
    if role == "primary" and _uses_weekly_briefing_cover(article):
        return True
    if role == "secondary" and _is_smart_manufacturing_daily(article):
        news_items = article.get("news_items", [])
        return isinstance(news_items, list) and bool(news_items)
    return False


def _cover_image_prompt(article: dict[str, Any]) -> str:
    title = str(article.get("title", "")).strip()
    topic = str(article.get("topic", "AI 自动化")).strip()
    role = str(article.get("package_role_label", "文章")).strip()
    if _is_smart_manufacturing_daily(article):
        return "\n".join(
            [
                "Use case: editorial cover image for a Chinese WeChat Official Account smart manufacturing daily article",
                "Primary request: Create a polished, modern industrial technology cover for 智能制造日报.",
                f"Article title context: {title}",
                f"Topic: {topic}",
                "Style/medium: premium editorial illustration, clean industrial technology magazine look, trustworthy and concrete",
                "Composition/framing: landscape cover, smart factory floor, robotics, production lines, drones or satellites as subtle supporting elements",
                "Color palette: bright white and light gray base, industrial orange accent, restrained teal and steel blue details",
                "Constraints: no logos, no brand marks, no readable text, no fake UI text, no watermarks, no distorted Chinese characters",
                "Avoid: dark cyberpunk, scary robot faces, cluttered factory smoke, stock-photo people, single-color purple palette",
            ]
        )
    return "\n".join(
        [
            "Use case: editorial cover image for a Chinese WeChat Official Account article",
            f"Primary request: Create a polished, modern AI-themed cover for the {role}.",
            f"Article title context: {title}",
            f"Topic: {topic}",
            "Style/medium: premium digital illustration, editorial technology magazine style, clean and trustworthy",
            "Composition/framing: landscape cover, strong central visual, generous breathing room, readable at mobile thumbnail size",
            "Scene/backdrop: abstract AI workflow with documents, connected nodes, news cards, soft interface panels, and a subtle sense of daily briefing",
            "Color palette: bright but restrained; white, blue, teal, warm yellow accents; not dark, not monochrome",
            "Constraints: no logos, no brand marks, no watermarks, no readable text, no fake UI text, no distorted Chinese characters",
            "Avoid: clutter, tiny illegible text, scary robot faces, stock-photo cliches, heavy gradients",
        ]
    )


def _article_image_prompt(article: dict[str, Any]) -> str:
    topic = str(article.get("topic", "AI 自动化")).strip()
    role = str(article.get("package_role_label", "文章")).strip()
    if _is_smart_manufacturing_daily(article):
        return "\n".join(
            [
                "Use case: inline article illustration for a Chinese WeChat smart manufacturing daily post",
                f"Primary request: Create a clean supporting illustration for the {role}.",
                f"Topic: {topic}",
                "Style/medium: modern editorial industrial infographic illustration, polished but calm",
                "Composition/framing: landscape inline image, one clear smart factory workflow with robots, sensors, control panels, and logistics paths",
                "Scene/backdrop: robotic arms, automated production line, AI inspection nodes, low-altitude vehicle or satellite element in the background",
                "Color palette: fresh white background, warm orange accent, restrained teal/blue and soft gray structure",
                "Constraints: no readable text, no logos, no watermarks, no fake charts with labels, no distorted characters",
                "Avoid: dense dashboards, screenshots, dark cyberpunk mood, messy factory scenes, photorealistic workers",
            ]
        )
    return "\n".join(
        [
            "Use case: inline article illustration for a Chinese WeChat Official Account post",
            f"Primary request: Create a clean supporting illustration for the {role}.",
            f"Topic: {topic}",
            "Style/medium: modern editorial infographic-style illustration, polished but calm",
            "Composition/framing: landscape inline image, one clear focal workflow, enough detail to feel rich on mobile",
            "Scene/backdrop: AI assistant helping organize news, tasks, notes, and decision checkpoints; abstract interface panels and connected flow lines",
            "Color palette: fresh white background with blue, teal, green, and warm accent colors",
            "Constraints: no readable text, no logos, no watermarks, no fake charts with labels, no distorted characters",
            "Avoid: dense paragraphs, screenshots, dark cyberpunk mood, single-color purple palette",
        ]
    )


def _inline_image_specs(article: dict[str, Any], *, role: str) -> list[dict[str, str]]:
    if role == "tertiary" and str(article.get("source_policy", "")).strip() == "daily_practical_takeaway":
        return [
            {
                "placeholder": "{{ARTICLE_INLINE_TERTIARY_1}}",
                "alt": "四行任务模板的流程配图",
                "prompt": _toolbox_inline_image_prompt(article),
            }
        ]
    if not _uses_briefing_inline_images(article, role=role):
        return []
    source_specs = _source_inline_image_specs(article, role=role)
    if source_specs:
        return source_specs
    grouped_items = _briefing_visual_topics(article)
    specs = []
    for index, topic in enumerate(grouped_items[:2], start=1):
        specs.append(
            {
                "placeholder": f"{{{{ARTICLE_INLINE_{role.upper()}_{index}}}}}",
                "alt": f"{topic}的智能制造日报配图" if _is_smart_manufacturing_daily(article) else f"{topic}的科技早报配图",
                "prompt": _inline_image_prompt(article, topic=topic),
            }
        )
    return specs


def _source_inline_image_specs(article: dict[str, Any], *, role: str) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    news_items = article.get("news_items", [])
    if not isinstance(news_items, list):
        return specs
    for item_index, item in enumerate(news_items):
        if not isinstance(item, dict):
            continue
        image_url = _news_item_image_url(item)
        if not image_url or image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        spec_index = len(specs) + 1
        title = str(item.get("detail_heading") or item.get("title") or "新闻").strip()
        source = str(item.get("source_name") or "公开来源").strip()
        specs.append(
            {
                "placeholder": f"{{{{ARTICLE_INLINE_{role.upper()}_{spec_index}}}}}",
                "alt": f"{title}配图",
                "caption": f"来源配图：{source}",
                "source_url": image_url,
                "source_item_index": str(item_index),
                "source_name": source,
                "prompt": _inline_image_prompt(article, topic=title[:40] or "科技早报重点新闻"),
            }
        )
        if len(specs) >= MAX_INLINE_IMAGES_PER_ARTICLE:
            break
    return specs


def _news_item_image_url(item: dict[str, Any]) -> str:
    for key in ("image_url", "thumbnail_url", "cover_url", "image"):
        url = str(item.get(key, "")).strip()
        if _is_source_image_url(url):
            return url
    return ""


def _briefing_visual_topics(article: dict[str, Any]) -> list[str]:
    news_items = [item for item in article.get("news_items", []) if isinstance(item, dict)]
    combined = " ".join(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('source_name', '')}".lower()
        for item in news_items
    )
    topics = []
    if _is_smart_manufacturing_daily(article):
        if any(keyword in combined for keyword in ("robot", "robotics", "机器人", "人形", "具身", "自动化")):
            topics.append("机器人、具身智能和自动化产线")
        if any(keyword in combined for keyword in ("factory", "manufacturing", "industrial", "智能工厂", "制造", "工业")):
            topics.append("AI工厂、工业平台和制造数字化")
        if any(keyword in combined for keyword in ("evtol", "低空", "satellite", "卫星", "自动驾驶")):
            topics.append("低空经济、卫星和交通装备")
        if not topics:
            topics.append("智能制造产业日报重点新闻")
        return topics
    if any(keyword in combined for keyword in ("codex", "chatgpt", "agent", "workflow", "developer")):
        topics.append("AI 产品、智能体和工作流")
    if any(keyword in combined for keyword in ("lawsuit", "court", "biodefense", "safety", "policy")):
        topics.append("AI 治理、法律和安全边界")
    if any(keyword in combined for keyword in ("virtual power", "energy", "data center")):
        topics.append("算力、电力和数据中心基础设施")
    if not topics:
        topics.append("科技早报重点新闻")
    return topics


def _inline_image_prompt(article: dict[str, Any], *, topic: str) -> str:
    title = str(article.get("title", "")).strip()
    if _is_smart_manufacturing_daily(article):
        return "\n".join(
            [
                "Use case: inline section illustration for a Chinese WeChat smart manufacturing daily briefing",
                f"News cluster: {topic}",
                f"Article title context: {title}",
                "Style/medium: clean editorial industrial technology illustration, premium newsletter look, calm and concrete",
                "Composition/framing: landscape inline image, one clear visual metaphor with generous whitespace, mobile-friendly",
                "Scene/backdrop: smart factory production line, robotic arms, sensors, logistics route, industrial AI control nodes, subtle low-altitude or satellite element if relevant",
                "Color palette: white background, black and soft gray structure, orange accent matching the briefing, restrained teal/steel-blue details",
                "Constraints: no logos, no brand marks, no readable text, no fake UI text, no watermarks, no distorted Chinese characters",
                "Avoid: screenshots, real company logos, photorealistic people, dark cyberpunk mood, clutter, scary robots",
            ]
        )
    return "\n".join(
        [
            "Use case: inline section illustration for a Chinese WeChat technology morning briefing",
            f"News cluster: {topic}",
            f"Article title context: {title}",
            "Style/medium: clean editorial technology illustration, premium newsletter look, calm and trustworthy",
            "Composition/framing: landscape inline image, one clear visual metaphor, generous whitespace, mobile-friendly",
            "Scene/backdrop: abstract news cards, AI workflow nodes, documents, light interface panels, and subtle human decision checkpoints",
            "Color palette: white background, black and soft gray structure, orange accent matching a morning briefing, with restrained blue/teal details",
            "Constraints: no logos, no brand marks, no readable text, no fake UI text, no watermarks, no distorted Chinese characters",
            "Avoid: screenshots, real company logos, photorealistic people, dark cyberpunk mood, clutter, scary robots",
        ]
    )


def _toolbox_inline_image_prompt(article: dict[str, Any]) -> str:
    title = str(article.get("title", "")).strip()
    return "\n".join(
        [
            "Use case: inline illustration for a Chinese WeChat AI toolbox article",
            f"Article title context: {title}",
            "Core idea: a simple four-line task template: input, steps, boundaries, check standard",
            "Style/medium: clean editorial workflow illustration, practical and collectible, not promotional",
            "Composition/framing: landscape inline image, one clear task card connected to four checklist rows and a review checkpoint",
            "Scene/backdrop: papers, task cards, flow arrows, small assistant icon, human review mark; no readable words",
            "Color palette: white background, warm orange accent, fresh teal/green details, soft gray structure",
            "Constraints: no logos, no brand marks, no readable text, no fake UI text, no watermarks, no distorted Chinese characters",
            "Avoid: news cards, morning briefing mood, screenshots, dark cyberpunk mood, clutter, photorealistic people",
        ]
    )


def _image_provider_name(article: dict[str, Any]) -> str:
    generation = article.get("image_generation", {})
    if not isinstance(generation, dict):
        return ""
    providers = []
    for key in ("cover", "illustration"):
        value = generation.get(key, {})
        if isinstance(value, dict):
            providers.append(str(value.get("provider", "")).strip())
    inline_images = generation.get("inline_images", [])
    if isinstance(inline_images, list):
        for value in inline_images:
            if isinstance(value, dict):
                providers.append(str(value.get("provider", "")).strip())
    if providers and all(provider == "openai_image_api" for provider in providers):
        return "openai_image_api"
    if "openai_image_api" in providers:
        return "mixed"
    if providers and all(provider == "local_template" for provider in providers):
        return "local_template"
    if "local_template" in providers:
        return "mixed"
    if providers:
        return "local_fallback"
    return ""
