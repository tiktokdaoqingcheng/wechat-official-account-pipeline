from __future__ import annotations

import json
import tempfile
import unittest
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.pipeline.content_package import (
    CONTENT_PACKAGE_SCHEMA_VERSION,
    _prepare_article_for_render,
    _is_manufacturing_news_item,
    _replenish_article_from_reserve,
    _recover_article_from_final_review,
    _run_final_review_recovery_cycles,
    _smart_manufacturing_seed,
    _sources_for_selected_items,
    aggregate_package_review,
    build_content_package,
    build_daily_article_seeds,
    validate_content_package,
)
from src.pipeline.content_engine import build_article
from src.pipeline.text_model_enhancer import _parse_json_object
from src.review.publish_policy import decide_publish_action


class ContentPackageTest(unittest.TestCase):
    def test_english_compute_infrastructure_is_manufacturing_news(self) -> None:
        item = {
            "title": "Nscale expands its AI compute stack for new data centers",
            "summary": "The company is adding GPU infrastructure and data-center capacity.",
            "source_name": "Example Technology",
            "url": "https://example.com/compute-stack",
        }

        self.assertTrue(_is_manufacturing_news_item(item))

    def test_tv_production_story_is_not_manufacturing_news(self) -> None:
        item = {
            "title": "电视剧演员署名新规今起施行",
            "summary": "The production rule changes how actors and celebrity names appear in network dramas.",
            "source_name": "文娱新闻",
            "url": "https://entertainment.example/tv-cast-rule",
        }

        self.assertFalse(_is_manufacturing_news_item(item))

    def test_manufacturing_seed_keeps_primary_items_as_recovery_reserve(self) -> None:
        selected = [
            {
                "title": f"制造企业{index}发布机器人产线项目",
                "summary": f"制造企业{index}公布机器人产线项目和交付计划。",
                "source_name": f"制造来源{index}",
                "url": f"https://manufacturing.example/{index}",
            }
            for index in range(1, 5)
        ]
        supplemental = [
            {
                "title": "补位企业开放工业自动化平台",
                "summary": "补位企业开放工业自动化平台，并披露首批工厂接入安排。",
                "source_name": "补位来源",
                "url": "https://primary.example/reserve",
            }
        ]

        seed = _smart_manufacturing_seed(
            {},
            manufacturing_seed={"news_items": selected, "reserve_news_items": [], "news_status": "ok"},
            seed_source="manufacturing.json",
            supplemental_items=supplemental,
            supplemental_source="primary.json",
        )

        self.assertEqual(len(seed["news_items"]), 4)
        self.assertEqual([item["url"] for item in seed["news_items"]], [item["url"] for item in selected])
        self.assertEqual([item["url"] for item in seed["reserve_news_items"]], [supplemental[0]["url"]])

    def test_manufacturing_seed_prefers_items_not_already_selected_for_primary(self) -> None:
        overlapping = {
            "title": "博登智能公布具身机器人订单和交付安排",
            "summary": "博登智能公布具身机器人订单，并介绍首批客户交付安排。",
            "source_name": "产业媒体",
            "url": "https://example.com/shared-robotics",
        }
        independent = [
            {
                "title": f"制造企业{index}公布自动化产线项目",
                "summary": f"制造企业{index}公布自动化产线项目和交付计划。",
                "source_name": f"制造来源{index}",
                "url": f"https://example.com/unique-{index}",
            }
            for index in range(1, 5)
        ]

        seed = _smart_manufacturing_seed(
            {},
            manufacturing_seed={
                "news_items": [overlapping, *independent[:3]],
                "reserve_news_items": [independent[3]],
                "news_status": "ok",
            },
            seed_source="manufacturing.json",
            supplemental_items=[overlapping],
            supplemental_source="primary.json",
        )

        selected_urls = [item["url"] for item in seed["news_items"]]
        self.assertEqual(len(selected_urls), 4)
        self.assertNotIn(overlapping["url"], selected_urls)
        self.assertIn(independent[3]["url"], selected_urls)

    def test_selected_sources_only_include_final_news_items(self) -> None:
        selected = [
            {
                "title": "新闻一",
                "source_name": "来源一",
                "url": "https://example.com/one",
                "summary": "新闻一摘要。",
            }
        ]
        configured = [
            {"name": "来源一：新闻一", "url": "https://example.com/one", "summary": "新闻一摘要。"},
            {"name": "来源二：新闻二", "url": "https://example.com/two", "summary": "新闻二摘要。"},
        ]

        sources = _sources_for_selected_items(selected, configured)

        self.assertEqual([source["url"] for source in sources], ["https://example.com/one"])

    def test_final_review_recovery_drops_platform_item_and_rebuilds_sources(self) -> None:
        items = [
            {
                "title": f"制造企业{index}发布新的机器人项目",
                "source_name": f"来源{index}",
                "url": f"https://example.com/{index}",
                "summary": f"制造企业{index}发布机器人项目，并披露交付安排。",
                "detail_heading": f"制造企业{index}发布机器人项目，交付安排同步披露",
                "reader_summary": f"制造企业{index}已经公布机器人项目和交付安排，相关信息来自公开报道。",
                "fact_card": {
                    "subject": f"制造企业{index}",
                    "action": "发布机器人项目",
                    "confidence": "high",
                    "source_name": f"来源{index}",
                },
            }
            for index in range(1, 5)
        ]
        article = build_article(
            {
                "topic": "智能制造日报",
                "column": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "news_status": "ok",
                "news_items": items,
                "sources": [
                    {
                        "name": f"来源{index}：制造企业{index}发布新的机器人项目",
                        "url": f"https://example.com/{index}",
                        "summary": "公开报道。",
                    }
                    for index in range(1, 5)
                ],
            }
        )
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "high",
            "issues": [
                {
                    "type": "platform_risk",
                    "severity": "high",
                    "news_index": 2,
                    "evidence": items[1]["reader_summary"],
                    "action": "drop_item",
                }
            ],
        }

        repaired, report = _recover_article_from_final_review(article, role="secondary", min_news_items=3)

        self.assertTrue(report["changed"])
        self.assertFalse(report["below_minimum"])
        self.assertEqual(len(repaired["news_items"]), 3)
        self.assertNotIn("https://example.com/2", [source["url"] for source in repaired["sources"]])

    def test_final_review_recovery_drops_medium_english_fragment(self) -> None:
        items = [
            {
                "title": "Researchers build missing infrastructure to move AI between robots",
                "source_name": "Example Robotics Desk",
                "url": "https://example.invalid/researchers",
                "summary": "Robotics researchers built an open-source framework.",
                "detail_heading": "Researchers开源机器人能力",
                "reader_summary": "Researchers开源机器人能力。",
            },
            {
                "title": "甲公司发布工业机器人平台",
                "source_name": "来源甲",
                "url": "https://example.com/a",
                "summary": "甲公司发布工业机器人平台。",
                "detail_heading": "甲公司发布工业机器人平台",
                "reader_summary": "甲公司发布工业机器人平台并披露部署安排。",
            },
            {
                "title": "乙公司建设智能工厂",
                "source_name": "来源乙",
                "url": "https://example.com/b",
                "summary": "乙公司建设智能工厂。",
                "detail_heading": "乙公司建设智能工厂",
                "reader_summary": "乙公司建设智能工厂并公布投产计划。",
            },
            {
                "title": "丙公司上线自动化质检系统",
                "source_name": "来源丙",
                "url": "https://example.com/c",
                "summary": "丙公司上线自动化质检系统。",
                "detail_heading": "丙公司上线自动化质检系统",
                "reader_summary": "丙公司上线自动化质检系统并说明应用产线。",
            },
        ]
        article = build_article(
            {
                "topic": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "news_items": items,
                "sources": [],
            }
        )
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "medium",
            "issues": [
                {
                    "type": "english_fragment",
                    "severity": "medium",
                    "news_index": 1,
                    "evidence": "Researchers开源机器人能力",
                    "action": "repair_field",
                }
            ],
        }

        repaired, report = _recover_article_from_final_review(article, role="secondary", min_news_items=3)

        self.assertTrue(report["changed"])
        self.assertFalse(report["below_minimum"])
        self.assertEqual(len(repaired["news_items"]), 3)
        self.assertNotIn(items[0]["url"], [item["url"] for item in repaired["news_items"]])

    def test_low_keep_issue_does_not_clear_publishable_model_copy(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "科技公司发布企业AI工具",
                        "source_name": "公开来源",
                        "url": "https://example.com/ai-tool",
                        "summary": "科技公司发布企业AI工具，并披露首批应用场景。",
                        "detail_heading": "科技公司发布企业AI工具，首批应用场景同步披露",
                        "reader_summary": "科技公司已经发布企业AI工具，并披露首批应用场景和使用范围。",
                    }
                ],
                "sources": [{"name": "公开来源", "url": "https://example.com/ai-tool", "summary": "公开报道。"}],
            }
        )
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "low",
            "issues": [
                {
                    "type": "ai_cliche",
                    "severity": "low",
                    "news_index": 1,
                    "evidence": "首批应用场景和使用范围",
                    "action": "keep",
                }
            ],
        }

        repaired, report = _recover_article_from_final_review(article, role="primary", min_news_items=1)

        self.assertFalse(report["changed"])
        self.assertEqual(repaired["news_items"][0]["reader_summary"], article["news_items"][0]["reader_summary"])

    def test_advisory_style_issue_keeps_publishable_copy(self) -> None:
        items = [
            {
                "title": f"科技公司{index}公布新的产业项目",
                "source_name": f"来源{index}",
                "url": f"https://example.com/style-{index}",
                "summary": f"科技公司{index}公布新的产业项目，并披露首批客户和交付安排。",
                "detail_heading": f"科技公司{index}公布产业项目，首批客户和交付安排披露",
                "reader_summary": (
                    f"科技公司{index}已经公布新的产业项目，公开资料说明了首批客户、交付安排和应用范围。"
                ),
            }
            for index in range(1, 4)
        ]
        article = build_article(
            {
                "topic": "昨天科技与产业动态",
                "column": "科技早报",
                "news_status": "ok",
                "news_items": items,
                "sources": [],
            }
        )
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "medium",
            "issues": [
                {
                    "type": "ai_cliche",
                    "severity": "medium",
                    "news_index": 1,
                    "evidence": items[0]["reader_summary"],
                    "action": "repair_field",
                }
            ],
        }

        repaired, report = _recover_article_from_final_review(article, role="primary", min_news_items=2)

        self.assertFalse(report["changed"])
        self.assertEqual(len(repaired["news_items"]), 3)
        self.assertIn(items[0]["url"], [item["url"] for item in repaired["news_items"]])
        self.assertEqual(report["actions"], [])

    def test_prepare_rebuilds_title_after_sanitizer_drops_bad_item(self) -> None:
        article = build_article(
            {
                "title": "坏新闻要素；好新闻要素丨科技早报",
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "坏新闻要素",
                        "source_name": "来源一",
                        "url": "https://example.com/bad",
                        "summary": "公开信息。",
                        "detail_heading": "这条消息值得放进今天的科技早报",
                        "reader_summary": "公开信息。",
                    },
                    {
                        "title": "好新闻要素正式进入企业工作流",
                        "source_name": "来源二",
                        "url": "https://example.com/good",
                        "summary": "好新闻要素正式进入企业工作流，并披露使用范围。",
                        "detail_heading": "好新闻要素正式进入企业工作流，使用范围同步披露",
                        "reader_summary": "好新闻要素已经进入企业工作流，公开报道同时披露了使用范围和首批场景。",
                    },
                ],
                "sources": [
                    {"name": "来源一", "url": "https://example.com/bad", "summary": "公开报道。"},
                    {"name": "来源二", "url": "https://example.com/good", "summary": "公开报道。"},
                ],
            }
        )

        prepared = _prepare_article_for_render(article, role="primary")

        self.assertEqual(len(prepared["news_items"]), 1)
        self.assertNotIn("坏新闻要素", prepared["title"])
        self.assertIn("好新闻要素", prepared["title"])

    def test_replenishment_fills_deleted_item_from_same_day_reserve(self) -> None:
        current_items = [
            {
                "title": f"科技公司{index}发布企业AI工具",
                "source_name": f"来源{index}",
                "url": f"https://example.com/current-{index}",
                "summary": f"科技公司{index}发布企业AI工具，并披露应用范围。",
                "detail_heading": f"科技公司{index}发布企业AI工具，应用范围同步披露",
                "reader_summary": f"科技公司{index}已经发布企业AI工具，公开信息同时披露了首批应用范围。",
            }
            for index in range(1, 3)
        ]
        reserve_items = [
            {
                "title": f"候选公司{index}开放新的AI能力",
                "source_name": f"候选来源{index}",
                "url": f"https://example.com/reserve-{index}",
                "summary": f"候选公司{index}开放新的AI能力，并公布使用条件。",
                "detail_heading": f"候选公司{index}开放新的AI能力，使用条件同步公布",
                "reader_summary": f"候选公司{index}已经开放新的AI能力，公开报道同时说明了使用条件和适用场景。",
            }
            for index in range(1, 3)
        ]
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "news_status": "ok",
                "news_items": current_items,
                "reserve_news_items": reserve_items,
                "target_news_item_count": 3,
                "sources": [_source for _source in (
                    {"name": "来源1", "url": "https://example.com/current-1", "summary": "公开报道。"},
                    {"name": "来源2", "url": "https://example.com/current-2", "summary": "公开报道。"},
                )],
            }
        )

        replenished, report = _replenish_article_from_reserve(
            article,
            role="primary",
            target_news_items=3,
            text_model_context={"clients": {}, "roles": {}},
        )

        self.assertTrue(report["changed"])
        self.assertEqual(report["added"], 1)
        self.assertEqual(len(replenished["news_items"]), 3)
        self.assertEqual(len(replenished["reserve_news_items"]), 1)
        self.assertIn("候选公司1", replenished["news_items"][-1]["title"])

    def test_content_package_never_lowers_configured_secondary_minimum(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "dry_run"},
            "final_review": {
                "mode": "enforce",
                "auto_repair": True,
                "min_news_items": {"primary": 1, "secondary": 3},
            },
        }
        primary_item = {
            "title": "AI company releases an enterprise assistant",
            "source_name": "Primary Source",
            "url": "https://example.com/primary",
            "summary": "The company released an assistant and documented its enterprise rollout.",
        }
        secondary_items = [
            {
                "title": f"Factory company {index} deploys an industrial robot",
                "source_name": f"Manufacturing Source {index}",
                "url": f"https://example.com/manufacturing-{index}",
                "summary": f"Factory company {index} deployed a robot on a production line.",
            }
            for index in range(1, 3)
        ]
        decision = decide_publish_action(
            schedule,
            {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "src.pipeline.content_package.prepare_text_model_context",
            return_value={"clients": {}, "roles": {}},
        ):
            bundle = build_content_package(
                schedule_config=schedule,
                article_seeds={
                    "primary": {
                        "topic": "Daily technology briefing",
                        "column": "Technology Briefing",
                        "news_status": "ok",
                        "news_items": [primary_item],
                        "sources": [],
                    },
                    "secondary": {
                        "topic": "Smart Manufacturing Daily",
                        "column": "Smart Manufacturing Daily",
                        "news_status": "ok",
                        "news_items": secondary_items,
                        "reserve_news_items": [],
                        "target_news_item_count": 2,
                        "sources": [],
                    },
                },
                safety_config={},
                publish_decision=decision,
                output_dir=temp_dir,
                seed_source="test",
                already_published_today=False,
                generate_images=False,
            )

        replenishment = bundle["articles"]["secondary"]["news_replenishment"]
        self.assertEqual(replenishment["target_news_items"], 3)
        self.assertEqual(replenishment["minimum_news_items"], 3)
        self.assertEqual(replenishment["final_news_items"], 2)
        self.assertTrue(replenishment["below_minimum"])
        self.assertFalse(bundle["package"]["copy_quality"]["articles"]["secondary"]["ok"])

    def test_content_package_writes_both_articles_before_any_paid_review(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "dry_run"},
            "final_review": {"mode": "enforce", "min_news_items": {"primary": 1, "secondary": 1}},
        }

        def seed(role: str) -> dict[str, Any]:
            return {
                "topic": f"{role} topic",
                "column": role,
                "news_status": "ok",
                "target_news_item_count": 1,
                "news_items": [
                    {
                        "title": f"{role} company ships a production AI system",
                        "source_name": "Example News",
                        "url": f"https://example.com/{role}",
                        "summary": "The company published deployment scope and delivery details.",
                        "detail_heading": f"{role} company publishes production deployment details",
                        "reader_summary": "The company published the deployment scope, delivery plan and customer workflow.",
                    }
                ],
                "sources": [
                    {
                        "name": "Example News",
                        "url": f"https://example.com/{role}",
                        "summary": "Public source.",
                    }
                ],
            }

        order: list[str] = []

        def fake_enhance(article, _context):
            order.append(f"write:{article['column']}")
            return article

        def fake_review(article, _context, *, visible_html, mode):
            order.append(f"review:{article['column']}")
            updated = dict(article)
            updated["final_ai_review"] = {
                "mode": mode,
                "status": "applied",
                "risk_level": "low",
                "issues": [],
            }
            return updated

        decision = decide_publish_action(
            schedule,
            {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "src.pipeline.content_package.prepare_text_model_context",
            return_value={"clients": {}, "roles": {}},
        ), patch(
            "src.pipeline.content_package.enhance_article_with_text_models",
            side_effect=fake_enhance,
        ), patch(
            "src.pipeline.content_package.review_final_article_with_text_model",
            side_effect=fake_review,
        ):
            build_content_package(
                schedule_config=schedule,
                article_seeds={"primary": seed("primary"), "secondary": seed("secondary")},
                safety_config={},
                publish_decision=decision,
                output_dir=temp_dir,
                seed_source="test",
                already_published_today=False,
                generate_images=False,
            )

        self.assertEqual(order[:2], ["write:primary", "write:secondary"])
        self.assertEqual(order[2:], ["review:primary", "review:secondary"])

    def test_content_package_replenishes_after_final_ai_review_drops_item(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 1},
            "publishing": {"mode": "dry_run"},
            "final_review": {
                "mode": "enforce",
                "auto_repair": True,
                "min_news_items": {"primary": 2},
            },
        }
        items = [
            {
                "title": f"科技公司{index}发布企业AI工具",
                "source_name": f"来源{index}",
                "url": f"https://example.com/current-{index}",
                "summary": f"科技公司{index}发布企业AI工具，并披露应用范围和首批客户场景。",
                "detail_heading": f"科技公司{index}发布企业AI工具，应用范围和客户场景同步披露",
                "reader_summary": (
                    f"科技公司{index}已经发布企业AI工具，公开报道同时披露了应用范围、"
                    "首批客户场景和使用条件，相关信息可以从来源报道中核对。"
                ),
            }
            for index in range(1, 3)
        ]
        reserve = {
            "title": "候选公司开放新的企业AI能力",
            "source_name": "候选来源",
            "url": "https://example.com/reserve",
            "summary": "候选公司开放新的企业AI能力，并公布使用条件和首批适用场景。",
            "detail_heading": "候选公司开放新的企业AI能力，使用条件和适用场景同步公布",
            "reader_summary": (
                "候选公司已经开放新的企业AI能力，公开报道同时说明了使用条件、"
                "首批适用场景和产品边界，相关事实可以从来源报道中核对。"
            ),
        }
        seed = {
            "topic": "昨天 AI 界发生了什么",
            "column": "AI 昨日速览",
            "news_status": "ok",
            "news_items": items,
            "reserve_news_items": [reserve],
            "target_news_item_count": 2,
            "sources": [
                {"name": f"来源{index}", "url": f"https://example.com/current-{index}", "summary": "公开报道。"}
                for index in range(1, 3)
            ],
        }
        decision = decide_publish_action(
            schedule,
            {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
        )
        review_calls = 0

        def fake_final_review(article, _context, *, visible_html, mode):
            nonlocal review_calls
            review_calls += 1
            updated = dict(article)
            if review_calls == 1:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "high",
                    "issues": [
                        {
                            "type": "platform_risk",
                            "severity": "high",
                            "news_index": 1,
                            "evidence": items[0]["reader_summary"],
                            "action": "drop_item",
                        }
                    ],
                }
            else:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "low",
                    "issues": [],
                }
            return updated

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "src.pipeline.content_package.prepare_text_model_context",
            return_value={"clients": {}, "roles": {}},
        ), patch(
            "src.pipeline.content_package.review_final_article_with_text_model",
            side_effect=fake_final_review,
        ):
            bundle = build_content_package(
                schedule_config=schedule,
                article_seeds={"primary": seed},
                safety_config={},
                publish_decision=decision,
                output_dir=temp_dir,
                seed_source="test",
                already_published_today=False,
                generate_images=False,
            )

        article = bundle["articles"]["primary"]
        self.assertEqual(review_calls, 1)
        self.assertEqual(len(article["news_items"]), 2)
        self.assertTrue(any("候选公司" in item["title"] for item in article["news_items"]))
        self.assertFalse(article["news_replenishment"]["below_minimum"])
        self.assertTrue(bundle["package"]["copy_quality"]["articles"]["primary"]["ok"])

    def test_final_review_recovery_does_not_rerun_paid_reviewer(self) -> None:
        current_items = [
            {
                "title": "甲公司发布企业智能助手",
                "source_name": "来源甲",
                "url": "https://example.com/current-a",
                "summary": "甲公司发布企业智能助手，并说明首批使用范围和交付安排。",
                "detail_heading": "甲公司发布企业智能助手，首批使用范围同步公布",
                "reader_summary": "甲公司已经发布企业智能助手，公开资料同时说明了首批使用范围、交付安排和适用边界。",
            },
            {
                "title": "乙公司开放工业数据平台",
                "source_name": "来源乙",
                "url": "https://example.com/current-b",
                "summary": "乙公司开放工业数据平台，并公布首批合作企业和接入条件。",
                "detail_heading": "乙公司开放工业数据平台，首批合作企业和接入条件公布",
                "reader_summary": "乙公司已经开放工业数据平台，公开资料列出了首批合作企业、接入条件和服务范围。",
            },
        ]
        reserve_items = [
            {
                "title": "丙公司上线客服智能系统",
                "source_name": "来源丙",
                "url": "https://example.com/reserve-c",
                "summary": "丙公司上线客服智能系统，并披露试运行范围和服务指标。",
                "detail_heading": "丙公司上线客服智能系统，试运行范围和服务指标披露",
                "reader_summary": "丙公司已经上线客服智能系统，公开资料说明了试运行范围、服务指标和人工复核机制。",
            },
            {
                "title": "丁公司推出研发协同平台",
                "source_name": "来源丁",
                "url": "https://example.com/reserve-d",
                "summary": "丁公司推出研发协同平台，并公布首批应用团队和上线计划。",
                "detail_heading": "丁公司推出研发协同平台，首批应用团队和上线计划公布",
                "reader_summary": "丁公司已经推出研发协同平台，公开资料披露了首批应用团队、上线计划和权限边界。",
                "_replenishment_failures": 1,
            },
        ]
        article = build_article(
            {
                "topic": "昨日科技与产业动态",
                "column": "科技早报",
                "news_status": "ok",
                "news_items": current_items,
                "reserve_news_items": reserve_items,
                "target_news_item_count": 2,
                "sources": [],
            }
        )
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "high",
            "issues": [
                {
                    "type": "platform_risk",
                    "severity": "high",
                    "news_index": 1,
                    "evidence": current_items[0]["reader_summary"],
                    "action": "drop_item",
                }
            ],
        }
        review_calls = 0

        def fake_final_review(candidate, _context, *, visible_html, mode):
            nonlocal review_calls
            review_calls += 1
            updated = dict(candidate)
            if review_calls == 1:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "high",
                    "issues": [
                        {
                            "type": "platform_risk",
                            "severity": "high",
                            "news_index": 2,
                            "evidence": reserve_items[0]["reader_summary"],
                            "action": "drop_item",
                        }
                    ],
                }
            else:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "low",
                    "issues": [],
                }
            return updated

        with patch(
            "src.pipeline.content_package.review_final_article_with_text_model",
            side_effect=fake_final_review,
        ):
            recovered, report, replenishments = _run_final_review_recovery_cycles(
                article,
                role="primary",
                min_news_items=2,
                target_news_items=2,
                final_review_config={
                    "mode": "enforce",
                    "max_recovery_cycles": 3,
                    "minimum_cycle_budget_seconds": 0,
                },
                text_model_context={"clients": {}, "roles": {}},
                execution_budget=None,
            )

        self.assertEqual(review_calls, 0)
        self.assertEqual(len(report["cycles"]), 1)
        self.assertEqual(report["status"], "resolved")
        self.assertEqual(len(replenishments), 1)
        self.assertEqual(len(recovered["news_items"]), 2)
        self.assertIn("丙公司", "".join(item["title"] for item in recovered["news_items"]))
        self.assertNotIn("丁公司", "".join(item["title"] for item in recovered["news_items"]))
        self.assertTrue(
            all("_replenishment_failures" not in item for item in recovered["news_items"]),
        )

    def test_final_review_recovery_drops_persistent_fact_issue_and_replenishes(self) -> None:
        current_items = [
            {
                "title": "甲公司发布工业机器人平台",
                "source_name": "来源甲",
                "url": "https://example.com/current-a",
                "summary": "甲公司发布工业机器人平台，并披露首批部署安排。",
                "detail_heading": "甲公司发布工业机器人平台",
                "reader_summary": "甲公司发布工业机器人平台，并披露首批部署安排。",
                "fact_card": {
                    "subject": "甲公司",
                    "action": "发布工业机器人平台",
                    "facts": ["甲公司发布工业机器人平台"],
                    "confidence": "high",
                },
            },
            {
                "title": "乙公司建设智能工厂",
                "source_name": "来源乙",
                "url": "https://example.com/current-b",
                "summary": "乙公司建设智能工厂，并公布投产计划。",
                "detail_heading": "乙公司建设智能工厂并公布投产计划",
                "reader_summary": "乙公司建设智能工厂，并公布投产计划。",
            },
            {
                "title": "丙公司上线自动化质检系统",
                "source_name": "来源丙",
                "url": "https://example.com/current-c",
                "summary": "丙公司上线自动化质检系统。",
                "detail_heading": "丙公司上线自动化质检系统",
                "reader_summary": "丙公司上线自动化质检系统，并说明首批应用产线。",
            },
        ]
        reserve = {
            "title": "丁公司开放工业数据平台",
            "source_name": "来源丁",
            "url": "https://example.com/reserve-d",
            "summary": "丁公司开放工业数据平台，并公布首批接入工厂。",
            "detail_heading": "丁公司开放工业数据平台",
            "reader_summary": "丁公司开放工业数据平台，并公布首批接入工厂。",
        }
        reserve_two = {
            "title": "戊公司上线智能仓储系统",
            "source_name": "来源戊",
            "url": "https://example.com/reserve-e",
            "summary": "戊公司上线智能仓储系统，并公布首批应用园区。",
            "detail_heading": "戊公司上线智能仓储系统",
            "reader_summary": "戊公司上线智能仓储系统，并公布首批应用园区。",
        }
        article = build_article(
            {
                "topic": "智能制造日报",
                "column": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "news_status": "ok",
                "news_items": current_items,
                "reserve_news_items": [reserve, reserve_two],
                "target_news_item_count": 3,
                "sources": [],
            }
        )
        issue = {
            "type": "source_outside_fact",
            "severity": "medium",
            "news_index": 1,
            "evidence": current_items[0]["reader_summary"],
            "action": "repair_field",
        }
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "medium",
            "issues": [issue],
        }
        review_calls = 0

        def fake_final_review(candidate, _context, *, visible_html, mode):
            nonlocal review_calls
            review_calls += 1
            updated = dict(candidate)
            if review_calls == 1:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "medium",
                    "issues": [{**issue, "evidence": candidate["news_items"][0]["reader_summary"]}],
                }
            else:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "low",
                    "issues": [],
                }
            return updated

        with patch(
            "src.pipeline.content_package.review_final_article_with_text_model",
            side_effect=fake_final_review,
        ):
            recovered, report, _runs = _run_final_review_recovery_cycles(
                article,
                role="secondary",
                min_news_items=3,
                target_news_items=3,
                final_review_config={"mode": "enforce", "max_recovery_cycles": 3, "minimum_cycle_budget_seconds": 0},
                text_model_context={"clients": {}, "roles": {}},
                execution_budget=None,
            )

        urls = [item["url"] for item in recovered["news_items"]]
        self.assertEqual(report["status"], "resolved")
        self.assertNotIn(current_items[0]["url"], urls)
        self.assertIn(reserve["url"], urls)
        self.assertTrue(report["dropped_items"])

    def test_final_review_recovery_stops_after_deterministic_recheck(self) -> None:
        items = [
            {
                "title": f"制造企业{index}发布自动化项目",
                "source_name": f"来源{index}",
                "url": f"https://example.com/item-{index}",
                "summary": f"制造企业{index}发布自动化项目并披露交付安排。",
                "detail_heading": f"制造企业{index}发布自动化项目",
                "reader_summary": f"制造企业{index}发布自动化项目并披露交付安排。",
                "fact_card": {
                    "subject": f"制造企业{index}",
                    "action": "发布自动化项目",
                    "facts": [f"制造企业{index}发布自动化项目并披露交付安排"],
                    "confidence": "high",
                },
            }
            for index in range(1, 4)
        ]
        reserve = {
            "title": "补位企业开放工业平台",
            "source_name": "补位来源",
            "url": "https://example.com/reserve",
            "summary": "补位企业开放工业平台并公布接入安排。",
            "detail_heading": "补位企业开放工业平台",
            "reader_summary": "补位企业开放工业平台并公布接入安排。",
        }
        reserve_two = {
            "title": "候选企业建设智能工厂",
            "source_name": "候选来源二",
            "url": "https://example.com/reserve-two",
            "summary": "候选企业建设智能工厂并公布投产安排。",
            "detail_heading": "候选企业建设智能工厂",
            "reader_summary": "候选企业建设智能工厂并公布投产安排。",
        }
        reserve_three = {
            "title": "备用企业上线质检平台",
            "source_name": "候选来源三",
            "url": "https://example.com/reserve-three",
            "summary": "备用企业上线质检平台并披露部署范围。",
            "detail_heading": "备用企业上线质检平台",
            "reader_summary": "备用企业上线质检平台并披露部署范围。",
        }
        article = build_article(
            {
                "topic": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "news_items": items,
                "reserve_news_items": [reserve, reserve_two, reserve_three],
                "target_news_item_count": 3,
                "sources": [],
            }
        )
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "medium",
            "issues": [
                {
                    "type": "source_outside_fact",
                    "severity": "medium",
                    "news_index": 1,
                    "evidence": items[0]["reader_summary"],
                    "action": "repair_field",
                }
            ],
        }
        calls = 0

        def fake_final_review(candidate, _context, *, visible_html, mode):
            nonlocal calls
            calls += 1
            updated = dict(candidate)
            if calls == 1:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "medium",
                    "issues": [
                        {
                            "type": "source_outside_fact",
                            "severity": "medium",
                            "news_index": 2,
                            "evidence": candidate["news_items"][1]["reader_summary"],
                            "action": "repair_field",
                        }
                    ],
                }
            elif calls == 2:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "medium",
                    "issues": [
                        {
                            "type": "off_topic",
                            "severity": "medium",
                            "news_index": 2,
                            "evidence": candidate["news_items"][1]["reader_summary"],
                            "action": "drop_item",
                            "reason": "该新闻与智能制造无关，分类严重错位。",
                        }
                    ],
                }
            else:
                updated["final_ai_review"] = {
                    "mode": mode,
                    "status": "applied",
                    "risk_level": "low",
                    "issues": [],
                }
            return updated

        with patch(
            "src.pipeline.content_package.review_final_article_with_text_model",
            side_effect=fake_final_review,
        ):
            recovered, report, _runs = _run_final_review_recovery_cycles(
                article,
                role="secondary",
                min_news_items=2,
                target_news_items=3,
                final_review_config={"mode": "enforce", "max_recovery_cycles": 99, "minimum_cycle_budget_seconds": 0},
                text_model_context={"clients": {}, "roles": {}},
                execution_budget=None,
            )

        self.assertEqual(report["status"], "resolved")
        self.assertEqual(calls, 0)
        self.assertEqual(len(report["cycles"]), 1)
        self.assertTrue(all("final_quarantine" not in cycle for cycle in report["cycles"]))
        self.assertEqual(len(recovered["news_items"]), 2)

    def test_text_model_json_parser_repairs_unescaped_inner_quotes(self) -> None:
        payload = _parse_json_object('{"digest":"昨天几条新闻不像是"又多了几个新功能"，而是进入真实流程。"}')

        self.assertEqual(payload["digest"], '昨天几条新闻不像是"又多了几个新功能"，而是进入真实流程。')

    def test_text_model_json_parser_wraps_top_level_items_array(self) -> None:
        payload = _parse_json_object('[{"index": 1, "detail_heading": "A complete heading"}]')

        self.assertEqual(payload["items"][0]["index"], 1)

    def test_build_daily_article_seeds_creates_primary_and_secondary(self) -> None:
        schedule = {
            "daily_content_package": {
                "primary_article": {
                    "topic": "昨天 AI 界发生了什么",
                    "column": "AI 昨日速览",
                },
                "secondary_article": {
                    "topic": "智能制造日报",
                    "column": "智能制造日报",
                },
            }
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "column": "AI 工具与方法",
            "sources": [],
        }

        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-02",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "New AI model ships",
                        "source_name": "Example AI",
                        "url": "https://example.com/model",
                        "published_at": "2026-06-02T16:00:00+08:00",
                        "summary": "Useful model update for developers.",
                    }
                ],
                "sources": [
                    {
                        "name": "Example AI：New AI model ships",
                        "url": "https://example.com/model",
                        "summary": "Useful model update for developers.",
                    }
                ],
            },
            primary_seed_source="data/news-seeds/2026-06-02-ai-news.json",
            secondary_seed={
                "target_date": "2026-06-02",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "星云芯片与远航制造合建智能工厂，推进机器人与自动驾驶项目",
                        "source_name": "示例商业媒体",
                        "url": "https://example.invalid/smart-factory",
                        "published_at": "2026-06-02T18:00:00+08:00",
                        "summary": "双方将围绕AI工厂、机器人训练和自动驾驶基础设施合作。",
                    }
                ],
                "sources": [
                    {
                        "name": "示例商业媒体：星云芯片与远航制造合建智能工厂，推进机器人与自动驾驶项目",
                        "url": "https://example.invalid/smart-factory",
                        "summary": "双方将围绕AI工厂、机器人训练和自动驾驶基础设施合作。",
                    }
                ],
            },
            secondary_seed_source="data/news-seeds/2026-06-02-manufacturing-news.json",
            now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        self.assertEqual(seeds["primary"]["topic"], "昨天 AI 界发生了什么")
        self.assertEqual(seeds["primary"]["column"], "AI 昨日速览")
        self.assertEqual(seeds["primary"]["news_status"], "ok")
        self.assertEqual(seeds["primary"]["news_items"][0]["title"], "New AI model ships")
        self.assertEqual(seeds["secondary"]["topic"], "智能制造日报")
        self.assertEqual(seeds["secondary"]["column"], "智能制造日报")
        self.assertEqual(seeds["secondary"]["source_policy"], "smart_manufacturing_daily")
        self.assertEqual(seeds["secondary"]["digest"], "每日智能制造产业资讯速递。")
        self.assertEqual(seeds["secondary"]["news_items"][0]["title"], "星云芯片与远航制造合建智能工厂，推进机器人与自动驾驶项目")
        self.assertEqual(seeds["secondary"]["sources"][0]["name"], "示例商业媒体：星云芯片与远航制造合建智能工厂，推进机器人与自动驾驶项目")

    def test_primary_seed_replaces_wechat_platform_fraud_risk_news(self) -> None:
        schedule = {"daily_content_package": {"article_count": 1}}
        selected = {"topic": "普通人如何用 AI 提高工作效率", "sources": []}

        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-13",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "虚构平台起诉利用AI诈骗大量用户的跨境网络犯罪团伙",
                        "source_name": "Example Security Desk",
                        "url": "https://example.invalid/risky",
                        "published_at": "2026-06-13T10:00:00+08:00",
                        "summary": "该团伙涉嫌利用 AI 技术诈骗数十万名受害者，并在两周内发送了250万条短信。",
                    },
                    {
                        "title": "具身智能企业分享物理世界探索进展",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/safe",
                        "published_at": "2026-06-13T11:00:00+08:00",
                        "summary": "机器人企业展示了仿真训练、数据采集和线下部署的新进展。",
                    },
                ],
                "sources": [],
            },
            primary_seed_source="data/news-seeds/2026-06-13-ai-news.json",
            now=datetime(2026, 6, 14, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        titles = [item["title"] for item in seeds["primary"]["news_items"]]

        self.assertNotIn("虚构平台起诉利用AI诈骗大量用户的跨境网络犯罪团伙", titles)
        self.assertEqual(titles, ["具身智能企业分享物理世界探索进展"])
        self.assertEqual(
            seeds["primary"]["wechat_platform_risk_replacements"][0]["reason"],
            "wechat_platform_sensitive_combo",
        )
        self.assertEqual(seeds["primary"]["sources"][0]["name"], "Example AI：具身智能企业分享物理世界探索进展")

    def test_secondary_manufacturing_seed_replaces_cyber_leak_risk_news(self) -> None:
        schedule = {"daily_content_package": {"article_count": 2}}
        selected = {"topic": "普通人如何用 AI 提高工作效率", "sources": []}

        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-25",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "深圳将举办GW级Token工厂论坛",
                        "source_name": "Example Infrastructure Desk",
                        "url": "https://example.invalid/token-factory",
                        "published_at": "2026-06-25T10:00:00+08:00",
                        "summary": "论坛聚焦算力基础设施、数据中心和AI工厂建设。",
                    }
                ],
                "sources": [],
            },
            primary_seed_source="data/news-seeds/2026-06-25-ai-news.json",
            secondary_seed={
                "target_date": "2026-06-25",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "虚构代工企业被黑，未发布设备与芯片资料确认泄露",
                        "source_name": "Example Security Desk",
                        "url": "https://example.invalid/factory-leak",
                        "published_at": "2026-06-25T23:30:00+08:00",
                        "summary": "一家虚构工厂遭遇大规模网络攻击，超过 630GB 的机密数据被窃取，其中包括未发布设备设计图纸和芯片资料。",
                    },
                    {
                        "title": "具身智能产业链规模冲刺万亿",
                        "source_name": "Example Manufacturing Desk",
                        "url": "https://example.invalid/embodied",
                        "published_at": "2026-06-25T22:30:00+08:00",
                        "summary": "核心零部件、整机系统和场景应用协同加快，产业链进入量产验证阶段。",
                    },
                ],
                "sources": [],
            },
            secondary_seed_source="data/news-seeds/2026-06-25-manufacturing-news.json",
            now=datetime(2026, 6, 26, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        secondary = seeds["secondary"]
        titles = [item["title"] for item in secondary["news_items"]]

        self.assertNotIn("虚构代工企业被黑，未发布设备与芯片资料确认泄露", titles)
        self.assertIn("具身智能产业链规模冲刺万亿", titles)
        self.assertEqual(
            secondary["wechat_platform_risk_replacements"][0]["reason"],
            "wechat_platform_sensitive_combo",
        )

    def test_secondary_article_uses_manufacturing_items_from_primary_only_as_fallback(self) -> None:
        schedule = {"daily_content_package": {"article_count": 2}}
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [],
        }

        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "A broad vision for future AI systems",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/future-vision",
                        "published_at": "2026-06-08T08:00:00+08:00",
                        "summary": "A general statement without a concrete product or delivery date.",
                    },
                    {
                        "title": "云虎机器人收购仓储自动化团队",
                        "source_name": "Example Robotics Desk",
                        "url": "https://example.invalid/robotics",
                        "published_at": "2026-06-08T11:00:00+08:00",
                        "summary": "具身智能创业公司进入物流机器人和仓储自动化场景。",
                    },
                ],
                "sources": [],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        self.assertEqual(seeds["secondary"]["topic"], "智能制造日报")
        self.assertEqual(seeds["secondary"]["source_policy"], "smart_manufacturing_daily")
        self.assertEqual(seeds["secondary"]["news_items"][0]["source_name"], "Example Robotics Desk")
        self.assertIn("仓储自动化", seeds["secondary"]["news_items"][0]["title"])
        self.assertNotIn("broad vision", seeds["secondary"]["topic"])

    def test_secondary_manufacturing_seed_supplements_sparse_independent_sources(self) -> None:
        schedule = {"daily_content_package": {"article_count": 2}}
        selected = {"topic": "普通人如何用 AI 提高工作效率", "sources": []}

        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "A broad vision for future AI systems",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/future-vision",
                        "published_at": "2026-06-08T08:00:00+08:00",
                        "summary": "A general statement without a concrete product or delivery date.",
                    },
                    {
                        "title": "云虎机器人把视觉拣选系统接入仓储试点",
                        "source_name": "Example Robotics Desk",
                        "url": "https://example.invalid/robot-picking",
                        "published_at": "2026-06-08T14:00:00+08:00",
                        "summary": "具身智能创业公司进入物流机器人和仓储自动化场景。",
                    },
                    {
                        "title": "Harbor Motors schedules a software and AI event for embodied intelligence",
                        "source_name": "Example Mobility Desk",
                        "url": "https://example.invalid/harbor-motors-ai",
                        "published_at": "2026-06-08T15:00:00+08:00",
                        "summary": "Harbor Motors will detail its software, AI and embodied intelligence roadmap.",
                    },
                ],
                "sources": [],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            secondary_seed={
                "target_date": "2026-06-08",
                "news_status": "insufficient_sources",
                "news_items": [
                    {
                        "title": "Northstar Industrial and Orion Robotics build an AI factory",
                        "source_name": "Example Manufacturer Blog",
                        "url": "https://example.invalid/northstar-orion",
                        "published_at": "2026-06-08T11:00:00+08:00",
                        "summary": "AI factory, robotics, autonomous driving and data center technologies.",
                    },
                    {
                        "title": "Riverlight Energy and Harbor Motors collaborate on a smart factory",
                        "source_name": "Example Manufacturer Blog",
                        "url": "https://example.invalid/riverlight-harbor",
                        "published_at": "2026-06-08T12:00:00+08:00",
                        "summary": "Physical AI, robotics and AI factory infrastructure.",
                    },
                ],
                "sources": [],
            },
            secondary_seed_source="data/news-seeds/2026-06-08-manufacturing-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        secondary = seeds["secondary"]
        titles = [item["title"] for item in secondary["news_items"]]
        source_names = {item["source_name"] for item in secondary["news_items"]}
        source_labels = [source["name"] for source in secondary["sources"]]

        self.assertEqual(secondary["news_status"], "supplemented")
        self.assertEqual(secondary["supplemental_seed_source"], "data/news-seeds/2026-06-08-ai-news.json")
        self.assertIn("Northstar Industrial and Orion Robotics build an AI factory", titles)
        self.assertIn("云虎机器人把视觉拣选系统接入仓储试点", titles)
        self.assertIn("Example Robotics Desk", source_names)
        self.assertGreaterEqual(len(source_names), 2)
        self.assertTrue(any(label.startswith("Example Robotics Desk：") for label in source_labels))

    def test_secondary_manufacturing_seed_dedupes_same_event_across_languages(self) -> None:
        schedule = {"daily_content_package": {"article_count": 2}}
        selected = {"topic": "普通人如何用 AI 提高工作效率", "sources": []}

        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-11",
                "news_status": "ok",
                "news_items": [],
                "sources": [],
            },
            primary_seed_source="data/news-seeds/2026-06-11-ai-news.json",
            secondary_seed={
                "target_date": "2026-06-11",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "传统零部件业务承压，北辰工业将加大力度布局人形机器人领域",
                        "source_name": "Example Business Desk",
                        "url": "https://example.invalid/northstar-robot",
                        "published_at": "2026-06-11T00:20:00+08:00",
                        "summary": "北辰工业表示，将加大在人形机器人领域的研发和投入力度。",
                    },
                    {
                        "title": "Northstar Industrial expands its humanoid robotics program",
                        "source_name": "Example Robotics Desk",
                        "url": "https://example.invalid/northstar-robot-en",
                        "published_at": "2026-06-11T00:27:25+08:00",
                        "summary": "Northstar Industrial will increase research and investment in humanoid robotics.",
                    },
                    {
                        "title": "云港航空出售传统资产，加码低空经济装备制造",
                        "source_name": "Example Manufacturing Desk",
                        "url": "https://example.invalid/low-altitude",
                        "published_at": "2026-06-11T10:00:00+08:00",
                        "summary": "公司拟出售传统资产，继续投入低空经济和航空装备制造。",
                    },
                ],
                "sources": [],
            },
            secondary_seed_source="data/news-seeds/2026-06-11-manufacturing-news.json",
            now=datetime(2026, 6, 12, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        titles = [item["title"] for item in seeds["secondary"]["news_items"]]
        self.assertIn("传统零部件业务承压，北辰工业将加大力度布局人形机器人领域", titles)
        self.assertNotIn("Northstar Industrial expands its humanoid robotics program", titles)

    def test_build_content_package_writes_two_article_outputs(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "dry_run"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            provisional = decide_publish_action(
                schedule,
                {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
            )
            bundle = build_content_package(
                schedule_config=schedule,
                article_seeds=seeds,
                safety_config={"blocked_claim_types": []},
                publish_decision=provisional,
                output_dir=temp_dir,
                seed_source="config/topics.yaml",
                already_published_today=False,
                now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

            package = bundle["package"]
            package_review = aggregate_package_review(bundle["reviews"])

            self.assertEqual(package["schema_version"], CONTENT_PACKAGE_SCHEMA_VERSION)
            self.assertEqual(package["article_count"], 2)
            self.assertEqual(package["primary_article"]["role"], "primary")
            self.assertEqual(package["secondary_article"]["role"], "secondary")
            self.assertEqual(package["package_risk_level"], "medium")
            self.assertEqual(package_review["risk_level"], "medium")
            self.assertEqual(validate_content_package(package), [])
            self.assertTrue((Path(temp_dir) / "primary" / "article.json").exists())
            self.assertTrue((Path(temp_dir) / "primary" / "illustration.png").exists())
            self.assertTrue((Path(temp_dir) / "secondary" / "article.md").exists())
            self.assertTrue((Path(temp_dir) / "secondary" / "illustration.png").exists())
            self.assertTrue((Path(temp_dir) / "content-package.json").exists())
            self.assertIn("illustration", package["files"]["primary"])
            self.assertEqual(package["image_generation"]["provider_status"]["status"], "disabled")
            self.assertEqual(package["primary_article"]["image_provider"], "local_fallback")

    def test_content_package_sanitizes_removable_rss_residue_before_copy_quality_gate(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 1},
            "final_review": {"min_news_items": {"primary": 1}},
            "publishing": {"mode": "auto_publish_low_risk", "allow_low_risk_auto_publish": True},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-15",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Agent 工程实践图谱：Context Design、Subagents 与 Harness",
                        "source_name": "Example Developer Desk",
                        "url": "https://example.invalid/agent-engineering",
                        "published_at": "2026-06-15T18:31:38+08:00",
                        "summary": "2026-06-15T18:31:38+08:00；点击查看原文>",
                    }
                ],
                "sources": [
                    {
                        "name": "Example Developer Desk：Agent 工程实践图谱：Context Design、Subagents 与 Harness",
                        "url": "https://example.invalid/agent-engineering",
                        "summary": "2026-06-15T18:31:38+08:00；点击查看原文>",
                    }
                ],
            },
            primary_seed_source="data/news-seeds/2026-06-15-ai-news.json",
            now=datetime(2026, 6, 16, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("TEXT_MODEL_API_KEY=\n", encoding="utf-8")
            provisional = decide_publish_action(
                schedule,
                {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
            )
            bundle = build_content_package(
                schedule_config=schedule,
                article_seeds=seeds,
                safety_config={"blocked_claim_types": []},
                publish_decision=provisional,
                output_dir=temp_dir,
                seed_source="config/topics.yaml",
                already_published_today=False,
                env_path=env_path,
                now=datetime(2026, 6, 16, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

        article = bundle["articles"]["primary"]
        article_text = json.dumps(article, ensure_ascii=False)
        self.assertNotIn("点击查看原文", article_text)
        self.assertTrue(article["copy_sanitization"]["removed_low_quality_fragments"])
        self.assertEqual(
            article["news_items"][0]["summary"],
            "Agent 工程实践图谱：Context Design、Subagents 与 Harness。",
        )
        self.assertEqual(
            article["sources"][0]["summary"],
            "Example Developer Desk：Agent 工程实践图谱：Context Design、Subagents 与 Harness 的公开报道。",
        )
        self.assertTrue(bundle["package"]["copy_quality"]["articles"]["primary"]["ok"])
        self.assertTrue(bundle["reviews"]["primary"]["wechat_ready"])

    def test_build_content_package_caps_requested_three_articles_to_two(self) -> None:
        schedule = {
            "daily_content_package": {
                "article_count": 3,
                "tertiary_article": {"column": "AI 工具箱"},
            },
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-06",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-06T16:00:00+08:00",
                        "summary": (
                            "Northstar AI is updating Atlas Assistant memory controls so users can review, delete, or disable "
                            "saved preferences from settings. The rollout is being enabled gradually by account, "
                            "with separate controls for long-term memory and the current conversation context."
                        ),
                    },
                    {
                        "title": "CloudDesk reserves accelerator capacity under a multi-year agreement",
                        "source_name": "Example Cloud",
                        "url": "https://example.invalid/compute",
                        "published_at": "2026-06-06T17:00:00+08:00",
                        "summary": "Compute spending becomes a practical AI infrastructure signal.",
                    },
                ],
                "sources": [
                    {
                        "name": "Example AI：Northstar AI adds memory upgrades to Atlas Assistant",
                        "url": "https://example.invalid/model",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
            },
            primary_seed_source="data/news-seeds/2026-06-06-ai-news.json",
            secondary_seed={
                "target_date": "2026-06-06",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "云港首个eVTOL整机项目进入建设阶段",
                        "source_name": "示例商业媒体",
                        "url": "https://example.invalid/evtol-project",
                        "published_at": "2026-06-06T18:00:00+08:00",
                        "summary": "低空经济整机制造项目落户，后续看适航和订单交付。",
                    }
                ],
                "sources": [
                    {
                        "name": "示例商业媒体：云港首个eVTOL整机项目进入建设阶段",
                        "url": "https://example.invalid/evtol-project",
                        "summary": "低空经济整机制造项目落户，后续看适航和订单交付。",
                    }
                ],
            },
            secondary_seed_source="data/news-seeds/2026-06-06-manufacturing-news.json",
            now=datetime(2026, 6, 7, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            provisional = decide_publish_action(
                schedule,
                {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
            )
            bundle = build_content_package(
                schedule_config=schedule,
                article_seeds=seeds,
                safety_config={"blocked_claim_types": []},
                publish_decision=provisional,
                output_dir=temp_dir,
                seed_source="config/topics.yaml",
                already_published_today=False,
                now=datetime(2026, 6, 7, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

            package = bundle["package"]
            self.assertEqual(package["article_count"], 2)
            self.assertEqual(package["article_roles"], ["primary", "secondary"])
            self.assertNotIn("tertiary_article", package)
            self.assertNotIn("tertiary", bundle["articles"])
            self.assertEqual(package["secondary_article"]["column"], "智能制造日报")
            self.assertEqual(bundle["articles"]["secondary"]["source_policy"], "smart_manufacturing_daily")
            secondary_html = Path(bundle["files"]["secondary"]["article_html"]).read_text(encoding="utf-8")
            self.assertIn("智能制造日报", secondary_html)
            self.assertIn("低空经济与交通装备", secondary_html)
            self.assertEqual(validate_content_package(package), [])

    def test_primary_news_package_uses_weekday_briefing_cover(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "dry_run"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        now = datetime(2026, 6, 5, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-04",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-04T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
                "sources": [
                    {
                        "name": "Example AI：Northstar AI adds memory upgrades to Atlas Assistant",
                        "url": "https://example.invalid/model",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
            },
            primary_seed_source="data/news-seeds/2026-06-04-ai-news.json",
            now=now,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            provisional = decide_publish_action(
                schedule,
                {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
            )
            bundle = build_content_package(
                schedule_config=schedule,
                article_seeds=seeds,
                safety_config={"blocked_claim_types": []},
                publish_decision=provisional,
                output_dir=temp_dir,
                seed_source="config/topics.yaml",
                already_published_today=False,
                now=now,
            )

            cover = bundle["articles"]["primary"]["image_generation"]["cover"]
            inline_images = bundle["articles"]["primary"]["inline_images"]
            self.assertEqual(cover["provider"], "local_template")
            self.assertTrue(str(cover["template"]).endswith("tech-briefing-friday.png"))
            self.assertEqual(len(inline_images), 1)
            self.assertEqual(len(bundle["files"]["primary"]["inline_images"]), 1)
            self.assertTrue(Path(bundle["files"]["primary"]["inline_images"][0]).exists())
            self.assertIn("ARTICLE_INLINE_PRIMARY_1", Path(bundle["files"]["primary"]["article_html"]).read_text(encoding="utf-8"))
            self.assertEqual(
                Path(bundle["files"]["primary"]["cover"]).read_bytes(),
                Path("assets/covers/weekly-tech-briefing/tech-briefing-friday.png").read_bytes(),
            )

    def test_build_content_package_uses_source_inline_images_when_news_items_have_images(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 1},
            "publishing": {"mode": "draft_only"},
        }
        selected = {"topic": "unused", "sources": []}
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-04",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI ships workplace agents",
                        "source_name": "Example AI",
                        "url": "https://example.com/agents",
                        "published_at": "2026-06-04T16:00:00+08:00",
                        "summary": "AI agents enter workplace workflows.",
                        "image_url": "https://img.example.com/agents.jpg",
                    }
                ],
                "sources": [],
            },
            primary_seed_source="data/news-seeds/2026-06-04-ai-news.json",
            now=datetime(2026, 6, 5, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            response = FakeSourceImageResponse(b"\xff\xd8fakejpg", content_type="image/jpeg")
            with patch("src.pipeline.content_package.requests.get", return_value=response):
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    now=datetime(2026, 6, 5, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

            inline_path = Path(bundle["files"]["primary"]["inline_images"][0])
            html = Path(bundle["files"]["primary"]["article_html"]).read_text(encoding="utf-8")
            self.assertEqual(inline_path.name, "inline-1.jpg")
            self.assertEqual(inline_path.read_bytes(), b"\xff\xd8fakejpg")
            self.assertEqual(bundle["articles"]["primary"]["image_generation"]["inline_images"][0]["provider"], "source_image")
            self.assertIn("来源配图：Example AI", html)
            self.assertIn("ARTICLE_INLINE_PRIMARY_1", html)

    def test_build_content_package_converts_source_webp_inline_images(self) -> None:
        from PIL import Image

        schedule = {
            "daily_content_package": {"article_count": 1},
            "publishing": {"mode": "draft_only"},
        }
        selected = {"topic": "unused", "sources": []}
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-04",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar Lab publishes an AI research update",
                        "source_name": "Example Research Desk",
                        "url": "https://example.invalid/research-update",
                        "published_at": "2026-06-04T16:00:00+08:00",
                        "summary": "A fictional AI research update includes a webp social image.",
                        "image_url": "https://img.example.invalid/social-image.webp",
                    }
                ],
                "sources": [],
            },
            primary_seed_source="data/news-seeds/2026-06-04-ai-news.json",
            now=datetime(2026, 6, 5, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        image_buffer = BytesIO()
        Image.new("RGB", (4, 4), (255, 120, 60)).save(image_buffer, format="WEBP")

        with tempfile.TemporaryDirectory() as temp_dir:
            response = FakeSourceImageResponse(image_buffer.getvalue(), content_type="image/webp")
            with patch("src.pipeline.content_package.requests.get", return_value=response):
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    now=datetime(2026, 6, 5, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

            inline_path = Path(bundle["files"]["primary"]["inline_images"][0])
            image_result = bundle["articles"]["primary"]["image_generation"]["inline_images"][0]
            self.assertEqual(inline_path.name, "inline-1.jpg")
            self.assertTrue(inline_path.read_bytes().startswith(b"\xff\xd8"))
            self.assertEqual(image_result["provider"], "source_image")
            self.assertEqual(image_result["status"], "downloaded")
            self.assertEqual(image_result["source_format"], "webp")
            self.assertEqual(image_result["converted_to"], "jpeg")

    def test_build_content_package_uses_openai_images_when_configured(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("OPENAI_API_KEY=test-key\nOPENAI_IMAGE_MODEL=image-model\n", encoding="utf-8")
            fake_png = b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")

            ready_quality = {
                "ok": True,
                "text_models_enabled": False,
                "required_text_model_roles": [],
                "failed_roles": [],
                "articles": {
                    role: {"ok": True, "checks": [], "failed_checks": []}
                    for role in seeds
                },
            }
            def ready_review(article, _safety):
                return {
                    "schema_version": "review.v1",
                    "reviewed_at": "2026-06-03T08:00:00+08:00",
                    "article_schema_version": article.get("schema_version", ""),
                    "title": article.get("title", ""),
                    "risk_level": "low",
                    "wechat_ready": True,
                    "flags": {
                        "fact_uncertain": False,
                        "sensitive_topic": False,
                        "copyright_unclear": False,
                    },
                    "checks": [{"name": "test", "ok": True, "severity": "low", "details": []}],
                    "notes": [],
                    "recommendations": [],
                    "sensitive_hits": [],
                    "blocked_claim_hits": [],
                }
            with patch("src.integrations.openai_image_client.requests.Session") as session_class, patch(
                "src.pipeline.content_package.evaluate_package_copy_quality",
                return_value=ready_quality,
            ), patch(
                "src.pipeline.content_package.review_article",
                side_effect=ready_review,
            ):
                session = session_class.return_value
                session.post.return_value = FakeImageResponse(
                    status_code=200,
                    payload={"data": [{"b64_json": fake_png}]},
                )
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    generate_images=True,
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

            package = bundle["package"]
            self.assertEqual(package["image_generation"]["provider_status"]["status"], "configured")
            self.assertEqual(package["primary_article"]["image_provider"], "openai_image_api")
            self.assertEqual(session.post.call_count, 4)
            self.assertTrue(Path(bundle["files"]["primary"]["cover"]).read_bytes().startswith(b"\x89PNG"))

    def test_build_content_package_skips_paid_images_until_text_is_publish_ready(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 1},
            "publishing": {"mode": "draft_only"},
            "final_review": {"min_news_items": {"primary": 2}},
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed={"topic": "科技早报", "sources": []},
            seed_source="config/topics.yaml",
            primary_seed={
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Only one candidate remains",
                        "source_name": "Example",
                        "url": "https://example.com/one",
                        "summary": "Only one candidate is available.",
                    }
                ],
                "sources": [],
            },
            now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("OPENAI_API_KEY=test-key\nOPENAI_IMAGE_MODEL=image-model\n", encoding="utf-8")
            with patch("src.integrations.openai_image_client.requests.Session") as session_class:
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={},
                    publish_decision=decide_publish_action(
                        schedule,
                        {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                    ),
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    generate_images=True,
                    now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )
                cover_exists = Path(bundle["files"]["primary"]["cover"]).exists()

        self.assertEqual(session_class.return_value.post.call_count, 0)
        provider = bundle["package"]["image_generation"]["provider_status"]
        self.assertEqual(provider["status"], "skipped_content_quality_gate")
        self.assertFalse(bundle["package"]["image_generation"]["content_ready"])
        self.assertTrue(cover_exists)

    def test_build_content_package_records_disabled_text_models_by_default(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
                "sources": [{"name": "Example AI", "url": "https://example.invalid/model", "summary": "summary"}],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("", encoding="utf-8")
            provisional = decide_publish_action(
                schedule,
                {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
            )
            bundle = build_content_package(
                schedule_config=schedule,
                article_seeds=seeds,
                safety_config={"blocked_claim_types": []},
                publish_decision=provisional,
                output_dir=temp_dir,
                seed_source="config/topics.yaml",
                already_published_today=False,
                env_path=env_path,
                now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

        roles = bundle["package"]["text_model_generation"]["roles"]
        self.assertEqual(roles["summary"]["status"], "disabled")
        self.assertEqual(roles["batch"]["status"], "disabled")
        self.assertEqual(roles["polish"]["status"], "disabled")
        self.assertEqual(bundle["package"]["text_model_generation"]["articles"], {})

    def test_build_content_package_can_apply_configured_text_models(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "final_review": {"min_news_items": {"primary": 1, "secondary": 1}},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
                "sources": [{"name": "Example AI", "url": "https://example.invalid/model", "summary": "summary"}],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=text-key",
                        "TEXT_MODEL_BATCH_ENABLED=true",
                        "TEXT_MODEL_BATCH_MODEL=qwen3.7-plus",
                    ]
                ),
                encoding="utf-8",
            )
            fake_payload = {
                "title": "Northstar AI强化记忆能力；AI工具继续贴近日常工作丨科技早报",
                "digest": "今天重点看 AI 工具如何进入真实工作流。",
                "items": [
                    {
                        "index": 1,
                        "detail_heading": "Northstar AI强化Atlas记忆管理，长期助手边界被重新讨论",
                        "reader_summary": (
                            "Northstar AI 正在更新 Atlas Assistant 的记忆控制，用户可以在设置页查看、删除或停用系统保存的长期偏好，"
                            "不必再把所有历史信息交给助手持续调用。此次功能按账号逐步开放，并把长期记忆与当前对话上下文"
                            "分开管理，方便用户针对不同场景选择是否保留。已经保存的内容仍可逐条检查和清除，关闭记忆后，"
                            "后续对话也不会继续沿用这些偏好。"
                        ),
                    }
                ],
            }
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.return_value = FakeTextResponse(
                    status_code=200,
                    payload={"choices": [{"message": {"content": json.dumps(fake_payload, ensure_ascii=False)}}]},
                )
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        self.assertEqual(bundle["articles"]["primary"]["title"], fake_payload["title"])
        self.assertEqual(bundle["articles"]["primary"]["digest"], fake_payload["digest"])
        self.assertEqual(
            bundle["articles"]["primary"]["news_items"][0]["detail_heading"],
            fake_payload["items"][0]["detail_heading"],
        )
        self.assertEqual(
            bundle["articles"]["primary"]["news_items"][0]["reader_summary"],
            fake_payload["items"][0]["reader_summary"],
        )
        self.assertEqual(bundle["package"]["text_model_generation"]["roles"]["batch"]["status"], "configured")
        self.assertEqual(
            bundle["package"]["text_model_generation"]["articles"]["primary"]["roles"]["batch"]["status"],
            "applied",
        )
        self.assertEqual(bundle["package"]["copy_quality"]["required_text_model_roles"], ["batch"])
        self.assertTrue(bundle["package"]["copy_quality"]["articles"]["primary"]["ok"])
        self.assertEqual(session.post.call_count, 1 + (2 * len(bundle["articles"])))

    def test_text_model_generated_copy_must_preserve_precise_amounts(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-10",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "ContextWorks完成2400万美元融资",
                        "source_name": "Example Venture Desk",
                        "url": "https://example.invalid/contextworks",
                        "published_at": "2026-06-10T21:33:09+08:00",
                        "summary": (
                            "ContextWorks 完成 2400 万美元融资，由 Northbank 领投，River Ventures 参投。"
                            "公司为企业 AI 代理提供内部流程、权限和数据上下文，帮助系统执行跨部门任务。"
                            "本轮资金将用于扩充工程团队，并支持更多需要连接多套内部系统的企业部署。"
                            "客户可在部署时设置数据接口和访问范围，并为关键操作保留人工复核环节。"
                            "部署团队还将记录接口调用和人工确认结果，供企业客户验收系统的执行过程。"
                        ),
                    }
                ],
                "sources": [
                    {"name": "Example Venture Desk", "url": "https://example.invalid/contextworks", "summary": "summary"}
                ],
            },
            primary_seed_source="data/news-seeds/2026-06-10-ai-news.json",
            now=datetime(2026, 6, 11, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=text-key",
                        "TEXT_MODEL_BATCH_ENABLED=true",
                        "TEXT_MODEL_BATCH_MODEL=qwen3.7-plus",
                    ]
                ),
                encoding="utf-8",
            )
            fake_payload = {
                "title": "ContextWorks融资；企业AI代理进入业务上下文丨科技早报",
                "digest": "今天重点看 AI 代理如何进入企业业务流程。",
                "items": [
                    {
                        "index": 1,
                        "detail_heading": "ContextWorks完成两千万美元融资，为AI代理补充企业业务上下文",
                        "reader_summary": (
                            "ContextWorks 为企业 AI 代理提供内部流程、权限和数据上下文，让系统在执行跨部门任务时能够读取"
                            "必要信息并减少理解偏差。公司近日完成两千万美元融资，由 Northbank 领投、River Ventures"
                            "参投，资金将用于扩充工程团队。产品目前主要服务需要连接多套内部系统的企业客户，部署时还会"
                            "配置数据接口和访问范围。"
                        ),
                    }
                ],
            }
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.return_value = FakeTextResponse(
                    status_code=200,
                    payload={"choices": [{"message": {"content": json.dumps(fake_payload, ensure_ascii=False)}}]},
                )
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 11, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        item = bundle["articles"]["primary"]["news_items"][0]
        combined_copy = f"{item.get('detail_heading', '')}\n{item.get('reader_summary', '')}"

        self.assertEqual(session.post.call_count, 2 + (2 * len(bundle["articles"])))
        self.assertEqual(bundle["articles"]["primary"]["title"], fake_payload["title"])
        self.assertNotIn("两千万美元", combined_copy)
        self.assertNotEqual(item.get("detail_heading"), fake_payload["items"][0]["detail_heading"])
        self.assertNotEqual(item.get("reader_summary", ""), fake_payload["items"][0]["reader_summary"])

    def test_text_model_title_with_source_byline_is_rejected(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 1},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-14",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "星河科技回应首席研究员离职传言",
                        "source_name": "示例财经媒体",
                        "url": "https://example.invalid/company-rumor",
                        "published_at": "2026-06-14T18:29:00+08:00",
                        "summary": (
                            "2026-06-14T18:29:00+08:00；记者丨赵甲 钱乙 编辑丨孙丙 "
                            "6月14日，示例媒体注意到，市场上有传言称星河科技首席研究员离职。"
                            "星河科技随后回应称相关消息与事实不符，该研究员仍在正常履职，并提醒外界不要传播未经核实的信息。"
                        ),
                    },
                    {
                        "title": "远航计算发布企业工作站兼容性说明",
                        "source_name": "示例科技媒体",
                        "url": "https://example.invalid/workstation",
                        "published_at": "2026-06-14T10:00:00+08:00",
                        "summary": "远航计算说明了新款企业工作站的软件兼容范围与首批交付计划。",
                    },
                ],
                "sources": [
                    {
                        "name": "示例财经媒体：星河科技回应首席研究员离职传言",
                        "url": "https://example.invalid/company-rumor",
                        "summary": "记者丨赵甲 钱乙 编辑丨孙丙 6月14日，示例媒体注意到相关传言。",
                    }
                ],
            },
            primary_seed_source="data/news-seeds/2026-06-14-ai-news.json",
            now=datetime(2026, 6, 15, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        original_title = build_article(dict(seeds["primary"]))["title"]

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=text-key",
                        "TEXT_MODEL_BATCH_ENABLED=true",
                        "TEXT_MODEL_BATCH_MODEL=qwen3.7-plus",
                    ]
                ),
                encoding="utf-8",
            )
            fake_payload = {
                "title": "记者丨赵甲 钱乙 编辑丨孙丙 6月14日；模型公司发布安全说明丨科技早报",
                "digest": "今天重点看 AI 行业里的公开新闻。",
                "items": [
                    {
                        "index": 1,
                        "detail_heading": "星河科技回应首席研究员离职传言，呼吁勿传不实信息",
                        "reader_summary": (
                            "针对市场流传的首席研究员离职消息，星河科技作出回应，"
                            "明确表示相关内容与事实不符。公司称该研究员仍在正常履职，目前没有来源所说的离职安排，"
                            "公开回应也未涉及其他管理层变动。此次说明针对的是一则未经核实的市场传言，星河科技同时"
                            "提醒外界以公司公开信息为准，不要继续传播缺少证据的说法。"
                        ),
                    }
                ],
            }
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.return_value = FakeTextResponse(
                    status_code=200,
                    payload={"choices": [{"message": {"content": json.dumps(fake_payload, ensure_ascii=False)}}]},
                )
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 15, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        self.assertGreaterEqual(session.post.call_count, 3)
        self.assertIn("星河科技回应", bundle["articles"]["primary"]["title"])
        self.assertNotIn("记者", bundle["articles"]["primary"]["title"])
        self.assertNotIn("编辑", bundle["articles"]["primary"]["title"])

    def test_batch_text_model_does_not_fan_out_failed_chunk_into_item_retries(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    },
                    {
                        "title": "CloudDesk expands AI tools for workspace users",
                        "source_name": "Example Cloud",
                        "url": "https://example.invalid/clouddesk-ai",
                        "published_at": "2026-06-08T17:00:00+08:00",
                        "summary": "Workspace users get new AI writing and data features.",
                    },
                ],
                "sources": [{"name": "Example AI", "url": "https://example.invalid/model", "summary": "summary"}],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=text-key",
                        "TEXT_MODEL_BATCH_ENABLED=true",
                        "TEXT_MODEL_BATCH_MODEL=qwen3.7-plus",
                        "TEXT_MODEL_BATCH_RETRY_COUNT=0",
                        "TEXT_MODEL_BATCH_RETRY_DELAY_SECONDS=0",
                    ]
                ),
                encoding="utf-8",
            )
            first_item_payload = {
                "title": "Northstar AI记忆能力更新；CloudDesk加码办公AI丨科技早报",
                "digest": "今天重点看 AI 工具如何进入真实办公和个人助手场景。",
                "items": [
                    {
                        "index": 1,
                        "detail_heading": "Northstar AI强化Atlas记忆管理，长期助手边界被重新讨论",
                        "reader_summary": (
                            "Northstar AI 正在调整 Atlas Assistant 的记忆能力，让用户更容易管理长期偏好和上下文。"
                            "这类更新会直接影响个人助手的使用边界，也让隐私控制和信息复核变得更重要。"
                        ),
                    }
                ],
            }
            second_item_payload = {
                "items": [
                    {
                        "index": 2,
                        "detail_heading": "CloudDesk扩展办公AI工具，Workspace用户获得更多写作和数据能力",
                        "reader_summary": (
                            "CloudDesk 正在为 Workspace 用户扩展 AI 写作和数据处理能力，相关功能会进入邮件、文档"
                            "和表格等日常办公入口。企业采用时需要同时看权限、复核流程和实际节省的工作量。"
                        ),
                    }
                ]
            }
            responses = [
                FakeTextResponse(status_code=200, payload={"choices": []}),
                FakeTextResponse(
                    status_code=200,
                    payload={"choices": [{"message": {"content": json.dumps(first_item_payload, ensure_ascii=False)}}]},
                ),
                FakeTextResponse(
                    status_code=200,
                    payload={"choices": [{"message": {"content": json.dumps(second_item_payload, ensure_ascii=False)}}]},
                ),
            ]
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.side_effect = responses
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        role_status = bundle["package"]["text_model_generation"]["articles"]["primary"]["roles"]["batch"]
        self.assertEqual(role_status["status"], "fallback_after_text_model_error")
        self.assertEqual(role_status["recovered_chunks"], 0)
        self.assertEqual(role_status["item_retries"], 0)
        self.assertLessEqual(session.post.call_count, 3 + len(bundle["articles"]))

    def test_polish_text_model_is_reviewer_only_and_failure_does_not_rewrite_copy(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 1},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-11",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar Industrial expands its humanoid robotics program",
                        "source_name": "Example Robotics Desk",
                        "url": "https://example.invalid/northstar-robot",
                        "published_at": "2026-06-11T00:27:25+08:00",
                        "summary": "Northstar Industrial will increase research and investment in humanoid robotics as its parts business changes.",
                    }
                ],
                "sources": [
                    {"name": "Example Robotics Desk", "url": "https://example.invalid/northstar-robot", "summary": "summary"}
                ],
            },
            primary_seed_source="data/news-seeds/2026-06-11-ai-news.json",
            now=datetime(2026, 6, 12, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=text-key",
                        "TEXT_MODEL_POLISH_ENABLED=true",
                        "TEXT_MODEL_POLISH_MODEL=polish-model",
                        "TEXT_MODEL_POLISH_RETRY_COUNT=0",
                        "TEXT_MODEL_POLISH_RETRY_DELAY_SECONDS=0",
                    ]
                ),
                encoding="utf-8",
            )
            fallback_payload = {
                "items": [
                    {
                        "index": 1,
                        "detail_heading": "博世传统汽车零部件业务承压，转向人形机器人研发",
                        "reader_summary": (
                            "德国工业巨头博世正把更多资源投向人形机器人。原始消息显示，"
                            "这家公司传统汽车零部件业务承受压力，因此希望在机器人方向寻找新的增长空间。"
                        ),
                    }
                ]
            }
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.side_effect = [
                    FakeTextResponse(
                        status_code=200,
                        payload={"choices": [{"message": {"content": '{"digest":"智能制造日报","sections": ['}}]},
                    ),
                    FakeTextResponse(
                        status_code=200,
                        payload={"choices": [{"message": {"content": '{"digest":"智能制造日报","sections": ['}}]},
                    ),
                    FakeTextResponse(
                        status_code=200,
                        payload={"choices": [{"message": {"content": json.dumps(fallback_payload, ensure_ascii=False)}}]},
                    ),
                ]
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 12, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        role_status = bundle["package"]["text_model_generation"]["articles"]["primary"]["roles"]["polish"]
        item = bundle["articles"]["primary"]["news_items"][0]

        self.assertEqual(role_status["status"], "fallback_after_text_model_error")
        self.assertTrue(role_status["reviewer_only"])
        self.assertNotEqual(item.get("detail_heading"), fallback_payload["items"][0]["detail_heading"])
        self.assertNotEqual(item.get("reader_summary"), fallback_payload["items"][0]["reader_summary"])
        self.assertEqual(session.post.call_count, 1)

    def test_text_model_invalid_json_does_not_trigger_paid_repair_retry(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
                "sources": [{"name": "Example AI", "url": "https://example.invalid/model", "summary": "summary"}],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=text-key",
                        "TEXT_MODEL_SUMMARY_ENABLED=true",
                        "TEXT_MODEL_SUMMARY_MODEL=gemini-3.5-flash",
                        "TEXT_MODEL_SUMMARY_RETRY_COUNT=0",
                        "TEXT_MODEL_SUMMARY_RETRY_DELAY_SECONDS=0",
                    ]
                ),
                encoding="utf-8",
            )
            repaired_payload = {
                "items": [
                    {
                        "index": 1,
                        "fact_card": {
                            "subject": "Northstar AI",
                            "action": "Northstar AI adds memory upgrades to Atlas Assistant",
                            "facts": ["Atlas Assistant memory becomes easier to manage for users"],
                            "numbers": [],
                            "uncertainties": ["The source does not provide rollout details"],
                        },
                    }
                ]
            }
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.side_effect = [
                    FakeTextResponse(
                        status_code=200,
                        payload={"choices": [{"message": {"content": '{"items": [bad json'}}]},
                    ),
                    FakeTextResponse(
                        status_code=200,
                        payload={"choices": [{"message": {"content": json.dumps(repaired_payload, ensure_ascii=False)}}]},
                    ),
                ]
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        role_status = bundle["package"]["text_model_generation"]["articles"]["primary"]["roles"]["summary"]
        self.assertEqual(role_status["status"], "fallback_after_text_model_error")
        self.assertNotIn("json_repair_retries", role_status)
        self.assertEqual(session.post.call_count, 1)

    def test_build_content_package_falls_back_when_text_model_fails(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
                "sources": [{"name": "Example AI", "url": "https://example.invalid/model", "summary": "summary"}],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "TEXT_MODEL_API_KEY=text-key\nTEXT_MODEL_BATCH_ENABLED=true\nTEXT_MODEL_BATCH_MODEL=batch-model\n",
                encoding="utf-8",
            )
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.return_value = FakeTextResponse(status_code=200, payload={"choices": []})
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        self.assertNotEqual(bundle["articles"]["primary"]["title"], "")
        self.assertEqual(
            bundle["package"]["text_model_generation"]["articles"]["primary"]["roles"]["batch"]["status"],
            "fallback_after_text_model_error",
        )
        self.assertFalse(bundle["package"]["copy_quality"]["ok"])
        self.assertIn("primary", bundle["package"]["copy_quality"]["failed_roles"])
        self.assertEqual(bundle["reviews"]["primary"]["risk_level"], "medium")
        self.assertFalse(bundle["reviews"]["primary"]["wechat_ready"])

    def test_batch_model_enhances_ninth_visible_news_item(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "draft_only"},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        news_items = [
            {
                "title": f"Example AI product update number {index}",
                "source_name": "Example AI",
                "url": f"https://example.com/news-{index}",
                "published_at": "2026-06-08T16:00:00+08:00",
                "summary": (
                    f"Example AI released product update number {index} for enterprise workflow testing. "
                    "Pilot users can connect documents, task lists, and internal knowledge bases in one view. "
                    "Administrators can set access scope and require human confirmation before key actions."
                ),
            }
            for index in range(1, 10)
        ]
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": news_items,
                "sources": [{"name": "Example AI", "url": "https://example.com/model", "summary": "summary"}],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "TEXT_MODEL_API_KEY=text-key\nTEXT_MODEL_BATCH_ENABLED=true\nTEXT_MODEL_BATCH_MODEL=batch-model\n",
                encoding="utf-8",
            )

            def batch_payload(*, call_index: int) -> dict[str, object]:
                start = (call_index - 1) * 3 + 1
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "title": "AI产品更新；模型进入工作流丨科技早报" if start == 1 else "",
                                        "digest": "多条 AI 产品和模型应用新闻进入真实工作流。" if start == 1 else "",
                                        "items": [
                                            {
                                                "index": item_index,
                                                "detail_heading": f"Example AI第{item_index}项产品更新进入企业工作场景",
                                                "reader_summary": (
                                                    f"Example AI 发布第{item_index}项产品更新，面向企业工作流展开小范围测试，"
                                                    "首批用户可以在同一界面连接文档、任务列表和内部知识库。新版本允许管理员"
                                                    "设置数据访问范围，并在系统执行关键操作前要求人工确认，测试团队也能查看"
                                                    "完整执行步骤。当前能力尚未全面开放，后续范围将根据试用反馈和权限配置情况调整。"
                                                ),
                                            }
                                            for item_index in range(start, min(start + 3, 10))
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }

            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                call_count = {"value": 0}

                def post(*_args: object, **_kwargs: object) -> FakeTextResponse:
                    call_count["value"] += 1
                    return FakeTextResponse(status_code=200, payload=batch_payload(call_index=call_count["value"]))

                session.post.side_effect = post
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        ninth_item = bundle["articles"]["primary"]["news_items"][8]
        self.assertIn("Example AI第9项产品更新", ninth_item["detail_heading"])
        self.assertIn("关键操作前要求人工确认", ninth_item["reader_summary"])
        self.assertEqual(
            bundle["package"]["text_model_generation"]["articles"]["primary"]["roles"]["batch"]["successful_chunks"],
            3,
        )

    def test_text_model_role_failure_blocks_auto_publish_package(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "auto_publish_low_risk", "allow_low_risk_auto_publish": True},
        }
        selected = {
            "topic": "普通人如何用 AI 提高工作效率",
            "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
        }
        seeds = build_daily_article_seeds(
            schedule_config=schedule,
            selected_seed=selected,
            seed_source="config/topics.yaml",
            primary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
                "sources": [{"name": "Example AI", "url": "https://example.invalid/model", "summary": "summary"}],
            },
            primary_seed_source="data/news-seeds/2026-06-08-ai-news.json",
            secondary_seed={
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar Industrial and Orion Robotics build an AI factory",
                        "source_name": "Example Manufacturer Blog",
                        "url": "https://example.invalid/northstar-orion",
                        "published_at": "2026-06-08T11:00:00+08:00",
                        "summary": "AI factory, robotics, autonomous driving and data center technologies.",
                    }
                ],
                "sources": [
                    {
                        "name": "Example Manufacturer Blog",
                        "url": "https://example.invalid/northstar-orion",
                        "summary": "summary",
                    }
                ],
            },
            secondary_seed_source="data/news-seeds/2026-06-08-manufacturing-news.json",
            now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "TEXT_MODEL_API_KEY=text-key",
                        "TEXT_MODEL_SUMMARY_ENABLED=true",
                        "TEXT_MODEL_SUMMARY_MODEL=summary-model",
                        "TEXT_MODEL_BATCH_ENABLED=true",
                        "TEXT_MODEL_BATCH_MODEL=batch-model",
                        "TEXT_MODEL_POLISH_ENABLED=true",
                        "TEXT_MODEL_POLISH_MODEL=polish-model",
                    ]
                ),
                encoding="utf-8",
            )
            with patch("src.integrations.text_model_client.requests.Session") as session_class:
                session = session_class.return_value
                session.post.return_value = FakeTextResponse(status_code=200, payload={"choices": []})
                provisional = decide_publish_action(
                    schedule,
                    {"risk_level": "medium", "flags": {"fact_uncertain": True}, "wechat_ready": False},
                )
                bundle = build_content_package(
                    schedule_config=schedule,
                    article_seeds=seeds,
                    safety_config={"blocked_claim_types": []},
                    publish_decision=provisional,
                    output_dir=temp_dir,
                    seed_source="config/topics.yaml",
                    already_published_today=False,
                    env_path=env_path,
                    now=datetime(2026, 6, 9, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                )

        package_review = aggregate_package_review(bundle["reviews"])
        decision = decide_publish_action(schedule, package_review)

        required_roles = bundle["package"]["copy_quality"]["required_text_model_roles"]
        self.assertEqual(required_roles, ["summary", "batch", "polish"])
        self.assertFalse(bundle["package"]["copy_quality"]["ok"])
        self.assertEqual(bundle["package"]["package_risk_level"], "medium")
        self.assertFalse(bundle["package"]["wechat_ready"])
        self.assertEqual(decision.action, "hold")
        self.assertFalse(decision.allowed)


class FakeImageResponse:
    def __init__(self, *, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class FakeTextResponse(FakeImageResponse):
    pass


class FakeSourceImageResponse:
    def __init__(self, body: bytes, *, content_type: str = "image/jpeg") -> None:
        self.status_code = 200
        self.headers = {"Content-Type": content_type}
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        yield self._body


if __name__ == "__main__":
    unittest.main()
