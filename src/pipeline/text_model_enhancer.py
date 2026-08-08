from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.integrations.text_model_client import TextModelClient, TextModelError, load_text_model_configs
from src.runtime_budget import ExecutionBudget
from src.pipeline.candidate_preflight import apply_candidate_verdict, drop_rejected_candidates
from src.pipeline.copy_quality import (
    contains_generic_editorial_filler,
    contains_promotional_copy,
    deduplicate_reader_summary,
    reader_summary_has_publishable_depth,
)
from src.pipeline.editorial_contract import (
    EDITORIAL_CONTRACT_VERSION,
    MODEL_BANNED_PHRASES,
    EditorialContract,
    contract_for_article,
    contract_metadata,
    fact_card_readiness_report,
    fact_extraction_prompt_contract,
    repair_prompt_contract,
    review_prompt_contract,
    writing_prompt_contract,
)
from src.pipeline.fact_cards import fact_card_prompt_payload
from src.pipeline.news_seed import blocked_by_wechat_platform_risk


BANNED_PHRASES = MODEL_BANNED_PHRASES
SUMMARY_CHUNK_SIZE = 3
BATCH_CHUNK_SIZE = 3
VISIBLE_NEWS_ITEM_LIMIT = 10
FACT_READINESS_PRESERVED_REJECTION_CODES = {
    "not_news",
    "promotional",
    "missing_subject_action",
    "off_topic",
    "corrupted_source",
}


@dataclass
class EnhancementResult:
    changed: bool = False
    attempted_chunks: int = 0
    successful_chunks: int = 0
    recovered_chunks: int = 0
    failed_chunks: int = 0
    item_retries: int = 0
    json_repair_retries: int = 0
    quality_repair_chunks: int = 0
    quality_repair_items: int = 0
    quality_repaired_items: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def generation_status(self) -> str:
        if self.failed_chunks:
            return "partial_fallback_after_text_model_error" if self.changed else "fallback_after_text_model_error"
        return "applied" if self.changed else "no_change"

    def generation_metadata(self) -> dict[str, Any]:
        metadata = {
            "attempted_chunks": self.attempted_chunks,
            "successful_chunks": self.successful_chunks,
            "recovered_chunks": self.recovered_chunks,
            "failed_chunks": self.failed_chunks,
            "item_retries": self.item_retries,
        }
        if self.json_repair_retries:
            metadata["json_repair_retries"] = self.json_repair_retries
        if self.quality_repair_chunks:
            metadata.update(
                {
                    "quality_repair_chunks": self.quality_repair_chunks,
                    "quality_repair_items": self.quality_repair_items,
                    "quality_repaired_items": self.quality_repaired_items,
                }
            )
        if self.failures:
            metadata["failures"] = self.failures[:3]
        return metadata


def prepare_text_model_context(
    env_path: str | Path = ".env",
    *,
    execution_budget: ExecutionBudget | None = None,
) -> dict[str, Any]:
    configs = load_text_model_configs(env_path)
    clients: dict[str, TextModelClient] = {}
    roles: dict[str, dict[str, Any]] = {}
    model_calls: list[dict[str, Any]] = []
    for role, config in configs.items():
        role_status = {
            "enabled": config.enabled,
            "configured": config.configured,
            "model": config.model,
            "api_base": config.api_base,
        }
        if not config.enabled:
            role_status["status"] = "disabled"
        elif not config.configured:
            role_status["status"] = "missing_api_key"
        else:
            try:
                clients[role] = TextModelClient(
                    config,
                    execution_budget=execution_budget,
                    call_ledger=model_calls,
                )
                role_status["status"] = "configured"
            except TextModelError as exc:
                role_status.update({"status": "unavailable", "reason": str(exc), "payload": exc.payload})
        roles[role] = role_status
    return {"clients": clients, "roles": roles, "model_calls": model_calls}


def enhance_article_with_text_models(article: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    article["editorial_contract"] = contract_metadata(article)
    clients = context.get("clients", {})
    if not isinstance(clients, dict) or not clients:
        return article

    drop_rejected_candidates(article)
    applied: dict[str, dict[str, Any]] = {}
    for role, enhancer in (
        ("summary", _apply_summary_model),
        ("batch", _apply_batch_model),
    ):
        client = clients.get(role)
        if client is None:
            continue
        started_at = time.perf_counter()
        try:
            result = enhancer(article, client)
            if not isinstance(result, EnhancementResult):
                result = EnhancementResult(changed=bool(result))
            applied[role] = {
                "status": result.generation_status(),
                "model": client.config.model,
                "duration_seconds": round(time.perf_counter() - started_at, 3),
                **result.generation_metadata(),
            }
            if role == "summary":
                preflight = drop_rejected_candidates(article)
                if preflight["rejected"]:
                    applied[role]["preflight_rejected"] = preflight["rejected"]
        except TextModelError as exc:
            applied[role] = {
                "status": "fallback_after_text_model_error",
                "model": client.config.model,
                "duration_seconds": round(time.perf_counter() - started_at, 3),
                "reason": str(exc),
                "payload": exc.payload,
            }
        except Exception as exc:
            applied[role] = {
                "status": "fallback_after_text_model_error",
                "model": client.config.model,
                "duration_seconds": round(time.perf_counter() - started_at, 3),
                "reason": str(exc),
            }

    if applied:
        article["text_model_generation"] = {"roles": applied}
    return article


def generate_final_article_title(article: dict[str, Any], context: dict[str, Any]) -> str:
    clients = context.get("clients", {}) if isinstance(context.get("clients", {}), dict) else {}
    client = clients.get("batch")
    if client is None:
        return ""
    started_at = time.perf_counter()
    generation = article.setdefault("text_model_generation", {"roles": {}})
    try:
        payload = _chat_json(
            client,
            _messages_for_final_title(article),
            temperature=0.35,
            max_tokens=500,
        )
        title = _clean_model_title(
            payload.get("title"),
            limit=contract_for_article(article).article_title_max_chars,
        )
        if not _usable_title(title, article):
            raise TextModelError("Final title model returned an unusable title.", {"title": title})
        generation["final_title"] = {
            "status": "applied",
            "model": client.config.model,
            "duration_seconds": round(time.perf_counter() - started_at, 3),
        }
        return title
    except Exception as exc:
        generation["final_title"] = {
            "status": "fallback_after_text_model_error",
            "model": client.config.model,
            "duration_seconds": round(time.perf_counter() - started_at, 3),
            "reason": str(exc),
        }
        return ""


def review_final_article_with_text_model(
    article: dict[str, Any],
    context: dict[str, Any],
    *,
    visible_html: str,
    mode: str = "shadow",
) -> dict[str, Any]:
    normalized_mode = mode if mode in {"disabled", "shadow", "enforce"} else "shadow"
    if normalized_mode == "disabled":
        article["final_ai_review"] = {"mode": "disabled", "status": "disabled", "issues": []}
        return article

    reviewers = _reviewer_clients(context)
    if not reviewers:
        article["final_ai_review"] = {
            "mode": normalized_mode,
            "status": "unavailable",
            "risk_level": "unknown",
            "issues": [],
        }
        return article

    generation = article.setdefault("text_model_generation", {"roles": {}})
    roles = generation.setdefault("roles", {}) if isinstance(generation, dict) else {}
    attempts: list[dict[str, Any]] = []
    for reviewer_role, client in reviewers:
        started_at = time.perf_counter()
        try:
            payload = _chat_json(
                client,
                _messages_for_final_review(article, visible_html),
                temperature=0,
                max_tokens=2800,
            )
            review = _validated_final_review(payload, visible_html=visible_html, mode=normalized_mode)
            duration = round(time.perf_counter() - started_at, 3)
            attempts.append(
                {
                    "role": reviewer_role,
                    "model": client.config.model,
                    "status": "applied",
                    "duration_seconds": duration,
                }
            )
            review.update(
                {
                    "model": client.config.model,
                    "reviewer_role": reviewer_role,
                    "fallback_used": reviewer_role != "polish",
                    "attempts": attempts,
                }
            )
            article["final_ai_review"] = review
            generation["final_review"] = {
                "status": "applied",
                "reviewer_role": reviewer_role,
                "model": client.config.model,
                "fallback_used": reviewer_role != "polish",
                "attempts": attempts,
            }
            if isinstance(roles, dict) and reviewer_role == "polish":
                roles["polish"] = {
                    "status": "applied",
                    "model": client.config.model,
                    "reviewer_only": True,
                    "risk_level": review["risk_level"],
                    "issue_count": len(review["issues"]),
                    "duration_seconds": duration,
                }
            return article
        except Exception as exc:
            attempt = {
                "role": reviewer_role,
                "model": client.config.model,
                "status": "failed",
                "duration_seconds": round(time.perf_counter() - started_at, 3),
                "reason": str(exc),
            }
            attempts.append(attempt)
            if isinstance(roles, dict) and reviewer_role == "polish":
                roles["polish"] = {
                    "status": "fallback_after_text_model_error",
                    "model": client.config.model,
                    "reviewer_only": True,
                    "reason": str(exc),
                    "duration_seconds": attempt["duration_seconds"],
                }

    review = _deterministic_final_review(article, mode=normalized_mode)
    review["attempts"] = attempts
    review["fallback_used"] = True
    article["final_ai_review"] = review
    generation["final_review"] = {
        "status": review["status"],
        "reviewer_role": "deterministic",
        "fallback_used": True,
        "attempts": attempts,
        "risk_level": review["risk_level"],
        "issue_count": len(review["issues"]),
    }
    return article


def review_final_article_deterministically(
    article: dict[str, Any],
    *,
    mode: str = "shadow",
) -> dict[str, Any]:
    normalized_mode = mode if mode in {"disabled", "shadow", "enforce"} else "shadow"
    if normalized_mode == "disabled":
        article["final_ai_review"] = {"mode": "disabled", "status": "disabled", "issues": []}
        return article

    generation = article.setdefault("text_model_generation", {"roles": {}})
    previous = generation.get("final_review", {}) if isinstance(generation, dict) else {}
    attempts = previous.get("attempts", []) if isinstance(previous, dict) else []
    review = _deterministic_final_review(article, mode=normalized_mode)
    review["attempts"] = attempts if isinstance(attempts, list) else []
    review["fallback_used"] = True
    article["final_ai_review"] = review
    if isinstance(generation, dict):
        generation["final_review"] = {
            "status": review["status"],
            "reviewer_role": "deterministic",
            "fallback_used": True,
            "attempts": review["attempts"],
            "risk_level": review["risk_level"],
            "issue_count": len(review["issues"]),
        }
    return article


def _reviewer_clients(context: dict[str, Any]) -> list[tuple[str, TextModelClient]]:
    clients = context.get("clients", {})
    if not isinstance(clients, dict):
        return []
    # One independent review request is enough. Prefer the polish model so the
    # batch writer does not approve its own copy.
    for role in ("polish", "batch"):
        client = clients.get(role)
        if client is not None:
            return [(role, client)]
    return []


def _deterministic_final_review(article: dict[str, Any], *, mode: str) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(article.get("news_items", []), start=1):
        if not isinstance(item, dict):
            continue
        if blocked_by_wechat_platform_risk(item):
            evidence = _first_visible_item_text(item)
            if evidence:
                issues.append(
                    {
                        "type": "platform_risk",
                        "severity": "high",
                        "news_index": index,
                        "evidence": evidence,
                        "action": "drop_item",
                        "reason": "Deterministic fallback detected a WeChat platform-risk combination.",
                    }
                )
                continue
        summary = str(item.get("reader_summary", "")).strip()
        phrase = next((value for value in BANNED_PHRASES if value and value in summary), "")
        if phrase:
            issues.append(
                {
                    "type": "ai_cliche",
                    "severity": "medium",
                    "news_index": index,
                    "evidence": phrase,
                    "action": "repair_field",
                    "reason": "Deterministic fallback detected a banned repetitive phrase.",
                }
            )
    risk_level = "high" if any(issue["severity"] == "high" for issue in issues) else "medium" if issues else "low"
    return {
        "mode": mode,
        "status": "degraded_applied",
        "risk_level": risk_level,
        "summary": "All configured AI reviewers were unavailable; deterministic fallback review was applied.",
        "issues": issues,
        "reviewer_role": "deterministic",
        "degraded": True,
    }


def _first_visible_item_text(item: dict[str, Any]) -> str:
    for field in ("detail_heading", "reader_summary", "title"):
        value = str(item.get(field, "")).strip()
        if value:
            return value[:180]
    return ""


def text_model_package_status(context: dict[str, Any], articles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "roles": context.get("roles", {}),
        "calls": context.get("model_calls", []),
        "articles": {
            role: article.get("text_model_generation", {})
            for role, article in articles.items()
            if isinstance(article.get("text_model_generation"), dict)
        },
    }


def _apply_summary_model(article: dict[str, Any], client: TextModelClient) -> EnhancementResult:
    news_items = article.get("news_items", [])
    if not isinstance(news_items, list) or not news_items:
        return EnhancementResult()
    result = EnhancementResult()
    for start_index, chunk in _indexed_chunks(news_items, SUMMARY_CHUNK_SIZE):
        chunk_result = _apply_summary_chunk(article, client, news_items, chunk, start_index=start_index)
        _merge_result(result, chunk_result)
    return result


def _apply_batch_model(article: dict[str, Any], client: TextModelClient) -> EnhancementResult:
    news_items = article.get("news_items", [])
    if not isinstance(news_items, list) or not news_items:
        return EnhancementResult()
    result = EnhancementResult()
    for start_index, chunk in _indexed_chunks(news_items[:VISIBLE_NEWS_ITEM_LIMIT], BATCH_CHUNK_SIZE):
        include_article_fields = start_index == 1
        chunk_result = _apply_batch_chunk(
            article,
            client,
            news_items,
            chunk,
            start_index=start_index,
            include_article_fields=include_article_fields,
        )
        _merge_result(result, chunk_result)
        if chunk_result.successful_chunks:
            repair_indexes = _copy_quality_repair_indexes(
                news_items,
                start_index=start_index,
                item_count=len(chunk),
                contract=contract_for_article(article),
            )
            if repair_indexes:
                repair_result = _apply_batch_quality_repair_chunk(
                    article,
                    client,
                    news_items,
                    chunk,
                    start_index=start_index,
                    repair_indexes=repair_indexes,
                )
                _merge_result(result, repair_result)
    return result


def _apply_polish_model(article: dict[str, Any], client: TextModelClient) -> EnhancementResult:
    result = EnhancementResult(attempted_chunks=1)
    try:
        payload = _chat_json(
            client,
            _messages_for_polish(article),
            temperature=0.35,
            max_tokens=2600,
            result=result,
        )
    except TextModelError as exc:
        fallback = _apply_polish_items_model(article, client, first_error=exc)
        fallback.attempted_chunks += result.attempted_chunks
        fallback.json_repair_retries += result.json_repair_retries
        return fallback
    changed = False
    digest = _clean_model_text(payload.get("digest"), limit=140)
    if _usable_news_text(digest, max_length=140):
        article["digest"] = digest
        changed = True
    sections = payload.get("sections")
    if isinstance(sections, list) and _same_section_shape(article.get("sections"), sections):
        cleaned_sections = []
        for source, generated in zip(article["sections"], sections):
            heading = _clean_model_text(generated.get("heading"), limit=32) or str(source.get("heading", "")).strip()
            paragraphs = [
                _clean_model_text(paragraph, limit=220)
                for paragraph in generated.get("paragraphs", [])
                if _usable_news_text(paragraph, max_length=220)
            ]
            if not paragraphs:
                paragraphs = source.get("paragraphs", [])
            cleaned_sections.append({"heading": heading, "paragraphs": paragraphs})
        article["sections"] = cleaned_sections
        changed = True
    if _apply_generated_news_items(
        payload.get("items"),
        article.get("news_items", []),
        allow_title_summary=False,
        contract=contract_for_article(article),
    ):
        changed = True
    result.changed = changed
    result.successful_chunks = 1
    return result


def _apply_polish_items_model(
    article: dict[str, Any],
    client: TextModelClient,
    *,
    first_error: TextModelError,
) -> EnhancementResult:
    news_items = article.get("news_items", [])
    if not isinstance(news_items, list) or not news_items:
        raise first_error
    result = EnhancementResult(
        failed_chunks=1,
        failures=[{"stage": "full_polish", "reason": str(first_error), "payload": first_error.payload}],
    )
    for start_index, chunk in _indexed_chunks(news_items[:VISIBLE_NEWS_ITEM_LIMIT], BATCH_CHUNK_SIZE):
        chunk_result = _apply_polish_items_chunk(article, client, news_items, chunk, start_index=start_index)
        if chunk_result.failed_chunks and len(chunk) > 1:
            recovered = _retry_polish_items(article, client, news_items, chunk, start_index=start_index)
            if recovered.successful_chunks:
                recovered.recovered_chunks = 1
                recovered.attempted_chunks += chunk_result.attempted_chunks
                _merge_result(result, recovered)
            else:
                _merge_result(result, chunk_result)
                result.item_retries += recovered.item_retries
        else:
            _merge_result(result, chunk_result)
    if not result.successful_chunks:
        raise first_error
    result.failed_chunks = 0
    return result


def _apply_polish_items_chunk(
    article: dict[str, Any],
    client: TextModelClient,
    news_items: list[Any],
    chunk: list[Any],
    *,
    start_index: int,
) -> EnhancementResult:
    result = EnhancementResult(attempted_chunks=1)
    try:
        payload = _chat_json(
            client,
            _messages_for_polish_items(article, chunk, start_index=start_index),
            temperature=0.25,
            max_tokens=1200,
            result=result,
        )
        result.changed = _apply_generated_news_items(
            payload.get("items"),
            news_items,
            allow_title_summary=False,
            expected_indexes=range(start_index, start_index + len(chunk)),
            contract=contract_for_article(article),
        )
        result.successful_chunks = 1
    except TextModelError as exc:
        result.failed_chunks = 1
        result.failures.append({"start_index": start_index, "reason": str(exc), "payload": exc.payload})
    return result


def _retry_polish_items(
    article: dict[str, Any],
    client: TextModelClient,
    news_items: list[Any],
    chunk: list[Any],
    *,
    start_index: int,
) -> EnhancementResult:
    result = EnhancementResult(item_retries=len(chunk))
    for offset, item in enumerate(chunk):
        item_result = _apply_polish_items_chunk(article, client, news_items, [item], start_index=start_index + offset)
        _merge_result(result, item_result)
    return result


def _apply_summary_chunk(
    article: dict[str, Any],
    client: TextModelClient,
    news_items: list[Any],
    chunk: list[Any],
    *,
    start_index: int,
) -> EnhancementResult:
    result = EnhancementResult(attempted_chunks=1)
    try:
        payload = _chat_json(
            client,
            _messages_for_summary(article, chunk, start_index=start_index),
            temperature=0.2,
            max_tokens=2200,
            result=result,
        )
        items = payload.get("items")
        if not isinstance(items, list) and len(chunk) == 1:
            payload = {"items": [{**payload, "index": start_index}]}
            items = payload.get("items")
        if not isinstance(items, list):
            raise TextModelError("Summary model response must contain items list.", payload)
        result.changed = _apply_generated_fact_cards(
            items,
            news_items,
            contract=contract_for_article(article),
        )
        result.successful_chunks = 1
    except TextModelError as exc:
        result.failed_chunks = 1
        result.failures.append({"start_index": start_index, "reason": str(exc), "payload": exc.payload})
    return result


def _apply_batch_chunk(
    article: dict[str, Any],
    client: TextModelClient,
    news_items: list[Any],
    chunk: list[Any],
    *,
    start_index: int,
    include_article_fields: bool,
) -> EnhancementResult:
    result = EnhancementResult(attempted_chunks=1)
    try:
        payload = _chat_json(
            client,
            _messages_for_batch(
                article,
                chunk,
                start_index=start_index,
                include_article_fields=include_article_fields,
            ),
            temperature=0.45,
            max_tokens=1800,
            result=result,
        )
        changed = False
        if include_article_fields:
            raw_title = str(payload.get("title") or "")
            title = _clean_model_title(
                raw_title,
                limit=contract_for_article(article).article_title_max_chars,
            )
            if _usable_title(title, article) and not _contains_news_byline(raw_title):
                article["title"] = title
                changed = True
            digest = _clean_complete_model_text(payload.get("digest"), limit=120, prose=True)
            if _usable_news_text(digest, max_length=120):
                article["digest"] = digest
                changed = True
        if _apply_generated_news_items(
            payload.get("items"),
            news_items,
            allow_title_summary=False,
            expected_indexes=range(start_index, start_index + len(chunk)),
            contract=contract_for_article(article),
        ):
            changed = True
        result.changed = changed
        result.successful_chunks = 1
    except TextModelError as exc:
        result.failed_chunks = 1
        result.failures.append({"start_index": start_index, "reason": str(exc), "payload": exc.payload})
    return result


def _apply_batch_quality_repair_chunk(
    article: dict[str, Any],
    client: TextModelClient,
    news_items: list[Any],
    chunk: list[Any],
    *,
    start_index: int,
    repair_indexes: list[int],
) -> EnhancementResult:
    result = EnhancementResult(
        attempted_chunks=1,
        quality_repair_chunks=1,
        quality_repair_items=len(repair_indexes),
    )
    try:
        payload = _chat_json(
            client,
            _messages_for_batch_quality_repair(
                article,
                chunk,
                start_index=start_index,
                repair_indexes=repair_indexes,
            ),
            temperature=0.25,
            max_tokens=2200,
            result=result,
        )
        generated_items = payload.get("items")
        filtered_items = (
            [
                item
                for item in generated_items
                if isinstance(item, dict) and _int_value(item.get("index"), 0) in repair_indexes
            ]
            if isinstance(generated_items, list)
            else []
        )
        result.changed = _apply_generated_news_items(
            filtered_items,
            news_items,
            allow_title_summary=False,
            expected_indexes=repair_indexes,
            contract=contract_for_article(article),
        )
        remaining = _copy_quality_repair_indexes(
            news_items,
            indexes=repair_indexes,
            contract=contract_for_article(article),
        )
        result.quality_repaired_items = len(repair_indexes) - len(remaining)
        result.successful_chunks = 1
    except TextModelError as exc:
        result.failed_chunks = 1
        result.failures.append(
            {
                "stage": "batch_copy_quality_repair",
                "start_index": start_index,
                "reason": str(exc),
                "payload": exc.payload,
            }
        )
    return result


def _merge_result(target: EnhancementResult, source: EnhancementResult) -> None:
    target.changed = target.changed or source.changed
    target.attempted_chunks += source.attempted_chunks
    target.successful_chunks += source.successful_chunks
    target.recovered_chunks += source.recovered_chunks
    target.failed_chunks += source.failed_chunks
    target.item_retries += source.item_retries
    target.json_repair_retries += source.json_repair_retries
    target.quality_repair_chunks += source.quality_repair_chunks
    target.quality_repair_items += source.quality_repair_items
    target.quality_repaired_items += source.quality_repaired_items
    target.failures.extend(source.failures)


def _messages_for_summary(
    article: dict[str, Any],
    news_items: list[Any],
    *,
    start_index: int = 1,
) -> list[dict[str, str]]:
    editorial_contract = fact_extraction_prompt_contract(article)
    minimum_extra_facts = max(1, int(editorial_contract["minimum_supported_fact_units"]) - 1)
    return [
        {
            "role": "system",
            "content": (
                "你是新闻事实提取员，不负责写公众号文案。只根据输入标题、摘要、来源和链接提取事实卡片，"
                "不得补充输入中没有的人名、数字、时间、公司动作或结论。只返回 JSON。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        "逐条输出 fact_card。subject 是新闻主体；action 是主体做了什么；facts 是来源明确支持的事实句；"
                        "numbers 是原文中的数字和单位；uncertainties 写来源没有说清的地方。"
                        "输出必须精简并覆盖每个输入 index：subject 不超过40字符，action 不超过80字符，"
                        f"facts 保留{minimum_extra_facts}-4条且每条不超过100字符；如果来源不足以提供这么多独立事实，不要补写，"
                        "numbers 最多8项，uncertainties 最多2条；"
                        "同一事实不要在 subject、action 和 facts 中反复扩写。"
                        "同时输出 candidate_verdict。只有在内容不是新闻、是广告导流、主体动作缺失、事实不足、明显跑题或来源文本损坏时，"
                        "usable 才能为 false，并使用规定的 reason_code；英文新闻本身不是拒绝理由。"
                        "只有评论口号、孤立引语或趋势判断，却没有可核验的主体动作，也属于事实不足；"
                        "标题与摘要明显讲不同事情、摘要出现多层媒体名套娃或语义损坏时，使用 corrupted_source。"
                        "删除广告、公众号导流和作者署名。不要写影响判断，不要写公众号文案。"
                        "严格执行 editorial_contract 的事实充分性规则。"
                    ),
                    "editorial_contract": editorial_contract,
                    "banned_phrases": BANNED_PHRASES,
                    "article": _article_brief(article),
                    "items": _news_items_for_prompt(news_items, start_index=start_index),
                    "output_schema": {
                        "items": [
                            {
                                "index": 1,
                                "fact_card": {
                                    "subject": "...",
                                    "action": "...",
                                    "facts": ["..."],
                                    "numbers": ["..."],
                                    "uncertainties": ["..."],
                                },
                                "candidate_verdict": {
                                    "usable": True,
                                    "reason_code": "",
                                    "reason": "",
                                },
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def _messages_for_final_title(article: dict[str, Any]) -> list[dict[str, str]]:
    contract = contract_for_article(article)
    return [
        {
            "role": "system",
            "content": (
                "你是微信公众号日报标题编辑。新闻集合已经冻结，只能依据输入中最终保留的新闻生成标题。"
                "标题必须全部使用清楚的中文新闻要素，不得加入记者署名、来源站日期、英文疑问词残片或输入外事实。只返回 JSON。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        f"从最终新闻中选择{contract.article_title_min_elements}-{contract.article_title_max_elements}个最有点击价值且彼此不同的主体+动作要素，用中文分号连接。"
                        f"结尾必须保留 column_suffix，总长度不超过{contract.article_title_max_chars}个字符。不要写空泛趋势判断。"
                    ),
                    "column_suffix": _title_suffix(article),
                    "editorial_contract": contract_metadata(article),
                    "items": [
                        {
                            "detail_heading": item.get("detail_heading", ""),
                            "source_name": item.get("source_name", ""),
                            "fact_card": fact_card_prompt_payload(item),
                        }
                        for item in article.get("news_items", [])
                        if isinstance(item, dict)
                    ],
                    "output_schema": {"title": "..."},
                },
                ensure_ascii=False,
            ),
        },
    ]


def _messages_for_batch(
    article: dict[str, Any],
    news_items: list[Any],
    *,
    start_index: int = 1,
    include_article_fields: bool = True,
) -> list[dict[str, str]]:
    contract = contract_for_article(article)
    editorial_contract = writing_prompt_contract(article)
    return [
        {
            "role": "system",
            "content": (
                "你是微信公众号早报主编，负责把当天新闻排成一篇可发布的中文稿。"
                "只使用输入新闻事实，不能编造未提供的信息。标题要有点击欲，但不能标题党。"
                "每条新闻的橙色核心句和正文第一段都要像人工编辑写的新闻，不要像模型模板。只返回 JSON。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        "重写这一小组新闻的 detail_heading 和 reader_summary。"
                        + (
                            f"同时生成整篇文章标题和摘要。标题包含{contract.article_title_min_elements}-{contract.article_title_max_elements}个具体新闻要素，用中文分号分隔，结尾必须是 column_suffix，总长度不能超过{contract.article_title_max_chars}个汉字/字符。标题要像“新闻变化拼接”，不要只是名词清单。"
                            if include_article_fields
                            else "本组不要输出整篇标题和摘要，只输出 items。"
                        )
                        + f"detail_heading 用于橙色【】标题，要包含主体+动作+核心看点，长度为{contract.detail_heading_min_chars}-{contract.detail_heading_max_chars}个汉字/字符，读者单看这一句就能知道这条新闻讲什么。"
                        + f"reader_summary 用于正文第一段，目标为{contract.reader_summary_target_min_chars}-{contract.reader_summary_target_max_chars}个有效字符，"
                        + f"优先写{contract.reader_summary_preferred_min_sentences}-{contract.reader_summary_preferred_max_sentences}个职责不同的完整句子，并严格按 editorial_contract.sentence_jobs 分配事实；"
                        + f"如果事实只适合写2句，去掉空格和标点后至少{contract.reader_summary_two_sentence_min_chars}个字符，不能用判断句凑长度。"
                        + "reader_summary 的第一整句不得复述 detail_heading，末尾必须是完整句号、问号或叹号，严禁输出半句话。"
                        + "渲染层会另行显示来源名和橙色核心句；detail_heading 和 reader_summary 都禁止写“据某媒体”“某媒体报道/消息/讯”，也不要复述来源名。"
                        + "相邻句不要连续用同一个公司名、产品名或人名开头，更不能换几个词重复同一事实。"
                        + "只写这一段，不要追加统一的观察模板、读者建议或后续关注清单。"
                        + "所有具体事实和数字只能来自每条新闻的 fact_card；每个完整句子都必须增加至少一个不同事实，fact_card 没有的信息不要补。"
                        + "行业影响只有在 fact_card 明确支持时才能写；不要为了凑字数补“标志着、意味着、倒逼、重塑格局、生存底线”等宏大判断。"
                        + "不要写“它值得放进早报”“换个角度看”“读这类消息时”等模板话。"
                    ),
                    "column_suffix": _title_suffix(article),
                    "editorial_contract": editorial_contract,
                    "banned_phrases": BANNED_PHRASES,
                    "style_rules": [
                        "标题优先使用中文信息，不要整句英文。",
                        "标题要带动作或变化词，如警示、下放、升温、加速、补齐、进入、验证、落地。",
                        "正文遵守 editorial_contract 的固定信息结构，但不得复用示例措辞、固定连接词或统一开头。",
                        "有来源名和链接，但不要在正文里写链接。",
                        "信息不足以满足 editorial_contract 时不要凑字，候选应退出并由 reserve 补位。",
                    ],
                    "current_title": article.get("title", ""),
                    "current_digest": article.get("digest", ""),
                    "items": _news_items_for_prompt(news_items, start_index=start_index),
                    "output_schema": {
                        "title": "..." if include_article_fields else "",
                        "digest": "..." if include_article_fields else "",
                        "items": [{"index": 1, "detail_heading": "...", "reader_summary": "..."}],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def _messages_for_batch_quality_repair(
    article: dict[str, Any],
    news_items: list[Any],
    *,
    start_index: int,
    repair_indexes: list[int],
) -> list[dict[str, str]]:
    contract = contract_for_article(article)
    editorial_contract = repair_prompt_contract(article)
    current_copy = []
    for offset, item in enumerate(news_items):
        if not isinstance(item, dict):
            continue
        index = start_index + offset
        if index not in repair_indexes:
            continue
        validation = item.get("copy_generation_validation", {})
        current_copy.append(
            {
                "index": index,
                "detail_heading": str(item.get("detail_heading", "")),
                "reader_summary": str(item.get("reader_summary", "")),
                "rejection_reason": (
                    str(validation.get("reason", "insufficient_depth"))
                    if isinstance(validation, dict)
                    else "insufficient_depth"
                ),
                "rejection_reasons": (
                    validation.get("reasons", {})
                    if isinstance(validation, dict) and isinstance(validation.get("reasons", {}), dict)
                    else {}
                ),
            }
        )
    return [
        {
            "role": "system",
            "content": (
                "你是微信公众号新闻正文编辑。上一轮文案未达到长度或完整性要求。"
                "只依据输入事实卡片补写，不得新增事实，只返回 JSON。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        "只重写 required_indexes 对应的 detail_heading 和 reader_summary，其他索引不要输出。"
                        f"reader_summary 目标为{contract.reader_summary_target_min_chars}-{contract.reader_summary_target_max_chars}个有效字符，"
                        f"优先写{contract.reader_summary_preferred_min_sentences}-{contract.reader_summary_preferred_max_sentences}个完整句子；"
                        f"如果只写2句，去掉空格和标点后至少{contract.reader_summary_two_sentence_min_chars}个字符。"
                        "每句话都要增加一个可核验事实：主体动作、数字、产品、应用场景或输入已明确的背景；"
                        "不能用评论、趋势判断、读者建议或同义反复凑字数。第一句不得复述 detail_heading。"
                        f"detail_heading 为{contract.detail_heading_min_chars}-{contract.detail_heading_max_chars}个汉字/字符，写清主体、动作和核心看点。"
                        "根据 current_copy.rejection_reason 和 rejection_reasons 执行 editorial_contract.repair_reason_rules，不能对所有失败项套用同一种补写句式。"
                        "输出前自行检查每条正文的长度和句子数，但不要输出检查过程。"
                    ),
                    "required_indexes": repair_indexes,
                    "column": str(article.get("column", "")),
                    "source_policy": str(article.get("source_policy", "")),
                    "editorial_contract": editorial_contract,
                    "banned_phrases": BANNED_PHRASES,
                    "current_copy": current_copy,
                    "items": _news_items_for_prompt(news_items, start_index=start_index),
                    "output_schema": {
                        "items": [
                            {
                                "index": 1,
                                "detail_heading": "...",
                                "reader_summary": "...",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def _messages_for_polish(article: dict[str, Any]) -> list[dict[str, str]]:
    contract = contract_for_article(article)
    editorial_contract = writing_prompt_contract(article)
    return [
        {
            "role": "system",
            "content": (
                "你是中文编辑，负责去掉 AI 味和重复句式。保持事实不变，保持 section 数量和段落数量不变。"
                "不要添加输入以外的事实。只返回 JSON。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        "润色 digest 和 sections，目标是更像人工编辑：少概念堆砌，少固定句式，"
                        "每段都要有具体信息。保持原意和事实范围不变，避开禁用句式。"
                        "同时检查所有输入新闻的 detail_heading 和 reader_summary，如有模板味、英文碎片、"
                        "空泛判断、过短或 RSS 原文味，请重写 items。reader_summary 要像公众号正文第一段，"
                        f"目标为{contract.reader_summary_target_min_chars}-{contract.reader_summary_target_max_chars}个有效字符，"
                        f"优先写{contract.reader_summary_preferred_min_sentences}-{contract.reader_summary_preferred_max_sentences}个各自提供新事实的完整句子；如果只写2句，"
                        f"去掉空格和标点后至少{contract.reader_summary_two_sentence_min_chars}个字符；第一整句不得复述 detail_heading，"
                        "相邻句不得重复同一主体和事实，末尾必须结束在完整标点，不能输出半句话。"
                        "渲染层会单独显示来源名和橙色句，正文禁止再写媒体名、‘据某媒体’或‘某媒体报道/消息/讯’。"
                        "只依据输入事实，不添加新事实。标题和橙色句子要体现变化，而不是只列公司名。"
                    ),
                    "editorial_contract": editorial_contract,
                    "banned_phrases": BANNED_PHRASES,
                    "article": {
                        "title": article.get("title", ""),
                        "digest": article.get("digest", ""),
                        "sections": article.get("sections", []),
                        "items": _news_items_for_prompt(article.get("news_items", [])),
                    },
                    "output_schema": {
                        "digest": "...",
                        "sections": [{"heading": "...", "paragraphs": ["..."]}],
                        "items": [{"index": 1, "detail_heading": "...", "reader_summary": "..."}],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def _messages_for_final_review(article: dict[str, Any], visible_html: str) -> list[dict[str, str]]:
    visible_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible_html)).strip()
    editorial_contract = review_prompt_contract(article)
    return [
        {
            "role": "system",
            "content": (
                "你是微信公众号终稿审核员，只审核，不改写文章。根据事实卡片和最终可见稿识别问题。"
                "不要输出思考过程，不要给长篇建议，只返回结构化 JSON。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        "检查最终稿是否存在来源外事实、数字漂移、AI固定口癖、重复句式、广告或RSS尾巴、"
                        "记者编辑署名、英文碎片、标题或句子截断、微信平台高风险表达。"
                        "还要逐条检查：橙色核心句与正文第一句是否同义复述，正文是否再次写来源媒体名，"
                        "相邻句是否重复同一主体和事实，以及正文是否只剩一句引语、口号或空泛判断。"
                        "这些问题分别标记 repetition 或 ai_cliche；事实不足以形成正文时建议 drop_item，不要让它静默通过。"
                        "只有终稿新增了事实卡和来源都不支持的具体事实时，才标记 source_outside_fact；"
                        "不要把标题不够具体、遗漏产品全名或信息取舍标记为来源外事实。"
                        "英文媒体品牌和产品名可以保留，不要仅因来源名称是英文就标记 english_fragment。"
                        "如果新闻与文章栏目领域无关或被放入明显错误的分类，标记 off_topic 并建议 drop_item。"
                        "evidence 必须逐字来自 final_visible_text；没有证据就不要报问题。"
                        "必须在 item_verdicts 中对每个 news_index 输出 pass 或 reject，不能只检查前几条。"
                        "reject 时必须同时给出类型、严重度、逐字证据和动作；没有问题时 issues 返回空数组。"
                    ),
                    "title": article.get("title", ""),
                    "digest": article.get("digest", ""),
                    "editorial_contract": editorial_contract,
                    "fact_cards": article.get("fact_cards", []),
                    "news_items": [
                        {
                            "index": index,
                            "fact_card": fact_card_prompt_payload(item),
                            "detail_heading": item.get("detail_heading", ""),
                            "reader_summary": item.get("reader_summary", ""),
                        }
                        for index, item in enumerate(article.get("news_items", []), start=1)
                        if isinstance(item, dict)
                    ],
                    "final_visible_text": visible_text[:16000],
                    "output_schema": {
                        "risk_level": "low|medium|high",
                        "summary": "...",
                        "issues": [
                            {
                                "type": "source_outside_fact|numeric_drift|off_topic|ai_cliche|repetition|promotion|byline|english_fragment|truncation|platform_risk",
                                "severity": "low|medium|high",
                                "news_index": 0,
                                "evidence": "必须逐字来自终稿",
                                "action": "keep|drop_item|repair_field|block",
                                "reason": "一句话说明",
                            }
                        ],
                        "item_verdicts": [
                            {
                                "news_index": 1,
                                "verdict": "pass|reject",
                                "type": "source_outside_fact|numeric_drift|off_topic|ai_cliche|repetition|promotion|byline|english_fragment|truncation|platform_risk",
                                "severity": "low|medium|high",
                                "evidence": "reject时必须逐字来自终稿",
                                "action": "keep|drop_item|repair_field|block",
                                "reason": "一句话说明",
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def _messages_for_polish_items(
    article: dict[str, Any],
    news_items: list[Any],
    *,
    start_index: int = 1,
) -> list[dict[str, str]]:
    contract = contract_for_article(article)
    editorial_contract = writing_prompt_contract(article)
    return [
        {
            "role": "system",
            "content": (
                "你是中文产业新闻编辑。只根据输入新闻改写每条新闻的橙色核心句和正文第一段，"
                "不得新增输入之外的事实、数字、公司动作或结论。只返回 JSON。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        "为这些新闻生成 items。detail_heading 用于正文橙色【】核心句，必须是中文，"
                        f"包含主体、动作和核心看点，{contract.detail_heading_min_chars}-{contract.detail_heading_max_chars}个汉字/字符。reader_summary 是正文第一段，"
                        f"要像公众号正文，不是英文摘要翻译或要点列表，目标为{contract.reader_summary_target_min_chars}-{contract.reader_summary_target_max_chars}个有效字符，"
                        f"优先写{contract.reader_summary_preferred_min_sentences}-{contract.reader_summary_preferred_max_sentences}个各自提供新事实的完整句子；如果只写2句，"
                        f"去掉空格和标点后至少{contract.reader_summary_two_sentence_min_chars}个字符。"
                        "reader_summary 第一整句不得复述 detail_heading，最后必须以完整句号、问号或叹号结束，不能截成半句。"
                        "渲染层会单独显示来源和橙色句，正文禁止再写媒体名、‘据某媒体’或‘某媒体报道/消息/讯’；"
                        "相邻句不要连续用同一主体开头，也不要换词重复同一事实。"
                        "如果原始标题或摘要是英文，必须改写成自然中文；不要保留英文标题碎片。"
                        "如果是智能制造日报，要优先写 fact_card 已明确提供的产线、交付、客户、供应链或验证信息，"
                        "没有这些事实时不要自行补齐，更不要泛泛说 AI 很重要。"
                    ),
                    "column": str(article.get("column", "")),
                    "source_policy": str(article.get("source_policy", "")),
                    "editorial_contract": editorial_contract,
                    "banned_phrases": BANNED_PHRASES,
                    "items": _news_items_for_prompt(news_items, start_index=start_index),
                    "output_schema": {
                        "items": [{"index": 1, "detail_heading": "...", "reader_summary": "..."}],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def _chat_json(
    client: TextModelClient,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    result: EnhancementResult | None = None,
) -> dict[str, Any]:
    content = client.chat(messages, temperature=temperature, max_tokens=max_tokens)
    payload = _parse_json_object(content)
    if not isinstance(payload, dict):
        raise TextModelError("Text model response must be a JSON object.")
    return payload


def _parse_json_object(content: str) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    if not value.startswith(("{", "[")):
        match = re.search(r"\{.*\}", value, flags=re.DOTALL)
        if match:
            value = match.group(0)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        for repaired in _json_repair_candidates(value):
            if repaired == value:
                continue
            try:
                parsed = json.loads(repaired)
                break
            except json.JSONDecodeError:
                continue
        else:
            raise TextModelError("Text model response is not valid JSON.", {"text": value[:500]}) from exc
    if isinstance(parsed, list):
        return {"items": parsed}
    if not isinstance(parsed, dict):
        raise TextModelError("Text model response JSON must be an object.")
    return parsed


def _repair_unescaped_cjk_quotes(value: str) -> str:
    text = value
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if escaped:
            result.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and in_string:
            result.append(char)
            escaped = True
            index += 1
            continue
        if char == '"':
            if not in_string:
                in_string = True
                result.append(char)
            elif _quote_closes_json_string(text, index):
                in_string = False
                result.append(char)
            else:
                result.append('\\"')
            index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _quote_closes_json_string(text: str, quote_index: int) -> bool:
    index = quote_index + 1
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return True
    return text[index] in {",", "}", "]", ":"}


def _json_repair_candidates(value: str) -> list[str]:
    balanced = _truncate_to_balanced_json(value)
    candidates = [
        value,
        balanced,
        _repair_unescaped_cjk_quotes(value),
        _repair_unescaped_cjk_quotes(balanced),
    ]
    candidates.extend(_strip_trailing_commas(item) for item in list(candidates))
    result: list[str] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def _truncate_to_balanced_json(value: str) -> str:
    start = value.find("{")
    if start < 0:
        return value
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(value[start:], start=start):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return value


def _strip_trailing_commas(value: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", value)


def _article_brief(article: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(article.get("title", "")),
        "digest": str(article.get("digest", "")),
        "column": str(article.get("column", "")),
        "source_policy": str(article.get("source_policy", "")),
    }


def _indexed_chunks(items: list[Any], size: int) -> list[tuple[int, list[Any]]]:
    return [(index + 1, items[index : index + size]) for index in range(0, len(items), size)]


def _news_items_for_prompt(news_items: list[Any], *, start_index: int = 1) -> list[dict[str, Any]]:
    items = []
    for offset, item in enumerate(news_items):
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "index": start_index + offset,
                "title": str(item.get("title", ""))[:220],
                "summary": _strip_news_byline(str(item.get("summary", "")))[:360],
                "source_name": str(item.get("source_name", ""))[:80],
                "url": str(item.get("url", ""))[:220],
                "published_at": str(item.get("published_at", ""))[:80],
                "fact_card": fact_card_prompt_payload(item),
            }
        )
    return items


def _title_suffix(article: dict[str, Any]) -> str:
    title = str(article.get("title", "")).strip()
    if "智能制造日报" in title or str(article.get("source_policy", "")) == "smart_manufacturing_daily":
        return "丨智能制造日报"
    return "丨科技早报"


def _usable_title(value: str, article: dict[str, Any]) -> bool:
    contract = contract_for_article(article)
    suffix = _title_suffix(article)
    generic_phrases = (
        "科技应用更新",
        "AI模型更新",
        "AI模型能力继续更新",
        "具体场景和持续交付能力更值得看",
    )
    return (
        _usable_news_text(value, max_length=contract.article_title_max_chars)
        and value.endswith(suffix)
        and len(value) >= len(suffix) + 8
        and not _contains_news_byline(value)
        and not any(phrase in value for phrase in generic_phrases)
    )


def _usable_news_text(value: Any, *, max_length: int) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > max_length:
        return False
    return not _has_banned_model_phrase(text) and not _contains_news_byline(text)


def _apply_generated_news_items(
    generated_items: Any,
    news_items: list[Any],
    *,
    allow_title_summary: bool,
    expected_indexes: Iterable[int] | None = None,
    contract: EditorialContract | None = None,
) -> bool:
    selected_contract = contract or contract_for_article(None)
    changed = False
    by_index = {index + 1: item for index, item in enumerate(news_items) if isinstance(item, dict)}
    received_indexes: set[int] = set()
    for generated in generated_items if isinstance(generated_items, list) else []:
        if not isinstance(generated, dict):
            continue
        index = _int_value(generated.get("index"), 0)
        target = by_index.get(index)
        if target is None:
            continue
        received_indexes.add(index)
        validation = (
            dict(target.get("copy_generation_validation", {}))
            if isinstance(target.get("copy_generation_validation", {}), dict)
            else {}
        )
        rejection_reasons: dict[str, str] = {}
        if allow_title_summary:
            title = _clean_model_text(generated.get("title"), limit=64)
            summary = _clean_model_text(generated.get("summary"), limit=180)
            if _usable_news_text(title, max_length=64) and _preserves_source_numeric_facts(target, title):
                target["title"] = title
                changed = True
            if _usable_news_text(summary, max_length=180) and _preserves_source_numeric_facts(target, summary):
                target["summary"] = summary
                changed = True
        detail_heading = _clean_complete_model_text(
            generated.get("detail_heading"),
            limit=selected_contract.detail_heading_max_chars,
            prose=False,
        )
        if _usable_detail_heading(
            detail_heading,
            contract=selected_contract,
        ) and _preserves_source_numeric_facts(target, detail_heading):
            target["detail_heading"] = detail_heading
            validation.pop("detail_heading", None)
            changed = True
        else:
            rejection_reasons["detail_heading"] = _detail_heading_rejection_reason(
                detail_heading,
                target=target,
                contract=selected_contract,
            )
            validation["detail_heading"] = "rejected"
        actual_heading = str(target.get("detail_heading", "")).strip()
        reader_summary = _clean_complete_model_text(
            generated.get("reader_summary"),
            limit=selected_contract.reader_summary_output_max_chars,
            prose=True,
        )
        reader_summary = _drop_repeated_heading_sentence(
            reader_summary,
            actual_heading,
        )
        if _usable_reader_summary(
            reader_summary,
            contract=selected_contract,
        ) and _preserves_source_numeric_facts(target, reader_summary):
            target["reader_summary"] = reader_summary
            validation.pop("reader_summary", None)
            changed = True
        else:
            rejection_reasons["reader_summary"] = _reader_summary_rejection_reason(
                reader_summary,
                target=target,
                contract=selected_contract,
            )
            validation["reader_summary"] = "rejected"
        if validation.get("detail_heading") == "rejected" or validation.get("reader_summary") == "rejected":
            validation["reason"] = rejection_reasons.get(
                "reader_summary",
                rejection_reasons.get("detail_heading", str(validation.get("reason", "contract_mismatch"))),
            )
            validation["reasons"] = rejection_reasons
            validation["editorial_contract_version"] = EDITORIAL_CONTRACT_VERSION
            target["copy_generation_validation"] = validation
        else:
            target.pop("copy_generation_validation", None)
    for index in expected_indexes or ():
        target = by_index.get(index)
        if target is None or index in received_indexes:
            continue
        target["copy_generation_validation"] = {
            "detail_heading": "rejected",
            "reader_summary": "rejected",
            "reason": "missing_item",
            "reasons": {
                "detail_heading": "missing_item",
                "reader_summary": "missing_item",
            },
            "editorial_contract_version": EDITORIAL_CONTRACT_VERSION,
        }
    return changed


def _apply_generated_fact_cards(
    generated_items: Any,
    news_items: list[Any],
    *,
    contract: EditorialContract | None = None,
) -> bool:
    if not isinstance(generated_items, list):
        return False
    selected_contract = contract or contract_for_article(None)
    changed = False
    by_index = {index + 1: item for index, item in enumerate(news_items) if isinstance(item, dict)}
    for generated in generated_items:
        if not isinstance(generated, dict):
            continue
        target = by_index.get(_int_value(generated.get("index"), 0))
        card = generated.get("fact_card")
        if target is None or not isinstance(card, dict):
            continue
        current = target.get("fact_card", {}) if isinstance(target.get("fact_card", {}), dict) else {}
        updated = dict(current)
        for key, limit in (("subject", 64), ("action", 160)):
            value = _clean_model_text(card.get(key), limit=limit)
            if value and _preserves_source_numeric_facts(target, value):
                updated[key] = value
        for key, limit in (("facts", 220), ("uncertainties", 120)):
            values = card.get(key)
            if not isinstance(values, list):
                continue
            cleaned = []
            for raw_value in values[:6]:
                value = _clean_model_text(raw_value, limit=limit)
                if value and _preserves_source_numeric_facts(target, value):
                    cleaned.append(value)
            if cleaned:
                updated[key] = cleaned
        numbers = card.get("numbers")
        if isinstance(numbers, list):
            source_text = "\n".join(str(target.get(key, "")) for key in ("title", "summary"))
            cleaned_numbers = [str(value).strip() for value in numbers if str(value).strip() in source_text]
            if cleaned_numbers:
                updated["numbers"] = cleaned_numbers[:12]
        target["fact_card"] = updated
        apply_candidate_verdict(target, generated.get("candidate_verdict"), clean_reason=_clean_model_text)
        readiness = fact_card_readiness_report(updated, selected_contract)
        target["editorial_fact_readiness"] = {
            "editorial_contract_version": EDITORIAL_CONTRACT_VERSION,
            "status": "ready" if readiness["ok"] else "insufficient_facts",
            "supported_fact_units": readiness["supported_fact_units"],
            "minimum_supported_fact_units": readiness["minimum_supported_fact_units"],
        }
        verdict = target.get("candidate_verdict", {})
        preserves_stronger_rejection = (
            isinstance(verdict, dict)
            and verdict.get("usable") is False
            and str(verdict.get("reason_code", "")).strip().lower()
            in FACT_READINESS_PRESERVED_REJECTION_CODES
        )
        if not readiness["ok"] and not preserves_stronger_rejection:
            target["candidate_verdict"] = {
                "usable": False,
                "reason_code": "insufficient_facts",
                "reason": (
                    "事实卡不足以支撑编辑合同要求的独立新闻句，候选应由同日 reserve 补位。"
                ),
            }
        changed = True
    return changed


def _drop_summary_rejected_items(article: dict[str, Any]) -> dict[str, Any]:
    return drop_rejected_candidates(article)


def _validated_final_review(payload: dict[str, Any], *, visible_html: str, mode: str) -> dict[str, Any]:
    visible_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible_html)).strip()
    risk_level = str(payload.get("risk_level", "low")).strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "medium"
    allowed_types = {
        "source_outside_fact",
        "numeric_drift",
        "off_topic",
        "ai_cliche",
        "repetition",
        "promotion",
        "byline",
        "english_fragment",
        "truncation",
        "platform_risk",
    }
    allowed_actions = {"keep", "drop_item", "repair_field", "block"}
    issues = []
    raw_issues = list(payload.get("issues", [])) if isinstance(payload.get("issues", []), list) else []
    item_verdicts = payload.get("item_verdicts", [])
    for verdict in item_verdicts if isinstance(item_verdicts, list) else []:
        if not isinstance(verdict, dict) or str(verdict.get("verdict", "")).strip().lower() != "reject":
            continue
        raw_issues.append(
            {
                "type": verdict.get("type"),
                "severity": verdict.get("severity"),
                "news_index": verdict.get("news_index"),
                "evidence": verdict.get("evidence"),
                "action": verdict.get("action"),
                "reason": verdict.get("reason"),
            }
        )
    for issue in raw_issues:
        if not isinstance(issue, dict):
            continue
        evidence = _clean_model_text(issue.get("evidence"), limit=180)
        issue_type = str(issue.get("type", "")).strip()
        severity = str(issue.get("severity", "medium")).strip().lower()
        action = str(issue.get("action", "keep")).strip()
        reason = _clean_model_text(issue.get("reason"), limit=180)
        if severity in {"medium", "high"} and any(
            phrase in reason for phrase in ("与栏目无关", "与智能制造无关", "分类错位", "明显跑题", "不属于该栏目")
        ):
            issue_type = "off_topic"
            action = "drop_item"
        if not evidence or evidence not in visible_text or issue_type not in allowed_types:
            continue
        if severity not in {"low", "medium", "high"}:
            severity = "medium"
        if action not in allowed_actions:
            action = "keep"
        issues.append(
            {
                "type": issue_type,
                "severity": severity,
                "news_index": _int_value(issue.get("news_index"), 0),
                "evidence": evidence,
                "action": action,
                "reason": reason,
            }
        )
        if len(issues) >= 20:
            break
    if any(issue["severity"] == "high" for issue in issues):
        risk_level = "high"
    elif any(issue["severity"] == "medium" for issue in issues):
        risk_level = "medium"
    else:
        risk_level = "low"
    return {
        "mode": mode,
        "status": "applied",
        "risk_level": risk_level,
        "summary": _clean_model_text(payload.get("summary"), limit=220),
        "issues": issues,
    }


def _preserves_source_numeric_facts(source_item: dict[str, Any], generated_text: str) -> bool:
    generated_facts = _numeric_facts(generated_text)
    if not generated_facts:
        return True
    source_text = "\n".join(
        str(source_item.get(key, ""))
        for key in ("title", "summary")
    )
    source_facts = set(_numeric_facts(source_text))
    if not source_facts:
        return True
    source_units = {unit for _, unit in source_facts}
    for fact in generated_facts:
        value, unit = fact
        if unit in source_units and fact not in source_facts:
            return False
    return True


def _numeric_facts(text: str) -> list[tuple[int, str]]:
    facts: list[tuple[int, str]] = []
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*(千万|百万|万|亿|千|百)?\s*(万美元|亿元|万元|美元|人民币|元|%|家|台|名|人|个)",
        text,
    ):
        raw_number = match.group(1)
        multiplier = match.group(2) or ""
        unit = match.group(3)
        value = _numeric_value(raw_number)
        if value is None:
            continue
        value *= _numeric_multiplier(multiplier)
        canonical_unit = _canonical_numeric_unit(unit)
        if unit in {"万美元", "亿元", "万元"}:
            value *= _numeric_multiplier(unit[:-2] if unit.endswith("美元") else unit[:-1])
        facts.append((int(round(value)), canonical_unit))
    return facts


def _numeric_value(raw_number: str) -> float | None:
    value = raw_number.replace(",", "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    return _chinese_number_value(value)


def _numeric_multiplier(unit: str) -> int:
    return {
        "百": 100,
        "千": 1000,
        "万": 10000,
        "百万": 1000000,
        "千万": 10000000,
        "亿": 100000000,
    }.get(unit, 1)


def _canonical_numeric_unit(unit: str) -> str:
    if unit.endswith("美元"):
        return "美元"
    if unit.endswith("元") or unit == "人民币":
        return "元"
    return unit


def _chinese_number_value(value: str) -> float | None:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    section_units = {"万": 10000, "亿": 100000000}
    if not value or any(char not in {**digits, **units, **section_units} for char in value):
        return None
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        elif char in units:
            section += (number or 1) * units[char]
            number = 0
        elif char in section_units:
            section = (section + number) * section_units[char]
            total += section
            section = 0
            number = 0
    return float(total + section + number)


def _usable_detail_heading(
    value: str,
    *,
    contract: EditorialContract | None = None,
) -> bool:
    selected = contract or contract_for_article(None)
    text = str(value or "").strip()
    if len(text) < selected.detail_heading_min_chars or len(text) > selected.detail_heading_max_chars:
        return False
    if _has_banned_model_phrase(text):
        return False
    if re.fullmatch(r"[A-Za-z0-9 ._\-]+", text) and len(text.split()) <= 4:
        return False
    if text.count("【") or text.count("】"):
        return False
    return True


def _detail_heading_rejection_reason(
    value: str,
    *,
    target: dict[str, Any],
    contract: EditorialContract,
) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing_or_incomplete"
    if not _preserves_source_numeric_facts(target, text):
        return "numeric_fact_mismatch"
    if len(text) < contract.detail_heading_min_chars:
        return "heading_too_short"
    if len(text) > contract.detail_heading_max_chars:
        return "heading_too_long"
    if _has_banned_model_phrase(text):
        return "banned_phrase"
    if re.fullmatch(r"[A-Za-z0-9 ._\-]+", text) and len(text.split()) <= 4:
        return "english_fragment"
    if text.count("【") or text.count("】"):
        return "nested_brackets"
    return "contract_mismatch"


def _usable_reader_summary(
    value: str,
    *,
    contract: EditorialContract | None = None,
) -> bool:
    selected = contract or contract_for_article(None)
    text = str(value or "").strip()
    if len(text) > selected.reader_summary_output_max_chars or not reader_summary_has_publishable_depth(
        text,
        contract=selected,
    ):
        return False
    if _has_banned_model_phrase(text):
        return False
    if contains_promotional_copy(text):
        return False
    if contains_generic_editorial_filler(text):
        return False
    if not re.search(r"[。！？!?][”’\"']?$", text):
        return False
    return True


def _reader_summary_rejection_reason(
    value: str,
    *,
    target: dict[str, Any],
    contract: EditorialContract,
) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing_or_incomplete"
    if not _preserves_source_numeric_facts(target, text):
        return "numeric_fact_mismatch"
    if len(text) > contract.reader_summary_output_max_chars:
        return "too_long"
    if not reader_summary_has_publishable_depth(text, contract=contract):
        return "insufficient_depth"
    if _has_banned_model_phrase(text):
        return "banned_phrase"
    if contains_promotional_copy(text):
        return "promotional_copy"
    if contains_generic_editorial_filler(text):
        return "generic_editorial_filler"
    if not re.search(r"[。！？!?][”’\"']?$", text):
        return "incomplete_ending"
    return "contract_mismatch"


def _copy_quality_repair_indexes(
    news_items: list[Any],
    *,
    start_index: int = 1,
    item_count: int | None = None,
    indexes: Iterable[int] | None = None,
    contract: EditorialContract | None = None,
) -> list[int]:
    if indexes is None:
        upper = len(news_items) + 1 if item_count is None else start_index + item_count
        candidate_indexes = range(start_index, upper)
    else:
        candidate_indexes = indexes
    result = []
    for index in candidate_indexes:
        if index < 1 or index > len(news_items):
            continue
        item = news_items[index - 1]
        if not isinstance(item, dict):
            result.append(index)
            continue
        summary = str(item.get("reader_summary", "")).strip()
        heading = str(item.get("detail_heading", "")).strip()
        validation = item.get("copy_generation_validation", {})
        rejected = isinstance(validation, dict) and (
            validation.get("reader_summary") == "rejected"
            or validation.get("detail_heading") == "rejected"
        )
        cleaned = _drop_repeated_heading_sentence(summary, heading)
        if (
            rejected
            or not _usable_detail_heading(heading, contract=contract)
            or not _usable_reader_summary(cleaned, contract=contract)
        ):
            result.append(index)
    return result


def _has_banned_model_phrase(text: str) -> bool:
    return any(phrase and phrase in text for phrase in BANNED_PHRASES)


def _contains_news_byline(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    role_pattern = r"(^|[\s；;，,、。])(?:文|作者|记者|编辑)\s*[|丨｜:：]"
    if re.search(role_pattern, value):
        return True
    return bool(re.search(r"记者\s*[|丨｜:：]?.{0,30}编辑\s*[|丨｜:：]", value))


def _strip_news_byline(text: str) -> str:
    value = str(text or "").strip()
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


def _clean_model_text(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.strip(" \t\r\n\"'“”‘’")
    if len(text) > limit:
        text = text[:limit].rstrip("，,；;。 ")
    return text


def _clean_model_title(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.strip(" \t\r\n\"'“”‘’")
    if not text or len(text) <= limit:
        return text

    suffix = ""
    body = text
    if "丨" in text:
        body, raw_suffix = text.rsplit("丨", 1)
        suffix = f"丨{raw_suffix.strip()}"
    parts = [part.strip(" 　，,；;。") for part in re.split(r"[；;]", body) if part.strip()]
    selected: list[str] = []
    for part in parts:
        candidate = "；".join([*selected, part]) + suffix
        if len(candidate) > limit:
            break
        selected.append(part)
    if not selected:
        return ""
    return "；".join(selected) + suffix


def _clean_complete_model_text(value: Any, *, limit: int, prose: bool) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.strip(" \t\r\n\"'“”‘’")
    if len(text) <= limit and (not prose or re.search(r"[。！？!?][”’\"']?$", text)):
        return text
    minimum_cut = 60 if prose else max(12, int(limit * 0.55))
    separators = ("。", "！", "？", "!", "?") if prose else ("。", "！", "？", "；", ";", "，", ",", "：", ":")
    cuts = [text.rfind(separator, minimum_cut, min(len(text), limit) + 1) for separator in separators]
    cut = max(cuts, default=-1)
    if cut >= minimum_cut:
        candidate = text[: cut + 1] if prose else text[:cut]
        return candidate.strip(" 　，,；;：:")
    return ""


def _drop_repeated_heading_sentence(reader_summary: str, detail_heading: str) -> str:
    value = deduplicate_reader_summary(reader_summary, detail_heading)
    if detail_heading and _normalize_model_sentence(value) == _normalize_model_sentence(detail_heading):
        return ""
    return value


def _normalize_model_sentence(text: str) -> str:
    return re.sub(r"[\s。！？!?；;，,：:'\"“”‘’]", "", str(text)).lower()


def _same_section_shape(current: Any, generated: Any) -> bool:
    if not isinstance(current, list) or not isinstance(generated, list):
        return False
    if len(current) != len(generated):
        return False
    for source, target in zip(current, generated):
        if not isinstance(source, dict) or not isinstance(target, dict):
            return False
        source_paragraphs = source.get("paragraphs")
        target_paragraphs = target.get("paragraphs")
        if not isinstance(source_paragraphs, list) or not isinstance(target_paragraphs, list):
            return False
        if len(source_paragraphs) != len(target_paragraphs):
            return False
    return True


def _int_value(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
