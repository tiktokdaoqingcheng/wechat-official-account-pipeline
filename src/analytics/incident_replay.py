from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from src.pipeline.copy_quality import sanitize_article_copy
from src.pipeline.news_seed import blocked_by_wechat_platform_risk


def run_incident_replay(fixtures_dir: str | Path) -> dict[str, Any]:
    directory = Path(fixtures_dir)
    cases: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        values = payload if isinstance(payload, list) else payload.get("cases", [])
        for case in values if isinstance(values, list) else []:
            if isinstance(case, dict):
                cases.append({**case, "fixture": path.name})

    results = [_replay_case(case) for case in cases]
    passed = sum(1 for result in results if result["passed"])
    return {
        "status": "ok" if passed == len(results) else "failed",
        "fixtures_dir": str(directory),
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


def _replay_case(case: dict[str, Any]) -> dict[str, Any]:
    article = copy.deepcopy(case.get("article", {}))
    sanitized = sanitize_article_copy(article) if isinstance(article, dict) else {}
    visible = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    expected = case.get("expected", {}) if isinstance(case.get("expected", {}), dict) else {}
    failures: list[str] = []
    for value in expected.get("absent", []) if isinstance(expected.get("absent", []), list) else []:
        if str(value) in visible:
            failures.append(f"expected absent text remains: {value}")
    for value in expected.get("contains", []) if isinstance(expected.get("contains", []), list) else []:
        if str(value) not in visible:
            failures.append(f"expected text is missing: {value}")

    risk_items = case.get("platform_risk_items", [])
    actual_risks = [
        blocked_by_wechat_platform_risk(item)
        for item in risk_items
        if isinstance(item, dict)
    ] if isinstance(risk_items, list) else []
    expected_risks = expected.get("platform_risks", [])
    if isinstance(expected_risks, list) and actual_risks != expected_risks:
        failures.append(f"platform risk mismatch: expected={expected_risks} actual={actual_risks}")

    return {
        "name": str(case.get("name", "unnamed")),
        "fixture": str(case.get("fixture", "")),
        "passed": not failures,
        "failures": failures,
        "news_items": len(sanitized.get("news_items", [])) if isinstance(sanitized.get("news_items", []), list) else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay deterministic checks against historical content incidents.")
    parser.add_argument("--fixtures", default="tests/fixtures/incidents")
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run_incident_replay(args.fixtures)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Incident replay: {report['passed']}/{report['total']} passed")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
