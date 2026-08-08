from __future__ import annotations

import argparse
import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_layered_yaml
from src.integrations.wechat_client import WeChatApiError, client_from_env
from src.pipeline.package_manifest import verify_frozen_package
from src.review.publish_guard import append_audit_event, retry_call
from src.runtime_guard import config_fingerprint, require_wechat_write_access


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_ARTICLE_ROLES = ("primary", "secondary", "tertiary")
WECHAT_DRAFT_TITLE_LIMIT = 64
WECHAT_DRAFT_DIGEST_LIMIT = 120
WECHAT_TITLE_SUFFIXES = ("丨智能制造日报", "丨科技早报")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a non-published WeChat draft from a content package.")
    parser.add_argument("--package", required=True, help="Path to content-package.json.")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--retry-count", type=int, default=None)
    parser.add_argument("--retry-delay-seconds", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = create_draft_from_package(
        package_path=args.package,
        env_path=args.env,
        schedule_path=args.schedule,
        output_dir=args.output_dir or None,
        state_dir=args.state_dir,
        retry_count=args.retry_count,
        retry_delay_seconds=args.retry_delay_seconds,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status: {result['status']}")
        if result.get("draft"):
            print(f"Draft media_id: {result['draft'].get('media_id')}")
        if result.get("error"):
            print(f"Error: {result['error']}")
        print(f"Result file: {Path(result['output_dir']) / 'create-draft-result.json'}")
    return 0 if result["status"] == "ok" else 1


def create_draft_from_package(
    *,
    package_path: str | Path,
    env_path: str | Path = ".env",
    schedule_path: str | Path = "config/schedule.yaml",
    output_dir: str | Path | None = None,
    state_dir: str | Path = "data/publish-state",
    retry_count: int | None = None,
    retry_delay_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    package_file = Path(package_path)
    package = json.loads(package_file.read_text(encoding="utf-8-sig"))
    output_path = Path(output_dir) if output_dir else package_file.parent / "wechat-draft"
    output_path.mkdir(parents=True, exist_ok=True)
    schedule = load_layered_yaml(schedule_path)
    configured_retry_count, configured_retry_delay_seconds = _retry_settings(
        schedule,
        retry_count=retry_count,
        retry_delay_seconds=retry_delay_seconds,
    )

    result: dict[str, Any] = {
        "status": "unknown",
        "created_at": current.isoformat(),
        "published": False,
        "package": str(package_file),
        "output_dir": str(output_path),
        "state_dir": str(Path(state_dir)),
        "retry": {
            "retry_count": configured_retry_count,
            "retry_delay_seconds": configured_retry_delay_seconds,
        },
        "article_count": int(package.get("article_count", 0) or 0),
        "effective_config": {
            "fingerprint": config_fingerprint(schedule),
            "version": str(schedule.get("runtime", {}).get("config_version", ""))
            if isinstance(schedule.get("runtime", {}), dict)
            else "",
        },
    }

    try:
        errors = validate_draft_package(package)
        if errors:
            raise ValueError("Invalid content package for draft creation: " + "; ".join(errors))

        runtime_identity = require_wechat_write_access(schedule, operation="create WeChat draft")
        result["runtime"] = runtime_identity.public_dict()
        publishing = schedule.get("publishing", {}) if isinstance(schedule.get("publishing", {}), dict) else {}
        if bool(publishing.get("require_frozen_package", runtime_identity.environment == "production")):
            frozen = verify_frozen_package(
                package_file,
                expected_config_fingerprint=result["effective_config"]["fingerprint"],
                expected_config_version=result["effective_config"]["version"],
            )
            result["artifact_manifest"] = frozen
            if not frozen["ok"]:
                raise ValueError("Frozen content package verification failed: " + "; ".join(frozen["errors"]))
        client = client_from_env(env_path)
        token = retry_call(
            "get_access_token",
            client.get_access_token,
            retry_count=configured_retry_count,
            retry_delay_seconds=configured_retry_delay_seconds,
            state_dir=state_dir,
        )
        result["access_token"] = {"ok": True, "expires_in_seconds": token.expires_in_seconds}

        articles, uploads = _wechat_articles_from_package(
            client=client,
            access_token=token.value,
            package=package,
            retry_count=configured_retry_count,
            retry_delay_seconds=configured_retry_delay_seconds,
            state_dir=state_dir,
        )
        result["uploads"] = uploads
        result["article_preview"] = [
            {
                "title": article["title"],
                "author": article["author"],
                "digest": article["digest"],
            }
            for article in articles
        ]

        draft = retry_call(
            "add_draft_articles",
            lambda: client.add_draft_articles(token.value, articles),
            retry_count=configured_retry_count,
            retry_delay_seconds=configured_retry_delay_seconds,
            state_dir=state_dir,
        )
        result["draft"] = {"ok": True, "media_id": draft.get("media_id")}
        result["status"] = "ok"
        append_audit_event(
            "wechat_package_draft_created",
            {
                "package": str(package_file),
                "article_count": len(articles),
                "titles": [article["title"] for article in articles],
                "draft": result["draft"],
            },
            state_dir,
            status="ok",
            now=current,
        )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        if isinstance(exc, WeChatApiError):
            result["payload"] = exc.payload
        append_audit_event(
            "wechat_package_draft_failed",
            {
                "package": str(package_file),
                "error": str(exc),
                "payload": result.get("payload", {}),
            },
            state_dir,
            status="failed",
            now=current,
        )

    (output_path / "create-draft-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def validate_draft_package(package: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if package.get("schema_version") != "content_package.v1":
        errors.append("schema_version must be content_package.v1")
    roles = _package_roles(package)
    article_count = int(package.get("article_count", 0) or 0)
    if article_count != len(roles) or article_count not in {1, 2, 3}:
        errors.append("article_count must match 1-3 article_roles")
    files = package.get("files")
    if not isinstance(files, dict):
        errors.append("files must be an object")
        return errors

    for role in roles:
        role_files = files.get(role)
        if not isinstance(role_files, dict):
            errors.append(f"files.{role} must be an object")
            continue
        for name in ("article_json", "article_html", "cover", "illustration"):
            path = Path(str(role_files.get(name, "")))
            if not str(path).strip():
                errors.append(f"files.{role}.{name} is required")
            elif not path.exists():
                errors.append(f"files.{role}.{name} does not exist: {path}")
        inline_images = role_files.get("inline_images", [])
        if inline_images and not isinstance(inline_images, list):
            errors.append(f"files.{role}.inline_images must be a list when provided")
        if isinstance(inline_images, list):
            for index, value in enumerate(inline_images, start=1):
                path = Path(str(value))
                if not path.exists():
                    errors.append(f"files.{role}.inline_images[{index}] does not exist: {path}")
    return errors


def _wechat_articles_from_package(
    *,
    client: Any,
    access_token: str,
    package: dict[str, Any],
    retry_count: int,
    retry_delay_seconds: float,
    state_dir: str | Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files = package["files"]
    articles: list[dict[str, Any]] = []
    uploads: list[dict[str, Any]] = []

    for role in _package_roles(package):
        role_files = files[role]
        article_path = Path(str(role_files["article_json"]))
        html_path = Path(str(role_files["article_html"]))
        cover_path = Path(str(role_files["cover"]))
        illustration_path = Path(str(role_files["illustration"]))
        inline_image_paths = [
            Path(str(path))
            for path in role_files.get("inline_images", [])
            if str(path).strip()
        ]
        article = json.loads(article_path.read_text(encoding="utf-8-sig"))

        cover_uploaded = retry_call(
            f"upload_permanent_image:{role}",
            lambda path=cover_path: client.upload_permanent_image(access_token, path),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
        )
        thumb_media_id = str(cover_uploaded.get("media_id", "")).strip()
        if not thumb_media_id:
            raise WeChatApiError("upload_permanent_image response is missing media_id", cover_uploaded)

        article_image_uploaded = retry_call(
            f"upload_article_image:{role}",
            lambda path=illustration_path: client.upload_article_image(access_token, path),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=state_dir,
        )
        illustration_url = str(article_image_uploaded.get("url", "")).strip()
        if not illustration_url:
            raise WeChatApiError("upload_article_image response is missing url", article_image_uploaded)
        inline_uploads = []
        for index, inline_path in enumerate(inline_image_paths, start=1):
            inline_uploaded = retry_call(
                f"upload_article_image:{role}:inline:{index}",
                lambda path=inline_path: client.upload_article_image(access_token, path),
                retry_count=retry_count,
                retry_delay_seconds=retry_delay_seconds,
                state_dir=state_dir,
            )
            inline_url = str(inline_uploaded.get("url", "")).strip()
            if not inline_url:
                raise WeChatApiError("upload_article_image response is missing url", inline_uploaded)
            inline_uploads.append(
                {
                    "index": index,
                    "url": inline_url,
                    "path": str(inline_path),
                }
            )

        uploads.append(
            {
                "role": role,
                "cover": {
                    "media_id": thumb_media_id,
                    "url": cover_uploaded.get("url"),
                    "path": str(cover_path),
                },
                "illustration": {
                    "url": illustration_url,
                    "path": str(illustration_path),
                },
                "inline_images": inline_uploads,
            }
        )
        content = _replace_illustration_placeholder(
            html_path.read_text(encoding="utf-8-sig"),
            placeholder=str(article.get("illustration_placeholder", "")).strip(),
            url=illustration_url,
        )
        content = _replace_inline_image_placeholders(content, article=article, inline_uploads=inline_uploads)
        articles.append(
            {
                "title": _wechat_safe_title(str(article.get("title", "")).strip()),
                "author": str(article.get("author", "AI 自动化编辑")).strip(),
                "digest": _wechat_safe_text(str(article.get("digest", "")).strip(), WECHAT_DRAFT_DIGEST_LIMIT),
                "content": content,
                "content_source_url": "",
                "thumb_media_id": thumb_media_id,
                **_cover_crop_fields(cover_path),
                "need_open_comment": 0,
                "only_fans_can_comment": 0,
            }
        )
    return articles, uploads


def _wechat_safe_title(title: str, max_length: int = WECHAT_DRAFT_TITLE_LIMIT) -> str:
    value = " ".join(str(title or "").strip().split())
    if len(value) <= max_length:
        return value

    suffix = next((item for item in WECHAT_TITLE_SUFFIXES if value.endswith(item)), "")
    if not suffix:
        return _wechat_safe_text(value, max_length)

    max_body_length = max(max_length - len(suffix), 0)
    body = value[: -len(suffix)].rstrip(" 　:：，,；;、。")
    selected: list[str] = []
    for raw_clause in body.split("；"):
        clause = raw_clause.strip(" 　:：，,；;、。")
        if not clause:
            continue
        candidate = "；".join([*selected, clause])
        if len(candidate) <= max_body_length:
            selected.append(clause)
            continue
        used = len("；".join(selected))
        remaining = max_body_length - used - (1 if selected else 0)
        if remaining >= 4:
            selected.append(clause[:remaining].rstrip(" 　:：，,；;、。"))
        break

    trimmed_body = "；".join(selected).rstrip(" 　:：，,；;、。")
    if not trimmed_body:
        trimmed_body = body[:max_body_length].rstrip(" 　:：，,；;、。")
    return (trimmed_body + suffix)[:max_length]


def _wechat_safe_text(value: str, max_length: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip(" 　:：，,；;、。")


def _package_roles(package: dict[str, Any]) -> tuple[str, ...]:
    roles = package.get("article_roles")
    if isinstance(roles, list):
        requested = {str(item) for item in roles}
        normalized = tuple(role for role in MAX_ARTICLE_ROLES if role in requested)
        if normalized:
            return normalized
    try:
        count = int(package.get("article_count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    if count:
        return MAX_ARTICLE_ROLES[: min(max(count, 1), len(MAX_ARTICLE_ROLES))]
    return ("primary", "secondary")


def _replace_illustration_placeholder(content: str, *, placeholder: str, url: str) -> str:
    if not placeholder:
        return content
    return content.replace(placeholder, url)


def _replace_inline_image_placeholders(
    content: str,
    *,
    article: dict[str, Any],
    inline_uploads: list[dict[str, Any]],
) -> str:
    inline_images = article.get("inline_images", [])
    if not isinstance(inline_images, list) or not inline_images:
        return content
    result = content
    for image, uploaded in zip(inline_images, inline_uploads):
        if not isinstance(image, dict):
            continue
        placeholder = str(image.get("placeholder", "")).strip()
        url = str(uploaded.get("url", "")).strip()
        if placeholder and url:
            result = result.replace(placeholder, url)
    return result


def _cover_crop_fields(cover_path: Path) -> dict[str, str]:
    dimensions = _png_dimensions(cover_path)
    if dimensions is None:
        return {
            "pic_crop_235_1": "0_0_1_1",
            "pic_crop_1_1": "0_0_1_1",
        }
    width, height = dimensions
    return {
        "pic_crop_235_1": _crop_box(width, height, target_ratio=2.35),
        "pic_crop_1_1": _crop_box(width, height, target_ratio=1.0),
    }


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n") or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        return None
    return width, height


def _crop_box(width: int, height: int, *, target_ratio: float) -> str:
    image_ratio = width / height
    if image_ratio >= target_ratio:
        crop_width = height * target_ratio
        x1 = (width - crop_width) / (2 * width)
        y1 = 0.0
        x2 = 1.0 - x1
        y2 = 1.0
    else:
        crop_height = width / target_ratio
        x1 = 0.0
        y1 = (height - crop_height) / (2 * height)
        x2 = 1.0
        y2 = 1.0 - y1
    return "_".join(_crop_coord(value) for value in (x1, y1, x2, y2))


def _crop_coord(value: float) -> str:
    clamped = min(1.0, max(0.0, value))
    if abs(clamped) < 0.0001:
        return "0"
    if abs(clamped - 1.0) < 0.0001:
        return "1"
    return f"{clamped:.6f}".rstrip("0").rstrip(".")


def _retry_settings(
    schedule: dict[str, Any],
    *,
    retry_count: int | None,
    retry_delay_seconds: float | None,
) -> tuple[int, float]:
    publishing = schedule.get("publishing", {})
    if not isinstance(publishing, dict):
        publishing = {}

    configured_count = int(publishing.get("retry_count", 0) or 0)
    configured_delay = float(publishing.get("retry_delay_minutes", 0) or 0) * 60
    return (
        min(2, max(0, retry_count if retry_count is not None else configured_count)),
        max(0.0, retry_delay_seconds if retry_delay_seconds is not None else configured_delay),
    )


if __name__ == "__main__":
    raise SystemExit(main())
