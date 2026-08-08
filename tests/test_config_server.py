from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.integrations.config_server import (
    _prepared_run_blocks_config_change,
    _render_page,
    update_env_file,
    update_yaml_configs,
)
from src.integrations.text_model_client import load_text_model_configs
from src.integrations.wechat_client import load_env_file


class ConfigServerTest(unittest.TestCase):
    def test_prepared_draft_freezes_config_until_publish_lock_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_root = root / "outputs"
            state_dir = root / "state"
            now = datetime(2026, 7, 12, 9, 45, tzinfo=timezone(timedelta(hours=8)))
            run_dir = output_root / "2026-07-12-daily-run"
            run_dir.mkdir(parents=True)
            (run_dir / "run-state.json").write_text(
                json.dumps({"stages": {"wechat_draft": {"status": "completed"}}}),
                encoding="utf-8",
            )

            self.assertTrue(
                _prepared_run_blocks_config_change(output_root=output_root, state_dir=state_dir, now=now)
            )
            (state_dir / "published").mkdir(parents=True)
            (state_dir / "published" / "2026-07-12.json").write_text("{}", encoding="utf-8")
            self.assertFalse(
                _prepared_run_blocks_config_change(output_root=output_root, state_dir=state_dir, now=now)
            )

    def test_update_env_file_preserves_existing_secret_when_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "WECHAT_APP_ID=wx",
                        "TEXT_MODEL_API_KEY=old-text-key",
                        "TEXT_MODEL_SUMMARY_API_KEY=old-summary-key",
                        "OPENAI_IMAGE_API_KEY=old-relay-key",
                        "OPENAI_API_KEY=old-openai-key",
                    ]
                ),
                encoding="utf-8",
            )

            update_env_file(
                env_path,
                {
                    "OPENAI_IMAGE_API_BASE": "https://relay.example.com/v1",
                    "TEXT_MODEL_API_BASE": "https://api.openai.com/v1",
                    "TEXT_MODEL_API_KEY": "",
                    "TEXT_MODEL_SUMMARY_ENABLED": "on",
                    "TEXT_MODEL_SUMMARY_API_KEY": "",
                    "TEXT_MODEL_SUMMARY_MODEL": "gemini-3.5-flash",
                    "TEXT_MODEL_BATCH_MODEL": "qwen3.7-plus",
                    "TEXT_MODEL_POLISH_MODEL": "claude-sonnet-4-6",
                    "OPENAI_IMAGE_API_KEY": "",
                    "OPENAI_API_KEY": "",
                    "OPENAI_IMAGE_MODEL": "gpt-image-2",
                    "OPENAI_IMAGE_SIZE": "1536x1024",
                    "OPENAI_IMAGE_QUALITY": "medium",
                    "OPENAI_IMAGE_FORMAT": "png",
                },
            )
            values = load_env_file(env_path)

            self.assertFalse(env_path.with_suffix(env_path.suffix + ".tmp").exists())

        self.assertEqual(values["WECHAT_APP_ID"], "wx")
        self.assertEqual(values["TEXT_MODEL_API_KEY"], "old-text-key")
        self.assertEqual(values["TEXT_MODEL_SUMMARY_API_KEY"], "old-summary-key")
        self.assertEqual(values["TEXT_MODEL_SUMMARY_MODEL"], "gemini-3.5-flash")
        self.assertEqual(values["TEXT_MODEL_BATCH_MODEL"], "qwen3.7-plus")
        self.assertEqual(values["TEXT_MODEL_POLISH_MODEL"], "claude-sonnet-4-6")
        self.assertEqual(values["OPENAI_IMAGE_API_BASE"], "https://relay.example.com/v1")
        self.assertEqual(values["OPENAI_IMAGE_API_KEY"], "old-relay-key")
        self.assertEqual(values["OPENAI_API_KEY"], "old-openai-key")

    def test_update_env_file_writes_new_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            update_env_file(
                env_path,
                {
                    "OPENAI_IMAGE_API_BASE": "https://relay.example.com/v1",
                    "OPENAI_IMAGE_API_KEY": "new-relay-key",
                    "OPENAI_IMAGE_MODEL": "gpt-image-2",
                },
            )
            values = load_env_file(env_path)

        self.assertEqual(values["OPENAI_IMAGE_API_KEY"], "new-relay-key")
        self.assertEqual(values["OPENAI_IMAGE_MODEL"], "gpt-image-2")

    def test_update_env_file_writes_text_model_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            update_env_file(
                env_path,
                {
                    "TEXT_MODEL_API_BASE": "https://api.openai.com/v1",
                    "TEXT_MODEL_API_KEY": "shared-text-key",
                    "TEXT_MODEL_SUMMARY_ENABLED": "on",
                    "TEXT_MODEL_SUMMARY_MODEL": "gemini-3.5-flash",
                    "TEXT_MODEL_BATCH_ENABLED": "on",
                    "TEXT_MODEL_BATCH_MODEL": "qwen3.7-plus",
                    "TEXT_MODEL_POLISH_MODEL": "claude-sonnet-4-6",
                },
            )
            values = load_env_file(env_path)
            configs = load_text_model_configs(env_path)

        self.assertEqual(values["TEXT_MODEL_API_BASE"], "https://api.openai.com/v1")
        self.assertEqual(values["TEXT_MODEL_API_KEY"], "shared-text-key")
        self.assertEqual(configs["summary"].model, "gemini-3.5-flash")
        self.assertTrue(configs["summary"].configured)
        self.assertEqual(configs["batch"].model, "qwen3.7-plus")
        self.assertTrue(configs["batch"].configured)
        self.assertEqual(configs["polish"].model, "claude-sonnet-4-6")
        self.assertFalse(configs["polish"].configured)

    def test_update_yaml_configs_writes_publish_and_topic_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            schedule = root / "schedule.yaml"
            topics = root / "topics.yaml"
            news = root / "news-sources.yaml"
            schedule.write_text("daily_run:\n  enabled: true\npublishing:\n  mode: draft_only\n", encoding="utf-8")
            topics.write_text("blocked_topics:\n  - old\n", encoding="utf-8")
            news.write_text("sources: []\n", encoding="utf-8")

            saved = update_yaml_configs(
                schedule,
                topics,
                news,
                {
                    "DAILY_RUN_ENABLED": "on",
                    "DAILY_RUN_CONTROLLER_TIME": "09:00",
                    "DAILY_RUN_PUBLISH_TIME": "09:00",
                    "DAILY_RUN_MAX_RUNTIME_SECONDS": "900",
                    "DAILY_RUN_PUBLISH_RESERVE_SECONDS": "150",
                    "DAILY_RUN_MAX_PREPARE_ATTEMPTS": "9",
                    "DAILY_RUN_MAX_TEXT_MODEL_CALLS_PER_ATTEMPT": "99",
                    "DAILY_RUN_MAX_TEXT_MODEL_CALLS_PER_DAY": "999",
                    "PUBLISHING_MODE": "auto_publish_low_risk",
                    "PUBLISHING_PUBLISH_CHANNEL": "mass_send_all",
                    "PUBLISHING_ALLOW_LOW_RISK_AUTO_PUBLISH": "on",
                    "PUBLISHING_REJECT_HIGH_RISK": "on",
                    "PUBLISHING_ALLOW_IMAGE_FALLBACK_FOR_PUBLISH": "on",
                    "PUBLISHING_MASS_SEND_IGNORE_REPRINT": "on",
                    "FINAL_REVIEW_MODE": "enforce",
                    "FINAL_REVIEW_AUTO_REPAIR": "on",
                    "FINAL_REVIEW_MAX_RECOVERY_CYCLES": "4",
                    "FINAL_REVIEW_MINIMUM_CYCLE_BUDGET_SECONDS": "60",
                    "FINAL_REVIEW_MIN_PRIMARY_ITEMS": "5",
                    "FINAL_REVIEW_MIN_SECONDARY_ITEMS": "3",
                    "TEMPORARY_TOPIC_ENABLED": "on",
                    "TEMPORARY_TOPIC_TOPIC": "AI 浏览器",
                    "TOPICS_BLOCKED_TOPICS": "医疗诊断\n投资荐股",
                    "NEWS_MINIMUM_SUCCESSFUL_SOURCES": "2",
                    "NEWS_FETCH_WORKERS": "8",
                    "NEWS_PREFERRED_KEYWORDS": "agent\ncoding",
                    "NEWS_BLOCKED_KEYWORDS": "giveaway",
                },
            )
            schedule_data = yaml.safe_load(schedule.read_text(encoding="utf-8"))
            topics_data = yaml.safe_load(topics.read_text(encoding="utf-8"))
            news_data = yaml.safe_load(news.read_text(encoding="utf-8"))

        self.assertGreater(saved, 0)
        self.assertEqual(schedule_data["daily_run"]["controller_start_time"], "09:00")
        self.assertEqual(schedule_data["daily_run"]["max_runtime_seconds"], 900)
        self.assertEqual(schedule_data["daily_run"]["publish_reserve_seconds"], 150)
        self.assertEqual(schedule_data["daily_run"]["max_prepare_attempts_per_day"], 3)
        self.assertEqual(schedule_data["daily_run"]["max_text_model_calls_per_attempt"], 30)
        self.assertEqual(schedule_data["daily_run"]["max_text_model_calls_per_day"], 50)
        self.assertEqual(schedule_data["publishing"]["mode"], "auto_publish_low_risk")
        self.assertEqual(schedule_data["publishing"]["publish_channel"], "mass_send_all")
        self.assertTrue(schedule_data["publishing"]["mass_send_ignore_reprint"])
        self.assertEqual(schedule_data["final_review"]["mode"], "enforce")
        self.assertTrue(schedule_data["final_review"]["auto_repair"])
        self.assertEqual(schedule_data["final_review"]["max_recovery_cycles"], 3)
        self.assertEqual(schedule_data["final_review"]["minimum_cycle_budget_seconds"], 60)
        self.assertEqual(schedule_data["final_review"]["min_news_items"], {"primary": 5, "secondary": 3})
        self.assertEqual(topics_data["temporary_topic"]["topic"], "AI 浏览器")
        self.assertEqual(topics_data["blocked_topics"], ["医疗诊断", "投资荐股"])
        self.assertEqual(news_data["fetch_workers"], 8)
        self.assertEqual(news_data["preferred_keywords"], ["agent", "coding"])

    def test_render_page_uses_chinese_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / ".env"
            schedule = root / "schedule.yaml"
            topics = root / "topics.yaml"
            news = root / "news-sources.yaml"
            env_path.write_text("", encoding="utf-8")
            schedule.write_text("daily_run:\n  enabled: true\npublishing:\n  mode: draft_only\n", encoding="utf-8")
            topics.write_text("blocked_topics: []\n", encoding="utf-8")
            news.write_text("sources: []\n", encoding="utf-8")

            html = _render_page(env_path, schedule, topics, news, "token")

        self.assertIn("自动化配置", html)
        self.assertIn("发布控制", html)
        self.assertIn("发布渠道", html)
        self.assertIn("mass_send_all", html)
        self.assertIn("文本模型 API", html)
        self.assertIn("填写供应商提供的模型 ID", html)
        self.assertIn("图片 API 与通知", html)
        self.assertIn("运行状态", html)
        self.assertIn("终稿 AI 二审模式", html)
        self.assertIn("终稿审核模型", html)
        self.assertIn("保存配置", html)

    def test_render_page_does_not_echo_notification_webhook_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / ".env"
            schedule = root / "schedule.yaml"
            topics = root / "topics.yaml"
            news = root / "news-sources.yaml"
            env_path.write_text("NOTIFICATION_WEBHOOK_URL=https://example.invalid/private-hook\n", encoding="utf-8")
            schedule.write_text("daily_run:\n  enabled: true\npublishing:\n  mode: dry_run\n", encoding="utf-8")
            topics.write_text("blocked_topics: []\n", encoding="utf-8")
            news.write_text("sources: []\n", encoding="utf-8")

            html = _render_page(env_path, schedule, topics, news, "token")

        self.assertNotIn("https://example.invalid/private-hook", html)
        self.assertIn("已配置", html)


if __name__ == "__main__":
    unittest.main()
