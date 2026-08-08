from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.review.publish_guard import (
    already_published_today,
    append_audit_event,
    create_manual_confirmation_request,
    load_manual_confirmation,
    record_publish_intent,
    record_publish_lock,
    retry_call,
    update_publish_intent,
)


class PublishGuardTest(unittest.TestCase):
    def test_record_publish_lock_blocks_same_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 6, 3, 8, 30, tzinfo=timezone.utc)

            self.assertFalse(already_published_today(temp_dir, now=now))
            lock_path = record_publish_lock({"title": "test"}, temp_dir, now=now)

            self.assertTrue(lock_path.exists())
            self.assertTrue(already_published_today(temp_dir, now=now))

    def test_audit_event_redacts_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            append_audit_event(
                "test",
                {
                    "access_token": "test-value",
                    "nested": {"app_secret": "placeholder"},
                    "title": "visible",
                },
                temp_dir,
            )

            log_path = Path(temp_dir) / "audit-log.jsonl"
            event = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])

            self.assertEqual(event["payload"]["access_token"], "test...alue")
            self.assertEqual(event["payload"]["nested"]["app_secret"], "plac...lder")
            self.assertEqual(event["payload"]["title"], "visible")

    def test_manual_confirmation_requires_approval_and_operator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = create_manual_confirmation_request(temp_dir, action="publish", title="test")

            pending = load_manual_confirmation(path)
            self.assertFalse(pending.ok)

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["confirmed"] = True
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            missing_operator = load_manual_confirmation(path)
            self.assertFalse(missing_operator.ok)

            payload["operator"] = "operator-a"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            approved = load_manual_confirmation(path)
            self.assertTrue(approved.ok)
            self.assertEqual(approved.operator, "operator-a")

    def test_retry_call_retries_then_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calls = {"count": 0}

            def flaky() -> str:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("temporary")
                return "ok"

            result = retry_call(
                "flaky",
                flaky,
                retry_count=1,
                retry_delay_seconds=0,
                state_dir=temp_dir,
            )

            self.assertEqual(result, "ok")
            self.assertEqual(calls["count"], 2)

    def test_retry_call_can_refuse_retry_for_non_idempotent_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            calls = {"count": 0}

            def unsafe_write() -> str:
                calls["count"] += 1
                raise RuntimeError("ambiguous")

            with self.assertRaises(RuntimeError):
                retry_call(
                    "unsafe_write",
                    unsafe_write,
                    retry_count=3,
                    retry_delay_seconds=0,
                    state_dir=temp_dir,
                    retry_if=lambda _exc: False,
                )

            self.assertEqual(calls["count"], 1)

    def test_publish_intent_blocks_all_same_day_resubmissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2026, 7, 12, 10, 15, tzinfo=timezone.utc)
            payload = {
                "media_id": "media-1",
                "publish_channel": "mass_send_all",
                "artifact_fingerprint": "fingerprint-1",
                "clientmsgid": "client-1",
            }

            first = record_publish_intent(payload, temp_dir, now=now)
            update_publish_intent(first, status="submitted", payload={"msg_id": "123"}, now=now)

            stored = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(stored["status"], "submitted")
            self.assertEqual(stored["result"]["msg_id"], "123")
            with self.assertRaises(RuntimeError):
                record_publish_intent(payload, temp_dir, now=now)
            with self.assertRaises(RuntimeError):
                record_publish_intent({**payload, "media_id": "media-2"}, temp_dir, now=now)


if __name__ == "__main__":
    unittest.main()
