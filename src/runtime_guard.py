from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import asdict, dataclass
from typing import Any, Mapping


EXPECTED_CONFIG_VERSION = "public.v0.1.0"
INSTANCE_ENV_KEY = "WECHAT_AUTOMATION_INSTANCE_ID"


class RuntimeWriteBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeIdentity:
    environment: str
    instance_id: str
    allowed_instance_ids: tuple[str, ...]
    allow_wechat_writes: bool
    allow_external_providers: bool
    config_version: str
    expected_config_version: str
    wechat_write_allowed: bool
    reasons: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_runtime_identity(
    schedule: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> RuntimeIdentity:
    runtime = schedule.get("runtime", {})
    if not isinstance(runtime, Mapping):
        runtime = {}

    values = environ if environ is not None else os.environ
    detected_hostname = str(hostname or socket.gethostname()).strip()
    instance_id = str(values.get(INSTANCE_ENV_KEY, "")).strip() or detected_hostname
    environment = str(runtime.get("environment", "local")).strip().lower() or "local"
    allow_wechat_writes = bool(runtime.get("allow_wechat_writes", False))
    allow_external_providers = bool(runtime.get("allow_external_providers", environment == "production"))
    config_version = str(runtime.get("config_version", "")).strip()
    allowed = _string_tuple(runtime.get("allowed_instance_ids", ()))

    reasons: list[str] = []
    if environment != "production":
        reasons.append("runtime.environment is not production")
    if not allow_wechat_writes:
        reasons.append("runtime.allow_wechat_writes is false")
    if not instance_id:
        reasons.append("runtime instance id is empty")
    if not allowed:
        reasons.append("runtime.allowed_instance_ids is empty")
    elif instance_id not in allowed:
        reasons.append("runtime instance id is not allowlisted")
    if config_version != EXPECTED_CONFIG_VERSION:
        reasons.append("runtime.config_version does not match this release")

    return RuntimeIdentity(
        environment=environment,
        instance_id=instance_id,
        allowed_instance_ids=allowed,
        allow_wechat_writes=allow_wechat_writes,
        allow_external_providers=allow_external_providers,
        config_version=config_version,
        expected_config_version=EXPECTED_CONFIG_VERSION,
        wechat_write_allowed=not reasons,
        reasons=tuple(reasons),
    )


def require_wechat_write_access(
    schedule: Mapping[str, Any],
    *,
    operation: str,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> RuntimeIdentity:
    identity = resolve_runtime_identity(schedule, environ=environ, hostname=hostname)
    if identity.wechat_write_allowed:
        return identity
    reason = "; ".join(identity.reasons) or "runtime identity did not authorize WeChat writes"
    raise RuntimeWriteBlocked(f"{operation} blocked by runtime identity guard: {reason}.")


def config_fingerprint(config: Mapping[str, Any]) -> str:
    normalized = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = []
    return tuple(str(item).strip() for item in items if str(item).strip())
