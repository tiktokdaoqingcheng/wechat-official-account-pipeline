from __future__ import annotations

import argparse
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Wrap generated WeChat HTML for an offline documentation preview.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    prepare_demo_preview(Path(args.input), Path(args.output))
    print(f"Prepared documentation preview: {args.output}")
    return 0


def prepare_demo_preview(input_path: Path, output_path: Path) -> Path:
    fragment = input_path.read_text(encoding="utf-8")
    fragment = re.sub(
        r'<section[^>]*>\s*<img\s+src="\{\{[^}]+\}\}"[\s\S]*?</section>',
        "",
        fragment,
        flags=re.IGNORECASE,
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Synthetic WeChat dry-run preview</title>
</head>
<body style="margin:0;background:#eef2f5;color:#111827;font-family:Arial,'Microsoft YaHei',sans-serif;">
  <main style="width:min(760px,calc(100% - 40px));margin:24px auto;background:#ffffff;padding:24px 28px;box-sizing:border-box;border:1px solid #dfe4e8;">
    <p style="margin:0 0 18px;padding:10px 12px;background:#e8f7f2;border-left:4px solid #0f9f7f;color:#155e4b;font-size:13px;line-height:1.6;">
      OFFLINE SYNTHETIC DEMO · No account data · No provider calls · No publication
    </p>
    {fragment}
  </main>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    raise SystemExit(main())
