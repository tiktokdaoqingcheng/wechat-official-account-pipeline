from __future__ import annotations

import tempfile
import unittest
from base64 import b64encode
from pathlib import Path

from src.integrations.openai_image_client import (
    OpenAIImageClient,
    OpenAIImageConfig,
    OpenAIImageError,
    load_openai_image_config,
)


class OpenAIImageClientTest(unittest.TestCase):
    def test_image_provider_requires_explicit_model(self) -> None:
        config = OpenAIImageConfig(api_key="placeholder", model="")

        self.assertFalse(config.configured)
        with self.assertRaises(OpenAIImageError):
            OpenAIImageClient(config)

    def test_load_openai_image_config_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "OPENAI_API_KEY=test-key",
                        "OPENAI_IMAGE_API_BASE=https://relay.example.com/v1/",
                        "OPENAI_IMAGE_API_KEY=relay-key",
                        "OPENAI_IMAGE_MODEL=gpt-image-2",
                        "OPENAI_IMAGE_SIZE=1024x1024",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_openai_image_config(env_path)

        self.assertTrue(config.configured)
        self.assertEqual(config.api_key, "relay-key")
        self.assertEqual(config.api_base, "https://relay.example.com/v1")
        self.assertEqual(config.model, "gpt-image-2")
        self.assertEqual(config.size, "1024x1024")

    def test_generate_image_writes_b64_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"
            client = OpenAIImageClient(OpenAIImageConfig(api_key="test-key", model="image-model"))
            fake_png = b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
            client.session = FakeSession({"data": [{"b64_json": fake_png}]})

            result = client.generate_image(prompt="test prompt", output_path=output)

            self.assertEqual(result["provider"], "openai_image_api")
            self.assertEqual(result["response_source"], "b64_json")
            self.assertEqual(result["path"], str(output))
            self.assertEqual(output.read_bytes(), b"\x89PNG\r\n\x1a\nfake")
            self.assertEqual(client.session.payload["model"], "image-model")
            self.assertEqual(client.session.last_url, "https://api.openai.com/v1/images/generations")

    def test_generate_image_uses_configured_relay_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"
            client = OpenAIImageClient(
                OpenAIImageConfig(
                    api_key="relay-key",
                    model="image-model",
                    api_base="https://relay.example.com/openai/v1",
                )
            )
            fake_png = b64encode(b"\x89PNG\r\n\x1a\nrelay").decode("ascii")
            client.session = FakeSession({"data": [{"b64_json": fake_png}]})

            result = client.generate_image(prompt="test prompt", output_path=output)

            self.assertEqual(result["api_base"], "https://relay.example.com/openai/v1")
            self.assertEqual(client.session.last_url, "https://relay.example.com/openai/v1/images/generations")

    def test_generate_image_downloads_url_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "image.png"
            client = OpenAIImageClient(OpenAIImageConfig(api_key="relay-key", model="image-model"))
            client.session = FakeSession({"data": [{"url": "https://cdn.example.com/image.png"}]})

            result = client.generate_image(prompt="test prompt", output_path=output)

            self.assertEqual(result["response_source"], "url")
            self.assertEqual(client.session.last_download_url, "https://cdn.example.com/image.png")
            self.assertEqual(output.read_bytes(), b"\x89PNG\r\n\x1a\nurl")


class FakeResponse:
    status_code = 200
    text = "{}"

    def __init__(self, payload: dict[str, object], *, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content

    def json(self) -> dict[str, object]:
        return self._payload


class FakeSession:
    def __init__(self, response_payload: dict[str, object]) -> None:
        self.response_payload = response_payload
        self.payload: dict[str, object] = {}
        self.last_url = ""
        self.last_download_url = ""

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.last_url = url
        self.payload = kwargs.get("json", {})  # type: ignore[assignment]
        return FakeResponse(self.response_payload)

    def get(self, url: str, **_kwargs: object) -> FakeResponse:
        self.last_download_url = url
        return FakeResponse({}, content=b"\x89PNG\r\n\x1a\nurl")


if __name__ == "__main__":
    unittest.main()
