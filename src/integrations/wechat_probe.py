from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.integrations.wechat_client import WeChatApiError, client_from_env, load_env_file, redact


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe WeChat Official Account API permissions.")
    parser.add_argument("--env", default=".env", help="Path to .env file.")
    parser.add_argument("--upload-image", help="Optional image path to test permanent image upload.")
    parser.add_argument("--json", action="store_true", help="Print JSON result.")
    args = parser.parse_args()

    result: dict[str, object] = {
        "status": "unknown",
        "checks": [],
        "notes": [],
    }

    try:
        env_values = load_env_file(args.env)
        result["env"] = {
            "path": str(Path(args.env).resolve()),
            "has_wechat_app_id": bool(env_values.get("WECHAT_APP_ID")),
            "has_wechat_app_secret": bool(env_values.get("WECHAT_APP_SECRET")),
            "wechat_app_id_redacted": redact(env_values.get("WECHAT_APP_ID")),
        }

        client = client_from_env(args.env)
        token = client.get_access_token()
        result["checks"].append(
            {
                "name": "access_token",
                "ok": True,
                "expires_in_seconds": token.expires_in_seconds,
            }
        )

        try:
            material_count = client.get_material_count(token.value)
            result["checks"].append(
                {
                    "name": "material_count",
                    "ok": True,
                    "data": material_count,
                }
            )
        except WeChatApiError as exc:
            result["checks"].append(
                {
                    "name": "material_count",
                    "ok": False,
                    "error": str(exc),
                    "payload": exc.payload,
                }
            )

        try:
            draft_count = client.get_draft_count(token.value)
            result["checks"].append(
                {
                    "name": "draft_count",
                    "ok": True,
                    "data": draft_count,
                }
            )
        except WeChatApiError as exc:
            result["checks"].append(
                {
                    "name": "draft_count",
                    "ok": False,
                    "error": str(exc),
                    "payload": exc.payload,
                }
            )

        if args.upload_image:
            uploaded = client.upload_permanent_image(token.value, args.upload_image)
            result["checks"].append(
                {
                    "name": "upload_permanent_image",
                    "ok": True,
                    "data": uploaded,
                }
            )
        else:
            result["notes"].append("Skipped upload_permanent_image. Pass --upload-image to test it.")

        checks = result["checks"]
        result["status"] = "ok" if all(item.get("ok") for item in checks if isinstance(item, dict)) else "partial"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        if isinstance(exc, WeChatApiError):
            result["payload"] = exc.payload

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Status: {result['status']}")
        env = result.get("env")
        if isinstance(env, dict):
            print(f"Env: {env.get('path')}")
            print(f"AppID: {env.get('wechat_app_id_redacted')}")
        for check in result.get("checks", []):
            if isinstance(check, dict):
                print(f"- {check.get('name')}: {'OK' if check.get('ok') else 'FAILED'}")
                if not check.get("ok"):
                    print(f"  {check.get('error')}")
        if result.get("error"):
            print(f"Error: {result['error']}")
        for note in result.get("notes", []):
            print(f"Note: {note}")

    return 0 if result["status"] in {"ok", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
