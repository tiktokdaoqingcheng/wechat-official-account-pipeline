from __future__ import annotations

import unittest

from src.pipeline.content_engine import build_article
from src.review.review_engine import REVIEW_SCHEMA_VERSION, review_article, validate_review


class ReviewEngineTest(unittest.TestCase):
    def test_review_low_risk_article(self) -> None:
        article = build_article(
            {
                "topic": "AI 自动化入门",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )

        review = review_article(article, {"blocked_claim_types": []})

        self.assertEqual(review["schema_version"], REVIEW_SCHEMA_VERSION)
        self.assertEqual(review["risk_level"], "low")
        self.assertTrue(review["wechat_ready"])
        self.assertEqual(validate_review(review), [])

    def test_local_only_source_marks_fact_uncertain(self) -> None:
        article = build_article(
            {
                "topic": "AI 新闻对普通人的影响",
                "audience": "新手读者",
                "sources": [
                    {
                        "name": "本地选题配置",
                        "url": "config/topics.yaml",
                        "summary": "本地配置来源",
                    }
                ],
            }
        )

        review = review_article(article, {"blocked_claim_types": []})

        self.assertEqual(review["risk_level"], "medium")
        self.assertFalse(review["wechat_ready"])
        self.assertTrue(review["flags"]["fact_uncertain"])

    def test_local_topic_explainer_source_can_pass_for_secondary_article(self) -> None:
        article = build_article(
            {
                "topic": "大模型、智能体、多模态、自动化的白话解释",
                "audience": "新手读者",
                "source_policy": "local_topic_explainer",
                "sources": [
                    {
                        "name": "昨日讨论记录",
                        "url": "config/topics.yaml",
                        "summary": "副文章承接前一天与用户讨论后确定的主题。",
                    }
                ],
            }
        )

        review = review_article(article, {"blocked_claim_types": []})

        self.assertEqual(review["risk_level"], "low")
        self.assertTrue(review["wechat_ready"])
        self.assertFalse(review["flags"]["fact_uncertain"])

    def test_local_topic_explainer_source_can_pass_from_shared_topics_path(self) -> None:
        article = build_article(
            {
                "topic": "AI 新闻对普通人的影响",
                "audience": "新手读者",
                "source_policy": "local_topic_explainer",
                "sources": [
                    {
                        "name": "本地选题配置",
                        "url": "/srv/example-wechat-automation/topics.yaml",
                        "summary": "该选题来自本地配置。",
                    }
                ],
            }
        )

        review = review_article(article, {"blocked_claim_types": []})

        self.assertEqual(review["risk_level"], "low")
        self.assertTrue(review["wechat_ready"])
        self.assertFalse(review["flags"]["fact_uncertain"])

    def test_high_risk_claim_keywords_are_blocked(self) -> None:
        article = build_article(
            {
                "topic": "AI 投资建议和保证收益",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )

        review = review_article(article, {"blocked_claim_types": ["investment_advice", "guaranteed_income"]})

        self.assertEqual(review["risk_level"], "high")
        self.assertFalse(review["wechat_ready"])
        self.assertTrue(review["flags"]["sensitive_topic"])

    def test_medical_news_context_is_not_medical_advice(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        article["sections"][0]["paragraphs"][0] = "智能体 AI 被讨论用于缓解医疗服务压力，但需要明确审核和人工把关。"

        review = review_article(article, {"blocked_claim_types": ["medical_advice"]})

        self.assertEqual(review["risk_level"], "low")
        self.assertEqual(review["blocked_claim_hits"], [])

    def test_ai_medical_diagnosis_news_is_not_medical_advice(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        article["sections"][0]["paragraphs"][0] = (
            "研究人员报道，Northstar AI 推理模型被用于辅助罕见病诊断研究，并帮助医生发现新的诊断线索。"
            "文章强调这仍需要临床医生和人工审核，不是面向读者的诊疗建议。"
        )

        review = review_article(article, {"blocked_claim_types": ["medical_advice"]})

        self.assertEqual(review["risk_level"], "low")
        self.assertEqual(review["blocked_claim_hits"], [])

    def test_ai_health_response_quality_news_is_not_medical_advice(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        article["sections"][0]["paragraphs"][0] = (
            "Northstar AI介绍模型提升健康信息回答质量，并通过医生参与评估减少医疗建议场景中的幻觉与误导。"
            "这是一条产品能力和安全评估新闻，不提供诊疗建议。"
        )

        review = review_article(article, {"blocked_claim_types": ["medical_advice"]})

        self.assertEqual(review["risk_level"], "low")
        self.assertEqual(review["blocked_claim_hits"], [])

    def test_medical_advice_context_is_still_blocked(self) -> None:
        article = build_article(
            {
                "topic": "如何诊断和治疗疾病",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        article["sections"][0]["paragraphs"][0] = "这里提供如何诊断和治疗疾病的医疗建议，包括自行用药和无需就医的判断方法。"

        review = review_article(article, {"blocked_claim_types": ["medical_advice"]})

        self.assertEqual(review["risk_level"], "high")
        self.assertTrue(review["blocked_claim_hits"])

    def test_investment_disclaimer_is_not_investment_advice(self) -> None:
        article = build_article(
            {
                "topic": "智能制造日报",
                "audience": "产业读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        safe_disclaimers = [
            "本文基于公开信息进行整理，不构成投资建议。",
            "本文基于公开资料梳理，不构成具体投资建议。",
            "本文基于公开信息整理，未对单条新闻作投资建议或结论。",
            "本文仅梳理产业动态，不作投资建议。",
            "本文基于公开资料进行整理，不为任何新闻背书投资建议。",
        ]

        for disclaimer in safe_disclaimers:
            with self.subTest(disclaimer=disclaimer):
                article["sections"][0]["paragraphs"][0] = disclaimer

                review = review_article(article, {"blocked_claim_types": ["investment_advice"]})

                self.assertEqual(review["risk_level"], "low")
                self.assertEqual(review["blocked_claim_hits"], [])

    def test_real_investment_advice_stays_blocked(self) -> None:
        article = build_article(
            {
                "topic": "AI 投资建议和保证收益",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        risky_claims = [
            "这家公司值得买入，本文给出具体投资建议。",
            "这只股票适合长期持有，属于确定性荐股机会。",
            "这个项目保证收益，普通人也可以稳赚。",
        ]

        for claim in risky_claims:
            with self.subTest(claim=claim):
                article["sections"][0]["paragraphs"][0] = claim

                review = review_article(article, {"blocked_claim_types": ["investment_advice", "guaranteed_income"]})

                self.assertEqual(review["risk_level"], "high")
                self.assertTrue(review["blocked_claim_hits"])

    def test_negated_guaranteed_income_context_is_not_blocked(self) -> None:
        article = build_article(
            {
                "topic": "AI 基础设施商业模式",
                "audience": "产业读者",
                "sources": [
                    {
                        "name": "Example Infrastructure Desk：算力服务并非稳赚不赔",
                        "url": "https://example.invalid/token-economics",
                        "summary": "AI 推理时代的 Token 工厂",
                    }
                ],
            }
        )
        article["sections"][0]["paragraphs"][0] = (
            "文件显示，高昂的算力成本与激烈的价格战使得卖 Token 并非稳赚不赔的生意。"
        )

        review = review_article(article, {"blocked_claim_types": ["investment_advice", "guaranteed_income"]})

        self.assertEqual(review["risk_level"], "low")
        self.assertEqual(review["blocked_claim_hits"], [])

    def test_legal_news_context_is_not_legal_advice(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        article["sections"][0]["paragraphs"][0] = "谷歌提起诉讼，指控一个网络犯罪团伙利用 AI 发送诈骗短信。"

        review = review_article(article, {"blocked_claim_types": ["legal_advice"]})

        self.assertEqual(review["risk_level"], "low")
        self.assertEqual(review["blocked_claim_hits"], [])

    def test_legal_advice_context_is_still_blocked(self) -> None:
        article = build_article(
            {
                "topic": "合同纠纷诉讼策略",
                "audience": "新手读者",
                "sources": [{"name": "source", "url": "https://example.com", "summary": "summary"}],
            }
        )
        article["sections"][0]["paragraphs"][0] = "这里给出合同纠纷的诉讼建议和起诉模板，并保证胜诉。"

        review = review_article(article, {"blocked_claim_types": ["legal_advice"]})

        self.assertEqual(review["risk_level"], "high")
        self.assertFalse(review["wechat_ready"])
        self.assertTrue(review["blocked_claim_hits"])

    def test_placeholder_news_source_marks_fact_uncertain(self) -> None:
        article = build_article(
            {
                "topic": "昨天 AI 界发生了什么",
                "audience": "新手读者",
                "sources": [
                    {
                        "name": "待补充：昨日 AI 新闻源",
                        "url": "data/news-seeds/2026-06-02-ai-news.json",
                        "summary": "主文章需要接入前一天可核验 AI 新闻源；当前为结构化占位来源。",
                    }
                ],
            }
        )

        review = review_article(article, {"blocked_claim_types": []})

        self.assertEqual(review["risk_level"], "medium")
        self.assertFalse(review["wechat_ready"])
        self.assertIn("placeholder_source_requires_fact_check", review["checks"][3]["details"])

    def test_validate_review_rejects_missing_flags(self) -> None:
        errors = validate_review({"schema_version": REVIEW_SCHEMA_VERSION, "risk_level": "low", "wechat_ready": True})

        self.assertTrue(any("flags must be an object" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
