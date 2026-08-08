from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.analytics.run_summary import build_run_summary, render_markdown, write_summary_report
from src.storage.local_db import record_daily_run


class RunSummaryTest(unittest.TestCase):
    def test_build_run_summary_counts_recent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "app.sqlite"

            record_daily_run(
                {
                    "status": "ok",
                    "business_outcome": "published",
                    "created_at": "2026-06-03T08:00:00+08:00",
                    "publishing_mode": "dry_run",
                    "next_action": "none",
                    "reason": "dry run",
                    "output_dir": str(root / "run-1"),
                    "stages": [
                        {
                            "name": "content_package",
                            "title": "first",
                            "risk_level": "low",
                            "publish_action": "dry_run",
                            "output_dir": str(root / "run-1" / "content-package"),
                        }
                    ],
                },
                db_path,
            )
            record_daily_run(
                {
                    "status": "blocked",
                    "created_at": "2026-06-02T08:00:00+08:00",
                    "publishing_mode": "auto_publish_low_risk",
                    "next_action": "blocked",
                    "reason": "blocked",
                    "output_dir": str(root / "run-2"),
                    "stages": [],
                },
                db_path,
            )

            summary = build_run_summary(
                db_path,
                days=7,
                now=datetime(2026, 6, 3, 9, 0, tzinfo=timezone(timedelta(hours=8))),
            )

        self.assertEqual(summary["totals"]["runs"], 2)
        self.assertEqual(summary["totals"]["ok"], 1)
        self.assertEqual(summary["totals"]["published"], 1)
        self.assertEqual(summary["totals"]["blocked"], 1)
        self.assertEqual(summary["next_action_counts"]["blocked"], 1)
        self.assertEqual(summary["business_outcome_counts"]["published"], 1)

    def test_render_and_write_summary_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = {
                "generated_at": "2026-06-03T09:00:00+08:00",
                "window": {"days": 7, "start_date": "2026-05-28", "end_date": "2026-06-03"},
                "totals": {"runs": 1, "ok": 1, "blocked": 0, "failed": 0, "success_rate": 1.0},
                "publishing_mode_counts": {"dry_run": 1},
                "next_action_counts": {"none": 1},
                "business_outcome_counts": {"published": 1},
                "risk_level_counts": {"low": 1},
                "latest_runs": [
                    {
                        "created_at": "2026-06-03T08:00:00+08:00",
                        "status": "ok",
                        "publishing_mode": "dry_run",
                        "next_action": "none",
                        "title": "first",
                    }
                ],
                "recommendations": ["keep going"],
            }

            markdown = render_markdown(summary)
            files = write_summary_report(Path(temp_dir), summary)

            self.assertIn("自动化运行复盘", markdown)
            self.assertTrue(Path(files["json"]).exists())
            self.assertTrue(Path(files["markdown"]).exists())


if __name__ == "__main__":
    unittest.main()
