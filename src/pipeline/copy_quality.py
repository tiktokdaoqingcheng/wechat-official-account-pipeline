from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from src.pipeline.editorial_contract import (
    AI_COPY_PHRASES,
    EDITORIAL_CONTRACT_VERSION,
    GENERIC_EDITORIAL_PATTERNS,
    TECH_BRIEFING_CONTRACT,
    EditorialContract,
    contract_for_article,
    reader_summary_depth_report,
)

MODEL_REQUIRED_ROLES = ("summary", "batch", "polish")
READER_SUMMARY_MIN_CHARS = TECH_BRIEFING_CONTRACT.reader_summary_accept_min_chars
READER_SUMMARY_TWO_SENTENCE_MIN_CHARS = TECH_BRIEFING_CONTRACT.reader_summary_two_sentence_min_chars
READER_SUMMARY_MIN_SENTENCES = TECH_BRIEFING_CONTRACT.reader_summary_min_sentences
GENERIC_ENGLISH_SUBJECTS = {
    "birdlike",
    "could",
    "how",
    "researcher",
    "researchers",
    "teleoperated",
    "what",
    "why",
    "will",
    "would",
}
REMOVABLE_LOW_QUALITY_PHRASES = (
    "点击查看原文",
    "欢迎关注",
    "微信公众号",
    "微信号",
    "更多精彩内容",
)
STYLE_LOW_QUALITY_PHRASES = tuple(
    phrase for phrase in AI_COPY_PHRASES if phrase not in REMOVABLE_LOW_QUALITY_PHRASES
)
PROMOTIONAL_COPY_PATTERNS = (
    r"(?:补贴|优惠|折扣)活动(?:仍|还|正)?在持续",
    r"(?:符合条件的)?消费者.{0,30}(?:购买|下单).{0,30}(?:享受|获得).{0,16}(?:优惠|折扣|补贴)",
    r"(?:购买|下单).{0,24}(?:可|能)?(?:享受|获得).{0,16}(?:优惠|折扣|补贴)",
)
HARD_FINAL_REVIEW_TYPES = {
    "source_outside_fact",
    "numeric_drift",
    "off_topic",
    "promotion",
    "byline",
    "english_fragment",
    "truncation",
    "platform_risk",
}


def sanitize_article_copy(article: dict[str, Any]) -> dict[str, Any]:
    updated = dict(article)
    changes: list[dict[str, Any]] = []
    generated_reader_copy = _has_generated_reader_copy(updated)
    model_copy_degraded = _model_copy_degraded(updated)

    for field in ("title", "digest"):
        original = str(updated.get(field, ""))
        cleaned = _clean_removable_low_quality_text(original)
        if field == "title":
            cleaned = _clean_article_title(cleaned)
        if cleaned != original:
            updated[field] = cleaned
            changes.append({"field": field, "action": "sanitized", "to": cleaned[:120]})

    sections = []
    for section_index, section in enumerate(updated.get("sections", []) if isinstance(updated.get("sections"), list) else []):
        if not isinstance(section, dict):
            sections.append(section)
            continue
        section_copy = dict(section)
        paragraphs = []
        for paragraph_index, paragraph in enumerate(section.get("paragraphs", []) if isinstance(section.get("paragraphs"), list) else []):
            original = str(paragraph)
            cleaned = _clean_removable_low_quality_text(original)
            if cleaned != original:
                changes.append(
                    {
                        "field": f"sections[{section_index}].paragraphs[{paragraph_index}]",
                        "action": "sanitized",
                        "to": cleaned[:120],
                    }
                )
            if cleaned:
                paragraphs.append(cleaned)
        if paragraphs:
            section_copy["paragraphs"] = paragraphs
            sections.append(section_copy)
        else:
            changes.append({"field": f"sections[{section_index}]", "action": "removed_empty_section", "to": ""})
    if isinstance(updated.get("sections"), list):
        updated["sections"] = sections

    news_items = []
    removed_news_urls: set[str] = set()
    for item_index, item in enumerate(updated.get("news_items", []) if isinstance(updated.get("news_items"), list) else []):
        if not isinstance(item, dict):
            news_items.append(item)
            continue
        item_copy = dict(item)
        heading_repaired = False
        original_reader_summary = str(item_copy.get("reader_summary", ""))
        nested_source_attribution = _source_attribution_count(
            original_reader_summary,
            str(item_copy.get("source_name", "")),
        ) >= 2
        original_visible = "\n".join(
            str(item_copy.get(key, ""))
            for key in ("detail_heading", "reader_summary")
        )
        original_style_phrase = any(
            phrase in original_visible for phrase in STYLE_LOW_QUALITY_PHRASES
        ) or contains_generic_editorial_filler(original_visible)
        for key in ("title", "summary", "detail_heading", "reader_summary"):
            original = str(item_copy.get(key, ""))
            cleaned = _clean_removable_low_quality_text(original)
            if key in {"detail_heading", "reader_summary"} and _generic_mixed_english_heading(cleaned):
                cleaned = ""
            if key == "summary" and not cleaned:
                cleaned = _fallback_news_summary(item_copy)
            if cleaned != original:
                item_copy[key] = cleaned
                changes.append(
                    {
                        "field": f"news_items[{item_index}].{key}",
                        "action": "sanitized",
                        "to": cleaned[:120],
                    }
                )
        heading = str(item_copy.get("detail_heading", "")).strip()
        reader_summary = str(item_copy.get("reader_summary", "")).strip()
        heading_is_fragment = _heading_is_prefix_fragment(heading, reader_summary)
        generic_mixed_heading = _generic_mixed_english_heading(heading)
        if (
            not heading
            or (_mostly_ascii(heading) and not _publishable_mixed_heading(heading))
            or heading_is_fragment
            or generic_mixed_heading
        ):
            heading_repaired = True
            item_copy.pop("detail_heading", None)
            fallback_heading = _fallback_detail_heading_from_fact_card(item_copy)
            if heading_is_fragment:
                fallback_heading = fallback_heading or _fallback_detail_heading_from_reader_summary(item_copy)
                if not fallback_heading:
                    fallback_heading = _fallback_detail_heading_from_title(item_copy)
            else:
                fallback_heading = fallback_heading or _fallback_detail_heading_from_title(item_copy)
                if not fallback_heading:
                    fallback_heading = _fallback_detail_heading_from_reader_summary(item_copy)
            if fallback_heading:
                item_copy["detail_heading"] = fallback_heading
                changes.append(
                    {
                        "field": f"news_items[{item_index}].detail_heading",
                        "action": "filled_from_reader_summary",
                        "to": fallback_heading[:120],
                    }
                )
        heading = str(item_copy.get("detail_heading", "")).strip()
        reader_summary = str(item_copy.get("reader_summary", "")).strip()
        without_repeated_heading = _drop_repeated_opening_sentence(reader_summary, heading)
        deduplicated_summary = _deduplicate_reader_summary_body(without_repeated_heading)
        if (
            without_repeated_heading != reader_summary
            and reader_summary_has_publishable_depth(reader_summary)
            and not reader_summary_has_publishable_depth(without_repeated_heading)
        ):
            deduplicated_summary = _deduplicate_reader_summary_body(reader_summary)
        if deduplicated_summary != reader_summary:
            item_copy["reader_summary"] = deduplicated_summary
            changes.append(
                {
                    "field": f"news_items[{item_index}].reader_summary",
                    "action": "removed_repeated_heading_sentence",
                    "to": deduplicated_summary[:120],
                }
            )
        if not str(item_copy.get("reader_summary", "")).strip():
            fallback_summary = _fallback_reader_summary_from_raw_summary(item_copy)
            if not fallback_summary:
                fallback_summary = _fallback_reader_summary_from_fact_card(item_copy)
            if not fallback_summary:
                fallback_summary = _fallback_reader_summary_from_detail_heading(
                    item_copy,
                    article_source_policy=str(updated.get("source_policy", "")).strip(),
                )
            if fallback_summary:
                item_copy["reader_summary"] = fallback_summary
                changes.append(
                    {
                        "field": f"news_items[{item_index}].reader_summary",
                        "action": "filled_from_detail_heading",
                        "to": fallback_summary[:120],
                    }
                )
        source_echo = (
            nested_source_attribution or generated_reader_copy
        ) and _reader_summary_is_source_echo(item_copy, include_heading=not heading_repaired)
        drop_reason = (
            "fixed_ai_copy_phrase"
            if original_style_phrase
            else "source_or_title_echo"
            if source_echo
            else "unprocessed_model_fallback"
            if model_copy_degraded and _unprocessed_model_copy(item_copy)
            else _low_quality_news_item_reason(item_copy)
        )
        if drop_reason:
            removed_url = str(item_copy.get("url") or item_copy.get("source_url") or "").strip()
            if removed_url:
                removed_news_urls.add(removed_url)
            changes.append(
                {
                    "field": f"news_items[{item_index}]",
                    "action": "removed_low_quality_item",
                    "reason": drop_reason,
                    "to": str(item_copy.get("title", ""))[:120],
                }
            )
            continue
        news_items.append(item_copy)
    if isinstance(updated.get("news_items"), list):
        updated["news_items"] = news_items

    original_digest = str(updated.get("digest", ""))
    repaired_digest = _restore_digest_numeric_ranges(original_digest, news_items)
    if repaired_digest != original_digest:
        updated["digest"] = repaired_digest
        changes.append({"field": "digest", "action": "restored_numeric_range", "to": repaired_digest[:120]})

    sources = []
    for source_index, source in enumerate(updated.get("sources", []) if isinstance(updated.get("sources"), list) else []):
        if not isinstance(source, dict):
            sources.append(source)
            continue
        source_copy = dict(source)
        source_url = str(source_copy.get("url", "")).strip()
        if source_url and source_url in removed_news_urls:
            changes.append(
                {
                    "field": f"sources[{source_index}]",
                    "action": "removed_with_low_quality_item",
                    "to": str(source_copy.get("name", ""))[:120],
                }
            )
            continue
        for key in ("name", "summary"):
            original = str(source_copy.get(key, ""))
            cleaned = _clean_removable_low_quality_text(original)
            if key == "summary" and not cleaned:
                cleaned = _fallback_source_summary(source_copy)
            if cleaned != original:
                source_copy[key] = cleaned
                changes.append(
                    {
                        "field": f"sources[{source_index}].{key}",
                        "action": "sanitized",
                        "to": cleaned[:120],
                    }
                )
        sources.append(source_copy)
    if isinstance(updated.get("sources"), list):
        updated["sources"] = sources

    if changes:
        updated["copy_sanitization"] = {
            "status": "applied",
            "removed_low_quality_fragments": changes,
        }
        updated["word_count_estimate"] = sum(
            len(str(paragraph))
            for section in updated.get("sections", [])
            if isinstance(section, dict)
            for paragraph in section.get("paragraphs", [])
            if isinstance(paragraph, str)
        )
    return updated


def _low_quality_news_item_reason(item: dict[str, Any]) -> str:
    title = str(item.get("title", "")).strip()
    heading = str(item.get("detail_heading", "")).strip()
    reader_summary = str(item.get("reader_summary", "")).strip()
    visible = "\n".join((heading, reader_summary))
    if not title or len(title) < 5:
        return "missing_or_too_short_title"
    if _generic_mixed_english_heading(heading):
        return "untranslated_or_generic_mixed_heading"
    if any(phrase in visible for phrase in STYLE_LOW_QUALITY_PHRASES):
        return "fixed_ai_copy_phrase"
    if contains_generic_editorial_filler(visible):
        return "generic_editorial_filler"
    if _promotional_text("\n".join((title, str(item.get("summary", "")), visible))):
        return "promotional_or_subscription_text"
    validation = item.get("copy_generation_validation", {})
    if (
        isinstance(validation, dict)
        and validation.get("reader_summary") == "rejected"
        and _reader_summary_is_thin(item)
    ):
        return "model_reader_summary_rejected_without_publishable_fallback"
    if _reader_summary_is_tag_list(reader_summary):
        return "tag_style_reader_summary"
    fact_card = item.get("fact_card", {})
    if isinstance(fact_card, dict):
        facts = fact_card.get("facts", [])
        confidence = str(fact_card.get("confidence", ""))
        if confidence == "low" and not facts and not reader_summary:
            return "no_publishable_source_facts"
    return ""


def _promotional_text(text: str) -> bool:
    return contains_promotional_copy(text)


def contains_promotional_copy(text: str) -> bool:
    value = str(text or "")
    return (
        ("欢迎关注" in value and ("公众号" in value or "微信号" in value or "微信" in value))
        or ("更多精彩内容" in value and ("公众号" in value or "微信号" in value or "微信" in value))
        or "第一时间为您奉上" in value
        or any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in PROMOTIONAL_COPY_PATTERNS)
    )


def contains_generic_editorial_filler(text: str) -> bool:
    value = str(text or "")
    return any(re.search(pattern, value) for pattern in GENERIC_EDITORIAL_PATTERNS)


def reader_summary_has_publishable_depth(
    value: str,
    *,
    contract: EditorialContract | None = None,
) -> bool:
    return bool(reader_summary_depth_report(value, contract)["ok"])


def evaluate_article_copy_quality(
    article: dict[str, Any],
    *,
    text_model_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required_roles = _enabled_model_roles(text_model_context or {})
    checks = [
        _phrase_check(article),
        _repetition_check(article),
        _byline_leak_check(article),
        _article_title_quality_check(article),
        _generic_article_title_check(article),
        _final_ai_review_check(article),
        _final_review_recovery_check(article),
        _news_replenishment_check(article),
    ]
    if required_roles:
        checks.extend(
            [
                _reader_summary_depth_check(article),
                _ascii_title_check(article),
                _raw_summary_check(article),
                _model_health_check(article, required_roles),
            ]
        )
    else:
        checks.append(_skipped_model_health_check())
    failed = [check for check in checks if not check["ok"] and not check.get("diagnostic_only")]
    return {
        "ok": not failed,
        "required_text_model_roles": required_roles,
        "checks": checks,
        "failed_checks": failed,
        "model_health": next((check for check in checks if check["name"] == "text_model_health"), {}),
    }


def evaluate_package_copy_quality(
    articles: dict[str, dict[str, Any]],
    text_model_context: dict[str, Any],
) -> dict[str, Any]:
    article_results = {
        role: evaluate_article_copy_quality(article, text_model_context=text_model_context)
        for role, article in articles.items()
    }
    failed_roles = [role for role, result in article_results.items() if not result.get("ok")]
    enabled_roles = _enabled_model_roles(text_model_context)
    return {
        "ok": not failed_roles,
        "text_models_enabled": bool(enabled_roles),
        "required_text_model_roles": enabled_roles,
        "failed_roles": failed_roles,
        "articles": article_results,
    }


def apply_copy_quality_to_review(review: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    if quality.get("ok", True):
        return review
    updated = dict(review)
    checks = list(updated.get("checks", []))
    checks.append(
        {
            "name": "copy_quality",
            "ok": False,
            "severity": "medium",
            "details": quality.get("failed_checks", []),
        }
    )
    flags = dict(updated.get("flags", {}))
    flags["fact_uncertain"] = True
    updated["checks"] = checks
    updated["flags"] = flags
    updated["risk_level"] = "medium" if str(updated.get("risk_level")) == "low" else updated.get("risk_level", "medium")
    updated["wechat_ready"] = False
    notes = list(updated.get("notes", []))
    notes.append("文案质量 gate 未通过：模型状态、模板口癖或原始新闻摘要需要人工复核。")
    updated["notes"] = notes
    return updated


def _phrase_check(article: dict[str, Any]) -> dict[str, Any]:
    text = _article_text(article)
    hits = [phrase for phrase in AI_COPY_PHRASES if phrase in text]
    hits.extend(
        match.group(0)
        for pattern in GENERIC_EDITORIAL_PATTERNS
        if (match := re.search(pattern, text))
    )
    return {
        "name": "ai_copy_phrases",
        "ok": not hits,
        "hits": hits,
    }


def _byline_leak_check(article: dict[str, Any]) -> dict[str, Any]:
    hits = []
    for field in ("title", "digest"):
        value = str(article.get(field, "")).strip()
        if _contains_news_byline(value):
            hits.append({"field": field, "value": value[:120]})
    return {
        "name": "news_byline_leak",
        "ok": not hits,
        "hits": hits,
    }


def _article_title_quality_check(article: dict[str, Any]) -> dict[str, Any]:
    title = str(article.get("title", "")).strip()
    hits = []
    if title:
        if _contains_source_dateline(title):
            hits.append({"reason": "source_dateline", "value": title[:120]})
        if _contains_title_truncation(title):
            hits.append({"reason": "truncated_or_ellipsis_fragment", "value": title[:120]})
    return {
        "name": "article_title_quality",
        "ok": not hits,
        "hits": hits,
    }


def _generic_article_title_check(article: dict[str, Any]) -> dict[str, Any]:
    title = str(article.get("title", "")).strip()
    phrases = (
        "科技应用更新",
        "AI模型能力继续更新",
        "AI模型更新",
        "具体场景和持续交付能力更值得看",
    )
    hits = [phrase for phrase in phrases if phrase in title]
    return {
        "name": "generic_article_title",
        "ok": not hits,
        "hits": hits,
    }


def _repetition_check(article: dict[str, Any]) -> dict[str, Any]:
    sentences: dict[str, list[str]] = {}
    for field, value in _visible_copy_fields(article):
        for sentence in re.split(r"(?<=[。！？!?])\s*", value):
            normalized = re.sub(r"\s+", "", sentence).strip("。！？!?；;，,")
            if len(normalized) < 16:
                continue
            sentences.setdefault(normalized, []).append(field)
    hits = [
        {"text": sentence[:120], "fields": fields[:6]}
        for sentence, fields in sentences.items()
        if _repetition_fields_are_blocking(fields)
    ]
    return {
        "name": "repeated_visible_sentences",
        "ok": not hits,
        "hits": hits[:12],
    }


def _repetition_fields_are_blocking(fields: list[str]) -> bool:
    if any(fields.count(field) >= 2 for field in set(fields)):
        return True
    item_indexes = {
        match.group(1)
        for field in fields
        if (match := re.match(r"news_items\[(\d+)\]", field))
    }
    if len(item_indexes) >= 2:
        return True
    return not item_indexes and len(set(fields)) >= 2


def final_review_issue_blocks_publish(issue: Any) -> bool:
    if not isinstance(issue, dict):
        return False
    issue_type = str(issue.get("type", "")).strip().lower()
    severity = str(issue.get("severity", "")).strip().lower()
    action = str(issue.get("action", "keep")).strip().lower()
    if severity == "high" or action in {"drop_item", "block"}:
        return True
    return severity == "medium" and issue_type in HARD_FINAL_REVIEW_TYPES


def _final_ai_review_check(article: dict[str, Any]) -> dict[str, Any]:
    review = article.get("final_ai_review", {})
    if not isinstance(review, dict) or not review:
        return {
            "name": "final_ai_review",
            "ok": True,
            "diagnostic_only": True,
            "status": "not_configured",
        }
    mode = str(review.get("mode", "shadow"))
    status = str(review.get("status", "unknown"))
    issues = review.get("issues", []) if isinstance(review.get("issues", []), list) else []
    blocking_issues = [
        issue
        for issue in issues
        if final_review_issue_blocks_publish(issue)
    ]
    enforce = mode == "enforce"
    return {
        "name": "final_ai_review",
        "ok": not blocking_issues if enforce and status in {"applied", "degraded_applied"} else not enforce,
        "diagnostic_only": not enforce,
        "degraded": status == "degraded_applied",
        "mode": mode,
        "status": status,
        "risk_level": review.get("risk_level", "unknown"),
        "issues": issues,
        "blocking_issues": blocking_issues,
    }


def _final_review_recovery_check(article: dict[str, Any]) -> dict[str, Any]:
    recovery = article.get("final_review_recovery", {})
    if not isinstance(recovery, dict) or not recovery:
        return {
            "name": "final_review_recovery",
            "ok": True,
            "diagnostic_only": True,
            "status": "not_configured",
        }
    status = str(recovery.get("status", "unknown"))
    below_minimum = bool(recovery.get("below_minimum", False))
    budget_exhausted = bool(recovery.get("budget_exhausted", False))
    unresolved = recovery.get("unresolved_issues", [])
    unresolved = [issue for issue in unresolved if isinstance(issue, dict)] if isinstance(unresolved, list) else []
    blocking_unresolved = [issue for issue in unresolved if final_review_issue_blocks_publish(issue)]
    incomplete_after_budget = budget_exhausted and bool(blocking_unresolved or below_minimum)
    return {
        "name": "final_review_recovery",
        "ok": not below_minimum and not blocking_unresolved and not incomplete_after_budget,
        "status": status,
        "below_minimum": below_minimum,
        "budget_exhausted": budget_exhausted,
        "dropped_items": recovery.get("dropped_items", []),
        "unresolved_issues": unresolved,
        "blocking_unresolved_issues": blocking_unresolved,
    }


def _news_replenishment_check(article: dict[str, Any]) -> dict[str, Any]:
    replenishment = article.get("news_replenishment", {})
    if not isinstance(replenishment, dict) or not replenishment:
        return {
            "name": "news_replenishment",
            "ok": True,
            "diagnostic_only": True,
            "status": "not_configured",
        }
    below_minimum = bool(replenishment.get("below_minimum", False))
    return {
        "name": "news_replenishment",
        "ok": not below_minimum,
        "status": str(replenishment.get("status", "unknown")),
        "target_news_items": replenishment.get("target_news_items", 0),
        "minimum_news_items": replenishment.get("minimum_news_items", 0),
        "final_news_items": replenishment.get("final_news_items", 0),
        "below_minimum": below_minimum,
        "runs": replenishment.get("runs", []),
    }


def _ascii_title_check(article: dict[str, Any]) -> dict[str, Any]:
    hits = []
    for index, item in enumerate(article.get("news_items", []) if isinstance(article.get("news_items"), list) else [], start=1):
        title = str(item.get("title", "")).strip() if isinstance(item, dict) else ""
        heading = str(item.get("detail_heading", "")).strip() if isinstance(item, dict) else ""
        if _mostly_ascii(title) and not heading:
            hits.append({"index": index, "title": title[:120]})
    return {
        "name": "ascii_news_titles_without_model_heading",
        "ok": not hits,
        "hits": hits,
    }


def _reader_summary_depth_check(article: dict[str, Any]) -> dict[str, Any]:
    contract = contract_for_article(article)
    hits = []
    for index, item in enumerate(
        article.get("news_items", []) if isinstance(article.get("news_items"), list) else [],
        start=1,
    ):
        if not isinstance(item, dict):
            continue
        summary = str(item.get("reader_summary", "")).strip()
        report = reader_summary_depth_report(summary, contract)
        if report["ok"]:
            continue
        hits.append(
            {
                "index": index,
                "effective_chars": report["effective_chars"],
                "complete_sentences": report["complete_sentences"],
                "summary": summary[:160],
            }
        )
    return {
        "name": "reader_summary_publishable_depth",
        "ok": not hits,
        "editorial_contract_version": EDITORIAL_CONTRACT_VERSION,
        "minimum_effective_chars": contract.reader_summary_accept_min_chars,
        "two_sentence_minimum_effective_chars": contract.reader_summary_two_sentence_min_chars,
        "hits": hits,
    }


def _raw_summary_check(article: dict[str, Any]) -> dict[str, Any]:
    hits = []
    for index, item in enumerate(article.get("news_items", []) if isinstance(article.get("news_items"), list) else [], start=1):
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary", "")).strip()
        if _raw_summary_is_low_quality(summary) and not str(item.get("reader_summary", "")).strip():
            hits.append({"index": index, "summary": summary[:120]})
    return {
        "name": "raw_or_low_quality_summary_without_model_copy",
        "ok": not hits,
        "hits": hits,
    }


def _model_health_check(article: dict[str, Any], required_roles: list[str]) -> dict[str, Any]:
    generation = article.get("text_model_generation", {})
    roles = generation.get("roles", {}) if isinstance(generation, dict) else {}
    statuses = {
        role: str((roles.get(role) or {}).get("status", "")).strip()
        for role in required_roles
    }
    partial = {
        role: status
        for role, status in statuses.items()
        if status == "partial_fallback_after_text_model_error"
    }
    failed = {
        role: status
        for role, status in statuses.items()
        if status not in {"applied", "partial_fallback_after_text_model_error"}
    }
    return {
        "name": "text_model_health",
        "ok": not failed,
        "diagnostic_only": True,
        "statuses": statuses,
        "partial": partial,
        "failed": failed,
    }


def _skipped_model_health_check() -> dict[str, Any]:
    return {
        "name": "text_model_health",
        "ok": True,
        "diagnostic_only": True,
        "status": "skipped_text_models_disabled",
    }


def _enabled_model_roles(context: dict[str, Any]) -> list[str]:
    roles = context.get("roles", {})
    if not isinstance(roles, dict):
        return []
    enabled = []
    for role in MODEL_REQUIRED_ROLES:
        value = roles.get(role)
        if isinstance(value, dict) and bool(value.get("enabled")):
            enabled.append(role)
    return enabled


def _article_text(article: dict[str, Any]) -> str:
    chunks = [
        str(article.get("title", "")),
        str(article.get("digest", "")),
    ]
    for section in article.get("sections", []) if isinstance(article.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        chunks.append(str(section.get("heading", "")))
        chunks.extend(str(paragraph) for paragraph in section.get("paragraphs", []) if isinstance(paragraph, str))
    for item in article.get("news_items", []) if isinstance(article.get("news_items"), list) else []:
        if not isinstance(item, dict):
            continue
        for key in ("title", "summary", "detail_heading", "reader_summary", "source_name"):
            chunks.append(str(item.get(key, "")))
    return "\n".join(chunks)


def _visible_copy_fields(article: dict[str, Any]) -> list[tuple[str, str]]:
    fields = [
        ("title", str(article.get("title", ""))),
        ("digest", str(article.get("digest", ""))),
    ]
    news_items = article.get("news_items", []) if isinstance(article.get("news_items"), list) else []
    if not news_items:
        for section_index, section in enumerate(article.get("sections", []) if isinstance(article.get("sections"), list) else []):
            if not isinstance(section, dict):
                continue
            for paragraph_index, paragraph in enumerate(section.get("paragraphs", []) if isinstance(section.get("paragraphs"), list) else []):
                fields.append((f"sections[{section_index}].paragraphs[{paragraph_index}]", str(paragraph)))
    for item_index, item in enumerate(news_items):
        if not isinstance(item, dict):
            continue
        for key in ("detail_heading", "reader_summary"):
            fields.append((f"news_items[{item_index}].{key}", str(item.get(key, ""))))
    return fields


def _mostly_ascii(text: str) -> bool:
    value = text.strip()
    if len(value) < 12:
        return False
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return ascii_count / len(value) > 0.8


def _publishable_mixed_heading(text: str) -> bool:
    value = str(text).strip()
    chinese_count = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    action_words = ("发布", "推出", "开放", "进入", "接入", "支持", "升级", "完成", "成为", "合作", "开源")
    return chinese_count >= 2 and any(word in value for word in action_words)


def _generic_mixed_english_heading(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text)).strip()
    if not value:
        return False
    if re.match(
        r"^[A-Za-z][A-Za-z0-9._+-]{3,}(?:出现新进展|推进|进入应用)"
        r"(?:[A-Za-z0-9._+-]+)?(?:机器人能力|智能体能力|AI模型|AI产品能力)",
        value,
    ):
        return True
    match = re.match(
        r"^([A-Za-z][A-Za-z0-9._+-]{2,})(?:发布|推出|开源|升级)"
        r"(?:[A-Za-z0-9._+-]+)?(?:机器人能力|智能体能力|AI模型|AI产品能力|AI算力服务)",
        value,
    )
    return bool(match and match.group(1).lower() in GENERIC_ENGLISH_SUBJECTS)


def _raw_summary_is_low_quality(summary: str) -> bool:
    value = summary.strip()
    if not value:
        return True
    lowered = value.lower()
    if "点击查看原文" in value or "click" in lowered and "original" in lowered:
        return True
    if _contains_news_byline(value):
        return True
    if _mostly_ascii(value) and len(value) > 120:
        return True
    return False


def _clean_removable_low_quality_text(text: str) -> str:
    value = str(text).strip()
    if not value:
        return ""
    for _ in range(4):
        before = value
        cleaned = _normalize_product_spacing(value)
        cleaned = _strip_leading_source_dateline(cleaned)
        cleaned = re.sub(r"\s*[；;，,、。]?\s*点击查看原文\s*[>＞》）)]*\s*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*[；;，,、。]?\s*(?:click|tap)\s+(?:to\s+)?(?:read|view|see)\s+(?:the\s+)?original(?:\s+article)?\s*[>＞》）)]*\s*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*#?\s*欢迎关注.+$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*更多精彩内容.+$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*官方微信公众号.+$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*微信公众号[:：].+$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*微信号[:：].+$", "", cleaned, flags=re.IGNORECASE)
        cleaned = _remove_style_low_quality_sentences(cleaned)
        if cleaned != before:
            cleaned = re.sub(r"\s+", " ", cleaned).strip(" 　，,；;、")
            cleaned = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", cleaned)
            cleaned = re.sub(r"\s+([，。！？；：])", r"\1", cleaned)
            if _is_orphan_timestamp(cleaned):
                cleaned = ""
        if cleaned == value:
            break
        value = cleaned
    return value


def _normalize_product_spacing(text: str) -> str:
    value = str(text)
    replacements = (
        (r"ChatGPT\s*Work", "ChatGPT Work"),
        (r"Microsoft\s*365\s*Copilot", "Microsoft 365 Copilot"),
        (r"Data\s*for\s*Agents", "Data for Agents"),
        (r"Hugging\s*Face", "Hugging Face"),
        (r"Agent\s*Data\s*Kit", "Agent Data Kit"),
        (r"(?<![A-Za-z0-9])Toekn(?![A-Za-z0-9])", "Token"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value


def _strip_leading_source_dateline(text: str) -> str:
    value = str(text).strip()
    if not value:
        return ""
    source_prefix = (
        r"^(?!来自\s|公开来源)(?:据\s*)?"
        r"(?:[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9 .·_\-]{1,40})"
        r"\s*(?:\d{1,2}\s*月\s*\d{1,2}\s*日\s*)?"
        r"(?:消息|报道|讯)(?:显示|称|指出|介绍|披露|提到|了)?"
        r"\s*(?:[，,:：]\s*|[“\"']\s*)"
    )
    for _ in range(5):
        cleaned = re.sub(source_prefix, "", value, flags=re.IGNORECASE)
        cleaned = re.sub(r"^(?:北京时间\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日\s*[，,:：]\s*", "", cleaned)
        if cleaned == value:
            break
        value = cleaned.strip()
    return _strip_unbalanced_outer_quotes(value)


def _strip_unbalanced_outer_quotes(text: str) -> str:
    value = str(text).strip()
    for opening, closing in (("“", "”"), ("‘", "’")):
        depth = 0
        unmatched_closings: set[int] = set()
        for index, char in enumerate(value):
            if char == opening:
                depth += 1
            elif char == closing:
                if depth:
                    depth -= 1
                else:
                    unmatched_closings.add(index)
        if unmatched_closings:
            value = "".join(char for index, char in enumerate(value) if index not in unmatched_closings)
    return value


def _clean_article_title(text: str) -> str:
    value = str(text).strip()
    if not value:
        return ""
    parts = [part.strip() for part in re.split(r"[；;]", value) if part.strip()]
    if len(parts) <= 1:
        return value
    suffix = ""
    if "丨" in parts[-1]:
        last_prefix, last_suffix = parts[-1].rsplit("丨", 1)
        suffix = f"丨{last_suffix.strip()}"
        parts[-1] = last_prefix.strip()
    kept = [
        part
        for part in parts
        if part and not _contains_source_dateline(part) and not _contains_news_byline(part) and not _contains_title_truncation(part)
    ]
    if suffix and kept:
        return "；".join(kept[:5]).rstrip("；;丨 ") + suffix
    return value


def _remove_style_low_quality_sentences(text: str) -> str:
    value = text
    for phrase in STYLE_LOW_QUALITY_PHRASES:
        if phrase not in value:
            continue
        pattern = rf"[^。！？!?；;\n]*{re.escape(phrase)}[^。！？!?；;\n]*(?:[。！？!?；;]|$)"
        value = re.sub(pattern, "", value)
    return value


def _is_orphan_timestamp(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    iso_datetime = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+\d{2}:\d{2}|Z)?"
    date_only = r"\d{4}-\d{2}-\d{2}"
    return bool(re.fullmatch(rf"(?:{iso_datetime}|{date_only}|(?:\d{{1,2}}月\d{{1,2}}日))", value))


def _fallback_news_summary(item: dict[str, Any]) -> str:
    title = str(item.get("title", "")).strip()
    source = str(item.get("source_name", "")).strip()
    if source and title:
        return f"{source} 报道了“{title}”。"
    if title:
        return f"公开来源报道了“{title}”。"
    if source:
        return f"{source} 的公开报道。"
    return "公开来源报道。"


def _fallback_detail_heading_from_reader_summary(item: dict[str, Any]) -> str:
    summary = str(item.get("reader_summary", "")).strip()
    if not summary or _mostly_ascii(summary):
        return ""
    sentence = re.split(r"[。！？!?；;\n]", summary, maxsplit=1)[0].strip()
    sentence = _strip_leading_source_dateline(sentence)
    sentence = re.sub(r"^(据[^，,。]{1,16}[，,]\s*)", "", sentence)
    sentence = re.sub(r"^(这[一条则]?消息|该消息|这篇报道|该报道)[，,：:]\s*", "", sentence)
    sentence = re.sub(r"\s+", "", sentence).strip(" 　，,。；;：:")
    if len(sentence) < 10:
        return ""
    sentence = _complete_heading_candidate(sentence, max_length=72)
    if len(sentence) < 10 or _mostly_ascii(sentence):
        return ""
    if any(phrase in sentence for phrase in AI_COPY_PHRASES):
        return ""
    if _contains_news_byline(sentence):
        return ""
    return sentence


def _fallback_detail_heading_from_title(item: dict[str, Any]) -> str:
    title = _strip_leading_source_dateline(str(item.get("title", "")).strip())
    title = re.sub(r"^(?:刚刚|消息称|报道称)\s*[，,:：]?\s*", "", title)
    title = re.sub(r"\s+", " ", title).strip(" 　，,。；;")
    if not title or _mostly_ascii(title) or _contains_news_byline(title):
        return ""
    if sum(1 for char in title if "\u4e00" <= char <= "\u9fff") < 6:
        return ""
    return _complete_heading_candidate(title, max_length=72)


def _fallback_detail_heading_from_fact_card(item: dict[str, Any]) -> str:
    for candidate in _fact_card_text_candidates(item):
        value = _strip_leading_source_dateline(candidate)
        value = re.sub(r"\s+", " ", value).strip(" 　，,。；;")
        if len(value) < 10 or _mostly_ascii(value) or _contains_news_byline(value):
            continue
        if any(phrase in value for phrase in AI_COPY_PHRASES):
            continue
        heading = _complete_heading_candidate(value, max_length=72)
        if len(heading) >= 10 and not _generic_mixed_english_heading(heading):
            return heading
    return ""


def _complete_heading_candidate(text: str, *, max_length: int) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip(" 　，,。；;")
    if len(value) <= max_length:
        return value
    minimum_cut = max(12, int(max_length * 0.55))
    for separator in ("。", "！", "？", "；", ";", "，", ",", "：", ":"):
        cut = value.rfind(separator, minimum_cut, max_length + 1)
        if cut >= minimum_cut:
            return value[:cut].strip(" 　，,。；;：:")
    return ""


def _heading_is_prefix_fragment(heading: str, reader_summary: str) -> bool:
    if not heading or not reader_summary:
        return False
    first_sentence = re.split(r"[。！？!?]", reader_summary, maxsplit=1)[0]
    normalized_heading = _normalize_visible_sentence(heading)
    normalized_first = _normalize_visible_sentence(first_sentence)
    if (
        len(normalized_heading) >= 16
        and len(normalized_first) >= len(normalized_heading) + 4
        and normalized_first.startswith(normalized_heading)
    ):
        return True
    similarity = SequenceMatcher(None, normalized_heading, normalized_first).ratio()
    return (
        len(normalized_heading) >= 20
        and len(normalized_first) >= len(normalized_heading) + 4
        and similarity >= 0.78
    )


def deduplicate_reader_summary(reader_summary: str, heading: str) -> str:
    value = _drop_repeated_opening_sentence(reader_summary, heading)
    return _deduplicate_reader_summary_body(value)


def _deduplicate_reader_summary_body(value: str) -> str:
    value = _drop_adjacent_duplicate_sentences(value)
    for _ in range(4):
        merged = _merge_repeated_latin_subject(value)
        if merged == value:
            break
        value = merged
    return value


def _drop_repeated_opening_sentence(reader_summary: str, heading: str) -> str:
    value = str(reader_summary).strip()
    if not value or not heading:
        return value
    match = re.match(r"^(.+?[。！？!?])\s*(.*)$", value, flags=re.DOTALL)
    if match:
        first_sentence, remainder = match.group(1), match.group(2).strip()
    else:
        first_sentence, remainder = value, ""
    if not remainder or not _sentences_substantially_overlap(first_sentence, heading):
        return value
    return remainder


def _sentences_substantially_overlap(left: str, right: str) -> bool:
    normalized_left = _normalize_visible_sentence(left)
    normalized_right = _normalize_visible_sentence(right)
    shorter = min(len(normalized_left), len(normalized_right))
    if shorter < 12:
        return normalized_left == normalized_right
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return True
    return SequenceMatcher(None, normalized_left, normalized_right).ratio() >= 0.82


def _drop_adjacent_duplicate_sentences(text: str) -> str:
    sentences = [
        match.group(0).strip()
        for match in re.finditer(r"[^。！？!?]+(?:[。！？!?]|$)", str(text))
        if match.group(0).strip()
    ]
    if len(sentences) < 2:
        return str(text).strip()
    kept: list[str] = []
    for sentence in sentences:
        if kept and _sentences_substantially_overlap(kept[-1], sentence):
            continue
        kept.append(sentence)
    return "".join(kept)


def _merge_repeated_latin_subject(text: str) -> str:
    value = str(text).strip()
    match = re.match(
        r"^([A-Za-z][A-Za-z0-9._+\-]{2,30})([^。！？!?]*[。！？!?])\s*\1([^。！？!?]*[。！？!?])(.*)$",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return value
    subject, first_tail, second_tail, remainder = match.groups()
    first_clause = first_tail.rstrip("。！？!?")
    second_clause = second_tail.lstrip("，,；;：: ")
    return f"{subject}{first_clause}；{second_clause}{remainder}".strip()


def _normalize_visible_sentence(text: str) -> str:
    return re.sub(r"[\s。！？!?；;，,：:'\"“”‘’]", "", str(text)).lower()


def _reader_summary_is_source_echo(item: dict[str, Any], *, include_heading: bool = True) -> bool:
    summary = str(item.get("reader_summary", "")).strip()
    if not summary:
        return False
    normalized_summary = _normalize_visible_sentence(_strip_unbalanced_outer_quotes(summary.strip("“”'\"")))
    candidates = [str(item.get("title", ""))]
    if include_heading:
        candidates.append(str(item.get("detail_heading", "")))
    return any(
        len(normalized) >= 10 and normalized_summary == normalized
        for candidate in candidates
        if (normalized := _normalize_visible_sentence(candidate))
    )


def _has_generated_reader_copy(article: dict[str, Any]) -> bool:
    generation = article.get("text_model_generation", {})
    roles = generation.get("roles", {}) if isinstance(generation, dict) else {}
    if not isinstance(roles, dict):
        return False
    return any(
        isinstance(roles.get(role), dict)
        and roles[role].get("status") in {"applied", "partial_fallback_after_text_model_error"}
        for role in ("batch", "polish")
    )


def _model_copy_degraded(article: dict[str, Any]) -> bool:
    generation = article.get("text_model_generation", {})
    roles = generation.get("roles", {}) if isinstance(generation, dict) else {}
    batch = roles.get("batch", {}) if isinstance(roles, dict) else {}
    if not isinstance(batch, dict):
        return False
    status = str(batch.get("status", "")).strip()
    return bool(status and status not in {"applied", "partial_fallback_after_text_model_error"})


def _unprocessed_model_copy(item: dict[str, Any]) -> bool:
    heading = str(item.get("detail_heading", "")).strip()
    reader_summary = str(item.get("reader_summary", "")).strip()
    validation = item.get("copy_generation_validation", {})
    return (
        not heading
        or not reader_summary
        or _mostly_ascii(heading)
        or _mostly_ascii(reader_summary)
        or _reader_summary_is_thin(item)
        or (
            isinstance(validation, dict)
            and validation.get("reader_summary") == "rejected"
        )
    )


def _source_attribution_count(text: str, source_name: str) -> int:
    value = re.sub(r"\s+", "", str(text)).lower()
    source = re.sub(r"\s+", "", str(source_name)).lower()
    if not value or not source:
        return 0
    return len(re.findall(rf"{re.escape(source)}(?:消息|报道|讯)", value))


def _reader_summary_is_thin(item: dict[str, Any]) -> bool:
    value = str(item.get("reader_summary", "")).strip()
    if not value:
        return True
    if _reader_summary_is_source_echo(item):
        return True
    if _reader_summary_is_tag_list(value):
        return True
    if not reader_summary_has_publishable_depth(value):
        return True
    complete_sentences = re.findall(r"[^。！？!?]+[。！？!?]", value)
    if len(complete_sentences) <= 1 and re.fullmatch(r"[“\"].+[”\"]。?", value):
        return True
    return False


def _reader_summary_is_tag_list(value: str) -> bool:
    parts = [part.strip(" 　。！？!?；;，,") for part in value.split("、") if part.strip()]
    return len(parts) >= 5 and max((len(part) for part in parts), default=0) <= 18


def _fallback_reader_summary_from_raw_summary(item: dict[str, Any]) -> str:
    summary = _clean_removable_low_quality_text(str(item.get("summary", "")))
    summary = _strip_leading_source_dateline(summary)
    if not summary or _mostly_ascii(summary):
        return ""
    sentences = [match.group(0).strip() for match in re.finditer(r"[^。！？!?]+[。！？!?]", summary)]
    selected: list[str] = []
    for sentence in sentences:
        if len(sentence) > 220 and not selected:
            return ""
        if sum(len(value) for value in selected) + len(sentence) > 220:
            break
        selected.append(sentence)
        candidate = _drop_repeated_opening_sentence(
            "".join(selected),
            str(item.get("detail_heading", "")),
        )
        if reader_summary_has_publishable_depth(candidate):
            break
    result = "".join(selected)
    result = _drop_repeated_opening_sentence(result, str(item.get("detail_heading", "")))
    if len(result) < 28 or any(phrase in result for phrase in AI_COPY_PHRASES):
        return ""
    return result


def _restore_digest_numeric_ranges(digest: str, news_items: list[Any]) -> str:
    value = str(digest).strip()
    if not value:
        return value
    source_text = " ".join(
        str(item.get(key, ""))
        for item in news_items
        if isinstance(item, dict)
        for key in ("title", "summary", "detail_heading", "reader_summary")
    )
    range_pattern = re.compile(
        r"(?<!\d)(\d[\d,.]*)\s*[-~—–至]\s*(\d[\d,.]*)\s*"
        r"(亿元|万元|万美元|美元|人民币|元|%|台|家|个|人|名|套|架|辆|周|月|年)"
    )
    for match in range_pattern.finditer(source_text):
        lower, upper, unit = match.groups()
        try:
            if float(lower.replace(",", "")) >= float(upper.replace(",", "")):
                continue
        except ValueError:
            continue
        compact_range = f"{lower}-{upper}{unit}"
        if compact_range in re.sub(r"\s+", "", value):
            continue
        upper_pattern = re.compile(rf"(?<![\d\-~—–至]){re.escape(upper)}\s*{re.escape(unit)}")
        if upper_pattern.search(value):
            value = upper_pattern.sub(compact_range, value, count=1)
    return value


def _fallback_reader_summary_from_detail_heading(
    item: dict[str, Any],
    *,
    article_source_policy: str,
) -> str:
    heading = str(item.get("detail_heading", "")).strip()
    if not heading or _mostly_ascii(heading) or len(heading) < 10:
        return ""
    if any(phrase in heading for phrase in AI_COPY_PHRASES):
        return ""
    if _contains_news_byline(heading):
        return ""
    source = str(item.get("source_name", "")).strip() or "公开来源"
    return f"{source}报道“{heading}”。"


def _fallback_reader_summary_from_fact_card(item: dict[str, Any]) -> str:
    heading = str(item.get("detail_heading", "")).strip()
    selected: list[str] = []
    for candidate in _fact_card_text_candidates(item):
        value = _clean_removable_low_quality_text(candidate)
        value = _strip_leading_source_dateline(value)
        value = re.sub(r"\s+", " ", value).strip(" 　，,。；;")
        if len(value) < 12 or _mostly_ascii(value) or _contains_news_byline(value):
            continue
        if any(phrase in value for phrase in AI_COPY_PHRASES):
            continue
        if _normalize_visible_sentence(value) == _normalize_visible_sentence(heading):
            continue
        sentence = value if value.endswith(("。", "！", "？")) else f"{value}。"
        if len("".join(selected)) + len(sentence) > 220:
            break
        if sentence not in selected:
            selected.append(sentence)
        if reader_summary_has_publishable_depth("".join(selected)):
            break
    return "".join(selected)


def _fact_card_text_candidates(item: dict[str, Any]) -> list[str]:
    card = item.get("fact_card", {})
    if not isinstance(card, dict):
        return []
    result: list[str] = []
    facts = card.get("facts", [])
    if isinstance(facts, list):
        result.extend(str(value).strip() for value in facts if isinstance(value, str) and value.strip())
    action = str(card.get("action", "")).strip()
    subject = str(card.get("subject", "")).strip()
    if action:
        result.append(action)
    if subject and action and subject not in action:
        result.append(f"{subject}{action}")
    return result


def _looks_like_manufacturing_news(combined: str) -> bool:
    keywords = (
        "manufacturing",
        "industrial",
        "factory",
        "robot",
        "robotics",
        "production",
        "evtol",
        "electric vehicle",
        "smart driving",
        "智能制造",
        "工业",
        "制造",
        "工厂",
        "产线",
        "机器人",
        "具身",
        "自动化",
        "低空",
        "智能车",
        "电动车",
    )
    return any(keyword in combined for keyword in keywords)


def _fallback_source_summary(source: dict[str, Any]) -> str:
    name = str(source.get("name", "")).strip()
    if name:
        return f"{name} 的公开报道。"
    return "公开来源报道。"


def _contains_source_dateline(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日\s*(?:消息|讯|报道)", value):
        return True
    if re.search(r"^[^，,；;。]{2,24}\s+\d{1,2}\s*月\s*\d{1,2}\s*日(?:\s|[，,:：；;]|$)", value):
        return True
    return False


def _contains_title_truncation(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    return "…" in value or "..." in value


def _contains_news_byline(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    role_pattern = r"(^|[\s；;，,、。])(?:文|作者|记者|编辑)\s*[|丨｜:：]"
    if re.search(role_pattern, value):
        return True
    return bool(re.search(r"记者\s*[|丨｜:：]?.{0,30}编辑\s*[|丨｜:：]", value))
