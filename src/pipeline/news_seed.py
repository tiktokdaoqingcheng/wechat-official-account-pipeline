from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests

from src.config_loader import load_yaml
from src.pipeline.candidate_preflight import strip_promotional_tail


NEWS_SEED_SCHEMA_VERSION = "news_seed.v1"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_MAX_ITEMS = 10
DEFAULT_MAX_ITEMS_PER_SOURCE = 3
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_USER_AGENT = "wechat-official-account-automation/0.1"
DEFAULT_MINIMUM_SUCCESSFUL_SOURCES = 2
DEFAULT_FETCH_WORKERS = 6


class NewsFetchCache:
    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, url: str) -> bytes | None:
        with self._lock:
            return self._payloads.get(url)

    def put(self, url: str, payload: bytes) -> None:
        with self._lock:
            self._payloads[url] = payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch previous-day AI news and write a primary article seed.")
    parser.add_argument("--config", default="config/news-sources.yaml")
    parser.add_argument("--output-dir", default="data/news-seeds")
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = generate_news_seed(
        config_path=args.config,
        output_dir=args.output_dir,
        output_path=args.output or None,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status: {result['status']}")
        print(f"Target date: {result['target_date']}")
        print(f"Items: {result['item_count']}")
        print(f"Output: {result['output_path']}")
    return 0 if result["status"] in {"ok", "no_items"} else 1


def generate_news_seed(
    *,
    config_path: str | Path = "config/news-sources.yaml",
    output_dir: str | Path = "data/news-seeds",
    output_path: str | Path | None = None,
    fetch_cache: NewsFetchCache | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    config = load_yaml(config_path)
    target = target_news_date(config, now=current)
    seed = build_news_seed(config, target_date=target, fetch_cache=fetch_cache, now=current)

    output = Path(output_path) if output_path else Path(output_dir) / f"{target.isoformat()}-ai-news.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": seed["news_status"],
        "schema_version": seed["schema_version"],
        "target_date": seed["target_date"],
        "item_count": len(seed["news_items"]),
        "reserve_item_count": len(seed.get("reserve_news_items", [])),
        "output_path": str(output),
        "seed": seed,
        "diagnostics": seed["diagnostics"],
    }


def build_news_seed(
    config: dict[str, Any],
    *,
    target_date: date,
    fetch_cache: NewsFetchCache | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    collected = collect_news_items(config, target_date=target_date, fetch_cache=fetch_cache)
    max_items = _max_items(config)
    reserve_items = _reserve_items(config)
    items = collected["items"][:max_items]
    reserve = collected["items"][max_items : max_items + reserve_items]
    status = _news_status(config, items=items, diagnostics=collected["diagnostics"])
    sources = [_source_from_news_item(item) for item in items]
    if not sources:
        sources = [
            {
                "name": "待补充：昨日 AI 新闻源",
                "url": f"data/news-seeds/{target_date.isoformat()}-ai-news.json",
                "summary": "自动新闻源没有抓取到目标日期的可用条目，需要人工补充或调整新闻源。",
            }
        ]

    return {
        "schema_version": NEWS_SEED_SCHEMA_VERSION,
        "generated_at": current.isoformat(),
        "target_date": target_date.isoformat(),
        "topic": "昨天 AI 界发生了什么",
        "column": "AI 昨日速览",
        "audience": "对 AI 感兴趣但不熟悉技术细节的读者",
        "topic_reason": f"基于 {target_date.isoformat()} 可抓取的公开 AI 新闻源自动生成每日主文章。",
        "risk_tags": ["previous_day_ai_news", "external_sources"],
        "news_status": status,
        "news_items": items,
        "reserve_news_items": reserve,
        "sources": sources,
        "source_quality": {
            "successful_sources": _successful_source_count(collected["diagnostics"]),
            "minimum_successful_sources": _minimum_successful_sources(config),
            "event_duplicates_removed": int(collected.get("event_duplicates_removed", 0) or 0),
            "cache_hits": sum(1 for item in collected["diagnostics"] if item.get("cache_hit")),
            "source_request_seconds_total": round(
                sum(float(item.get("duration_seconds", 0) or 0) for item in collected["diagnostics"]),
                3,
            ),
            "preferred_keywords": _keyword_list(config, "preferred_keywords"),
            "blocked_keywords": _keyword_list(config, "blocked_keywords"),
        },
        "diagnostics": collected["diagnostics"],
    }


def collect_news_items(
    config: dict[str, Any],
    *,
    target_date: date,
    fetch_cache: NewsFetchCache | None = None,
) -> dict[str, Any]:
    timeout = int(config.get("request_timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS)
    user_agent = str(config.get("user_agent") or DEFAULT_USER_AGENT)
    sources = _enabled_sources(config)
    workers = _bounded_int(config.get("fetch_workers", DEFAULT_FETCH_WORKERS), DEFAULT_FETCH_WORKERS, 1, 12)
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(sources)))) as executor:
        fetched = list(
            executor.map(
                lambda source: _fetch_source_items(
                    source,
                    config=config,
                    target_date=target_date,
                    timeout=timeout,
                    user_agent=user_agent,
                    fetch_cache=fetch_cache,
                ),
                sources,
            )
        )
    items = [item for result in fetched for item in result["items"]]
    diagnostics = [result["diagnostic"] for result in fetched]
    items.sort(key=lambda item: (_news_item_score(item, config), str(item.get("published_at", ""))), reverse=True)
    deduped = _dedupe_items(items)
    return {
        "items": _limit_items_per_source(deduped, config),
        "diagnostics": diagnostics,
        "event_duplicates_removed": max(0, len(items) - len(deduped)),
    }


def _fetch_source_items(
    source: dict[str, Any],
    *,
    config: dict[str, Any],
    target_date: date,
    timeout: int,
    user_agent: str,
    fetch_cache: NewsFetchCache | None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    name = str(source.get("name", "")).strip() or "Unnamed source"
    url = str(source.get("url", "")).strip()
    if not url:
        return {
            "items": [],
            "diagnostic": {"source": name, "status": "skipped", "reason": "missing_url", "duration_seconds": 0.0},
        }
    cache_hit = False
    try:
        payload = fetch_cache.get(url) if fetch_cache is not None else None
        if payload is not None:
            cache_hit = True
        else:
            response = requests.get(
                url,
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.content
            if fetch_cache is not None:
                fetch_cache.put(url, payload)
        feed_items = parse_feed(payload, source_name=name)
        dated_items = [
            _enrich_item(item, source)
            for item in feed_items
            if _item_date(item) == target_date
            and _matches_source_required_keywords(item, source)
            and not _blocked_by_keyword(item, config)
            and not _blocked_by_promotional_content(item)
        ]
        blocked_platform_risk = [item for item in dated_items if blocked_by_wechat_platform_risk(item)]
        matched = [item for item in dated_items if not blocked_by_wechat_platform_risk(item)]
        return {
            "items": matched,
            "diagnostic": {
                "source": name,
                "status": "ok",
                "url": url,
                "cache_hit": cache_hit,
                "duration_seconds": round(time.perf_counter() - started_at, 3),
                "fetched_items": len(feed_items),
                "matched_items": len(matched),
                "blocked_wechat_platform_risk_items": len(blocked_platform_risk),
            },
        }
    except Exception as exc:
        return {
            "items": [],
            "diagnostic": {
                "source": name,
                "status": "failed",
                "url": url,
                "cache_hit": cache_hit,
                "duration_seconds": round(time.perf_counter() - started_at, 3),
                "error": str(exc),
            },
        }


def parse_feed(payload: bytes | str, *, source_name: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(payload)
    if _local_name(root.tag) == "rss":
        channel = _first_child(root, "channel")
        if channel is None:
            channel = root
        entries = _children(channel, "item")
    else:
        entries = _children(root, "entry")
        if not entries:
            entries = _children(root, "item")

    return [_entry_to_item(entry, source_name=source_name) for entry in entries]


def target_news_date(config: dict[str, Any], *, now: datetime | None = None) -> date:
    current = now or datetime.now(SHANGHAI_TZ)
    window = str(config.get("target_window", "previous_day"))
    if window == "today":
        return current.astimezone(SHANGHAI_TZ).date()
    return (current.astimezone(SHANGHAI_TZ) - timedelta(days=1)).date()


def _enabled_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    sources = config.get("sources", [])
    if not isinstance(sources, list):
        return []
    enabled = [source for source in sources if isinstance(source, dict) and source.get("enabled", True)]
    enabled.sort(key=lambda source: float(source.get("weight", 1.0) or 1.0), reverse=True)
    return enabled


def _entry_to_item(entry: ElementTree.Element, *, source_name: str) -> dict[str, Any]:
    title = _clean_text(_child_text(entry, "title"))
    url = _entry_link(entry)
    summary = _best_entry_summary(entry, title=title)
    published_at = _published_at(entry)
    item = {
        "title": title,
        "source_name": source_name,
        "url": url,
        "published_at": published_at,
        "summary": summary or title,
    }
    image_url = _entry_image_url(entry)
    if image_url:
        item["image_url"] = image_url
        item["image_source"] = "rss"
    return item


def _best_entry_summary(entry: ElementTree.Element, *, title: str) -> str:
    candidates = [
        strip_promotional_tail(_clean_text(_child_text(entry, field)))
        for field in ("description", "summary", "encoded", "content")
    ]
    candidates = [value for value in candidates if value]
    if not candidates:
        return ""
    normalized_title = _normalized_news_text(title)
    return max(
        candidates,
        key=lambda value: (
            _summary_novelty(value, normalized_title=normalized_title),
            len(value),
        ),
    )


def _summary_novelty(value: str, *, normalized_title: str) -> int:
    normalized = _normalized_news_text(value)
    if not normalized:
        return 0
    if normalized_title and normalized in normalized_title:
        return 0
    return len(normalized)


def _normalized_news_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value).lower())


def _entry_image_url(entry: ElementTree.Element) -> str:
    for child in entry.iter():
        name = _local_name(child.tag)
        if name in {"thumbnail", "content"}:
            url = str(child.attrib.get("url", "")).strip()
            if _looks_like_image_url(url):
                return url
        if name == "enclosure":
            url = str(child.attrib.get("url", "")).strip()
            media_type = str(child.attrib.get("type", "")).lower()
            if _looks_like_image_url(url) or media_type.startswith("image/"):
                return url
    html_payload = (
        _child_raw_text(entry, "description")
        or _child_raw_text(entry, "summary")
        or _child_raw_text(entry, "encoded")
        or _child_raw_text(entry, "content")
    )
    for match in re.finditer(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", html_payload, flags=re.IGNORECASE):
        url = html.unescape(match.group(1)).strip()
        if _looks_like_image_url(url):
            return url
    return ""


def _published_at(entry: ElementTree.Element) -> str:
    raw = (
        _child_text(entry, "pubDate")
        or _child_text(entry, "published")
        or _child_text(entry, "updated")
        or _child_text(entry, "date")
    )
    parsed = _parse_datetime(raw)
    return parsed.astimezone(SHANGHAI_TZ).isoformat() if parsed else ""


def _parse_datetime(value: str) -> datetime | None:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _item_date(item: dict[str, Any]) -> date | None:
    parsed = _parse_datetime(str(item.get("published_at", "")))
    return parsed.astimezone(SHANGHAI_TZ).date() if parsed else None


def _entry_link(entry: ElementTree.Element) -> str:
    link_text = _child_text(entry, "link").strip()
    if link_text:
        return link_text
    for child in entry:
        if _local_name(child.tag) == "link":
            href = str(child.attrib.get("href", "")).strip()
            if href:
                return href
    return ""


def _child_text(entry: ElementTree.Element, name: str) -> str:
    child = _first_child(entry, name)
    return "".join(child.itertext()).strip() if child is not None else ""


def _child_raw_text(entry: ElementTree.Element, name: str) -> str:
    child = _first_child(entry, name)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _first_child(entry: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in entry:
        if _local_name(child.tag) == name.lower():
            return child
    return None


def _children(entry: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    target = name.lower()
    return [child for child in entry if _local_name(child.tag) == target]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _looks_like_image_url(url: str) -> bool:
    value = url.strip().lower()
    if not value.startswith(("http://", "https://")):
        return False
    base = value.split("?", 1)[0]
    return base.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) or any(
        marker in value for marker in ("/image/", "/images/", "mmbiz.qpic.cn")
    )


def _clean_text(value: str, *, max_length: int = 260) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        return text[: max_length - 1].rstrip() + "..."
    return text


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("url") or item.get("title", "")).strip().lower()
        if not key or key in seen:
            continue
        if any(_same_news_event(item, existing) for existing in deduped):
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _same_news_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_raw_title = str(left.get("title", ""))
    right_raw_title = str(right.get("title", ""))
    left_title = _normalized_event_title(left_raw_title)
    right_title = _normalized_event_title(right_raw_title)
    if not left_title or not right_title:
        return False
    left_title_numbers = _event_numeric_tokens(left_raw_title)
    right_title_numbers = _event_numeric_tokens(right_raw_title)
    left_source = re.sub(r"\s+", "", str(left.get("source_name", ""))).lower()
    right_source = re.sub(r"\s+", "", str(right.get("source_name", ""))).lower()
    left_combined = f"{left_raw_title} {left.get('summary', '')}"
    right_combined = f"{right_raw_title} {right.get('summary', '')}"
    shared_products = _event_product_tokens(left_combined) & _event_product_tokens(right_combined)
    shared_markers = _event_markers(left_combined) & _event_markers(right_combined)
    same_source_product_event = bool(
        left_source
        and left_source == right_source
        and shared_products
        and shared_markers
    )
    if (
        left_title_numbers
        and right_title_numbers
        and left_title_numbers.isdisjoint(right_title_numbers)
        and not same_source_product_event
    ):
        return False
    shorter = min(len(left_title), len(right_title))
    if shorter >= 10 and (left_title in right_title or right_title in left_title):
        return True
    if shorter >= 12 and SequenceMatcher(None, left_title, right_title).ratio() >= 0.8:
        return True
    left_tokens = _event_tokens(left_raw_title)
    right_tokens = _event_tokens(right_raw_title)
    if len(left_tokens) >= 4 and len(right_tokens) >= 4:
        overlap = len(left_tokens & right_tokens)
        containment = overlap / min(len(left_tokens), len(right_tokens))
        if overlap >= 4 and containment >= 0.72:
            return True

    if not left_source or left_source != right_source:
        return False
    if same_source_product_event:
        return True
    shared_numbers = _event_numeric_tokens(left_combined) & _event_numeric_tokens(right_combined)
    return bool(shared_products and shared_numbers and shared_markers)


def _normalized_event_title(value: str) -> str:
    text = html.unescape(value).lower()
    text = re.sub(r"^(?:刚刚|消息称|报道称|独家|快讯)\s*[丨|:：，,\-—]*\s*", "", text)
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return text


def _event_tokens(value: str) -> set[str]:
    english = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+-]{1,}", value)
        if token not in {"the", "and", "with", "from", "for"}
    }
    chinese_runs = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    chinese = {
        run[index : index + 2]
        for run in chinese_runs
        for index in range(len(run) - 1)
        if run[index : index + 2] not in {"正式", "发布", "宣布", "消息", "公司", "今日", "最新"}
    }
    numbers = set(re.findall(r"\d+(?:\.\d+)?(?:%|亿|万|b|m)?", value))
    return english | chinese | numbers


def _event_product_tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"(?<![A-Za-z0-9])[A-Za-z]*\d+[A-Za-z0-9._+-]*(?![A-Za-z0-9])", value)
        if len(token) >= 3 and not re.fullmatch(r"20\d{2}", token)
    }


def _event_numeric_tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?:km|公里|万元|亿元|万美元|%|台|辆)?", value)
        if token not in {"2025", "2026", "2027"}
    }


def _event_markers(value: str) -> set[str]:
    lowered = str(value).lower()
    markers = (
        "发布会",
        "预售",
        "发布",
        "宣布",
        "推出",
        "融资",
        "收购",
        "召回",
        "投产",
        "签约",
        "launch",
        "announce",
        "funding",
        "acquire",
        "recall",
    )
    return {marker for marker in markers if marker in lowered}


def _max_items(config: dict[str, Any]) -> int:
    return max(1, int(config.get("max_items", DEFAULT_MAX_ITEMS) or DEFAULT_MAX_ITEMS))


def _reserve_items(config: dict[str, Any]) -> int:
    try:
        return max(0, int(config.get("reserve_items", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _max_items_per_source(config: dict[str, Any]) -> int:
    return max(1, int(config.get("max_items_per_source", DEFAULT_MAX_ITEMS_PER_SOURCE) or DEFAULT_MAX_ITEMS_PER_SOURCE))


def _minimum_successful_sources(config: dict[str, Any]) -> int:
    return max(
        1,
        int(config.get("minimum_successful_sources", DEFAULT_MINIMUM_SUCCESSFUL_SOURCES) or DEFAULT_MINIMUM_SUCCESSFUL_SOURCES),
    )


def _news_status(config: dict[str, Any], *, items: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> str:
    if not items:
        return "no_items"
    if _successful_source_count(diagnostics) < _minimum_successful_sources(config):
        return "insufficient_sources"
    return "ok"


def _successful_source_count(diagnostics: list[dict[str, Any]]) -> int:
    return sum(1 for item in diagnostics if item.get("status") == "ok" and int(item.get("matched_items", 0) or 0) > 0)


def _enrich_item(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(item)
    enriched["source_weight"] = float(source.get("weight", 1.0) or 1.0)
    enriched["source_tier"] = str(source.get("tier", "")).strip()
    enriched["source_url"] = str(source.get("url", "")).strip()
    enriched["score"] = enriched["source_weight"]
    return enriched


def _news_item_score(item: dict[str, Any], config: dict[str, Any]) -> float:
    score = float(item.get("source_weight", 1.0) or 1.0) * 100
    combined = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    for keyword in _keyword_list(config, "preferred_keywords"):
        if keyword.lower() in combined:
            score += 20
    for keyword in _keyword_list(config, "priority_keywords"):
        if keyword.lower() in combined:
            score += 60
    if str(item.get("source_tier", "")).lower() == "official":
        score += 25
    return score


def _blocked_by_keyword(item: dict[str, Any], config: dict[str, Any]) -> bool:
    combined = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return any(keyword.lower() in combined for keyword in _keyword_list(config, "blocked_keywords"))


def _blocked_by_promotional_content(item: dict[str, Any]) -> bool:
    combined = f"{item.get('title', '')} {item.get('summary', '')} {item.get('source_name', '')}".lower()
    if "欢迎关注" in combined and ("公众号" in combined or "微信号" in combined or "微信" in combined):
        return True
    if "更多精彩内容" in combined and ("公众号" in combined or "微信号" in combined or "微信" in combined):
        return True
    promotional_terms = (
        "#欢迎关注",
        "官方微信公众号",
        "微信号：",
        "微信号:",
        "第一时间为您奉上",
    )
    return any(term.lower() in combined for term in promotional_terms)


def blocked_by_wechat_platform_risk(item: dict[str, Any]) -> bool:
    combined = (
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('source_name', '')} "
        f"{item.get('detail_heading', '')} {item.get('reader_summary', '')}"
    ).lower()
    fraud_terms = ("诈骗", "fraud", "scam", "网络犯罪", "犯罪团伙", "criminal gang", "cybercrime")
    if any(term in combined for term in fraud_terms):
        scale_or_target_terms = (
            "受害者",
            "受害人",
            "受害",
            "数十万",
            "数百万",
            "百万",
            "250万",
            "短信",
            "sms",
            "中国",
            "chinese",
            "团伙",
            "gang",
        )
        legal_or_action_terms = ("起诉", "诉讼", "指控", "涉嫌", "利用ai", "利用 ai", "ai-generated", "大规模")
        if any(term in combined for term in scale_or_target_terms) and any(
            term in combined for term in legal_or_action_terms
        ):
            return True

    cyber_event_terms = (
        "网络攻击",
        "黑客",
        "被黑",
        "勒索软件",
        "ransomware",
        "暗网",
        "cyberattack",
        "data breach",
    )
    leak_action_terms = (
        "被窃",
        "窃取",
        "泄露",
        "leak",
        "leaked",
        "stolen",
    )
    confidential_terms = (
        "机密",
        "敏感数据",
        "内部资料",
        "设计图纸",
        "未发布",
        "新品",
        "iphone",
        "芯片资料",
        "数据手册",
        "供应链",
        "代工厂",
    )
    scale_terms = ("gb", "tb", "文件", "超过", "大量", "20 万", "20万", "630gb")
    if any(term in combined for term in cyber_event_terms) and any(
        term in combined for term in leak_action_terms
    ) and any(term in combined for term in confidential_terms):
        return True
    if any(term in combined for term in leak_action_terms) and any(
        term in combined for term in confidential_terms
    ) and any(term in combined for term in scale_terms):
        return True

    accusation_terms = ("虚假", "假的", "造假", "错误百出", "不存在", "子虚乌有")
    exposure_terms = ("被查出", "被指", "调查", "抓包", "点名", "证实", "核查")
    reputation_context_terms = ("报告", "案例", "脚注", "公司", "机构", "事务所", "企业")
    return (
        any(term in combined for term in accusation_terms)
        and any(term in combined for term in exposure_terms)
        and any(term in combined for term in reputation_context_terms)
    )


def _limit_items_per_source(items: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    limit = _max_items_per_source(config)
    counts: dict[str, int] = {}
    limited: list[dict[str, Any]] = []
    for item in items:
        source = str(item.get("source_name", "")).strip() or "unknown"
        count = counts.get(source, 0)
        if count >= limit:
            continue
        counts[source] = count + 1
        limited.append(item)
    return limited


def _matches_source_required_keywords(item: dict[str, Any], source: dict[str, Any]) -> bool:
    keywords = _keyword_list(source, "required_keywords")
    if not keywords:
        return True
    combined = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return any(keyword.lower() in combined for keyword in keywords)


def _keyword_list(config: dict[str, Any], key: str) -> list[str]:
    values = config.get(key, [])
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


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


if __name__ == "__main__":
    raise SystemExit(main())
