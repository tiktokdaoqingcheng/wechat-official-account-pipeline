from __future__ import annotations

import unittest

from src.pipeline.fact_cards import attach_fact_cards, build_fact_card


class FactCardsTest(unittest.TestCase):
    def test_fact_card_keeps_source_facts_and_numbers(self) -> None:
        card = build_fact_card(
            {
                "title": "远航设备与星云芯片签署超300亿美元芯片协议",
                "summary": "双方计划生产超过150亿颗芯片，并扩建制造设施。",
                "source_name": "Example Technology Desk",
                "url": "https://example.invalid/chip-agreement",
                "published_at": "2026-07-08T18:20:27+08:00",
            }
        )

        self.assertIn("远航设备", card["subject"])
        self.assertIn("300亿美元", card["numbers"])
        self.assertIn("150亿", card["numbers"])
        self.assertEqual(card["confidence"], "high")

    def test_fact_card_removes_promotional_tail(self) -> None:
        article = attach_fact_cards(
            {
                "news_items": [
                    {
                        "title": "AI工具更新",
                        "summary": "产品增加新的数据整理能力。欢迎关注官方微信公众号，更多精彩内容第一时间为您奉上",
                        "source_name": "Example",
                        "url": "https://example.com/item",
                    }
                ]
            }
        )

        facts = "\n".join(article["news_items"][0]["fact_card"]["facts"])
        self.assertNotIn("欢迎关注", facts)
        self.assertNotIn("微信公众号", facts)
        self.assertNotIn("欢迎关注", article["news_items"][0]["summary"])
        self.assertTrue(article["news_items"][0]["summary"].endswith("。"))

    def test_promotional_footer_does_not_reject_substantive_news(self) -> None:
        article = attach_fact_cards(
            {
                "news_items": [
                    {
                        "title": "机器人企业开放首条试产线",
                        "summary": (
                            "首批设备已经进入两家电子工厂连续测试，企业计划完成三个月的节拍、"
                            "精度和故障率验证后启动分批交付。欢迎关注官方微信公众号。"
                        ),
                        "source_name": "Example Manufacturing",
                        "url": "https://example.com/robot-line",
                    }
                ]
            }
        )

        item = article["news_items"][0]
        self.assertTrue(item["candidate_verdict"]["usable"])
        self.assertNotIn("微信公众号", item["summary"])

    def test_attach_fact_cards_rejects_title_only_source_evidence(self) -> None:
        article = attach_fact_cards(
            {
                "news_items": [
                    {
                        "title": "机器人企业开放首条试产线",
                        "summary": "机器人企业开放首条试产线。",
                        "source_name": "Example Manufacturing",
                        "url": "https://example.com/robot-line",
                    }
                ]
            }
        )

        verdict = article["news_items"][0]["candidate_verdict"]
        self.assertFalse(verdict["usable"])
        self.assertEqual(verdict["reason_code"], "insufficient_facts")

    def test_attach_fact_cards_accepts_incremental_source_evidence(self) -> None:
        article = attach_fact_cards(
            {
                "news_items": [
                    {
                        "title": "机器人企业开放首条试产线",
                        "summary": "首批设备已经进入两座电子工厂进行连续测试，企业计划在完成三个月的节拍、精度和故障率验证后启动分批交付。",
                        "source_name": "Example Manufacturing",
                        "url": "https://example.com/robot-line",
                    }
                ]
            }
        )

        self.assertTrue(article["news_items"][0]["candidate_verdict"]["usable"])


if __name__ == "__main__":
    unittest.main()
