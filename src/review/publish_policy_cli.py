from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from src.config_loader import load_layered_yaml
from src.review.publish_policy import decide_publish_action


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate publish policy for a review report.")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--review", required=True)
    parser.add_argument("--already-published-today", action="store_true")
    args = parser.parse_args()

    schedule = load_layered_yaml(args.schedule)
    review_path = Path(args.review)
    review = json.loads(review_path.read_text(encoding="utf-8-sig"))

    decision = decide_publish_action(
        schedule,
        review,
        already_published_today=args.already_published_today,
    )
    print(json.dumps(asdict(decision), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
