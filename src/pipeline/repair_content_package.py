from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_layered_yaml, load_yaml
from src.pipeline.content_engine import build_article, render_markdown, render_wechat_html, validate_article
from src.pipeline.content_package import _dedupe_news_items, aggregate_package_review
from src.pipeline.copy_quality import apply_copy_quality_to_review, evaluate_package_copy_quality, sanitize_article_copy
from src.pipeline.news_seed import blocked_by_wechat_platform_risk
from src.pipeline.package_manifest import freeze_content_package
from src.pipeline.text_model_enhancer import (
    prepare_text_model_context,
    review_final_article_with_text_model,
    text_model_package_status,
)
from src.review.publish_policy import decide_publish_action
from src.review.review_engine import review_article, validate_review
from src.runtime_guard import config_fingerprint


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_ARTICLE_ROLES = ("primary", "secondary")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a repaired content package from an existing package.")
    parser.add_argument("--package", required=True, help="Path to the existing content-package.json.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--safety", default="config/safety-rules.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--already-published-today", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = repair_content_package(
        package_path=args.package,
        output_dir=args.output_dir,
        schedule_path=args.schedule,
        safety_path=args.safety,
        env_path=args.env,
        already_published_today=args.already_published_today,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status: {result['status']}")
        print(f"Output: {result['output_dir']}")
        print(f"Removed items: {len(result.get('removed_items', []))}")
        print(f"Publish action: {result.get('publish_decision', {}).get('action')}")
    return 0 if result["status"] == "ok" else 1


def repair_content_package(
    *,
    package_path: str | Path,
    output_dir: str | Path,
    schedule_path: str | Path = "config/schedule.yaml",
    safety_path: str | Path = "config/safety-rules.yaml",
    env_path: str | Path = ".env",
    already_published_today: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    original_package_path = Path(package_path)
    original_dir = original_package_path.parent
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    original_package = json.loads(original_package_path.read_text(encoding="utf-8-sig"))
    schedule_config = load_layered_yaml(schedule_path)
    safety_config = load_yaml(safety_path)
    text_model_context = prepare_text_model_context(env_path)
    roles = _package_roles(original_package)

    articles: dict[str, dict[str, Any]] = {}
    reviews: dict[str, dict[str, Any]] = {}
    files: dict[str, Any] = {}
    removed_items: list[dict[str, str]] = []

    for role in roles:
        role_dir = output_path / role
        role_dir.mkdir(parents=True, exist_ok=True)
        source_article_path = Path(str(original_package["files"][role]["article_json"]))
        article = json.loads(source_article_path.read_text(encoding="utf-8-sig"))
        seed = _seed_from_article(article)
        removed_items.extend(_remove_wechat_platform_risk_items(seed, role=role))

        repaired = build_article(seed)
        repaired = _carry_runtime_fields(repaired, article)
        repaired = sanitize_article_copy(repaired)
        final_review_config = (
            schedule_config.get("final_review", {})
            if isinstance(schedule_config.get("final_review", {}), dict)
            else {}
        )
        repaired = review_final_article_with_text_model(
            repaired,
            text_model_context,
            visible_html=render_wechat_html(repaired),
            mode=str(final_review_config.get("mode", "shadow")),
        )
        errors = validate_article(repaired)
        if errors:
            raise ValueError(f"Invalid repaired {role} article: " + "; ".join(errors))

        review = review_article(repaired, safety_config)
        review_errors = validate_review(review)
        if review_errors:
            raise ValueError(f"Invalid repaired {role} review: " + "; ".join(review_errors))

        role_files = _copy_role_assets(original_package, role=role, output_dir=role_dir)
        role_files["article_json"].write_text(json.dumps(repaired, ensure_ascii=False, indent=2), encoding="utf-8")
        role_files["article_md"].write_text(render_markdown(repaired), encoding="utf-8")
        role_files["article_html"].write_text(render_wechat_html(repaired), encoding="utf-8")
        role_files["review_json"].write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")

        articles[role] = repaired
        reviews[role] = review
        files[role] = _role_files_for_package(role_files)

    copy_quality = evaluate_package_copy_quality(articles, text_model_context)
    for role in roles:
        role_quality = copy_quality.get("articles", {}).get(role, {})
        if isinstance(role_quality, dict):
            reviews[role] = apply_copy_quality_to_review(reviews[role], role_quality)
            (output_path / role / "review.json").write_text(
                json.dumps(reviews[role], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    package_review = aggregate_package_review(reviews)
    publish_decision = decide_publish_action(
        schedule_config,
        package_review,
        already_published_today=already_published_today,
    )
    package = {
        **original_package,
        "created_at": current.isoformat(),
        "article_count": len(roles),
        "article_roles": list(roles),
        "primary_article": _article_summary(articles["primary"], reviews["primary"]) if "primary" in roles else {},
        "secondary_article": _article_summary(articles["secondary"], reviews["secondary"]) if "secondary" in roles else {},
        "package_risk_level": package_review["risk_level"],
        "wechat_ready": bool(package_review["wechat_ready"]),
        "already_published_today": already_published_today,
        "publish_decision": asdict(publish_decision),
        "repair": {
            "source_package": str(original_package_path),
            "created_at": current.isoformat(),
            "removed_items": removed_items,
        },
        "text_model_generation": text_model_package_status(text_model_context, articles),
        "copy_quality": copy_quality,
        "files": files,
    }
    package_path_out = output_path / "content-package.json"
    files["content_package_json"] = str(package_path_out)
    package["files"] = files
    package_path_out.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_path / "review.json").write_text(json.dumps(package_review, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_path / "publish-decision.json").write_text(
        json.dumps(asdict(publish_decision), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    runtime_config = schedule_config.get("runtime", {}) if isinstance(schedule_config.get("runtime", {}), dict) else {}
    manifest = freeze_content_package(
        package_path_out,
        config_fingerprint=config_fingerprint(schedule_config),
        config_version=str(runtime_config.get("config_version", "")),
        now=current,
    )
    (output_path / "repair-result.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "created_at": current.isoformat(),
                "source_package": str(original_package_path),
                "output_dir": str(output_path),
                "removed_items": removed_items,
                "package_risk_level": package["package_risk_level"],
                "wechat_ready": package["wechat_ready"],
                "publish_decision": package["publish_decision"],
                "artifact_manifest": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "output_dir": str(output_path),
        "removed_items": removed_items,
        "package_risk_level": package["package_risk_level"],
        "wechat_ready": package["wechat_ready"],
        "publish_decision": package["publish_decision"],
        "artifact_manifest": manifest,
    }


def _package_roles(package: dict[str, Any]) -> tuple[str, ...]:
    roles = package.get("article_roles")
    if isinstance(roles, list):
        normalized = tuple(role for role in MAX_ARTICLE_ROLES if role in {str(item) for item in roles})
        if normalized:
            return normalized
    count = int(package.get("article_count", 2) or 2)
    return MAX_ARTICLE_ROLES[: min(max(count, 1), len(MAX_ARTICLE_ROLES))]


def _seed_from_article(article: dict[str, Any]) -> dict[str, Any]:
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
        "news_status",
        "wechat_platform_risk_replacements",
    )
    seed = {key: article[key] for key in keys if key in article}
    news_items = seed.get("news_items", [])
    if isinstance(news_items, list):
        deduped = _dedupe_news_items([item for item in news_items if isinstance(item, dict)])
        if len(deduped) != len(news_items):
            seed["news_items"] = deduped
            seed["sources"] = [_source_from_news_item(item) for item in deduped]
    return seed


def _remove_wechat_platform_risk_items(seed: dict[str, Any], *, role: str) -> list[dict[str, str]]:
    news_items = seed.get("news_items", [])
    if not isinstance(news_items, list) or not news_items:
        return []

    safe_items = []
    removed_items = []
    for item in news_items:
        if isinstance(item, dict) and blocked_by_wechat_platform_risk(item):
            removed_items.append(
                {
                    "role": role,
                    "title": str(item.get("title") or item.get("detail_heading") or "").strip(),
                    "source_name": str(item.get("source_name", "")).strip(),
                    "reason": "wechat_platform_sensitive_combo",
                }
            )
            continue
        safe_items.append(item)

    if not removed_items:
        return []

    seed["news_items"] = safe_items
    seed["sources"] = [_source_from_news_item(item) for item in safe_items if isinstance(item, dict)]
    seed["wechat_platform_risk_replacements"] = removed_items
    seed.pop("title", None)
    seed.pop("digest", None)
    if not safe_items:
        seed["news_status"] = "blocked_by_wechat_platform_risk"
    return removed_items


def _carry_runtime_fields(repaired: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "package_role",
        "package_role_label",
        "illustration_prompt",
        "illustration_alt",
        "illustration_placeholder",
        "inline_images",
        "image_generation",
        "text_model_generation",
    ):
        if key in original:
            repaired[key] = original[key]
    _refresh_inline_image_text(repaired)
    return repaired


def _copy_role_assets(package: dict[str, Any], *, role: str, output_dir: Path) -> dict[str, Any]:
    source_files = package["files"][role]
    copied = {
        "article_json": output_dir / "article.json",
        "article_md": output_dir / "article.md",
        "article_html": output_dir / "article.html",
        "review_json": output_dir / "review.json",
        "cover": output_dir / "cover.png",
        "illustration": output_dir / "illustration.png",
        "inline_images": [],
    }
    for key in ("cover", "illustration"):
        source = Path(str(source_files.get(key, "")))
        if source.exists():
            shutil.copy2(source, copied[key])
    for index, source_value in enumerate(source_files.get("inline_images", []) or [], start=1):
        source = Path(str(source_value))
        if not source.exists():
            continue
        target = output_dir / f"inline-{index}{source.suffix or '.png'}"
        shutil.copy2(source, target)
        copied["inline_images"].append(target)
    return copied


def _role_files_for_package(role_files: dict[str, Any]) -> dict[str, Any]:
    return {
        "article_json": str(role_files["article_json"]),
        "article_md": str(role_files["article_md"]),
        "article_html": str(role_files["article_html"]),
        "review_json": str(role_files["review_json"]),
        "cover": str(role_files["cover"]),
        "illustration": str(role_files["illustration"]),
        "inline_images": [str(path) for path in role_files.get("inline_images", [])],
    }


def _article_summary(article: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": str(article.get("package_role", "")).strip(),
        "title": str(article.get("title", "")).strip(),
        "topic": str(article.get("topic", "")).strip(),
        "column": str(article.get("column", "")).strip(),
        "risk_level": str(review.get("risk_level", "")).strip(),
        "wechat_ready": bool(review.get("wechat_ready")),
    }


def _refresh_inline_image_text(article: dict[str, Any]) -> None:
    inline_images = article.get("inline_images", [])
    news_items = article.get("news_items", [])
    if not isinstance(inline_images, list) or not isinstance(news_items, list):
        return
    refreshed_images = []
    for index, image in enumerate(inline_images):
        if not isinstance(image, dict):
            continue
        try:
            source_index = int(str(image.get("source_item_index", "")).strip())
        except (TypeError, ValueError):
            source_index = index
        if source_index < 0 or source_index >= len(news_items):
            continue
        item = news_items[source_index]
        if not isinstance(item, dict):
            continue
        title = str(item.get("detail_heading") or item.get("title") or "智能制造日报").strip()
        image["alt"] = f"{title}配图"
        if image.get("caption"):
            source = str(item.get("source_name") or "公开来源").strip()
            image["caption"] = f"来源配图：{source}"
        prompt = str(image.get("prompt", ""))
        if prompt:
            lines = []
            for line in prompt.splitlines():
                if line.startswith("News cluster:"):
                    lines.append(f"News cluster: {title}")
                elif line.startswith("Article title context:"):
                    lines.append(f"Article title context: {article.get('title', '')}")
                else:
                    lines.append(line)
            image["prompt"] = "\n".join(lines)
        image["source_item_index"] = source_index
        refreshed_images.append(image)
    article["inline_images"] = refreshed_images


def _source_from_news_item(item: dict[str, Any]) -> dict[str, str]:
    title = str(item.get("title", "")).strip()
    source_name = str(item.get("source_name", "")).strip()
    published_at = str(item.get("published_at", "")).strip()
    summary = str(item.get("summary", "")).strip()
    return {
        "name": f"{source_name}：{title}" if source_name else title,
        "url": str(item.get("url", "")).strip(),
        "summary": f"{published_at}；{summary}" if published_at else summary,
    }


if __name__ == "__main__":
    raise SystemExit(main())
