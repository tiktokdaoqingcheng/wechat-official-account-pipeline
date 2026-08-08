from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

WEEKDAY_TEMPLATE_FILES = {
    0: "tech-briefing-monday.png",
    1: "tech-briefing-tuesday.png",
    2: "tech-briefing-wednesday.png",
    3: "tech-briefing-thursday.png",
    4: "tech-briefing-friday.png",
    5: "tech-briefing-saturday.png",
    6: "tech-briefing-sunday.png",
}


def weekly_template_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "covers" / "weekly-tech-briefing"


def weekly_template_path(now: datetime | None = None, *, template_dir: str | Path | None = None) -> Path:
    current = now or datetime.now(SHANGHAI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)
    current = current.astimezone(SHANGHAI_TZ)
    root = Path(template_dir) if template_dir else weekly_template_dir()
    return root / WEEKDAY_TEMPLATE_FILES[current.weekday()]


def write_weekly_briefing_cover(
    output_path: str | Path,
    *,
    now: datetime | None = None,
    template_dir: str | Path | None = None,
) -> dict[str, Any]:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    template = weekly_template_path(now, template_dir=template_dir)
    if not template.exists():
        raise FileNotFoundError(f"Weekly briefing cover template is missing: {template}")
    shutil.copyfile(template, destination)
    return {
        "provider": "local_template",
        "status": "weekly_briefing_template",
        "path": str(destination),
        "template": str(template),
        "weekday": template.stem.rsplit("-", 1)[-1],
    }
