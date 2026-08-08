from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.integrations.wechat_client import load_env_file
from src.runtime_budget import ExecutionBudget


DEFAULT_TEXT_MODEL_API_BASE = "https://api.openai.com/v1"
DEFAULT_SUMMARY_MODEL = ""
DEFAULT_BATCH_MODEL = ""
DEFAULT_POLISH_MODEL = ""
DEFAULT_RETRY_COUNT = 2
MAX_RETRY_COUNT = 2
DEFAULT_RETRY_DELAY_SECONDS = 1.0
TRANSIENT_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
QWEN_REASONING_CONTROL_MODELS = ("qwen",)
GEMINI_NO_THINKING_MODELS = ("gemini-2.5",)
OPTIONAL_REASONING_KEYS = ("enable_thinking", "chat_template_kwargs", "reasoning_effort")


class TextModelError(RuntimeError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


@dataclass(frozen=True)
class TextModelConfig:
    role: str
    enabled: bool
    api_key: str
    model: str
    api_base: str = DEFAULT_TEXT_MODEL_API_BASE
    timeout_seconds: int = 60
    retry_count: int = DEFAULT_RETRY_COUNT
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key.strip() and self.model.strip())


def load_text_model_configs(env_path: str | Path = ".env") -> dict[str, TextModelConfig]:
    values = {**load_env_file(env_path), **os.environ}
    default_base = str(values.get("TEXT_MODEL_API_BASE", DEFAULT_TEXT_MODEL_API_BASE)).strip() or DEFAULT_TEXT_MODEL_API_BASE
    default_key = str(values.get("TEXT_MODEL_API_KEY", "")).strip()
    default_retry_count = _retry_count_value(values.get("TEXT_MODEL_RETRY_COUNT"), DEFAULT_RETRY_COUNT)
    default_retry_delay_seconds = _float_value(
        values.get("TEXT_MODEL_RETRY_DELAY_SECONDS"),
        DEFAULT_RETRY_DELAY_SECONDS,
    )
    return {
        "summary": _role_config(
            values,
            role="summary",
            prefix="TEXT_MODEL_SUMMARY",
            default_model=DEFAULT_SUMMARY_MODEL,
            default_timeout=90,
            default_base=default_base,
            default_key=default_key,
            default_retry_count=default_retry_count,
            default_retry_delay_seconds=default_retry_delay_seconds,
        ),
        "batch": _role_config(
            values,
            role="batch",
            prefix="TEXT_MODEL_BATCH",
            default_model=DEFAULT_BATCH_MODEL,
            default_timeout=120,
            default_base=default_base,
            default_key=default_key,
            default_retry_count=default_retry_count,
            default_retry_delay_seconds=default_retry_delay_seconds,
        ),
        "polish": _role_config(
            values,
            role="polish",
            prefix="TEXT_MODEL_POLISH",
            default_model=DEFAULT_POLISH_MODEL,
            default_timeout=120,
            default_base=default_base,
            default_key=default_key,
            default_retry_count=default_retry_count,
            default_retry_delay_seconds=default_retry_delay_seconds,
        ),
    }


def load_text_model_config(role: str, env_path: str | Path = ".env") -> TextModelConfig:
    configs = load_text_model_configs(env_path)
    try:
        return configs[role]
    except KeyError as exc:
        raise ValueError(f"Unknown text model role: {role}") from exc


class TextModelClient:
    def __init__(
        self,
        config: TextModelConfig,
        *,
        execution_budget: ExecutionBudget | None = None,
        call_ledger: list[dict[str, Any]] | None = None,
    ):
        if not config.configured:
            raise TextModelError(f"Text model role is not configured: {config.role}")
        self.config = config
        self.session = requests.Session()
        self.execution_budget = execution_budget
        self.call_ledger = call_ledger
        self._circuit_open_reason = ""

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.4, max_tokens: int = 1200) -> str:
        request_json = {
            "model": self.config.model,
            "messages": _messages_with_reasoning_disabled(messages, self.config.model),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        request_json.update(_reasoning_control_payload(self.config.model))
        response, payload = self._post_with_retries(request_json)
        if response.status_code >= 400:
            active_request = request_json
            if _response_format_not_supported(payload):
                fallback_json = dict(active_request)
                fallback_json.pop("response_format", None)
                response, payload = self._post_with_retries(fallback_json)
                active_request = fallback_json
                if response.status_code < 400:
                    content = _first_message_content(payload)
                    if not content:
                        raise TextModelError(_missing_content_message(payload), _redacted_payload(payload))
                    return content
            if response.status_code >= 400 and _reasoning_controls_not_supported(payload) and _has_reasoning_controls(active_request):
                fallback_json = _without_reasoning_controls(active_request)
                response, payload = self._post_with_retries(fallback_json)
                if response.status_code < 400:
                    content = _first_message_content(payload)
                    if not content:
                        raise TextModelError(_missing_content_message(payload), _redacted_payload(payload))
                    return content
            raise TextModelError(
                f"Text model request failed: HTTP {response.status_code}",
                _redacted_payload(payload),
            )
        content = _first_message_content(payload)
        if not content and _has_reasoning_content(payload):
            retry_json = _reasoning_content_retry_payload(request_json)
            response, retry_payload = self._post_with_retries(retry_json)
            active_request = retry_json
            if response.status_code >= 400 and _reasoning_controls_not_supported(retry_payload):
                fallback_json = _without_reasoning_controls(active_request)
                response, retry_payload = self._post_with_retries(fallback_json)
            if response.status_code >= 400:
                raise TextModelError(
                    f"Text model request failed: HTTP {response.status_code}",
                    _redacted_payload(retry_payload),
                )
            content = _first_message_content(retry_payload)
            if not content:
                raise TextModelError(_missing_content_message(retry_payload), _redacted_payload(retry_payload))
        if not content:
            raise TextModelError(_missing_content_message(payload), _redacted_payload(payload))
        return content

    def _post_with_retries(self, request_json: dict[str, Any]) -> tuple[requests.Response, dict[str, Any]]:
        if self._circuit_open_reason:
            raise TextModelError(
                f"Text model circuit is open for role {self.config.role}: {self._circuit_open_reason}",
                {"role": self.config.role, "circuit_open": True},
            )
        url = f"{self.config.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        max_attempts = min(MAX_RETRY_COUNT, max(0, self.config.retry_count)) + 1
        failures: list[dict[str, Any]] = []
        for attempt in range(1, max_attempts + 1):
            timeout_seconds = self._request_timeout_seconds()
            if timeout_seconds <= 0:
                self._circuit_open_reason = "execution budget exhausted"
                self._record_call(
                    attempt=attempt,
                    duration_seconds=0.0,
                    outcome="budget_exhausted",
                    timeout_seconds=0.0,
                )
                raise TextModelError(
                    f"Text model role {self.config.role} skipped because the execution budget is exhausted.",
                    {"role": self.config.role, "budget_exhausted": True},
                )
            if self.execution_budget is not None and not self.execution_budget.try_reserve_provider_call():
                self._circuit_open_reason = "provider call budget exhausted"
                self._record_call(
                    attempt=attempt,
                    duration_seconds=0.0,
                    outcome="call_budget_exhausted",
                    timeout_seconds=timeout_seconds,
                )
                raise TextModelError(
                    f"Text model role {self.config.role} skipped because the provider call budget is exhausted.",
                    {"role": self.config.role, "call_budget_exhausted": True},
                )
            started_at = time.perf_counter()
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=request_json,
                    timeout=timeout_seconds,
                )
            except requests.RequestException as exc:
                duration = round(time.perf_counter() - started_at, 3)
                self._record_call(
                    attempt=attempt,
                    duration_seconds=duration,
                    outcome="request_error",
                    timeout_seconds=timeout_seconds,
                    error_type=exc.__class__.__name__,
                )
                failures.append(_request_failure_payload(exc, attempt=attempt))
                if attempt < max_attempts and self._sleep_before_retry(attempt):
                    continue
                if isinstance(exc, requests.Timeout):
                    self._circuit_open_reason = "provider request timed out"
                raise TextModelError(
                    "Text model request failed after retries.",
                    {"attempts": attempt, "failures": failures[-3:]},
                ) from exc
            try:
                payload = _json(response)
            except TextModelError as exc:
                duration = round(time.perf_counter() - started_at, 3)
                self._record_call(
                    attempt=attempt,
                    duration_seconds=duration,
                    outcome="invalid_json",
                    timeout_seconds=timeout_seconds,
                    status_code=response.status_code,
                )
                if (
                    response.status_code in TRANSIENT_HTTP_STATUS_CODES
                    and attempt < max_attempts
                    and self._sleep_before_retry(attempt)
                ):
                    failures.append(
                        {
                            "attempt": attempt,
                            "status_code": response.status_code,
                            "reason": str(exc),
                        }
                    )
                    continue
                if failures:
                    raise TextModelError(
                        str(exc),
                        {"attempts": attempt, "failures": failures[-3:], "payload": exc.payload},
                    ) from exc
                raise
            duration = round(time.perf_counter() - started_at, 3)
            self._record_call(
                attempt=attempt,
                duration_seconds=duration,
                outcome="http_error" if response.status_code >= 400 else "ok",
                timeout_seconds=timeout_seconds,
                status_code=response.status_code,
                usage=_response_usage(payload),
                finish_reason=_response_finish_reason(payload),
            )
            if response.status_code in TRANSIENT_HTTP_STATUS_CODES and attempt < max_attempts:
                if _has_reasoning_controls(request_json) and _reasoning_controls_not_supported(payload):
                    return response, payload
                failures.append(
                    {
                        "attempt": attempt,
                        "status_code": response.status_code,
                        "payload": _redacted_payload(payload),
                    }
                )
                if self._sleep_before_retry(attempt):
                    continue
            if response.status_code in TRANSIENT_HTTP_STATUS_CODES:
                self._circuit_open_reason = (
                    f"provider returned HTTP {response.status_code} after {attempt} attempt(s)"
                )
            return response, payload
        raise TextModelError("Text model request failed after retries.", {"failures": failures[-3:]})

    def _request_timeout_seconds(self) -> float:
        if self.execution_budget is None:
            return float(self.config.timeout_seconds)
        return self.execution_budget.request_timeout(self.config.timeout_seconds, optional=True)

    def _sleep_before_retry(self, attempt: int) -> bool:
        delay = max(0.0, float(self.config.retry_delay_seconds))
        if delay <= 0:
            return True
        actual_delay = min(delay * (2 ** (attempt - 1)), 8.0)
        if self.execution_budget is not None and not self.execution_budget.retry_delay_allowed(actual_delay):
            return False
        time.sleep(actual_delay)
        return True

    def _record_call(
        self,
        *,
        attempt: int,
        duration_seconds: float,
        outcome: str,
        timeout_seconds: float,
        status_code: int | None = None,
        error_type: str = "",
        usage: dict[str, int] | None = None,
        finish_reason: str = "",
    ) -> None:
        if self.call_ledger is None:
            return
        entry: dict[str, Any] = {
            "role": self.config.role,
            "model": self.config.model,
            "attempt": attempt,
            "duration_seconds": duration_seconds,
            "timeout_seconds": round(timeout_seconds, 3),
            "outcome": outcome,
        }
        if status_code is not None:
            entry["status_code"] = status_code
        if error_type:
            entry["error_type"] = error_type
        if usage:
            entry["usage"] = usage
        if finish_reason:
            entry["finish_reason"] = finish_reason
        self.call_ledger.append(entry)


def _role_config(
    values: dict[str, Any],
    *,
    role: str,
    prefix: str,
    default_model: str,
    default_timeout: int,
    default_base: str,
    default_key: str,
    default_retry_count: int,
    default_retry_delay_seconds: float,
) -> TextModelConfig:
    return TextModelConfig(
        role=role,
        enabled=_env_bool(values.get(f"{prefix}_ENABLED", "false")),
        api_key=str(values.get(f"{prefix}_API_KEY", "")).strip() or default_key,
        model=str(values.get(f"{prefix}_MODEL", default_model)).strip() or default_model,
        api_base=str(values.get(f"{prefix}_API_BASE", "")).strip() or default_base,
        timeout_seconds=_int_value(values.get(f"{prefix}_TIMEOUT_SECONDS"), default_timeout),
        retry_count=_retry_count_value(values.get(f"{prefix}_RETRY_COUNT"), default_retry_count),
        retry_delay_seconds=_float_value(values.get(f"{prefix}_RETRY_DELAY_SECONDS"), default_retry_delay_seconds),
    )


def _env_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_value(value: Any, default: int) -> int:
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _retry_count_value(value: Any, default: int) -> int:
    try:
        return min(MAX_RETRY_COUNT, max(0, int(str(value).strip())))
    except (TypeError, ValueError):
        return min(MAX_RETRY_COUNT, max(0, default))


def _float_value(value: Any, default: float) -> float:
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _request_failure_payload(exc: requests.RequestException, *, attempt: int) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "error_type": exc.__class__.__name__,
        "reason": str(exc)[:300],
    }


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TextModelError(
            f"Text model API returned non-JSON response: HTTP {response.status_code}",
            {"status_code": response.status_code, "text": response.text[:500]},
        ) from exc
    return payload if isinstance(payload, dict) else {"payload": payload}


def _first_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def _reasoning_control_payload(model: str) -> dict[str, Any]:
    value = str(model or "").strip().lower()
    if any(marker in value for marker in QWEN_REASONING_CONTROL_MODELS):
        return {
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    if any(marker in value for marker in GEMINI_NO_THINKING_MODELS):
        return {"reasoning_effort": "none"}
    return {}


def _messages_with_reasoning_disabled(messages: list[dict[str, str]], model: str) -> list[dict[str, str]]:
    if not _reasoning_control_payload(model):
        return messages
    instruction = "/no_think\n只输出最终 JSON，不要输出思考过程、推理过程或解释。"
    return _prepend_system_instruction(messages, instruction)


def _prepend_system_instruction(messages: list[dict[str, str]], instruction: str) -> list[dict[str, str]]:
    if messages and str(messages[0].get("role", "")).strip() == "system":
        updated = [dict(message) for message in messages]
        content = str(updated[0].get("content", "")).strip()
        updated[0]["content"] = f"{content}\n{instruction}".strip()
        return updated
    return [{"role": "system", "content": instruction}, *messages]


def _reasoning_content_retry_payload(request_json: dict[str, Any]) -> dict[str, Any]:
    retry = dict(request_json)
    retry.update(_reasoning_control_payload(str(request_json.get("model", ""))))
    try:
        current_max_tokens = int(str(retry.get("max_tokens", 1200)).strip())
    except (TypeError, ValueError):
        current_max_tokens = 1200
    retry["max_tokens"] = max(current_max_tokens, min(current_max_tokens * 2, 3200), 2400)
    retry["temperature"] = min(float(retry.get("temperature", 0.4) or 0.4), 0.2)
    messages = retry.get("messages", [])
    if isinstance(messages, list):
        retry["messages"] = _prepend_system_instruction(
            [dict(message) for message in messages if isinstance(message, dict)],
            "/no_think\n禁用思考模式。不要输出 reasoning_content、thinking 或解释文字。直接输出最终 JSON 对象。",
        )
    return retry


def _has_reasoning_controls(request_json: dict[str, Any]) -> bool:
    return any(key in request_json for key in OPTIONAL_REASONING_KEYS)


def _without_reasoning_controls(request_json: dict[str, Any]) -> dict[str, Any]:
    fallback = dict(request_json)
    for key in OPTIONAL_REASONING_KEYS:
        fallback.pop(key, None)
    return fallback


def _has_reasoning_content(payload: dict[str, Any]) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and str(message.get("reasoning_content", "")).strip():
            return True
    return False


def _missing_content_message(payload: dict[str, Any]) -> str:
    if _has_reasoning_content(payload):
        return "Text model response is missing message content; reasoning_content was returned instead."
    return "Text model response is missing message content."


def _redacted_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = _redact_value(payload)
    return redacted if isinstance(redacted, dict) else {"payload": redacted}


def _response_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            value = int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            result[key] = value
    return result


def _response_finish_reason(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason", "")).strip()


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"api_key", "token", "access_token", "authorization"}:
                result[key] = "[REDACTED]"
            elif normalized == "reasoning_content":
                result[key] = "[REDACTED_REASONING_CONTENT]"
            else:
                result[key] = _redact_value(item)
        return result
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _response_format_not_supported(payload: dict[str, Any]) -> bool:
    text = str(payload).lower()
    return "response_format" in text and any(
        marker in text
        for marker in (
            "unsupported",
            "not support",
            "not_supported",
            "unknown parameter",
            "invalid parameter",
        )
    )


def _reasoning_controls_not_supported(payload: dict[str, Any]) -> bool:
    text = str(payload).lower()
    return any(key in text for key in OPTIONAL_REASONING_KEYS) and any(
        marker in text
        for marker in (
            "unsupported",
            "not support",
            "not_supported",
            "unknown parameter",
            "invalid parameter",
            "not permitted",
            "not allowed",
            "extra inputs",
            "unrecognized",
        )
    )
