from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import requests

from src.integrations.text_model_client import (
    TextModelClient,
    TextModelConfig,
    TextModelError,
    load_text_model_config,
    load_text_model_configs,
)
from src.runtime_budget import ExecutionBudget


class TextModelClientTest(unittest.TestCase):
    def test_load_text_model_configs_use_shared_defaults_and_role_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_BASE=https://api.openai.com/v1",
                        "TEXT_MODEL_API_KEY=shared-key",
                        "TEXT_MODEL_SUMMARY_ENABLED=true",
                        "TEXT_MODEL_SUMMARY_MODEL=summary-model",
                        "TEXT_MODEL_BATCH_ENABLED=true",
                        "TEXT_MODEL_BATCH_API_BASE=https://relay.example.com/openai/v1",
                        "TEXT_MODEL_BATCH_API_KEY=batch-key",
                        "TEXT_MODEL_BATCH_MODEL=qwen3.7-plus",
                        "TEXT_MODEL_RETRY_COUNT=3",
                        "TEXT_MODEL_RETRY_DELAY_SECONDS=0.25",
                        "TEXT_MODEL_BATCH_RETRY_COUNT=4",
                        "TEXT_MODEL_BATCH_RETRY_DELAY_SECONDS=0.5",
                        "TEXT_MODEL_POLISH_MODEL=claude-sonnet-4-6",
                    ]
                ),
                encoding="utf-8",
            )

            configs = load_text_model_configs(env_path)

        self.assertTrue(configs["summary"].configured)
        self.assertEqual(configs["summary"].api_base, "https://api.openai.com/v1")
        self.assertEqual(configs["summary"].api_key, "shared-key")
        self.assertEqual(configs["summary"].model, "summary-model")
        self.assertTrue(configs["batch"].configured)
        self.assertEqual(configs["batch"].api_base, "https://relay.example.com/openai/v1")
        self.assertEqual(configs["batch"].api_key, "batch-key")
        self.assertEqual(configs["batch"].model, "qwen3.7-plus")
        self.assertEqual(configs["batch"].timeout_seconds, 120)
        self.assertEqual(configs["summary"].retry_count, 2)
        self.assertEqual(configs["summary"].retry_delay_seconds, 0.25)
        self.assertEqual(configs["batch"].retry_count, 2)
        self.assertEqual(configs["batch"].retry_delay_seconds, 0.5)
        self.assertFalse(configs["polish"].configured)
        self.assertEqual(configs["polish"].model, "claude-sonnet-4-6")

    def test_role_timeout_can_be_lowered_to_fit_execution_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=shared-key",
                        "TEXT_MODEL_BATCH_ENABLED=true",
                        "TEXT_MODEL_BATCH_MODEL=batch-model",
                        "TEXT_MODEL_BATCH_TIMEOUT_SECONDS=60",
                        "TEXT_MODEL_POLISH_ENABLED=true",
                        "TEXT_MODEL_POLISH_MODEL=polish-model",
                        "TEXT_MODEL_POLISH_TIMEOUT_SECONDS=30",
                    ]
                ),
                encoding="utf-8",
            )

            configs = load_text_model_configs(env_path)

        self.assertEqual(configs["batch"].timeout_seconds, 60)
        self.assertEqual(configs["polish"].timeout_seconds, 30)

    def test_load_unknown_role_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            load_text_model_config("unknown-role")

    def test_client_refuses_disabled_or_missing_key_config(self) -> None:
        with self.assertRaises(TextModelError):
            TextModelClient(TextModelConfig(role="batch", enabled=False, api_key="key", model="qwen3.7-plus"))
        with self.assertRaises(TextModelError):
            TextModelClient(TextModelConfig(role="batch", enabled=True, api_key="", model="qwen3.7-plus"))

    def test_enabled_role_requires_explicit_model(self) -> None:
        config = TextModelConfig(role="batch", enabled=True, api_key="placeholder", model="")

        self.assertFalse(config.configured)
        with self.assertRaises(TextModelError):
            TextModelClient(config)

    def test_chat_posts_openai_compatible_payload(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="batch",
                enabled=True,
                api_key="batch-key",
                model="qwen3.7-plus",
                api_base="https://api.openai.com/v1/",
                timeout_seconds=12,
            )
        )
        client.session = FakeSession({"choices": [{"message": {"content": "  候选标题  "}}]})

        result = client.chat([{"role": "user", "content": "写标题"}], temperature=0.2, max_tokens=300)

        self.assertEqual(result, "候选标题")
        self.assertEqual(client.session.last_url, "https://api.openai.com/v1/chat/completions")
        self.assertEqual(client.session.last_timeout, 12)
        self.assertEqual(client.session.headers["Authorization"], "Bearer batch-key")
        self.assertEqual(client.session.payload["model"], "qwen3.7-plus")
        self.assertEqual(client.session.payload["messages"][-1]["content"], "写标题")
        self.assertEqual(client.session.payload["temperature"], 0.2)
        self.assertEqual(client.session.payload["max_tokens"], 300)
        self.assertEqual(client.session.payload["response_format"], {"type": "json_object"})
        self.assertEqual(client.session.payload["enable_thinking"], False)

    def test_chat_clamps_timeout_to_optional_execution_budget_and_records_usage(self) -> None:
        current = [0.0]
        budget = ExecutionBudget(100, publish_reserve_seconds=40, clock=lambda: current[0])
        ledger: list[dict[str, object]] = []
        client = TextModelClient(
            TextModelConfig(
                role="summary",
                enabled=True,
                api_key="summary-key",
                model="gemini-3.5-flash",
                timeout_seconds=90,
            ),
            execution_budget=budget,
            call_ledger=ledger,
        )
        client.session = FakeSession(
            {
                "choices": [{"finish_reason": "stop", "message": {"content": "{\"items\":[]}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

        client.chat([{"role": "user", "content": "总结"}])

        self.assertEqual(client.session.last_timeout, 60)
        self.assertEqual(ledger[0]["outcome"], "ok")
        self.assertEqual(ledger[0]["usage"], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        self.assertEqual(ledger[0]["finish_reason"], "stop")

    def test_chat_skips_request_when_optional_budget_is_exhausted(self) -> None:
        budget = ExecutionBudget(60, publish_reserve_seconds=60, clock=lambda: 0.0)
        ledger: list[dict[str, object]] = []
        client = TextModelClient(
            TextModelConfig(role="batch", enabled=True, api_key="key", model="qwen3.7-plus"),
            execution_budget=budget,
            call_ledger=ledger,
        )
        client.session = FakeSession({"choices": [{"message": {"content": "{}"}}]})

        with self.assertRaises(TextModelError) as context:
            client.chat([{"role": "user", "content": "写稿"}])

        self.assertIn("execution budget is exhausted", str(context.exception))
        self.assertEqual(client.session.last_url, "")
        self.assertEqual(ledger[0]["outcome"], "budget_exhausted")

    def test_chat_stops_before_post_when_provider_call_budget_is_exhausted(self) -> None:
        budget = ExecutionBudget(120, max_provider_calls=1, clock=lambda: 0.0)
        ledger: list[dict[str, object]] = []
        client = TextModelClient(
            TextModelConfig(
                role="batch",
                enabled=True,
                api_key="key",
                model="qwen3.7-plus",
                retry_count=2,
                retry_delay_seconds=0,
            ),
            execution_budget=budget,
            call_ledger=ledger,
        )
        client.session = FakeSessionSequence(
            [
                requests.Timeout("read timed out"),
                ({"choices": [{"message": {"content": "{}"}}]}, 200),
            ]
        )

        with self.assertRaises(TextModelError) as context:
            client.chat([{"role": "user", "content": "写稿"}])

        self.assertIn("provider call budget is exhausted", str(context.exception))
        self.assertEqual(len(client.session.calls), 1)
        self.assertEqual(ledger[-1]["outcome"], "call_budget_exhausted")

    def test_direct_config_retry_count_is_capped_at_three_total_attempts(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="summary",
                enabled=True,
                api_key="key",
                model="gemini-2.5-flash",
                retry_count=99,
                retry_delay_seconds=0,
            )
        )
        client.session = FakeSessionSequence(
            [
                requests.Timeout("first"),
                requests.Timeout("second"),
                requests.Timeout("third"),
                ({"choices": [{"message": {"content": "{}"}}]}, 200),
            ]
        )

        with self.assertRaises(TextModelError):
            client.chat([{"role": "user", "content": "总结"}])

        self.assertEqual(len(client.session.calls), 3)

    def test_chat_adds_qwen_no_think_instruction_to_system_message(self) -> None:
        client = TextModelClient(
            TextModelConfig(role="batch", enabled=True, api_key="batch-key", model="qwen3.7-plus")
        )
        client.session = FakeSession({"choices": [{"message": {"content": "{\"items\":[]}"}}]})

        client.chat(
            [
                {"role": "system", "content": "你是编辑。"},
                {"role": "user", "content": "写标题"},
            ]
        )

        system_message = client.session.payload["messages"][0]["content"]
        self.assertIn("/no_think", system_message)
        self.assertIn("只输出最终 JSON", system_message)
        self.assertEqual(client.session.payload["messages"][1]["content"], "写标题")

    def test_chat_disables_gemini_25_reasoning_budget(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="summary",
                enabled=True,
                api_key="summary-key",
                model="gemini-2.5-flash",
            )
        )
        client.session = FakeSession({"choices": [{"message": {"content": "{\"items\":[]}"}}]})

        client.chat([{"role": "user", "content": "提取事实"}])

        self.assertEqual(client.session.payload["reasoning_effort"], "none")

    def test_chat_does_not_send_reasoning_control_to_non_qwen_models(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="polish",
                enabled=True,
                api_key="polish-key",
                model="claude-sonnet-4-6",
            )
        )
        client.session = FakeSession({"choices": [{"message": {"content": "{\"items\":[]}"}}]})

        result = client.chat([{"role": "user", "content": "润色"}])

        self.assertEqual(result, "{\"items\":[]}")
        self.assertNotIn("enable_thinking", client.session.payload)

    def test_chat_retries_without_json_mode_when_provider_rejects_it(self) -> None:
        client = TextModelClient(
            TextModelConfig(role="batch", enabled=True, api_key="batch-key", model="qwen3.7-plus")
        )
        client.session = FakeSessionSequence(
            [
                ({"error": {"message": "unsupported response_format parameter"}}, 400),
                ({"choices": [{"message": {"content": "{\"title\":\"候选\"}"}}]}, 200),
            ]
        )

        result = client.chat([{"role": "user", "content": "写标题"}])

        self.assertEqual(result, "{\"title\":\"候选\"}")
        self.assertEqual(len(client.session.calls), 2)
        self.assertEqual(client.session.calls[0]["json"]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", client.session.calls[1]["json"])

    def test_chat_retries_without_qwen_reasoning_control_when_provider_rejects_it(self) -> None:
        client = TextModelClient(
            TextModelConfig(role="batch", enabled=True, api_key="batch-key", model="qwen3.7-plus")
        )
        client.session = FakeSessionSequence(
            [
                ({"error": {"message": "unknown parameter: enable_thinking"}}, 400),
                ({"choices": [{"message": {"content": "{\"items\":[]}"}}]}, 200),
            ]
        )

        result = client.chat([{"role": "user", "content": "写标题"}])

        self.assertEqual(result, "{\"items\":[]}")
        self.assertEqual(len(client.session.calls), 2)
        self.assertEqual(client.session.calls[0]["json"]["enable_thinking"], False)
        self.assertNotIn("enable_thinking", client.session.calls[1]["json"])

    def test_chat_retries_without_qwen_reasoning_control_when_transient_status_reports_invalid_param(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="batch",
                enabled=True,
                api_key="batch-key",
                model="qwen3.7-plus",
                retry_count=2,
                retry_delay_seconds=0,
            )
        )
        client.session = FakeSessionSequence(
            [
                ({"error": {"message": "Extra inputs are not permitted, field: 'enable_thinking'"}}, 429),
                ({"choices": [{"message": {"content": "{\"items\":[]}"}}]}, 200),
            ]
        )

        result = client.chat([{"role": "user", "content": "写标题"}])

        self.assertEqual(result, "{\"items\":[]}")
        self.assertEqual(len(client.session.calls), 2)
        self.assertEqual(client.session.calls[0]["json"]["enable_thinking"], False)
        self.assertNotIn("enable_thinking", client.session.calls[1]["json"])

    def test_chat_retries_timeout_then_succeeds(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="batch",
                enabled=True,
                api_key="batch-key",
                model="qwen3.7-plus",
                retry_count=1,
                retry_delay_seconds=0,
            )
        )
        client.session = FakeSessionSequence(
            [
                requests.Timeout("read timed out"),
                ({"choices": [{"message": {"content": "{\"title\":\"恢复成功\"}"}}]}, 200),
            ]
        )

        result = client.chat([{"role": "user", "content": "写标题"}])

        self.assertEqual(result, "{\"title\":\"恢复成功\"}")
        self.assertEqual(len(client.session.calls), 2)

    def test_chat_retries_transient_http_then_succeeds(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="summary",
                enabled=True,
                api_key="summary-key",
                model="gemini-3.5-flash",
                retry_count=1,
                retry_delay_seconds=0,
            )
        )
        client.session = FakeSessionSequence(
            [
                ({"error": {"message": "provider queue timeout"}}, 500),
                ({"choices": [{"message": {"content": "{\"items\":[]}"}}]}, 200),
            ]
        )

        result = client.chat([{"role": "user", "content": "总结"}])

        self.assertEqual(result, "{\"items\":[]}")
        self.assertEqual(len(client.session.calls), 2)

    def test_chat_opens_circuit_after_transient_http_retries_are_exhausted(self) -> None:
        client = TextModelClient(
            TextModelConfig(
                role="summary",
                enabled=True,
                api_key="summary-key",
                model="gemini-3.5-flash",
                retry_count=1,
                retry_delay_seconds=0,
            )
        )
        client.session = FakeSessionSequence(
            [
                ({"error": {"message": "no available distributor"}}, 503),
                ({"error": {"message": "no available distributor"}}, 503),
            ]
        )

        with self.assertRaises(TextModelError) as first_context:
            client.chat([{"role": "user", "content": "总结"}])
        with self.assertRaises(TextModelError) as second_context:
            client.chat([{"role": "user", "content": "继续总结"}])

        self.assertIn("HTTP 503", str(first_context.exception))
        self.assertIn("circuit is open", str(second_context.exception))
        self.assertTrue(second_context.exception.payload["circuit_open"])
        self.assertEqual(len(client.session.calls), 2)

    def test_chat_redacts_error_payload(self) -> None:
        client = TextModelClient(
            TextModelConfig(role="polish", enabled=True, api_key="secret-key", model="claude-sonnet-4-6")
        )
        client.session = FakeSession(
            {"error": "bad key", "api_key": "secret-key", "access_token": "token-value"},
            status_code=401,
        )

        with self.assertRaises(TextModelError) as context:
            client.chat([{"role": "user", "content": "润色"}])

        self.assertEqual(context.exception.payload["api_key"], "[REDACTED]")
        self.assertEqual(context.exception.payload["access_token"], "[REDACTED]")
        self.assertNotIn("secret-key", str(context.exception.payload))

    def test_chat_redacts_reasoning_content_when_message_content_is_empty(self) -> None:
        client = TextModelClient(
            TextModelConfig(role="batch", enabled=True, api_key="batch-key", model="qwen3.7-plus")
        )
        client.session = FakeSession(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "这里是一大段模型思考过程，不应写入日志。",
                        }
                    }
                ]
            }
        )

        with self.assertRaises(TextModelError) as context:
            client.chat([{"role": "user", "content": "写标题"}])

        self.assertIn("reasoning_content was returned instead", str(context.exception))
        payload_text = str(context.exception.payload)
        self.assertIn("[REDACTED_REASONING_CONTENT]", payload_text)
        self.assertNotIn("这里是一大段模型思考过程", payload_text)

    def test_chat_retries_reasoning_only_qwen_response_with_stronger_no_think_controls(self) -> None:
        client = TextModelClient(
            TextModelConfig(role="batch", enabled=True, api_key="batch-key", model="qwen3.7-plus")
        )
        client.session = FakeSessionSequence(
            [
                (
                    {
                        "choices": [
                            {
                                "finish_reason": "length",
                                "message": {
                                    "content": "",
                                    "reasoning_content": "模型仍然输出了思考过程。",
                                },
                            }
                        ]
                    },
                    200,
                ),
                ({"choices": [{"message": {"content": "{\"items\":[]}"}}]}, 200),
            ]
        )

        result = client.chat([{"role": "user", "content": "写标题"}], max_tokens=900)

        self.assertEqual(result, "{\"items\":[]}")
        self.assertEqual(len(client.session.calls), 2)
        retry_payload = client.session.calls[1]["json"]
        self.assertEqual(retry_payload["enable_thinking"], False)
        self.assertEqual(retry_payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertGreaterEqual(retry_payload["max_tokens"], 2400)
        self.assertIn("不要输出 reasoning_content", retry_payload["messages"][0]["content"])


class FakeResponse:
    text = "{}"

    def __init__(self, payload: dict[str, object], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self._payload


class FakeSession:
    def __init__(self, response_payload: dict[str, object], *, status_code: int = 200) -> None:
        self.response_payload = response_payload
        self.status_code = status_code
        self.payload: dict[str, object] = {}
        self.headers: dict[str, str] = {}
        self.last_url = ""
        self.last_timeout = 0

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.last_url = url
        self.payload = kwargs.get("json", {})  # type: ignore[assignment]
        self.headers = kwargs.get("headers", {})  # type: ignore[assignment]
        self.last_timeout = kwargs.get("timeout", 0)  # type: ignore[assignment]
        return FakeResponse(self.response_payload, status_code=self.status_code)


class FakeSessionSequence:
    def __init__(self, responses: list[tuple[dict[str, object], int] | requests.RequestException]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        next_response = self.responses.pop(0)
        if isinstance(next_response, requests.RequestException):
            raise next_response
        payload, status_code = next_response
        return FakeResponse(payload, status_code=status_code)


if __name__ == "__main__":
    unittest.main()
