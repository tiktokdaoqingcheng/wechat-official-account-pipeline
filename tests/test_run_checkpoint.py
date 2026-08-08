from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.run_checkpoint import load_run_checkpoint, record_run_stage


class RunCheckpointTest(unittest.TestCase):
    def test_config_change_carries_only_prepare_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run-state.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "daily_run_checkpoint.v1",
                        "created_at": "2026-08-06T07:00:00+08:00",
                        "config_fingerprint": "old-fingerprint",
                        "config_version": "old-version",
                        "stages": {
                            "prepare_budget": {
                                "status": "completed",
                                "attempts_started": 1,
                                "provider_calls_cumulative": 28,
                            },
                            "content_package": {"status": "completed", "publish_allowed": True},
                            "wechat_draft": {"status": "completed", "draft_media_id": "draft-id"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            checkpoint = load_run_checkpoint(
                temp_dir,
                config_fingerprint="new-fingerprint",
                config_version="new-version",
            )

            self.assertEqual(list(checkpoint["stages"]), ["prepare_budget"])
            self.assertEqual(checkpoint["stages"]["prepare_budget"]["provider_calls_cumulative"], 28)
            self.assertEqual(checkpoint["config_fingerprint"], "new-fingerprint")
            self.assertTrue(checkpoint["checkpoint_migration"]["prepare_budget_carried_forward"])

    def test_record_after_config_change_keeps_budget_and_drops_stale_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_time = datetime(2026, 8, 6, 7, 0, tzinfo=timezone.utc)
            record_run_stage(
                temp_dir,
                stage="prepare_budget",
                data={"status": "running", "attempts_started": 1, "active_call_reservation": 28},
                config_fingerprint="old-fingerprint",
                config_version="old-version",
                now=old_time,
            )
            record_run_stage(
                temp_dir,
                stage="content_package",
                data={"status": "completed", "publish_allowed": True},
                config_fingerprint="old-fingerprint",
                config_version="old-version",
                now=old_time,
            )

            migrated = record_run_stage(
                temp_dir,
                stage="prepare_budget",
                data={
                    "status": "running",
                    "attempts_started": 2,
                    "provider_calls_cumulative": 28,
                    "active_call_reservation": 20,
                },
                config_fingerprint="new-fingerprint",
                config_version="new-version",
                now=datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(list(migrated["stages"]), ["prepare_budget"])
            self.assertEqual(migrated["stages"]["prepare_budget"]["attempts_started"], 2)
            self.assertEqual(migrated["stages"]["prepare_budget"]["provider_calls_cumulative"], 28)
            self.assertEqual(migrated["stages"]["prepare_budget"]["active_call_reservation"], 20)


if __name__ == "__main__":
    unittest.main()
