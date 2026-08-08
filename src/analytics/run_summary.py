from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.storage.local_db import recent_runs


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def build_run_summary(
    db_path: str | Path = "data/app.sqlite",
    *,
    days: int = 7,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI_TZ)
    window_start = (current - timedelta(days=max(1, days) - 1)).date().isoformat()
    raw_runs = [
        run
        for run in recent_runs(db_path, limit=max(1, limit))
        if str(run.get("run_date", "")) >= window_start
    ]
    latest_by_date: dict[str, dict[str, Any]] = {}
    for run in raw_runs:
        run_date = str(run.get("run_date", ""))
        if run_date and run_date not in latest_by_date:
            latest_by_date[run_date] = run
    runs = list(latest_by_date.values())

    status_counts = Counter(str(run.get("status", "unknown")) for run in runs)
    mode_counts = Counter(str(run.get("publishing_mode", "unknown")) for run in runs)
    next_action_counts = Counter(str(run.get("next_action", "unknown")) for run in runs)
    risk_counts = Counter(str(run.get("risk_level", "unknown")) for run in runs if run.get("risk_level"))
    outcome_counts = Counter(_run_business_outcome(run) for run in runs)

    total = len(runs)
    published_count = outcome_counts.get("published", 0) + outcome_counts.get("duplicate_skipped", 0)
    blocked_count = status_counts.get("blocked", 0)
    failed_count = status_counts.get("failed", 0)

    return {
        "generated_at": current.isoformat(),
        "window": {
            "days": max(1, days),
            "start_date": window_start,
            "end_date": current.date().isoformat(),
        },
        "totals": {
            "runs": total,
            "ok": published_count,
            "published": published_count,
            "blocked": blocked_count,
            "failed": failed_count,
            "success_rate": round(published_count / total, 4) if total else 0,
        },
        "status_counts": dict(status_counts),
        "business_outcome_counts": dict(outcome_counts),
        "publishing_mode_counts": dict(mode_counts),
        "next_action_counts": dict(next_action_counts),
        "risk_level_counts": dict(risk_counts),
        "latest_runs": runs[: min(10, len(runs))],
        "recommendations": _recommendations(
            total=total,
            failed_count=failed_count,
            blocked_count=blocked_count,
            next_action_counts=next_action_counts,
        ),
    }


def render_markdown(summary: dict[str, Any]) -> str:
    totals = summary["totals"]
    window = summary["window"]
    lines = [
        "# 自动化运行复盘",
        "",
        f"- 时间窗口：{window['start_date']} 至 {window['end_date']}（{window['days']} 天）",
        f"- 总运行次数：{totals['runs']}",
        f"- 成功：{totals['ok']}",
        f"- 阻塞：{totals['blocked']}",
        f"- 失败：{totals['failed']}",
        f"- 成功率：{totals['success_rate']:.2%}",
        "",
        "## 发布模式分布",
        "",
        *_counter_lines(summary.get("publishing_mode_counts", {})),
        "",
        "## 下一步动作分布",
        "",
        *_counter_lines(summary.get("next_action_counts", {})),
        "",
        "## 业务终态分布",
        "",
        *_counter_lines(summary.get("business_outcome_counts", {})),
        "",
        "## 风险等级分布",
        "",
        *_counter_lines(summary.get("risk_level_counts", {})),
        "",
        "## 最近运行",
        "",
    ]

    latest_runs = summary.get("latest_runs", [])
    if latest_runs:
        for run in latest_runs:
            lines.append(
                f"- {run.get('created_at')} | {run.get('status')} | "
                f"{run.get('publishing_mode')} | next={run.get('next_action')} | "
                f"{run.get('title') or '(no title)'}"
            )
            for article in run.get("published_articles", []) if isinstance(run.get("published_articles", []), list) else []:
                url = str(article.get("url", "")).strip() if isinstance(article, dict) else ""
                if url:
                    lines.append(f"  - 发布链接：{url}")
    else:
        lines.append("- 暂无运行记录。")

    lines.extend(["", "## 建议", ""])
    recommendations = summary.get("recommendations", [])
    if recommendations:
        lines.extend(f"- {item}" for item in recommendations)
    else:
        lines.append("- 当前运行记录稳定，继续保持 dry-run 观察。")

    lines.append("")
    return "\n".join(lines)


def write_summary_report(
    output_dir: str | Path,
    summary: dict[str, Any],
) -> dict[str, str]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / "run-summary.json"
    markdown_path = output_path / "run-summary.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(summary), encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
    }


def _counter_lines(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["- 暂无数据。"]
    return [f"- {key}: {value}" for key, value in sorted(counts.items())]


def _recommendations(
    *,
    total: int,
    failed_count: int,
    blocked_count: int,
    next_action_counts: Counter[str],
) -> list[str]:
    if total == 0:
        return ["先运行一次 `scripts/daily-run.cmd`，建立第一条本地台账。"]

    recommendations: list[str] = []
    if failed_count:
        recommendations.append("存在失败运行，优先查看 `logs/` 和对应 `daily-run-result.json`。")
    if blocked_count:
        recommendations.append("存在阻塞运行，检查每日发布锁、发布模式和审核报告。")
    if next_action_counts.get("await_manual_confirmation", 0):
        recommendations.append("有内容等待人工确认，可检查输出目录中的 `manual-confirmation.json`。")
    if next_action_counts.get("create_draft", 0):
        recommendations.append("有内容可进入草稿创建阶段，确认微信接口权限后再执行草稿链路。")
    if next_action_counts.get("ready_to_publish", 0):
        recommendations.append("有内容已满足低风险发布门槛，正式发布前仍需确认账号 API 保护状态。")
    if not recommendations:
        recommendations.append("当前运行未发现失败或阻塞，建议继续累计多日 dry-run 记录。")
    return recommendations


def _run_business_outcome(run: dict[str, Any]) -> str:
    explicit = str(run.get("business_outcome", "")).strip()
    if explicit:
        return explicit
    next_action = str(run.get("next_action", "")).strip()
    if next_action == "duplicate_skipped":
        return "duplicate_skipped"
    if next_action == "publish_submitted" and str(run.get("status", "")) == "ok":
        return "published_legacy_unconfirmed"
    if str(run.get("status", "")) in {"blocked", "failed"}:
        return str(run.get("status", ""))
    return "unknown"
