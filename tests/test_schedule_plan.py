from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src.pipeline.content_engine import build_article
from src.review.publish_policy import decide_publish_action
from src.review.review_engine import review_article
from src.scheduler.schedule_plan import SCHEDULE_SCHEMA_VERSION, build_schedule_plan, validate_schedule_plan


class SchedulePlanTest(unittest.TestCase):
    def test_build_schedule_plan_outputs_schema_v1(self) -> None:
        schedule = {
            "timezone": "Asia/Shanghai",
            "daily_run": {
                "enabled": True,
                "topic_time": "06:00",
                "draft_time": "06:10",
                "image_time": "06:25",
                "review_time": "06:40",
                "draft_upload_time": "06:45",
                "publish_time": "09:00",
            },
            "publishing": {
                "mode": "dry_run",
                "allow_low_risk_auto_publish": False,
            },
        }
        article = build_article(
            {
                "topic": "AI 自动化入门",
                "sources": [
                    {
                        "name": "OpenAI 文档",
                        "url": "https://platform.openai.com/docs",
                        "summary": "用于测试的外部来源。",
                    }
                ],
            }
        )
        review = review_article(article, {"sensitive_terms": ["荐股"]})
        decision = decide_publish_action(schedule, review)

        plan = build_schedule_plan(
            schedule,
            article=article,
            review=review,
            publish_decision=decision,
            files={
                "article_json": "outputs/article.json",
                "article_md": "outputs/article.md",
                "review_json": "outputs/review.json",
                "publish_decision_json": "outputs/publish-decision.json",
                "cover": "outputs/cover.png",
            },
            seed_source="config/topics.yaml",
            now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        self.assertEqual(plan["schema_version"], SCHEDULE_SCHEMA_VERSION)
        self.assertEqual(plan["publishing_mode"], "dry_run")
        self.assertEqual(plan["next_action"], "none")
        self.assertEqual(plan["article"]["title"], article["title"])
        self.assertEqual([stage["stage"] for stage in plan["timeline"]], [
            "topic",
            "article",
            "cover",
            "review",
            "draft_upload",
            "publish",
        ])
        self.assertEqual(plan["timeline"][0]["scheduled_at"], "2026-06-03T06:00:00+08:00")
        self.assertEqual(plan["timeline"][3]["status"], "completed")
        self.assertEqual(plan["timeline"][4]["status"], "skipped")
        self.assertEqual(validate_schedule_plan(plan), [])

    def test_schedule_plan_blocks_existing_publish_lock(self) -> None:
        schedule = {
            "publishing": {
                "mode": "auto_publish_low_risk",
                "allow_low_risk_auto_publish": True,
            }
        }
        article = build_article(
            {
                "topic": "AI 自动化入门",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        review = review_article(article, {"sensitive_terms": ["荐股"]})
        decision = decide_publish_action(schedule, review, already_published_today=True)

        plan = build_schedule_plan(
            schedule,
            article=article,
            review=review,
            publish_decision=decision,
            files={},
            already_published_today=True,
        )

        self.assertEqual(plan["next_action"], "blocked")
        self.assertIn("publish lock", plan["blocker"])
        self.assertEqual(plan["timeline"][-1]["status"], "blocked")
        self.assertEqual(validate_schedule_plan(plan), [])

    def test_validate_schedule_plan_detects_missing_fields(self) -> None:
        errors = validate_schedule_plan({"schema_version": SCHEDULE_SCHEMA_VERSION})

        self.assertTrue(any("generated_at is required" in error for error in errors))
        self.assertTrue(any("timeline must be a non-empty list" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
