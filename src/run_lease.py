from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class DailyRunLease:
    path: Path
    token: str
    acquired: bool
    existing: dict[str, Any] | None = None

    def release(self) -> None:
        if not self.acquired or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        if str(payload.get("token", "")) != self.token:
            return
        self.path.unlink(missing_ok=True)
        self.acquired = False


def acquire_daily_run_lease(
    state_dir: str | Path,
    *,
    now: datetime,
    instance_id: str,
    stale_after: timedelta = timedelta(hours=8),
) -> DailyRunLease:
    lock_dir = Path(state_dir) / "run-leases"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{now:%Y-%m-%d}.json"
    token = secrets.token_hex(16)
    payload = {
        "schema_version": "daily_run_lease.v1",
        "created_at": now.isoformat(),
        "instance_id": instance_id,
        "pid": os.getpid(),
        "token": token,
    }

    for attempt in range(2):
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            return DailyRunLease(path=path, token=token, acquired=True)
        except FileExistsError:
            existing = _read_existing(path)
            reclaimable = _is_stale(existing, now=now, stale_after=stale_after) or _owner_process_is_gone(
                existing,
                instance_id=instance_id,
            )
            if attempt == 0 and reclaimable:
                path.unlink(missing_ok=True)
                continue
            return DailyRunLease(path=path, token=token, acquired=False, existing=existing)

    return DailyRunLease(path=path, token=token, acquired=False, existing=_read_existing(path))


def _read_existing(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_stale(existing: dict[str, Any], *, now: datetime, stale_after: timedelta) -> bool:
    created_at = str(existing.get("created_at", "")).strip()
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None and now.tzinfo is not None:
        created = created.replace(tzinfo=now.tzinfo)
    return now - created > stale_after


def _owner_process_is_gone(existing: dict[str, Any], *, instance_id: str) -> bool:
    if str(existing.get("instance_id", "")).strip() != str(instance_id).strip():
        return False
    try:
        pid = int(existing.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False
