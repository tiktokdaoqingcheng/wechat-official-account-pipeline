from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class PublicDefaultsTest(unittest.TestCase):
    def test_schedule_denies_writes_and_external_providers(self) -> None:
        schedule = yaml.safe_load(Path("config/schedule.yaml").read_text(encoding="utf-8"))

        self.assertFalse(schedule["runtime"]["allow_wechat_writes"])
        self.assertFalse(schedule["runtime"]["allow_external_providers"])
        self.assertEqual(schedule["publishing"]["mode"], "dry_run")
        self.assertFalse(schedule["publishing"]["allow_low_risk_auto_publish"])

    def test_public_feed_examples_are_synthetic_and_disabled(self) -> None:
        for config_path in ("config/news-sources.yaml", "config/manufacturing-news-sources.yaml"):
            payload = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
            self.assertTrue(payload["sources"])
            self.assertTrue(all(source["enabled"] is False for source in payload["sources"]))
            self.assertTrue(all("example.invalid" in source["url"] for source in payload["sources"]))

    def test_example_credentials_and_model_ids_are_blank(self) -> None:
        values = {}
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value

        for key in (
            "WECHAT_APP_ID",
            "WECHAT_APP_SECRET",
            "WECHAT_TOKEN",
            "TEXT_MODEL_API_KEY",
            "TEXT_MODEL_SUMMARY_MODEL",
            "TEXT_MODEL_BATCH_MODEL",
            "TEXT_MODEL_POLISH_MODEL",
            "OPENAI_IMAGE_API_KEY",
            "OPENAI_IMAGE_MODEL",
            "NOTIFICATION_WEBHOOK_TOKEN",
        ):
            self.assertEqual(values[key], "")


if __name__ == "__main__":
    unittest.main()
