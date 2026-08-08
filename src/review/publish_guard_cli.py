from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from src.review.publish_guard import (
    already_published_today,
    create_manual_confirmation_request,
    load_manual_confirmation,
    publish_date,
    record_publish_lock,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local publish guard state.")
    parser.add_argument("--state-dir", default="data/publish-state")
    parser.add_argument("--json", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Check whether a publish lock exists for today.")

    request_parser = subparsers.add_parser(
        "request-confirmation",
        help="Create a manual confirmation request file.",
    )
    request_parser.add_argument("--output-dir", required=True)
    request_parser.add_argument("--action", default="publish")
    request_parser.add_argument("--title", required=True)

    check_parser = subparsers.add_parser(
        "check-confirmation",
        help="Validate a manual confirmation file.",
    )
    check_parser.add_argument("--confirmation-file", required=True)

    lock_parser = subparsers.add_parser(
        "record-lock",
        help="Record that today's publish action has completed.",
    )
    lock_parser.add_argument("--title", required=True)
    lock_parser.add_argument("--media-id", default="")
    lock_parser.add_argument("--publish-id", default="")

    args = parser.parse_args()

    if args.command == "status":
        result = {
            "date": publish_date(),
            "already_published_today": already_published_today(args.state_dir),
            "state_dir": args.state_dir,
        }
    elif args.command == "request-confirmation":
        path = create_manual_confirmation_request(
            args.output_dir,
            action=args.action,
            title=args.title,
        )
        result = {
            "status": "ok",
            "confirmation_file": str(path),
        }
    elif args.command == "check-confirmation":
        confirmation = load_manual_confirmation(args.confirmation_file)
        result = asdict(confirmation)
        result["status"] = "ok" if confirmation.ok else "blocked"
    elif args.command == "record-lock":
        path = record_publish_lock(
            {
                "title": args.title,
                "media_id": args.media_id,
                "publish_id": args.publish_id,
            },
            args.state_dir,
        )
        result = {
            "status": "ok",
            "lock_file": str(path),
        }
    else:
        raise RuntimeError(f"Unhandled command: {args.command}")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)

    return 0 if result.get("status") != "blocked" else 1


def _print_human(result: dict[str, object]) -> None:
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
