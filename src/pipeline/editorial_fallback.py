from __future__ import annotations

import html
import re
from typing import Any


ACTION_RULES = (
    (("open source", "open-source", "开源"), "开源"),
    (("upgrade", "update", "adds", "improves", "升级", "更新"), "升级"),
    (("launch", "unveil", "release", "introduc", "ship", "发布", "推出", "上线"), "发布"),
    (("funding", "raises", "investment", "融资", "获投"), "完成融资"),
    (("acquire", "acquisition", "收购", "并购"), "完成收购"),
    (("partner", "partnership", "collaborat", "合作"), "推进合作"),
    (("buy", "purchase", "pay", "采购", "购买"), "采购"),
    (("deploy", "rollout", "量产", "交付", "落地"), "推进落地"),
    (("ipo", "s-1", "上市"), "推进IPO"),
)

TOPIC_RULES = (
    (("memory", "记忆"), "记忆能力"),
    (("virtual power plant", "虚拟电厂"), "虚拟电厂"),
    (("biodefense", "biological resilience", "生物安全"), "AI生物安全"),
    (("lawsuit", "court", "诉状", "诉讼", "法院"), "AI司法应用"),
    (("copyright", "版权"), "AI版权治理"),
    (("command line", " cli", "命令行"), "命令行工具"),
    (("customer-service", "customer service", "chatbot", "客服"), "客服AI体验"),
    (("retail", "dashboard", "零售", "看板"), "零售AI看板"),
    (("logistics", "planning", "物流", "排产"), "物流AI规划"),
    (("workflow", "工作流"), "企业工作流能力"),
    (("agent-to-agent", "agentic", "智能体", " agent"), "智能体能力"),
    (("robot", "机器人", "具身智能"), "机器人能力"),
    (("compute", "gpu", "算力", "数据中心"), "AI算力服务"),
    (("payment", "支付", "结算"), "AI支付方案"),
    (("cloud", "云服务", "云平台"), "AI云服务"),
    (("chip", "芯片"), "AI芯片"),
    (("model", "模型", "大模型"), "AI模型"),
    (("api", "接口"), "AI能力API"),
    (("pc", "personal computer", "个人电脑"), "个人电脑AI能力"),
    (("healthcare", "health care", "医疗"), "医疗AI应用"),
    (("insurance", "claims", "理赔"), "保险AI应用"),
)

STOP_TOKENS = {
    "a",
    "age",
    "ai",
    "ai-generated",
    "api",
    "an",
    "and",
    "announces",
    "built",
    "biodefense",
    "biological",
    "cli",
    "confidential",
    "for",
    "from",
    "enterprise",
    "how",
    "is",
    "are",
    "could",
    "will",
    "introducing",
    "launches",
    "new",
    "news",
    "intelligence",
    "pc",
    "gpu",
    "cpu",
    "of",
    "official",
    "our",
    "the",
    "retail",
    "logistics",
    "startup",
    "virtual",
    "to",
    "update",
    "with",
    "would",
    "researcher",
    "researchers",
    "teleoperated",
    "birdlike",
}


def fallback_headline_clause(item: dict[str, Any]) -> str:
    fact_clause = _fact_card_clause(item, limit=30)
    if fact_clause:
        return fact_clause
    chinese = _usable_chinese_title(item)
    if chinese and len(chinese) <= 30:
        return _complete_clause(chinese, 30)
    subject = _subject(item)
    topic = _topic(item)
    action = _action(item)
    product = _product(item, subject=subject)
    object_label = _object_label(topic, product=product)
    return _compose(subject, action, object_label, max_length=30)


def fallback_title_element(item: dict[str, Any]) -> str:
    topic = _topic(item)
    subject = _subject(item)
    product = _product(item, subject=subject)
    if not subject and not product and topic != "AI产品能力":
        return topic
    clause = fallback_headline_clause(item)
    for separator in ("，", "；", "：", ":", "。"):
        if separator in clause:
            clause = clause.split(separator, 1)[0]
            break
    return clause[:18].strip(" 　:：，,；;、。") or "科技产品更新"


def fallback_detail_heading(item: dict[str, Any]) -> str:
    fact_clause = _fact_card_clause(item, limit=64)
    if fact_clause:
        return fact_clause
    chinese = _usable_chinese_title(item)
    if chinese:
        return _complete_clause(chinese, 64)
    clause = fallback_headline_clause(item)
    summary = _clean_text(str(item.get("summary", "")))
    if _contains_chinese(summary) and len(summary) >= 12:
        return _complete_clause(summary, 64)
    return clause


def _fact_card_clause(item: dict[str, Any], *, limit: int) -> str:
    card = item.get("fact_card", {})
    if not isinstance(card, dict):
        return ""
    facts = card.get("facts", [])
    candidates = [str(value) for value in facts if isinstance(value, str)] if isinstance(facts, list) else []
    action = str(card.get("action", "")).strip()
    subject = str(card.get("subject", "")).strip()
    candidates.extend([action, f"{subject}{action}" if subject and subject not in action else ""])
    for candidate in candidates:
        value = _clean_text(candidate)
        if len(value) < 8 or not _contains_chinese(value):
            continue
        if len(value) <= limit:
            return value
        for separator in ("；", "，", "：", ":", "。", "！", "？"):
            cut = value.rfind(separator, 8, limit + 1)
            if cut >= 8:
                return value[:cut].strip(" 　:：，,；;、。")
    return ""


def _subject(item: dict[str, Any]) -> str:
    title = str(item.get("title", ""))
    chinese_prefix = re.match(r"^([\u4e00-\u9fffA-Za-z0-9.+-]{2,12})[：:]", title.strip())
    if chinese_prefix:
        return chinese_prefix.group(1)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", title):
        if token.lower() in STOP_TOKENS:
            continue
        if _looks_like_name(token):
            return token
    return ""


def _product(item: dict[str, Any], *, subject: str) -> str:
    title = str(item.get("title", ""))
    candidates = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", title):
        if token.lower() in STOP_TOKENS or token.lower() == subject.lower():
            continue
        if _looks_like_name(token):
            candidates.append(token)
    return candidates[0] if candidates else ""


def _looks_like_name(token: str) -> bool:
    return bool(
        token[0].isupper()
        or
        any(char.isdigit() for char in token)
        or any(char.isupper() for char in token[1:])
        or token.isupper()
        or token.lower().endswith(("ai", "gpt"))
    )


def _topic(item: dict[str, Any]) -> str:
    combined = _combined(item)
    for keywords, label in TOPIC_RULES:
        if any(keyword in combined for keyword in keywords):
            return label
    return "AI产品能力"


def _action(item: dict[str, Any]) -> str:
    combined = _combined(item)
    if any(keyword in combined for keyword in ("lawsuit", "诉状", "诉讼")) and any(
        keyword in combined for keyword in ("court", "法院")
    ):
        return "进入法院"
    if "virtual power plant" in combined and any(keyword in combined for keyword in ("data center", "供电")):
        return "开始供电"
    for keywords, label in ACTION_RULES:
        if any(keyword in combined for keyword in keywords):
            return label
    if any(keyword in combined for keyword in ("进入", "采用", "使用", "应用")):
        return "进入应用"
    return "出现新进展"


def _object_label(topic: str, *, product: str) -> str:
    if product and product.lower() not in topic.lower():
        if topic == "记忆能力":
            return f"{product}记忆能力"
        if topic in {"AI模型", "AI能力API"}:
            suffix = "模型" if topic == "AI模型" else "API"
            return f"{product}{suffix}"
        if topic in {"智能体能力", "机器人能力", "企业工作流能力"}:
            return f"{product}{topic}"
    return topic


def _compose(subject: str, action: str, object_label: str, *, max_length: int) -> str:
    if subject and object_label.lower().startswith(subject.lower()):
        value = f"{object_label}{action}"
    elif subject:
        value = f"{subject}{action}{object_label}"
    else:
        value = f"{object_label}{action}"
    return _complete_clause(value, max_length)


def _usable_chinese_title(item: dict[str, Any]) -> str:
    title = _clean_text(str(item.get("title", "")))
    if not _contains_chinese(title):
        return ""
    title = re.sub(r"^(?:刚刚|消息称|报道称|独家|快讯)\s*[丨|:：，,\-—]*\s*", "", title)
    title = re.sub(r"^(?:记者|编辑|作者|文)\s*[丨|:：].*$", "", title)
    return title if len(title) >= 8 else ""


def _combined(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(key, "")) for key in ("title", "summary", "source_name")).lower()


def _clean_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip(" 　:：，,；;、。")


def _contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def _complete_clause(value: str, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    for separator in ("；", "，", "：", ":", "。", "！", "？"):
        cut = text.rfind(separator, 8, limit + 1)
        if cut >= 8:
            return text[:cut].strip(" 　:：，,；;、。")
    return text[:limit].rstrip(" 　:：，,；;、。")
