from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.integrations.notification_client import notify_daily_run


class NotificationClientTest(unittest.TestCase):
    def test_duplicate_terminal_notification_is_sent_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / ".env"
            env_path.write_text("NOTIFICATION_WEBHOOK_URL=https://example.invalid/hook\n", encoding="utf-8")
            response = Mock(status_code=200)
            response.raise_for_status.return_value = None
            result = {
                "status": "failed",
                "business_outcome": "failed",
                "next_action": "blocked",
                "created_at": "2026-07-10T09:45:00+08:00",
                "reason": "test failure",
            }

            with patch("src.integrations.notification_client.requests.post", return_value=response) as post:
                first = notify_daily_run(result, env_path=env_path, state_dir=root / "state")
                second = notify_daily_run(result, env_path=env_path, state_dir=root / "state")

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "duplicate_skipped")
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
