from __future__ import annotations

import unittest

from src.pipeline.content_engine import (
    ARTICLE_SCHEMA_VERSION,
    _briefing_news_body_paragraphs,
    _briefing_news_context_text,
    _briefing_news_reader_text,
    _specific_headline_clause,
    _trim_headline_clause,
    build_article,
    news_detail_heading,
    news_title_element,
    render_markdown,
    render_wechat_html,
    validate_article,
)


class ContentEngineTest(unittest.TestCase):
    def test_headline_clause_rejects_dangling_fragments_but_keeps_complete_product_names(self) -> None:
        self.assertFalse(_specific_headline_clause("消息称星河科技计划 9"))
        self.assertFalse(_specific_headline_clause("首个具身视频基模开源！L"))
        self.assertEqual(_trim_headline_clause("Cube Sandbox正式支持Arm架构！后续说明", 24), "Cube Sandbox正式支持Arm架构")
        self.assertTrue(_specific_headline_clause("Atlas模型进入企业办公助手"))
        self.assertTrue(_specific_headline_clause("星河科技开放Muse Spark模型API"))

    def test_detail_heading_keeps_text_after_chinese_colon(self) -> None:
        heading = news_detail_heading(
            {
                "title": "远程实验：研究团队操控星云 G1 人形机器人完成精细任务",
                "summary": "研究团队使用两台星云 G1 完成远程协同操作。",
                "source_name": "Example Technology Desk",
            }
        )

        self.assertEqual(heading, "远程实验：研究团队操控星云 G1 人形机器人完成精细任务")

    def test_model_reader_summary_between_181_and_220_chars_is_not_truncated(self) -> None:
        summary = (
            "Northstar AI发布面向企业工作流的新能力，允许智能代理在多个应用和文件之间连续执行任务。"
            "系统会根据用户设定的目标保留项目上下文，并在长时间运行中记录关键步骤与结果。"
            "企业部署时仍需明确权限范围、人工复核节点和失败后的接管方式，以便把效率提升与责任边界同时纳入日常管理。"
            "对于跨部门项目，管理员还要记录数据从哪里进入、哪些应用可以被调用，以及任务结束后如何审计关键操作。"
        )
        self.assertGreater(len(summary), 180)
        self.assertLessEqual(len(summary), 220)

        paragraphs = _briefing_news_body_paragraphs(
            {
                "title": "Northstar AI发布企业工作流能力",
                "source_name": "Example AI Desk",
                "summary": "Northstar AI发布企业工作流能力。",
                "detail_heading": "Northstar AI发布企业工作流能力，智能代理可跨应用连续执行任务",
                "reader_summary": summary,
            }
        )

        self.assertEqual(paragraphs[0], summary)
        self.assertEqual(len(paragraphs), 1)
        self.assertNotIn("它到底能省掉哪一步", "\n".join(paragraphs))
        self.assertNotIn("不同选择会决定它扩散得快不快", "\n".join(paragraphs))

    def test_smart_manufacturing_context_varies_by_robot_event(self) -> None:
        production_item = {
            "title": "北辰工业三代人形机器人初步定型，供应商开始排产",
            "summary": "机器人供应商收到零件采购指引。",
            "source_name": "Example Manufacturing Desk",
        }
        surgery_item = {
            "title": "星云G1完成远程精细操作实验",
            "summary": "研究团队远程操控两台机器人完成精细操作。",
            "source_name": "Example Research Desk",
        }
        video_item = {
            "title": "具身智能MoE视频模型开源",
            "summary": "视频模型用于机器人环境理解与训练。",
            "source_name": "Example Robotics Desk",
        }
        production = _briefing_news_context_text(production_item)
        surgery = _briefing_news_context_text(surgery_item)
        video_model = _briefing_news_context_text(video_item)

        self.assertEqual(len({production, surgery, video_model}), 3)
        self.assertTrue("供应链" in production or "供应商" in production)
        self.assertTrue("远程" in surgery or "遥操作" in surgery)
        self.assertIn("视频", video_model)
        self.assertNotIn("机器人新闻真正有价值的部分", production + surgery + video_model)
        reader_texts = {
            _briefing_news_reader_text(production_item),
            _briefing_news_reader_text(surgery_item),
            _briefing_news_reader_text(video_item),
        }
        self.assertEqual(len(reader_texts), 3)

    def test_watchlist_uses_complete_heading_instead_of_truncated_label(self) -> None:
        article = build_article(
            {
                "topic": "智能制造日报",
                "column": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "target_date": "2026-07-09",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "远程实验：研究团队操控星云 G1 人形机器人完成精细任务",
                        "source_name": "Example Technology Desk",
                        "summary": "研究团队使用两台星云 G1 完成远程协同操作。",
                    }
                ],
                "sources": [
                    {"name": "Example Technology Desk", "url": "https://example.invalid/research", "summary": "summary"}
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("远程实验：研究团队操控星云 G1 人形机器人完成精细任务", html)
        self.assertIn("来源：Example Technology Desk", html)
        self.assertNotIn(">远程实验：研究团队操控<", html)

    def test_generic_analysis_does_not_repeat_standalone_watch_templates(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "科技早报",
                "target_date": "2026-07-09",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": f"科技公司{index}发布新的AI服务",
                        "source_name": f"来源{index}",
                        "url": f"https://example.com/{index}",
                        "summary": "公司发布新的服务能力，面向企业和开发者开放。",
                        "detail_heading": f"科技公司{index}发布新的AI服务，面向企业开放使用",
                        "reader_summary": f"科技公司{index}发布新的AI服务，首批能力已经向企业客户开放。",
                    }
                    for index in range(1, 4)
                ],
                "sources": [{"name": "公开来源", "url": "https://example.com", "summary": "summary"}],
            }
        )

        html = render_wechat_html(article)

        self.assertNotIn("接下来要看使用门槛、责任边界和持续交付能力。", html)
        self.assertNotIn("这些条件会区分一次发布和真实进展", html)
        self.assertNotIn("如果这些问题有明确答案，它才更可能变成用户能感知的产品变化。", html)

    def test_robot_only_manufacturing_takeaway_does_not_add_low_altitude_topic(self) -> None:
        article = build_article(
            {
                "topic": "智能制造日报",
                "column": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "target_date": "2026-07-09",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "人形机器人进入量产验证",
                        "source_name": "产业媒体",
                        "url": "https://example.com/robot",
                        "summary": "供应商开始验证机器人关键零部件和整机节拍。",
                    }
                ],
                "sources": [{"name": "产业媒体", "url": "https://example.com/robot", "summary": "summary"}],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("人形机器人与具身训练", html)
        self.assertNotIn("低空装备", html)

    def test_build_article_outputs_schema_v1(self) -> None:
        article = build_article(
            {
                "topic": "AI 自动化入门",
                "column": "AI 新手教学",
                "audience": "新手读者",
                "topic_reason": "测试",
                "risk_tags": [],
                "sources": [
                    {
                        "name": "本地选题配置",
                        "url": "config/topics.yaml",
                        "summary": "测试来源",
                    }
                ],
            }
        )

        self.assertEqual(article["schema_version"], ARTICLE_SCHEMA_VERSION)
        self.assertEqual(article["topic"], "AI 自动化入门")
        self.assertEqual(article["column"], "AI 新手教学")
        self.assertGreater(article["word_count_estimate"], 0)
        self.assertEqual(validate_article(article), [])

    def test_validate_article_detects_missing_required_fields(self) -> None:
        errors = validate_article({"schema_version": ARTICLE_SCHEMA_VERSION})

        self.assertTrue(any("title is required" in error for error in errors))
        self.assertTrue(any("sections must be a non-empty list" in error for error in errors))

    def test_renderers_include_title_and_sources(self) -> None:
        article = build_article(
            {
                "topic": "普通人如何用 AI 提高工作效率",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "", "summary": "summary"}],
            }
        )

        markdown = render_markdown(article)
        html = render_wechat_html(article)

        self.assertIn(article["title"], markdown)
        self.assertIn("参考来源", markdown)
        self.assertIn("<section", html)
        self.assertIn("style=", html)
        self.assertIn("border-left:4px solid", html)
        self.assertIn("source", html)

    def test_news_seed_builds_yesterday_ai_roundup(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-02",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-02T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
                "sources": [
                    {
                        "name": "Example AI：New AI model ships",
                        "url": "https://example.invalid/model",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
            }
        )

        self.assertIn("Atlas记忆能力", article["title"])
        self.assertIn("科技早报", article["title"])
        self.assertEqual(article["news_status"], "ok")
        all_paragraphs = "\n".join(paragraph for section in article["sections"] for paragraph in section["paragraphs"])
        self.assertIn("记忆能力", all_paragraphs)
        self.assertIn("只保留能说清主体、动作和来源的变化", article["sections"][0]["paragraphs"][0])

    def test_news_seed_wechat_html_uses_briefing_layout(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-04",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-04T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    },
                    {
                        "title": "DevForge rebuilds its CLI for AI developers",
                        "source_name": "Example Dev",
                        "url": "https://example.invalid/devforge-cli",
                        "published_at": "2026-06-04T17:00:00+08:00",
                        "summary": "The developer workflow gets a cleaner command line.",
                    },
                ],
                "sources": [
                    {
                        "name": "Example AI：Northstar AI adds memory upgrades to Atlas Assistant",
                        "url": "https://example.invalid/model",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    }
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("科技早报", html)
        self.assertIn("今日主线", html)
        self.assertIn("今日看点", html)
        self.assertIn("今天最值得盯的一点", html)
        self.assertIn("AI 产品与平台", html)
        self.assertIn("color:#ea5414", html)
        self.assertIn("Atlas记忆能力", html)
        self.assertNotIn("先给你一句话", html)

    def test_news_briefing_labels_biodefense_without_truncation(self) -> None:
        label = news_title_element(
            {
                "title": "Biodefense action plan for resilient systems",
                "source_name": "Example AI Desk",
                "summary": "An action plan for AI-powered biological resilience",
            }
        )

        self.assertEqual(label, "AI生物安全")

    def test_news_detail_heading_uses_informative_sentence(self) -> None:
        heading = news_detail_heading(
            {
                "title": "Northstar AI launches Atlas model updates for enterprise AI agents",
                "source_name": "Example AI Desk",
                "summary": "Atlas models are designed to help companies build AI agents.",
            }
        )

        self.assertIn("Northstar", heading)
        self.assertIn("Atlas", heading)
        self.assertGreaterEqual(len(heading), 18)

    def test_news_detail_heading_avoids_blind_truncation(self) -> None:
        item = {
            "title": "Are AI chatbots creating too much customer-service confusion for users?",
            "source_name": "Example Technology Desk",
            "summary": "AI chatbots are changing customer service workflows.",
        }

        self.assertEqual(news_title_element(item), "客服AI体验")
        self.assertIn("客服AI体验", news_detail_heading(item))
        self.assertNotIn("Are AI", news_detail_heading(item))

    def test_news_detail_heading_maps_wechat_phone_agent(self) -> None:
        item = {
            "title": "移动助手向手机厂商开放设备协作接口｜焦点分析",
            "source_name": "Example Technology Desk",
            "summary": "Agent-to-Agent助手能力进入手机生态。",
        }

        self.assertIn("移动助手", news_title_element(item))
        self.assertIn("手机厂商", news_detail_heading(item))

    def test_briefing_uses_model_heading_and_reader_summary(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-09",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-09T08:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                        "detail_heading": "Northstar AI强化Atlas记忆管理，长期助手边界被重新讨论",
                        "reader_summary": (
                            "Northstar AI 正在调整 Atlas Assistant 的记忆能力，让用户更容易管理长期偏好和上下文。"
                            "这类更新会直接影响个人助手的使用边界，也会让隐私控制和信息复核变得更重要。"
                        ),
                    }
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertEqual(
            news_detail_heading(article["news_items"][0]),
            "Northstar AI强化Atlas记忆管理，长期助手边界被重新讨论",
        )
        self.assertIn("【Northstar AI强化Atlas记忆管理，长期助手边界被重新讨论】", html)
        self.assertIn("Northstar AI 正在调整 Atlas Assistant 的记忆能力", html)
        self.assertIn("长期助手", html)
        self.assertNotIn("哪些信息被保存", html)
        self.assertNotIn("定期检查记忆设置", html)
        self.assertNotIn("这类消息的重点不是", html)
        self.assertNotIn("谁使用、谁付费、谁复核", html)

    def test_primary_briefing_adds_deeper_news_body(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-10",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI confidentially files S-1 draft with SEC",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/northstar-s1",
                        "published_at": "2026-06-10T08:00:00+08:00",
                        "summary": "Northstar AI has confidentially submitted a draft registration statement to the SEC.",
                        "reader_summary": (
                            "Northstar AI 确认已向美国 SEC 提交保密 S-1 草案，"
                            "这意味着外界会更关注它的收入结构、算力成本和治理安排。"
                        ),
                    }
                ],
            }
        )

        paragraphs = _briefing_news_body_paragraphs(article["news_items"][0], article=article)

        self.assertEqual(len(paragraphs), 1)
        self.assertIn("向美国 SEC 提交保密 S-1 草案", paragraphs[0])
        self.assertIn("收入结构", paragraphs[0])

    def test_primary_briefing_deeper_body_avoids_follow_up_cliches(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-10",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Satellite AI terminal company plans manufacturing expansion",
                        "source_name": "Example AI",
                        "url": "https://example.com/satellite-ai",
                        "published_at": "2026-06-10T08:00:00+08:00",
                        "summary": "Space infrastructure, terminal equipment, and manufacturing capacity support the project.",
                    }
                ],
            }
        )

        paragraphs = _briefing_news_body_paragraphs(article["news_items"][0], article=article)
        text = "\n".join(paragraphs)

        self.assertNotIn("后续看", text)
        self.assertNotIn("后续看点", text)
        self.assertNotIn("从概念走向", text)

    def test_briefing_ignores_banned_model_copy(self) -> None:
        item = {
            "title": "Northstar AI launches Atlas model updates for enterprise AI agents",
            "source_name": "Example AI Desk",
            "summary": "Atlas models are designed to help companies build AI agents.",
            "detail_heading": "这条消息值得放进今天的科技早报",
            "reader_summary": "读这类消息时，可以把注意力放在三个点：谁使用、谁付费、谁复核。",
        }

        heading = news_detail_heading(item)

        self.assertNotEqual(heading, item["detail_heading"])
        self.assertIn("Northstar", heading)

    def test_briefing_ignores_follow_up_cliche_model_copy(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-10",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "ContextWorks raises $24M to give AI agents business context",
                        "source_name": "Example Venture Desk",
                        "url": "https://example.invalid/contextworks",
                        "published_at": "2026-06-10T08:00:00+08:00",
                        "summary": "ContextWorks raised $24 million to give enterprise AI agents more business context.",
                        "detail_heading": "后续看点是ContextWorks能否拿到可验证的企业客户",
                        "reader_summary": "后续看点是ContextWorks能不能拿到可验证的企业客户，以及上下文供给方式是否会被大平台直接内置。",
                    }
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertNotIn("后续看点", html)
        self.assertNotEqual(news_detail_heading(article["news_items"][0]), article["news_items"][0]["detail_heading"])

    def test_lawsuits_are_not_treated_as_aws_news(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-04",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Courts test review workflows for AI-generated lawsuits",
                        "source_name": "Example Research Review",
                        "url": "https://example.invalid/ai-lawsuits",
                        "published_at": "2026-06-04T18:50:18+08:00",
                        "summary": "Courts are seeing more AI-generated lawsuits and need better review workflows.",
                    }
                ],
                "sources": [
                    {
                        "name": "Example Research Review：Courts test review workflows for AI-generated lawsuits",
                        "url": "https://example.invalid/ai-lawsuits",
                        "summary": "Courts are seeing more AI-generated lawsuits and need better review workflows.",
                    }
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("AI司法应用", html)
        self.assertIn("责任边界", html)
        self.assertIn("谁负责", html)
        self.assertNotIn("Northstar AI 正在进入企业熟悉的云平台货架", html)

    def test_newsletter_digest_item_is_not_displayed_as_separate_news(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-04",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Daily Digest: AI-generated lawsuits and virtual power plants for data centers",
                        "source_name": "Example Research Review",
                        "url": "https://example.invalid/daily-digest",
                        "published_at": "2026-06-04T20:10:00+08:00",
                        "summary": "A newsletter digest covering lawsuits and virtual power plants.",
                    },
                    {
                        "title": "Virtual power plants begin supplying a regional data center",
                        "source_name": "Example Research Review",
                        "url": "https://example.invalid/virtual-power",
                        "published_at": "2026-06-04T00:51:38+08:00",
                        "summary": "Virtual power plants could help provide energy for data centers.",
                    },
                ],
                "sources": [
                    {
                        "name": "Example Research Review：Daily Digest: AI-generated lawsuits and virtual power plants for data centers",
                        "url": "https://example.invalid/daily-digest",
                        "summary": "A newsletter digest covering lawsuits and virtual power plants.",
                    },
                    {
                        "name": "Example Research Review：Virtual power plants begin supplying a regional data center",
                        "url": "https://example.invalid/virtual-power",
                        "summary": "Virtual power plants could help provide energy for data centers.",
                    },
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("虚拟电厂", article["title"])
        self.assertIn("供电", article["title"])
        self.assertIn("虚拟电厂开始供电", html)
        self.assertNotIn("The Download: AI-generated lawsuits", html)

    def test_promotional_feed_item_is_not_displayed_as_news(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "示例效率工具体验记录",
                        "source_name": "示例媒体",
                        "url": "https://example.invalid/promotional-item",
                        "published_at": "2026-06-08T10:00:00+08:00",
                        "summary": "这是一段产品体验文字。欢迎关注示例媒体官方微信公众号，更多内容稍后更新。",
                    },
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    },
                ],
                "sources": [
                    {
                        "name": "示例媒体：示例效率工具体验记录",
                        "url": "https://example.invalid/promotional-item",
                        "summary": "这是一段产品体验文字。欢迎关注示例媒体官方微信公众号，更多内容稍后更新。",
                    },
                    {
                        "name": "Example AI Desk：Northstar AI adds memory upgrades to Atlas Assistant",
                        "url": "https://example.invalid/model",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    },
                ],
            }
        )

        html = render_wechat_html(article)
        markdown = render_markdown(article)

        self.assertIn("Atlas记忆能力", article["title"])
        self.assertIn("Atlas记忆能力", html)
        for blocked in ("产品体验文字", "欢迎关注", "示例效率工具", "官方微信公众号", "更多内容"):
            self.assertNotIn(blocked, article["title"])
            self.assertNotIn(blocked, html)
            self.assertNotIn(blocked, markdown)

    def test_briefing_fallback_does_not_repeat_static_attention_sentence(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-05",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Enterprise AI dashboard launches for retail teams",
                        "source_name": "Example Tech",
                        "url": "https://example.com/retail-ai",
                        "published_at": "2026-06-05T10:00:00+08:00",
                        "summary": "Retail teams are trying new AI dashboards for daily planning.",
                    },
                    {
                        "title": "Startup pilots AI planning software with logistics customers",
                        "source_name": "Example Startup",
                        "url": "https://example.com/logistics-ai",
                        "published_at": "2026-06-05T11:00:00+08:00",
                        "summary": "A startup is testing AI planning software with logistics customers.",
                    },
                ],
                "sources": [
                    {
                        "name": "Example Tech：Enterprise AI dashboard launches for retail teams",
                        "url": "https://example.com/retail-ai",
                        "summary": "Retail teams are trying new AI dashboards for daily planning.",
                    }
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertNotIn("这条消息值得关注，因为它反映了 AI 正在进入更具体的产品、行业和工作流程", html)
        self.assertNotIn("这条消息值得关注", html)
        self.assertNotIn("它值得放进早报", html)
        self.assertNotIn("这条消息值得放进今天的科技早报", html)
        self.assertNotIn("成为今天科技早报的重点动态", html)
        self.assertNotIn("换个角度看", html)
        self.assertNotIn("读这类消息时，可以把注意力放在三个点", html)
        self.assertIn("零售AI看板", html)
        self.assertIn("物流AI规划", html)
        self.assertNotIn("Retail teams are trying new AI dashboards", html)
        self.assertNotIn("A startup is testing AI planning software", html)
        self.assertNotIn("读这条不用先追术语", html)
        self.assertNotIn("谁使用、谁付费、谁复核", html)

    def test_briefing_title_maps_generic_english_startup_news_to_chinese(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-25",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "AI startup raises funding for enterprise workflow agents",
                        "source_name": "Example Startup",
                        "url": "https://example.com/workflow-agent",
                        "published_at": "2026-06-25T09:00:00+08:00",
                        "summary": "The company raised funding to build AI agents for business workflows.",
                    },
                    {
                        "title": "Retail analytics dashboard launches AI planning tools",
                        "source_name": "Example Retail",
                        "url": "https://example.com/retail-dashboard",
                        "published_at": "2026-06-25T10:00:00+08:00",
                        "summary": "Retail teams are trying new AI dashboards for daily planning.",
                    },
                    {
                        "title": "Logistics vendor pilots AI planning workflow with customers",
                        "source_name": "Example Logistics",
                        "url": "https://example.com/logistics-planning",
                        "published_at": "2026-06-25T11:00:00+08:00",
                        "summary": "A vendor is testing AI planning systems with logistics customers.",
                    },
                ],
                "sources": [],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("企业工作流能力完成融资", article["title"])
        self.assertIn("零售AI看板", article["title"])
        for blocked in ("AI startup", "startup", "enterprise w", "Retail analytics dashboard", "Logistics vendor"):
            self.assertNotIn(blocked, article["title"])
            self.assertNotIn(blocked, html)
        self.assertIn("企业工作流能力完成融资", html)
        self.assertIn("零售AI看板", html)

    def test_briefing_generic_context_varies_by_news_item(self) -> None:
        items = [
            {
                "title": "Enterprise AI dashboard launches for retail teams",
                "source_name": "Example Tech",
                "url": "https://example.com/retail-ai",
                "published_at": "2026-06-05T10:00:00+08:00",
                "summary": "Retail teams are trying new AI dashboards for daily planning.",
            },
            {
                "title": "Startup pilots AI planning workflow with logistics customers",
                "source_name": "Example Startup",
                "url": "https://example.com/logistics-ai",
                "published_at": "2026-06-05T11:00:00+08:00",
                "summary": "A startup is testing AI planning systems with logistics customers.",
            },
            {
                "title": "Analytics vendor adds AI budget controls for finance teams",
                "source_name": "Example SaaS",
                "url": "https://example.com/budget-ai",
                "published_at": "2026-06-05T12:00:00+08:00",
                "summary": "Finance teams can set limits before using AI analysis features.",
            },
            {
                "title": "AI assistant app reaches small business stores",
                "source_name": "Example App",
                "url": "https://example.com/store-ai",
                "published_at": "2026-06-05T13:00:00+08:00",
                "summary": "Small shops are using a new assistant app for everyday operations.",
            },
        ]

        contexts = [_briefing_news_context_text(item) for item in items]
        joined = "\n".join(contexts)
        openings = [context[:8] for context in contexts]

        self.assertEqual(len(contexts), len(set(contexts)))
        self.assertGreaterEqual(len(set(openings)), 3)
        self.assertNotIn("换个角度看", joined)
        self.assertNotIn("它值得放进早报", joined)
        self.assertNotIn("这条消息值得放进今天的科技早报", joined)
        self.assertNotIn("成为今天科技早报的重点动态", joined)
        self.assertNotIn("读这类消息时，可以把注意力放在三个点", joined)

    def test_briefing_generic_reader_text_avoids_fixed_three_point_phrase(self) -> None:
        items = [
            {
                "title": "AI assistant app reaches small business stores",
                "source_name": "Example App",
                "url": "https://example.com/store-ai",
                "published_at": "2026-06-05T13:00:00+08:00",
                "summary": "Small shops are using a new assistant app for everyday operations.",
            },
            {
                "title": "Cloud vendor adds AI budget controls for finance teams",
                "source_name": "Example Cloud",
                "url": "https://example.com/budget-ai",
                "published_at": "2026-06-05T14:00:00+08:00",
                "summary": "Finance teams can set spending limits before using AI analysis features.",
            },
            {
                "title": "Robotics startup pilots warehouse picking system",
                "source_name": "Example Robotics",
                "url": "https://example.com/warehouse-ai",
                "published_at": "2026-06-05T15:00:00+08:00",
                "summary": "The company is testing robots with logistics customers.",
            },
            {
                "title": "AI search company reports paid enterprise growth",
                "source_name": "Example Search",
                "url": "https://example.com/search-ai",
                "published_at": "2026-06-05T16:00:00+08:00",
                "summary": "Enterprise subscriptions and revenue are growing.",
            },
            {
                "title": "Design platform introduces AI review notes",
                "source_name": "Example Design",
                "url": "https://example.com/design-ai",
                "published_at": "2026-06-05T17:00:00+08:00",
                "summary": "Product teams can review drafts with AI-generated notes.",
            },
        ]

        reader_notes = [_briefing_news_reader_text(item) for item in items]
        joined = "\n".join(reader_notes)

        self.assertNotIn("读这类消息时，可以把注意力放在三个点", joined)
        self.assertNotIn("这样新闻就不只是热闹，而会变成判断工具的线索", joined)
        self.assertGreaterEqual(len(set(reader_notes)), 3)

    def test_daily_practical_takeaway_does_not_render_as_briefing(self) -> None:
        article = build_article(
            {
                "topic": "把重复任务交给 AI 前，先写清这四行",
                "title": "把重复任务交给 AI 前，先写清这四行",
                "column": "AI 工具箱",
                "source_policy": "daily_practical_takeaway",
                "target_date": "2026-06-07",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI unveils controlled mode for enterprise AI",
                        "source_name": "Example AI",
                        "url": "https://example.invalid/controlled-mode",
                        "published_at": "2026-06-07T10:00:00+08:00",
                        "summary": "An enterprise AI product adds stricter review and control steps.",
                    }
                ],
                "sources": [
                    {
                        "name": "AI 工具箱模板",
                        "url": "config/topics.yaml",
                        "summary": "第三篇副文章使用固定工具箱结构生成。",
                    }
                ],
            }
        )
        article["inline_images"] = [
            {
                "placeholder": "{{ARTICLE_INLINE_TERTIARY_1}}",
                "alt": "四行任务模板的流程配图",
            }
        ]

        html = render_wechat_html(article)

        self.assertIn("AI 工具箱", html)
        self.assertIn("四行任务模板", html)
        self.assertIn("{{ARTICLE_INLINE_TERTIARY_1}}", html)
        self.assertIn("AI 生成配图 · 辅助理解任务流程", html)
        self.assertNotIn("科技早报", html)
        self.assertNotIn("今日看点", html)
        self.assertNotIn("它值得放进早报", html)

    def test_briefing_title_uses_headline_worthy_news_not_short_fragments(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-06",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "公募基金增加先进制造配置，行业协会发布季度数据",
                        "source_name": "Example Finance Desk",
                        "url": "https://example.invalid/funds",
                        "published_at": "2026-06-06T08:00:00+08:00",
                        "summary": "行业协会发布公募基金配置先进制造领域的季度数据。",
                    },
                    {
                        "title": "北辰能源推出算力中心供电站，试点电力成本下降30%",
                        "source_name": "Example Business Desk",
                        "url": "https://example.invalid/compute-power",
                        "published_at": "2026-06-06T09:00:00+08:00",
                        "summary": "北辰能源在示范园区部署模块化供电站，为算力中心降低峰值用电成本。",
                    },
                    {
                        "title": "行业协会提醒公募基金避免概念炒作和通道空转",
                        "source_name": "Example Finance Desk",
                        "url": "https://example.invalid/industry-guidance",
                        "published_at": "2026-06-06T09:30:00+08:00",
                        "summary": "行业协会发布面向基金机构的风险提示。",
                    },
                    {
                        "title": "云帆平台推出企业AI云编排服务，按任务效果计费",
                        "source_name": "Example Technology Desk",
                        "url": "https://example.invalid/cloud-orchestration",
                        "published_at": "2026-06-06T10:00:00+08:00",
                        "summary": "云帆平台把模型调用、审核和工作流编排整合进企业控制台。",
                    },
                    {
                        "title": "面向开发者的AI应用创新大赛正式启动",
                        "source_name": "Example Community Desk",
                        "url": "https://example.invalid/contest",
                        "published_at": "2026-06-06T11:00:00+08:00",
                        "summary": "活动面向开发者征集概念方案。",
                    },
                    {
                        "title": "远航汽车将智能体接入座舱测试流程",
                        "source_name": "Example Mobility Desk",
                        "url": "https://example.invalid/vehicle-agent",
                        "published_at": "2026-06-06T12:00:00+08:00",
                        "summary": "远航汽车开始在封闭测试车队验证智能体座舱能力。",
                    },
                    {
                        "title": "Harbor Compute signs a multi-year capacity agreement",
                        "source_name": "Example Infrastructure Desk",
                        "url": "https://example.invalid/capacity-agreement",
                        "published_at": "2026-06-06T13:00:00+08:00",
                        "summary": "Harbor Compute will purchase reserved accelerator capacity under a multi-year agreement.",
                    },
                ],
                "sources": [],
            }
        )

        self.assertIn("科技早报", article["title"])
        strong_subjects = ("北辰能源", "云帆平台", "远航汽车", "Harbor")
        self.assertGreaterEqual(sum(subject in article["title"] for subject in strong_subjects), 2)
        self.assertNotIn("公募", article["title"])
        self.assertNotIn("行业协会", article["title"])
        self.assertNotIn("创新大赛", article["title"])
        self.assertNotIn("，丨科技早报", article["title"])
        self.assertLessEqual(len(article["title"]), 64)

    def test_june_9_briefing_title_maps_english_source_titles_to_chinese(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "A broad vision for future AI systems",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/future-vision",
                        "published_at": "2026-06-08T08:00:00+08:00",
                        "summary": "A general statement about future systems without a product or delivery date.",
                    },
                    {
                        "title": "Introducing an economic research exchange",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/economic-research",
                        "published_at": "2026-06-08T09:00:00+08:00",
                        "summary": "A research exchange will study AI and the economy.",
                    },
                    {
                        "title": "Confidential submission of draft S-1 to the SEC",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/company-s1",
                        "published_at": "2026-06-08T10:00:00+08:00",
                        "summary": "A fictional company confirms a confidential draft S-1 submission to the SEC.",
                    },
                    {
                        "title": "跨境支付服务接入企业采购和结算流程",
                        "source_name": "Example Business Desk",
                        "url": "https://example.invalid/payment-workflow",
                        "published_at": "2026-06-08T11:00:00+08:00",
                        "summary": "首批商户可在同一工作流中完成采购付款和跨境收款。",
                    },
                    {
                        "title": "Open-source community releases SandboxLab for agentic RL",
                        "source_name": "Example Developer Desk",
                        "url": "https://example.invalid/sandboxlab",
                        "published_at": "2026-06-08T12:00:00+08:00",
                        "summary": "SandboxLab gives agentic RL systems a reproducible environment.",
                    },
                    {
                        "title": "跨境支付服务接入AI企业的采购和结算流程",
                        "source_name": "Example Business Desk",
                        "url": "https://example.invalid/ai-payment",
                        "published_at": "2026-06-08T13:00:00+08:00",
                        "summary": "企业可在同一工作流中完成采购付款和跨境收款。",
                    },
                    {
                        "title": "Is this the dawn of Tokenpocalypse?",
                        "source_name": "AI News",
                        "url": "https://example.com/tokenpocalypse",
                        "published_at": "2026-06-08T13:30:00+08:00",
                        "summary": "AI companies are facing higher token costs and customer sticker shock.",
                    },
                    {
                        "title": "云虎机器人收购仓储自动化团队",
                        "source_name": "Example Robotics Desk",
                        "url": "https://example.invalid/robotics",
                        "published_at": "2026-06-08T14:00:00+08:00",
                        "summary": "云虎机器人计划把分拣设备和调度软件整合进统一交付方案。",
                    },
                ],
                "sources": [],
            }
        )

        self.assertIn("科技早报", article["title"])
        self.assertNotIn("broad vision", article["title"])
        self.assertNotIn("Introducing an economic", article["title"])
        self.assertNotIn("Confidential", article["title"])
        self.assertNotIn("Is this the dawn", article["title"])
        self.assertTrue(
            any(
                phrase in article["title"]
                for phrase in ("星河云", "支付", "机器人", "智能体", "AI模型")
            )
        )

    def test_briefing_html_moves_more_to_read_before_category_sections(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "跨境支付服务接入企业采购和结算流程",
                        "source_name": "Example Business Desk",
                        "url": "https://example.invalid/payment-workflow",
                        "published_at": "2026-06-08T11:00:00+08:00",
                        "summary": "首批商户可在同一流程中完成采购付款和跨境收款。",
                    },
                    {
                        "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/model",
                        "published_at": "2026-06-08T16:00:00+08:00",
                        "summary": "Atlas Assistant memory becomes easier to manage for users.",
                    },
                ],
                "sources": [],
            }
        )

        html = render_wechat_html(article)

        self.assertLess(html.index("其他重要动态"), html.index("AI 产品与平台"))

    def test_briefing_title_strips_source_byline_from_summary(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "AI 昨日速览",
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
                "sources": [],
            }
        )

        self.assertIn("丨科技早报", article["title"])
        self.assertNotIn("记者", article["title"])
        self.assertNotIn("编辑", article["title"])
        self.assertNotIn("赵甲", article["title"])

    def test_single_news_extension_cleans_byline_and_avoids_repeated_intro(self) -> None:
        article = build_article(
            {
                "topic": "科技新闻延展",
                "column": "科技新闻延展",
                "source_policy": "single_news_extension",
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Enterprise AI operations platform reaches finance teams",
                        "source_name": "Example Business Desk",
                        "url": "https://example.invalid/finance-ai",
                        "published_at": "2026-06-08T15:00:00+08:00",
                        "summary": "文｜赵甲 编辑｜钱乙 这家公司发布了新的AI运营工具。它面向出海企业。",
                    }
                ],
                "sources": [
                    {
                        "name": "Example Business Desk：Enterprise AI operations platform reaches finance teams",
                        "url": "https://example.invalid/finance-ai",
                        "summary": "文｜赵甲 编辑｜钱乙 这家公司发布了新的AI运营工具。它面向出海企业。",
                    }
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertNotIn("赵甲", html)
        self.assertNotIn("钱乙", html)
        self.assertNotIn(
            "来自 Example Business Desk 的消息提到：“Enterprise AI operations platform reaches finance teams”。来自 Example Business Desk",
            html,
        )
        self.assertIn("这家公司发布了新的AI运营工具", html)

    def test_smart_manufacturing_daily_uses_briefing_layout_and_industrial_copy(self) -> None:
        article = build_article(
            {
                "topic": "智能制造日报",
                "column": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "target_date": "2026-06-08",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "星云芯片与远航制造合建智能工厂，推进机器人与自动驾驶项目",
                        "source_name": "示例商业媒体",
                        "url": "https://example.invalid/smart-factory",
                        "published_at": "2026-06-08T08:00:00+08:00",
                        "summary": "双方将围绕AI工厂、机器人训练和自动驾驶基础设施合作。",
                    },
                    {
                        "title": "产业研究院预计示例卫星产业保持增长，地面设备需求同步上升",
                        "source_name": "示例商业媒体",
                        "url": "https://example.invalid/satellite-outlook",
                        "published_at": "2026-06-08T09:00:00+08:00",
                        "summary": "卫星产业链包括通信、发射、地面设备和数据应用。",
                    },
                    {
                        "title": "云虎机器人计划在下半年启动通用人形机器人小批量交付",
                        "source_name": "示例商业媒体",
                        "url": "https://example.invalid/humanoid-delivery",
                        "published_at": "2026-06-08T10:00:00+08:00",
                        "summary": "人形机器人进入量产和交付节奏。",
                    },
                    {
                        "title": "云港首个eVTOL整机项目进入建设阶段",
                        "source_name": "示例商业媒体",
                        "url": "https://example.invalid/evtol-project",
                        "published_at": "2026-06-08T11:00:00+08:00",
                        "summary": "低空经济整机制造项目落户，后续看适航和订单交付。",
                    },
                ],
                "sources": [
                    {
                        "name": "示例商业媒体：星云芯片与远航制造合建智能工厂，推进机器人与自动驾驶项目",
                        "url": "https://example.invalid/smart-factory",
                        "summary": "双方将围绕AI工厂、机器人训练和自动驾驶基础设施合作。",
                    }
                ],
            }
        )
        article["inline_images"] = [
            {"placeholder": "{{ARTICLE_INLINE_SECONDARY_1}}", "alt": "机器人与自动化产线配图"}
        ]

        html = render_wechat_html(article)
        section_text = "\n".join(paragraph for section in article["sections"] for paragraph in section["paragraphs"])

        self.assertIn("丨智能制造日报", article["title"])
        self.assertIn("星云芯片与远航制造合建智能工厂", article["title"])
        self.assertIn("云港首个eVTOL整机项目进入建设阶段", article["title"])
        self.assertLessEqual(len(article["title"]), 64)
        self.assertEqual(article["digest"], "每日智能制造产业资讯速递。")
        self.assertIn("智能制造日报", html)
        self.assertIn("昨日产业线索", html)
        self.assertIn("今日主线", html)
        self.assertIn("今天最值得盯的一点", html)
        self.assertIn("机器人与自动化", html)
        self.assertIn("低空经济与交通装备", html)
        self.assertIn("{{ARTICLE_INLINE_SECONDARY_1}}", html)
        self.assertIn("智能工厂", section_text)
        self.assertIn("真实产线", html)
        self.assertIn("客户", html)
        self.assertIn("供应链", html)
        self.assertIn("验证", html)
        self.assertIn("昨日产业线索覆盖", html)
        self.assertNotIn("判断进展时", html)
        self.assertNotIn("避免把一次演示", html)
        self.assertNotIn("先给你一句话", html)
        self.assertNotIn("它值得放进早报", html)
        self.assertNotIn("普通读者不需要记住", html)

        paragraphs = _briefing_news_body_paragraphs(article["news_items"][0], article=article)
        self.assertEqual(len(paragraphs), 3)

    def test_smart_manufacturing_title_prefers_chinese_detail_heading(self) -> None:
        article = build_article(
            {
                "topic": "智能制造日报",
                "column": "智能制造日报",
                "source_policy": "smart_manufacturing_daily",
                "target_date": "2026-06-25",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Robots identify materials and map unknown industrial environments",
                        "source_name": "Example Robotics Desk",
                        "url": "https://example.invalid/robot-map",
                        "published_at": "2026-06-25T10:00:00+08:00",
                        "summary": "Robots can map unknown environments for industrial inspection.",
                        "detail_heading": "新型机器人可识别材料并绘制未知工业环境三维地图",
                        "reader_summary": "示例实验室研发了能实时构建3D地图并识别物体材质的机器人。",
                    },
                    {
                        "title": "全天候自主水下机器人问世，保障海底管道与电缆安全",
                        "source_name": "Example Robotics Desk",
                        "url": "https://example.invalid/underwater-robot",
                        "published_at": "2026-06-25T11:00:00+08:00",
                        "summary": "自主水下机器人可执行长时间巡检任务。",
                    },
                ],
                "sources": [
                    {"name": "Example Robotics Desk", "url": "https://example.invalid/robotics", "summary": "robotics"}
                ],
            }
        )

        self.assertIn("新型机器人可识别材料", article["title"])
        self.assertNotIn("Robots that", article["title"])

    def test_agent_topic_uses_natural_opening(self) -> None:
        article = build_article(
            {
                "topic": "大模型、智能体、多模态、自动化的白话解释",
                "audience": "新手读者",
                "sources": [{"name": "本地选题配置", "url": "", "summary": "summary"}],
            }
        )

        self.assertIn("智能体就是能按目标连续做事的 AI", article["sections"][0]["paragraphs"][0])
        self.assertNotIn("大模型、智能体、多模态、自动化的白话解释不是", article["sections"][0]["paragraphs"][0])

    def test_wechat_html_summarizes_sources_without_long_urls(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "audience": "新手读者",
                "sources": [
                    {
                        "name": "Example Developer Desk：workflow update",
                        "url": "https://example.invalid/workflow-update",
                        "summary": "summary",
                    },
                    {
                        "name": "Example Research Review：AI administration",
                        "url": "https://example.invalid/ai-administration",
                        "summary": "summary",
                    },
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("本文参考 Example Developer Desk、Example Research Review的公开报道整理", html)
        self.assertNotIn("系统产物", html)

    def test_wechat_source_summary_only_lists_visible_news_sources(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "科技早报",
                "target_date": "2026-07-09",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "Northstar AI发布企业工作流能力",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/enterprise-workflow",
                        "summary": "Northstar AI发布企业工作流能力，帮助用户跨应用执行任务。",
                    }
                ],
                "sources": [
                    {
                        "name": "Example AI Desk",
                        "url": "https://example.invalid/enterprise-workflow",
                        "summary": "visible",
                    },
                    {"name": "未采用来源", "url": "https://example.invalid/unused", "summary": "unused"},
                ],
            }
        )

        html = render_wechat_html(article)

        self.assertIn("本文参考 Example AI Desk的公开报道整理", html)
        self.assertNotIn("未采用来源", html)

    def test_wechat_source_summary_names_all_visible_sources(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "column": "科技早报",
                "target_date": "2026-07-09",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": f"新闻{index}",
                        "source_name": source,
                        "url": f"https://example.invalid/{index}",
                        "summary": f"{source}发布一条可核验科技新闻。",
                    }
                    for index, source in enumerate(
                        ("Example Technology Desk", "Example AI Desk", "Example Developer Desk", "Example Robotics Desk"),
                        start=1,
                    )
                ],
                "sources": [{"name": "公开来源", "url": "https://example.invalid/source", "summary": "summary"}],
            }
        )

        html = render_wechat_html(article)

        self.assertIn(
            "本文参考 Example Technology Desk、Example AI Desk、Example Developer Desk、Example Robotics Desk的公开报道整理",
            html,
        )
        self.assertNotIn("等 4 个公开来源", html)
        self.assertNotIn("https://example.invalid/1", html)


if __name__ == "__main__":
    unittest.main()
