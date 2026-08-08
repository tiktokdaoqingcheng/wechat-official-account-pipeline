from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


class WeChatApiError(RuntimeError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


@dataclass(frozen=True)
class AccessToken:
    value: str
    expires_at: float

    @property
    def expires_in_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))


def load_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def redact(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


class WeChatClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        api_base: str = "https://api.weixin.qq.com",
        timeout_seconds: int = 20,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def get_access_token(self) -> AccessToken:
        response = self.session.get(
            f"{self.api_base}/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": self.app_id,
                "secret": self.app_secret,
            },
            timeout=self.timeout_seconds,
        )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "get_access_token")

        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token or expires_in <= 0:
            raise WeChatApiError("access_token response is missing token data", payload)
        return AccessToken(value=token, expires_at=time.time() + expires_in)

    def get_material_count(self, access_token: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.api_base}/cgi-bin/material/get_materialcount",
            params={"access_token": access_token},
            timeout=self.timeout_seconds,
        )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "get_material_count")
        return payload

    def get_draft_count(self, access_token: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.api_base}/cgi-bin/draft/count",
            params={"access_token": access_token},
            timeout=self.timeout_seconds,
        )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "get_draft_count")
        return payload

    def upload_permanent_image(self, access_token: str, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        with path.open("rb") as handle:
            response = self.session.post(
                f"{self.api_base}/cgi-bin/material/add_material",
                params={"access_token": access_token, "type": "image"},
                files={"media": (path.name, handle, self._guess_image_mime(path))},
                timeout=self.timeout_seconds,
            )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "upload_permanent_image")
        return payload

    def upload_article_image(self, access_token: str, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        with path.open("rb") as handle:
            response = self.session.post(
                f"{self.api_base}/cgi-bin/media/uploadimg",
                params={"access_token": access_token},
                files={"media": (path.name, handle, self._guess_image_mime(path))},
                timeout=self.timeout_seconds,
            )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "upload_article_image")
        return payload

    def add_draft(self, access_token: str, article: dict[str, Any]) -> dict[str, Any]:
        return self.add_draft_articles(access_token, [article])

    def add_draft_articles(self, access_token: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
        if not articles:
            raise WeChatApiError("add_draft_articles requires at least one article")
        response = self.session.post(
            f"{self.api_base}/cgi-bin/draft/add",
            params={"access_token": access_token},
            data=json.dumps({"articles": articles}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout_seconds,
        )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "add_draft")
        return payload

    def submit_free_publish(self, access_token: str, media_id: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.api_base}/cgi-bin/freepublish/submit",
            params={"access_token": access_token},
            data=json.dumps({"media_id": media_id}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout_seconds,
        )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "submit_free_publish")
        return payload

    def get_free_publish_status(self, access_token: str, publish_id: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.api_base}/cgi-bin/freepublish/get",
            params={"access_token": access_token},
            data=json.dumps({"publish_id": publish_id}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout_seconds,
        )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "get_free_publish_status")
        return payload

    def send_mass_mpnews_to_all(
        self,
        access_token: str,
        media_id: str,
        *,
        send_ignore_reprint: bool = False,
        clientmsgid: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "filter": {"is_to_all": True},
            "mpnews": {"media_id": media_id},
            "msgtype": "mpnews",
            "send_ignore_reprint": 1 if send_ignore_reprint else 0,
        }
        if clientmsgid:
            payload["clientmsgid"] = clientmsgid
        response = self.session.post(
            f"{self.api_base}/cgi-bin/message/mass/sendall",
            params={"access_token": access_token},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout_seconds,
        )
        result = self._json(response)
        self._raise_for_wechat_error(result, "send_mass_mpnews_to_all")
        return result

    def get_mass_send_status(self, access_token: str, msg_id: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.api_base}/cgi-bin/message/mass/get",
            params={"access_token": access_token},
            data=json.dumps({"msg_id": msg_id}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout_seconds,
        )
        payload = self._json(response)
        self._raise_for_wechat_error(payload, "get_mass_send_status")
        return payload

    @staticmethod
    def _guess_image_mime(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix == ".png":
            return "image/png"
        return "application/octet-stream"

    @staticmethod
    def _json(response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise WeChatApiError(
                f"WeChat API returned non-JSON response: HTTP {response.status_code}",
                {"status_code": response.status_code, "text": response.text[:500]},
            ) from exc

    @staticmethod
    def _raise_for_wechat_error(payload: dict[str, Any], operation: str) -> None:
        errcode = payload.get("errcode")
        if errcode not in (None, 0, "0"):
            errmsg = payload.get("errmsg", "unknown error")
            raise WeChatApiError(f"{operation} failed: errcode={errcode}, errmsg={errmsg}", payload)


def client_from_env(env_path: str | Path = ".env") -> WeChatClient:
    values = {**load_env_file(env_path), **os.environ}
    app_id = values.get("WECHAT_APP_ID", "").strip()
    app_secret = values.get("WECHAT_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise WeChatApiError("WECHAT_APP_ID and WECHAT_APP_SECRET are required")
    return WeChatClient(app_id=app_id, app_secret=app_secret)
