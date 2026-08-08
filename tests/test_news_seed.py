from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.pipeline.news_seed import (
    NEWS_SEED_SCHEMA_VERSION,
    NewsFetchCache,
    _dedupe_items,
    build_news_seed,
    collect_news_items,
    generate_news_seed,
    parse_feed,
)


SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>AI Feed</title>
    <item>
      <title>New AI model ships</title>
      <link>https://example.com/model</link>
      <pubDate>Tue, 02 Jun 2026 08:00:00 GMT</pubDate>
      <description>Useful model update for developers.</description>
    </item>
  </channel>
</rss>
"""


class NewsSeedTest(unittest.TestCase):
    @patch("src.pipeline.news_seed.collect_news_items")
    def test_build_news_seed_keeps_hidden_reserve_candidates(self, collect_news_items_mock) -> None:
        collect_news_items_mock.return_value = {
            "items": [
                {
                    "title": f"AI news {index}",
                    "summary": "Useful AI update.",
                    "source_name": f"Source {index}",
                    "url": f"https://example.com/{index}",
                }
                for index in range(1, 7)
            ],
            "diagnostics": [],
        }

        seed = build_news_seed(
            {"max_items": 3, "reserve_items": 2, "minimum_successful_sources": 0},
            target_date=datetime(2026, 6, 2).date(),
            now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        self.assertEqual(len(seed["news_items"]), 3)
        self.assertEqual(len(seed["reserve_news_items"]), 2)
        self.assertEqual(seed["reserve_news_items"][0]["title"], "AI news 4")

    def test_parse_feed_extracts_rss_item(self) -> None:
        items = parse_feed(SAMPLE_RSS, source_name="Example AI")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "New AI model ships")
        self.assertEqual(items[0]["source_name"], "Example AI")
        self.assertIn("2026-06-02", items[0]["published_at"])

    def test_parse_feed_extracts_media_thumbnail_image(self) -> None:
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <item>
      <title>AI factory ships</title>
      <link>https://example.com/factory</link>
      <pubDate>Tue, 02 Jun 2026 08:00:00 GMT</pubDate>
      <description>Factory AI news.</description>
      <media:thumbnail url="https://img.example.com/factory.jpg" />
    </item>
  </channel>
</rss>
"""

        items = parse_feed(payload, source_name="Example AI")

        self.assertEqual(items[0]["image_url"], "https://img.example.com/factory.jpg")
        self.assertEqual(items[0]["image_source"], "rss")

    def test_parse_feed_prefers_richer_content_encoded_over_short_description(self) -> None:
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title>Factory robot enters pilot production</title>
      <link>https://example.com/factory-robot</link>
      <pubDate>Tue, 02 Jun 2026 08:00:00 GMT</pubDate>
      <description>Factory robot enters pilot production.</description>
      <content:encoded><![CDATA[
        <p>The manufacturer opened a pilot line for the robot.</p>
        <p>The first batch will be tested in two electronics plants before delivery.</p>
      ]]></content:encoded>
    </item>
  </channel>
</rss>
"""

        items = parse_feed(payload, source_name="Example Manufacturing")

        self.assertIn("first batch", items[0]["summary"])
        self.assertIn("two electronics plants", items[0]["summary"])
        self.assertNotEqual(items[0]["summary"], "Factory robot enters pilot production.")

    def test_parse_feed_accepts_numeric_timezone_without_weekday(self) -> None:
        payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Mobile assistant opens device collaboration interface</title>
      <link>https://example.invalid/device-assistant</link>
      <pubDate>2026-06-05 11:39:48  +0800</pubDate>
      <description>Technology briefing item.</description>
    </item>
  </channel>
</rss>
"""

        items = parse_feed(payload, source_name="Example Technology Desk")

        self.assertEqual(items[0]["published_at"], "2026-06-05T11:39:48+08:00")

    def test_build_news_seed_marks_no_items(self) -> None:
        seed = build_news_seed(
            {"sources": []},
            target_date=datetime(2026, 6, 2).date(),
            now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        )

        self.assertEqual(seed["schema_version"], NEWS_SEED_SCHEMA_VERSION)
        self.assertEqual(seed["news_status"], "no_items")
        self.assertEqual(seed["sources"][0]["name"], "待补充：昨日 AI 新闻源")

    def test_generate_news_seed_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "news-sources.yaml"
            config_path.write_text("sources: []\n", encoding="utf-8")

            result = generate_news_seed(
                config_path=config_path,
                output_dir=root,
                now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

            self.assertEqual(result["status"], "no_items")
            self.assertTrue(Path(result["output_path"]).exists())

    def test_source_required_keywords_filter_broad_feeds(self) -> None:
        config = {
            "minimum_successful_sources": 1,
            "sources": [
                {
                    "name": "Broad Tech",
                    "url": "unused",
                    "required_keywords": ["AI", "机器人"],
                }
            ],
        }
        accepted = {
            "title": "微信AI对手机厂商打开一道窄门",
            "summary": "手机厂商正在尝试新的AI助手能力。",
            "source_name": "Broad Tech",
        }
        rejected = {
            "title": "日本或通过抛售美债筹资",
            "summary": "宏观市场新闻。",
            "source_name": "Broad Tech",
        }

        from src.pipeline.news_seed import _matches_source_required_keywords

        self.assertTrue(_matches_source_required_keywords(accepted, config["sources"][0]))
        self.assertFalse(_matches_source_required_keywords(rejected, config["sources"][0]))

    def test_limits_items_per_source(self) -> None:
        from src.pipeline.news_seed import _limit_items_per_source

        items = [
            {"title": "a1", "source_name": "A"},
            {"title": "a2", "source_name": "A"},
            {"title": "a3", "source_name": "A"},
            {"title": "b1", "source_name": "B"},
        ]

        limited = _limit_items_per_source(items, {"max_items_per_source": 2})

        self.assertEqual([item["title"] for item in limited], ["a1", "a2", "b1"])

    def test_priority_keywords_get_extra_score(self) -> None:
        from src.pipeline.news_seed import _news_item_score

        item = {
            "title": "微信AI对手机厂商打开一道窄门",
            "summary": "Agent-to-Agent助手能力进入手机生态。",
            "source_weight": 1.0,
            "source_tier": "editorial",
        }

        self.assertGreaterEqual(
            _news_item_score(item, {"priority_keywords": ["Agent-to-Agent"]}),
            160,
        )

    def test_promotional_wechat_follow_text_blocks_feed_item(self) -> None:
        from src.pipeline.news_seed import _blocked_by_promotional_content

        promotional = {
            "title": "示例效率工具体验记录",
            "summary": "今天效率又提高了 #欢迎关注示例媒体官方微信公众号：示例媒体（微信号：demo_account），更多精彩内容第一时间为您奉上",
            "source_name": "示例媒体",
        }
        normal = {
            "title": "AI 工厂项目进入制造业产线",
            "summary": "企业正在把机器人、质检和排产系统接入工业 AI 平台。",
            "source_name": "Industry News",
        }

        self.assertTrue(_blocked_by_promotional_content(promotional))
        self.assertFalse(_blocked_by_promotional_content(normal))

    def test_source_summary_strips_leading_reporter_and_editor_byline(self) -> None:
        from src.pipeline.news_seed import _source_from_news_item

        source = _source_from_news_item(
            {
                "title": "星河科技回应首席研究员离职传言",
                "summary": "记者丨赵甲 钱乙 编辑丨孙丙 6月14日，示例媒体注意到相关传言。",
                "source_name": "示例财经媒体",
                "published_at": "2026-06-14T18:29:00+08:00",
                "url": "https://example.invalid/company-rumor",
            }
        )

        self.assertIn("2026-06-14T18:29:00+08:00；", source["summary"])
        self.assertIn("示例媒体注意到相关传言", source["summary"])
        self.assertNotIn("记者", source["summary"])
        self.assertNotIn("编辑", source["summary"])

    def test_wechat_platform_risk_blocks_fraud_crime_combo(self) -> None:
        from src.pipeline.news_seed import blocked_by_wechat_platform_risk

        risky = {
            "title": "虚构平台起诉利用AI诈骗大量用户的跨境网络犯罪团伙",
            "summary": "该团伙涉嫌利用 AI 技术诈骗数十万名受害者，并在两周内发送了250万条短信。",
            "source_name": "Example Security Desk",
        }
        safety = {
            "title": "AI 安全公司发布模型滥用治理工具",
            "summary": "企业可用该工具识别异常调用并加强平台风控。",
            "source_name": "Example AI",
        }

        self.assertTrue(blocked_by_wechat_platform_risk(risky))
        self.assertFalse(blocked_by_wechat_platform_risk(safety))

    def test_wechat_platform_risk_blocks_accusation_combo(self) -> None:
        from src.pipeline.news_seed import blocked_by_wechat_platform_risk

        risky = {
            "title": "示例咨询机构的 AI 行业报告被指含有幻觉与虚假案例",
            "summary": "调查发现报告中多个案例不存在，并包含大量错误信息和虚假脚注。",
            "source_name": "Example Technology Desk",
        }
        safety = {
            "title": "AI 安全团队发布报告解释幻觉评测方法",
            "summary": "报告介绍了如何评估模型在问答中的错误率和引用质量。",
            "source_name": "Example AI",
        }

        self.assertTrue(blocked_by_wechat_platform_risk(risky))
        self.assertFalse(blocked_by_wechat_platform_risk(safety))

    def test_wechat_platform_risk_blocks_cyber_leak_combo(self) -> None:
        from src.pipeline.news_seed import blocked_by_wechat_platform_risk

        risky = {
            "title": "虚构代工企业被黑，未发布设备与芯片资料确认泄露",
            "summary": "一家虚构工厂遭遇大规模网络攻击，超过 630GB 的机密数据被窃取，其中包括未发布设备设计图纸和芯片资料。",
            "source_name": "Example Security Desk",
        }
        safety = {
            "title": "工厂上线新的工业 AI 质检系统",
            "summary": "该系统用于识别产线异常并提升良率，帮助工厂把质检结果接入生产流程。",
            "source_name": "Example Manufacturing",
        }

        self.assertTrue(blocked_by_wechat_platform_risk(risky))
        self.assertFalse(blocked_by_wechat_platform_risk(safety))

    def test_wechat_platform_risk_reads_final_visible_copy_fields(self) -> None:
        from src.pipeline.news_seed import blocked_by_wechat_platform_risk

        risky = {
            "title": "虚构设备代工企业",
            "summary": "供应链安全事件。",
            "source_name": "Example Security Desk",
            "detail_heading": "虚构设备代工企业遭网络攻击，机密数据严重泄露",
            "reader_summary": "事件导致超630GB的敏感数据被窃，其中包含尚未发布的设备机型和相关芯片资料。",
        }

        self.assertTrue(blocked_by_wechat_platform_risk(risky))

    def test_shared_fetch_cache_reuses_same_feed_across_article_roles(self) -> None:
        response = Mock(content=SAMPLE_RSS)
        response.raise_for_status.return_value = None
        config = {
            "minimum_successful_sources": 1,
            "fetch_workers": 2,
            "sources": [{"name": "Example AI", "url": "https://example.com/rss", "enabled": True}],
        }
        cache = NewsFetchCache()
        target = datetime(2026, 6, 2).date()

        with patch("src.pipeline.news_seed.requests.get", return_value=response) as request:
            first = collect_news_items(config, target_date=target, fetch_cache=cache)
            second = collect_news_items(config, target_date=target, fetch_cache=cache)

        self.assertEqual(request.call_count, 1)
        self.assertFalse(first["diagnostics"][0]["cache_hit"])
        self.assertTrue(second["diagnostics"][0]["cache_hit"])

    def test_event_dedupe_clusters_syndicated_titles_with_different_urls(self) -> None:
        items = [
            {
                "title": "Northstar AI launches Atlas-6 enterprise agent platform",
                "url": "https://example.invalid/one",
            },
            {
                "title": "Atlas-6 enterprise agent platform launched by Northstar AI",
                "url": "https://example.invalid/two",
            },
        ]

        self.assertEqual(len(_dedupe_items(items)), 1)

    def test_event_dedupe_keeps_distinct_stories_from_same_company(self) -> None:
        items = [
            {
                "title": "Northstar AI launches Atlas-6 enterprise agent platform",
                "url": "https://example.invalid/one",
            },
            {
                "title": "Northstar AI opens a new research office in Harbor City",
                "url": "https://example.invalid/two",
            },
        ]

        self.assertEqual(len(_dedupe_items(items)), 2)

    def test_event_dedupe_merges_two_angles_from_the_same_product_launch(self) -> None:
        items = [
            {
                "title": "远航汽车 R9S 官宣首搭 PhoneLink 小窗模式",
                "summary": "远航R9S预售发布会同时公布纯电续航1100公里。",
                "source_name": "Example Mobility Desk",
                "url": "https://example.invalid/r9s-phonelink",
            },
            {
                "title": "远航 R9S 公布量产版纯电续航，测试工况达 1100km",
                "summary": "远航R9S在同一场预售发布会上公布1100公里纯电续航。",
                "source_name": "Example Mobility Desk",
                "url": "https://example.invalid/r9s-range",
            },
        ]

        self.assertEqual(len(_dedupe_items(items)), 1)

    def test_event_dedupe_merges_same_launch_with_different_angle_numbers(self) -> None:
        items = [
            {
                "title": "远航 R9S 发布：测试续航 1100km",
                "summary": "新车在发布活动上公布续航信息。",
                "source_name": "Example Mobility Desk",
                "url": "https://example.invalid/r9s-range",
            },
            {
                "title": "远航 R9S 发布：支持 7 个设备品牌互联",
                "summary": "新车在发布活动上公布手机互联能力。",
                "source_name": "Example Mobility Desk",
                "url": "https://example.invalid/r9s-connectivity",
            },
        ]

        self.assertEqual(len(_dedupe_items(items)), 1)

    def test_event_dedupe_keeps_different_numbered_products(self) -> None:
        items = [
            {
                "title": "制造企业发布 R100 机器人，首批交付 100 台",
                "source_name": "行业日报",
                "url": "https://example.com/r100",
            },
            {
                "title": "制造企业发布 R200 机器人，首批交付 200 台",
                "source_name": "行业日报",
                "url": "https://example.com/r200",
            },
        ]

        self.assertEqual(len(_dedupe_items(items)), 2)


if __name__ == "__main__":
    unittest.main()
