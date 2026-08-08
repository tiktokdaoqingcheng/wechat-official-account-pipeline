from __future__ import annotations

import json
import unittest

from src.pipeline.editorial_contract import (
    EDITORIAL_CONTRACT_VERSION,
    SMART_MANUFACTURING_CONTRACT,
    TECH_BRIEFING_CONTRACT,
    contract_for_article,
    fact_card_readiness_report,
    reader_summary_depth_report,
)
from src.pipeline.text_model_enhancer import (
    _apply_generated_fact_cards,
    _apply_generated_news_items,
    _copy_quality_repair_indexes,
    _messages_for_batch,
    _messages_for_batch_quality_repair,
    _messages_for_final_title,
    _messages_for_final_review,
    _messages_for_polish,
    _messages_for_polish_items,
    _messages_for_summary,
    enhance_article_with_text_models,
)


class EditorialContractTest(unittest.TestCase):
    def test_profiles_share_quantitative_contract_but_keep_domain_rules_separate(self) -> None:
        primary = contract_for_article({"source_policy": "tech_briefing"})
        secondary = contract_for_article({"source_policy": "smart_manufacturing_daily"})

        self.assertEqual(primary.profile, "tech_briefing")
        self.assertEqual(secondary.profile, "smart_manufacturing_daily")
        self.assertEqual(
            primary.reader_summary_target_min_chars,
            secondary.reader_summary_target_min_chars,
        )
        self.assertNotEqual(primary.sentence_jobs, secondary.sentence_jobs)
        self.assertIn("产线", "".join(secondary.domain_rules))

    def test_positive_examples_meet_their_own_target_not_only_the_hard_floor(self) -> None:
        for contract in (TECH_BRIEFING_CONTRACT, SMART_MANUFACTURING_CONTRACT):
            with self.subTest(profile=contract.profile):
                report = reader_summary_depth_report(contract.positive_example_summary, contract)
                self.assertTrue(report["ok"])
                self.assertGreaterEqual(
                    report["effective_chars"],
                    contract.reader_summary_target_min_chars,
                )
                self.assertGreaterEqual(
                    report["complete_sentences"],
                    contract.reader_summary_preferred_min_sentences,
                )

    def test_target_is_stronger_than_hard_floor_without_turning_style_into_a_blocker(self) -> None:
        summary = "甲" * 43 + "。" + "乙" * 43 + "。" + "丙" * 43 + "。"
        report = reader_summary_depth_report(summary, TECH_BRIEFING_CONTRACT)

        self.assertTrue(report["ok"])
        self.assertLess(
            report["effective_chars"],
            TECH_BRIEFING_CONTRACT.reader_summary_target_min_chars,
        )

    def test_all_model_stages_receive_the_same_contract_version_and_profile(self) -> None:
        article = {
            "title": "测试丨智能制造日报",
            "source_policy": "smart_manufacturing_daily",
            "news_items": [
                {
                    "title": "机器人进入装配线测试",
                    "summary": "首套设备开始验证抓取精度、运行节拍和异常停机处理。",
                    "source_name": "产业媒体",
                    "fact_card": {
                        "subject": "机器人厂商",
                        "action": "启动装配线测试",
                        "facts": ["验证抓取精度", "记录异常停机处理"],
                    },
                }
            ],
        }
        payloads = [
            json.loads(_messages_for_summary(article, article["news_items"])[1]["content"]),
            json.loads(_messages_for_final_title(article)[1]["content"]),
            json.loads(_messages_for_batch(article, article["news_items"])[1]["content"]),
            json.loads(
                _messages_for_batch_quality_repair(
                    article,
                    article["news_items"],
                    start_index=1,
                    repair_indexes=[1],
                )[1]["content"]
            ),
            json.loads(_messages_for_polish(article)[1]["content"]),
            json.loads(_messages_for_polish_items(article, article["news_items"])[1]["content"]),
            json.loads(_messages_for_final_review(article, "<p>终稿</p>")[1]["content"]),
        ]

        for payload in payloads:
            contract = payload["editorial_contract"]
            self.assertEqual(contract["version"], EDITORIAL_CONTRACT_VERSION)
            self.assertEqual(contract["profile"], "smart_manufacturing_daily")
            if "reader_summary_target_chars" in contract:
                self.assertEqual(
                    contract["reader_summary_target_chars"],
                    [
                        SMART_MANUFACTURING_CONTRACT.reader_summary_target_min_chars,
                        SMART_MANUFACTURING_CONTRACT.reader_summary_target_max_chars,
                    ],
                )

    def test_sparse_fact_card_is_rejected_before_copywriting(self) -> None:
        news_items = [
            {
                "title": "某公司发布新品",
                "summary": "新品今天发布，但来源没有提供其他产品信息。",
                "source_name": "公开来源",
            }
        ]
        generated = [
            {
                "index": 1,
                "fact_card": {
                    "subject": "某公司",
                    "action": "发布新品",
                    "facts": ["新品今天发布"],
                    "numbers": [],
                    "uncertainties": ["产品能力未说明"],
                },
                "candidate_verdict": {"usable": True, "reason_code": "", "reason": ""},
            }
        ]

        self.assertTrue(
            _apply_generated_fact_cards(
                generated,
                news_items,
                contract=TECH_BRIEFING_CONTRACT,
            )
        )
        self.assertEqual(news_items[0]["candidate_verdict"]["reason_code"], "insufficient_facts")
        self.assertEqual(news_items[0]["editorial_fact_readiness"]["status"], "insufficient_facts")

    def test_short_generated_heading_enters_the_same_targeted_repair_loop(self) -> None:
        news_items = [
            {
                "title": "某公司发布企业智能助手",
                "summary": "产品首批进入企业工作区并提供权限控制和审计记录。",
                "detail_heading": "旧核心句",
                "reader_summary": "旧正文。",
                "fact_card": {
                    "subject": "某公司",
                    "action": "发布企业智能助手",
                    "facts": ["首批进入企业工作区", "提供权限控制和审计记录"],
                },
            }
        ]
        generated = [
            {
                "index": 1,
                "detail_heading": "新品发布",
                "reader_summary": TECH_BRIEFING_CONTRACT.positive_example_summary,
            }
        ]

        _apply_generated_news_items(
            generated,
            news_items,
            allow_title_summary=False,
            contract=TECH_BRIEFING_CONTRACT,
        )

        validation = news_items[0]["copy_generation_validation"]
        self.assertEqual(validation["detail_heading"], "rejected")
        self.assertEqual(validation["reasons"]["detail_heading"], "heading_too_short")
        self.assertEqual(
            _copy_quality_repair_indexes(news_items, contract=TECH_BRIEFING_CONTRACT),
            [1],
        )

    def test_fact_readiness_deduplicates_action_echoes(self) -> None:
        sparse = fact_card_readiness_report(
            {
                "action": "开放企业智能助手",
                "facts": ["该公司开放企业智能助手", "首批面向研发团队"],
            },
            TECH_BRIEFING_CONTRACT,
        )
        ready = fact_card_readiness_report(
            {
                "action": "开放企业智能助手",
                "facts": ["首批面向研发团队", "管理员可以限制应用范围"],
            },
            TECH_BRIEFING_CONTRACT,
        )

        self.assertFalse(sparse["ok"])
        self.assertTrue(ready["ok"])

    def test_contract_metadata_is_attached_without_calling_a_provider(self) -> None:
        article = enhance_article_with_text_models(
            {"source_policy": "smart_manufacturing_daily", "news_items": []},
            {"clients": {}},
        )

        self.assertEqual(article["editorial_contract"]["version"], EDITORIAL_CONTRACT_VERSION)
        self.assertEqual(article["editorial_contract"]["profile"], "smart_manufacturing_daily")


if __name__ == "__main__":
    unittest.main()
