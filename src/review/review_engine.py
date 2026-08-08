from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_yaml


REVIEW_SCHEMA_VERSION = "review.v1"
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_SENSITIVE_TERMS = ["荐股", "诊断", "保证收益", "政治敏感"]
HIGH_RISK_KEYWORDS = {
    "medical_advice": ["医疗", "诊断", "处方", "治疗"],
    "legal_advice": ["法律定性", "诉讼", "合同纠纷"],
    "investment_advice": ["荐股", "投资建议", "保证收益"],
    "guaranteed_income": ["保证收益", "稳赚"],
    "political_assertion": ["政治敏感", "公共事件定性"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Review an article draft and output review.v1 report.")
    parser.add_argument("--article", required=True)
    parser.add_argument("--safety", default="config/safety-rules.yaml")
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    article = json.loads(Path(args.article).read_text(encoding="utf-8-sig"))
    safety = load_yaml(args.safety)
    review = review_article(article, safety)
    errors = validate_review(review)
    if errors:
        raise ValueError("Invalid review report: " + "; ".join(errors))

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(review, ensure_ascii=False, indent=2))
    else:
        print(f"Risk level: {review['risk_level']}")
        print(f"WeChat ready: {review['wechat_ready']}")
        if args.output:
            print(f"Output: {args.output}")
    return 0


def review_article(article: dict[str, Any], safety_config: dict[str, Any] | None = None) -> dict[str, Any]:
    safety = safety_config or {}
    text = json.dumps(article, ensure_ascii=False)
    sensitive_terms = _sensitive_terms(safety)
    sensitive_hits = _sensitive_hits(text, sensitive_terms)
    claim_hits = _claim_hits(text, safety)
    sources = article.get("sources", [])
    source_warnings = _source_warnings(sources, article)

    checks = [
        {
            "name": "sensitive_terms",
            "ok": not sensitive_hits,
            "severity": "medium" if sensitive_hits else "low",
            "details": sensitive_hits,
        },
        {
            "name": "high_risk_claims",
            "ok": not claim_hits,
            "severity": "high" if claim_hits else "low",
            "details": claim_hits,
        },
        {
            "name": "sources_present",
            "ok": bool(sources),
            "severity": "medium" if not sources else "low",
            "details": source_warnings,
        },
        {
            "name": "local_only_sources",
            "ok": not source_warnings,
            "severity": "medium" if source_warnings else "low",
            "details": source_warnings,
        },
    ]

    risk_level = _risk_level(checks)
    flags = {
        "fact_uncertain": bool(source_warnings),
        "sensitive_topic": bool(sensitive_hits or claim_hits),
        "copyright_unclear": False,
    }
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "reviewed_at": datetime.now(SHANGHAI_TZ).isoformat(),
        "article_schema_version": str(article.get("schema_version", "")),
        "title": str(article.get("title", "")),
        "risk_level": risk_level,
        "wechat_ready": risk_level == "low",
        "flags": flags,
        "checks": checks,
        "notes": [
            "本地规则审核，仅用于流水线验证。",
            "正式发布前仍建议增加事实核验、来源检查和图片版权检查。",
        ],
        "recommendations": _recommendations(risk_level, flags, source_warnings),
        "sensitive_hits": sensitive_hits,
        "blocked_claim_hits": claim_hits,
    }


def validate_review(review: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append(f"schema_version must be {REVIEW_SCHEMA_VERSION}")
    if str(review.get("risk_level", "")) not in {"low", "medium", "high"}:
        errors.append("risk_level must be low, medium, or high")
    if not isinstance(review.get("wechat_ready"), bool):
        errors.append("wechat_ready must be a boolean")
    flags = review.get("flags")
    if not isinstance(flags, dict):
        errors.append("flags must be an object")
    else:
        for name in ("fact_uncertain", "sensitive_topic", "copyright_unclear"):
            if not isinstance(flags.get(name), bool):
                errors.append(f"flags.{name} must be a boolean")
    checks = review.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
    return errors


def _sensitive_terms(safety: dict[str, Any]) -> list[str]:
    configured = safety.get("sensitive_terms", [])
    if isinstance(configured, list) and configured:
        return [str(item) for item in configured if str(item).strip()]
    return DEFAULT_SENSITIVE_TERMS


def _sensitive_hits(text: str, sensitive_terms: list[str]) -> list[str]:
    hits = [term for term in sensitive_terms if term in text]
    if "诊断" in hits and not _medical_advice_matches(text, ["诊断"]):
        hits = [term for term in hits if term != "诊断"]
    if "保证收益" in hits and not _guaranteed_income_matches(text, ["保证收益"]):
        hits = [term for term in hits if term != "保证收益"]
    return hits


def _claim_hits(text: str, safety: dict[str, Any]) -> list[dict[str, Any]]:
    claim_types = safety.get("blocked_claim_types", [])
    if not isinstance(claim_types, list):
        claim_types = []
    hits: list[dict[str, Any]] = []
    for claim_type in claim_types:
        keywords = HIGH_RISK_KEYWORDS.get(str(claim_type), [])
        matched = [keyword for keyword in keywords if keyword in text]
        if str(claim_type) == "medical_advice":
            matched = _medical_advice_matches(text, matched)
        if str(claim_type) == "investment_advice":
            matched = _investment_advice_matches(text, matched)
        if str(claim_type) == "guaranteed_income":
            matched = _guaranteed_income_matches(text, matched)
        if str(claim_type) == "legal_advice":
            matched = _legal_advice_matches(text, matched)
        if matched:
            hits.append({"claim_type": str(claim_type), "keywords": matched})
    return hits


def _medical_advice_matches(text: str, matched: list[str]) -> list[str]:
    if not matched:
        return []
    value = _remove_negated_medical_advice_phrases(text)
    value = _remove_descriptive_medical_news_phrases(value)
    advice_terms = [
        "医疗建议",
        "就医建议",
        "诊疗建议",
        "用药建议",
        "处方建议",
        "治疗方案",
        "如何治疗",
        "如何诊断",
        "自行诊断",
        "自行用药",
        "推荐用药",
        "无需就医",
        "替代医生",
    ]
    if any(term in value for term in advice_terms):
        return matched
    news_context_terms = [
        "报道",
        "新闻",
        "研究",
        "论文",
        "模型",
        "AI",
        "人工智能",
        "辅助",
        "罕见病",
        "医疗服务",
        "健康回复",
        "临床",
        "医生",
        "人工审核",
        "人工把关",
    ]
    if any(term in text for term in news_context_terms):
        return []
    return [term for term in matched if term != "医疗"]


def _remove_negated_medical_advice_phrases(text: str) -> str:
    patterns = [
        r"不(?:是|属于|构成|提供|作为)[^。；;\n]{0,24}(?:医疗建议|诊疗建议|就医建议|用药建议|处方建议)",
        r"不是面向读者的[^。；;\n]{0,16}(?:医疗建议|诊疗建议|就医建议)",
    ]
    value = text
    for pattern in patterns:
        value = re.sub(pattern, "", value)
    return value


def _remove_descriptive_medical_news_phrases(text: str) -> str:
    patterns = [
        r"(?:AI|人工智能|模型|大模型|ChatGPT)[^。；;\n]{0,40}医疗建议场景[^。；;\n]{0,40}(?:幻觉|误导|评估|质量|可靠|表现|问题)",
        r"健康(?:信息|资讯)[^。；;\n]{0,24}(?:回答|回复|质量|准确性|可靠性)",
        r"医生[^。；;\n]{0,24}(?:评估|审核|复核|把关|参与)",
        r"辅助医生诊断",
        r"帮助医生诊断",
    ]
    value = text
    for pattern in patterns:
        value = re.sub(pattern, "", value)
    return value


def _investment_advice_matches(text: str, matched: list[str]) -> list[str]:
    if not matched:
        return []
    value = text
    disclaimer_patterns = [
        r"不构成(?:任何|具体)?投资建议",
        r"不作为(?:任何|具体)?投资建议",
        r"不(?:作|做)(?:任何|具体)?投资建议",
        r"未(?:对[^。；;\n]{0,40})?(?:作|做)(?:任何|具体)?投资建议(?:或结论)?",
        r"不(?:为|替)[^。；;\n]{0,40}背书(?:任何|具体)?投资建议",
        r"本文(?:仅)?基于公开(?:信息|资料)(?:进行)?(?:整理|梳理)[^。；;\n]{0,40}不构成(?:任何|具体)?投资建议",
    ]
    for pattern in disclaimer_patterns:
        value = re.sub(pattern, "", value)
    value = _remove_negated_guaranteed_income_phrases(value)
    return [keyword for keyword in matched if keyword in value]


def _guaranteed_income_matches(text: str, matched: list[str]) -> list[str]:
    if not matched:
        return []
    value = _remove_negated_guaranteed_income_phrases(text)
    return [keyword for keyword in matched if keyword in value]


def _remove_negated_guaranteed_income_phrases(text: str) -> str:
    patterns = [
        r"(?:不|并不|不是|并非|非|没有|未|无法|不能|不会)[^。；;，,、!！?\n]{0,24}(?:保证收益|稳赚(?:不赔)?)",
        r"(?:不代表|不等于|不意味着|并不代表|并不等于|并不意味着|未必(?:代表|等于|意味着)?)[^。；;，,、!！?\n]{0,36}(?:保证收益|稳赚(?:不赔)?)",
    ]
    value = text
    for pattern in patterns:
        value = re.sub(pattern, "", value)
    return value


def _legal_advice_matches(text: str, matched: list[str]) -> list[str]:
    if not matched:
        return []
    advice_terms = [
        "法律建议",
        "法律意见",
        "法律咨询",
        "维权指导",
        "起诉模板",
        "诉讼策略",
        "诉讼建议",
        "胜诉保证",
        "保证胜诉",
        "合同范本",
        "合同模板",
        "如何起诉",
        "如何应诉",
    ]
    if any(term in text for term in advice_terms):
        return matched
    # News reports about lawsuits, patents, regulation, and enforcement are
    # allowed; the gate is for advice-like legal claims, not legal news.
    news_context_terms = [
        "报道",
        "新闻",
        "起诉",
        "提起诉讼",
        "专利战",
        "互告",
        "法院",
        "监管",
        "指控",
        "案件",
        "涉嫌",
        "调查",
        "执法",
    ]
    if any(term in text for term in news_context_terms):
        return [term for term in matched if term not in {"诉讼", "合同纠纷"}]
    return matched


def _source_warnings(sources: Any, article: dict[str, Any] | None = None) -> list[str]:
    if not isinstance(sources, list) or not sources:
        return ["missing_sources"]
    policy = str((article or {}).get("source_policy", "")).strip()
    warnings = []
    news_status = str((article or {}).get("news_status", "")).strip()
    if news_status and news_status not in {"ok", "loaded", "supplemented"}:
        warnings.append(f"news_source_quality_{news_status}")
    for source in sources:
        if not isinstance(source, dict):
            warnings.append("invalid_source")
            continue
        name = str(source.get("name", ""))
        url = str(source.get("url", ""))
        if policy in {"local_topic_explainer", "daily_practical_takeaway"} and _is_topics_config_source(url):
            continue
        if "本地选题配置" in name or _is_topics_config_source(url):
            warnings.append("local_topic_config_source_requires_fact_check")
        if "待补充" in name or "待补充" in str(source.get("summary", "")):
            warnings.append("placeholder_source_requires_fact_check")
        if "昨日讨论记录" in name:
            warnings.append("discussion_source_requires_review")
    return warnings


def _is_topics_config_source(url: str) -> bool:
    normalized = url.replace("\\", "/").rstrip("/")
    return normalized == "topics.yaml" or normalized.endswith("/topics.yaml")


def _risk_level(checks: list[dict[str, Any]]) -> str:
    if any(not check["ok"] and check["severity"] == "high" for check in checks):
        return "high"
    if any(not check["ok"] and check["severity"] == "medium" for check in checks):
        return "medium"
    return "low"


def _recommendations(risk_level: str, flags: dict[str, bool], source_warnings: list[str]) -> list[str]:
    recommendations = []
    if risk_level != "low":
        recommendations.append("发布前需要人工复核并修改文章。")
    if flags.get("fact_uncertain") or source_warnings:
        recommendations.append("补充最新、可核验的外部事实来源。")
    if flags.get("sensitive_topic"):
        recommendations.append("移除或改写敏感主题、医疗/法律/投资等高风险表达。")
    if not recommendations:
        recommendations.append("本地规则审核未发现明显风险，可进入下一步发布策略判断。")
    return recommendations


if __name__ == "__main__":
    raise SystemExit(main())
