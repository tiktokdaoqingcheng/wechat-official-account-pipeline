from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from src.storage.local_db import initialize, recent_runs, record_daily_run, record_event


class LocalDbTest(unittest.TestCase):
    def test_record_daily_run_and_recent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_dir = root / "package"
            package_dir.mkdir()
            (package_dir / "dry-run-result.json").write_text(
                """
                {
                  "files": {
                    "article_json": "article.json",
                    "review_json": "review.json"
                  }
                }
                """,
                encoding="utf-8",
            )
            db_path = root / "app.sqlite"

            run_id = record_daily_run(
                {
                    "status": "ok",
                    "business_outcome": "prepared",
                    "run_stage": "prepare",
                    "created_at": "2026-06-03T08:00:00+08:00",
                    "publishing_mode": "dry_run",
                    "next_action": "none",
                    "reason": "dry run",
                    "output_dir": str(root / "run"),
                    "stages": [
                        {
                            "name": "content_package",
                            "title": "test title",
                            "risk_level": "low",
                            "publish_action": "dry_run",
                            "output_dir": str(package_dir),
                            "duration_seconds": 12.5,
                            "resumed": False,
                        }
                    ],
                    "model_calls": [
                        {
                            "role": "summary",
                            "model": "gemini-test",
                            "attempt": 1,
                            "outcome": "ok",
                            "duration_seconds": 1.25,
                            "timeout_seconds": 30,
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                        }
                    ],
                },
                db_path,
            )

            runs = recent_runs(db_path, limit=5)
            connection = sqlite3.connect(db_path)
            stage_count = connection.execute("SELECT COUNT(*) FROM run_stages").fetchone()[0]
            model_call = connection.execute(
                "SELECT role, model, total_tokens FROM model_calls"
            ).fetchone()
            connection.close()

        self.assertEqual(run_id, 1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["title"], "test title")
        self.assertEqual(runs[0]["next_action"], "none")
        self.assertEqual(runs[0]["business_outcome"], "prepared")
        self.assertEqual(runs[0]["run_stage"], "prepare")
        self.assertEqual(stage_count, 1)
        self.assertEqual(model_call, ("summary", "gemini-test", 15))

    def test_initialize_and_record_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "app.sqlite"

            initialize(db_path)
            event_id = record_event("test_event", {"ok": True}, db_path)

        self.assertEqual(event_id, 1)

    def test_record_daily_run_stores_published_articles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "app.sqlite"

            record_daily_run(
                {
                    "status": "ok",
                    "created_at": "2026-06-03T10:30:00+08:00",
                    "publishing_mode": "auto_publish_low_risk",
                    "next_action": "publish_submitted",
                    "publish_id": "publish-id",
                    "output_dir": str(root / "run"),
                    "published_articles": [
                        {
                            "index": "0",
                            "title": "first",
                            "article_id": "a1",
                            "url": "https://example.invalid/articles/one",
                        }
                    ],
                    "stages": [],
                },
                db_path,
            )

            runs = recent_runs(db_path, limit=5)

        self.assertEqual(runs[0]["published_articles"][0]["url"], "https://example.invalid/articles/one")


if __name__ == "__main__":
    unittest.main()
