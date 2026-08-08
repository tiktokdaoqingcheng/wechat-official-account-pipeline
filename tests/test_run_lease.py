from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.run_lease import acquire_daily_run_lease


class RunLeaseTest(unittest.TestCase):
    def test_second_controller_cannot_acquire_same_day_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            now = datetime(2026, 7, 10, 10, 15, tzinfo=timezone.utc)
            first = acquire_daily_run_lease(state_dir, now=now, instance_id="primary")
            second = acquire_daily_run_lease(state_dir, now=now, instance_id="secondary")

            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)
            self.assertEqual(second.existing["instance_id"], "primary")
            first.release()

            third = acquire_daily_run_lease(state_dir, now=now, instance_id="secondary")
            self.assertTrue(third.acquired)
            third.release()

    def test_stale_lease_can_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            old = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
            first = acquire_daily_run_lease(state_dir, now=old, instance_id="old")
            self.assertTrue(first.acquired)

            current = old + timedelta(hours=9)
            replacement = acquire_daily_run_lease(state_dir, now=current, instance_id="new")

            self.assertTrue(replacement.acquired)
            replacement.release()

    def test_same_instance_can_reclaim_lease_when_owner_process_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            now = datetime(2026, 7, 10, 10, 15, tzinfo=timezone.utc)
            first = acquire_daily_run_lease(state_dir, now=now, instance_id="primary")
            self.assertTrue(first.acquired)

            with patch("src.run_lease.os.kill", side_effect=ProcessLookupError):
                replacement = acquire_daily_run_lease(state_dir, now=now, instance_id="primary")

            self.assertTrue(replacement.acquired)
            replacement.release()

    def test_different_instance_cannot_reclaim_dead_process_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            now = datetime(2026, 7, 10, 10, 15, tzinfo=timezone.utc)
            first = acquire_daily_run_lease(state_dir, now=now, instance_id="primary")
            self.assertTrue(first.acquired)

            with patch("src.run_lease.os.kill", side_effect=ProcessLookupError):
                replacement = acquire_daily_run_lease(state_dir, now=now, instance_id="secondary")

            self.assertFalse(replacement.acquired)
            first.release()


if __name__ == "__main__":
    unittest.main()
