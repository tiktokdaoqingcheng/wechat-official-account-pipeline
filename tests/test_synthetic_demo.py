from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.run_synthetic_demo import run_synthetic_demo
from src.pipeline.package_manifest import verify_frozen_package


class SyntheticDemoTest(unittest.TestCase):
    def test_complete_demo_is_offline_low_risk_and_non_publishing(self) -> None:
        now = datetime(2026, 8, 8, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("synthetic demo attempted a network request"),
        ):
            result = run_synthetic_demo(Path(temp_dir) / "demo", now=now)
            manifest_check = verify_frozen_package(result["files"]["content_package_json"])

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["published"])
        self.assertEqual(result["publish_action"], "dry_run")
        self.assertFalse(result["publish_allowed"])
        self.assertEqual(result["risk_level"], "low")
        self.assertTrue(result["content_package"]["wechat_ready"])
        self.assertGreaterEqual(result["content_package"]["primary_article"]["news_item_count"], 5)
        self.assertGreaterEqual(result["content_package"]["secondary_article"]["news_item_count"], 3)
        self.assertTrue(manifest_check["ok"])


if __name__ == "__main__":
    unittest.main()
