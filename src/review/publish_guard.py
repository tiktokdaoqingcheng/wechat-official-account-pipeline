from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_STATE_DIR = Path("data/publish-state")
SENSITIVE_KEY_PARTS = (
    "access_token",
    "app_secret",
    "authorization",
    "cookie",
    "password",
    "secret",
    "session",
    "token",
)

T = TypeVar("T")


@dataclass(frozen=True)
class ManualConfirmationResult:
    ok: bool
    path: str
    reason: str
    operator: str = ""
    confirmed_at: str = ""


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def publish_date(now: datetime | None = None) -> str:
    current = now or now_shanghai()
    return current.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d")


def already_published_today(
    state_dir: str | Path = DEFAULT_STATE_DIR,
    *,
    now: datetime | None = None,
) -> bool:
    return _publish_lock_path(state_dir, publish_date(now)).exists()


def record_publish_lock(
    payload: dict[str, Any],
    state_dir: str | Path = DEFAULT_STATE_DIR,
    *,
    now: datetime | None = None,
) -> Path:
    current = now or now_shanghai()
    state_path = _ensure_state_dir(state_dir)
    lock_path = _publish_lock_path(state_path, publish_date(current))
    lock = {
        "publish_date": publish_date(current),
        "recorded_at": current.isoformat(),
        "payload": redact_sensitive(payload),
    }
    _atomic_write_json(lock_path, lock)
    append_audit_event(
        "publish_lock_recorded",
        lock,
        state_path,
        status="ok",
        now=current,
    )
    return lock_path


def record_publish_intent(
    payload: dict[str, Any],
    state_dir: str | Path = DEFAULT_STATE_DIR,
    *,
    now: datetime | None = None,
) -> Path:
    current = now or now_shanghai()
    state_path = _ensure_state_dir(state_dir)
    intent_dir = state_path / "publish-intents"
    intent_dir.mkdir(parents=True, exist_ok=True)
    intent_path = intent_dir / f"{publish_date(current)}.json"
    sanitized = redact_sensitive(payload)
    if intent_path.exists():
        existing = _read_json_object(intent_path)
        existing_payload = existing.get("payload", {}) if isinstance(existing.get("payload", {}), dict) else {}
        identity_keys = ("media_id", "publish_channel", "artifact_fingerprint", "clientmsgid")
        if any(str(existing_payload.get(key, "")) != str(sanitized.get(key, "")) for key in identity_keys):
            raise RuntimeError("A different publish intent already exists for today.")
        raise RuntimeError(
            "A publish intent already exists for today; automatic resubmission is forbidden "
            f"while its status is {existing.get('status', 'unknown')}."
        )
    intent = {
        "publish_date": publish_date(current),
        "recorded_at": current.isoformat(),
        "status": "prepared",
        "payload": sanitized,
    }
    _atomic_write_json(intent_path, intent)
    append_audit_event("publish_intent_recorded", intent, state_path, status="ok", now=current)
    return intent_path


def update_publish_intent(
    intent_path: str | Path,
    *,
    status: str,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    path = Path(intent_path)
    current = now or now_shanghai()
    intent = _read_json_object(path)
    if not intent:
        raise FileNotFoundError(f"Publish intent does not exist: {path}")
    intent["status"] = str(status).strip() or "unknown"
    intent["updated_at"] = current.isoformat()
    if payload:
        updates = intent.get("result", {}) if isinstance(intent.get("result", {}), dict) else {}
        updates.update(redact_sensitive(payload))
        intent["result"] = updates
    _atomic_write_json(path, intent)
    return path


def record_published_links(
    payload: dict[str, Any],
    state_dir: str | Path = DEFAULT_STATE_DIR,
    *,
    now: datetime | None = None,
) -> Path:
    current = now or now_shanghai()
    state_path = _ensure_state_dir(state_dir)
    date_text = publish_date(current)
    snapshot_path = state_path / "published" / f"{date_text}-links.json"
    links_payload = {
        "publish_date": date_text,
        "recorded_at": current.isoformat(),
        "payload": redact_sensitive(payload),
    }
    _atomic_write_json(snapshot_path, links_payload)

    index_path = state_path / "published-links.jsonl"
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(links_payload, ensure_ascii=False) + "\n")
    append_audit_event(
        "published_links_recorded",
        links_payload,
        state_path,
        status="ok",
        now=current,
    )
    return snapshot_path


def append_audit_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    *,
    status: str = "ok",
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or now_shanghai()
    state_path = _ensure_state_dir(state_dir)
    event = {
        "event_id": uuid.uuid4().hex,
        "created_at": current.isoformat(),
        "event_type": event_type,
        "status": status,
        "payload": redact_sensitive(payload or {}),
    }
    with (state_path / "audit-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def create_manual_confirmation_request(
    output_dir: str | Path,
    *,
    action: str,
    title: str,
    decision: dict[str, Any] | None = None,
    review_summary: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    current = now or now_shanghai()
    path = Path(output_dir) / "manual-confirmation.json"
    payload = {
        "status": "pending",
        "created_at": current.isoformat(),
        "action": action,
        "title": title,
        "confirmed": False,
        "operator": "",
        "confirmed_at": "",
        "instructions": "Set confirmed to true and fill operator before rerunning with --confirmation-file.",
        "decision": redact_sensitive(decision or {}),
        "review_summary": redact_sensitive(review_summary or {}),
    }
    _atomic_write_json(path, payload)
    return path


def load_manual_confirmation(path: str | Path) -> ManualConfirmationResult:
    confirm_path = Path(path)
    if not confirm_path.exists():
        return ManualConfirmationResult(
            ok=False,
            path=str(confirm_path),
            reason="Confirmation file does not exist.",
        )

    try:
        payload = json.loads(confirm_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return ManualConfirmationResult(
            ok=False,
            path=str(confirm_path),
            reason=f"Confirmation file is not valid JSON: {exc}",
        )

    if not isinstance(payload, dict):
        return ManualConfirmationResult(
            ok=False,
            path=str(confirm_path),
            reason="Confirmation file root must be an object.",
        )

    confirmed = bool(payload.get("confirmed", False))
    operator = str(payload.get("operator", "")).strip()
    confirmed_at = str(payload.get("confirmed_at", "")).strip()

    if not confirmed:
        return ManualConfirmationResult(
            ok=False,
            path=str(confirm_path),
            reason="Confirmation has not been approved.",
            operator=operator,
            confirmed_at=confirmed_at,
        )
    if not operator:
        return ManualConfirmationResult(
            ok=False,
            path=str(confirm_path),
            reason="Confirmation is missing operator.",
            confirmed_at=confirmed_at,
        )

    return ManualConfirmationResult(
        ok=True,
        path=str(confirm_path),
        reason="Confirmation approved.",
        operator=operator,
        confirmed_at=confirmed_at,
    )


def retry_call(
    operation: str,
    call: Callable[[], T],
    *,
    retry_count: int = 0,
    retry_delay_seconds: float = 0,
    state_dir: str | Path = DEFAULT_STATE_DIR,
    sleep: Callable[[float], None] = time.sleep,
    retry_if: Callable[[Exception], bool] | None = None,
) -> T:
    attempts = max(0, retry_count) + 1
    delay = max(0.0, float(retry_delay_seconds))
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            result = call()
            if attempt > 1:
                append_audit_event(
                    "api_retry_recovered",
                    {"operation": operation, "attempt": attempt, "attempts": attempts},
                    state_dir,
                    status="ok",
                )
            return result
        except Exception as exc:
            last_exc = exc
            append_audit_event(
                "api_call_failed",
                {
                    "operation": operation,
                    "attempt": attempt,
                    "attempts": attempts,
                    "error": str(exc),
                },
                state_dir,
                status="failed",
            )
            if attempt >= attempts:
                break
            if retry_if is not None and not retry_if(exc):
                break
            if delay:
                sleep(delay)

    if last_exc is None:
        raise RuntimeError(f"{operation} failed without an exception")
    raise last_exc


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SENSITIVE_KEY_PARTS):
                redacted[str(key)] = _redact_scalar(item)
            else:
                redacted[str(key)] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def _redact_scalar(value: Any) -> str:
    text = str(value)
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _ensure_state_dir(state_dir: str | Path) -> Path:
    state_path = Path(state_dir)
    (state_path / "published").mkdir(parents=True, exist_ok=True)
    return state_path


def _publish_lock_path(state_dir: str | Path, date_text: str) -> Path:
    return Path(state_dir) / "published" / f"{date_text}.json"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
