from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.config_loader import load_layered_yaml
from src.integrations.wechat_client import WeChatApiError, client_from_env
from src.pipeline.test_assets import write_test_cover_png
from src.review.publish_guard import append_audit_event, retry_call
from src.runtime_guard import require_wechat_write_access


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a non-published WeChat test draft.")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--schedule", default="config/schedule.yaml")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--title", default=None)
    parser.add_argument("--retry-count", type=int, default=None)
    parser.add_argument("--retry-delay-seconds", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone(timedelta(hours=8), name="Asia/Shanghai"))
    output_dir = Path(args.output_dir or f"outputs/{now:%Y-%m-%d}-test-draft")
    output_dir.mkdir(parents=True, exist_ok=True)
    schedule = load_layered_yaml(args.schedule)
    retry_count, retry_delay_seconds = _retry_settings(
        schedule,
        retry_count=args.retry_count,
        retry_delay_seconds=args.retry_delay_seconds,
    )

    result: dict[str, object] = {
        "status": "unknown",
        "created_at": now.isoformat(),
        "published": False,
        "output_dir": str(output_dir),
        "state_dir": str(Path(args.state_dir)),
        "retry": {
            "retry_count": retry_count,
            "retry_delay_seconds": retry_delay_seconds,
        },
    }

    try:
        runtime_identity = require_wechat_write_access(schedule, operation="create WeChat test draft")
        result["runtime"] = runtime_identity.public_dict()
        cover_path = write_test_cover_png(output_dir / "test-cover.png")
        result["cover_path"] = str(cover_path)

        client = client_from_env(args.env)
        token = retry_call(
            "get_access_token",
            client.get_access_token,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=args.state_dir,
        )
        result["access_token"] = {
            "ok": True,
            "expires_in_seconds": token.expires_in_seconds,
        }

        uploaded = retry_call(
            "upload_permanent_image",
            lambda: client.upload_permanent_image(token.value, cover_path),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=args.state_dir,
        )
        thumb_media_id = uploaded["media_id"]
        result["cover_upload"] = {
            "ok": True,
            "media_id": thumb_media_id,
            "url": uploaded.get("url"),
        }

        title = args.title or f"【接口测试】AI 自动化草稿链路验证 - 可删除 {now:%Y-%m-%d %H:%M}"
        article = {
            "title": title,
            "author": "AI 自动化测试",
            "digest": "这是一篇由自动化系统创建的接口测试草稿，用于验证封面上传和草稿创建链路，不会自动发布。",
            "content": _test_article_html(now),
            "content_source_url": "",
            "thumb_media_id": thumb_media_id,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        result["article_preview"] = {
            "title": article["title"],
            "author": article["author"],
            "digest": article["digest"],
        }

        draft = retry_call(
            "add_draft",
            lambda: client.add_draft(token.value, article),
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            state_dir=args.state_dir,
        )
        result["draft"] = {
            "ok": True,
            "media_id": draft.get("media_id"),
        }
        result["status"] = "ok"
        append_audit_event(
            "wechat_test_draft_created",
            {
                "output_dir": str(output_dir),
                "title": article["title"],
                "cover_upload": result["cover_upload"],
                "draft": result["draft"],
            },
            args.state_dir,
            status="ok",
            now=now,
        )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        if isinstance(exc, WeChatApiError):
            result["payload"] = exc.payload
        append_audit_event(
            "wechat_test_draft_failed",
            {
                "output_dir": str(output_dir),
                "error": str(exc),
                "payload": result.get("payload", {}),
            },
            args.state_dir,
            status="failed",
            now=now,
        )

    (output_dir / "create-test-draft-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status: {result['status']}")
        if result.get("draft"):
            print(f"Draft media_id: {result['draft'].get('media_id')}")
        if result.get("error"):
            print(f"Error: {result['error']}")
        print(f"Result file: {output_dir / 'create-test-draft-result.json'}")

    return 0 if result["status"] == "ok" else 1


def _retry_settings(
    schedule: dict[str, object],
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


def _test_article_html(now: datetime) -> str:
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")
    return f"""
<section>
  <p><strong>这是一篇接口测试草稿，不会自动发布。</strong></p>
  <p>创建时间：中国时间 {created_at}</p>
  <p>本草稿用于验证微信公众号自动化项目的关键链路：</p>
  <p>1. 获取 access token；</p>
  <p>2. 上传封面图到素材库；</p>
  <p>3. 使用封面素材创建图文草稿。</p>
  <p>如果你在公众号后台看到这篇草稿，说明“自动配图 -> 自动创建草稿”的接口链路已经跑通。</p>
  <p>后续正式文章会基于 AI 新闻、工具教学和趋势解读自动生成，并在发布开关允许时进入自动发布流程。</p>
</section>
""".strip()


if __name__ == "__main__":
    raise SystemExit(main())
