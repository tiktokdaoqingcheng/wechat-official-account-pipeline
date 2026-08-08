from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_MEMORY_FILENAME = "recent-angles.jsonl"


def memory_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / DEFAULT_MEMORY_FILENAME


def load_recent_angles(
    path: str | Path,
    *,
    days: int = 21,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    memory_file = Path(path)
    if not memory_file.exists():
        return []

    current = now or datetime.now(SHANGHAI_TZ)
    cutoff = current.astimezone(SHANGHAI_TZ) - timedelta(days=max(1, days))
    entries: list[dict[str, Any]] = []
    for line in memory_file.read_text(encoding="utf-8-sig").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        created_at = _parse_datetime(str(entry.get("created_at", "")))
        if created_at and created_at.astimezone(SHANGHAI_TZ) < cutoff:
            continue
        entries.append(entry)
    return entries


def is_recent_angle(
    candidate: str,
    recent_angles: list[dict[str, Any]],
    *,
    threshold: float = 0.72,
) -> bool:
    normalized = _normalize(candidate)
    if not normalized:
        return False
    for entry in recent_angles:
        for key in ("topic", "title"):
            remembered = _normalize(str(entry.get(key, "")))
            if not remembered:
                continue
            if normalized == remembered or normalized in remembered or remembered in normalized:
                return True
            if _bigram_similarity(normalized, remembered) >= threshold:
                return True
    return False


def record_angles(
    state_dir: str | Path,
    entries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> Path:
    current = now or datetime.now(SHANGHAI_TZ)
    path = memory_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            title = str(entry.get("title", "")).strip()
            topic = str(entry.get("topic", "")).strip()
            if not title and not topic:
                continue
            payload = {
                "created_at": current.isoformat(),
                "role": str(entry.get("role", "")).strip(),
                "title": title,
                "topic": topic,
                "source": str(entry.get("source", "")).strip(),
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def _normalize(value: str) -> str:
    text = value.lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return text


def _bigram_similarity(left: str, right: str) -> float:
    left_grams = _bigrams(left)
    right_grams = _bigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    overlap = len(left_grams & right_grams)
    return overlap / max(len(left_grams), len(right_grams))


def _bigrams(value: str) -> set[str]:
    if len(value) <= 1:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed
