from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from src.pipeline.text_model_enhancer import (
    _apply_generated_fact_cards,
    _apply_generated_news_items,
    _apply_batch_model,
    _clean_complete_model_text,
    _clean_model_title,
    _drop_summary_rejected_items,
    _drop_repeated_heading_sentence,
    _usable_reader_summary,
    generate_final_article_title,
    review_final_article_with_text_model,
)


class FakeReviewClient:
    def __init__(self, payload: dict[str, object], *, model: str = "review-model") -> None:
        self.payload = payload
        self.config = SimpleNamespace(model=model, api_base=f"https://{model}.example/v1")
        self.calls = 0

    def chat(self, *_args: object, **_kwargs: object) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


class FailingReviewClient:
    def __init__(self, model: str) -> None:
        self.config = SimpleNamespace(model=model, api_base=f"https://{model}.example/v1")
        self.calls = 0

    def chat(self, *_args: object, **_kwargs: object) -> str:
        self.calls += 1
        raise RuntimeError(f"{self.config.model} unavailable")


class SequenceReviewClient:
    def __init__(self, payloads: list[dict[str, object]], *, model: str = "batch-model") -> None:
        self.payloads = list(payloads)
        self.config = SimpleNamespace(model=model, api_base=f"https://{model}.example/v1")
        self.calls = 0

    def chat(self, *_args: object, **_kwargs: object) -> str:
        self.calls += 1
        return json.dumps(self.payloads.pop(0), ensure_ascii=False)


class TextModelReviewTest(unittest.TestCase):
    def test_summary_candidate_verdict_drops_only_supported_rejection_codes(self) -> None:
        article = {
            "news_items": [
                {"title": "关注我们获取更多内容", "summary": "公众号广告导流", "source_name": "Feed"},
                {"title": "Would you host AI compute at home?", "summary": "A company is testing home compute.", "source_name": "Tech"},
            ]
        }
        generated = [
            {
                "index": 1,
                "fact_card": {"subject": "公众号", "action": "发布导流广告", "facts": ["内容为关注引导"]},
                "candidate_verdict": {
                    "usable": False,
                    "reason_code": "promotional",
                    "reason": "不是独立新闻事件",
                },
            },
            {
                "index": 2,
                "fact_card": {
                    "subject": "一家能源公司",
                    "action": "测试住宅AI算力设备",
                    "facts": ["设备部署在住宅", "试点用于分担家庭能源负载"],
                },
                "candidate_verdict": {"usable": True, "reason_code": "", "reason": "英文不是拒绝理由"},
            },
        ]

        self.assertTrue(_apply_generated_fact_cards(generated, article["news_items"]))
        report = _drop_summary_rejected_items(article)

        self.assertEqual(report["rejected"], 1)
        self.assertEqual(len(article["news_items"]), 1)
        self.assertIn("Would you host", article["news_items"][0]["title"])

    def test_final_title_uses_only_frozen_news_items(self) -> None:
        client = FakeReviewClient(
            {"title": "家庭能源企业测试住宅AI算力；机器人平台开放新接口丨科技早报"},
            model="qwen-title",
        )
        article = {
            "title": "旧标题丨科技早报",
            "source_policy": "tech_briefing",
            "news_items": [
                {
                    "detail_heading": "家庭能源企业测试住宅AI算力设备",
                    "source_name": "Tech",
                    "fact_card": {"subject": "家庭能源企业", "action": "测试住宅AI算力设备", "facts": ["设备部署在住宅"]},
                },
                {
                    "detail_heading": "机器人平台开放新的开发者接口",
                    "source_name": "Industry",
                    "fact_card": {"subject": "机器人平台", "action": "开放开发者接口", "facts": ["接口已经开放"]},
                },
            ],
        }

        title = generate_final_article_title(article, {"clients": {"batch": client}})

        self.assertEqual(title, "家庭能源企业测试住宅AI算力；机器人平台开放新接口丨科技早报")
        self.assertEqual(client.calls, 1)
        self.assertEqual(article["text_model_generation"]["final_title"]["status"], "applied")

    def test_model_title_drops_overflow_clause_instead_of_cutting_it(self) -> None:
        title = (
            "第一条具体新闻进入企业工作流；第二条具体新闻开放开发者接口；"
            "第三条具体新闻补齐多架构部署能力；第四条新闻故意写得很长不能被截成半句丨科技早报"
        )

        cleaned = _clean_model_title(title, limit=64)

        self.assertTrue(cleaned.endswith("丨科技早报"))
        self.assertLessEqual(len(cleaned), 64)
        self.assertNotIn("第四条", cleaned)

    def test_generated_reader_summary_is_never_cut_mid_sentence(self) -> None:
        text = (
            "第一句提供完整事实和背景，说明产品已经进入企业工作流。"
            "第二句补充权限、成本和人工复核要求，确保读者能理解部署条件以及失败后的接管方式。"
            "第三句故意写得很长并超过限制而且没有结束标点因此不能被机械截成半句"
        )

        cleaned = _clean_complete_model_text(text, limit=100, prose=True)

        self.assertTrue(cleaned.endswith("。"))
        self.assertNotIn("第三句", cleaned)

    def test_generated_summary_drops_heading_repetition_and_requires_complete_ending(self) -> None:
        heading = "Northstar AI发布企业工作流能力，智能代理可跨应用执行任务"
        summary = heading + "。管理员可以限制应用权限，并设置人工复核和失败接管节点。"

        cleaned = _drop_repeated_heading_sentence(summary, heading)

        self.assertEqual(cleaned, "管理员可以限制应用权限，并设置人工复核和失败接管节点。")
        complete = (
            cleaned
            + "这项更新还明确了项目上下文的保留范围，并要求管理员记录关键操作和失败接管节点。"
            + "首批开放范围覆盖企业工作区，个人账号暂不在本次更新名单内。"
            + "团队成员只能调用已经授权的文档和应用，权限变化会同步进入工作区审计记录。"
            + "管理员还可以导出变更清单，按时间核对每次授权、删除和人工接管操作。"
        )
        self.assertTrue(_usable_reader_summary(complete))
        self.assertFalse(_usable_reader_summary(cleaned.rstrip("。")))

    def test_unusable_generated_summary_records_rejected_copy_validation(self) -> None:
        news_items = [
            {
                "title": "Northstar Data opens an electrician training program",
                "summary": "Northstar Data is responding to a shortage affecting data-center construction.",
                "detail_heading": "Northstar Data开办电工培训项目，缓解数据中心建设用工短缺",
                "reader_summary": "旧的机械兜底正文。",
            }
        ]
        generated = [
            {
                "index": 1,
                "detail_heading": "Northstar Data开办电工培训项目，缓解数据中心建设用工短缺",
                "reader_summary": "电工短缺已经成为美国扩建数据中心的主要障碍。",
            }
        ]

        _apply_generated_news_items(generated, news_items, allow_title_summary=False)

        self.assertEqual(news_items[0]["reader_summary"], "旧的机械兜底正文。")
        self.assertEqual(
            news_items[0]["copy_generation_validation"]["reader_summary"],
            "rejected",
        )

    def test_missing_generated_item_records_rejected_copy_validation(self) -> None:
        news_items = [
            {
                "title": "制造企业发布机器人新品",
                "detail_heading": "制造企业发布机器人新品，首批设备进入装配测试",
                "reader_summary": "旧的机械兜底正文。",
            },
            {
                "title": "工业软件企业更新排产系统",
                "detail_heading": "工业软件企业更新排产系统，新增跨工厂调度能力",
                "reader_summary": "另一条旧的机械兜底正文。",
            },
        ]
        generated = [
            {
                "index": 1,
                "detail_heading": "制造企业发布机器人新品，首批设备进入装配测试",
                "reader_summary": (
                    "首批设备已经进入汽车装配线测试，企业披露了节拍、精度和人工复核安排。"
                    "后续交付将根据试运行结果分批推进，并保留现场工程师的调整权限。"
                    "项目团队还公布了测试范围和验收流程，便于产线负责人核对每个部署节点。"
                    "每次参数变更都会写入试运行记录，验收人员可以按设备编号追溯调整过程。"
                    "异常停机后仍由现场工程师确认恢复条件，系统不会跳过人工复核直接重启产线。"
                ),
            }
        ]

        _apply_generated_news_items(
            generated,
            news_items,
            allow_title_summary=False,
            expected_indexes=range(1, 3),
        )

        self.assertNotIn("copy_generation_validation", news_items[0])
        validation = news_items[1]["copy_generation_validation"]
        self.assertEqual(validation["detail_heading"], "rejected")
        self.assertEqual(validation["reader_summary"], "rejected")
        self.assertEqual(validation["reason"], "missing_item")

    def test_batch_writer_repairs_thin_items_once_without_rewriting_valid_copy(self) -> None:
        valid_second = (
            "工业软件企业已向三座工厂开放跨厂排产能力，首批用户可以统一查看订单、设备与库存状态。"
            "系统会保留人工确认节点，并记录计划调整的时间、负责人和受影响产线。"
            "现阶段开放范围仅覆盖已接入同一数据平台的生产基地。"
            "各基地仍由现场计划员确认最终排程，异常订单不会自动下发到生产设备，并会保留人工处理记录。"
        )
        repaired_first = (
            "首批版本已经向研发团队开放，可在同一工作区内调用文档、代码与项目资料。"
            "管理员能够限制可访问的应用范围，并为关键操作设置人工复核与失败接管节点。"
            "本次开放仍按企业工作区分批进行，个人账号不在首批名单内。"
            "所有权限调整都会进入工作区审计记录，团队负责人可以按账号核对调用范围。"
        )
        repaired_third = (
            "首套设备已进入电子装配线测试，项目组同步公布了节拍、精度和异常停机处理流程。"
            "现场工程师可以在试运行期间调整参数，每次变更都会写入验收记录。"
            "后续交付将依据测试结果分批推进，并由产线负责人完成最终确认。"
            "设备出现异常停机时会保留故障代码和人工处理记录，供项目组复盘测试结果。"
        )
        client = SequenceReviewClient(
            [
                {
                    "items": [
                        {
                            "index": 1,
                            "detail_heading": "甲公司开放企业智能助手，首批面向研发团队",
                            "reader_summary": "首批版本已经向研发团队开放，并支持调用项目资料。",
                        },
                        {
                            "index": 2,
                            "detail_heading": "工业软件企业开放跨厂排产，覆盖三座生产基地",
                            "reader_summary": valid_second,
                        },
                    ]
                },
                {
                    "items": [
                        {
                            "index": 1,
                            "detail_heading": "甲公司开放企业智能助手，首批面向研发团队",
                            "reader_summary": repaired_first,
                        },
                        {
                            "index": 3,
                            "detail_heading": "机器人设备进入装配测试，交付将按验收结果推进",
                            "reader_summary": repaired_third,
                        },
                    ]
                },
            ]
        )
        news_items = [
            {
                "title": "甲公司开放企业智能助手",
                "summary": "企业智能助手向研发团队开放，并支持调用项目资料。",
                "detail_heading": "甲公司开放企业智能助手",
                "reader_summary": "旧正文。",
                "fact_card": {"subject": "甲公司", "action": "开放企业智能助手", "facts": ["首批面向研发团队"]},
            },
            {
                "title": "工业软件企业开放跨厂排产",
                "summary": "跨厂排产能力覆盖三座生产基地。",
                "detail_heading": "工业软件企业开放跨厂排产",
                "reader_summary": "旧正文。",
                "fact_card": {"subject": "工业软件企业", "action": "开放跨厂排产", "facts": ["覆盖三座生产基地"]},
            },
            {
                "title": "机器人设备进入装配测试",
                "summary": "机器人设备进入电子装配线测试。",
                "detail_heading": "机器人设备进入装配测试",
                "reader_summary": "旧正文。",
                "fact_card": {"subject": "机器人设备", "action": "进入装配测试", "facts": ["进入电子装配线"]},
            },
        ]

        result = _apply_batch_model({"title": "测试丨科技早报", "news_items": news_items}, client)

        self.assertEqual(client.calls, 2)
        self.assertEqual(result.quality_repair_chunks, 1)
        self.assertEqual(result.quality_repair_items, 2)
        self.assertEqual(result.quality_repaired_items, 2)
        self.assertEqual(news_items[0]["reader_summary"], repaired_first)
        self.assertEqual(news_items[1]["reader_summary"], valid_second)
        self.assertEqual(news_items[2]["reader_summary"], repaired_third)

    def test_final_reviewer_records_issue_without_rewriting_copy(self) -> None:
        phrase = "这条消息值得关注，因为它反映了AI正在进入工作流程"
        article = {
            "title": "Northstar AI发布新工具丨科技早报",
            "digest": "今日科技动态",
            "fact_cards": [{"index": 1, "subject": "Northstar AI", "action": "发布新工具"}],
            "news_items": [
                {
                    "title": "Northstar AI发布新工具",
                    "detail_heading": "Northstar AI发布新工具",
                    "reader_summary": phrase,
                    "fact_card": {
                        "subject": "Northstar AI",
                        "action": "发布新工具",
                        "facts": ["Northstar AI发布新工具"],
                    },
                }
            ],
        }
        client = FakeReviewClient(
            {
                "risk_level": "medium",
                "summary": "存在固定口癖",
                "issues": [
                    {
                        "type": "ai_cliche",
                        "severity": "medium",
                        "news_index": 1,
                        "evidence": phrase,
                        "action": "repair_field",
                        "reason": "句式模板化",
                    }
                ],
            }
        )
        original = article["news_items"][0]["reader_summary"]

        reviewed = review_final_article_with_text_model(
            article,
            {"clients": {"polish": client}, "roles": {"polish": {"enabled": True}}},
            visible_html=f"<p>{phrase}</p>",
            mode="shadow",
        )

        self.assertEqual(reviewed["news_items"][0]["reader_summary"], original)
        self.assertEqual(reviewed["final_ai_review"]["issues"][0]["type"], "ai_cliche")
        self.assertTrue(reviewed["text_model_generation"]["roles"]["polish"]["reviewer_only"])

    def test_final_reviewer_drops_issue_without_visible_evidence(self) -> None:
        client = FakeReviewClient(
            {
                "risk_level": "high",
                "issues": [
                    {
                        "type": "platform_risk",
                        "severity": "high",
                        "evidence": "不存在于终稿的句子",
                        "action": "block",
                        "reason": "unsupported",
                    }
                ],
            }
        )

        reviewed = review_final_article_with_text_model(
            {"title": "正常标题", "digest": "正常摘要", "news_items": [], "fact_cards": []},
            {"clients": {"polish": client}, "roles": {"polish": {"enabled": True}}},
            visible_html="<p>正常终稿</p>",
            mode="shadow",
        )

        self.assertEqual(reviewed["final_ai_review"]["issues"], [])
        self.assertEqual(reviewed["final_ai_review"]["risk_level"], "low")

    def test_final_reviewer_converts_rejected_item_verdict_to_recovery_issue(self) -> None:
        evidence = "记者丨某某 编辑丨某某"
        client = FakeReviewClient(
            {
                "risk_level": "medium",
                "issues": [],
                "item_verdicts": [
                    {
                        "news_index": 2,
                        "verdict": "reject",
                        "type": "byline",
                        "severity": "medium",
                        "evidence": evidence,
                        "action": "drop_item",
                        "reason": "媒体署名进入正文",
                    }
                ],
            }
        )

        reviewed = review_final_article_with_text_model(
            {
                "title": "正常标题丨科技早报",
                "digest": "正常摘要",
                "news_items": [{"detail_heading": "第一条"}, {"detail_heading": evidence}],
                "fact_cards": [],
            },
            {"clients": {"polish": client}, "roles": {"polish": {"enabled": True}}},
            visible_html=f"<p>第一条</p><p>{evidence}</p>",
            mode="enforce",
        )

        issue = reviewed["final_ai_review"]["issues"][0]
        self.assertEqual(issue["news_index"], 2)
        self.assertEqual(issue["type"], "byline")
        self.assertEqual(issue["action"], "drop_item")

    def test_final_reviewer_normalizes_category_mismatch_to_off_topic(self) -> None:
        evidence = "电视剧演员署名新规今起施行"
        client = FakeReviewClient(
            {
                "risk_level": "medium",
                "issues": [
                    {
                        "type": "repetition",
                        "severity": "medium",
                        "news_index": 3,
                        "evidence": evidence,
                        "action": "repair_field",
                        "reason": "该新闻与智能制造无关，分类严重错位。",
                    }
                ],
            }
        )

        reviewed = review_final_article_with_text_model(
            {"title": "智能制造日报", "digest": "产业动态", "news_items": [], "fact_cards": []},
            {"clients": {"polish": client}, "roles": {"polish": {"enabled": True}}},
            visible_html=f"<p>{evidence}</p>",
            mode="enforce",
        )

        issue = reviewed["final_ai_review"]["issues"][0]
        self.assertEqual(issue["type"], "off_topic")
        self.assertEqual(issue["action"], "drop_item")

    def test_final_reviewer_does_not_fan_out_after_failure(self) -> None:
        primary = FailingReviewClient("claude-review")
        fallback = FakeReviewClient({"risk_level": "low", "issues": []}, model="gemini-review")

        reviewed = review_final_article_with_text_model(
            {"title": "正常标题", "digest": "正常摘要", "news_items": [], "fact_cards": []},
            {
                "clients": {"polish": primary, "summary": fallback},
                "roles": {"polish": {"enabled": True}, "summary": {"enabled": True}},
            },
            visible_html="<p>正常终稿</p>",
            mode="enforce",
        )

        self.assertEqual(reviewed["final_ai_review"]["status"], "degraded_applied")
        self.assertEqual(reviewed["final_ai_review"]["reviewer_role"], "deterministic")
        self.assertTrue(reviewed["final_ai_review"]["fallback_used"])
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_all_reviewer_failures_use_explicit_deterministic_review(self) -> None:
        reviewed = review_final_article_with_text_model(
            {
                "title": "安全治理更新",
                "digest": "公开报道摘要",
                "news_items": [
                    {
                        "title": "谷歌起诉利用AI诈骗数十万人的中国网络犯罪团伙",
                        "summary": "该团伙涉嫌利用 AI 诈骗受害者并大规模发送短信。",
                        "detail_heading": "高风险新闻标题",
                        "reader_summary": "中国犯罪团伙涉嫌利用AI诈骗数十万受害者并大规模发送短信。",
                    }
                ],
                "fact_cards": [],
            },
            {
                "clients": {"polish": FailingReviewClient("claude-review")},
                "roles": {"polish": {"enabled": True}},
            },
            visible_html="<p>高风险新闻标题</p>",
            mode="enforce",
        )

        self.assertEqual(reviewed["final_ai_review"]["status"], "degraded_applied")
        self.assertEqual(reviewed["final_ai_review"]["reviewer_role"], "deterministic")
        self.assertEqual(reviewed["final_ai_review"]["issues"][0]["type"], "platform_risk")


if __name__ == "__main__":
    unittest.main()
