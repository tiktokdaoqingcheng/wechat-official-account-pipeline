from __future__ import annotations

import json
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from src.config_loader import load_layered_yaml
from src.pipeline.package_manifest import freeze_content_package
from src.review.publish_guard import record_publish_lock
from src.runtime_guard import EXPECTED_CONFIG_VERSION, config_fingerprint
from src.scheduler.daily_runner import (
    _archive_prepare_attempt,
    _content_package_retry_eligible,
    _start_prepare_attempt,
    run_daily,
)


def _production_runtime() -> dict[str, object]:
    return {
        "environment": "production",
        "allowed_instance_ids": [socket.gethostname()],
        "allow_wechat_writes": True,
        "allow_external_providers": True,
        "config_version": EXPECTED_CONFIG_VERSION,
    }


class DailyRunnerTest(unittest.TestCase):
    def test_prepare_attempt_archive_keeps_only_reviewable_nonsecret_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_dir = root / "package"
            (package_dir / "primary").mkdir(parents=True)
            (package_dir / "primary" / "article.json").write_text(
                json.dumps({"title": "测试科技早报"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (package_dir / "primary" / "illustration.png").write_bytes(b"image")
            (package_dir / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")

            archive = Path(
                _archive_prepare_attempt(
                    run_dir=root / "run",
                    package_dir=package_dir,
                    attempt_number=2,
                    dry_run={
                        "status": "ok",
                        "risk_level": "medium",
                        "publish_action": "hold",
                        "publish_allowed": False,
                        "title": "测试科技早报",
                        "text_model_generation": {"calls": [{"outcome": "ok"}]},
                    },
                    now=datetime(2026, 8, 7, 9, 30, tzinfo=timezone(timedelta(hours=8))),
                )
            )

            summary = json.loads((archive / "attempt-summary.json").read_text(encoding="utf-8"))
            self.assertTrue((archive / "primary" / "article.json").is_file())
            self.assertFalse((archive / "primary" / "illustration.png").exists())
            self.assertFalse((archive / ".env").exists())
            self.assertEqual(summary["attempt"], 2)
            self.assertEqual(summary["model_call_count"], 1)

    def test_content_package_retry_only_repeats_transient_failures(self) -> None:
        editorial_hold = {
            "publish_allowed": False,
            "news_fetch": {"status": "ok"},
            "manufacturing_news_fetch": {"status": "supplemented"},
            "text_model_generation": {
                "articles": {"primary": {"roles": {"summary": {"status": "applied"}}}}
            },
        }
        model_timeout = {
            **editorial_hold,
            "text_model_generation": {
                "articles": {
                    "primary": {
                        "roles": {"summary": {"status": "partial_fallback_after_text_model_error"}}
                    }
                }
            },
        }
        source_failure = {
            **editorial_hold,
            "news_fetch": {"status": "insufficient_sources"},
        }
        missing_key = {
            **editorial_hold,
            "text_model_generation": {"roles": {"summary": {"status": "missing_api_key"}}},
        }
        exhausted_budget = {
            **editorial_hold,
            "text_model_generation": {"recovery": {"status": "budget_exhausted"}},
        }

        self.assertFalse(_content_package_retry_eligible(editorial_hold))
        self.assertTrue(_content_package_retry_eligible(model_timeout))
        self.assertTrue(_content_package_retry_eligible(source_failure))
        self.assertFalse(_content_package_retry_eligible(missing_key))
        self.assertFalse(_content_package_retry_eligible(exhausted_budget))
        self.assertFalse(_content_package_retry_eligible({**editorial_hold, "publish_allowed": True}))

    def test_prepare_limits_are_hard_capped_and_account_running_reservation(self) -> None:
        running = {
            "status": "running",
            "attempts_started": 2,
            "provider_calls_cumulative": 20,
            "active_call_reservation": 28,
        }

        third = _start_prepare_attempt(
            running,
            {
                "max_prepare_attempts_per_day": 99,
                "max_text_model_calls_per_attempt": 999,
                "max_text_model_calls_per_day": 999,
            },
        )

        self.assertTrue(third["allowed"])
        self.assertEqual(third["attempts_started"], 3)
        self.assertEqual(third["provider_calls_cumulative"], 48)
        self.assertEqual(third["provider_call_limit"], 2)
        exhausted = _start_prepare_attempt(
            {"status": "completed", "attempts_started": 3, "provider_calls_cumulative": 48},
            {},
        )
        self.assertFalse(exhausted["allowed"])
        self.assertEqual(exhausted["next_action"], "prepare_attempt_limit_reached")

    def test_daily_call_budget_stops_later_prepare_window_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "daily_run": {
                            "max_prepare_attempts_per_day": 3,
                            "max_text_model_calls_per_attempt": 1,
                            "max_text_model_calls_per_day": 2,
                        },
                        "publishing": {
                            "mode": "auto_publish_low_risk",
                            "allow_low_risk_auto_publish": True,
                        },
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            now = datetime(2026, 8, 6, 7, 0, tzinfo=timezone(timedelta(hours=8)))

            def blocked_package(**kwargs):
                budget = kwargs["execution_budget"]
                self.assertTrue(budget.try_reserve_provider_call())
                output_dir = Path(kwargs["output_dir"])
                output_dir.mkdir(parents=True, exist_ok=True)
                return {
                    "status": "ok",
                    "output_dir": str(output_dir),
                    "title": "测试科技早报",
                    "secondary_title": "测试智能制造日报",
                    "article_count": 2,
                    "risk_level": "medium",
                    "publish_action": "hold",
                    "publish_allowed": False,
                    "already_published_today": False,
                    "files": {},
                    "news_fetch": {"status": "ok"},
                    "manufacturing_news_fetch": {"status": "ok"},
                    "image_generation": {},
                    "text_model_generation": {
                        "calls": [],
                        "articles": {
                            "primary": {
                                "roles": {
                                    "summary": {"status": "partial_fallback_after_text_model_error"}
                                }
                            }
                        },
                    },
                }

            with patch("src.scheduler.daily_runner.generate_dry_run_package", side_effect=blocked_package) as generate:
                first = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    db_path=root / "app.sqlite",
                    run_stage="prepare",
                    now=now,
                )
                second = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    db_path=root / "app.sqlite",
                    run_stage="prepare",
                    now=now,
                )
                third = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    db_path=root / "app.sqlite",
                    run_stage="prepare",
                    now=now,
                )

        self.assertEqual(first["prepare_budget"]["provider_calls_cumulative"], 1)
        self.assertEqual(second["prepare_budget"]["provider_calls_cumulative"], 2)
        self.assertEqual(third["next_action"], "prepare_call_budget_exhausted")
        self.assertEqual(generate.call_count, 2)

    def test_dry_run_mode_generates_daily_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "dry_run",
                            "allow_low_risk_auto_publish": False,
                        }
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            result = run_daily(
                schedule_path=schedule_path,
                state_dir=root / "state",
                output_root=root / "outputs",
                now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["publishing_mode"], "dry_run")
            self.assertEqual(result["next_action"], "none")
            self.assertTrue((Path(result["output_dir"]) / "daily-run-result.json").exists())
            self.assertTrue((Path(result["output_dir"]) / "content-package" / "article.md").exists())
            self.assertTrue((Path(result["output_dir"]) / "content-package" / "content-package.json").exists())
            self.assertTrue((Path(result["output_dir"]) / "content-package" / "primary" / "article.md").exists())
            self.assertTrue((Path(result["output_dir"]) / "content-package" / "secondary" / "article.md").exists())
            self.assertTrue((Path(result["output_dir"]) / "content-package" / "schedule-plan.json").exists())
            self.assertEqual(result["stages"][0]["article_count"], 2)
            self.assertEqual(
                result["schedule_plan"],
                str(Path(result["output_dir"]) / "content-package" / "schedule-plan.json"),
            )

    def test_existing_publish_lock_blocks_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            now = datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8)))
            record_publish_lock({"title": "already done"}, state_dir, now=now)

            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump({"publishing": {"mode": "dry_run"}}, allow_unicode=True),
                encoding="utf-8",
            )

            result = run_daily(
                schedule_path=schedule_path,
                state_dir=state_dir,
                output_root=root / "outputs",
                now=now,
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["next_action"], "duplicate_skipped")
            self.assertEqual(result["business_outcome"], "duplicate_skipped")

    def test_draft_only_creates_wechat_draft_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "draft_only",
                            "reject_high_risk": True,
                            "retry_count": 0,
                            "retry_delay_minutes": 0,
                        }
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            seed_path = root / "primary-news.json"
            seed_path.write_text(
                json.dumps(
                    {
                        "target_date": "2026-06-02",
                        "news_status": "ok",
                        "news_items": [
                            {
                                "title": "New AI model ships",
                                "source_name": "Example AI",
                                "url": "https://example.com/model",
                                "published_at": "2026-06-02T16:00:00+08:00",
                                "summary": "Useful model update for developers.",
                            }
                        ],
                        "sources": [
                            {
                                "name": "Example AI：New AI model ships",
                                "url": "https://example.com/model",
                                "summary": "Useful model update for developers.",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("src.scheduler.daily_runner.create_draft_from_package") as create_draft:
                create_draft.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "draft"),
                    "article_count": 2,
                    "draft": {"media_id": "draft-media-id"},
                }
                result = run_daily(
                    schedule_path=schedule_path,
                    news_seed_path=seed_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["next_action"], "draft_created")
            self.assertEqual(result["draft_media_id"], "draft-media-id")
            self.assertEqual(result["stages"][1]["name"], "wechat_draft")
            create_draft.assert_called_once()

    def test_draft_retry_reuses_frozen_content_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "draft_only",
                            "reject_high_risk": True,
                            "retry_count": 0,
                            "retry_delay_minutes": 0,
                        },
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            effective_schedule = load_layered_yaml(schedule_path)
            effective_fingerprint = config_fingerprint(effective_schedule)
            generation_calls = 0

            def fake_generate_package(**kwargs):
                nonlocal generation_calls
                generation_calls += 1
                output_dir = Path(kwargs["output_dir"])
                output_dir.mkdir(parents=True, exist_ok=True)
                package_path = output_dir / "content-package.json"
                package_path.write_text(json.dumps({"files": {}}), encoding="utf-8")
                manifest = freeze_content_package(
                    package_path,
                    config_fingerprint=effective_fingerprint,
                    config_version=EXPECTED_CONFIG_VERSION,
                )
                result = {
                    "status": "ok",
                    "output_dir": str(output_dir),
                    "title": "AI news digest",
                    "secondary_title": "Manufacturing digest",
                    "article_count": 2,
                    "risk_level": "low",
                    "publish_action": "publish",
                    "publish_allowed": True,
                    "already_published_today": False,
                    "files": {
                        "content_package_json": str(package_path),
                        "content_package_manifest": str(manifest["path"]),
                        "review_json": str(output_dir / "review.json"),
                        "schedule_plan_json": str(output_dir / "schedule-plan.json"),
                    },
                }
                (output_dir / "dry-run-result.json").write_text(
                    json.dumps(result),
                    encoding="utf-8",
                )
                return result

            draft_results = [
                {
                    "status": "failed",
                    "output_dir": str(root / "outputs" / "draft-first"),
                    "article_count": 0,
                    "draft": {},
                    "error": "temporary upload failure",
                },
                {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "draft-second"),
                    "article_count": 2,
                    "draft": {"media_id": "resumed-draft-media-id"},
                },
            ]
            now = datetime(2026, 7, 10, 8, 0, tzinfo=timezone(timedelta(hours=8)))

            with patch(
                "src.scheduler.daily_runner.generate_dry_run_package",
                side_effect=fake_generate_package,
            ) as generate_package, patch(
                "src.scheduler.daily_runner.create_draft_from_package",
                side_effect=draft_results,
            ) as create_draft:
                first = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=now,
                )
                second = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=now,
                )

        self.assertEqual(first["status"], "blocked")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["next_action"], "draft_created")
        self.assertEqual(second["draft_media_id"], "resumed-draft-media-id")
        self.assertIn("content_package", second["resumed_stages"])
        self.assertEqual(generation_calls, 1)
        generate_package.assert_called_once()
        self.assertEqual(create_draft.call_count, 2)

    def test_auto_publish_creates_draft_and_submits_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "auto_publish_low_risk",
                            "allow_low_risk_auto_publish": True,
                            "retry_count": 0,
                            "retry_delay_minutes": 0,
                        }
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            package_path = root / "content-package.json"
            review_path = root / "review.json"

            with (
                patch("src.scheduler.daily_runner.generate_dry_run_package") as generate_package,
                patch("src.scheduler.daily_runner.create_draft_from_package") as create_draft,
                patch("src.scheduler.daily_runner.publish_draft") as publish,
            ):
                generate_package.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "content-package"),
                    "title": "AI news digest",
                    "secondary_title": "AI explainer",
                    "article_count": 2,
                    "risk_level": "low",
                    "publish_action": "publish",
                    "publish_allowed": True,
                    "already_published_today": False,
                    "files": {
                        "content_package_json": str(package_path),
                        "review_json": str(review_path),
                        "schedule_plan_json": str(root / "schedule-plan.json"),
                    },
                }
                create_draft.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "draft"),
                    "article_count": 2,
                    "draft": {"media_id": "draft-media-id"},
                }
                publish.return_value = {
                    "status": "submitted",
                    "output_dir": str(root / "outputs" / "publish"),
                    "publish_channel": "freepublish",
                    "submitted": {"publish_id": "publish-id"},
                    "published": False,
                }

                result = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["next_action"], "publish_submitted")
            self.assertEqual(result["draft_media_id"], "draft-media-id")
            self.assertEqual(result["publish_channel"], "freepublish")
            self.assertEqual(result["publish_id"], "publish-id")
            self.assertEqual([stage["name"] for stage in result["stages"]], ["content_package", "wechat_draft", "wechat_publish"])
            self.assertTrue((Path(result["output_dir"]) / "run-archive.json").exists())
            publish.assert_called_once()

    def test_prepare_then_publish_stage_reuses_frozen_draft_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "auto_publish_low_risk",
                            "allow_low_risk_auto_publish": True,
                            "retry_count": 0,
                            "retry_delay_minutes": 0,
                        },
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            effective_schedule = load_layered_yaml(schedule_path)
            effective_fingerprint = config_fingerprint(effective_schedule)
            now = datetime(2026, 7, 12, 7, 0, tzinfo=timezone(timedelta(hours=8)))

            def fake_generate_package(**kwargs):
                output_dir = Path(kwargs["output_dir"])
                output_dir.mkdir(parents=True, exist_ok=True)
                package_path = output_dir / "content-package.json"
                review_path = output_dir / "review.json"
                package_path.write_text(json.dumps({"files": {}}), encoding="utf-8")
                review_path.write_text(json.dumps({"risk_level": "low"}), encoding="utf-8")
                manifest = freeze_content_package(
                    package_path,
                    config_fingerprint=effective_fingerprint,
                    config_version=EXPECTED_CONFIG_VERSION,
                )
                result = {
                    "status": "ok",
                    "output_dir": str(output_dir),
                    "title": "AI news digest",
                    "secondary_title": "Manufacturing digest",
                    "article_count": 2,
                    "risk_level": "low",
                    "publish_action": "publish",
                    "publish_allowed": True,
                    "already_published_today": False,
                    "files": {
                        "content_package_json": str(package_path),
                        "content_package_manifest": str(manifest["path"]),
                        "review_json": str(review_path),
                        "schedule_plan_json": str(output_dir / "schedule-plan.json"),
                    },
                }
                (output_dir / "dry-run-result.json").write_text(json.dumps(result), encoding="utf-8")
                return result

            with (
                patch("src.scheduler.daily_runner.generate_dry_run_package", side_effect=fake_generate_package) as generate,
                patch("src.scheduler.daily_runner.create_draft_from_package") as create_draft,
                patch("src.scheduler.daily_runner.publish_draft") as publish,
            ):
                create_draft.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "draft"),
                    "article_count": 2,
                    "draft": {"media_id": "frozen-draft-media-id"},
                }
                publish.return_value = {
                    "status": "submitted",
                    "output_dir": str(root / "outputs" / "publish"),
                    "publish_channel": "mass_send_all",
                    "submitted": {"msg_id": "mass-msg-id", "msg_data_id": "mass-data-id"},
                    "published": False,
                }

                prepared = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    run_stage="prepare",
                    now=now,
                )
                published = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    run_stage="publish",
                    now=now.replace(hour=10, minute=15),
                )

            self.assertEqual(prepared["status"], "ok")
            self.assertEqual(prepared["next_action"], "prepared_for_publish")
            self.assertEqual(prepared["business_outcome"], "prepared")
            self.assertEqual(published["next_action"], "publish_submitted")
            self.assertIn("content_package", published["resumed_stages"])
            self.assertIn("wechat_draft", published["resumed_stages"])
            generate.assert_called_once()
            create_draft.assert_called_once()
            publish.assert_called_once()

    def test_publish_stage_blocks_without_prepare_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "auto_publish_low_risk",
                            "allow_low_risk_auto_publish": True,
                        },
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            with (
                patch("src.scheduler.daily_runner.generate_dry_run_package") as generate,
                patch("src.scheduler.daily_runner.create_draft_from_package") as create_draft,
                patch("src.scheduler.daily_runner.publish_draft") as publish,
            ):
                result = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    run_stage="publish",
                    now=datetime(2026, 7, 12, 10, 15, tzinfo=timezone(timedelta(hours=8))),
                )

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["next_action"], "prepare_required")
            generate.assert_not_called()
            create_draft.assert_not_called()
            publish.assert_not_called()

    def test_auto_publish_records_official_account_mass_send_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "auto_publish_low_risk",
                            "publish_channel": "mass_send_all",
                            "allow_low_risk_auto_publish": True,
                            "retry_count": 0,
                            "retry_delay_minutes": 0,
                        }
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            with (
                patch("src.scheduler.daily_runner.generate_dry_run_package") as generate_package,
                patch("src.scheduler.daily_runner.create_draft_from_package") as create_draft,
                patch("src.scheduler.daily_runner.publish_draft") as publish,
            ):
                generate_package.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "content-package"),
                    "title": "AI news digest",
                    "secondary_title": "AI explainer",
                    "article_count": 2,
                    "risk_level": "low",
                    "publish_action": "publish",
                    "publish_allowed": True,
                    "already_published_today": False,
                    "files": {
                        "content_package_json": str(root / "content-package.json"),
                        "review_json": str(root / "review.json"),
                        "schedule_plan_json": str(root / "schedule-plan.json"),
                    },
                }
                create_draft.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "draft"),
                    "article_count": 2,
                    "draft": {"media_id": "draft-media-id"},
                }
                publish.return_value = {
                    "status": "sending",
                    "output_dir": str(root / "outputs" / "publish"),
                    "publish_channel": "mass_send_all",
                    "submitted": {"msg_id": "mass-msg-id", "msg_data_id": "mass-data-id"},
                    "published": False,
                }

                result = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["next_action"], "publish_submitted")
        self.assertEqual(result["publish_channel"], "mass_send_all")
        self.assertEqual(result["msg_id"], "mass-msg-id")
        self.assertEqual(result["msg_data_id"], "mass-data-id")
        self.assertEqual(result["stages"][2]["msg_id"], "mass-msg-id")

    def test_auto_publish_blocks_insufficient_news_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "auto_publish_low_risk",
                            "allow_low_risk_auto_publish": True,
                        }
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            with patch("src.scheduler.daily_runner.generate_dry_run_package") as generate_package:
                generate_package.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "content-package"),
                    "title": "AI news digest",
                    "secondary_title": "AI explainer",
                    "article_count": 2,
                    "risk_level": "low",
                    "publish_action": "publish",
                    "publish_allowed": True,
                    "already_published_today": False,
                    "news_fetch": {"status": "insufficient_sources"},
                    "files": {},
                }

                result = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["next_action"], "blocked")
        self.assertIn("news source", result["reason"])

    def test_auto_publish_blocks_image_fallback_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": _production_runtime(),
                        "publishing": {
                            "mode": "auto_publish_low_risk",
                            "allow_low_risk_auto_publish": True,
                            "allow_image_fallback_for_publish": False,
                        }
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            with patch("src.scheduler.daily_runner.generate_dry_run_package") as generate_package:
                generate_package.return_value = {
                    "status": "ok",
                    "output_dir": str(root / "outputs" / "content-package"),
                    "title": "AI news digest",
                    "secondary_title": "AI explainer",
                    "article_count": 2,
                    "risk_level": "low",
                    "publish_action": "publish",
                    "publish_allowed": True,
                    "already_published_today": False,
                    "news_fetch": {"status": "ok"},
                    "image_generation": {
                        "primary": {
                            "cover": {
                                "provider": "local_fallback",
                                "status": "fallback_after_openai_error",
                            }
                        }
                    },
                    "files": {},
                }

                result = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        self.assertEqual(result["status"], "blocked")
        self.assertIn("image", result["reason"])

    def test_non_production_runtime_blocks_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": {
                            "environment": "local",
                            "allow_wechat_writes": False,
                            "allowed_instance_ids": [],
                            "config_version": EXPECTED_CONFIG_VERSION,
                        },
                        "publishing": {"mode": "draft_only"},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            with patch("src.scheduler.daily_runner.generate_dry_run_package") as generate_package:
                result = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["next_action"], "runtime_guard_blocked")
            generate_package.assert_not_called()

    def test_production_runtime_blocks_when_external_providers_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule_path = root / "schedule.yaml"
            runtime = _production_runtime()
            runtime["allow_external_providers"] = False
            schedule_path.write_text(
                yaml.safe_dump(
                    {
                        "runtime": runtime,
                        "publishing": {"mode": "auto_publish_low_risk"},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            with patch("src.scheduler.daily_runner.generate_dry_run_package") as generate_package:
                result = run_daily(
                    schedule_path=schedule_path,
                    state_dir=root / "state",
                    output_root=root / "outputs",
                    env_path=root / ".env",
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

            self.assertEqual(result["status"], "blocked")
            self.assertIn("allow_external_providers", result["runtime_guard_reasons"][-1])
            generate_package.assert_not_called()


if __name__ == "__main__":
    unittest.main()
