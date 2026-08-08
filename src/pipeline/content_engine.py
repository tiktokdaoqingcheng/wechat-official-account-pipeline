from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.pipeline.editorial_fallback import fallback_detail_heading, fallback_headline_clause, fallback_title_element


ARTICLE_SCHEMA_VERSION = "article.v1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and validate an article draft from a topic seed.")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    seed = json.loads(Path(args.seed).read_text(encoding="utf-8-sig"))
    article = build_article(seed)
    errors = validate_article(article)
    if errors:
        raise ValueError("Invalid article draft: " + "; ".join(errors))

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(article, ensure_ascii=False, indent=2))
    else:
        print(f"Title: {article['title']}")
        print(f"Topic: {article['topic']}")
        print(f"Word count estimate: {article['word_count_estimate']}")
        if args.output:
            print(f"Output: {args.output}")
    return 0


def build_article(seed: dict[str, Any]) -> dict[str, Any]:
    topic = str(seed["topic"]).strip()
    audience = str(seed.get("audience") or "对 AI 感兴趣但专业知识了解不多的读者")
    column = str(seed.get("column", "")).strip()
    title = str(seed.get("title") or _title_for_seed(topic, seed)).strip()
    digest = str(seed.get("digest") or _digest_for_seed(topic, seed)).strip()
    sections = _sections_for_seed(topic, seed)

    article = {
        "schema_version": ARTICLE_SCHEMA_VERSION,
        "title": title,
        "author": "AI 自动化编辑",
        "digest": digest,
        "topic": topic,
        "column": column,
        "audience": audience,
        "topic_reason": str(seed.get("topic_reason", "")).strip(),
        "source_policy": str(seed.get("source_policy", "")).strip(),
        "risk_tags": seed.get("risk_tags", []) if isinstance(seed.get("risk_tags", []), list) else [],
        "sections": sections,
        "sources": seed.get("sources", []) if isinstance(seed.get("sources", []), list) else [],
        "cover_prompt": _cover_prompt(topic),
        "word_count_estimate": sum(len(p) for s in sections for p in s["paragraphs"]),
    }
    if seed.get("target_date"):
        article["target_date"] = str(seed.get("target_date", "")).strip()
    if isinstance(seed.get("news_items"), list):
        article["news_items"] = seed["news_items"]
        article["news_status"] = str(seed.get("news_status", "")).strip()
    if isinstance(seed.get("reserve_news_items"), list):
        article["reserve_news_items"] = seed["reserve_news_items"]
    if seed.get("target_news_item_count") is not None:
        try:
            article["target_news_item_count"] = max(0, int(seed.get("target_news_item_count", 0) or 0))
        except (TypeError, ValueError):
            pass
    if isinstance(seed.get("wechat_platform_risk_replacements"), list):
        article["wechat_platform_risk_replacements"] = seed["wechat_platform_risk_replacements"]
    return article


def validate_article(article: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required_text_fields = ["schema_version", "title", "author", "digest", "topic", "audience", "cover_prompt"]
    for field in required_text_fields:
        if not str(article.get(field, "")).strip():
            errors.append(f"{field} is required")

    if article.get("schema_version") != ARTICLE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {ARTICLE_SCHEMA_VERSION}")

    sections = article.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("sections must be a non-empty list")
    else:
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                errors.append(f"sections[{index}] must be an object")
                continue
            if not str(section.get("heading", "")).strip():
                errors.append(f"sections[{index}].heading is required")
            paragraphs = section.get("paragraphs")
            if not isinstance(paragraphs, list) or not paragraphs:
                errors.append(f"sections[{index}].paragraphs must be a non-empty list")
            elif any(not str(paragraph).strip() for paragraph in paragraphs):
                errors.append(f"sections[{index}].paragraphs contains empty text")

    sources = article.get("sources")
    if not isinstance(sources, list):
        errors.append("sources must be a list")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                errors.append(f"sources[{index}] must be an object")
                continue
            if not str(source.get("name", "")).strip():
                errors.append(f"sources[{index}].name is required")
            if not str(source.get("summary", "")).strip():
                errors.append(f"sources[{index}].summary is required")

    if int(article.get("word_count_estimate", 0) or 0) <= 0:
        errors.append("word_count_estimate must be positive")
    return errors


def render_markdown(article: dict[str, Any]) -> str:
    parts = [f"# {article['title']}", "", f"> {article['digest']}", ""]
    if article.get("column"):
        parts.extend([f"栏目：{article['column']}", ""])
    for section in article["sections"]:
        parts.append(f"## {section['heading']}")
        parts.append("")
        parts.extend(section["paragraphs"])
        parts.append("")
    parts.append("## 参考来源")
    parts.append("")
    for source in article["sources"]:
        if _source_is_promotional(source):
            continue
        url = str(source.get("url", "")).strip()
        if url:
            parts.append(f"- [{source['name']}]({url}): {source['summary']}")
        else:
            parts.append(f"- {source['name']}: {source['summary']}")
    parts.append("")
    return "\n".join(parts)


def render_wechat_html(article: dict[str, Any]) -> str:
    if _is_single_news_extension(article):
        return _news_extension_wechat_html(article)
    if _is_primary_news_briefing(article):
        return _tech_briefing_wechat_html(article)

    parts = [
        '<section style="margin:0;padding:0 1px;font-size:16px;line-height:1.85;color:#2f3440;letter-spacing:0;background:#ffffff;">',
        _wechat_header_html(article),
        _wechat_illustration_html(article),
        _wechat_insight_card_html(article),
        _wechat_reading_map_html(article),
    ]
    sections = article["sections"]
    inline_image_index = 0
    for index, section in enumerate(sections):
        heading = str(section["heading"])
        parts.append(_wechat_section_heading_html(str(section["heading"])))
        for paragraph in section["paragraphs"]:
            parts.append(_wechat_paragraph_html(str(paragraph)))
        if _should_insert_inline_image(article, heading=heading, section_index=index, image_index=inline_image_index):
            image_html = _wechat_inline_image_html(article, inline_image_index)
            if image_html:
                parts.append(image_html)
                inline_image_index += 1
        if index == len(sections) - 1:
            parts.append(_wechat_action_card_html(article))
    parts.append(_wechat_sources_html(article["sources"], news_items=article.get("news_items", [])))
    parts.append("</section>")
    return "\n".join(parts)


def _is_primary_news_briefing(article: dict[str, Any]) -> bool:
    news_items = article.get("news_items", [])
    return (
        isinstance(news_items, list)
        and bool(news_items)
        and not _is_single_news_extension(article)
        and not _is_daily_practical_takeaway(article)
    )


def _is_single_news_extension(article: dict[str, Any]) -> bool:
    return str(article.get("source_policy", "")).strip() == "single_news_extension"


def _is_daily_practical_takeaway(article: dict[str, Any]) -> bool:
    return str(article.get("source_policy", "")).strip() == "daily_practical_takeaway"


def _is_smart_manufacturing_daily(article: dict[str, Any]) -> bool:
    return str(article.get("source_policy", "")).strip() == "smart_manufacturing_daily"


def _tech_briefing_wechat_html(article: dict[str, Any]) -> str:
    news_items = _briefing_news_items(article.get("news_items", []))[:10]
    grouped_items = _briefing_grouped_items(news_items, article=article)
    inline_image_index = 0
    inline_images = article.get("inline_images", [])
    has_source_inline_images = isinstance(inline_images, list) and any(
        isinstance(image, dict) and str(image.get("source_item_index", "")).strip()
        for image in inline_images
    )
    parts = [
        '<section style="margin:0;padding:0 1px;font-size:16px;line-height:1.86;color:#222222;letter-spacing:0;background:#ffffff;">',
        _briefing_header_html(article),
        _wechat_illustration_html(article),
        _briefing_daily_thread_html(article, news_items),
        _briefing_watchlist_html(news_items),
    ]
    for group_index, (title, items) in enumerate(grouped_items):
        parts.append(_briefing_category_heading_html(title))
        if group_index < 2 and not has_source_inline_images:
            image_html = _briefing_inline_image_html(article, inline_image_index)
            if image_html:
                parts.append(image_html)
                inline_image_index += 1
        for index, item in enumerate(items, start=1):
            parts.append(_briefing_news_item_html(item, index=index, article=article))
            if has_source_inline_images:
                image_html = _briefing_source_inline_image_html(article, item)
                if image_html:
                    parts.append(image_html)
    parts.append(_briefing_takeaway_html(article))
    parts.append(_wechat_sources_html(article["sources"], news_items=news_items))
    parts.append("</section>")
    return "\n".join(part for part in parts if part)


def _news_extension_wechat_html(article: dict[str, Any]) -> str:
    parts = [
        '<section style="margin:0;padding:0 1px;font-size:16px;line-height:1.86;color:#222222;letter-spacing:0;background:#ffffff;">',
        _extension_header_html(article),
        _wechat_illustration_html(article),
    ]
    sections = article["sections"]
    for section in sections:
        parts.append(_briefing_category_heading_html(str(section["heading"])))
        for paragraph in section["paragraphs"]:
            parts.append(_briefing_paragraph_html(str(paragraph)))
    parts.append(_wechat_sources_html(article["sources"], news_items=article.get("news_items", [])))
    parts.append("</section>")
    return "\n".join(part for part in parts if part)


def _briefing_header_html(article: dict[str, Any]) -> str:
    day_line = _briefing_day_line(str(article.get("target_date", "")).strip())
    digest = html.escape(str(article.get("digest", "")).strip())
    if _is_smart_manufacturing_daily(article):
        label = "智能制造日报"
        line = "昨日产业线索"
    else:
        label = "科技早报"
        line = "昨日科技线索"
    return "\n".join(
        [
            '<section style="margin:0 0 20px 0;padding:0 0 14px 0;border-bottom:2px solid #ff6a00;">',
            f'<p style="margin:0 0 8px 0;color:#ff6a00;font-size:15px;line-height:1.5;font-weight:900;">{label}</p>',
            f'<p style="margin:0 0 8px 0;color:#111111;font-size:18px;line-height:1.55;font-weight:900;">{html.escape(day_line)}，{line}</p>',
            f'<p style="margin:0;color:#666666;font-size:14px;line-height:1.8;">{digest}</p>',
            "</section>",
        ]
    )


def _extension_header_html(article: dict[str, Any]) -> str:
    title = html.escape(str(article.get("title", "")).strip())
    digest = html.escape(str(article.get("digest", "")).strip())
    return "\n".join(
        [
            '<section style="margin:0 0 20px 0;padding:0 0 14px 0;border-bottom:2px solid #ff6a00;">',
            '<p style="margin:0 0 8px 0;color:#ff6a00;font-size:15px;line-height:1.5;font-weight:900;">科技新闻延展</p>',
            f'<p style="margin:0 0 8px 0;color:#111111;font-size:20px;line-height:1.5;font-weight:900;">{title}</p>',
            f'<p style="margin:0;color:#666666;font-size:14px;line-height:1.8;">{digest}</p>',
            "</section>",
        ]
    )


def _briefing_day_line(target_date: str) -> str:
    if target_date:
        try:
            current = datetime.strptime(target_date, "%Y-%m-%d").date() + timedelta(days=1)
            return f"{current.month}月{current.day}日，星期{_weekday_cn(current.weekday())}"
        except ValueError:
            pass
    return "今天"


def _weekday_cn(index: int) -> str:
    return ("一", "二", "三", "四", "五", "六", "日")[index]


def _briefing_watchlist_html(news_items: list[dict[str, Any]]) -> str:
    if not news_items:
        return ""
    lines = []
    seen_labels: set[str] = set()
    for item in news_items:
        label = _watchlist_subtitle(item)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        index = len(lines) + 1
        title = html.escape(label)
        source = html.escape(str(item.get("source_name", "")).strip() or "公开来源")
        lines.append(
            '<section style="margin:0;padding:12px 0;border-bottom:1px solid #f1f1f1;">'
            f'<p style="margin:0 0 4px 0;color:#111111;font-size:16px;line-height:1.6;font-weight:900;"><span style="display:inline-block;margin:0 8px 0 0;color:#ff6a00;font-size:16px;font-weight:900;">{index}</span>{title}</p>'
            f'<p style="margin:0;color:#777777;font-size:13px;line-height:1.65;">来源：{source}</p>'
            "</section>"
        )
        if len(lines) >= 5:
            break
    return "\n".join(
        [
            '<section style="margin:0 0 22px 0;padding:2px 0 0 0;">',
            '<p style="margin:0 0 8px 0;color:#ff6a00;font-size:14px;line-height:1.5;font-weight:900;">今日看点</p>',
            *lines,
            "</section>",
        ]
    )


def _briefing_daily_thread_html(article: dict[str, Any], news_items: list[dict[str, Any]]) -> str:
    if not news_items:
        return ""
    label = "今日主线"
    body = _briefing_daily_thread_text(article, news_items)
    return "\n".join(
        [
            '<section style="margin:0 0 22px 0;padding:14px 14px 13px 14px;background:#fff7f0;border-left:3px solid #ff6a00;">',
            f'<p style="margin:0 0 7px 0;color:#ff6a00;font-size:14px;line-height:1.5;font-weight:900;">{label}</p>',
            f'<p style="margin:0;color:#333333;font-size:15px;line-height:1.9;">{html.escape(body)}</p>',
            "</section>",
        ]
    )


def _briefing_daily_thread_text(article: dict[str, Any], news_items: list[dict[str, Any]]) -> str:
    lenses = _briefing_lens_counts(news_items)
    top_lenses = [name for name, _count in sorted(lenses.items(), key=lambda row: (-row[1], row[0])) if _count][:3]
    if _is_smart_manufacturing_daily(article):
        if not top_lenses:
            top_lenses = ["产业落地", "交付验证"]
        return (
            "今天的智能制造线索可以先看一条主线："
            f"{'、'.join(top_lenses)}正在把机器人、工厂和产业项目从概念推进到真实交付。"
            "读这篇时，重点看客户、产线、供应链和验证标准是否越来越具体。"
        )
    if not top_lenses:
        top_lenses = ["产品入口", "基础设施", "真实场景"]
    return (
        "今天的几条科技新闻其实指向同一件事："
        f"{'、'.join(top_lenses)}正在把 AI 从模型新闻继续推向产品、预算和工作流程。"
        "读完这篇，先记住哪些能力真的进入了入口，哪些还只是行业信号。"
    )


def _briefing_lens_counts(news_items: list[dict[str, Any]]) -> dict[str, int]:
    lenses = {
        "产品入口": 0,
        "基础设施": 0,
        "商业信号": 0,
        "工作流程": 0,
        "责任边界": 0,
        "产业落地": 0,
        "交付验证": 0,
    }
    for item in news_items:
        combined = _combined_news_text(item)
        if any(keyword in combined for keyword in ("app", "product", "assistant", "手机", "入口", "工具", "产品", "助手")):
            lenses["产品入口"] += 1
        if any(keyword in combined for keyword in ("model", "chip", "gpu", "cloud", "token", "data center", "算力", "云", "芯片", "成本")):
            lenses["基础设施"] += 1
        if any(keyword in combined for keyword in ("funding", "raises", "ipo", "revenue", "融资", "获投", "并购", "上市", "营收")):
            lenses["商业信号"] += 1
        if any(keyword in combined for keyword in ("workflow", "admin", "claims", "insurance", "retail", "logistics", "流程", "理赔", "行政", "物流")):
            lenses["工作流程"] += 1
        if any(keyword in combined for keyword in ("court", "lawsuit", "safety", "security", "copyright", "regulation", "法院", "诉讼", "监管", "版权", "安全")):
            lenses["责任边界"] += 1
        if any(keyword in combined for keyword in ("factory", "manufacturing", "industrial", "robot", "evtol", "satellite", "工厂", "制造", "工业", "机器人", "低空", "卫星")):
            lenses["产业落地"] += 1
        if any(keyword in combined for keyword in ("delivery", "production", "benchmark", "standard", "量产", "交付", "验证", "标准", "供应链", "客户", "订单")):
            lenses["交付验证"] += 1
    return lenses


def _watchlist_subtitle(item: dict[str, Any]) -> str:
    title = str(item.get("title", "")).strip()
    model_heading = _model_detail_heading(item)
    if model_heading:
        return model_heading
    detail = news_detail_heading(item)
    if _mostly_ascii(title) or len(title) > 46:
        return detail
    return title or detail


def _briefing_grouped_items(
    news_items: list[dict[str, Any]],
    *,
    article: dict[str, Any] | None = None,
) -> list[tuple[str, list[dict[str, Any]]]]:
    if article and _is_smart_manufacturing_daily(article):
        groups = [
            ("机器人与自动化", ("robot", "robotics", "embodied intelligence", "机器人", "人形", "具身", "自动化", "产线")),
            ("低空经济与交通装备", ("evtol", "低空", "飞行器", "自动驾驶", "航空", "卫星", "satellite", "space", "smart driving", "electric vehicle", "battery output", "production")),
            ("智能工厂与工业AI", ("ai factory", "工厂", "制造", "工业", "仿真", "数字孪生", "工业软件")),
            ("芯片、算力与供应链", ("chip", "gpu", "芯片", "算力", "数据中心", "半导体", "供应链")),
            ("项目落地与资本", ("融资", "获投", "总部", "落户", "ipo", "合作", "项目", "签约")),
        ]
    else:
        groups = [
            ("AI 产品与平台", ("model", "assistant", "agent", "api", "platform", "search", "office", "developer", "cli", "模型", "助手", "智能体", "平台")),
            ("政策、版权与治理", ("lawsuit", "court", "copyright", "policy", "regulation", "privacy", "safety")),
            ("科技公司与资本", ("ipo", "startup", "funding", "revenue", "spending", "market")),
            ("产业应用", ("virtual power", "energy", "robot", "warehouse", "health", "claims", "small business", "admin")),
            ("硬件与算力", ("gpu", "chip", "compute", "data center", "windows pc", "device", "pc", "芯片", "算力", "设备")),
        ]
    used: set[int] = set()
    result: list[tuple[str, list[dict[str, Any]]]] = []
    for title, keywords in groups:
        items = []
        for index, item in enumerate(news_items):
            if index in used:
                continue
            combined = _combined_news_text(item)
            if any(_keyword_in_news_text(combined, keyword) for keyword in keywords):
                items.append(item)
                used.add(index)
        if items:
            result.append((title, items))
    remaining = [item for index, item in enumerate(news_items) if index not in used]
    if remaining:
        result.insert(0, ("其他重要动态", remaining))
    return result


def _briefing_category_heading_html(title: str) -> str:
    return "\n".join(
        [
            '<section style="margin:28px 0 12px 0;padding:0 0 8px 0;border-bottom:1px solid #f3c29d;">',
            f'<p style="margin:0;color:#ff6a00;font-size:17px;line-height:1.5;font-weight:900;">{html.escape(title)}</p>',
            "</section>",
        ]
    )


def _briefing_news_item_html(item: dict[str, Any], *, index: int, article: dict[str, Any] | None = None) -> str:
    label = html.escape(news_detail_heading(item))
    source = html.escape(str(item.get("source_name", "")).strip() or "公开来源")
    paragraphs = [html.escape(paragraph) for paragraph in _briefing_news_body_paragraphs(item, article=article)]
    if not paragraphs:
        paragraphs = [html.escape(_briefing_news_body_text(item))]
    first_paragraph = paragraphs[0]
    extra_paragraphs = [
        f'<p style="margin:9px 0 0 0;color:#333333;font-size:15px;line-height:2.02;">{paragraph}</p>'
        for paragraph in paragraphs[1:]
    ]
    return "\n".join(
        [
            '<section style="margin:0 0 30px 0;padding:0;">',
            f'<p style="margin:0;color:#222222;font-size:15px;line-height:2.05;"><span style="color:#ea5414;font-weight:800;">【{label}】</span>{first_paragraph}</p>',
            *extra_paragraphs,
            f'<p style="margin:9px 0 0 0;color:#777777;font-size:13px;line-height:1.7;">来源：{source}</p>',
            "</section>",
        ]
    )


def _briefing_inline_image_html(article: dict[str, Any], index: int) -> str:
    inline_images = article.get("inline_images", [])
    if not isinstance(inline_images, list) or index >= len(inline_images):
        return ""
    image = inline_images[index]
    if not isinstance(image, dict):
        return ""
    placeholder = str(image.get("placeholder", "")).strip()
    if not placeholder:
        return ""
    alt = html.escape(str(image.get("alt", "科技早报配图")).strip())
    caption = html.escape(str(image.get("caption", "")).strip() or "AI 生成配图 · 辅助理解新闻脉络")
    return "\n".join(
        [
            '<section style="margin:2px 0 24px 0;">',
            f'<img src="{html.escape(placeholder)}" alt="{alt}" style="display:block;width:100%;height:auto;border-radius:6px;border:1px solid #eeeeee;" />',
            f'<p style="margin:7px 0 0 0;color:#999999;font-size:12px;line-height:1.5;text-align:center;">{caption}</p>',
            "</section>",
        ]
    )


def _briefing_source_inline_image_html(article: dict[str, Any], item: dict[str, Any]) -> str:
    inline_images = article.get("inline_images", [])
    if not isinstance(inline_images, list):
        return ""
    item_index = _news_item_original_index(article, item)
    if item_index < 0:
        return ""
    for index, image in enumerate(inline_images):
        if not isinstance(image, dict):
            continue
        try:
            source_item_index = int(str(image.get("source_item_index", "")).strip())
        except ValueError:
            continue
        if source_item_index == item_index:
            return _briefing_inline_image_html(article, index)
    return ""


def _news_item_original_index(article: dict[str, Any], item: dict[str, Any]) -> int:
    news_items = article.get("news_items", [])
    if not isinstance(news_items, list):
        return -1
    for index, candidate in enumerate(news_items):
        if candidate is item:
            return index
    url = str(item.get("url", "")).strip()
    title = str(item.get("title", "")).strip()
    for index, candidate in enumerate(news_items):
        if not isinstance(candidate, dict):
            continue
        if url and str(candidate.get("url", "")).strip() == url:
            return index
        if title and str(candidate.get("title", "")).strip() == title:
            return index
    return -1


def _should_insert_inline_image(
    article: dict[str, Any],
    *,
    heading: str,
    section_index: int,
    image_index: int,
) -> bool:
    inline_images = article.get("inline_images", [])
    if not isinstance(inline_images, list) or image_index >= len(inline_images):
        return False
    if _is_daily_practical_takeaway(article):
        return heading in {"四行任务模板", "拿公众号运营举个例子"} and image_index == 0
    return section_index == 1 and image_index == 0


def _wechat_inline_image_html(article: dict[str, Any], index: int) -> str:
    inline_images = article.get("inline_images", [])
    if not isinstance(inline_images, list) or index >= len(inline_images):
        return ""
    image = inline_images[index]
    if not isinstance(image, dict):
        return ""
    placeholder = str(image.get("placeholder", "")).strip()
    if not placeholder:
        return ""
    alt = html.escape(str(image.get("alt", "AI 工具箱配图")).strip())
    caption = "AI 生成配图 · 辅助理解任务流程" if _is_daily_practical_takeaway(article) else "AI 生成配图 · 辅助理解文章脉络"
    return "\n".join(
        [
            '<section style="margin:4px 0 22px 0;">',
            f'<img src="{html.escape(placeholder)}" alt="{alt}" style="display:block;width:100%;height:auto;border-radius:6px;border:1px solid #e5e7eb;" />',
            f'<p style="margin:7px 0 0 0;color:#94a3b8;font-size:12px;line-height:1.5;text-align:center;">{caption}</p>',
            "</section>",
        ]
    )


def _briefing_news_body_paragraphs(item: dict[str, Any], *, article: dict[str, Any] | None = None) -> list[str]:
    core = _briefing_news_body_text(item)
    context = _briefing_news_context_text(item)
    reader = _briefing_news_reader_text(item)
    if _model_reader_summary(item):
        # The batch model already writes the publishable body. Appending a rule-generated
        # commentary paragraph is the main source of repeated, mechanical copy.
        candidates = (core,)
    elif article and _is_smart_manufacturing_daily(article):
        candidates = (core, context, reader)
    else:
        candidates = (core, context, _briefing_news_development_text(item), reader)
    paragraphs = []
    for paragraph in candidates:
        value = paragraph.strip()
        if value and value not in paragraphs:
            paragraphs.append(value)
    return paragraphs


def _briefing_news_body_text(item: dict[str, Any]) -> str:
    model_summary = _model_reader_summary(item)
    if model_summary:
        return model_summary
    line = _plain_chinese_news_line(item)
    if "”。" in line:
        body = line.split("”。", 1)[1].strip()
        if body:
            return body
    return line


def _briefing_news_context_text(item: dict[str, Any]) -> str:
    if _is_smart_manufacturing_item(item):
        return _smart_manufacturing_context_text(item)
    return _generic_briefing_news_context_text(item)


def _generic_briefing_news_context_text(item: dict[str, Any]) -> str:
    combined = _combined_news_text(item)
    label = news_detail_heading(item)
    subject = f"“{label}”" if label else "这条消息"
    source = str(item.get("source_name", "")).strip()
    source_intro = f"从{source}这条线索看，" if source else "从公开信息看，"
    lens, claim, watch = _generic_news_context_focus(combined)
    claim_clause = claim.rstrip("。！？!?")
    watch_clause = _briefing_watch_clause(watch)
    templates = [
        f"{subject}落在{lens}这一层：{claim_clause}。围绕{subject}，还要核对{watch_clause}，这些条件会决定它能否进入日常产品、企业采购或一线流程。",
        f"{source_intro}{subject}给出的信号在{lens}。对{subject}而言，{watch_clause}；信息越具体，越容易判断这项变化能否扩大。",
        f"{subject}短期看的是具体动作，长期看的是实际使用。把{subject}放进真实场景后，流程、成本和审核是否清楚，会决定它能否持续。",
        (
            f"{source_intro}{subject}可以从{lens}来读。关于{subject}，{watch_clause}。"
            f"能否把这些条件说清，是{subject}从发布走向实际应用的分界。"
        ),
        f"{subject}的后劲由{lens}决定。评价{subject}时，既要看到{claim_clause}，也要核对{watch_clause}。",
    ]
    return templates[_stable_news_variant(item, len(templates))]


def _briefing_watch_clause(text: str) -> str:
    value = str(text).strip().rstrip("。！？!?")
    value = re.sub(r"^(?:接下来要看|需要继续确认)\s*", "", value)
    return value or "真实使用条件是否清楚"


def _generic_news_context_focus(combined: str) -> tuple[str, str, str]:
    if any(
        _keyword_in_news_text(combined, keyword)
        for keyword in (
            "app",
            "product",
            "launch",
            "unveil",
            "introduce",
            "feature",
            "assistant",
            "手机",
            "入口",
            "工具",
            "产品",
            "助手",
        )
    ):
        return (
            "产品入口",
            "真正影响使用频率的，是它能否进入用户原本每天打开的工具，并减少切换和重复操作。",
            "需要继续确认默认入口、付费门槛和人工确认流程。",
        )
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("model", "chip", "gpu", "cloud", "token", "data center", "算力", "云", "芯片", "成本")):
        return (
            "基础设施",
            "模型、芯片、云和成本决定了 AI 能不能稳定供给。",
            "需要继续确认容量、价格和企业是否愿意持续采购。",
        )
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("funding", "raises", "ipo", "acquire", "revenue", "融资", "获投", "并购", "上市", "营收")):
        return (
            "商业信号",
            "融资、并购和收入变化真正说明的是资源开始往某些赛道集中。",
            "需要继续确认收入质量、客户留存和场景能不能复制。",
        )
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("court", "lawsuit", "safety", "security", "copyright", "regulation", "法院", "诉讼", "监管", "版权", "安全")):
        return (
            "责任边界",
            "越接近法律、安全或版权场景，越不能只看效率。",
            "需要继续确认谁审核、谁负责，以及哪些流程会被写进制度。",
        )
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("workflow", "admin", "claims", "insurance", "retail", "logistics", "流程", "理赔", "行政", "物流")):
        return (
            "工作流程",
            "它影响的往往不是一个岗位，而是输入、处理、审核和交付的顺序。",
            "需要继续确认它能省掉哪一步，以及哪一步仍然需要人来兜底。",
        )
    return (
        "真实场景",
        "科技新闻真正影响读者的地方，通常在产品、预算、组织流程或基础设施的变化里。",
        "接下来要看使用门槛、责任边界和持续交付能力。",
    )


def _stable_news_variant(item: dict[str, Any], modulo: int) -> int:
    if modulo <= 1:
        return 0
    text = "|".join(
        str(item.get(key, "")).strip()
        for key in ("title", "summary", "source_name", "url", "published_at")
    )
    if not text:
        return 0
    return sum((index + 1) * ord(char) for index, char in enumerate(text)) % modulo


def _briefing_news_reader_text(item: dict[str, Any]) -> str:
    if _is_smart_manufacturing_item(item):
        return _smart_manufacturing_reader_text(item)
    return _generic_briefing_reader_text(item)


def _briefing_news_development_text(item: dict[str, Any]) -> str:
    combined = _combined_news_text(item)
    label = news_title_element(item) or news_detail_heading(item)
    subject = f"“{label}”" if label else "这条新闻"
    source = str(item.get("source_name", "")).strip()
    source_part = f"结合{source}给出的信息，" if source else ""

    lens, _, watch = _generic_news_context_focus(combined)
    watch_clause = _briefing_watch_clause(watch)
    templates = [
        f"{source_part}{subject}要看推进节奏，而不只是发布表述。围绕{subject}，{watch_clause}；条件明确后，用户才可能真正感知到变化。",
        f"{subject}也会影响同类公司怎么跟进：是把能力做成独立工具，还是塞进已有平台；是先服务企业客户，还是先抢个人入口。不同选择会决定它扩散得快不快。",
        f"把时间线拉长一点，{subject}更像是{lens}里的一个信号。短期看产品和合作，长期看成本、合规、客户留存和复购，只有这些指标接上，新闻才有持续价值。",
        f"{source_part}{subject}值得继续观察的不是声量，而是它是否带来可验证的改变：有没有真实用户，是否降低成本，能否被复核，出了问题由谁负责。",
    ]
    return templates[_stable_news_variant(item, len(templates))]


def _generic_briefing_reader_text(item: dict[str, Any]) -> str:
    combined = _combined_news_text(item)
    label = news_detail_heading(item)
    subject = f"“{label}”" if label else "这条新闻"
    lens, _, watch = _generic_news_context_focus(combined)
    watch_clause = _briefing_watch_clause(watch)
    templates = [
        f"普通读者可以把{subject}放回自己的工作或生活里判断：它到底能省掉哪一步，还是只是多了一层包装。能回答这个问题，新闻就不再只是热闹，而会变成一个可以试用、观察或避坑的线索。",
        f"观察{subject}时，不必只看发布声量。围绕{subject}，试点客户、使用门槛、价格体系和复核机制越清楚，越说明它接近真正交付。",
        f"阅读{subject}时可以记住一条线：{watch_clause}。关于{subject}的答案越具体，越能说明它是否靠近真实可用的工具。",
        f"{subject}不用马上下结论。等它进入真实产品、预算或流程，再看体验、价格、责任边界和用户反馈，会比追热度更稳。科技新闻最后拼的不是谁说得快，而是谁能持续交付。",
    ]
    return templates[_stable_news_variant(item, len(templates))]


def _briefing_paragraph_html(paragraph: str) -> str:
    return (
        '<p style="margin:0 0 15px 0;color:#333333;font-size:16px;line-height:1.92;">'
        f"{html.escape(paragraph)}"
        "</p>"
    )


def _briefing_takeaway_html(article: dict[str, Any]) -> str:
    news_items = _briefing_news_items(article.get("news_items", []))[:10]
    if _is_smart_manufacturing_daily(article):
        label = "今天最值得盯的一点"
        body = _smart_manufacturing_takeaway_text(news_items)
    else:
        label = "今天最值得盯的一点"
        body = _tech_briefing_takeaway_text(news_items)
    return "\n".join(
        [
            '<section style="margin:26px 0 0 0;padding:14px 0 0 0;border-top:2px solid #ff6a00;">',
            f'<p style="margin:0 0 8px 0;color:#ff6a00;font-size:15px;line-height:1.5;font-weight:900;">{label}</p>',
            f'<p style="margin:0;color:#333333;font-size:15px;line-height:1.85;">{body}</p>',
            "</section>",
        ]
    )


def _tech_briefing_takeaway_text(news_items: list[dict[str, Any]]) -> str:
    lenses = _briefing_lens_counts(news_items)
    top_lens = max(lenses.items(), key=lambda row: row[1])[0] if any(lenses.values()) else "真实场景"
    if top_lens == "产品入口":
        return "今天最值得盯的是入口变化：AI 正在继续被塞进手机、办公、开发和消息场景。真正能留下来的，不是多一个按钮，而是能不能减少切换、接住连续任务，并让用户清楚知道结果怎么复核。"
    if top_lens == "基础设施":
        return (
            "今天的基础设施线索集中在模型、芯片、云和电力。对企业来说，价格、可用容量、能耗和采购周期"
            "会直接进入部署预算；能力更新能否被采用，最后仍要落到这些数字上。"
        )
    if top_lens == "商业信号":
        return "今天最值得盯的是钱流向哪里：融资、并购和上市不会自动证明赛道成熟，但能看出资源正在集中到哪些产品和基础设施。后面要看真实客户、收入质量和复购，而不是只看估值。"
    if top_lens == "责任边界":
        return "今天最值得盯的是责任边界：AI 进入法律、安全、版权和隐私场景后，效率不是唯一问题。谁审核、谁负责、出了错怎么追溯，会越来越像产品能力的一部分。"
    return "今天最值得盯的是落地位置：这些新闻最终都要回到产品、预算、组织流程或基础设施里。能进入真实场景、降低成本、让结果可复核的变化，才值得持续跟踪。"


def _smart_manufacturing_takeaway_text(news_items: list[dict[str, Any]]) -> str:
    combined = " ".join(_combined_news_text(item) for item in news_items)
    themes: list[str] = []
    if any(keyword in combined for keyword in ("robot", "机器人", "人形", "具身", "moe", "视频模型")):
        themes.append("人形机器人与具身训练")
    if any(keyword in combined for keyword in ("factory", "工厂", "工业ai", "智能制造")):
        themes.append("智能工厂")
    if any(keyword in combined for keyword in ("evtol", "低空", "飞行器", "航空")):
        themes.append("低空与交通装备")
    if any(keyword in combined for keyword in ("chip", "gpu", "芯片", "算力", "供应链")):
        themes.append("芯片与供应链")
    focus = "、".join(themes[:3]) or "制造项目"
    return (
        f"昨日产业线索覆盖{focus}。有的公司已经给出采购或产能安排，有的项目仍停留在实验验证，"
        "也有开源工具刚进入开发者阶段，商业化进度并不相同。"
    )


def _title_for_seed(topic: str, seed: dict[str, Any]) -> str:
    news_items = seed.get("news_items", [])
    if isinstance(news_items, list) and news_items:
        if str(seed.get("source_policy", "")).strip() == "smart_manufacturing_daily":
            return _smart_manufacturing_daily_title(news_items)
        return _tech_briefing_title(news_items)
    return _title_for_topic(topic)


def _source_policy(seed: dict[str, Any]) -> str:
    return str(seed.get("source_policy", "")).strip()


def _title_for_topic(topic: str) -> str:
    if "智能体" in topic:
        return "AI 不只是聊天了：普通人为什么要开始理解“智能体”？"
    if "工具" in topic:
        return f"{topic}：新手真正需要先搞懂的几件事"
    if "效率" in topic or "工作" in topic:
        return f"{topic}：把重复工作交给 AI 之前，先想清楚这三点"
    return f"{topic}：普通人也能看懂的 AI 白话解读"


def news_title_element(item: dict[str, Any]) -> str:
    return _news_title_element(item)


def news_detail_heading(item: dict[str, Any]) -> str:
    return _news_detail_heading(item)


def _tech_briefing_title(news_items: list[dict[str, Any]]) -> str:
    headline_clauses = _headline_title_clauses(news_items)
    if headline_clauses:
        return _join_headline_title(headline_clauses)

    elements: list[str] = []
    for item in _briefing_news_items(news_items):
        element = _news_title_element(item)
        if element and _specific_headline_clause(element) and element not in elements:
            elements.append(element)
        if len(elements) >= 5:
            break
    if not elements:
        return "今日科技动态丨科技早报"

    selected: list[str] = []
    suffix = "丨科技早报"
    for element in elements:
        candidate = "；".join([*selected, element]) + suffix
        if len(candidate) <= 56:
            selected.append(element)
    if not selected:
        selected = [_shorten_title_element(elements[0], 12)]
    return "；".join(selected) + suffix


def _smart_manufacturing_daily_title(news_items: list[dict[str, Any]]) -> str:
    clauses: list[str] = []
    seen: set[str] = set()
    candidates: list[tuple[int, int, str]] = []
    for index, item in enumerate(_briefing_news_items(news_items)):
        clause = _smart_manufacturing_headline_clause(item)
        if not clause:
            continue
        candidates.append((_smart_manufacturing_headline_score(item), index, clause))
    for _score, _index, clause in sorted(candidates, key=lambda row: (-row[0], row[1])):
        key = _headline_dedupe_key(clause)
        if not key or key in seen:
            continue
        seen.add(key)
        clauses.append(clause)
        if len(clauses) >= 4:
            break
    if not clauses:
        clauses = ["智能工厂与机器人项目继续落地"]
    return _join_briefing_title(clauses, suffix="丨智能制造日报", fallback="智能制造日报", max_length=64, max_items=3)


def _smart_manufacturing_headline_clause(item: dict[str, Any]) -> str:
    model_detail = _clean_detail_heading(str(item.get("detail_heading", "")))
    if model_detail and not _needs_chinese_fallback(model_detail):
        return _trim_headline_clause(model_detail, 24)
    detail = _clean_detail_heading(str(item.get("title", "")))
    if detail and not _needs_chinese_fallback(detail):
        return _trim_headline_clause(detail, 24)
    return _trim_headline_clause(fallback_headline_clause(item), 24)


def _smart_manufacturing_headline_score(item: dict[str, Any]) -> int:
    combined = _combined_news_text(item)
    score = 0
    boosts = [
        (("ai factory",), 7),
        (("ai工厂",), 7),
        (("机器人",), 6),
        (("robot",), 6),
        (("evtol",), 6),
        (("低空",), 5),
        (("卫星",), 5),
        (("satellite",), 5),
        (("embodied intelligence",), 5),
        (("battery output",), 4),
        (("总部",), 4),
        (("量产",), 4),
        (("落户",), 4),
        (("融资",), 3),
        (("获投",), 3),
    ]
    for keywords, value in boosts:
        if all(keyword in combined for keyword in keywords):
            score += value
    if re.search(r"\d+(?:\.\d+)?\s*(?:%|亿|万|亿美元|million|billion)", combined, flags=re.IGNORECASE):
        score += 2
    return score


def _headline_title_clauses(news_items: list[dict[str, Any]]) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    fallback: list[tuple[int, int, str]] = []
    for index, item in enumerate(_briefing_news_items(news_items)):
        clause = _news_headline_clause(item)
        if not clause or not _specific_headline_clause(clause):
            continue
        score = _news_headline_score(item)
        if _is_weak_headline_item(item):
            fallback.append((score, index, clause))
            continue
        if score >= 3:
            candidates.append((score, index, clause))
        else:
            fallback.append((score, index, clause))

    ordered = sorted(candidates, key=lambda row: (-row[0], row[1]))
    if len(ordered) < 2:
        ordered.extend(sorted(fallback, key=lambda row: (-row[0], row[1])))

    clauses: list[str] = []
    seen: set[str] = set()
    for _score, _index, clause in ordered:
        key = _headline_dedupe_key(clause)
        if not key or key in seen:
            continue
        seen.add(key)
        clauses.append(clause)
        if len(clauses) >= 5:
            break
    return clauses


def _join_headline_title(clauses: list[str]) -> str:
    return _join_briefing_title(clauses, suffix="丨科技早报", fallback="今日科技动态", max_length=64, max_items=5)


def _specific_headline_clause(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    generic = (
        "科技应用更新",
        "AI模型更新",
        "AI模型能力继续更新",
        "具体场景和持续交付能力更值得看",
    )
    if any(phrase in text for phrase in generic):
        return False
    if re.search(r"(?:^|[\s，,:：；;、!！?？。])[A-Za-z]$", text):
        return False
    if re.search(r"(?:计划|拟|预计|目标|达|约|超|至|为|投入|估值)\s*\d+(?:\.\d+)?$", text):
        return False
    if text.endswith(("消息称", "报道称", "计划", "正式", "目标")):
        return False
    return True


def _join_briefing_title(
    clauses: list[str],
    *,
    suffix: str,
    fallback: str,
    max_length: int,
    max_items: int,
) -> str:
    selected: list[str] = []
    for clause in clauses:
        options = [clause, _trim_headline_clause(clause, 18), _trim_headline_clause(clause, 12)]
        for option in options:
            if not option:
                continue
            candidate = "；".join([*selected, option]) + suffix
            if len(candidate) <= max_length:
                selected.append(option)
                break
        if len(selected) >= max_items:
            break
    if not selected:
        selected = [_trim_headline_clause(clauses[0], max_length - len(suffix))]
    return ("；".join(selected).rstrip(" 　:：，,；;、。") or fallback) + suffix


def _news_headline_clause(item: dict[str, Any]) -> str:
    title = str(item.get("title", "")).strip()
    summary = _strip_leading_news_byline(str(item.get("summary", "")).strip())

    model_heading = _model_detail_heading(item)
    if model_heading:
        return _headline_clause_from_detail(model_heading, item)

    if _needs_chinese_fallback(title or summary):
        return _generic_chinese_headline_clause(item)
    heading = _news_detail_heading(item)
    if heading and heading != "科技热点":
        return _headline_clause_from_detail(heading, item)
    return _trim_headline_clause(_clean_title_element(title or summary), 18)


def _news_headline_score(item: dict[str, Any]) -> int:
    combined = _combined_news_text(item)
    score = 0
    strong_patterns = [
        (("发布", "企业"), 5),
        (("推出", "企业"), 5),
        (("launch", "enterprise"), 5),
        (("open source",), 4),
        (("开源",), 4),
        (("量产",), 5),
        (("交付",), 4),
        (("融资",), 4),
        (("acquisition",), 4),
        (("partnership",), 3),
        (("合作",), 3),
        (("机器人",), 4),
        (("robot",), 4),
        (("算力",), 4),
        (("compute",), 4),
        (("token",), 4),
        (("智能体",), 4),
        (("agent",), 4),
        (("大模型",), 3),
        (("ai",), 2),
    ]
    for keywords, value in strong_patterns:
        if all(keyword in combined for keyword in keywords):
            score += value
    if re.search(r"\d+(?:\.\d+)?\s*(?:%|亿|万|m|b|million|billion)", combined, flags=re.IGNORECASE):
        score += 2
    if any(keyword in combined for keyword in ("融资", "获投", "ipo", "收购", "转型")):
        score += 1
    if any(keyword in combined for keyword in ("确认出席", "applications officially close", "大赛", "正式启动", "开放报名", "峰会报名")):
        score -= 4
    if _is_low_signal_news_item(item):
        score -= 6
    if any(keyword in combined for keyword in ("公募基金", "私募基金", "基金业协会", "通道空转", "概念炒作", "荐股", "涨停")):
        score -= 8
    return score


def _is_weak_headline_item(item: dict[str, Any]) -> bool:
    combined = _combined_news_text(item)
    weak_terms = ("公募基金", "私募基金", "基金业协会", "通道空转", "概念炒作", "荐股", "涨停")
    if any(term in combined for term in weak_terms):
        return True
    event_terms = ("确认出席", "applications officially close", "大赛", "正式启动", "开放报名", "峰会报名")
    if any(term in combined for term in event_terms):
        return True
    title = str(item.get("title", "")).strip()
    return len(_clean_title_element(title)) <= 4 and _news_headline_score(item) < 4


def _trim_headline_clause(text: str, max_length: int) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip(" 　:：，,；;、。")
    value = re.sub(r"^(?:刚刚|消息称|报道称)\s*[，,:：]\s*", "", value)
    value = re.sub(r"[！!？?。](?:\s*[^；;，,。！？!?]{1,24})$", "", value).strip(" 　:：，,；;、。")
    if len(value) <= max_length:
        return value if _specific_headline_clause(value) else ""
    for separator in ("；", "，", "：", ":", "、", "！", "!", "？", "?", "。"):
        cut = value.rfind(separator, 0, max_length + 1)
        if cut >= 8:
            candidate = value[:cut].strip(" 　:：，,；;、。")
            return candidate if _specific_headline_clause(candidate) else ""
    return ""


def _headline_clause_from_detail(heading: str, item: dict[str, Any]) -> str:
    value = _trim_headline_clause(heading, 24)
    if not value or _needs_chinese_fallback(value):
        return _generic_chinese_headline_clause(item)
    combined = _combined_news_text(item)
    if any(keyword in combined for keyword in ("launch", "unveil", "发布", "推出", "更新", "上线")) and not any(
        word in value for word in ("发布", "推出", "更新", "上线", "升级")
    ):
        value = _trim_headline_clause(f"{value}更新", 24)
    if any(keyword in combined for keyword in ("funding", "raises", "融资", "获投", "投资")) and not any(
        word in value for word in ("融资", "获投", "升温", "获投")
    ):
        value = _trim_headline_clause(f"{value}获投", 24)
    if any(keyword in combined for keyword in ("robot", "factory", "manufacturing", "工厂", "机器人", "制造", "产线")) and not any(
        word in value for word in ("落地", "量产", "交付", "推进", "升级")
    ):
        value = _trim_headline_clause(f"{value}推进", 24)
    return value


def _generic_chinese_headline_clause(item: dict[str, Any]) -> str:
    return fallback_headline_clause(item)


def _generic_chinese_title_element(item: dict[str, Any]) -> str:
    return fallback_title_element(item)


def _generic_chinese_detail_heading(item: dict[str, Any]) -> str:
    return fallback_detail_heading(item)


def _headline_dedupe_key(text: str) -> str:
    return re.sub(r"[\s:：，,；;、。丨|]+", "", text).lower()[:18]


def _news_title_element(item: dict[str, Any]) -> str:
    title = str(item.get("title", "")).strip()
    summary = _strip_leading_news_byline(str(item.get("summary", "")).strip())

    model_heading = _model_detail_heading(item)
    if model_heading:
        return model_heading

    if _needs_chinese_fallback(title or summary):
        return _generic_chinese_title_element(item)
    return _shorten_title_element(_clean_title_element(title or summary), 9)


def _news_detail_heading(item: dict[str, Any]) -> str:
    model_heading = _model_detail_heading(item)
    if model_heading:
        return model_heading
    title = str(item.get("title", "")).strip()
    summary = str(item.get("summary", "")).strip()
    title_label = _clean_detail_heading(title)
    if _is_useful_detail_heading(title_label) and not _needs_chinese_fallback(title_label):
        return title_label
    summary_label = _clean_detail_heading(summary)
    if _is_useful_detail_heading(summary_label) and not _needs_chinese_fallback(summary_label):
        return summary_label
    if _needs_chinese_fallback(title or summary):
        return _generic_chinese_detail_heading(item)
    return _expand_short_news_label(_news_title_element(item), item)


def _clean_detail_heading(text: str) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    value = _strip_leading_news_byline(value)
    value = _strip_leading_news_dateline(value)
    value = re.sub(r"^(硬氪独家|焦点分析|最前线|36氪首发|早报|晚报)\s*[|丨｜:：-]\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(作者|文|记者|编辑)\s*[|丨｜:：-].*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:刚刚|消息称|报道称)\s*[，,:：]?\s*", "", value)
    if not value:
        return ""
    value = _clean_title_element(value)
    value = re.sub(r"\s+", " ", value).strip(" 　:：。；;，,")
    if not value:
        return ""
    return _complete_heading_within_limit(value, 72)


def _is_useful_detail_heading(text: str) -> bool:
    value = text.strip()
    if len(value) < 10:
        return False
    if re.fullmatch(r"[A-Za-z0-9 ._\-]+", value) and len(value.split()) <= 3:
        return False
    return True


def _expand_short_news_label(label: str, item: dict[str, Any]) -> str:
    source = str(item.get("source_name", "")).strip()
    if source and label:
        return _shorten_title_element(f"{source}报道{label}，核心看点是落地场景和使用门槛", 42)
    if label:
        return _shorten_title_element(f"{label}的变化会影响产品入口、成本结构或工作流程", 42)
    return "这条科技新闻背后，值得继续看产品、资本和产业分工怎么变化"


def _clean_title_element(text: str) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    value = re.sub(r"^[#\-\s]+", "", value)
    for separator in ("|", "丨"):
        if separator in value:
            first = value.split(separator, 1)[0].strip()
            if len(first) >= 3:
                value = first
                break
    value = re.sub(r"\b(Breaking|Exclusive|Report|Analysis)\b", "", value, flags=re.IGNORECASE).strip()
    return value or "科技热点"


def _shorten_title_element(text: str, max_length: int) -> str:
    value = text.strip()
    if len(value) <= max_length:
        return value
    return value[:max_length]


def _digest_for_seed(topic: str, seed: dict[str, Any]) -> str:
    news_items = seed.get("news_items", [])
    if isinstance(news_items, list) and news_items:
        if _source_policy(seed) == "smart_manufacturing_daily":
            return "每日智能制造产业资讯速递。"
        target_date = str(seed.get("target_date", "")).strip()
        date_label = f"{target_date} " if target_date else ""
        return f"这篇主文章基于 {date_label}可核验公开来源，帮你快速看懂昨天 AI 圈真正值得关注的变化。"
    return _digest_for_topic(topic)


def _digest_for_topic(topic: str) -> str:
    if "智能体" in topic:
        return "AI 正在从聊天工具变成能执行任务的智能体。普通人不必懂复杂技术，但要学会把目标、流程和边界说清楚。"
    return f"这篇文章用新手能听懂的方式解释“{topic}”，重点说清它是什么、为什么重要，以及普通人可以怎么用。"


def _wechat_header_html(article: dict[str, Any]) -> str:
    title = html.escape(str(article["title"]))
    digest = html.escape(str(article["digest"]))
    column = html.escape(str(article.get("column", "")).strip())
    target_date = html.escape(str(article.get("target_date", "")).strip())
    meta_items = [item for item in (column, target_date) if item]
    meta = " · ".join(meta_items)
    meta_html = (
        f'<p style="margin:0 0 12px 0;color:#6b7280;font-size:13px;line-height:1.6;">{meta}</p>'
        if meta
        else ""
    )
    return "\n".join(
        [
            '<section style="margin:0 0 18px 0;padding:22px 18px 18px 18px;border-radius:8px;background:#f7f9fc;border:1px solid #e6ecf5;">',
            f'<p style="display:inline-block;margin:0 0 10px 0;padding:3px 9px;border-radius:999px;background:#e0f2fe;color:#075985;font-size:12px;line-height:1.5;font-weight:800;">{column or "AI 自动化编辑"}</p>',
            f'<h1 style="margin:0 0 12px 0;font-size:23px;line-height:1.35;color:#111827;font-weight:900;">{title}</h1>',
            meta_html,
            f'<p style="margin:0;padding:12px 0 0 0;border-top:1px solid #dbe4f0;color:#374151;font-size:15px;line-height:1.8;"><strong>{digest}</strong></p>',
            "</section>",
        ]
    )


def _wechat_illustration_html(article: dict[str, Any]) -> str:
    placeholder = str(article.get("illustration_placeholder", "")).strip()
    if not placeholder:
        return ""
    alt = html.escape(str(article.get("illustration_alt", "AI 主题配图")).strip())
    return "\n".join(
        [
            '<section style="margin:0 0 18px 0;">',
            f'<img src="{html.escape(placeholder)}" alt="{alt}" style="display:block;width:100%;height:auto;border-radius:8px;border:1px solid #e5e7eb;" />',
            '<p style="margin:7px 0 0 0;color:#94a3b8;font-size:12px;line-height:1.5;text-align:center;">自动生成配图 · 用于辅助理解，不代表新闻原图</p>',
            "</section>",
        ]
    )


def _wechat_insight_card_html(article: dict[str, Any]) -> str:
    topic = html.escape(str(article.get("topic", "AI 自动化")).strip())
    if _is_daily_practical_takeaway(article):
        text = "这篇不复盘新闻，只解决一个小问题：把要交给 AI 的任务说清楚，让结果能被检查、能被复用。"
    elif str(article.get("target_date", "")).strip():
        text = "今天不用追全网热搜，先抓三层信息：哪些事值得看，它们共同说明什么，普通人能立刻调整哪一步。"
    elif "智能体" in str(article.get("topic", "")):
        text = "理解智能体，不是背一个新概念，而是学会把模糊目标拆成可执行、可检查、可交付的任务。"
    else:
        text = f"这篇文章围绕“{topic}”做白话整理：少讲术语，多讲它和普通人的工作、学习、内容生产有什么关系。"
    return "\n".join(
        [
            '<section style="margin:0 0 18px 0;padding:15px 15px;border-radius:8px;background:#fff7ed;border:1px solid #fed7aa;">',
            '<p style="margin:0 0 6px 0;color:#c2410c;font-size:13px;font-weight:900;">先给你一句话</p>',
            f'<p style="margin:0;color:#7c2d12;font-size:15px;line-height:1.8;">{html.escape(text)}</p>',
            "</section>",
        ]
    )


def _wechat_reading_map_html(article: dict[str, Any]) -> str:
    if _is_daily_practical_takeaway(article):
        items = ("四行模板", "一个例子", "一套检查标准")
    elif str(article.get("target_date", "")).strip():
        items = ("8 条消息", "3 个方向", "1 个行动清单")
    else:
        items = ("一个概念", "一个例子", "一套做法")
    item_html = "".join(
        f'<span style="display:inline-block;margin:0 6px 8px 0;padding:5px 9px;border-radius:999px;background:#ecfdf5;color:#047857;font-size:12px;line-height:1.5;font-weight:700;">{html.escape(item)}</span>'
        for item in items
    )
    return "\n".join(
        [
            '<section style="margin:0 0 22px 0;padding:13px 14px;border-radius:8px;background:#f8fafc;border:1px solid #e5e7eb;">',
            '<p style="margin:0 0 8px 0;color:#111827;font-size:14px;font-weight:900;">这一篇你会读到</p>',
            f'<p style="margin:0;">{item_html}</p>',
            "</section>",
        ]
    )


def _wechat_section_heading_html(heading: str) -> str:
    if heading in {"AI 进入真实工作流", "AI 公司开始接受账本考验", "AI 的入口从云端扩到电脑和平台", "其他重要动态"}:
        return "\n".join(
            [
                '<section style="margin:30px 0 14px 0;padding:12px 13px;border-radius:8px;background:#111827;">',
                f'<h2 style="margin:0;font-size:18px;line-height:1.45;color:#ffffff;font-weight:900;">{html.escape(heading)}</h2>',
                "</section>",
            ]
        )
    return "\n".join(
        [
            '<section style="margin:30px 0 14px 0;padding:0 0 0 10px;border-left:4px solid #2563eb;">',
            f'<h2 style="margin:0;font-size:18px;line-height:1.45;color:#111827;font-weight:900;">{html.escape(heading)}</h2>',
            "</section>",
        ]
    )


def _wechat_paragraph_html(paragraph: str) -> str:
    numbered = re.match(r"^(\d+)\.\s*(.+)$", paragraph.strip())
    if numbered:
        return _wechat_numbered_item_html(numbered.group(1), numbered.group(2))
    point = re.match(r"^(第一|第二|第三|其次|最后)，(.+)$", paragraph.strip())
    if point:
        return _wechat_point_item_html(point.group(1), point.group(2))
    return (
        '<p style="margin:0 0 15px 0;color:#374151;font-size:16px;line-height:1.92;">'
        f"{html.escape(paragraph)}"
        "</p>"
    )


def _wechat_numbered_item_html(number: str, text: str) -> str:
    return "\n".join(
        [
            '<section style="margin:0 0 12px 0;padding:14px 14px;border-radius:8px;background:#ffffff;border:1px solid #e5e7eb;">',
            f'<p style="margin:0;color:#374151;font-size:15px;line-height:1.85;"><span style="display:inline-block;width:27px;height:27px;margin:0 8px 4px 0;border-radius:50%;background:#2563eb;color:#ffffff;text-align:center;font-size:14px;line-height:27px;font-weight:800;">{html.escape(number)}</span>{html.escape(text)}</p>',
            "</section>",
        ]
    )


def _wechat_point_item_html(label: str, text: str) -> str:
    return "\n".join(
        [
            '<section style="margin:0 0 12px 0;padding:13px 14px;border-radius:8px;background:#f8fafc;border:1px solid #e5e7eb;">',
            f'<p style="margin:0;color:#374151;font-size:15px;line-height:1.85;"><span style="display:inline-block;margin:0 8px 4px 0;padding:2px 7px;border-radius:999px;background:#dbeafe;color:#1d4ed8;font-size:12px;line-height:1.5;font-weight:800;">{html.escape(label)}</span>{html.escape(text)}</p>',
            "</section>",
        ]
    )


def _wechat_action_card_html(article: dict[str, Any]) -> str:
    if _is_daily_practical_takeaway(article):
        text = "选一个十分钟以内的小任务，写下输入、步骤、边界和检查标准。先跑一遍，再把最容易出错的地方补进提示词。"
    elif str(article.get("target_date", "")).strip():
        text = "挑一条和你工作相关的 AI 新闻，写下三句话：它能替你省哪一步、可能在哪一步出错、结果要怎么检查。"
    elif "智能体" in str(article.get("topic", "")):
        text = "把一个重复任务写成三行：目标、步骤、检查标准。能写清这三行，你就已经在用智能体思维了。"
    else:
        topic = str(article.get("topic", "今天的 AI 变化")).strip()
        text = f"把“{topic}”相关的一件重复工作写成三行：目标、步骤、检查标准。明天再看它能不能被 AI 自动化一半。"
    return "\n".join(
        [
            '<section style="margin:24px 0 4px 0;padding:15px 15px;border-radius:8px;background:#eff6ff;border:1px solid #bfdbfe;">',
            '<p style="margin:0 0 7px 0;color:#1d4ed8;font-size:14px;font-weight:900;">今天可以马上试试</p>',
            f'<p style="margin:0;color:#1e3a8a;font-size:15px;line-height:1.8;">{html.escape(text)}</p>',
            "</section>",
        ]
    )


def _wechat_sources_html(
    sources: list[dict[str, Any]],
    *,
    news_items: list[dict[str, Any]] | None = None,
) -> str:
    names = _visible_source_names(sources, news_items=news_items)
    if not names:
        return ""
    if len(names) == 1 and names[0] in {"本地选题配置", "昨日讨论记录"}:
        title = "主题来源"
        body = "本文为解释型副文章，主题来自前一天讨论和本地选题配置；文中不把单一事实包装成新闻结论。"
    else:
        title = "参考来源"
        visible_names = "、".join(names)
        body = f"本文参考 {visible_names}的公开报道整理。"
    parts = [
        '<section style="margin:30px 0 0 0;padding:14px 14px;border-radius:8px;background:#f8fafc;border:1px solid #e5e7eb;">',
        f'<p style="margin:0 0 10px 0;color:#111827;font-size:15px;font-weight:800;">{html.escape(title)}</p>',
        '<p style="margin:0;color:#6b7280;font-size:13px;line-height:1.7;">'
        f"{html.escape(body)}"
        "</p>",
    ]
    parts.append("</section>")
    return "\n".join(parts)


def _source_names(sources: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for source in sources:
        if _source_is_promotional(source):
            continue
        raw_name = str(source.get("name", "")).strip()
        if not raw_name:
            continue
        source_name = raw_name.split("：", 1)[0].split(":", 1)[0].strip()
        if source_name and source_name not in names:
            names.append(source_name)
    return names


def _visible_source_names(
    sources: list[dict[str, Any]],
    *,
    news_items: list[dict[str, Any]] | None,
) -> list[str]:
    if isinstance(news_items, list) and news_items:
        names: list[str] = []
        for item in news_items:
            if not isinstance(item, dict) or _is_promotional_news_item(item):
                continue
            source_name = str(item.get("source_name", "")).strip()
            if source_name and source_name not in names:
                names.append(source_name)
        if names:
            return names
    return _source_names(sources)


def _sections_for_seed(topic: str, seed: dict[str, Any]) -> list[dict[str, Any]]:
    news_items = seed.get("news_items", [])
    if isinstance(news_items, list) and news_items:
        if _source_policy(seed) == "daily_practical_takeaway":
            return _daily_practical_takeaway_sections(topic, news_items)
        if _source_policy(seed) == "single_news_extension":
            return _single_news_extension_sections(topic, news_items[0])
        if _source_policy(seed) == "smart_manufacturing_daily":
            return _smart_manufacturing_daily_sections(topic, news_items)
        return _news_sections(topic, news_items)
    return _sections_for_topic(topic)


def _sections_for_topic(topic: str) -> list[dict[str, Any]]:
    return [
        {
            "heading": "先把话说白",
            "paragraphs": [
                _topic_opening_line(topic),
                "很多 AI 新闻看起来热闹，其实可以用一个朴素标准判断：它能不能减少重复操作，能不能帮你更快理解信息，能不能让普通人完成过去需要很多工具和经验才能完成的事。",
            ],
        },
        {
            "heading": "智能体到底多了什么",
            "paragraphs": [
                "过去大家使用 AI，更多是问一句、答一句。让它改邮件、总结资料、写标题，这当然有用，但它更像一个随叫随到的助手。",
                "智能体更进一步：它会围绕一个目标连续推进。比如先理解你要什么，再拆步骤，再调用工具，最后把结果整理出来并提醒你哪里需要检查。",
                "差别不在于它说得更像人，而在于它开始接近“帮你做完一段流程”。这也是普通人最值得关注的地方。",
            ],
        },
        {
            "heading": "为什么普通人也该关注",
            "paragraphs": [
                "第一，很多软件操作会变得更像“指挥任务”。你不一定要记住每个按钮在哪里，但要学会把目标、限制和验收标准说清楚。",
                "第二，学习重点会从追新工具，变成整理自己的固定流程。只要一件事你每周反复做，就值得拆成步骤，交给 AI 先做初稿或预处理。",
                "第三，判断力会更重要。AI 可以帮你省时间，但内容是否准确、语气是否合适、是否适合发布，仍然要由人来拍板。",
            ],
        },
        {
            "heading": "一个接地气的例子",
            "paragraphs": [
                "就拿公众号运营来说，单独让 AI 写文章，只是解决了写作的一小段。真正耗时间的，还有选题、资料、标题、封面、排版、审核、发布和复盘。",
                "如果把这些步骤连成流水线，事情就变了：每天先选题，再生成草稿，再做风险检查，再根据发布开关决定是保存报告、创建草稿，还是等待人工确认。",
                "这不是为了炫技，而是为了把每天都要重复的流程稳定下来。稳定之后，人才有精力去判断内容方向，而不是被琐碎步骤拖住。",
            ],
        },
        {
            "heading": "新手现在怎么做",
            "paragraphs": [
                "先别急着追每个新模型。更好的起点，是找一个真实重复任务，把它写成清晰流程：输入是什么，输出是什么，哪些内容不能碰，最后怎么检查。",
                "其次，给 AI 留检查点。比如文章生成后，检查标题是否夸张、事实是否有来源、图片是否合规、是否触碰敏感主题。",
                "最后，把每次运行结果记录下来。只有知道系统每天跑了什么、哪里被阻塞、哪里失败，后续才谈得上真正自动化。",
            ],
        },
        {
            "heading": "最后提醒",
            "paragraphs": [
                "AI 自动化不是一上来就完全放手。比较稳妥的路线，是先生成内容和报告，再进入草稿、人工确认，最后才是低风险自动发布。",
                "真正有价值的变化，不是人完全不工作了，而是把重复步骤交出去，把人的注意力留给判断、选择和最终负责。",
            ],
        },
    ]


def _cover_prompt(topic: str) -> str:
    return f"明亮现代的 AI 主题微信公众号封面，主题是“{topic}”，包含抽象数字助手、任务清单和流程节点，清晰、友好、科技感适中。"


def _topic_opening_line(topic: str) -> str:
    if "智能体" in topic:
        return "如果只用一句话解释，智能体就是能按目标连续做事的 AI：它不只回答问题，还会拆步骤、调用工具、整理结果。"
    if "多模态" in topic:
        return "如果只用一句话解释，多模态就是让 AI 同时理解文字、图片、语音等信息，离真实工作场景更近一步。"
    if "自动化" in topic:
        return "如果只用一句话解释，AI 自动化就是把重复任务拆成流程，让 AI 先完成初稿、整理和检查。"
    return f"如果只用一句话解释，“{topic}”不是离普通人很远的技术名词，而是一种正在进入日常工作和学习的方法。"


def _single_news_extension_sections(topic: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    title = _reader_news_title(item) or topic
    source = str(item.get("source_name", "")).strip()
    source_part = f"来自 {source} 的消息" if source else "公开来源的消息"
    plain_line = _plain_chinese_news_line(item)
    first_line = _strip_plain_news_intro(plain_line, title=title, source=source)
    return [
        {
            "heading": "这件事的来龙去脉",
            "paragraphs": [
                f"{source_part}提到：“{title}”。{first_line or _clean_news_summary_for_reader(item)}",
                "早报里它可能只是一条信息，但单独展开看，它背后通常有两个问题：这件事改变了谁的工作方式，以及它会不会变成更多产品和公司的默认动作。",
            ],
        },
        {
            "heading": "为什么值得单独展开",
            "paragraphs": [
                plain_line,
                "判断一条科技新闻有没有后劲，不只看它是不是热闹，而要看它是不是进入了真实场景：企业流程、个人设备、内容生产、开发工具、医疗服务、能源系统，或者资本市场的账本。",
            ],
        },
        {
            "heading": "读者需要留意什么",
            "paragraphs": [
                "第一，看它替人省掉的是哪一步。如果只是多一个入口，影响可能有限；如果它能接管一段重复流程，就值得持续关注。",
                "第二，看它的风险边界有没有被说清楚。越接近钱、健康、法律、隐私和关键基础设施，越不能只看效率，也要看审核、责任和人工把关。",
                "第三，看它会不会被平台化。只要进入云平台、操作系统、办公软件或行业软件，这类能力就可能从新闻变成日常工具。",
            ],
        },
        {
            "heading": "今天可以顺手做的小动作",
            "paragraphs": [
                "把这条新闻和自己的工作放在一起想三句话：它可能替我省哪一步，它可能在哪一步出错，我需要用什么标准检查结果。",
                "如果三句话能写清楚，这条新闻对你就不只是行业动态，而是一个可以观察、试用或避坑的具体线索。",
            ],
        },
    ]


def _strip_plain_news_intro(line: str, *, title: str, source: str) -> str:
    value = line.strip()
    intro_options = []
    if source:
        intro_options.extend(
            [
                f"来自 {source} 的消息提到“{title}”。",
                f"来自 {source} 的消息提到：“{title}”。",
                f"来自 {source} 的消息“{title}”。",
                f"来自 {source} 的消息：“{title}”。",
            ]
        )
    intro_options.extend(
        [
            f"公开来源提到“{title}”。",
            f"公开来源提到：“{title}”。",
            f"公开来源的消息“{title}”。",
            f"公开来源的消息：“{title}”。",
        ]
    )
    for intro in intro_options:
        if value.startswith(intro):
            return value[len(intro) :].strip()
    return value


def _daily_practical_takeaway_sections(topic: str, news_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "heading": "这篇不复盘新闻",
            "paragraphs": [
                "前面的早报已经负责告诉你今天发生了什么，这篇只做一件更小的事：给你一个可以直接拿去用的 AI 任务模板。",
                "很多人用 AI 的问题，不是工具不够新，而是任务没有说清楚。你只要把输入、步骤、边界和检查标准写明白，AI 的结果就会从“看起来很顺”变成“真的能复核”。",
            ],
        },
        {
            "heading": "先挑一种适合交给 AI 的任务",
            "paragraphs": [
                "最适合先交给 AI 的，不是你完全不会做的事，而是你已经会做、只是每次都要花时间整理的小任务。",
                "它通常有三个特征：材料比较明确，输出格式比较固定，最后结果你能一眼检查。比如整理会议纪要、改写一段文案、把聊天记录拆成待办、给文章列提纲、把一组链接整理成摘要。",
                "如果一件事需要高责任判断，或者你自己也不知道正确答案长什么样，就先不要交给 AI 独立完成，只让它做资料整理和草稿。",
            ],
        },
        {
            "heading": "四行任务模板",
            "paragraphs": [
                "第一行写输入：我要给 AI 什么材料。可以是链接、文章、聊天记录、表格、录音转写，也可以是一段背景说明。",
                "第二行写步骤：先筛掉什么，再整理什么，最后输出成什么格式。步骤不用复杂，三到五步就够。",
                "第三行写边界：哪些内容不能编，哪些结论必须标注不确定，哪些地方不能给建议，只能提醒复核。",
                "第四行写检查标准：结果里必须有什么，不能有什么，你准备用什么方式快速验收。",
            ],
        },
        {
            "heading": "拿公众号运营举个例子",
            "paragraphs": [
                "输入：我给你 5 条新闻链接和每条新闻的摘要，请只使用这些材料，不要补充材料外的信息。",
                "步骤：先提取每条新闻的一句话重点，再按读者最关心的顺序排序，最后输出 5 条适合公众号早报的短摘要。",
                "边界：不确定的事实必须标注“需复核”；不要夸大产品能力；不要写成广告口吻；不要使用读者难懂的技术术语。",
                "检查标准：每条摘要必须说清“谁做了什么”和“为什么值得看”；每条不超过 80 字；输出后再列出 3 个最需要人工复核的点。",
            ],
        },
        {
            "heading": "可以直接复制这一段",
            "paragraphs": [
                "请根据我提供的材料完成任务。第一，只使用我给你的材料，不要编造事实。第二，先列出处理步骤，再输出结果。第三，遇到不确定内容请标注“需复核”。第四，输出完成后，请按我的检查标准自查一遍，并列出最需要人工确认的 3 个点。",
                "你可以把这段话放在任何任务前面，再补上自己的材料、输出格式和检查标准。它不高级，但很管用，因为它把 AI 最容易出错的地方提前框住了。",
            ],
        },
        {
            "heading": "常见错误",
            "paragraphs": [
                "第一个错误，是只说“帮我整理一下”。这句话太空，AI 不知道你要摘要、清单、表格，还是行动建议。",
                "第二个错误，是不给边界。只要没有说“不要编造、哪些内容需复核、哪些结论不能下”，AI 就容易把语气写得很确定。",
                "第三个错误，是没有检查标准。你越清楚什么算合格，AI 越容易产出能用的初稿，而不是一段漂亮但没法交付的文字。",
            ],
        },
        {
            "heading": "今天就做一个小实验",
            "paragraphs": [
                "选一个十分钟以内的小任务，把它写成这四行，然后交给 AI 跑一遍。不要追求一次完美，只看两个结果：它省了哪一步，它错在了哪一步。",
                "如果确实省时间，就把这段提示词保存下来；如果经常出错，就把边界和检查标准写得更具体。普通人用 AI 的进步，往往不是换了一个新工具，而是把一个小流程调顺了。",
            ],
        },
    ]


def _smart_manufacturing_daily_sections(topic: str, news_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    top_items = _briefing_news_items(news_items)[:8]
    grouped_sections = _smart_manufacturing_grouped_news_sections(top_items)
    return [
        _dynamic_briefing_intro(top_items, smart_manufacturing=True),
        *grouped_sections,
        {
            "heading": "发布前提醒",
            "paragraphs": [
                "本文只做公开来源的白话整理，不把单条新闻包装成确定投资结论。",
                "涉及量产时间、项目落地、融资和市场规模预测时，正式引用前仍应回看原始来源和企业公告。",
            ],
        },
    ]


def _smart_manufacturing_grouped_news_sections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = [
        ("机器人与自动化", ("robot", "robotics", "embodied intelligence", "机器人", "人形", "具身", "自动化", "产线")),
        ("低空经济与交通装备", ("evtol", "低空", "飞行器", "自动驾驶", "航空", "卫星", "satellite", "space", "smart driving", "electric vehicle", "battery output", "production")),
        ("智能工厂与工业AI", ("ai factory", "ai工厂", "智能工厂", "制造", "工业", "isaac", "gr00t", "数字孪生")),
        ("芯片、算力与供应链", ("chip", "gpu", "芯片", "算力", "数据中心", "半导体", "供应链")),
    ]
    used: set[int] = set()
    sections: list[dict[str, Any]] = []
    for heading, keywords in groups:
        paragraphs = []
        for index, item in enumerate(items):
            if index in used:
                continue
            combined = _combined_news_text(item)
            if any(keyword in combined for keyword in keywords):
                paragraphs.append(f"{len(paragraphs) + 1}. {_plain_chinese_news_line(item)}")
                used.add(index)
        if paragraphs:
            sections.append({"heading": heading, "paragraphs": paragraphs})

    remaining_items = [item for index, item in enumerate(items) if index not in used]
    remaining = [f"{index + 1}. {_plain_chinese_news_line(item)}" for index, item in enumerate(remaining_items)]
    if remaining:
        sections.insert(0, {"heading": "其他产业动态", "paragraphs": remaining})
    return sections


def _news_sections(topic: str, news_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    top_items = _briefing_news_items(news_items)[:8]
    grouped_sections = _grouped_news_sections(top_items)

    return [
        _dynamic_briefing_intro(top_items, smart_manufacturing=False),
        *grouped_sections,
        {
            "heading": "发布前提醒",
            "paragraphs": [
                "本文只做公开来源的白话整理，不把单条新闻包装成确定结论。",
                "AI 行业变化很快，涉及产品能力、价格、开放范围和地区可用性时，正式使用前应再看一次原始来源。",
            ],
        },
    ]


def _dynamic_briefing_intro(
    news_items: list[dict[str, Any]],
    *,
    smart_manufacturing: bool,
) -> dict[str, Any]:
    lenses = _briefing_lens_counts(news_items)
    top_lenses = [
        name
        for name, count in sorted(lenses.items(), key=lambda row: (-row[1], row[0]))
        if count
    ][:3]
    concrete = []
    for item in news_items:
        heading = _news_detail_heading(item)
        if heading and _specific_headline_clause(heading) and heading not in concrete:
            concrete.append(heading)
        if len(concrete) >= 2:
            break
    if smart_manufacturing:
        heading = "先说结论：今天的产业变化落在具体项目上"
        lens_text = "、".join(top_lenses or ["产业落地", "交付验证"])
        first = f"今天的智能制造线索主要集中在{lens_text}。判断价值时，先看客户、产线、订单、量产时间和供应链动作。"
    else:
        heading = "先说结论：今天先看具体变化"
        lens_text = "、".join(top_lenses or ["产品入口", "基础设施", "真实场景"])
        first = f"昨天的科技新闻主要集中在{lens_text}。这篇只保留能说清主体、动作和来源的变化。"
    second = "；".join(concrete)
    if second:
        second = f"两条最具体的线索是：{second}。"
    else:
        second = "当天信息较分散，正文按事实完整度和来源质量排序。"
    return {"heading": heading, "paragraphs": [first, second]}


def _grouped_news_sections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for heading, grouped_items in _briefing_grouped_items(items):
        paragraphs = [
            f"{index}. {_plain_chinese_news_line(item)}"
            for index, item in enumerate(grouped_items, start=1)
        ]
        sections.append({"heading": heading, "paragraphs": paragraphs})
    return sections


def _plain_chinese_news_line(item: dict[str, Any]) -> str:
    title = _reader_news_title(item)
    source = str(item.get("source_name", "")).strip()
    combined = _combined_news_text(item)
    source_part = f"来自 {source} 的消息提到" if source else "公开来源提到"
    cleaned_summary = _clean_news_summary_for_reader(item)
    if cleaned_summary:
        return f"{source_part}“{title}”。{cleaned_summary}"
    return f"{source_part}“{title}”。{_generic_news_line_tail(item, combined)}"


def _clean_news_summary_for_reader(item: dict[str, Any]) -> str:
    model_summary = _model_reader_summary(item)
    if model_summary:
        return model_summary
    summary = str(item.get("summary", "")).strip()
    title = str(item.get("title", "")).strip()
    if not summary or summary.lower() == title.lower():
        return ""
    value = _strip_leading_news_byline(summary)
    value = _strip_leading_news_dateline(value)
    value = _strip_promotional_tail(value)
    value = re.sub(r"\s+", " ", value).strip(" 　，,")
    if not value:
        return ""
    result = _complete_sentences_within_limit(value, max_length=220, target_length=90)
    if not result:
        return ""
    if _needs_chinese_fallback(result):
        return ""
    return result


def _strip_leading_news_byline(summary: str) -> str:
    value = summary.strip()
    dateline = r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+\d{2}:\d{2})?|(?:\d{1,2}月\d{1,2}日))\s*[；;，,、]\s*"
    surname = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元顾孟平黄穆萧尹姚邵汪祁毛米贝明伏成戴宋庞熊纪舒屈项祝董梁杜阮蓝闵季麻强贾路危江童颜郭梅林钟徐邱骆高夏蔡田胡凌霍虞万柯管卢莫房解应宗丁宣邓杭洪包左石崔龚程邢裴陆翁荀惠曲封储段富焦巴谷车侯全班秋仲伊宫宁仇甘祖武符刘景龙叶白蒲赖卓池乔闻党谭劳申冉雍桂牛边尚农温庄柴瞿阎慕连习艾鱼古易戈廖终居衡步都耿满弘匡寇广沃利越师巩聂晁辛阚简饶曾关相查游权盖益桓公"
    compound_surname = "欧阳|司马|上官|诸葛|东方|令狐|夏侯|尉迟|皇甫|公孙|长孙|慕容|司徒|司空"
    name_token = rf"(?!(?:作者|文|记者|编辑)(?:\s|[|丨｜:：]|$))(?:(?:{compound_surname})[\u4e00-\u9fff]{{1,2}}|[{surname}][\u4e00-\u9fff]{{1,2}}|[A-Za-z·.\-]{{2,24}})"
    byline = (
        rf"^(?:作者|文|记者|编辑)\s*[|丨｜:：]\s*{name_token}"
        rf"(?:\s+{name_token}){{0,3}}"
    )
    for _ in range(8):
        stripped = re.sub(dateline, "", value).strip(" 　，,。；;")
        stripped = re.sub(byline, "", stripped).strip(" 　，,。；;")
        if stripped == value:
            break
        value = stripped
    return value


def _strip_leading_news_dateline(text: str) -> str:
    value = str(text).strip()
    if not value:
        return ""
    source_prefix = (
        r"^(?:据\s*)?"
        r"(?:[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9 .·_\-]{1,30})"
        r"\s*(?:\d{1,2}\s*月\s*\d{1,2}\s*日\s*)?"
        r"(?:消息|报道|讯)(?:显示|称|指出)?\s*[，,:：]\s*"
    )
    for _ in range(3):
        cleaned = re.sub(source_prefix, "", value, flags=re.IGNORECASE)
        cleaned = re.sub(r"^(?:北京时间\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日\s*[，,:：]\s*", "", cleaned)
        if cleaned == value:
            break
        value = cleaned.strip()
    return value


def _complete_sentences_within_limit(text: str, *, max_length: int, target_length: int) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip()
    if not value:
        return ""
    complete = [match.group(0).strip() for match in re.finditer(r"[^。！？!?]+[。！？!?]", value)]
    selected: list[str] = []
    for sentence in complete:
        if len(sentence) > max_length and not selected:
            return ""
        if sum(len(item) for item in selected) + len(sentence) > max_length:
            break
        selected.append(sentence)
        if sum(len(item) for item in selected) >= target_length:
            break
    return "".join(selected)


def _complete_heading_within_limit(text: str, max_length: int) -> str:
    value = re.sub(r"\s+", " ", str(text)).strip(" 　:：。；;，,")
    if len(value) <= max_length:
        return value
    minimum_cut = max(12, int(max_length * 0.55))
    for separator in ("。", "！", "？", "；", ";", "，", ",", "：", ":"):
        cut = value.rfind(separator, minimum_cut, max_length + 1)
        if cut >= minimum_cut:
            return value[:cut].strip(" 　:：。；;，,")
    return ""


def _strip_promotional_tail(text: str) -> str:
    value = text.strip()
    value = re.sub(r"\s*#?\s*欢迎关注.+$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*更多精彩内容.+$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*官方微信公众号.+$", "", value, flags=re.IGNORECASE)
    return value.strip(" 　，,")


def _generic_news_line_tail(item: dict[str, Any], combined: str) -> str:
    label = news_detail_heading(item)
    topic = f"“{label}”" if label else "这个方向"

    if any(_keyword_in_news_text(combined, keyword) for keyword in ("funding", "financing", "investment", "startup", "融资", "获投", "投资")):
        return f"{topic}更像一张风向图：资本正在押注哪些应用层和基础设施方向，接下来要看产品能否形成真实客户和持续收入。"
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("phone", "mobile", "app", "手机", "终端", "应用")):
        return f"{topic}的核心看点在入口变化：手机和应用一旦接入 AI，变化会直接落到日常使用习惯上，关键是能不能跨应用、跨设备稳定协作。"
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("cloud", "token", "compute", "cpu", "chip", "芯片", "算力", "云")):
        return f"{topic}指向的是落地成本：模型越常用，背后的部署效率、价格和基础设施越会成为硬约束。"
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("robot", "robotics", "机器人", "自动驾驶", "硬件")):
        return f"{topic}说明 AI 正在走出屏幕，进入物流、制造和线下服务场景，真正难点会落在稳定性和单位经济性上。"
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("factory", "manufacturing", "industrial", "evtol", "satellite", "工厂", "制造", "工业", "低空", "卫星")):
        return f"{topic}更像一条产业落地线索：项目地点、交付时间、供应链配套和客户需求是否足够具体，决定它能不能进入真实交付。"
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("model", "open source", "benchmark", "大模型", "开源")):
        return f"{topic}会影响开发者能拿到什么能力、企业能否降低试错成本，也会改变应用层产品迭代速度。"
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("court", "lawsuit", "policy", "safety", "privacy", "governance", "法院", "诉讼", "监管", "安全", "隐私", "治理")):
        return f"{topic}会影响 AI 产品能走多远、怎么上线、出了问题谁负责，是判断行业成熟度的重要信号。"
    if any(_keyword_in_news_text(combined, keyword) for keyword in ("agent", "智能体", "assistant", "workflow")):
        return f"{topic}的重点不只是展示能力，而是把 AI 放进消息、办公、开发或企业流程里，关键是能不能真正接住连续任务。"

    if label:
        return f"{topic}可以拆成三个问题：会进入什么产品，是否有人持续付费，结果能不能被清楚验收。"
    return "真实用户、明确场景和可复核结果，是判断这类新闻能否留下来的关键。三点都不清楚，就只能先按短期热度处理。"


def _reader_news_title(item: dict[str, Any]) -> str:
    title = str(item.get("title", "")).strip()
    if not title:
        return news_detail_heading(item)
    if _needs_chinese_fallback(title):
        return news_detail_heading(item)
    return title


def _briefing_news_items(news_items: Any) -> list[dict[str, Any]]:
    items = [item for item in news_items if isinstance(item, dict)] if isinstance(news_items, list) else []
    display_items = [
        item
        for item in items
        if not _is_newsletter_digest_item(item)
        and not _is_promotional_news_item(item)
    ]
    if len(display_items) <= 1:
        return display_items
    substantive = [item for item in display_items if not _is_low_signal_news_item(item)]
    return substantive if len(substantive) >= 4 else display_items


def _is_smart_manufacturing_item(item: dict[str, Any]) -> bool:
    combined = _combined_news_text(item)
    keywords = (
        "smart manufacturing",
        "manufacturing",
        "industrial",
        "ai factory",
        "factory",
        "robot",
        "robotics",
        "embodied intelligence",
        "battery output",
        "production",
        "smart driving",
        "evtol",
        "satellite",
        "智能制造",
        "智能工厂",
        "工业ai",
        "工业",
        "制造",
        "工厂",
        "产线",
        "机器人",
        "人形",
        "具身",
        "自动化",
        "供应商",
        "零部件",
        "排产",
        "产能",
        "量产",
        "订单",
        "低空",
        "卫星",
        "自动驾驶",
    )
    return any(keyword in combined for keyword in keywords)


def _mostly_ascii(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return ascii_count / len(value) > 0.8


def _needs_chinese_fallback(text: str) -> bool:
    value = re.sub(r"\s+", " ", str(text)).strip()
    if not value:
        return False
    chinese_count = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    ascii_alpha_count = sum(1 for char in value if char.isascii() and char.isalpha())
    if chinese_count >= 4:
        return False
    if ascii_alpha_count < 12:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", value)
    if len(words) >= 3:
        return True
    return _mostly_ascii(value) and len(value) >= 24


def _smart_manufacturing_context_text(item: dict[str, Any]) -> str:
    combined = _combined_news_text(item)
    label = news_detail_heading(item)
    subject = f"“{label}”" if label else "这条消息"
    if any(keyword in combined for keyword in ("ai factory", "ai工厂", "智能工厂", "factory", "工厂", "工业ai", "数字孪生")):
        return f"{subject}的重点在工厂级落地：企业需要的不只是一个模型，而是能把数据、仿真、设备、排产和质检串起来的系统。后续要看它能不能进入真实产线并稳定运行。"
    if any(keyword in combined for keyword in ("surgery", "surgical", "手术", "胆囊", "活体")):
        templates = [
            f"{subject}验证的不是普通动作展示，而是远程操控、双机协同和精细操作能否在高要求任务中保持稳定。后续评价应落到控制延迟、操作精度、故障接管和责任边界。",
            f"{subject}把人形机器人的能力测试推到了更精细的操作场景。产业上更值得核对的是遥操作链路、末端执行精度、异常中止机制，以及专用设备与通用机器人之间的成本差距。",
            f"{subject}说明通用人形平台开始尝试高精度任务，但一次实验不等于可规模部署。重复成功率、远程控制稳定性、消毒适配和人工接管机制，才决定它能否继续进入专业场景。",
        ]
        return templates[_stable_news_variant(item, len(templates))]
    if any(keyword in combined for keyword in ("moe", "video model", "视频模型", "具身视频", "世界模型")):
        templates = [
            f"{subject}落在机器人训练基础设施这一层：视频数据如果能更好地表达动作和物理变化，就可能减少真实设备反复采集的成本。接下来要看模型在不同机器人和场景之间的迁移效果。",
            f"{subject}关注的是机器人如何从视频中学习环境变化与动作结果。开源能降低验证门槛，但真正的产业价值取决于数据质量、训练成本，以及模型能否转成稳定的控制策略。",
            f"{subject}把视频生成能力往具身认知方向推进。判断它是否有用，不能只看演示画面，还要看物理一致性、长时序预测和真实机器人任务上的成功率。",
        ]
        return templates[_stable_news_variant(item, len(templates))]
    if any(keyword in combined for keyword in ("robot", "robotics", "embodied intelligence", "机器人", "人形", "具身", "自动化")):
        templates = [
            f"{subject}要放到具体任务里判断：它负责搬运、巡检、装配、仓储还是服务。任务边界越清楚，稳定性、维护成本和安全要求才越容易被验证。",
            f"{subject}的产业价值取决于能否持续完成同一类任务，而不是单次演示。客户会直接比较节拍、故障率、人工接管频次和整套部署成本。",
            f"{subject}需要继续核对交付条件：工作环境是否固定、末端工具是否成熟、现场人员能否维护，以及停机后有没有可靠的接管方案。",
        ]
        return templates[_stable_news_variant(item, len(templates))]
    if any(keyword in combined for keyword in ("battery output", "production", "factory output")):
        return f"{subject}的重点是制造能力：车型、软件和智能化卖点都要通过产能、良率、供应链和交付节奏兑现。后续要看扩产能否稳定跟上订单。"
    if any(keyword in combined for keyword in ("evtol", "低空", "航空", "飞行器", "自动驾驶")):
        return f"{subject}要放在装备制造和运营链条里看。低空经济不只看试飞和概念，还要看整机制造、适航验证、订单交付和地方配套能不能跟上。"
    if any(keyword in combined for keyword in ("satellite", "卫星", "space", "航天")):
        return f"{subject}指向的是更长的产业链：空间基础设施、地面设备、通信终端和数据应用都需要制造能力支撑。规模预测能否变成真实订单，是判断这条线索的关键。"
    return f"{subject}可以先按产业项目来判断：有没有明确客户、落地地点、量产时间和供应链配套。如果这些信息越来越具体，才说明它具备进入真实交付的条件。"


def _smart_manufacturing_reader_text(item: dict[str, Any]) -> str:
    combined = _combined_news_text(item)
    label = news_title_element(item)
    subject = f"“{label}”" if label else "这条新闻"
    if any(keyword in combined for keyword in ("surgery", "surgical", "手术", "胆囊", "活体")):
        return f"{subject}仍属于研究验证，不能直接等同于临床应用。判断进展时应区分远程操控与自主操作，并关注重复成功率、异常中止和人工接管。"
    if any(keyword in combined for keyword in ("moe", "video model", "视频模型", "具身视频", "世界模型")):
        return f"{subject}开源后，开发者可以更快验证训练方法；但真正有说服力的结果仍是跨场景迁移、物理一致性和真实机器人任务成功率。"
    templates = [
        f"{subject}需要放进产业链位置里看：对应研发、生产、质检、仓储、物流还是售后。环节越具体，新闻的含金量越高。",
        f"{subject}不能只停在概念里。客户、订单、产线、园区和交付时间，才会直接决定项目能不能跑起来。",
        f"{subject}可以拆成产业协作问题：谁提供技术，谁负责制造，谁来采购，最后用什么指标证明效率真的提高。",
        f"{subject}后续要看两条线：一条是技术稳定性，另一条是单位经济性。只有两条都过关，才可能从示范走向规模化。",
    ]
    if any(keyword in combined for keyword in ("融资", "获投", "投资", "ipo")):
        return "涉及资本动作时，先别急着下结论。更有用的是看资金投向研发、产能、销售还是并购，以及项目有没有真实客户和收入线索。"
    return templates[_stable_news_variant(item, len(templates))]


def _is_newsletter_digest_item(item: dict[str, Any]) -> bool:
    title = str(item.get("title", "")).strip().lower()
    digest_prefixes = ("daily digest:", "morning digest:", "newsletter:", "the download:")
    return title.startswith(digest_prefixes)


def _is_promotional_news_item(item: dict[str, Any]) -> bool:
    return _has_promotional_text(
        f"{item.get('title', '')} {item.get('summary', '')} {item.get('source_name', '')}"
    )


def _model_detail_heading(item: dict[str, Any]) -> str:
    value = _clean_detail_heading(str(item.get("detail_heading", "")).strip())
    if len(value) < 10 or len(value) > 72:
        return ""
    if _has_banned_model_phrase(value) or _has_promotional_text(value):
        return ""
    if re.fullmatch(r"[A-Za-z0-9 ._\-]+", value) and len(value.split()) <= 4:
        return ""
    return value


def _model_reader_summary(item: dict[str, Any]) -> str:
    value = str(item.get("reader_summary", "")).strip()
    if not value:
        return ""
    value = _strip_leading_news_dateline(value)
    value = _strip_promotional_tail(value)
    value = re.sub(r"\s+", " ", value).strip(" 　，,")
    if len(value) < 28 or len(value) > 220:
        return ""
    if _has_banned_model_phrase(value) or _has_promotional_text(value):
        return ""
    if not re.search(r"[。！？!?][”’\"']?$", value):
        return ""
    return value


def _has_banned_model_phrase(text: str) -> bool:
    banned = (
        "它值得放进早报",
        "这条消息值得放进今天的科技早报",
        "成为今天科技早报的重点动态",
        "换个角度看",
        "读这类消息时",
        "读这条不用先追术语",
        "先看落地位置",
        "普通读者不用急着判断",
        "它反映了 AI 正在进入更具体的产品、行业和工作流程",
        "AI 正在进入更具体的产品、行业和工作流程",
        "值得关注，因为",
        "可以关注",
        "后续可以关注",
        "后续看",
        "后续看点",
        "后续观察",
        "从概念走向",
        "更值得关注的是",
        "这条消息的重点不是",
        "关键不是又多一个按钮",
        "谁使用、谁付费、谁复核",
    )
    return any(phrase in text for phrase in banned)


def _source_is_promotional(source: dict[str, Any]) -> bool:
    return _has_promotional_text(f"{source.get('name', '')} {source.get('summary', '')}")


def _has_promotional_text(text: str) -> bool:
    combined = str(text).lower()
    if "欢迎关注" in combined and ("公众号" in combined or "微信号" in combined or "微信" in combined):
        return True
    if "更多精彩内容" in combined and ("公众号" in combined or "微信号" in combined or "微信" in combined):
        return True
    promotional_terms = (
        "#欢迎关注",
        "官方微信公众号",
        "微信号：",
        "微信号:",
        "第一时间为您奉上",
    )
    return any(term.lower() in combined for term in promotional_terms)


def _is_low_signal_news_item(item: dict[str, Any]) -> bool:
    combined = _combined_news_text(item)
    low_signal_terms = (
        "applications are now open",
        "applications officially close",
        "确认出席",
        "大赛",
        "正式启动",
    )
    if _is_promotional_news_item(item) or any(term in combined for term in low_signal_terms):
        return True
    title = str(item.get("title", "")).strip()
    summary = str(item.get("summary", "")).strip()
    return bool(title and summary and title.lower() == summary.lower() and len(title) < 80)


def _combined_news_text(item: dict[str, Any]) -> str:
    summary = _strip_leading_news_byline(str(item.get("summary", "")))
    return f"{item.get('title', '')} {summary} {item.get('source_name', '')}".lower()


def _keyword_in_news_text(combined: str, keyword: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", keyword):
        return keyword in combined
    plural_suffix = "" if keyword.endswith("s") else "(?:s|es)?"
    return re.search(
        rf"(?<![a-z0-9]){re.escape(keyword)}{plural_suffix}(?![a-z0-9])",
        combined,
    ) is not None


if __name__ == "__main__":
    raise SystemExit(main())
