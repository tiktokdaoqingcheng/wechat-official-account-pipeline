from __future__ import annotations

import argparse
import json

from src.analytics.run_summary import build_run_summary, write_summary_report
from src.storage.local_db import initialize, recent_runs


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the local automation SQLite database.")
    parser.add_argument("--db", default="data/app.sqlite")
    parser.add_argument("--json", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize local database schema.")
    runs_parser = subparsers.add_parser("recent-runs", help="Show recent daily runs.")
    runs_parser.add_argument("--limit", type=int, default=10)
    summary_parser = subparsers.add_parser("summary", help="Generate a local run summary report.")
    summary_parser.add_argument("--days", type=int, default=7)
    summary_parser.add_argument("--limit", type=int, default=100)
    summary_parser.add_argument("--output-dir", default="outputs/run-summary")

    args = parser.parse_args()

    if args.command == "init":
        initialize(args.db)
        result = {"status": "ok", "db": args.db}
    elif args.command == "recent-runs":
        result = {"status": "ok", "db": args.db, "runs": recent_runs(args.db, limit=args.limit)}
    elif args.command == "summary":
        summary = build_run_summary(args.db, days=args.days, limit=args.limit)
        files = write_summary_report(args.output_dir, summary)
        result = {
            "status": "ok",
            "db": args.db,
            "summary": summary,
            "files": files,
        }
    else:
        raise RuntimeError(f"Unhandled command: {args.command}")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0


def _print_human(result: dict[str, object]) -> None:
    print(f"status: {result.get('status')}")
    print(f"db: {result.get('db')}")
    runs = result.get("runs")
    if isinstance(runs, list):
        for run in runs:
            if isinstance(run, dict):
                print(
                    f"- #{run.get('id')} {run.get('run_date')} "
                    f"{run.get('status')} {run.get('publishing_mode')} "
                    f"next={run.get('next_action')} title={run.get('title')}"
                )
    files = result.get("files")
    if isinstance(files, dict):
        print(f"json: {files.get('json')}")
        print(f"markdown: {files.get('markdown')}")


if __name__ == "__main__":
    raise SystemExit(main())
