from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.monitoring.publish_watchdog import evaluate_publish_outcome, refresh_pending_publish
from src.monitoring.status_dashboard import build_operations_status


TZ = timezone(timedelta(hours=8))


class PublishWatchdogTest(unittest.TestCase):
    def test_published_run_requires_result_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_root = root / "outputs"
            state_dir = root / "state"
            run_dir = output_root / "2026-07-10-daily-run"
            publish_dir = run_dir / "wechat-publish"
            publish_dir.mkdir(parents=True)
            (state_dir / "published").mkdir(parents=True)
            (state_dir / "published" / "2026-07-10.json").write_text("{}", encoding="utf-8")
            publish_path = publish_dir / "publish-result.json"
            publish_path.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "published": True,
                        "status_checks": [{"normalized_status": "success"}],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "daily-run-result.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "next_action": "publish_submitted",
                        "published": True,
                        "publish_result": str(publish_path),
                        "publish_channel": "mass_send_all",
                    }
                ),
                encoding="utf-8",
            )

            result = evaluate_publish_outcome(
                date(2026, 7, 10),
                output_root=output_root,
                state_dir=state_dir,
                now=datetime(2026, 7, 10, 10, 40, tzinfo=TZ),
            )

        self.assertEqual(result["business_outcome"], "published")

    def test_blocked_business_result_is_not_treated_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "outputs" / "2026-07-10-daily-run"
            run_dir.mkdir(parents=True)
            (run_dir / "daily-run-result.json").write_text(
                json.dumps({"status": "blocked", "next_action": "blocked", "reason": "copy quality failed"}),
                encoding="utf-8",
            )

            result = evaluate_publish_outcome(
                date(2026, 7, 10),
                output_root=root / "outputs",
                state_dir=root / "state",
                expected_by="09:30",
                now=datetime(2026, 7, 10, 9, 25, tzinfo=TZ),
            )

        self.assertEqual(result["business_outcome"], "blocked")
        self.assertEqual(result["reason"], "copy quality failed")

    def test_successful_owner_manual_publish_overrides_blocked_daily_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "outputs" / "2026-07-10-daily-run"
            manual_dir = run_dir / "manual-publish-171405"
            manual_dir.mkdir(parents=True)
            (run_dir / "daily-run-result.json").write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "next_action": "blocked",
                        "reason": "copy quality failed",
                    }
                ),
                encoding="utf-8",
            )
            manual_path = manual_dir / "manual-publish-result.json"
            manual_path.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "manual_owner_requested": True,
                        "publish_channel": "mass_send_all",
                        "submitted": {
                            "msg_id": "manual-msg-id",
                            "msg_data_id": "manual-data-id",
                        },
                        "status_checks": [{"normalized_status": "success"}],
                    }
                ),
                encoding="utf-8",
            )
            lock_dir = root / "state" / "published"
            lock_dir.mkdir(parents=True)
            (lock_dir / "2026-07-10.json").write_text("{}", encoding="utf-8")

            result = evaluate_publish_outcome(
                date(2026, 7, 10),
                output_root=root / "outputs",
                state_dir=root / "state",
                now=datetime(2026, 7, 10, 22, 30, tzinfo=TZ),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["business_outcome"], "published")
        self.assertEqual(result["stage"], "manual_wechat_publish")
        self.assertEqual(result["publish_result"], str(manual_path))
        self.assertEqual(result["msg_id"], "manual-msg-id")
        self.assertEqual(result["msg_data_id"], "manual-data-id")
        self.assertTrue(result["manual_owner_requested"])

    def test_missing_run_becomes_failed_after_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = evaluate_publish_outcome(
                date(2026, 7, 10),
                output_root=root / "outputs",
                state_dir=root / "state",
                expected_by="09:30",
                now=datetime(2026, 7, 10, 10, 50, tzinfo=TZ),
            )

        self.assertEqual(result["business_outcome"], "failed")

    def test_duplicate_skip_requires_daily_publish_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "outputs" / "2026-07-10-daily-run"
            run_dir.mkdir(parents=True)
            (run_dir / "daily-run-result.json").write_text(
                json.dumps({"status": "ok", "next_action": "duplicate_skipped"}),
                encoding="utf-8",
            )

            pending = evaluate_publish_outcome(
                date(2026, 7, 10),
                output_root=root / "outputs",
                state_dir=root / "state",
                expected_by="09:30",
                now=datetime(2026, 7, 10, 9, 25, tzinfo=TZ),
            )
            failed = evaluate_publish_outcome(
                date(2026, 7, 10),
                output_root=root / "outputs",
                state_dir=root / "state",
                expected_by="09:30",
                now=datetime(2026, 7, 10, 9, 40, tzinfo=TZ),
            )
            (root / "state" / "published").mkdir(parents=True)
            (root / "state" / "published" / "2026-07-10.json").write_text("{}", encoding="utf-8")
            deduplicated = evaluate_publish_outcome(
                date(2026, 7, 10),
                output_root=root / "outputs",
                state_dir=root / "state",
                expected_by="09:30",
                now=datetime(2026, 7, 10, 9, 40, tzinfo=TZ),
            )

        self.assertEqual(pending["business_outcome"], "pending")
        self.assertEqual(failed["business_outcome"], "failed")
        self.assertEqual(deduplicated["business_outcome"], "duplicate_skipped")

    def test_pending_refresh_only_queries_existing_publish_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            publish_path = root / "publish-result.json"
            publish_path.write_text(
                json.dumps(
                    {
                        "status": "sending",
                        "published": False,
                        "publish_channel": "mass_send_all",
                        "submitted": {
                            "msg_id": "existing-msg-id",
                            "msg_data_id": "existing-data-id",
                        },
                        "status_checks": [{"normalized_status": "pending"}],
                    }
                ),
                encoding="utf-8",
            )
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                "publishing:\n  retry_count: 0\n  retry_delay_minutes: 0\n",
                encoding="utf-8",
            )
            client = Mock()
            client.get_access_token.return_value = SimpleNamespace(value="test-token")

            with patch(
                "src.monitoring.publish_watchdog.require_wechat_write_access",
            ), patch(
                "src.monitoring.publish_watchdog.client_from_env",
                return_value=client,
            ), patch(
                "src.monitoring.publish_watchdog.poll_publish_request_status",
                return_value=[
                    {
                        "normalized_status": "success",
                        "msg_id": "existing-msg-id",
                    }
                ],
            ) as poll_status:
                result = refresh_pending_publish(
                    {"publish_result": str(publish_path)},
                    env_path=root / ".env",
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                )

            stored = json.loads(publish_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["published"])
        self.assertEqual(stored["status"], "ok")
        self.assertTrue(stored["published"])
        self.assertEqual(len(stored["status_checks"]), 2)
        client.get_access_token.assert_called_once_with()
        poll_status.assert_called_once()
        self.assertEqual(poll_status.call_args.kwargs["attempts"], 1)
        self.assertEqual(poll_status.call_args.kwargs["interval_seconds"], 0)

    def test_dashboard_reports_last_published_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "outputs" / "2026-07-09-daily-run"
            publish_dir = run_dir / "wechat-publish"
            publish_dir.mkdir(parents=True)
            package_dir = run_dir / "content-package"
            primary_dir = package_dir / "primary"
            primary_dir.mkdir(parents=True)
            (root / "state" / "published").mkdir(parents=True)
            (root / "state" / "published" / "2026-07-09.json").write_text("{}", encoding="utf-8")
            publish_path = publish_dir / "publish-result.json"
            publish_path.write_text(json.dumps({"status": "ok", "published": True}), encoding="utf-8")
            (package_dir / "content-package.json").write_text(
                json.dumps(
                    {
                        "copy_quality": {"ok": True},
                        "package_risk_level": "low",
                        "text_model_generation": {
                            "articles": {
                                "primary": {
                                    "roles": {"summary": {"status": "applied"}},
                                    "final_review": {
                                        "status": "applied",
                                        "reviewer_role": "polish",
                                    },
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            (primary_dir / "article.json").write_text(
                json.dumps(
                    {
                        "title": "AI news digest",
                        "news_items": [
                            {"title": "Story one", "source_name": "Source A"},
                            {"title": "Story two", "source_name": "Source B"},
                        ],
                        "inline_images": [{"path": "inline.png"}],
                        "image_provider": "source_image",
                        "final_ai_review": {
                            "status": "degraded_applied",
                            "model": "deterministic_fallback",
                            "reviewer_role": "local",
                            "fallback_used": True,
                        },
                        "final_review_recovery": {"cycles": [{}, {}]},
                        "news_replenishment": {
                            "runs": [
                                {"added": 1, "remaining_reserve": 4},
                                {"added": 2, "remaining_reserve": 2},
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "daily-run-result.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "next_action": "publish_submitted",
                        "published": True,
                        "publish_result": str(publish_path),
                        "execution_budget": {
                            "total_seconds": 840,
                            "remaining_seconds": 180,
                        },
                        "resumed_stages": ["content_package"],
                        "stages": [
                            {
                                "name": "content_package",
                                "duration_seconds": 12.5,
                                "news_fetch": {
                                    "status": "ok",
                                    "diagnostics": [
                                        {"status": "ok", "matched_items": 2, "cache_hit": False},
                                        {"status": "failed", "matched_items": 0, "cache_hit": False},
                                    ],
                                },
                                "manufacturing_news_fetch": {
                                    "status": "ok",
                                    "diagnostics": [
                                        {"status": "ok", "matched_items": 1, "cache_hit": True}
                                    ],
                                },
                            },
                            {"name": "wechat_draft", "duration_seconds": 3.25},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            dashboard = build_operations_status(
                output_root=root / "outputs",
                state_dir=root / "state",
                current_release=root,
                now=datetime(2026, 7, 10, 8, 0, tzinfo=TZ),
                days=2,
            )

        self.assertEqual(dashboard["last_published_date"], "2026-07-09")
        row = dashboard["runs"][1]
        self.assertEqual(row["outcome"], "published")
        self.assertEqual(row["stage_durations"]["content_package"], 12.5)
        self.assertEqual(row["stage_durations"]["wechat_draft"], 3.25)
        self.assertEqual(row["execution_budget"]["remaining_seconds"], 180)
        self.assertEqual(row["resumed_stages"], ["content_package"])
        self.assertEqual(row["source_health"]["news_fetch"]["failed_sources"], 1)
        self.assertEqual(row["source_health"]["manufacturing_news_fetch"]["cache_hits"], 1)
        self.assertEqual(row["model_statuses"]["primary"]["final_review"], "applied:polish")
        self.assertTrue(row["primary"]["final_review_fallback"])
        self.assertEqual(row["primary"]["recovery_cycles"], 2)
        self.assertEqual(row["primary"]["replenished_items"], 3)
        self.assertEqual(row["primary"]["remaining_reserve"], 2)


if __name__ == "__main__":
    unittest.main()
