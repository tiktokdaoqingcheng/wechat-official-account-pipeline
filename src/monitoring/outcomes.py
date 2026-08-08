from __future__ import annotations

from typing import Any


def business_outcome(result: dict[str, Any]) -> str:
    explicit = str(result.get("business_outcome", "")).strip()
    if explicit:
        return explicit

    next_action = str(result.get("next_action", "")).strip()
    status = str(result.get("status", "")).strip()
    if next_action == "duplicate_skipped":
        return "duplicate_skipped"
    if bool(result.get("published", False)):
        return "published"
    if next_action in {"publish_submitted", "ambiguous_submission"}:
        return "pending"
    if next_action == "draft_created":
        return "draft_created"
    if next_action == "prepared_for_publish":
        return "prepared"
    if status == "blocked" or next_action in {"blocked", "runtime_guard_blocked", "paused"}:
        return "blocked"
    if status == "failed":
        return "failed"
    if next_action == "none":
        return "completed"
    return "pending"


def apply_business_outcome(result: dict[str, Any]) -> dict[str, Any]:
    result["business_outcome"] = business_outcome(result)
    return result
