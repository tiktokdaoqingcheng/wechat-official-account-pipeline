from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


PREFLIGHT_REJECTION_CODES = {
    "not_news",
    "promotional",
    "missing_subject_action",
    "insufficient_facts",
    "off_topic",
    "corrupted_source",
}
MIN_INCREMENTAL_SOURCE_CHARS = 32
PROMOTIONAL_MARKERS = (
    "欢迎关注",
    "微信公众号",
    "微信号",
    "更多精彩内容",
    "第一时间为您奉上",
    "点击查看原文",
)


def deterministic_candidate_verdict(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title", "")).strip()
    summary = str(item.get("summary", "")).strip()
    if any(marker in title for marker in PROMOTIONAL_MARKERS):
        return {
            "usable": False,
            "reason_code": "promotional",
            "reason": "来源标题是公众号导流或订阅广告。",
        }
    factual_summary = strip_promotional_tail(summary)
    normalized_title = _normalized_source_text(title)
    normalized_summary = _normalized_source_text(factual_summary)
    if not normalized_title:
        return {
            "usable": False,
            "reason_code": "missing_subject_action",
            "reason": "来源没有可识别的新闻标题。",
        }
    if (
        not normalized_summary
        or len(normalized_summary) < MIN_INCREMENTAL_SOURCE_CHARS
        or _source_summary_echoes_title(normalized_title, normalized_summary)
    ):
        return {
            "usable": False,
            "reason_code": "insufficient_facts",
            "reason": "来源摘要没有提供足够的标题外事实，无法安全扩写成新闻正文。",
        }
    return {"usable": True, "reason_code": "", "reason": ""}


def strip_promotional_tail(value: str) -> str:
    text = str(value or "").strip()
    indexes = [text.find(marker) for marker in PROMOTIONAL_MARKERS if marker in text]
    if indexes:
        text = text[: min(indexes)]
    return text.strip(" \u3000#")


def apply_candidate_verdict(target: dict[str, Any], verdict: Any, *, clean_reason: Any) -> bool:
    if not isinstance(verdict, dict):
        return False
    usable = verdict.get("usable")
    if not isinstance(usable, bool):
        return False
    target["candidate_verdict"] = {
        "usable": usable,
        "reason_code": str(verdict.get("reason_code", "")).strip().lower(),
        "reason": clean_reason(verdict.get("reason"), limit=160),
    }
    return True


def drop_rejected_candidates(article: dict[str, Any]) -> dict[str, Any]:
    items = article.get("news_items", [])
    if not isinstance(items, list):
        return {"rejected": 0, "items": []}
    kept: list[Any] = []
    rejected: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        verdict = item.get("candidate_verdict", {}) if isinstance(item.get("candidate_verdict", {}), dict) else {}
        reason_code = str(verdict.get("reason_code", "")).strip().lower()
        if verdict.get("usable") is False and reason_code in PREFLIGHT_REJECTION_CODES:
            rejected.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "source_name": str(item.get("source_name", "")).strip(),
                    "reason_code": reason_code,
                    "reason": str(verdict.get("reason", "")).strip(),
                }
            )
            continue
        kept.append(item)
    if rejected:
        article["news_items"] = kept
        previous = article.get("candidate_preflight", {})
        previous_items = previous.get("items", []) if isinstance(previous, dict) else []
        combined_rejections = [
            *[item for item in previous_items if isinstance(item, dict)],
            *rejected,
        ]
        article["candidate_preflight"] = {
            "status": "rejected_items",
            "rejected": len(combined_rejections),
            "items": combined_rejections,
        }
        article["fact_cards"] = [
            {
                "index": index,
                "subject": str(card.get("subject", "")),
                "action": str(card.get("action", "")),
                "confidence": str(card.get("confidence", "")),
                "source_name": str(item.get("source_name", "")),
            }
            for index, item in enumerate(kept, start=1)
            if isinstance(item, dict)
            and isinstance((card := item.get("fact_card", {})), dict)
        ]
    return {"rejected": len(rejected), "items": rejected}


def _normalized_source_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value.lower())


def _source_summary_echoes_title(title: str, summary: str) -> bool:
    if title == summary or summary in title:
        return True
    shorter = min(len(title), len(summary))
    if shorter < 16:
        return False
    return SequenceMatcher(None, title, summary).ratio() >= 0.92
