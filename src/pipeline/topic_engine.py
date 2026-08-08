from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_yaml
from src.pipeline.angle_memory import is_recent_angle, load_recent_angles


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_AUDIENCE = "对 AI 感兴趣但专业知识了解不多的读者"


def main() -> int:
    parser = argparse.ArgumentParser(description="Select a topic seed from local topic configuration.")
    parser.add_argument("--topics", default="config/topics.yaml")
    parser.add_argument("--output", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--memory", default="")
    parser.add_argument("--recent-days", type=int, default=21)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    current = _date_from_arg(args.date)
    seed = select_topic_seed(
        args.topics,
        now=current,
        memory_path=args.memory,
        recent_days=args.recent_days,
    )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(seed, ensure_ascii=False, indent=2))
    else:
        print(f"Date: {seed['date']}")
        print(f"Column: {seed.get('column', '')}")
        print(f"Topic: {seed['topic']}")
        if args.output:
            print(f"Output: {args.output}")
    return 0


def select_topic_seed(
    topics_path: str | Path = "config/topics.yaml",
    *,
    now: datetime | None = None,
    memory_path: str | Path = "",
    recent_days: int = 21,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    config = load_yaml(topics_path)
    temporary = _temporary_topic_seed(config, topics_path, current=current)
    if temporary:
        return temporary

    candidates = _topic_candidates(config)
    if not candidates:
        raise ValueError("No enabled topic candidates found in topics config.")

    blocked = {str(item).strip() for item in config.get("blocked_topics", []) if str(item).strip()}
    recent_angles = load_recent_angles(memory_path, days=recent_days, now=current) if memory_path else []
    allowed = [
        candidate
        for candidate in candidates
        if not _contains_blocked_text(candidate["topic"], blocked)
        and not is_recent_angle(candidate["topic"], recent_angles)
    ]
    if not allowed and recent_angles:
        allowed = [
            candidate
            for candidate in candidates
            if not _contains_blocked_text(candidate["topic"], blocked)
        ]
    if not allowed:
        raise ValueError("All topic candidates are blocked by blocked_topics.")

    selected = allowed[_stable_index([item["topic"] for item in allowed], current)]
    return {
        "date": current.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d"),
        "topic": selected["topic"],
        "column": selected.get("column", ""),
        "audience": selected.get("audience") or str(config.get("audience") or DEFAULT_AUDIENCE),
        "topic_reason": selected.get("reason", "来自本地选题池的稳定轮换。"),
        "risk_tags": selected.get("risk_tags", []),
        "sources": selected.get("sources") or _default_sources(topics_path),
    }


def _temporary_topic_seed(
    config: dict[str, Any],
    topics_path: str | Path,
    *,
    current: datetime,
) -> dict[str, Any] | None:
    temporary = config.get("temporary_topic", {})
    if not isinstance(temporary, dict) or not temporary.get("enabled", False):
        return None

    expires_on = str(temporary.get("expires_on", "")).strip()
    if expires_on:
        try:
            if datetime.strptime(expires_on, "%Y-%m-%d").date() < current.astimezone(SHANGHAI_TZ).date():
                return None
        except ValueError:
            return None

    topic = str(temporary.get("topic", "")).strip()
    if not topic:
        return None
    return {
        "date": current.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d"),
        "topic": topic,
        "column": str(temporary.get("column", "")).strip() or str(config.get("default_column", "")).strip(),
        "audience": str(temporary.get("audience", "")).strip()
        or str(config.get("audience") or DEFAULT_AUDIENCE),
        "topic_reason": str(temporary.get("reason", "")).strip() or "来自临时选题覆盖配置。",
        "risk_tags": temporary.get("risk_tags", []) if isinstance(temporary.get("risk_tags", []), list) else [],
        "sources": temporary.get("sources") if isinstance(temporary.get("sources"), list) else _default_sources(topics_path),
    }


def _topic_candidates(config: dict[str, Any]) -> list[dict[str, Any]]:
    structured = config.get("topic_pool", [])
    candidates: list[dict[str, Any]] = []
    if isinstance(structured, list):
        for item in structured:
            if not isinstance(item, dict):
                continue
            if item.get("enabled", True) is False:
                continue
            topic = str(item.get("topic", "")).strip()
            if topic:
                candidates.append(
                    {
                        "topic": topic,
                        "column": str(item.get("column", "")).strip(),
                        "audience": str(item.get("audience", "")).strip(),
                        "reason": str(item.get("reason", "")).strip(),
                        "risk_tags": item.get("risk_tags", []) if isinstance(item.get("risk_tags", []), list) else [],
                        "sources": item.get("sources", []) if isinstance(item.get("sources", []), list) else [],
                    }
                )

    if candidates:
        return candidates

    columns = _enabled_columns(config)
    legacy_topics = config.get("seed_topics", [])
    if not isinstance(legacy_topics, list):
        return []
    for index, raw_topic in enumerate(legacy_topics):
        topic = str(raw_topic).strip()
        if not topic:
            continue
        column = columns[index % len(columns)] if columns else ""
        candidates.append(
            {
                "topic": topic,
                "column": column,
                "reason": "来自 seed_topics 旧格式选题池。",
                "risk_tags": [],
                "sources": [],
            }
        )
    return candidates


def _enabled_columns(config: dict[str, Any]) -> list[str]:
    columns = config.get("fixed_columns", [])
    if not isinstance(columns, list):
        return []
    result = []
    for item in columns:
        if not isinstance(item, dict):
            continue
        if item.get("enabled", True) is False:
            continue
        name = str(item.get("name", "")).strip()
        if name:
            result.append(name)
    return result


def _stable_index(values: list[str], current: datetime) -> int:
    date_text = current.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d")
    digest = hashlib.sha256((date_text + "|" + "|".join(values)).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % len(values)


def _contains_blocked_text(topic: str, blocked: set[str]) -> bool:
    return any(item and item in topic for item in blocked)


def _default_sources(topics_path: str | Path) -> list[dict[str, str]]:
    return [
        {
            "name": "本地选题配置",
            "url": str(topics_path),
            "summary": "该选题来自本地配置；正式发布前需要补充最新事实来源。",
        }
    ]


def _date_from_arg(value: str) -> datetime:
    if not value:
        return datetime.now(SHANGHAI_TZ)
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return parsed.replace(tzinfo=SHANGHAI_TZ)


if __name__ == "__main__":
    raise SystemExit(main())
