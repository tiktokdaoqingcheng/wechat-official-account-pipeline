from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.pipeline.content_package import build_content_package
from src.pipeline.package_manifest import verify_frozen_package
from src.pipeline.repair_content_package import _refresh_inline_image_text, repair_content_package
from src.review.publish_policy import decide_publish_action


class RepairContentPackageTest(unittest.TestCase):
    def test_inline_image_text_uses_source_item_index(self) -> None:
        article = {
            "title": "科技早报",
            "news_items": [
                {"title": f"新闻{index}", "source_name": f"来源{index}"}
                for index in range(1, 6)
            ],
            "inline_images": [
                {
                    "source_item_index": "4",
                    "alt": "旧配图",
                    "caption": "旧来源",
                    "prompt": "News cluster: old\nArticle title context: old",
                }
            ],
        }

        _refresh_inline_image_text(article)

        image = article["inline_images"][0]
        self.assertEqual(image["alt"], "新闻5配图")
        self.assertEqual(image["caption"], "来源配图：来源5")
        self.assertIn("News cluster: 新闻5", image["prompt"])

    def test_repair_removes_secondary_cyber_leak_item_and_allows_publish(self) -> None:
        schedule = {
            "daily_content_package": {"article_count": 2},
            "publishing": {"mode": "auto_publish_low_risk", "allow_low_risk_auto_publish": True},
        }
        safety = {"blocked_claim_types": ["investment_advice"]}
        article_seeds = {
            "primary": {
                "topic": "昨天 AI 界发生了什么",
                "column": "科技早报",
                "audience": "对 AI 感兴趣的读者",
                "target_date": "2026-06-25",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "虚构设备代工企业遭网络攻击，大量未发布设计资料泄露",
                        "source_name": "Example Security Desk",
                        "url": "https://example.invalid/primary-leak",
                        "published_at": "2026-06-25T21:30:00+08:00",
                        "summary": "虚构设备代工企业遭遇网络攻击，涉及未发布设备与芯片资料泄露。",
                    },
                    {
                        "title": "Northstar AI发布智能体工作流研究",
                        "source_name": "Example AI Desk",
                        "url": "https://example.invalid/agent-research",
                        "published_at": "2026-06-25T10:00:00+08:00",
                        "summary": "研究展示智能体在复杂任务中的生产力提升。",
                    }
                ],
                "sources": [
                    {"name": "Example AI Desk", "url": "https://example.invalid/agent-research", "summary": "研究展示智能体。"}
                ],
            },
            "secondary": {
                "topic": "智能制造日报",
                "column": "智能制造日报",
                "audience": "产业读者",
                "source_policy": "smart_manufacturing_daily",
                "target_date": "2026-06-25",
                "news_status": "ok",
                "news_items": [
                    {
                        "title": "虚构设备代工企业被黑，未发布设备与芯片资料确认泄露",
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
                "sources": [
                    {
                        "name": "Example Manufacturing Desk",
                        "url": "https://example.invalid/embodied",
                        "summary": "具身智能产业链进入量产验证阶段。",
                    }
                ],
            },
        }
        now = datetime(2026, 6, 26, 8, 0, tzinfo=timezone(timedelta(hours=8)))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source"
            repair_dir = root / "repair"
            provisional = decide_publish_action(schedule, {"risk_level": "low", "flags": {}, "wechat_ready": True})
            build_content_package(
                schedule_config=schedule,
                article_seeds=article_seeds,
                safety_config=safety,
                publish_decision=provisional,
                output_dir=source_dir,
                seed_source="test",
                already_published_today=False,
                now=now,
            )
            result = repair_content_package(
                package_path=source_dir / "content-package.json",
                output_dir=repair_dir,
                schedule_path=_write_yaml(root / "schedule.yaml", schedule),
                safety_path=_write_yaml(root / "safety.yaml", safety),
                env_path=root / ".env",
                now=now,
            )

            repaired_package = json.loads((repair_dir / "content-package.json").read_text(encoding="utf-8"))
            repaired_primary = json.loads((repair_dir / "primary" / "article.json").read_text(encoding="utf-8"))
            repaired_secondary = json.loads((repair_dir / "secondary" / "article.json").read_text(encoding="utf-8"))
            primary_html = (repair_dir / "primary" / "article.html").read_text(encoding="utf-8")
            manifest = verify_frozen_package(
                repair_dir / "content-package.json",
                manifest_path=result["artifact_manifest"]["path"],
            )
            manifest_path_exists = (repair_dir / "content-package.manifest.json").exists()

        primary_titles = [item["title"] for item in repaired_primary["news_items"]]
        titles = [item["title"] for item in repaired_secondary["news_items"]]

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["removed_items"]), 2)
        self.assertNotIn("虚构设备代工企业遭网络攻击，大量未发布设计资料泄露", primary_titles)
        self.assertIn("Northstar AI发布智能体工作流研究", primary_titles)
        self.assertNotIn("虚构设备代工企业", repaired_primary["title"])
        self.assertNotIn("虚构设备代工企业", primary_html)
        self.assertNotIn("虚构设备代工企业被黑，未发布设备与芯片资料确认泄露", titles)
        self.assertIn("具身智能产业链规模冲刺万亿", titles)
        self.assertNotIn("虚构设备代工企业", repaired_secondary["title"])
        self.assertNotIn("虚构设备代工企业", repaired_secondary["digest"])
        self.assertNotIn(
            "虚构设备代工企业",
            json.dumps(repaired_secondary.get("inline_images", []), ensure_ascii=False),
        )
        self.assertEqual(repaired_package["package_risk_level"], "low")
        self.assertTrue(repaired_package["wechat_ready"])
        self.assertEqual(repaired_package["publish_decision"]["action"], "publish")
        self.assertIn("final_ai_review", repaired_primary)
        self.assertIn("final_ai_review", repaired_secondary)
        self.assertTrue(manifest["ok"])
        self.assertTrue(manifest_path_exists)


def _write_yaml(path: Path, data: dict[str, object]) -> Path:
    import yaml

    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
