from __future__ import annotations

import unittest

from src.pipeline.copy_quality import (
    evaluate_article_copy_quality,
    reader_summary_has_publishable_depth,
    sanitize_article_copy,
)


def _text_model_context() -> dict[str, object]:
    return {
        "roles": {
            "summary": {"enabled": True},
            "batch": {"enabled": True},
            "polish": {"enabled": True},
        }
    }


def _article_with_status(summary_status: str) -> dict[str, object]:
    return {
        "title": "Northstar AI强化记忆能力；CloudDesk扩展办公AI丨科技早报",
        "digest": "今天重点看 AI 工具如何进入真实办公和个人助手场景。",
        "sections": [],
        "news_items": [
            {
                "title": "Northstar AI adds memory upgrades to Atlas Assistant",
                "source_name": "Example AI",
                "summary": "Atlas Assistant memory becomes easier to manage for users.",
                "detail_heading": "Northstar AI强化Atlas记忆管理，长期助手边界被重新讨论",
                "reader_summary": (
                    "Northstar AI正在调整Atlas Assistant的记忆能力，首批设置已经向企业工作区开放，管理员可以查看长期保存的偏好和上下文。"
                    "用户能够逐项删除不希望继续保留的信息，并在新的对话中关闭记忆调用，相关变更会写入账户记录。"
                    "本轮更新还区分了个人资料与项目资料的权限范围，团队成员只能读取已获授权的工作区内容。"
                ),
            }
        ],
        "text_model_generation": {
            "roles": {
                "summary": {"status": summary_status},
                "batch": {"status": "applied"},
                "polish": {"status": "applied"},
            }
        },
    }


class CopyQualityTest(unittest.TestCase):
    def test_reader_summary_depth_requires_substantive_complete_facts(self) -> None:
        one_sentence = "企业已经开放新的生产调度平台，并公布首批接入工厂、使用范围、权限设置、验收流程和异常接管安排。"
        two_sentences = (
            "企业已经开放新的生产调度平台，首批接入范围覆盖三座工厂和两条装配线。"
            "管理员可以限制数据权限，现场工程师负责确认计划变更，并在异常时接管排产任务。"
            "本轮部署还保留了完整的操作记录，供验收团队复核。"
            "试点团队将在连续运行三十天后核对设备利用率、订单延期率和人工接管次数，再决定是否扩展到其他基地。"
        )
        dense_two_sentences = (
            "星云存储推出新一代EDSFF E1.S规格PCIe Gen5 NVMe数据中心固态硬盘NX1系列，"
            "用于接替XD家族并已向部分超大规模客户出样。该系列面向GPU服务器和人工智能数据中心，"
            "相比上代产品顺序写入性能最高提升38%，随机写入性能最高提升20%，并保留现有部署规格，"
            "同时提供企业级耐久度和容量选项。"
        )
        short_two_sentences = (
            "企业发布新的生产调度平台，首批接入三座工厂。"
            "管理员可以限制数据权限，并在异常时接管排产任务。"
        )

        self.assertFalse(reader_summary_has_publishable_depth(one_sentence))
        self.assertTrue(reader_summary_has_publishable_depth(two_sentences))
        self.assertTrue(reader_summary_has_publishable_depth(dense_two_sentences))
        self.assertFalse(reader_summary_has_publishable_depth(short_two_sentences))

    def test_sanitizer_does_not_delete_heading_overlap_when_body_would_become_thin(self) -> None:
        article = _article_with_status("applied")
        heading = "制造企业开放跨厂排产平台，首批覆盖三座生产基地"
        summary = (
            heading
            + "。管理员可以统一查看订单、设备和库存状态，并为计划变更保留人工确认节点。"
            + "所有调整都会写入操作记录，供产线负责人在验收时复核。"
            + "首批系统将在本月完成上线验收。"
            + "试点结束后，项目组会核对订单延期率和人工接管次数，再决定下一批部署范围。"
        )
        article["news_items"][0]["detail_heading"] = heading
        article["news_items"][0]["reader_summary"] = summary

        sanitized = sanitize_article_copy(article)

        self.assertEqual(sanitized["news_items"][0]["reader_summary"], summary)
        self.assertTrue(reader_summary_has_publishable_depth(sanitized["news_items"][0]["reader_summary"]))

    def test_enforced_final_review_blocks_medium_numeric_drift(self) -> None:
        article = _article_with_status("applied")
        article["final_ai_review"] = {
            "mode": "enforce",
            "status": "applied",
            "risk_level": "medium",
            "issues": [
                {
                    "type": "numeric_drift",
                    "severity": "medium",
                    "news_index": 1,
                    "evidence": "Northstar AI 正在调整 Atlas Assistant 的记忆能力",
                    "action": "repair_field",
                }
            ],
        }

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        final_review = next(check for check in result["checks"] if check["name"] == "final_ai_review")
        self.assertEqual(len(final_review["blocking_issues"]), 1)

    def test_enforced_final_review_only_blocks_hard_or_drop_issues(self) -> None:
        cases = [
            ({"type": "promotion", "severity": "medium", "news_index": 1, "action": "keep"}, True),
            ({"type": "repetition", "severity": "low", "news_index": 1, "action": "repair_field"}, False),
            ({"type": "ai_cliche", "severity": "medium", "news_index": 1, "action": "repair_field"}, False),
            ({"type": "ai_cliche", "severity": "low", "news_index": 1, "action": "drop_item"}, True),
        ]

        for issue, should_block in cases:
            with self.subTest(issue=issue, should_block=should_block):
                article = _article_with_status("applied")
                article["final_ai_review"] = {
                    "mode": "enforce",
                    "status": "applied",
                    "risk_level": issue["severity"],
                    "issues": [issue],
                }

                result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

                self.assertEqual(result["ok"], not should_block)
                final_review = next(check for check in result["checks"] if check["name"] == "final_ai_review")
                self.assertEqual(final_review["blocking_issues"], [issue] if should_block else [])

    def test_recovery_below_minimum_blocks_publish(self) -> None:
        article = _article_with_status("applied")
        article["final_review_recovery"] = {
            "status": "partial",
            "below_minimum": True,
            "dropped_items": [{"title": "低质量新闻"}],
            "unresolved_issues": [],
        }

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        recovery = next(check for check in result["checks"] if check["name"] == "final_review_recovery")
        self.assertFalse(recovery["ok"])

    def test_recovery_blocks_unresolved_medium_issue_even_above_minimum(self) -> None:
        article = _article_with_status("applied")
        issue = {
            "type": "ai_cliche",
            "severity": "medium",
            "news_index": 1,
            "action": "drop_item",
        }
        article["final_review_recovery"] = {
            "status": "partial",
            "below_minimum": False,
            "budget_exhausted": False,
            "unresolved_issues": [issue],
        }

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        recovery = next(check for check in result["checks"] if check["name"] == "final_review_recovery")
        self.assertEqual(recovery["blocking_unresolved_issues"], [issue])

    def test_recovery_allows_budget_exhaustion_with_only_advisory_style_issue(self) -> None:
        article = _article_with_status("applied")
        article["final_review_recovery"] = {
            "status": "budget_exhausted",
            "below_minimum": False,
            "budget_exhausted": True,
            "unresolved_issues": [
                {
                    "type": "repetition",
                    "severity": "low",
                    "news_index": 1,
                    "action": "keep",
                }
            ],
        }

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertTrue(result["ok"])
        recovery = next(check for check in result["checks"] if check["name"] == "final_review_recovery")
        self.assertTrue(recovery["budget_exhausted"])

    def test_sanitize_drops_unprocessed_raw_item_when_batch_model_failed(self) -> None:
        article = _article_with_status("applied")
        article["text_model_generation"]["roles"]["batch"]["status"] = "fallback_after_text_model_error"
        article["news_items"] = [
            {
                "title": "Human-aware robots learn complex manipulation",
                "source_name": "Example Research",
                "summary": "Researchers described a robot training approach for complex manipulation tasks.",
            }
        ]

        sanitized = sanitize_article_copy(article)

        self.assertEqual(sanitized["news_items"], [])
        change = sanitized["copy_sanitization"]["removed_low_quality_fragments"][-1]
        self.assertEqual(change["reason"], "unprocessed_model_fallback")

    def test_sanitize_drops_item_with_fixed_ai_phrase(self) -> None:
        article = _article_with_status("applied")
        article["news_items"].append(
            {
                "title": "Another AI product update",
                "source_name": "Example",
                "url": "https://example.com/bad",
                "summary": "A product update.",
                "detail_heading": "Another AI product update enters the market",
                "reader_summary": "换个角度看，这条消息值得关注，因为它反映了 AI 正在进入工作流程。",
            }
        )
        article["sources"] = [
            {"name": "Example", "url": "https://example.com/bad", "summary": "A product update."}
        ]

        sanitized = sanitize_article_copy(article)

        self.assertEqual(len(sanitized["news_items"]), 1)
        self.assertEqual(sanitized["sources"], [])

    def test_generic_article_title_fails_copy_quality(self) -> None:
        article = _article_with_status("applied")
        article["title"] = "科技应用更新；AI模型能力继续更新丨科技早报"

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        self.assertIn("generic_article_title", [check["name"] for check in result["failed_checks"]])

    def test_final_ai_review_is_diagnostic_in_shadow_mode(self) -> None:
        article = _article_with_status("applied")
        article["final_ai_review"] = {
            "mode": "shadow",
            "status": "applied",
            "risk_level": "high",
            "issues": [{"severity": "high", "type": "platform_risk"}],
        }

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        final_review = next(check for check in result["checks"] if check["name"] == "final_ai_review")
        self.assertTrue(final_review["diagnostic_only"])
        self.assertTrue(result["ok"])

    def test_partial_text_model_fallback_passes_when_final_copy_is_complete(self) -> None:
        result = evaluate_article_copy_quality(
            _article_with_status("partial_fallback_after_text_model_error"),
            text_model_context=_text_model_context(),
        )

        self.assertTrue(result["ok"])
        model_check = result["checks"][-1]
        self.assertTrue(model_check["ok"])
        self.assertEqual(model_check["partial"], {"summary": "partial_fallback_after_text_model_error"})

    def test_partial_text_model_fallback_still_fails_incomplete_final_copy(self) -> None:
        article = _article_with_status("partial_fallback_after_text_model_error")
        article["news_items"] = [
            {
                "title": "From data to decisions: how Example Exchange is scaling trusted AI",
                "source_name": "Example AI Desk",
                "summary": (
                    "See how Example Exchange uses AI to scale trusted workflows across its global business, accelerating "
                    "insights, shrinking release cycles, and supporting data-driven decisions for enterprise teams."
                ),
                "detail_heading": "",
                "reader_summary": "",
            }
        ]

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["failed_checks"]}
        self.assertIn("ascii_news_titles_without_model_heading", failed_names)
        self.assertIn("raw_or_low_quality_summary_without_model_copy", failed_names)
        self.assertNotIn("text_model_roles_sufficient_for_final_copy", failed_names)

    def test_full_text_model_fallback_is_diagnostic_when_final_copy_is_complete(self) -> None:
        result = evaluate_article_copy_quality(
            _article_with_status("fallback_after_text_model_error"),
            text_model_context=_text_model_context(),
        )

        self.assertTrue(result["ok"])
        model_check = result["checks"][-1]
        self.assertFalse(model_check["ok"])
        self.assertTrue(model_check["diagnostic_only"])
        self.assertEqual(model_check["failed"], {"summary": "fallback_after_text_model_error"})
        self.assertEqual(result["model_health"]["failed"], {"summary": "fallback_after_text_model_error"})

    def test_follow_up_phrase_fails_copy_quality(self) -> None:
        banned_variants = [
            "后续看企业是否愿意持续采购。",
            "后续看点是企业是否愿意持续采购。",
            "后续观察产品能否进入真实预算。",
            "这说明产品开始从概念走向交付。",
        ]

        for phrase in banned_variants:
            with self.subTest(phrase=phrase):
                article = _article_with_status("applied")
                article["news_items"][0]["reader_summary"] += phrase

                result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

                self.assertFalse(result["ok"])
                phrase_check = result["checks"][0]
                self.assertFalse(phrase_check["ok"])
                self.assertTrue(phrase_check["hits"])

    def test_source_byline_in_article_title_fails_copy_quality(self) -> None:
        article = _article_with_status("applied")
        article["title"] = "记者丨赵甲 钱乙 编辑丨孙丙 6月14日；模型公司发布安全说明丨科技早报"

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["failed_checks"]}
        self.assertIn("news_byline_leak", failed_names)

    def test_source_dateline_and_truncated_title_are_sanitized(self) -> None:
        article = _article_with_status("applied")
        article["title"] = "示例科技媒体 6 月 20 日消息；星云机器人开始研究机器人那套了…；远航实验室测试新一代探测车原型；云帆平台调整AI团队丨科技早报"

        sanitized = sanitize_article_copy(article)
        result = evaluate_article_copy_quality(sanitized, text_model_context=_text_model_context())

        self.assertEqual(sanitized["title"], "远航实验室测试新一代探测车原型；云帆平台调整AI团队丨科技早报")
        self.assertTrue(result["ok"])

    def test_unsanitized_source_dateline_title_fails_copy_quality(self) -> None:
        article = _article_with_status("applied")
        article["title"] = "示例科技媒体 6 月 20 日消息丨科技早报"

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        failed_names = {check["name"] for check in result["failed_checks"]}
        self.assertIn("article_title_quality", failed_names)

    def test_sanitize_article_copy_removes_rss_click_original_fragments(self) -> None:
        article = _article_with_status("applied")
        article["sections"] = [
            {
                "heading": "其他重要动态",
                "paragraphs": [
                    "来自 Example Developer Desk 的消息提到“Agent 工程实践图谱”。点击查看原文>",
                    "CloudDesk 收购 HelpFlow，企业智能体继续进入客服流程。",
                ],
            }
        ]
        article["news_items"][0]["summary"] = "2026-06-15T18:31:38+08:00；点击查看原文>"
        article["sources"] = [
            {
                "name": "Example Developer Desk：Agent 工程实践图谱",
                "url": "https://example.invalid/agent-engineering",
                "summary": "2026-06-15T18:31:38+08:00；点击查看原文>",
            }
        ]

        sanitized = sanitize_article_copy(article)
        result = evaluate_article_copy_quality(sanitized, text_model_context=_text_model_context())

        self.assertNotIn("点击查看原文", str(sanitized))
        self.assertEqual(
            sanitized["sections"][0]["paragraphs"][0],
            "来自 Example Developer Desk 的消息提到“Agent 工程实践图谱”",
        )
        self.assertEqual(
            sanitized["news_items"][0]["summary"],
            "Example AI 报道了“Northstar AI adds memory upgrades to Atlas Assistant”。",
        )
        self.assertTrue(sanitized["sources"][0]["summary"])
        self.assertTrue(result["ok"])
        self.assertEqual(sanitized["copy_sanitization"]["status"], "applied")

    def test_sanitize_article_copy_fills_missing_heading_from_reader_summary(self) -> None:
        article = _article_with_status("applied")
        article["news_items"] = [
            {
                "title": "Research-agent security boundaries in shared workspaces",
                "source_name": "DevForge Blog",
                "summary": "A technical research blog post about AI research agent security.",
                "detail_heading": "",
                "reader_summary": (
                    "DevForge博客发布安全研究，提醒企业在使用研究型AI代理处理敏感资料时，"
                    "需要重新检查数据隔离、权限控制和内部知识库边界。"
                    "研究列出了跨项目检索、临时文件缓存和外部工具调用三类暴露路径，并给出对应的测试步骤。"
                    "企业可以据此核对代理能够访问的目录、账号和知识库，再为高风险操作保留人工确认。"
                ),
            }
        ]

        sanitized = sanitize_article_copy(article)
        result = evaluate_article_copy_quality(sanitized, text_model_context=_text_model_context())

        self.assertEqual(
            sanitized["news_items"][0]["detail_heading"],
            "DevForge博客发布安全研究，提醒企业在使用研究型AI代理处理敏感资料时，需要重新检查数据隔离、权限控制和内部知识库边界",
        )
        self.assertTrue(result["ok"])
        actions = [
            change["action"]
            for change in sanitized["copy_sanitization"]["removed_low_quality_fragments"]
        ]
        self.assertIn("filled_from_reader_summary", actions)

    def test_sanitize_repairs_prefix_truncated_heading_from_complete_summary(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "title": "Agent Data Kit开放智能体训练数据资源",
                "detail_heading": "DevForge博客发布AgentDataKit开放数据资源，重点介绍面向智",
                "reader_summary": (
                    "DevForge博客发布Agent Data Kit开放数据资源，重点介绍面向智能体开发的训练与调试数据。"
                    "该项目由多家技术公司共同推动，目标是降低智能体开发阶段的数据准备门槛。"
                ),
            }
        )

        sanitized = sanitize_article_copy(article)

        self.assertEqual(
            sanitized["news_items"][0]["detail_heading"],
            "DevForge博客发布AgentDataKit开放数据资源，重点介绍面向智能体开发的训练与调试数据",
        )

    def test_same_item_heading_and_opening_sentence_are_deduplicated_without_blocking(self) -> None:
        article = _article_with_status("applied")
        heading = str(article["news_items"][0]["detail_heading"])
        article["news_items"][0]["reader_summary"] = (
            f"{heading}。Northstar AI同时调整了偏好管理和上下文控制，让用户能更清楚地决定哪些信息被长期保存。"
            "管理员可以限制项目资料的读取范围，用户也能逐项删除已经保存的偏好与历史上下文。"
            "本轮开放先覆盖企业工作区，关键设置变更会写入账户记录，便于团队核对权限调整。"
            "个人账号仍沿用原有设置，不在首批开放范围内。"
        )

        sanitized = sanitize_article_copy(article)
        result = evaluate_article_copy_quality(sanitized, text_model_context=_text_model_context())

        self.assertNotIn(heading + "。", sanitized["news_items"][0]["reader_summary"])
        self.assertTrue(result["ok"])

    def test_single_sentence_heading_echo_is_removed_for_replenishment(self) -> None:
        article = _article_with_status("applied")
        heading = "制造企业发布新一代工业机器人，首批设备进入汽车装配线测试并披露交付安排"
        article["news_items"][0].update(
            {
                "title": "制造企业发布新一代工业机器人",
                "detail_heading": heading,
                "reader_summary": heading + "。",
            }
        )

        sanitized = sanitize_article_copy(article)

        self.assertEqual(sanitized["news_items"], [])
        removal = sanitized["copy_sanitization"]["removed_low_quality_fragments"][-1]
        self.assertEqual(removal["reason"], "source_or_title_echo")

    def test_near_duplicate_heading_and_opening_sentence_are_deduplicated(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "detail_heading": "Northstar Systems推出Atlas自主运维系统，用AI智能体管理企业服务器",
                "reader_summary": (
                    "Northstar Systems宣布推出Atlas自主运维系统，这是一款用AI智能体管理企业服务器的工具。"
                    "首批能力覆盖日常巡检、异常定位和运维建议，管理员仍可保留人工复核节点。"
                ),
            }
        )

        sanitized = sanitize_article_copy(article)
        summary = sanitized["news_items"][0]["reader_summary"]

        self.assertNotIn("Northstar Systems宣布推出Atlas自主运维系统", summary)
        self.assertIn("首批能力覆盖日常巡检", summary)

    def test_repeated_latin_product_subject_is_compacted_without_losing_facts(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "title": "星河实验室开源MemoryWeave",
                "detail_heading": "星河实验室开源MemoryWeave，提供可迁移的智能体记忆层",
                "reader_summary": (
                    "MemoryWeave是面向AI Agent的可迁移、自演进记忆操作层。"
                    "MemoryWeave使AI的记忆和Skill可以随任务继续更新。"
                    "MemoryWeave还支持把记忆迁移到不同智能体任务中。"
                ),
            }
        )

        sanitized = sanitize_article_copy(article)
        summary = sanitized["news_items"][0]["reader_summary"]

        self.assertEqual(summary.count("MemoryWeave"), 1)
        self.assertIn("；使AI的记忆和Skill", summary)
        self.assertIn("；还支持把记忆迁移", summary)

    def test_nested_source_attribution_is_unwrapped_before_rendering(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "title": "How Northstar AI built responsive voice AI",
                "source_name": "Example AI Desk",
                "detail_heading": "ExampleAIDesk报道“ExampleAIDesk报道“Northstar AI用六个月构建实时语音系统””",
                "reader_summary": (
                    "Example AI Desk报道“ExampleAIDesk报道“Northstar AI用六个月构建实时语音系统””。"
                    "团队把持续音频输入、响应生成和打断处理放进同一条实时链路。"
                ),
            }
        )

        sanitized = sanitize_article_copy(article)
        item = sanitized["news_items"][0]

        self.assertNotIn("Example AI Desk报道", item["detail_heading"] + item["reader_summary"])
        self.assertNotIn("ExampleAIDesk报道", item["detail_heading"] + item["reader_summary"])
        self.assertEqual(item["reader_summary"], "团队把持续音频输入、响应生成和打断处理放进同一条实时链路。")

    def test_rejected_model_copy_with_thin_fallback_is_removed_for_replenishment(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "title": "Northstar Data为数据中心电工短缺开办培训项目",
                "detail_heading": "Northstar Data为数据中心扩建开办电工培训项目",
                "reader_summary": "电工短缺已经成为美国扩建数据中心的主要障碍。",
                "copy_generation_validation": {
                    "reader_summary": "rejected",
                    "reason": "length_or_style",
                },
            }
        )

        sanitized = sanitize_article_copy(article)

        self.assertEqual(sanitized["news_items"], [])
        removal = sanitized["copy_sanitization"]["removed_low_quality_fragments"][-1]
        self.assertEqual(removal["reason"], "model_reader_summary_rejected_without_publishable_fallback")

    def test_rejected_model_copy_uses_substantive_source_fallback(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "title": "Jedify完成2400万美元融资",
                "summary": (
                    "Jedify完成2400万美元融资，由Norwest领投，Snowflake Ventures参投。"
                    "公司为企业AI代理提供内部流程、权限和数据上下文，帮助系统执行跨部门任务。"
                    "本轮资金将用于扩充工程团队，并支持更多需要连接多套内部系统的企业部署。"
                    "客户可在部署时设置数据接口和访问范围，并为关键操作保留人工复核环节。"
                ),
                "detail_heading": "Jedify完成2400万美元融资，扩充企业AI代理部署能力",
                "reader_summary": "",
                "copy_generation_validation": {
                    "reader_summary": "rejected",
                    "reason": "numeric_fact_mismatch",
                },
            }
        )

        sanitized = sanitize_article_copy(article)

        self.assertEqual(len(sanitized["news_items"]), 1)
        summary = sanitized["news_items"][0]["reader_summary"]
        self.assertIn("2400万美元", summary)
        self.assertTrue(reader_summary_has_publishable_depth(summary))

    def test_generated_keyword_list_summary_is_removed(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0]["reader_summary"] = (
            "智元机器人、人形机器人、具身智能、人才激励、离职员工、机器人赛道、产业融资、"
            "供应链管理、量产交付、工厂部署、客户采购、技术研发。"
        )

        sanitized = sanitize_article_copy(article)

        self.assertEqual(sanitized["news_items"], [])
        removal = sanitized["copy_sanitization"]["removed_low_quality_fragments"][-1]
        self.assertEqual(removal["reason"], "tag_style_reader_summary")

    def test_consumer_discount_tail_is_removed_as_promotional_copy(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0]["reader_summary"] = (
            "厂商公布了新款终端的处理器、屏幕和系统配置，并说明首批上市区域。"
            "当前数码家电政府补贴活动仍在持续，消费者购买产品时可享受额外折扣优惠。"
        )

        sanitized = sanitize_article_copy(article)

        self.assertEqual(sanitized["news_items"], [])
        removal = sanitized["copy_sanitization"]["removed_low_quality_fragments"][-1]
        self.assertEqual(removal["reason"], "promotional_or_subscription_text")

    def test_repeated_sentence_across_news_items_still_blocks(self) -> None:
        article = _article_with_status("applied")
        repeated = "两家公司都把新的智能代理能力放进企业办公流程，并明确要求管理员设置权限边界。"
        article["news_items"][0]["reader_summary"] = repeated
        article["news_items"].append(
            {
                "title": "另一家公司更新企业智能代理",
                "source_name": "Another Source",
                "summary": "另一家公司更新企业智能代理。",
                "detail_heading": "另一家公司更新企业智能代理，管理员可设置应用权限",
                "reader_summary": repeated,
            }
        )

        result = evaluate_article_copy_quality(article, text_model_context=_text_model_context())

        self.assertFalse(result["ok"])
        self.assertIn("repeated_visible_sentences", [check["name"] for check in result["failed_checks"]])

    def test_missing_reader_summary_uses_complete_raw_summary_sentences(self) -> None:
        article = _article_with_status("applied")
        article["source_policy"] = "smart_manufacturing_daily"
        article["news_items"] = [
            {
                "title": "远程实验：研究团队操控星云G1完成精细手术任务",
                "source_name": "Example Technology Desk",
                "summary": (
                    "Example Technology Desk 7 月 9 日消息，研究团队使用两台星云G1人形机器人完成远程协同操作。"
                    "两台机器人均由专业人员通过控制台操控，其中一台负责主要操作，另一台完成辅助任务。"
                    "研究团队表示，下一步将继续验证控制延迟和重复成功率。后半段是不应进入正文的截断片段"
                ),
                "detail_heading": "",
                "reader_summary": "",
            }
        ]

        sanitized = sanitize_article_copy(article)
        item = sanitized["news_items"][0]

        self.assertIn("远程实验：研究团队操控星云G1完成精细手术任务", item["detail_heading"])
        self.assertTrue(item["reader_summary"].endswith("。"))
        self.assertNotIn("截断片段", item["reader_summary"])
        self.assertNotIn("Example Technology Desk 7 月 9 日消息", item["reader_summary"])

    def test_dateline_cleanup_preserves_reader_summary_sentence_ending(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0]["reader_summary"] = (
            "Example Technology Desk 7 月 9 日消息，Northstar AI发布企业工作流能力，允许智能代理跨应用执行连续任务。"
            "管理员可以设置权限边界、人工复核节点和失败后的接管方式。"
        )

        sanitized = sanitize_article_copy(article)
        summary = sanitized["news_items"][0]["reader_summary"]

        self.assertNotIn("Example Technology Desk 7 月 9 日消息", summary)
        self.assertTrue(summary.endswith("。"))

    def test_digest_restores_numeric_range_from_news_source(self) -> None:
        article = _article_with_status("applied")
        article["source_policy"] = "smart_manufacturing_daily"
        article["digest"] = "北辰工业要求年底产能提升至每周2500台。"
        article["news_items"][0].update(
            {
                "title": "北辰工业要求供应商提升Atlas机器人零部件产能",
                "summary": "供应商被要求在年底将产能提升到2000-2500台/周。",
                "detail_heading": "北辰工业下发Atlas机器人采购指引，供应商开始准备年底产能",
                "reader_summary": "北辰工业已向供应商下发零部件采购指引，要求年底将周产能提升至2000-2500台。",
            }
        )

        sanitized = sanitize_article_copy(article)

        self.assertIn("每周2000-2500台", sanitized["digest"])
        self.assertNotIn("每周2500台", sanitized["digest"])

    def test_digest_does_not_treat_date_span_as_replacement_range(self) -> None:
        article = _article_with_status("applied")
        article["digest"] = "基于2026年7月9日公开信息，梳理当天科技动态。"
        article["news_items"][0].update(
            {
                "title": "世界人工智能大会将于7月17-7月20日举办",
                "summary": "大会将于7月17-7月20日在上海举办。",
            }
        )

        sanitized = sanitize_article_copy(article)

        self.assertEqual(sanitized["digest"], "基于2026年7月9日公开信息，梳理当天科技动态。")

    def test_sanitize_normalizes_product_spacing_and_chinese_whitespace(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "detail_heading": "ChatGPTWork进入Microsoft365Copilot",
                "reader_summary": "研究团队完成两例 大型动物实验 。ChatGPTWork随后进入Microsoft365Copilot。",
            }
        )

        sanitized = sanitize_article_copy(article)
        item = sanitized["news_items"][0]

        self.assertEqual(item["detail_heading"], "ChatGPT Work进入Microsoft 365 Copilot")
        self.assertIn("两例大型动物实验。", item["reader_summary"])
        self.assertIn("ChatGPT Work", item["reader_summary"])
        self.assertIn("Microsoft 365 Copilot", item["reader_summary"])

    def test_sanitize_repairs_token_factory_typo(self) -> None:
        article = _article_with_status("applied")
        article["news_items"][0].update(
            {
                "detail_heading": "Example Developer Desk解读Toekn Factory技术进展",
                "reader_summary": "公开报道介绍了Toekn Factory的技术架构和应用范围。",
            }
        )

        sanitized = sanitize_article_copy(article)
        item = sanitized["news_items"][0]

        self.assertIn("TokenFactory", item["detail_heading"].replace(" ", ""))
        self.assertIn("TokenFactory", item["reader_summary"].replace(" ", ""))
        self.assertNotIn("Toekn", item["detail_heading"] + item["reader_summary"])

    def test_sanitize_article_copy_fills_missing_reader_summary_from_chinese_heading(self) -> None:
        article = _article_with_status("applied")
        article["source_policy"] = "smart_manufacturing_daily"
        article["news_items"] = [
            {
                "title": "Harbor Motors reorganizes robotics unit and adds nine departments",
                "source_name": "Example Mobility Desk",
                "summary": (
                    "Harbor Motors' robotics center has added nine second-tier departments while its operations lead "
                    "coordinates the production push for robotics."
                ),
                "detail_heading": "远航汽车机器人业务调整，增设九个二级部门",
                "reader_summary": "",
                "fact_card": {
                    "subject": "远航汽车机器人业务",
                    "action": "调整组织架构并增设九个二级部门",
                    "facts": [
                        "远航汽车机器人业务调整组织架构，增设九个二级部门",
                        "机器人中心由运营负责人协调推进生产工作",
                    ],
                    "confidence": "high",
                },
            }
        ]

        sanitized = sanitize_article_copy(article)
        result = evaluate_article_copy_quality(sanitized, text_model_context=_text_model_context())

        reader_summary = sanitized["news_items"][0]["reader_summary"]
        self.assertIn("远航汽车机器人业务调整组织架构", reader_summary)
        self.assertNotIn("这条线索可以放在产业落地里看", reader_summary)
        self.assertNotIn("客户、交付时间、量产计划和供应链配套", reader_summary)
        self.assertFalse(result["ok"])
        self.assertIn(
            "reader_summary_publishable_depth",
            {check["name"] for check in result["failed_checks"]},
        )
        self.assertEqual(
            sanitized["copy_sanitization"]["removed_low_quality_fragments"][-1]["action"],
            "filled_from_detail_heading",
        )

    def test_sanitize_replaces_generic_mixed_english_heading_with_fact_card_copy(self) -> None:
        article = _article_with_status("applied")
        article["source_policy"] = "smart_manufacturing_daily"
        article["news_items"] = [
            {
                "title": "Teleoperated humanoid robots complete first live surgical procedure",
                "source_name": "Example Robotics Desk",
                "summary": "Two humanoid robots completed a teleoperated live surgical procedure.",
                "detail_heading": "Teleoperated出现新进展机器人能力",
                "reader_summary": "Teleoperated出现新进展机器人能力。",
                "fact_card": {
                    "subject": "两台人形机器人",
                    "action": "首次完成活体手术程序",
                    "facts": [
                        "两台人形机器人首次完成活体手术程序",
                        "两台机器人均由专业人员通过远程控制台操作",
                    ],
                    "confidence": "high",
                },
            }
        ]

        sanitized = sanitize_article_copy(article)
        item = sanitized["news_items"][0]

        self.assertNotIn("Teleoperated出现新进展", item["detail_heading"])
        self.assertIn("人形机器人", item["detail_heading"])
        self.assertIn("远程控制台", item["reader_summary"])
        self.assertNotIn("这条线索可以放在产业落地里看", item["reader_summary"])

    def test_sanitize_replaces_question_word_generated_heading(self) -> None:
        article = _article_with_status("applied")
        article["news_items"] = [
            {
                "title": "Would you host part of an AI data center in your home?",
                "source_name": "Example Technology Desk",
                "summary": "A home energy company is testing distributed AI compute units.",
                "detail_heading": "Would发布AI算力服务",
                "reader_summary": "Would发布AI算力服务。",
                "fact_card": {
                    "subject": "家庭能源企业",
                    "action": "测试分布式AI算力设备",
                    "facts": [
                        "一家家庭能源企业正在测试分布式AI算力设备",
                        "试点计划将计算设备部署到参与用户的住宅中",
                    ],
                    "confidence": "high",
                },
            }
        ]

        sanitized = sanitize_article_copy(article)
        item = sanitized["news_items"][0]

        self.assertNotIn("Would发布", item["detail_heading"])
        self.assertIn("家庭能源企业", item["detail_heading"])
        self.assertIn("住宅", item["reader_summary"])


if __name__ == "__main__":
    unittest.main()
