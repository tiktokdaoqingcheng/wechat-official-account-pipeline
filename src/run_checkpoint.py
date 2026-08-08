from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_CHECKPOINT_SCHEMA_VERSION = "daily_run_checkpoint.v1"
RUN_CHECKPOINT_FILENAME = "run-state.json"


def load_run_checkpoint(
    run_dir: str | Path,
    *,
    config_fingerprint: str,
    config_version: str,
) -> dict[str, Any]:
    path = Path(run_dir) / RUN_CHECKPOINT_FILENAME
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema_version") != RUN_CHECKPOINT_SCHEMA_VERSION:
        return {}
    stages = payload.get("stages", {})
    if not isinstance(stages, dict):
        return {}
    fingerprint_matches = str(payload.get("config_fingerprint", "")) == str(config_fingerprint)
    version_matches = str(payload.get("config_version", "")) == str(config_version)
    if not fingerprint_matches or not version_matches:
        prepare_budget = stages.get("prepare_budget", {})
        if not isinstance(prepare_budget, dict) or not prepare_budget:
            return {}
        return {
            "schema_version": RUN_CHECKPOINT_SCHEMA_VERSION,
            "created_at": str(payload.get("created_at", "")),
            "config_fingerprint": str(config_fingerprint),
            "config_version": str(config_version),
            "stages": {"prepare_budget": dict(prepare_budget)},
            "checkpoint_migration": {"prepare_budget_carried_forward": True},
        }
    return payload


def record_run_stage(
    run_dir: str | Path,
    *,
    stage: str,
    data: dict[str, Any],
    config_fingerprint: str,
    config_version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = Path(run_dir) / RUN_CHECKPOINT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_run_checkpoint(
        path.parent,
        config_fingerprint=config_fingerprint,
        config_version=config_version,
    )
    current = now or datetime.now(timezone.utc)
    payload = existing or {
        "schema_version": RUN_CHECKPOINT_SCHEMA_VERSION,
        "created_at": current.isoformat(),
        "config_fingerprint": str(config_fingerprint),
        "config_version": str(config_version),
        "stages": {},
    }
    stages = payload.setdefault("stages", {})
    stages[str(stage)] = {**data, "updated_at": current.isoformat()}
    payload["updated_at"] = current.isoformat()
    _atomic_write_json(path, payload)
    return {**payload, "path": str(path)}


def checkpoint_stage(checkpoint: dict[str, Any], stage: str) -> dict[str, Any]:
    stages = checkpoint.get("stages", {}) if isinstance(checkpoint, dict) else {}
    value = stages.get(stage, {}) if isinstance(stages, dict) else {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
