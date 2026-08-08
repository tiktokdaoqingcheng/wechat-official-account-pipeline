from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "dist", "outputs", "demo-output"}
FORBIDDEN_PARTS = {"logs", "node_modules", "playwright-report", "test-results"}
FORBIDDEN_SUFFIXES = {
    ".7z",
    ".bak",
    ".backup",
    ".db",
    ".dump",
    ".log",
    ".sqlite",
    ".tar",
    ".tgz",
    ".zip",
}
TEXT_SUFFIXES = {
    ".cmd",
    ".html",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".service",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SECRET_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("provider token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("bearer credential", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("WeChat AppID", re.compile(r"\bwx[0-9a-fA-F]{16}\b")),
    ("Windows user path", re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")),
)
KNOWN_PRIVATE_MARKERS = (
    "tiktok" + "daoqingcheng/wechat-official-account-automation",
    "api." + "open" + "lux.ai",
    "/" + "opt/wechat-official-account-automation",
    "2026-07-10" + ".v13",
)
IP_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail when a public candidate contains private or generated artifacts.")
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).resolve()
    issues = scan_tree(root)
    if issues:
        print("Public tree check failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Public tree check passed: no forbidden artifacts or high-confidence secret patterns found.")
    return 0


def scan_tree(root: Path) -> list[str]:
    issues: list[str] = []
    for path in _git_visible_files(root):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_dir():
            continue
        normalized = relative.as_posix()
        lower_parts = {part.lower() for part in relative.parts}
        suffix = path.suffix.lower()
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            issues.append(f"forbidden environment file: {normalized}")
        if lower_parts & FORBIDDEN_PARTS:
            issues.append(f"forbidden generated directory: {normalized}")
        if suffix in FORBIDDEN_SUFFIXES or normalized.endswith(".tar.gz"):
            issues.append(f"forbidden generated/archive file: {normalized}")
        if path.name.lower() in {"secrets.md", "secrets.local.md"}:
            issues.append(f"forbidden secrets document: {normalized}")
        if suffix not in TEXT_SUFFIXES and path.name not in {".env.example", "LICENSE"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                issues.append(f"{label}: {normalized}")
        lowered = text.lower()
        for marker in KNOWN_PRIVATE_MARKERS:
            if marker.lower() in lowered:
                issues.append(f"private project marker: {normalized}")
        for match in IP_PATTERN.finditer(text):
            try:
                address = ipaddress.ip_address(match.group(0))
            except ValueError:
                continue
            if not (address.is_loopback or address.is_private or address.is_reserved or address.is_unspecified):
                issues.append(f"public IP address: {normalized}")
                break
    return sorted(set(issues))


def _git_visible_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(path for path in root.rglob("*") if path.is_file())
    return sorted(root / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw)


if __name__ == "__main__":
    sys.exit(main())
