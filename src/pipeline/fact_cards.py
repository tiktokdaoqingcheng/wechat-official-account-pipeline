from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from src.pipeline.candidate_preflight import (
    PROMOTIONAL_MARKERS,
    deterministic_candidate_verdict,
    strip_promotional_tail,
)


PROMOTIONAL_PHRASES = PROMOTIONAL_MARKERS


def attach_fact_cards(article: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(article)
    items = updated.get("news_items", [])
    if not isinstance(items, list):
        return updated

    digest = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        item["summary"] = _clean_source_text(str(item.get("summary", "")))
        card = build_fact_card(item)
        item["fact_card"] = card
        if not isinstance(item.get("candidate_verdict"), dict):
            item["candidate_verdict"] = deterministic_candidate_verdict(item)
        digest.append(
            {
                "index": index,
                "subject": card["subject"],
                "action": card["action"],
                "confidence": card["confidence"],
                "source_name": card["source_name"],
            }
        )
    updated["fact_cards"] = digest
    return updated


def build_fact_card(item: dict[str, Any]) -> dict[str, Any]:
    title = _clean_source_text(str(item.get("title", "")))
    summary = _clean_source_text(str(item.get("summary", "")))
    sentences = _sentences(title, summary)
    facts = sentences[:5]
    subject = _subject_from_title(title)
    action = facts[0] if facts else title
    confidence = "high" if summary and len(facts) >= 2 and item.get("url") else "medium"
    if not facts or not item.get("source_name"):
        confidence = "low"
    return {
        "subject": subject,
        "action": action[:120],
        "facts": facts,
        "numbers": _numbers("\n".join((title, summary))),
        "published_at": str(item.get("published_at", "")).strip(),
        "source_name": str(item.get("source_name", "")).strip(),
        "source_url": str(item.get("url") or item.get("source_url") or "").strip(),
        "confidence": confidence,
        "uncertainties": [] if confidence == "high" else ["source detail is limited"],
    }


def fact_card_prompt_payload(item: dict[str, Any]) -> dict[str, Any]:
    card = item.get("fact_card", {})
    return card if isinstance(card, dict) else build_fact_card(item)


def _clean_source_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    text = re.sub(
        r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+\d{2}:\d{2})?|\d{1,2}月\d{1,2}日)\s*[；;，,、]\s*",
        "",
        text,
    )
    text = re.sub(r"^(?:作者|文|记者|编辑)\s*[|丨｜:：].{0,80}?(?=(?:\d{1,2}月\d{1,2}日|[。！？]))", "", text)
    return strip_promotional_tail(text)


def _sentences(title: str, summary: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in (title, *re.split(r"(?<=[。！？!?；;])\s*", summary)):
        value = raw.strip(" 　；;，,。")
        if len(value) < 6 or any(phrase in value for phrase in PROMOTIONAL_PHRASES):
            continue
        key = re.sub(r"\W+", "", value).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value[:220])
    return result


def _subject_from_title(title: str) -> str:
    value = re.split(r"[：:；;，,。|丨｜]", title, maxsplit=1)[0].strip()
    if len(value) > 36:
        value = value[:36].rstrip(" 　；;，,。")
    return value or "未明确主体"


def _numbers(text: str) -> list[str]:
    values = re.findall(
        r"(?<![A-Za-z0-9])(?:\d+(?:,\d{3})*(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)\s*(?:%|亿美元|亿元|万美元|万元|美元|人民币|元|亿|万|家|台|名|人|个|公里|km)?",
        text,
        flags=re.IGNORECASE,
    )
    result: list[str] = []
    for value in values:
        normalized = re.sub(r"\s+", "", value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result[:12]
