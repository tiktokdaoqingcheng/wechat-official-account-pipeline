from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.integrations.wechat_client import load_env_file


DEFAULT_IMAGE_MODEL = ""
DEFAULT_IMAGE_SIZE = "1536x1024"
DEFAULT_IMAGE_QUALITY = "medium"
DEFAULT_IMAGE_FORMAT = "png"
DEFAULT_IMAGE_API_BASE = "https://api.openai.com/v1"


class OpenAIImageError(RuntimeError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


@dataclass(frozen=True)
class OpenAIImageConfig:
    api_key: str
    model: str = DEFAULT_IMAGE_MODEL
    size: str = DEFAULT_IMAGE_SIZE
    quality: str = DEFAULT_IMAGE_QUALITY
    output_format: str = DEFAULT_IMAGE_FORMAT
    api_base: str = DEFAULT_IMAGE_API_BASE
    organization: str = ""
    project: str = ""
    timeout_seconds: int = 120

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip() and self.model.strip())


def load_openai_image_config(env_path: str | Path = ".env") -> OpenAIImageConfig:
    values = {**load_env_file(env_path), **os.environ}
    api_base = (
        str(values.get("OPENAI_IMAGE_API_BASE", "")).strip()
        or str(values.get("OPENAI_API_BASE", "")).strip()
        or DEFAULT_IMAGE_API_BASE
    )
    return OpenAIImageConfig(
        api_key=(str(values.get("OPENAI_IMAGE_API_KEY", "")).strip() or str(values.get("OPENAI_API_KEY", "")).strip()),
        model=str(values.get("OPENAI_IMAGE_MODEL", DEFAULT_IMAGE_MODEL)).strip() or DEFAULT_IMAGE_MODEL,
        size=str(values.get("OPENAI_IMAGE_SIZE", DEFAULT_IMAGE_SIZE)).strip() or DEFAULT_IMAGE_SIZE,
        quality=str(values.get("OPENAI_IMAGE_QUALITY", DEFAULT_IMAGE_QUALITY)).strip() or DEFAULT_IMAGE_QUALITY,
        output_format=str(values.get("OPENAI_IMAGE_FORMAT", DEFAULT_IMAGE_FORMAT)).strip() or DEFAULT_IMAGE_FORMAT,
        api_base=api_base.rstrip("/"),
        organization=str(values.get("OPENAI_ORG_ID", "")).strip(),
        project=str(values.get("OPENAI_PROJECT_ID", "")).strip(),
        timeout_seconds=int(values.get("OPENAI_IMAGE_TIMEOUT_SECONDS", 120) or 120),
    )


class OpenAIImageClient:
    def __init__(self, config: OpenAIImageConfig):
        if not config.configured:
            raise OpenAIImageError("OPENAI_IMAGE_API_KEY and OPENAI_IMAGE_MODEL are required for image generation.")
        self.config = config
        self.session = requests.Session()

    def generate_image(self, *, prompt: str, output_path: str | Path) -> dict[str, Any]:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        response = self.session.post(
            f"{self.config.api_base}/images/generations",
            headers=self._headers(),
            json={
                "model": self.config.model,
                "prompt": prompt,
                "n": 1,
                "size": self.config.size,
                "quality": self.config.quality,
                "output_format": self.config.output_format,
                "background": "opaque",
            },
            timeout=self.config.timeout_seconds,
        )
        payload = self._json(response)
        if response.status_code >= 400:
            raise OpenAIImageError(
                f"OpenAI image generation failed: HTTP {response.status_code}",
                payload,
            )
        image_b64 = _first_image_value(payload, "b64_json")
        image_url = _first_image_value(payload, "url")
        if image_b64:
            path.write_bytes(base64.b64decode(image_b64))
            source = "b64_json"
        elif image_url:
            path.write_bytes(self._download_image(image_url))
            source = "url"
        else:
            raise OpenAIImageError("OpenAI image response is missing b64_json or url.", payload)
        return {
            "provider": "openai_image_api",
            "model": self.config.model,
            "size": self.config.size,
            "quality": self.config.quality,
            "output_format": self.config.output_format,
            "api_base": self.config.api_base,
            "response_source": source,
            "path": str(path),
        }

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        if self.config.organization:
            headers["OpenAI-Organization"] = self.config.organization
        if self.config.project:
            headers["OpenAI-Project"] = self.config.project
        return headers

    def _download_image(self, url: str) -> bytes:
        response = self.session.get(url, timeout=self.config.timeout_seconds)
        if response.status_code >= 400:
            raise OpenAIImageError(
                f"OpenAI image URL download failed: HTTP {response.status_code}",
                {"status_code": response.status_code, "url": url},
            )
        return response.content

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise OpenAIImageError(
                f"OpenAI image API returned non-JSON response: HTTP {response.status_code}",
                {"status_code": response.status_code, "text": response.text[:500]},
            ) from exc


def _first_image_value(payload: dict[str, Any], key: str) -> str:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return ""
    first = data[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get(key, "")).strip()
