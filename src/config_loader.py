from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8-sig") as handle:
        payload = yaml.safe_load(handle) or {}

    if not isinstance(payload, dict):
        raise ValueError(f"Config root must be an object: {config_path}")
    return payload


def load_layered_yaml(path: str | Path) -> dict[str, Any]:
    return _load_layered_yaml(Path(path), seen=set())


def _load_layered_yaml(path: Path, *, seen: set[Path]) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if resolved in seen:
        raise ValueError(f"Config base_config cycle detected: {resolved}")

    payload = load_yaml(resolved)
    base_value = str(payload.get("base_config", "")).strip()
    if not base_value:
        return payload

    base_path = Path(base_value).expanduser()
    if not base_path.is_absolute():
        base_path = resolved.parent / base_path
    base = _load_layered_yaml(base_path, seen={*seen, resolved})
    overlay = {key: value for key, value in payload.items() if key != "base_config"}
    merged = _deep_merge(base, overlay)
    merged["config_layers"] = {
        "base": str(base_path.resolve()),
        "overlay": str(resolved),
    }
    return merged


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result
