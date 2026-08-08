from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA_VERSION = "content_package_manifest.v1"


def freeze_content_package(
    package_path: str | Path,
    *,
    config_fingerprint: str,
    config_version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    package_file = Path(package_path)
    package = json.loads(package_file.read_text(encoding="utf-8-sig"))
    manifest_path = package_file.parent / "content-package.manifest.json"
    package["artifact_manifest"] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "path": str(manifest_path),
    }
    package_file.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")

    files = _protected_files(package, package_file)
    hashes = {str(path): _sha256_file(path) for path in files}
    artifact_fingerprint = _artifact_fingerprint(hashes)
    current = now or datetime.now(timezone.utc)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": current.isoformat(),
        "package": str(package_file),
        "config_fingerprint": str(config_fingerprint),
        "config_version": str(config_version),
        "artifact_fingerprint": artifact_fingerprint,
        "files": hashes,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**manifest, "path": str(manifest_path)}


def verify_frozen_package(
    package_path: str | Path,
    *,
    manifest_path: str | Path = "",
    expected_config_fingerprint: str = "",
    expected_config_version: str = "",
) -> dict[str, Any]:
    package_file = Path(package_path)
    errors: list[str] = []
    try:
        package = json.loads(package_file.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"ok": False, "errors": [f"content package cannot be read: {exc}"]}

    configured_manifest = package.get("artifact_manifest", {})
    configured_path = ""
    if isinstance(configured_manifest, dict):
        configured_path = str(configured_manifest.get("path", "")).strip()
    manifest_file = Path(manifest_path or configured_path or package_file.parent / "content-package.manifest.json")
    if not manifest_file.exists():
        return {"ok": False, "errors": [f"frozen package manifest is missing: {manifest_file}"]}

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"ok": False, "errors": [f"frozen package manifest cannot be read: {exc}"]}

    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}")
    if expected_config_fingerprint and manifest.get("config_fingerprint") != expected_config_fingerprint:
        errors.append("effective config fingerprint changed after package generation")
    if expected_config_version and manifest.get("config_version") != expected_config_version:
        errors.append("effective config version changed after package generation")

    file_hashes = manifest.get("files", {})
    if not isinstance(file_hashes, dict) or not file_hashes:
        errors.append("manifest files must be a non-empty object")
        file_hashes = {}
    current_hashes: dict[str, str] = {}
    for raw_path, expected_hash in file_hashes.items():
        path = Path(str(raw_path))
        if not path.exists() or not path.is_file():
            errors.append(f"protected artifact is missing: {path}")
            continue
        actual_hash = _sha256_file(path)
        current_hashes[str(path)] = actual_hash
        if actual_hash != str(expected_hash):
            errors.append(f"protected artifact changed after freeze: {path}")

    expected_artifact = str(manifest.get("artifact_fingerprint", "")).strip()
    actual_artifact = _artifact_fingerprint(current_hashes) if current_hashes else ""
    if expected_artifact and actual_artifact and expected_artifact != actual_artifact:
        errors.append("artifact fingerprint changed after freeze")

    return {
        "ok": not errors,
        "errors": errors,
        "path": str(manifest_file),
        "artifact_fingerprint": expected_artifact,
        "config_fingerprint": str(manifest.get("config_fingerprint", "")),
        "config_version": str(manifest.get("config_version", "")),
        "file_count": len(file_hashes),
    }


def _protected_files(package: dict[str, Any], package_file: Path) -> list[Path]:
    paths = [package_file]
    files = package.get("files", {})
    if isinstance(files, dict):
        paths.extend(_existing_paths(files))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        key = str(resolved)
        if key in seen or not resolved.is_file():
            continue
        seen.add(key)
        unique.append(resolved)
    return sorted(unique, key=lambda path: str(path))


def _existing_paths(value: Any) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, dict):
        for item in value.values():
            paths.extend(_existing_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_existing_paths(item))
    elif isinstance(value, str) and value.strip():
        path = Path(value)
        if path.exists() and path.is_file():
            paths.append(path)
    return paths


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_fingerprint(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, value in sorted(hashes.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()
