from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
ARTICLE_ROLES = ("primary", "secondary")


def write_run_archive(
    result: dict[str, Any],
    *,
    run_dir: str | Path,
    output_root: str | Path = "outputs",
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    run_path = Path(run_dir)
    archive = {
        "schema_version": "run_archive.v1",
        "created_at": current.isoformat(),
        "run_result": str(run_path / "daily-run-result.json"),
        "status": result.get("status", "unknown"),
        "next_action": result.get("next_action", "unknown"),
        "publishing_mode": result.get("publishing_mode", ""),
        "output_dir": str(run_path),
        "artifacts": _artifact_summary(result),
        "previews": _write_previews(result, run_path),
        "published_articles": result.get("published_articles", []),
    }
    archive_path = run_path / "run-archive.json"
    archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
    archive["archive_path"] = str(archive_path)
    _append_archive_index(output_root, archive)
    return archive


def _artifact_summary(result: dict[str, Any]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {
        "daily_run_result": str(Path(str(result.get("output_dir", ""))) / "daily-run-result.json")
        if result.get("output_dir")
        else "",
        "draft_result": result.get("draft_result", ""),
        "publish_result": result.get("publish_result", ""),
        "schedule_plan": result.get("schedule_plan", ""),
    }

    stages = result.get("stages", [])
    if isinstance(stages, list):
        artifacts["stages"] = stages
        for stage in stages:
            if isinstance(stage, dict) and stage.get("name") == "content_package":
                content_dir = str(stage.get("output_dir", ""))
                artifacts["content_package"] = _content_package_artifacts(content_dir)
                artifacts["news_fetch"] = stage.get("news_fetch", {})
                artifacts["image_generation"] = stage.get("image_generation", {})
    return artifacts


def _content_package_artifacts(content_dir: str) -> dict[str, Any]:
    if not content_dir:
        return {}
    path = Path(content_dir) / "dry-run-result.json"
    if not path.exists():
        return {"output_dir": content_dir}
    try:
        dry_run = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"output_dir": content_dir}
    files = dry_run.get("files", {})
    return {
        "output_dir": content_dir,
        "dry_run_result": str(path),
        "content_package_json": files.get("content_package_json", "") if isinstance(files, dict) else "",
        "primary_markdown": files.get("primary_article_md", "") if isinstance(files, dict) else "",
        "secondary_markdown": files.get("secondary_article_md", "") if isinstance(files, dict) else "",
        "primary_html": files.get("primary_article_html", "") if isinstance(files, dict) else "",
        "secondary_html": files.get("secondary_article_html", "") if isinstance(files, dict) else "",
        "primary_cover": files.get("primary_cover", "") if isinstance(files, dict) else "",
        "secondary_cover": files.get("secondary_cover", "") if isinstance(files, dict) else "",
        "audit_reports": [
            value
            for value in (
                files.get("primary_review_json", "") if isinstance(files, dict) else "",
                files.get("secondary_review_json", "") if isinstance(files, dict) else "",
                files.get("review_json", "") if isinstance(files, dict) else "",
            )
            if value
        ],
    }


def _write_previews(result: dict[str, Any], run_path: Path) -> dict[str, str]:
    content_dir = _content_stage_dir(result)
    if not content_dir:
        return {}
    previews: dict[str, str] = {}
    for role in ARTICLE_ROLES:
        role_dir = Path(content_dir) / role
        article_html = role_dir / "article.html"
        article_json = role_dir / "article.json"
        illustration = role_dir / "illustration.png"
        if not article_html.exists():
            continue
        output = run_path / f"preview-{role}.html"
        body = article_html.read_text(encoding="utf-8-sig")
        if article_json.exists() and illustration.exists():
            try:
                article = json.loads(article_json.read_text(encoding="utf-8-sig"))
                placeholder = str(article.get("illustration_placeholder", "")).strip()
            except (OSError, json.JSONDecodeError):
                placeholder = ""
            if placeholder:
                body = body.replace(placeholder, _relative_uri(output.parent, illustration))
        output.write_text(_preview_page(body, title=f"WeChat preview {role}"), encoding="utf-8")
        previews[role] = str(output)
    return previews


def _content_stage_dir(result: dict[str, Any]) -> str:
    stages = result.get("stages", [])
    if not isinstance(stages, list):
        return ""
    for stage in stages:
        if isinstance(stage, dict) and stage.get("name") == "content_package":
            return str(stage.get("output_dir", ""))
    return ""


def _preview_page(body: str, *, title: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    body {{
      margin: 0;
      background: #eef2f7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(430px, calc(100vw - 24px));
      margin: 24px auto;
      padding: 20px 16px;
      background: #fff;
      box-shadow: 0 14px 40px rgba(15, 23, 42, 0.12);
    }}
  </style>
</head>
<body>
  <main>
{body}
  </main>
</body>
</html>
"""


def _relative_uri(base_dir: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return "file:///" + target.resolve().as_posix()


def _append_archive_index(output_root: str | Path, archive: dict[str, Any]) -> None:
    index_path = Path(output_root) / "archive-index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "created_at": archive["created_at"],
        "status": archive["status"],
        "next_action": archive["next_action"],
        "publishing_mode": archive["publishing_mode"],
        "output_dir": archive["output_dir"],
        "archive_path": archive.get("archive_path", ""),
        "published_articles": archive.get("published_articles", []),
    }
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
