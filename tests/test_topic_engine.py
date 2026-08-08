from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.pipeline.angle_memory import record_angles
from src.pipeline.topic_engine import select_topic_seed


class TopicEngineTest(unittest.TestCase):
    def test_select_topic_from_legacy_seed_topics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            topics_path = Path(temp_dir) / "topics.yaml"
            topics_path.write_text(
                yaml.safe_dump(
                    {
                        "fixed_columns": [{"name": "AI 今日观察", "enabled": True}],
                        "seed_topics": ["AI 工具怎么用", "普通人如何理解智能体"],
                        "blocked_topics": ["投资荐股"],
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            seed = select_topic_seed(
                topics_path,
                now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

        self.assertEqual(seed["date"], "2026-06-03")
        self.assertIn(seed["topic"], {"AI 工具怎么用", "普通人如何理解智能体"})
        self.assertEqual(seed["column"], "AI 今日观察")
        self.assertTrue(seed["sources"])

    def test_select_topic_from_structured_pool_and_skip_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            topics_path = Path(temp_dir) / "topics.yaml"
            topics_path.write_text(
                yaml.safe_dump(
                    {
                        "topic_pool": [
                            {"topic": "投资荐股 AI 工具", "column": "blocked"},
                            {"topic": "AI 自动化入门", "column": "AI 新手教学"},
                        ],
                        "blocked_topics": ["投资荐股"],
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            seed = select_topic_seed(
                topics_path,
                now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

        self.assertEqual(seed["topic"], "AI 自动化入门")
        self.assertEqual(seed["column"], "AI 新手教学")

    def test_temporary_topic_overrides_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            topics_path = Path(temp_dir) / "topics.yaml"
            topics_path.write_text(
                yaml.safe_dump(
                    {
                        "temporary_topic": {
                            "enabled": True,
                            "topic": "临时热点：AI 浏览器",
                            "column": "AI 今日观察",
                            "expires_on": "2026-06-04",
                        },
                        "seed_topics": ["普通轮换主题"],
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )

            seed = select_topic_seed(
                topics_path,
                now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

        self.assertEqual(seed["topic"], "临时热点：AI 浏览器")
        self.assertEqual(seed["column"], "AI 今日观察")

    def test_recent_angle_memory_skips_matching_topic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            topics_path = root / "topics.yaml"
            topics_path.write_text(
                yaml.safe_dump(
                    {
                        "topic_pool": [
                            {"topic": "AI 自动化入门", "column": "AI 新手教学"},
                            {"topic": "多模态工具白话解释", "column": "AI 深聊延展"},
                        ],
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            memory = root / "recent-angles.jsonl"
            record_angles(
                root,
                [{"role": "secondary", "topic": "AI 自动化入门", "title": "AI 自动化入门"}],
                now=datetime(2026, 6, 2, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            )

            seed = select_topic_seed(
                topics_path,
                now=datetime(2026, 6, 3, 8, 0, tzinfo=timezone(timedelta(hours=8))),
                memory_path=memory,
            )

        self.assertEqual(seed["topic"], "多模态工具白话解释")


if __name__ == "__main__":
    unittest.main()
