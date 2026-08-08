from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_DB_PATH = Path("data/app.sqlite")


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    connection = connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS automation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                publishing_mode TEXT NOT NULL,
                next_action TEXT NOT NULL,
                reason TEXT,
                title TEXT,
                risk_level TEXT,
                output_dir TEXT NOT NULL,
                result_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_automation_runs_run_date
                ON automation_runs(run_date);

            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                risk_level TEXT,
                publish_action TEXT,
                output_dir TEXT,
                article_json_path TEXT,
                review_json_path TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES automation_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES automation_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS published_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                publish_id TEXT,
                article_index INTEGER,
                title TEXT,
                article_id TEXT,
                url TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES automation_runs(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS run_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                stage_name TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_seconds REAL NOT NULL DEFAULT 0,
                resumed INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES automation_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                model TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                status_code INTEGER,
                duration_seconds REAL NOT NULL DEFAULT 0,
                timeout_seconds REAL NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES automation_runs(id) ON DELETE CASCADE
            );
            """
        )
        _ensure_column(connection, "automation_runs", "business_outcome", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "automation_runs", "run_stage", "TEXT NOT NULL DEFAULT 'all'")
        connection.commit()
    finally:
        connection.close()


def record_daily_run(result: dict[str, Any], db_path: str | Path = DEFAULT_DB_PATH) -> int:
    initialize(db_path)
    created_at = str(result.get("created_at") or _now_iso())
    run_date = _date_from_iso(created_at)
    title, risk_level, publish_action, article_output_dir, files = _content_stage_summary(result)

    connection = connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO automation_runs (
                run_date,
                created_at,
                status,
                business_outcome,
                publishing_mode,
                run_stage,
                next_action,
                reason,
                title,
                risk_level,
                output_dir,
                result_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_date,
                created_at,
                str(result.get("status", "unknown")),
                str(result.get("business_outcome", "")),
                str(result.get("publishing_mode", "")),
                str(result.get("run_stage", "all")),
                str(result.get("next_action", "")),
                str(result.get("reason", "")),
                title,
                risk_level,
                str(result.get("output_dir", "")),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        run_id = int(cursor.lastrowid)

        if title:
            connection.execute(
                """
                INSERT INTO articles (
                    run_id,
                    title,
                    risk_level,
                    publish_action,
                    output_dir,
                    article_json_path,
                    review_json_path,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    title,
                    risk_level,
                    publish_action,
                    article_output_dir,
                    str(files.get("article_json", "")),
                    str(files.get("review_json", "")),
                    created_at,
                ),
            )

        for article in _published_articles(result):
            connection.execute(
                """
                INSERT INTO published_articles (
                    run_id,
                    publish_id,
                    article_index,
                    title,
                    article_id,
                    url,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(result.get("publish_id", "")),
                    int(article.get("index", 0) or 0),
                    str(article.get("title", "")),
                    str(article.get("article_id", "")),
                    str(article.get("url", "")),
                    created_at,
                ),
            )

        for stage in result.get("stages", []) if isinstance(result.get("stages", []), list) else []:
            if not isinstance(stage, dict) or not str(stage.get("name", "")).strip():
                continue
            connection.execute(
                """
                INSERT INTO run_stages (
                    run_id, stage_name, status, duration_seconds, resumed, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(stage.get("name", "")),
                    str(stage.get("status", "unknown")),
                    float(stage.get("duration_seconds", 0) or 0),
                    1 if stage.get("resumed") else 0,
                    json.dumps(stage, ensure_ascii=False),
                    created_at,
                ),
            )

        for call in _model_calls(result):
            usage = call.get("usage", {}) if isinstance(call.get("usage", {}), dict) else {}
            connection.execute(
                """
                INSERT INTO model_calls (
                    run_id, role, model, attempt, outcome, status_code,
                    duration_seconds, timeout_seconds, prompt_tokens,
                    completion_tokens, total_tokens, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(call.get("role", "")),
                    str(call.get("model", "")),
                    int(call.get("attempt", 0) or 0),
                    str(call.get("outcome", "unknown")),
                    int(call["status_code"]) if call.get("status_code") is not None else None,
                    float(call.get("duration_seconds", 0) or 0),
                    float(call.get("timeout_seconds", 0) or 0),
                    int(usage.get("prompt_tokens", 0) or 0),
                    int(usage.get("completion_tokens", 0) or 0),
                    int(usage.get("total_tokens", 0) or 0),
                    json.dumps(call, ensure_ascii=False),
                    created_at,
                ),
            )

        connection.execute(
            """
            INSERT INTO events (run_id, event_type, status, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "daily_run_recorded",
                str(result.get("status", "unknown")),
                created_at,
                json.dumps(
                    {
                        "next_action": result.get("next_action"),
                        "output_dir": result.get("output_dir"),
                        "title": title,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        connection.commit()
        return run_id
    finally:
        connection.close()


def recent_runs(db_path: str | Path = DEFAULT_DB_PATH, *, limit: int = 10) -> list[dict[str, Any]]:
    initialize(db_path)
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT
                id,
                run_date,
                created_at,
                status,
                business_outcome,
                publishing_mode,
                run_stage,
                next_action,
                reason,
                title,
                risk_level,
                output_dir
            FROM automation_runs
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        runs = [dict(row) for row in rows]
        publish_links = _published_links_by_run(connection, [int(run["id"]) for run in runs])
        for run in runs:
            run["published_articles"] = publish_links.get(int(run["id"]), [])
        return runs
    finally:
        connection.close()


def record_event(
    event_type: str,
    payload: dict[str, Any],
    db_path: str | Path = DEFAULT_DB_PATH,
    *,
    status: str = "ok",
    run_id: int | None = None,
    created_at: str | None = None,
) -> int:
    initialize(db_path)
    timestamp = created_at or _now_iso()
    connection = connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO events (run_id, event_type, status, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                status,
                timestamp,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _content_stage_summary(result: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]]:
    stages = result.get("stages", [])
    if not isinstance(stages, list):
        return "", "", "", "", {}

    for stage in stages:
        if not isinstance(stage, dict):
            continue
        if stage.get("name") != "content_package":
            continue
        output_dir = str(stage.get("output_dir", ""))
        files = _read_content_package_files(output_dir)
        return (
            _title_summary(stage),
            str(stage.get("risk_level", "")),
            str(stage.get("publish_action", "")),
            output_dir,
            files,
        )
    return "", "", "", "", {}


def _published_articles(result: dict[str, Any]) -> list[dict[str, Any]]:
    articles = result.get("published_articles", [])
    return [article for article in articles if isinstance(article, dict)] if isinstance(articles, list) else []


def _model_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    calls = result.get("model_calls", [])
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _published_links_by_run(connection: sqlite3.Connection, run_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        f"""
        SELECT run_id, publish_id, article_index, title, article_id, url, created_at
        FROM published_articles
        WHERE run_id IN ({placeholders})
        ORDER BY run_id DESC, article_index ASC, id ASC
        """,
        run_ids,
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        run_id = int(row["run_id"])
        grouped.setdefault(run_id, []).append(
            {
                "publish_id": row["publish_id"],
                "index": row["article_index"],
                "title": row["title"],
                "article_id": row["article_id"],
                "url": row["url"],
                "created_at": row["created_at"],
            }
        )
    return grouped


def _title_summary(stage: dict[str, Any]) -> str:
    title = str(stage.get("title", ""))
    secondary = str(stage.get("secondary_title", ""))
    if title and secondary:
        return f"{title} / {secondary}"
    return title


def _read_content_package_files(output_dir: str) -> dict[str, Any]:
    if not output_dir:
        return {}
    result_path = Path(output_dir) / "dry-run-result.json"
    if not result_path.exists():
        return {}
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    files = payload.get("files", {})
    return files if isinstance(files, dict) else {}


def _date_from_iso(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    return parsed.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
