from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


VALID_PUBLISH_MODES = {
    "dry_run",
    "draft_only",
    "manual_confirm",
    "auto_publish_low_risk",
}


@dataclass(frozen=True)
class PublishDecision:
    action: str
    allowed: bool
    reason: str
    gates: list[dict[str, Any]] = field(default_factory=list)


def decide_publish_action(
    schedule_config: dict[str, Any],
    review_report: dict[str, Any],
    *,
    already_published_today: bool = False,
) -> PublishDecision:
    publishing = schedule_config.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}

    mode = publishing.get("mode", "dry_run")
    if mode not in VALID_PUBLISH_MODES:
        return PublishDecision(
            action="hold",
            allowed=False,
            reason=f"Invalid publishing mode: {mode}",
        )

    gates = _evaluate_gates(
        review_report,
        already_published_today=already_published_today,
    )
    failed_gates = [gate for gate in gates if not gate["ok"]]

    if mode == "dry_run":
        return PublishDecision(
            action="dry_run",
            allowed=False,
            reason="Publishing mode is dry_run; no WeChat write or publish action is allowed.",
            gates=gates,
        )

    if mode == "draft_only":
        if _risk_level(review_report) == "high" and publishing.get("reject_high_risk", True):
            return PublishDecision(
                action="reject",
                allowed=False,
                reason="High-risk content is rejected before draft creation.",
                gates=gates,
            )
        return PublishDecision(
            action="create_draft",
            allowed=True,
            reason="Publishing mode is draft_only; create a draft but do not publish.",
            gates=gates,
        )

    if mode == "manual_confirm":
        if failed_gates:
            return PublishDecision(
                action="hold",
                allowed=False,
                reason="Manual confirmation mode is enabled, but one or more safety gates failed.",
                gates=gates,
            )
        return PublishDecision(
            action="await_manual_confirm",
            allowed=False,
            reason="Content passed safety gates, but explicit human confirmation is required.",
            gates=gates,
        )

    if mode == "auto_publish_low_risk":
        if not publishing.get("allow_low_risk_auto_publish", False):
            return PublishDecision(
                action="hold",
                allowed=False,
                reason="auto_publish_low_risk mode is selected, but allow_low_risk_auto_publish is false.",
                gates=gates,
            )
        if failed_gates:
            return PublishDecision(
                action="hold",
                allowed=False,
                reason="Automatic publish blocked by failed safety gates.",
                gates=gates,
            )
        return PublishDecision(
            action="publish",
            allowed=True,
            reason="Low-risk content passed all gates and automatic publishing is enabled.",
            gates=gates,
        )

    return PublishDecision(action="hold", allowed=False, reason="Unhandled publishing mode.", gates=gates)


def _evaluate_gates(
    review_report: dict[str, Any],
    *,
    already_published_today: bool,
) -> list[dict[str, Any]]:
    risk = _risk_level(review_report)
    flags = review_report.get("flags", {})
    if not isinstance(flags, dict):
        flags = {}

    return [
        {
            "name": "risk_level_low",
            "ok": risk == "low",
            "value": risk,
        },
        {
            "name": "fact_confidence",
            "ok": not flags.get("fact_uncertain", False),
            "value": flags.get("fact_uncertain", False),
        },
        {
            "name": "sensitive_topic",
            "ok": not flags.get("sensitive_topic", False),
            "value": flags.get("sensitive_topic", False),
        },
        {
            "name": "copyright",
            "ok": not flags.get("copyright_unclear", False),
            "value": flags.get("copyright_unclear", False),
        },
        {
            "name": "duplicate_today",
            "ok": not already_published_today,
            "value": already_published_today,
        },
        {
            "name": "wechat_ready",
            "ok": bool(review_report.get("wechat_ready", False)),
            "value": bool(review_report.get("wechat_ready", False)),
        },
    ]


def _risk_level(review_report: dict[str, Any]) -> str:
    value = str(review_report.get("risk_level", "unknown")).strip().lower()
    if value in {"low", "medium", "high"}:
        return value
    return "unknown"
