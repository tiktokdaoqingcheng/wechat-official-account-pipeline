from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline.daily_dry_run import generate_dry_run_package  # noqa: E402


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
EXAMPLE_DIR = ROOT / "examples" / "synthetic"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete pipeline with offline synthetic inputs.")
    parser.add_argument("--output-dir", default="demo-output")
    parser.add_argument("--date", default="", help="Run date in YYYY-MM-DD format; defaults to today in Asia/Shanghai.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = _run_datetime(args.date)
    result = run_synthetic_demo(Path(args.output_dir), now=now)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Synthetic dry run completed")
        print(f"Primary: {result['title']}")
        print(f"Secondary: {result['secondary_title']}")
        print(f"HTML preview: {result['files']['article_html']}")
        print(f"Result: {Path(result['output_dir']) / 'dry-run-result.json'}")
    return 0


def run_synthetic_demo(output_dir: Path, *, now: datetime) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    input_dir = output_dir / "inputs"
    run_dir = output_dir / "run"
    state_dir = output_dir / "state"
    input_dir.mkdir(parents=True, exist_ok=True)

    target_date = (now.astimezone(SHANGHAI_TZ) - timedelta(days=1)).date().isoformat()
    primary_path = input_dir / "primary-news.json"
    secondary_path = input_dir / "manufacturing-news.json"
    _write_dated_seed(EXAMPLE_DIR / "primary-news.json", primary_path, target_date=target_date, now=now)
    _write_dated_seed(EXAMPLE_DIR / "manufacturing-news.json", secondary_path, target_date=target_date, now=now)

    result = generate_dry_run_package(
        seed_path=EXAMPLE_DIR / "topic.json",
        topics_path=ROOT / "config" / "topics.yaml",
        schedule_path=EXAMPLE_DIR / "schedule.yaml",
        safety_path=ROOT / "config" / "safety-rules.yaml",
        news_sources_path=ROOT / "config" / "news-sources.yaml",
        news_seed_path=primary_path,
        manufacturing_news_sources_path=ROOT / "config" / "manufacturing-news-sources.yaml",
        manufacturing_news_seed_path=secondary_path,
        fetch_news=False,
        state_dir=state_dir,
        env_path=ROOT / ".env.example",
        generate_images=False,
        output_dir=run_dir,
        now=now,
    )
    if result.get("status") != "ok" or result.get("published") is not False:
        raise RuntimeError("Synthetic demo did not finish as a non-publishing dry run.")
    return result


def _write_dated_seed(template_path: Path, output_path: Path, *, target_date: str, now: datetime) -> None:
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    payload["target_date"] = target_date
    payload["generated_at"] = now.isoformat()
    for index, item in enumerate(payload.get("news_items", []), start=1):
        item["published_at"] = f"{target_date}T{8 + index:02d}:00:00+08:00"
    for index, item in enumerate(payload.get("reserve_news_items", []), start=1):
        item["published_at"] = f"{target_date}T{18 + index:02d}:00:00+08:00"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_datetime(value: str) -> datetime:
    if not value:
        return datetime.now(SHANGHAI_TZ)
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return parsed.replace(hour=8, tzinfo=SHANGHAI_TZ)


if __name__ == "__main__":
    raise SystemExit(main())
