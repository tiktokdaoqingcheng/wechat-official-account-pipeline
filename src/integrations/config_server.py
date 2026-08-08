from __future__ import annotations

import argparse
import html
import json
import secrets
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import yaml

from src.config_loader import load_layered_yaml, load_yaml
from src.integrations.wechat_client import load_env_file
from src.monitoring.status_dashboard import build_operations_status
from src.runtime_guard import config_fingerprint


DEFAULT_ENV_PATH = ".env"
DEFAULT_SCHEDULE_PATH = "config/schedule.yaml"
DEFAULT_TOPICS_PATH = "config/topics.yaml"
DEFAULT_NEWS_SOURCES_PATH = "config/news-sources.yaml"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

CONFIG_KEYS = (
    "TEXT_MODEL_API_BASE",
    "TEXT_MODEL_API_KEY",
    "TEXT_MODEL_RETRY_COUNT",
    "TEXT_MODEL_RETRY_DELAY_SECONDS",
    "TEXT_MODEL_SUMMARY_ENABLED",
    "TEXT_MODEL_SUMMARY_API_BASE",
    "TEXT_MODEL_SUMMARY_API_KEY",
    "TEXT_MODEL_SUMMARY_MODEL",
    "TEXT_MODEL_SUMMARY_TIMEOUT_SECONDS",
    "TEXT_MODEL_SUMMARY_RETRY_COUNT",
    "TEXT_MODEL_SUMMARY_RETRY_DELAY_SECONDS",
    "TEXT_MODEL_BATCH_ENABLED",
    "TEXT_MODEL_BATCH_API_BASE",
    "TEXT_MODEL_BATCH_API_KEY",
    "TEXT_MODEL_BATCH_MODEL",
    "TEXT_MODEL_BATCH_TIMEOUT_SECONDS",
    "TEXT_MODEL_BATCH_RETRY_COUNT",
    "TEXT_MODEL_BATCH_RETRY_DELAY_SECONDS",
    "TEXT_MODEL_POLISH_ENABLED",
    "TEXT_MODEL_POLISH_API_BASE",
    "TEXT_MODEL_POLISH_API_KEY",
    "TEXT_MODEL_POLISH_MODEL",
    "TEXT_MODEL_POLISH_TIMEOUT_SECONDS",
    "TEXT_MODEL_POLISH_RETRY_COUNT",
    "TEXT_MODEL_POLISH_RETRY_DELAY_SECONDS",
    "OPENAI_IMAGE_API_BASE",
    "OPENAI_IMAGE_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_IMAGE_MODEL",
    "OPENAI_IMAGE_SIZE",
    "OPENAI_IMAGE_QUALITY",
    "OPENAI_IMAGE_FORMAT",
    "OPENAI_IMAGE_TIMEOUT_SECONDS",
    "NOTIFICATION_WEBHOOK_URL",
    "NOTIFICATION_WEBHOOK_TYPE",
    "NOTIFICATION_WEBHOOK_TOKEN",
    "NOTIFICATION_TIMEOUT_SECONDS",
    "NOTIFICATION_ON_SUCCESS",
    "NOTIFICATION_ON_FAILURE",
    "NOTIFICATION_ON_BLOCKED",
)
SECRET_KEYS = {
    "TEXT_MODEL_API_KEY",
    "TEXT_MODEL_SUMMARY_API_KEY",
    "TEXT_MODEL_BATCH_API_KEY",
    "TEXT_MODEL_POLISH_API_KEY",
    "OPENAI_IMAGE_API_KEY",
    "OPENAI_API_KEY",
    "NOTIFICATION_WEBHOOK_URL",
    "NOTIFICATION_WEBHOOK_TOKEN",
}
BOOLEAN_ENV_KEYS = {
    "TEXT_MODEL_SUMMARY_ENABLED",
    "TEXT_MODEL_BATCH_ENABLED",
    "TEXT_MODEL_POLISH_ENABLED",
    "NOTIFICATION_ON_SUCCESS",
    "NOTIFICATION_ON_FAILURE",
    "NOTIFICATION_ON_BLOCKED",
}
DEFAULTS = {
    "TEXT_MODEL_API_BASE": "https://api.openai.com/v1",
    "TEXT_MODEL_RETRY_COUNT": "2",
    "TEXT_MODEL_RETRY_DELAY_SECONDS": "1",
    "TEXT_MODEL_SUMMARY_ENABLED": "false",
    "TEXT_MODEL_SUMMARY_MODEL": "",
    "TEXT_MODEL_SUMMARY_TIMEOUT_SECONDS": "90",
    "TEXT_MODEL_BATCH_ENABLED": "false",
    "TEXT_MODEL_BATCH_MODEL": "",
    "TEXT_MODEL_BATCH_TIMEOUT_SECONDS": "120",
    "TEXT_MODEL_POLISH_ENABLED": "false",
    "TEXT_MODEL_POLISH_MODEL": "",
    "TEXT_MODEL_POLISH_TIMEOUT_SECONDS": "120",
    "OPENAI_IMAGE_API_BASE": "https://api.openai.com/v1",
    "OPENAI_IMAGE_MODEL": "",
    "OPENAI_IMAGE_SIZE": "1536x1024",
    "OPENAI_IMAGE_QUALITY": "medium",
    "OPENAI_IMAGE_FORMAT": "png",
    "OPENAI_IMAGE_TIMEOUT_SECONDS": "120",
    "NOTIFICATION_WEBHOOK_TYPE": "generic",
    "NOTIFICATION_TIMEOUT_SECONDS": "12",
    "NOTIFICATION_ON_SUCCESS": "true",
    "NOTIFICATION_ON_FAILURE": "true",
    "NOTIFICATION_ON_BLOCKED": "true",
}
TEXT_MODEL_GROUPS = (
    {
        "prefix": "TEXT_MODEL_SUMMARY",
        "title": "新闻事实提取模型",
        "description": "填写供应商提供的模型 ID。该角色只提取事实卡片，不直接写最终文案。",
    },
    {
        "prefix": "TEXT_MODEL_BATCH",
        "title": "标题与短文案候选模型",
        "description": "填写供应商提供的模型 ID。该角色生成标题候选、橙色开头和短摘要。",
    },
    {
        "prefix": "TEXT_MODEL_POLISH",
        "title": "终稿审核模型",
        "description": "填写供应商提供的模型 ID。该角色只审核最终可见稿并返回问题。",
    },
)
PUBLISHING_MODES = ("dry_run", "draft_only", "manual_confirm", "auto_publish_low_risk")
PUBLISH_CHANNELS = ("freepublish", "mass_send_all")
FINAL_REVIEW_MODES = ("disabled", "shadow", "enforce")
WEBHOOK_TYPES = ("generic", "feishu", "dingtalk", "wecom")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local settings page for automation configuration.")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH)
    parser.add_argument("--schedule", default=DEFAULT_SCHEDULE_PATH)
    parser.add_argument("--topics", default=DEFAULT_TOPICS_PATH)
    parser.add_argument("--news-sources", default=DEFAULT_NEWS_SOURCES_PATH)
    parser.add_argument("--output-root", default="outputs")
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--current-release", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    token = secrets.token_urlsafe(18)
    server = _build_server(
        args.host,
        args.port,
        env_path=Path(args.env),
        schedule_path=Path(args.schedule),
        topics_path=Path(args.topics),
        news_sources_path=Path(args.news_sources),
        output_root=Path(args.output_root),
        state_dir=Path(args.state_dir),
        current_release=Path(args.current_release) if args.current_release else Path.cwd(),
        token=token,
    )
    url = f"http://{args.host}:{args.port}/?token={token}"
    print(f"Config page: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def _build_server(
    host: str,
    port: int,
    *,
    env_path: Path,
    schedule_path: Path,
    topics_path: Path,
    news_sources_path: Path,
    output_root: Path,
    state_dir: Path,
    current_release: Path,
    token: str,
) -> ThreadingHTTPServer:
    class ConfigHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send_text("Unauthorized", status=403)
                return
            request_path = self.path.split("?", 1)[0]
            if request_path == "/status.json":
                self._send_json(
                    _operations_status(
                        schedule_path=schedule_path,
                        output_root=output_root,
                        state_dir=state_dir,
                        current_release=current_release,
                        env_path=env_path,
                    )
                )
                return
            self._send_html(
                _render_page(
                    env_path,
                    schedule_path,
                    topics_path,
                    news_sources_path,
                    token,
                    output_root=output_root,
                    state_dir=state_dir,
                    current_release=current_release,
                )
            )

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._send_text("Unauthorized", status=403)
                return
            if _prepared_run_blocks_config_change(output_root=output_root, state_dir=state_dir):
                self._send_text(
                    "Configuration is frozen after today's draft preparation and before publish completion.",
                    status=409,
                )
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length).decode("utf-8")
            form = {key: values[-1] if values else "" for key, values in parse_qs(raw, keep_blank_values=True).items()}
            env_saved = update_env_file(env_path, form)
            config_saved = update_yaml_configs(schedule_path, topics_path, news_sources_path, form)
            notice = f"已保存 {env_saved} 项环境配置、{config_saved} 项运行配置。"
            self._send_html(
                _render_page(
                    env_path,
                    schedule_path,
                    topics_path,
                    news_sources_path,
                    token,
                    notice=notice,
                    output_root=output_root,
                    state_dir=state_dir,
                    current_release=current_release,
                )
            )

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _authorized(self) -> bool:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            values = parse_qs(query)
            return values.get("token", [""])[-1] == token

        def _send_html(self, body: str, *, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_text(self, body: str, *, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer((host, port), ConfigHandler)


def update_env_file(env_path: str | Path, form: dict[str, str]) -> int:
    path = Path(env_path)
    current = load_env_file(path)
    updates: dict[str, str] = {}
    for key in CONFIG_KEYS:
        if key in BOOLEAN_ENV_KEYS:
            updates[key] = "true" if key in form else "false"
            continue
        if key in SECRET_KEYS:
            secret_value = str(form.get(key, "")).strip()
            if secret_value:
                updates[key] = secret_value
            elif key in current:
                updates[key] = current[key]
            continue
        value = str(form.get(key, "")).strip()
        if value:
            updates[key] = value
        elif key in DEFAULTS:
            updates[key] = DEFAULTS[key]
        else:
            updates[key] = ""
    write_env_updates(path, updates)
    return len(updates)


def write_env_updates(env_path: str | Path, updates: dict[str, str]) -> None:
    path = Path(env_path)
    existing_lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    seen: set[str] = set()
    output_lines: list[str] = []
    for line in existing_lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            output_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output_lines.append(line)

    missing = [key for key in CONFIG_KEYS if key not in seen]
    if missing:
        if output_lines and output_lines[-1].strip():
            output_lines.append("")
        output_lines.append("# Automation settings")
        for key in missing:
            output_lines.append(f"{key}={updates.get(key, '')}")

    _atomic_write_text(path, "\n".join(output_lines) + "\n")


def update_yaml_configs(
    schedule_path: str | Path,
    topics_path: str | Path,
    news_sources_path: str | Path,
    form: dict[str, str],
) -> int:
    saved = 0
    saved += _update_schedule(schedule_path, form)
    saved += _update_topics(topics_path, form)
    saved += _update_news_sources(news_sources_path, form)
    return saved


def _update_schedule(schedule_path: str | Path, form: dict[str, str]) -> int:
    if not any(key.startswith(("DAILY_RUN_", "PUBLISHING_", "FINAL_REVIEW_")) for key in form):
        return 0
    path = Path(schedule_path)
    config = load_yaml(path)
    daily_run = config.setdefault("daily_run", {})
    publishing = config.setdefault("publishing", {})
    final_review = config.setdefault("final_review", {})
    if not isinstance(daily_run, dict) or not isinstance(publishing, dict) or not isinstance(final_review, dict):
        raise ValueError("daily_run, publishing and final_review must be YAML objects.")

    daily_run["enabled"] = _checkbox(form, "DAILY_RUN_ENABLED")
    daily_run["paused"] = _checkbox(form, "DAILY_RUN_PAUSED")
    if form.get("DAILY_RUN_CONTROLLER_TIME", "").strip():
        daily_run["controller_start_time"] = form["DAILY_RUN_CONTROLLER_TIME"].strip()
    if form.get("DAILY_RUN_PUBLISH_TIME", "").strip():
        daily_run["publish_time"] = form["DAILY_RUN_PUBLISH_TIME"].strip()
    daily_run["max_runtime_seconds"] = _int_value(
        form.get("DAILY_RUN_MAX_RUNTIME_SECONDS"),
        int(daily_run.get("max_runtime_seconds", 840) or 840),
    )
    daily_run["publish_reserve_seconds"] = _int_value(
        form.get("DAILY_RUN_PUBLISH_RESERVE_SECONDS"),
        int(daily_run.get("publish_reserve_seconds", 120) or 120),
    )
    daily_run["max_prepare_attempts_per_day"] = min(
        3,
        max(1, _int_value(form.get("DAILY_RUN_MAX_PREPARE_ATTEMPTS"), 3)),
    )
    daily_run["max_text_model_calls_per_attempt"] = min(
        30,
        max(1, _int_value(form.get("DAILY_RUN_MAX_TEXT_MODEL_CALLS_PER_ATTEMPT"), 28)),
    )
    daily_run["max_text_model_calls_per_day"] = min(
        50,
        max(1, _int_value(form.get("DAILY_RUN_MAX_TEXT_MODEL_CALLS_PER_DAY"), 36)),
    )

    mode = form.get("PUBLISHING_MODE", "").strip()
    if mode in PUBLISHING_MODES:
        publishing["mode"] = mode
    publish_channel = form.get("PUBLISHING_PUBLISH_CHANNEL", "").strip()
    if publish_channel in PUBLISH_CHANNELS:
        publishing["publish_channel"] = publish_channel
    publishing["allow_low_risk_auto_publish"] = _checkbox(form, "PUBLISHING_ALLOW_LOW_RISK_AUTO_PUBLISH")
    publishing["require_human_confirmation_for_medium_risk"] = _checkbox(
        form,
        "PUBLISHING_REQUIRE_HUMAN_CONFIRMATION_FOR_MEDIUM_RISK",
    )
    publishing["reject_high_risk"] = _checkbox(form, "PUBLISHING_REJECT_HIGH_RISK")
    publishing["allow_image_fallback_for_publish"] = _checkbox(form, "PUBLISHING_ALLOW_IMAGE_FALLBACK_FOR_PUBLISH")
    publishing["mass_send_ignore_reprint"] = _checkbox(form, "PUBLISHING_MASS_SEND_IGNORE_REPRINT")
    publishing["retry_count"] = min(
        2,
        max(0, _int_value(form.get("PUBLISHING_RETRY_COUNT"), int(publishing.get("retry_count", 0) or 0))),
    )
    publishing["retry_delay_minutes"] = _int_value(
        form.get("PUBLISHING_RETRY_DELAY_MINUTES"),
        int(publishing.get("retry_delay_minutes", 0) or 0),
    )
    final_review_mode = form.get("FINAL_REVIEW_MODE", "").strip()
    if final_review_mode in FINAL_REVIEW_MODES:
        final_review["mode"] = final_review_mode
    final_review["auto_repair"] = _checkbox(form, "FINAL_REVIEW_AUTO_REPAIR")
    final_review["max_recovery_cycles"] = min(
        3,
        max(
            1,
            _int_value(
                form.get("FINAL_REVIEW_MAX_RECOVERY_CYCLES"),
                int(final_review.get("max_recovery_cycles", 1) or 1),
            ),
        ),
    )
    final_review["minimum_cycle_budget_seconds"] = _int_value(
        form.get("FINAL_REVIEW_MINIMUM_CYCLE_BUDGET_SECONDS"),
        int(final_review.get("minimum_cycle_budget_seconds", 45) or 45),
    )
    minimums = final_review.setdefault("min_news_items", {})
    if not isinstance(minimums, dict):
        minimums = {}
        final_review["min_news_items"] = minimums
    minimums["primary"] = _int_value(
        form.get("FINAL_REVIEW_MIN_PRIMARY_ITEMS"),
        int(minimums.get("primary", 5) or 5),
    )
    minimums["secondary"] = _int_value(
        form.get("FINAL_REVIEW_MIN_SECONDARY_ITEMS"),
        int(minimums.get("secondary", 3) or 3),
    )

    _write_yaml(path, config)
    return 18


def _update_topics(topics_path: str | Path, form: dict[str, str]) -> int:
    if not any(key.startswith(("TEMPORARY_TOPIC_", "TOPICS_")) for key in form):
        return 0
    path = Path(topics_path)
    config = load_yaml(path)
    temporary = config.setdefault("temporary_topic", {})
    if not isinstance(temporary, dict):
        temporary = {}
        config["temporary_topic"] = temporary
    temporary["enabled"] = _checkbox(form, "TEMPORARY_TOPIC_ENABLED")
    temporary["topic"] = form.get("TEMPORARY_TOPIC_TOPIC", "").strip()
    temporary["column"] = form.get("TEMPORARY_TOPIC_COLUMN", "").strip()
    temporary["reason"] = form.get("TEMPORARY_TOPIC_REASON", "").strip()
    temporary["expires_on"] = form.get("TEMPORARY_TOPIC_EXPIRES_ON", "").strip()
    config["blocked_topics"] = _lines(form.get("TOPICS_BLOCKED_TOPICS", ""))
    _write_yaml(path, config)
    return 6


def _update_news_sources(news_sources_path: str | Path, form: dict[str, str]) -> int:
    if not any(key.startswith("NEWS_") for key in form):
        return 0
    path = Path(news_sources_path)
    config = load_yaml(path)
    config["minimum_successful_sources"] = _int_value(
        form.get("NEWS_MINIMUM_SUCCESSFUL_SOURCES"),
        int(config.get("minimum_successful_sources", 2) or 2),
    )
    config["fetch_workers"] = _int_value(
        form.get("NEWS_FETCH_WORKERS"),
        int(config.get("fetch_workers", 6) or 6),
    )
    config["preferred_keywords"] = _lines(form.get("NEWS_PREFERRED_KEYWORDS", ""))
    config["blocked_keywords"] = _lines(form.get("NEWS_BLOCKED_KEYWORDS", ""))
    _write_yaml(path, config)
    return 3


def _render_page(
    env_path: Path,
    schedule_path: Path,
    topics_path: Path,
    news_sources_path: Path,
    token: str,
    *,
    notice: str = "",
    output_root: Path | None = None,
    state_dir: Path | None = None,
    current_release: Path | None = None,
) -> str:
    env = {**DEFAULTS, **load_env_file(env_path)}
    schedule = load_layered_yaml(schedule_path)
    topics = load_yaml(topics_path)
    news_sources = load_yaml(news_sources_path)
    daily_run = schedule.get("daily_run", {}) if isinstance(schedule.get("daily_run", {}), dict) else {}
    publishing = schedule.get("publishing", {}) if isinstance(schedule.get("publishing", {}), dict) else {}
    final_review = schedule.get("final_review", {}) if isinstance(schedule.get("final_review", {}), dict) else {}
    operations = _operations_status(
        schedule_path=schedule_path,
        output_root=output_root or env_path.parent / "outputs",
        state_dir=state_dir or env_path.parent / "data" / "publish-state",
        current_release=current_release or Path.cwd(),
        env_path=env_path,
    )
    temporary = topics.get("temporary_topic", {}) if isinstance(topics.get("temporary_topic", {}), dict) else {}
    fields_json = json.dumps(
        {
            "env": {
                key: {
                    "value": "" if key in SECRET_KEYS else env.get(key, ""),
                    "configured": bool(env.get(key, "")) if key in SECRET_KEYS else False,
                }
                for key in CONFIG_KEYS
            },
            "daily_run": daily_run,
            "publishing": publishing,
            "final_review": final_review,
            "temporary_topic": temporary,
            "blocked_topics": topics.get("blocked_topics", []),
            "news": {
                "minimum_successful_sources": news_sources.get("minimum_successful_sources", 2),
                "fetch_workers": news_sources.get("fetch_workers", 6),
                "preferred_keywords": news_sources.get("preferred_keywords", []),
                "blocked_keywords": news_sources.get("blocked_keywords", []),
            },
            "operations": operations,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    notice_html = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>自动化配置</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --text: #172033;
      --muted: #667085;
      --line: #d9e2ef;
      --accent: #2f6fed;
      --ok: #116154;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    }}
    main {{
      width: min(1040px, calc(100vw - 32px));
      margin: 28px auto 48px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{ margin: 0; font-size: 25px; line-height: 1.2; }}
    h2 {{ margin: 28px 0 14px; font-size: 18px; }}
    .sub {{ margin: 8px 0 0; color: var(--muted); font-size: 14px; }}
    form {{
      margin-top: 20px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 22px;
      box-shadow: 0 12px 32px rgba(18, 35, 66, .08);
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
    }}
    label {{ display: block; font-weight: 700; font-size: 13px; margin-bottom: 8px; }}
    input, select, textarea {{
      width: 100%;
      min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }}
    textarea {{ min-height: 92px; resize: vertical; }}
    input[type="checkbox"] {{ width: 18px; min-height: 18px; margin: 0 8px 0 0; vertical-align: middle; }}
    .wide {{ grid-column: 1 / -1; }}
    .checkrow {{ display: flex; align-items: center; min-height: 42px; }}
    .hint {{ color: var(--muted); font-size: 12px; margin-top: 6px; line-height: 1.5; }}
    .configured {{ color: var(--ok); font-weight: 700; }}
    .actions {{
      display: flex;
      justify-content: flex-end;
      gap: 12px;
      margin-top: 24px;
      padding-top: 18px;
      border-top: 1px solid var(--line);
    }}
    button {{
      border: 0;
      border-radius: 6px;
      min-height: 42px;
      padding: 0 18px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }}
    .notice {{
      margin-top: 16px;
      padding: 12px 14px;
      border: 1px solid #b8e6dc;
      background: #eefbf7;
      border-radius: 6px;
      color: var(--ok);
      font-weight: 700;
    }}
    .status-band {{
      margin-top: 20px;
      padding: 18px 0 20px;
      border-bottom: 1px solid var(--line);
    }}
    .status-head {{ display:flex;align-items:center;justify-content:space-between;gap:12px; }}
    .status-head h2 {{ margin:0;font-size:18px; }}
    .icon-button {{
      width: 38px;
      height: 38px;
      min-height: 38px;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      font-size: 20px;
      font-weight: 700;
    }}
    .status-summary {{
      display:grid;
      grid-template-columns:repeat(4,minmax(0,1fr));
      gap:14px;
      margin-top:14px;
    }}
    .metric {{ min-width:0;padding:10px 0;border-top:2px solid var(--line); }}
    .metric-label {{ color:var(--muted);font-size:12px;line-height:1.4; }}
    .metric-value {{ margin-top:5px;font-size:15px;font-weight:800;line-height:1.45;overflow-wrap:anywhere; }}
    .run-table-wrap {{ overflow-x:auto;margin-top:14px; }}
    table {{ width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed; }}
    th, td {{ padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere; }}
    th {{ color:var(--muted);font-weight:700; }}
    .outcome-published {{ color:#116154;font-weight:800; }}
    .outcome-blocked, .outcome-failed {{ color:#b42318;font-weight:800; }}
    .outcome-pending {{ color:#9a6700;font-weight:800; }}
    @media (max-width: 720px) {{
      .grid {{ grid-template-columns: 1fr; }}
      header {{ display: block; }}
      .status-summary {{ grid-template-columns:1fr 1fr; }}
      table {{ min-width:760px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>自动化配置</h1>
        <p class="sub">仅限本地访问，用来调整发布模式、运行时间、图片兜底、临时选题、新闻过滤和通知。</p>
      </div>
    </header>
    <section class="status-band" aria-labelledby="status-title">
      <div class="status-head">
        <h2 id="status-title">运行状态</h2>
        <button type="button" class="icon-button" id="refresh-status" title="刷新状态" aria-label="刷新状态">↻</button>
      </div>
      <div class="status-summary" id="status-summary"></div>
      <div class="run-table-wrap">
        <table>
          <thead><tr><th>日期</th><th>结果</th><th>阶段</th><th>主文章</th><th>模型</th><th>通知</th></tr></thead>
          <tbody id="run-rows"></tbody>
        </table>
      </div>
    </section>
    {notice_html}
    <form method="post" action="/?token={html.escape(token)}">
      <div id="app"></div>
      <div class="actions">
        <button type="submit">保存配置</button>
      </div>
    </form>
  </main>
  <script type="application/json" id="config-data">{fields_json}</script>
  <script>
    const data = JSON.parse(document.getElementById("config-data").textContent);
    const app = document.getElementById("app");
    const textModelGroups = {json.dumps(TEXT_MODEL_GROUPS, ensure_ascii=False)};
    const envLabels = {{
      TEXT_MODEL_API_BASE: ["文本模型默认 API 地址", "例如：https://api.openai.com/v1。也可填写其他 OpenAI-compatible 地址。"],
      TEXT_MODEL_API_KEY: ["文本模型默认 API Key", "留空表示保留当前值；各组留空时会使用这个 key。"],
      TEXT_MODEL_RETRY_COUNT: ["文本模型默认重试次数", "默认：2。遇到超时、连接错误、429 或 5xx 时自动重试。"],
      TEXT_MODEL_RETRY_DELAY_SECONDS: ["文本模型默认重试间隔（秒）", "默认：1。会按 1、2、4 秒指数退避。"],
      TEXT_MODEL_SUMMARY_ENABLED: ["启用新闻总结模型", ""],
      TEXT_MODEL_SUMMARY_API_BASE: ["总结模型 API 地址", "留空使用文本模型默认 API 地址。"],
      TEXT_MODEL_SUMMARY_API_KEY: ["总结模型 API Key", "留空表示保留当前值；未配置时使用默认文本模型 key。"],
      TEXT_MODEL_SUMMARY_MODEL: ["总结模型名称", "必填：供应商提供的模型 ID。"],
      TEXT_MODEL_SUMMARY_TIMEOUT_SECONDS: ["总结模型超时时间（秒）", "默认：90"],
      TEXT_MODEL_SUMMARY_RETRY_COUNT: ["总结模型重试次数", "留空使用文本模型默认重试次数。"],
      TEXT_MODEL_SUMMARY_RETRY_DELAY_SECONDS: ["总结模型重试间隔（秒）", "留空使用文本模型默认重试间隔。"],
      TEXT_MODEL_BATCH_ENABLED: ["启用标题/短文案模型", ""],
      TEXT_MODEL_BATCH_API_BASE: ["标题/短文案 API 地址", "留空使用文本模型默认 API 地址。"],
      TEXT_MODEL_BATCH_API_KEY: ["标题/短文案 API Key", "留空表示保留当前值；未配置时使用默认文本模型 key。"],
      TEXT_MODEL_BATCH_MODEL: ["标题/短文案模型名称", "必填：供应商提供的模型 ID。"],
      TEXT_MODEL_BATCH_TIMEOUT_SECONDS: ["标题/短文案超时时间（秒）", "默认：120"],
      TEXT_MODEL_BATCH_RETRY_COUNT: ["标题/短文案重试次数", "留空使用文本模型默认重试次数。"],
      TEXT_MODEL_BATCH_RETRY_DELAY_SECONDS: ["标题/短文案重试间隔（秒）", "留空使用文本模型默认重试间隔。"],
      TEXT_MODEL_POLISH_ENABLED: ["启用终稿审核模型", ""],
      TEXT_MODEL_POLISH_API_BASE: ["审核模型 API 地址", "留空使用文本模型默认 API 地址。"],
      TEXT_MODEL_POLISH_API_KEY: ["审核模型 API Key", "留空表示保留当前值；未配置时使用默认文本模型 key。"],
      TEXT_MODEL_POLISH_MODEL: ["审核模型名称", "必填：供应商提供的模型 ID。"],
      TEXT_MODEL_POLISH_TIMEOUT_SECONDS: ["审核模型超时时间（秒）", "默认：120"],
      TEXT_MODEL_POLISH_RETRY_COUNT: ["审核模型重试次数", "留空使用文本模型默认重试次数。"],
      TEXT_MODEL_POLISH_RETRY_DELAY_SECONDS: ["审核模型重试间隔（秒）", "留空使用文本模型默认重试间隔。"],
      OPENAI_IMAGE_API_BASE: ["图片 API 地址", "默认：https://api.openai.com/v1；也可填写兼容地址。"],
      OPENAI_IMAGE_API_KEY: ["图片中转站密钥", "留空表示保留当前值。"],
      OPENAI_API_KEY: ["OpenAI API 密钥", "留空表示保留当前值。"],
      OPENAI_IMAGE_MODEL: ["图片模型", "必填：供应商提供的图片模型 ID。"],
      OPENAI_IMAGE_SIZE: ["图片尺寸", "默认：1536x1024"],
      OPENAI_IMAGE_QUALITY: ["图片质量", "可选：medium、low、high、auto"],
      OPENAI_IMAGE_FORMAT: ["图片格式", "可选：png、jpeg、webp"],
      OPENAI_IMAGE_TIMEOUT_SECONDS: ["图片超时时间（秒）", "默认：120"],
      NOTIFICATION_WEBHOOK_URL: ["通知 Webhook 地址", "留空表示关闭通知。"],
      NOTIFICATION_WEBHOOK_TYPE: ["通知类型", "可选：generic、feishu、dingtalk、wecom"],
      NOTIFICATION_WEBHOOK_TOKEN: ["通知 Bearer Token", "可选；留空表示保留当前值。"],
      NOTIFICATION_TIMEOUT_SECONDS: ["通知超时时间（秒）", "默认：12"],
      NOTIFICATION_ON_SUCCESS: ["成功时通知", ""],
      NOTIFICATION_ON_FAILURE: ["失败时通知", ""],
      NOTIFICATION_ON_BLOCKED: ["阻塞时通知", ""],
    }};
    const secretKeys = new Set({json.dumps(sorted(SECRET_KEYS))});
    const checkboxEnv = new Set({json.dumps(sorted(BOOLEAN_ENV_KEYS))});
    const wide = new Set([
      "TEXT_MODEL_API_BASE",
      "TEXT_MODEL_API_KEY",
      "TEXT_MODEL_SUMMARY_API_BASE",
      "TEXT_MODEL_SUMMARY_API_KEY",
      "TEXT_MODEL_BATCH_API_BASE",
      "TEXT_MODEL_BATCH_API_KEY",
      "TEXT_MODEL_POLISH_API_BASE",
      "TEXT_MODEL_POLISH_API_KEY",
      "OPENAI_IMAGE_API_BASE",
      "OPENAI_IMAGE_API_KEY",
      "OPENAI_API_KEY",
      "NOTIFICATION_WEBHOOK_URL",
      "NOTIFICATION_WEBHOOK_TOKEN",
    ]);

    function escapeText(value) {{ return String(value ?? ""); }}
    function renderOperations(operations) {{
      const latest = operations.latest || {{}};
      const metrics = [
        ["最近结果", latest.outcome || "unknown"],
        ["最后成功", operations.last_published_date || "暂无"],
        ["生产版本", operations.current_release || "unknown"],
        ["通知配置", operations.notification_configured ? "已配置" : "未配置"],
      ];
      const summary = document.getElementById("status-summary");
      summary.replaceChildren(...metrics.map(([label, value]) => {{
        const metric = document.createElement("div");
        metric.className = "metric";
        const labelNode = document.createElement("div");
        labelNode.className = "metric-label";
        labelNode.textContent = label;
        const valueNode = document.createElement("div");
        valueNode.className = "metric-value";
        valueNode.textContent = escapeText(value);
        metric.append(labelNode, valueNode);
        return metric;
      }}));
      const rows = document.getElementById("run-rows");
      rows.replaceChildren(...(operations.runs || []).map(run => {{
        const row = document.createElement("tr");
        const model = Object.entries(run.model_statuses || {{}})
          .map(([article, roles]) => `${{article}}: ${{Object.entries(roles).map(([role, status]) => `${{role}}=${{status}}`).join(", ")}}`)
          .join(" / ");
        const duration = Object.entries(run.stage_durations || {{}})
          .map(([stage, seconds]) => `${{stage}}=${{seconds}}s`)
          .join(" / ");
        const recovery = run.primary?.recovery_cycles
          ? `补位${{run.primary.recovery_cycles}}轮/${{run.primary.replenished_items || 0}}条`
          : "";
        const values = [
          run.date,
          run.outcome,
          duration || run.stage,
          [run.primary?.title || "", recovery].filter(Boolean).join(" / "),
          model,
          run.notification_status || "",
        ];
        values.forEach((value, index) => {{
          const cell = document.createElement("td");
          cell.textContent = escapeText(value);
          if (index === 1) cell.className = `outcome-${{run.outcome || "unknown"}}`;
          row.appendChild(cell);
        }});
        return row;
      }}));
    }}
    renderOperations(data.operations || {{}});
    document.getElementById("refresh-status").addEventListener("click", async () => {{
      const response = await fetch(`/status.json?token={html.escape(token)}`, {{cache: "no-store"}});
      if (response.ok) renderOperations(await response.json());
    }});

    function section(title) {{
      const h = document.createElement("h2");
      h.textContent = title;
      app.appendChild(h);
      const grid = document.createElement("div");
      grid.className = "grid";
      app.appendChild(grid);
      return grid;
    }}
    function field(grid, label, name, value, hint = "", options = null) {{
      const wrap = document.createElement("div");
      if (wide.has(name)) wrap.className = "wide";
      const lab = document.createElement("label");
      lab.htmlFor = name;
      lab.textContent = label;
      wrap.appendChild(lab);
      let input;
      if (Array.isArray(options)) {{
        input = document.createElement("select");
        for (const optionValue of options) {{
          const option = document.createElement("option");
          option.value = optionValue;
          option.textContent = optionValue;
          option.selected = String(value) === optionValue;
          input.appendChild(option);
        }}
      }} else {{
        input = document.createElement("input");
        input.value = value || "";
      }}
      input.id = name;
      input.name = name;
      if (secretKeys.has(name)) {{
        input.type = "password";
        input.value = "";
        input.placeholder = data.env[name]?.configured ? "已配置，留空保留当前值" : "未配置";
      }}
      wrap.appendChild(input);
      if (hint || data.env[name]?.configured) {{
        const help = document.createElement("div");
        help.className = "hint";
        help.innerHTML = hint + (data.env[name]?.configured ? ' <span class="configured">已配置</span>' : "");
        wrap.appendChild(help);
      }}
      grid.appendChild(wrap);
    }}
    function checkbox(grid, label, name, checked, hint = "") {{
      const wrap = document.createElement("div");
      const row = document.createElement("label");
      row.className = "checkrow";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = name;
      input.id = name;
      input.checked = Boolean(checked);
      row.appendChild(input);
      row.appendChild(document.createTextNode(label));
      wrap.appendChild(row);
      if (hint) {{
        const help = document.createElement("div");
        help.className = "hint";
        help.textContent = hint;
        wrap.appendChild(help);
      }}
      grid.appendChild(wrap);
    }}
    function textarea(grid, label, name, lines, hint = "") {{
      const wrap = document.createElement("div");
      wrap.className = "wide";
      const lab = document.createElement("label");
      lab.htmlFor = name;
      lab.textContent = label;
      wrap.appendChild(lab);
      const input = document.createElement("textarea");
      input.name = name;
      input.id = name;
      input.value = Array.isArray(lines) ? lines.join("\\n") : (lines || "");
      wrap.appendChild(input);
      if (hint) {{
        const help = document.createElement("div");
        help.className = "hint";
        help.textContent = hint;
        wrap.appendChild(help);
      }}
      grid.appendChild(wrap);
    }}

    let grid = section("发布控制");
    checkbox(grid, "启用每日运行", "DAILY_RUN_ENABLED", data.daily_run.enabled ?? true);
    checkbox(grid, "暂停每日运行", "DAILY_RUN_PAUSED", data.daily_run.paused ?? false);
    field(grid, "主控启动时间", "DAILY_RUN_CONTROLLER_TIME", data.daily_run.controller_start_time || "09:00", "服务器 systemd timer 应与这里保持一致。");
    field(grid, "目标发布时间", "DAILY_RUN_PUBLISH_TIME", data.daily_run.publish_time || "09:00");
    field(grid, "单次运行总预算（秒）", "DAILY_RUN_MAX_RUNTIME_SECONDS", data.daily_run.max_runtime_seconds ?? 840);
    field(grid, "预留发布时间（秒）", "DAILY_RUN_PUBLISH_RESERVE_SECONDS", data.daily_run.publish_reserve_seconds ?? 120, "内容生成和补位会为草稿上传与群发保留这段时间。");
    field(grid, "当天最多准备次数", "DAILY_RUN_MAX_PREPARE_ATTEMPTS", data.daily_run.max_prepare_attempts_per_day ?? 3, "硬上限为 3；发布阶段不会补跑内容生成。");
    field(grid, "单次文本模型调用上限", "DAILY_RUN_MAX_TEXT_MODEL_CALLS_PER_ATTEMPT", data.daily_run.max_text_model_calls_per_attempt ?? 28, "按真实 HTTP 请求计数，硬上限为 30。");
    field(grid, "当天文本模型调用上限", "DAILY_RUN_MAX_TEXT_MODEL_CALLS_PER_DAY", data.daily_run.max_text_model_calls_per_day ?? 36, "跨定时窗口累计；默认 36，硬上限为 50，避免失败任务反复消耗。");
    field(grid, "发布模式", "PUBLISHING_MODE", data.publishing.mode || "draft_only", "", {json.dumps(list(PUBLISHING_MODES))});
    field(grid, "发布渠道", "PUBLISHING_PUBLISH_CHANNEL", data.publishing.publish_channel || "freepublish", "freepublish 只发布文章；mass_send_all 会群发通知全部用户。", {json.dumps(list(PUBLISH_CHANNELS))});
    checkbox(grid, "允许低风险自动发布", "PUBLISHING_ALLOW_LOW_RISK_AUTO_PUBLISH", data.publishing.allow_low_risk_auto_publish ?? false);
    checkbox(grid, "中风险要求人工确认", "PUBLISHING_REQUIRE_HUMAN_CONFIRMATION_FOR_MEDIUM_RISK", data.publishing.require_human_confirmation_for_medium_risk ?? true);
    checkbox(grid, "拒绝高风险内容", "PUBLISHING_REJECT_HIGH_RISK", data.publishing.reject_high_risk ?? true);
    checkbox(grid, "发布时允许本地图片兜底", "PUBLISHING_ALLOW_IMAGE_FALLBACK_FOR_PUBLISH", data.publishing.allow_image_fallback_for_publish ?? true);
    checkbox(grid, "转载判定时继续群发", "PUBLISHING_MASS_SEND_IGNORE_REPRINT", data.publishing.mass_send_ignore_reprint ?? false, "仅 mass_send_all 生效；开启后按微信原创校验规则继续群发可转载内容。");
    field(grid, "重试次数", "PUBLISHING_RETRY_COUNT", data.publishing.retry_count ?? 2, "最多重试 2 次，即总尝试不超过 3 次；非幂等群发不会盲目重试。");
    field(grid, "重试间隔（分钟）", "PUBLISHING_RETRY_DELAY_MINUTES", data.publishing.retry_delay_minutes ?? 10);
    field(grid, "终稿 AI 二审模式", "FINAL_REVIEW_MODE", data.final_review.mode || "shadow", "enforce 会执行多模型审核、自动修复和有界补位；模型均不可用时进入明确标记的确定性降级审核。", {json.dumps(list(FINAL_REVIEW_MODES))});
    checkbox(grid, "启用终稿自动修复", "FINAL_REVIEW_AUTO_REPAIR", data.final_review.auto_repair ?? true);
    field(grid, "最多修复补位轮数", "FINAL_REVIEW_MAX_RECOVERY_CYCLES", data.final_review.max_recovery_cycles ?? 1, "默认只做 1 轮逐条修复、删稿与补位；硬上限为 3。");
    field(grid, "每轮最低剩余预算（秒）", "FINAL_REVIEW_MINIMUM_CYCLE_BUDGET_SECONDS", data.final_review.minimum_cycle_budget_seconds ?? 45);
    field(grid, "主文章最低条数", "FINAL_REVIEW_MIN_PRIMARY_ITEMS", data.final_review.min_news_items?.primary ?? 5);
    field(grid, "副文章最低条数", "FINAL_REVIEW_MIN_SECONDARY_ITEMS", data.final_review.min_news_items?.secondary ?? 3);

    grid = section("文本模型 API");
    field(grid, envLabels.TEXT_MODEL_API_BASE[0], "TEXT_MODEL_API_BASE", data.env.TEXT_MODEL_API_BASE?.value, envLabels.TEXT_MODEL_API_BASE[1]);
    field(grid, envLabels.TEXT_MODEL_API_KEY[0], "TEXT_MODEL_API_KEY", data.env.TEXT_MODEL_API_KEY?.value, envLabels.TEXT_MODEL_API_KEY[1]);
    field(grid, envLabels.TEXT_MODEL_RETRY_COUNT[0], "TEXT_MODEL_RETRY_COUNT", data.env.TEXT_MODEL_RETRY_COUNT?.value, envLabels.TEXT_MODEL_RETRY_COUNT[1]);
    field(grid, envLabels.TEXT_MODEL_RETRY_DELAY_SECONDS[0], "TEXT_MODEL_RETRY_DELAY_SECONDS", data.env.TEXT_MODEL_RETRY_DELAY_SECONDS?.value, envLabels.TEXT_MODEL_RETRY_DELAY_SECONDS[1]);
    for (const group of textModelGroups) {{
      const prefix = group.prefix;
      const title = document.createElement("div");
      title.className = "wide hint";
      title.innerHTML = `<strong>${{group.title}}</strong>：${{group.description}}`;
      grid.appendChild(title);
      checkbox(grid, envLabels[`${{prefix}}_ENABLED`][0], `${{prefix}}_ENABLED`, String(data.env[`${{prefix}}_ENABLED`]?.value || "false") === "true");
      field(grid, envLabels[`${{prefix}}_MODEL`][0], `${{prefix}}_MODEL`, data.env[`${{prefix}}_MODEL`]?.value, envLabels[`${{prefix}}_MODEL`][1]);
      field(grid, envLabels[`${{prefix}}_API_BASE`][0], `${{prefix}}_API_BASE`, data.env[`${{prefix}}_API_BASE`]?.value, envLabels[`${{prefix}}_API_BASE`][1]);
      field(grid, envLabels[`${{prefix}}_API_KEY`][0], `${{prefix}}_API_KEY`, data.env[`${{prefix}}_API_KEY`]?.value, envLabels[`${{prefix}}_API_KEY`][1]);
      field(grid, envLabels[`${{prefix}}_TIMEOUT_SECONDS`][0], `${{prefix}}_TIMEOUT_SECONDS`, data.env[`${{prefix}}_TIMEOUT_SECONDS`]?.value, envLabels[`${{prefix}}_TIMEOUT_SECONDS`][1]);
      field(grid, envLabels[`${{prefix}}_RETRY_COUNT`][0], `${{prefix}}_RETRY_COUNT`, data.env[`${{prefix}}_RETRY_COUNT`]?.value, envLabels[`${{prefix}}_RETRY_COUNT`][1]);
      field(grid, envLabels[`${{prefix}}_RETRY_DELAY_SECONDS`][0], `${{prefix}}_RETRY_DELAY_SECONDS`, data.env[`${{prefix}}_RETRY_DELAY_SECONDS`]?.value, envLabels[`${{prefix}}_RETRY_DELAY_SECONDS`][1]);
    }}

    grid = section("图片 API 与通知");
    for (const [key, pair] of Object.entries(envLabels)) {{
      if (key.startsWith("TEXT_MODEL_")) continue;
      if (checkboxEnv.has(key)) {{
        checkbox(grid, pair[0], key, String(data.env[key]?.value || "true") === "true", pair[1]);
      }} else if (key === "OPENAI_IMAGE_QUALITY") {{
        field(grid, pair[0], key, data.env[key]?.value, pair[1], ["medium", "low", "high", "auto"]);
      }} else if (key === "OPENAI_IMAGE_FORMAT") {{
        field(grid, pair[0], key, data.env[key]?.value, pair[1], ["png", "jpeg", "webp"]);
      }} else if (key === "NOTIFICATION_WEBHOOK_TYPE") {{
        field(grid, pair[0], key, data.env[key]?.value, pair[1], {json.dumps(list(WEBHOOK_TYPES))});
      }} else {{
        field(grid, pair[0], key, data.env[key]?.value, pair[1]);
      }}
    }}

    grid = section("临时选题");
    checkbox(grid, "启用临时选题", "TEMPORARY_TOPIC_ENABLED", data.temporary_topic.enabled ?? false);
    field(grid, "选题", "TEMPORARY_TOPIC_TOPIC", data.temporary_topic.topic || "");
    field(grid, "栏目", "TEMPORARY_TOPIC_COLUMN", data.temporary_topic.column || "");
    field(grid, "过期日期", "TEMPORARY_TOPIC_EXPIRES_ON", data.temporary_topic.expires_on || "", "格式：YYYY-MM-DD");
    textarea(grid, "选题原因", "TEMPORARY_TOPIC_REASON", data.temporary_topic.reason || "");
    textarea(grid, "禁写主题", "TOPICS_BLOCKED_TOPICS", data.blocked_topics || [], "每行一个词或短语。");

    grid = section("新闻源过滤");
    field(grid, "最少有效来源数", "NEWS_MINIMUM_SUCCESSFUL_SOURCES", data.news.minimum_successful_sources || 2);
    field(grid, "并发抓取数", "NEWS_FETCH_WORKERS", data.news.fetch_workers || 6, "建议 4-8；同一 URL 会在主副文章间复用抓取结果。");
    textarea(grid, "优先关键词", "NEWS_PREFERRED_KEYWORDS", data.news.preferred_keywords || [], "包含这些词的新闻会优先排序。");
    textarea(grid, "排除关键词", "NEWS_BLOCKED_KEYWORDS", data.news.blocked_keywords || [], "包含这些词的新闻会被排除。");
  </script>
</body>
</html>"""


def _checkbox(form: dict[str, str], key: str) -> bool:
    return key in form


def _lines(value: str) -> list[str]:
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _int_value(value: Any, default: int) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _write_yaml(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _prepared_run_blocks_config_change(
    *,
    output_root: Path,
    state_dir: Path,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(SHANGHAI_TZ)
    date_text = current.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d")
    if (state_dir / "published" / f"{date_text}.json").exists():
        return False
    checkpoint_path = output_root / f"{date_text}-daily-run" / "run-state.json"
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    stages = checkpoint.get("stages", {}) if isinstance(checkpoint, dict) else {}
    if not isinstance(stages, dict):
        return False
    draft = stages.get("wechat_draft", {}) if isinstance(stages.get("wechat_draft", {}), dict) else {}
    publish = stages.get("wechat_publish", {}) if isinstance(stages.get("wechat_publish", {}), dict) else {}
    return draft.get("status") == "completed" and publish.get("status") not in {
        "submitted",
        "publishing",
        "sending",
        "published",
    }


def _operations_status(
    *,
    schedule_path: Path,
    output_root: Path,
    state_dir: Path,
    current_release: Path,
    env_path: Path | None = None,
) -> dict[str, Any]:
    schedule = load_layered_yaml(schedule_path)
    runtime = schedule.get("runtime", {}) if isinstance(schedule.get("runtime", {}), dict) else {}
    result = build_operations_status(
        output_root=output_root,
        state_dir=state_dir,
        current_release=current_release,
        config_fingerprint=config_fingerprint(schedule),
        config_version=str(runtime.get("config_version", "")),
    )
    if env_path is not None:
        env = load_env_file(env_path)
        result["notification_configured"] = bool(str(env.get("NOTIFICATION_WEBHOOK_URL", "")).strip())
    return result


if __name__ == "__main__":
    raise SystemExit(main())
