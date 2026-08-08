from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.pipeline.publish_from_draft import (
    AmbiguousPublishSubmissionError,
    _schedule_with_stable_mass_send_clientmsgid,
    evaluate_publish_permission,
    extract_published_articles,
    normalize_mass_send_status,
    normalize_publish_status,
    poll_mass_send_status,
    poll_publish_status,
    submit_publish_request,
)
from src.review.publish_guard import create_manual_confirmation_request
from src.review.publish_policy import PublishDecision


class FakePublishStatusClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls = 0

    def get_free_publish_status(self, access_token: str, publish_id: str) -> dict[str, object]:
        self.calls += 1
        return self.responses.pop(0)


class FakeMassSendStatusClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls = 0

    def get_mass_send_status(self, access_token: str, msg_id: str) -> dict[str, object]:
        self.calls += 1
        return self.responses.pop(0)


class FakeSubmitClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_free_publish(self, access_token: str, media_id: str) -> dict[str, object]:
        self.calls.append(
            {
                "method": "submit_free_publish",
                "access_token": access_token,
                "media_id": media_id,
            }
        )
        return {"publish_id": "publish-id"}

    def send_mass_mpnews_to_all(
        self,
        access_token: str,
        media_id: str,
        *,
        send_ignore_reprint: bool,
        clientmsgid: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "method": "send_mass_mpnews_to_all",
                "access_token": access_token,
                "media_id": media_id,
                "send_ignore_reprint": send_ignore_reprint,
                "clientmsgid": clientmsgid,
            }
        )
        return {"msg_id": "mass-msg-id", "msg_data_id": "mass-data-id"}


class PublishFromDraftTest(unittest.TestCase):
    def test_auto_publish_decision_is_allowed(self) -> None:
        permission = evaluate_publish_permission(
            PublishDecision(
                action="publish",
                allowed=True,
                reason="ok",
            )
        )

        self.assertTrue(permission["ok"])
        self.assertEqual(permission["mode"], "auto_publish_low_risk")

    def test_manual_confirmation_requires_approved_file(self) -> None:
        decision = PublishDecision(
            action="await_manual_confirm",
            allowed=False,
            reason="manual required",
        )

        missing = evaluate_publish_permission(decision)
        self.assertFalse(missing["ok"])

        with tempfile.TemporaryDirectory() as temp_dir:
            path = create_manual_confirmation_request(temp_dir, action="publish", title="test")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["confirmed"] = True
            payload["operator"] = "operator-a"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            approved = evaluate_publish_permission(decision, confirmation_file=str(path))

        self.assertTrue(approved["ok"])
        self.assertEqual(approved["confirmation"]["operator"], "operator-a")

    def test_blocked_policy_cannot_publish(self) -> None:
        permission = evaluate_publish_permission(
            PublishDecision(
                action="dry_run",
                allowed=False,
                reason="dry run",
            )
        )

        self.assertFalse(permission["ok"])
        self.assertEqual(permission["mode"], "blocked")

    def test_normalize_publish_status(self) -> None:
        self.assertEqual(normalize_publish_status({"publish_status": 0})["normalized_status"], "success")
        self.assertEqual(normalize_publish_status({"publish_status": 1})["normalized_status"], "publishing")
        self.assertFalse(normalize_publish_status({"publish_status": "unknown"})["final"])

    def test_normalize_mass_send_status(self) -> None:
        self.assertEqual(normalize_mass_send_status({"msg_status": "SEND_SUCCESS"})["normalized_status"], "success")
        self.assertEqual(normalize_mass_send_status({"msg_status": "SENDING"})["normalized_status"], "sending")
        self.assertEqual(normalize_mass_send_status({"msg_status": "SEND_FAIL"})["normalized_status"], "failed")
        self.assertFalse(normalize_mass_send_status({"msg_status": ""})["final"])

    def test_poll_publish_status_stops_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakePublishStatusClient(
                [
                    {"publish_status": 1},
                    {"publish_status": 0, "article_id": "article-1"},
                    {"publish_status": 3},
                ]
            )

            checks = poll_publish_status(
                client,
                "token",
                "publish-id",
                retry_count=0,
                retry_delay_seconds=0,
                state_dir=Path(temp_dir),
                attempts=3,
                interval_seconds=0,
            )

        self.assertEqual(len(checks), 2)
        self.assertEqual(client.calls, 2)
        self.assertEqual(checks[-1]["normalized_status"], "success")

    def test_poll_mass_send_status_stops_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeMassSendStatusClient(
                [
                    {"msg_id": 123, "msg_status": "SENDING"},
                    {"msg_id": 123, "msg_status": "SEND_SUCCESS"},
                    {"msg_id": 123, "msg_status": "SEND_FAIL"},
                ]
            )

            checks = poll_mass_send_status(
                client,
                "token",
                "123",
                retry_count=0,
                retry_delay_seconds=0,
                state_dir=Path(temp_dir),
                attempts=3,
                interval_seconds=0,
            )

        self.assertEqual(len(checks), 2)
        self.assertEqual(client.calls, 2)
        self.assertEqual(checks[-1]["normalized_status"], "success")

    def test_submit_publish_request_uses_freepublish_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeSubmitClient()

            submitted = submit_publish_request(
                client,
                "token",
                "draft-media-id",
                schedule={"publishing": {"publish_channel": "freepublish"}},
                publish_channel="freepublish",
                retry_count=0,
                retry_delay_seconds=0,
                state_dir=Path(temp_dir),
                now=datetime(2026, 6, 4, 10, 30, tzinfo=timezone.utc),
            )

        self.assertEqual(submitted["publish_id"], "publish-id")
        self.assertEqual(client.calls[0]["method"], "submit_free_publish")

    def test_submit_publish_request_uses_official_account_mass_send_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeSubmitClient()

            submitted = submit_publish_request(
                client,
                "token",
                "draft-media-id",
                schedule={
                    "publishing": {
                        "publish_channel": "mass_send_all",
                        "mass_send_ignore_reprint": True,
                    }
                },
                publish_channel="mass_send_all",
                retry_count=0,
                retry_delay_seconds=0,
                state_dir=Path(temp_dir),
                now=datetime(2026, 6, 4, 10, 30, tzinfo=timezone.utc),
            )

        self.assertEqual(submitted["msg_id"], "mass-msg-id")
        self.assertEqual(submitted["msg_data_id"], "mass-data-id")
        self.assertEqual(submitted["send_ignore_reprint"], 1)
        self.assertNotIn("clientmsgid", submitted)
        self.assertEqual(client.calls[0]["method"], "send_mass_mpnews_to_all")
        self.assertEqual(client.calls[0]["media_id"], "draft-media-id")
        self.assertEqual(client.calls[0]["clientmsgid"], "")

    def test_submit_publish_request_can_use_explicit_mass_send_clientmsgid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeSubmitClient()

            submitted = submit_publish_request(
                client,
                "token",
                "draft-media-id",
                schedule={
                    "publishing": {
                        "publish_channel": "mass_send_all",
                        "mass_send_clientmsgid": "abc123",
                    }
                },
                publish_channel="mass_send_all",
                retry_count=0,
                retry_delay_seconds=0,
                state_dir=Path(temp_dir),
                now=datetime(2026, 6, 4, 10, 30, tzinfo=timezone.utc),
            )

        self.assertEqual(submitted["clientmsgid"], "abc123")
        self.assertEqual(client.calls[0]["clientmsgid"], "abc123")

    def test_mass_send_network_timeout_is_not_blindly_retried(self) -> None:
        class TimeoutClient(FakeSubmitClient):
            def send_mass_mpnews_to_all(self, *args: object, **kwargs: object) -> dict[str, object]:
                self.calls.append({"method": "send_mass_mpnews_to_all"})
                raise requests.Timeout("response lost")

        with tempfile.TemporaryDirectory() as temp_dir:
            client = TimeoutClient()

            with self.assertRaises(AmbiguousPublishSubmissionError):
                submit_publish_request(
                    client,
                    "token",
                    "draft-media-id",
                    schedule={"publishing": {"publish_channel": "mass_send_all"}},
                    publish_channel="mass_send_all",
                    retry_count=3,
                    retry_delay_seconds=0,
                    state_dir=Path(temp_dir),
                    now=datetime(2026, 7, 12, 10, 15, tzinfo=timezone.utc),
                )

        self.assertEqual(len(client.calls), 1)

    def test_stable_mass_send_clientmsgid_is_short_and_deterministic(self) -> None:
        now = datetime(2026, 7, 12, 10, 15, tzinfo=timezone.utc)
        first = _schedule_with_stable_mass_send_clientmsgid(
            {"publishing": {"publish_channel": "mass_send_all"}},
            now=now,
            media_id="media-1",
            artifact_fingerprint="fingerprint-1",
        )
        second = _schedule_with_stable_mass_send_clientmsgid(
            {"publishing": {"publish_channel": "mass_send_all"}},
            now=now,
            media_id="media-1",
            artifact_fingerprint="fingerprint-1",
        )

        clientmsgid = first["publishing"]["mass_send_clientmsgid"]
        self.assertEqual(clientmsgid, second["publishing"]["mass_send_clientmsgid"])
        self.assertEqual(len(clientmsgid), 16)

    def test_extract_published_articles_from_article_detail(self) -> None:
        articles = extract_published_articles(
            [
                {
                    "payload": {
                        "publish_status": 0,
                        "article_detail": {
                            "item": [
                                {"article_id": "a1", "article_url": "https://example.invalid/articles/one"},
                                {"article_id": "a2", "article_url": "https://example.invalid/articles/two"},
                            ]
                        },
                    }
                }
            ],
            article_titles=["one", "two"],
        )

        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]["title"], "one")
        self.assertEqual(articles[1]["url"], "https://example.invalid/articles/two")


if __name__ == "__main__":
    unittest.main()
