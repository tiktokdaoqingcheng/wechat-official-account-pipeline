from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from src.pipeline.create_draft_from_package import (
    _cover_crop_fields,
    _replace_illustration_placeholder,
    _replace_inline_image_placeholders,
    _wechat_safe_title,
    validate_draft_package,
)
from src.integrations.wechat_client import WeChatClient


class CreateDraftFromPackageTest(unittest.TestCase):
    def test_validate_draft_package_accepts_two_article_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for role in ("primary", "secondary"):
                role_dir = root / role
                role_dir.mkdir()
                (role_dir / "article.json").write_text(json.dumps({"title": role}), encoding="utf-8")
                (role_dir / "article.html").write_text("<section>ok</section>", encoding="utf-8")
                (role_dir / "cover.png").write_bytes(b"png")
                (role_dir / "illustration.png").write_bytes(b"png")
                (role_dir / "inline-1.png").write_bytes(b"png")

            package = {
                "schema_version": "content_package.v1",
                "article_count": 2,
                "files": {
                    role: {
                        "article_json": str(root / role / "article.json"),
                        "article_html": str(root / role / "article.html"),
                        "cover": str(root / role / "cover.png"),
                        "illustration": str(root / role / "illustration.png"),
                        "inline_images": [str(root / role / "inline-1.png")],
                    }
                    for role in ("primary", "secondary")
                },
            }

            self.assertEqual(validate_draft_package(package), [])

    def test_validate_draft_package_accepts_three_article_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for role in ("primary", "secondary", "tertiary"):
                role_dir = root / role
                role_dir.mkdir()
                (role_dir / "article.json").write_text(json.dumps({"title": role}), encoding="utf-8")
                (role_dir / "article.html").write_text("<section>ok</section>", encoding="utf-8")
                (role_dir / "cover.png").write_bytes(b"png")
                (role_dir / "illustration.png").write_bytes(b"png")

            package = {
                "schema_version": "content_package.v1",
                "article_count": 3,
                "article_roles": ["primary", "secondary", "tertiary"],
                "files": {
                    role: {
                        "article_json": str(root / role / "article.json"),
                        "article_html": str(root / role / "article.html"),
                        "cover": str(root / role / "cover.png"),
                        "illustration": str(root / role / "illustration.png"),
                        "inline_images": [],
                    }
                    for role in ("primary", "secondary", "tertiary")
                },
            }

            self.assertEqual(validate_draft_package(package), [])

    def test_add_draft_articles_posts_articles_array(self) -> None:
        client = WeChatClient("app", "secret")
        client.session = FakeSession({"media_id": "draft-media"})

        result = client.add_draft_articles("token", [{"title": "one"}, {"title": "two"}])

        self.assertEqual(result["media_id"], "draft-media")
        payload = json.loads(client.session.last_data.decode("utf-8"))
        self.assertEqual(len(payload["articles"]), 2)

    def test_upload_article_image_uses_uploadimg_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "body.png"
            image_path.write_bytes(b"png")
            client = WeChatClient("app", "secret")
            client.session = FakeSession({"url": "https://mmbiz.qpic.cn/body.png"})

            result = client.upload_article_image("token", image_path)

        self.assertEqual(result["url"], "https://mmbiz.qpic.cn/body.png")
        self.assertIn("/cgi-bin/media/uploadimg", client.session.last_url)

    def test_mass_send_all_posts_mpnews_payload(self) -> None:
        client = WeChatClient("app", "secret")
        client.session = FakeSession({"errcode": 0, "msg_id": 123, "msg_data_id": 456})

        result = client.send_mass_mpnews_to_all(
            "token",
            "draft-media-id",
            send_ignore_reprint=True,
            clientmsgid="dedupe-id",
        )

        self.assertEqual(result["msg_id"], 123)
        self.assertIn("/cgi-bin/message/mass/sendall", client.session.last_url)
        payload = json.loads(client.session.last_data.decode("utf-8"))
        self.assertEqual(payload["filter"], {"is_to_all": True})
        self.assertEqual(payload["mpnews"], {"media_id": "draft-media-id"})
        self.assertEqual(payload["msgtype"], "mpnews")
        self.assertEqual(payload["send_ignore_reprint"], 1)
        self.assertEqual(payload["clientmsgid"], "dedupe-id")

    def test_get_mass_send_status_posts_msg_id(self) -> None:
        client = WeChatClient("app", "secret")
        client.session = FakeSession({"msg_id": 123, "msg_status": "SEND_SUCCESS"})

        result = client.get_mass_send_status("token", "123")

        self.assertEqual(result["msg_status"], "SEND_SUCCESS")
        self.assertIn("/cgi-bin/message/mass/get", client.session.last_url)
        payload = json.loads(client.session.last_data.decode("utf-8"))
        self.assertEqual(payload, {"msg_id": "123"})

    def test_replace_illustration_placeholder(self) -> None:
        content = '<img src="{{ARTICLE_ILLUSTRATION_PRIMARY}}" />'

        replaced = _replace_illustration_placeholder(
            content,
            placeholder="{{ARTICLE_ILLUSTRATION_PRIMARY}}",
            url="https://mmbiz.qpic.cn/body.png",
        )

        self.assertIn("https://mmbiz.qpic.cn/body.png", replaced)
        self.assertNotIn("ARTICLE_ILLUSTRATION", replaced)

    def test_replace_inline_image_placeholders(self) -> None:
        content = '<img src="{{ARTICLE_INLINE_PRIMARY_1}}" /><img src="{{ARTICLE_INLINE_PRIMARY_2}}" />'

        replaced = _replace_inline_image_placeholders(
            content,
            article={
                "inline_images": [
                    {"placeholder": "{{ARTICLE_INLINE_PRIMARY_1}}"},
                    {"placeholder": "{{ARTICLE_INLINE_PRIMARY_2}}"},
                ]
            },
            inline_uploads=[
                {"url": "https://mmbiz.qpic.cn/inline-1.png"},
                {"url": "https://mmbiz.qpic.cn/inline-2.png"},
            ],
        )

        self.assertIn("https://mmbiz.qpic.cn/inline-1.png", replaced)
        self.assertIn("https://mmbiz.qpic.cn/inline-2.png", replaced)
        self.assertNotIn("ARTICLE_INLINE", replaced)

    def test_cover_crop_fields_use_png_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cover = Path(temp_dir) / "cover.png"
            cover.write_bytes(_png_header(width=1536, height=1024))

            fields = _cover_crop_fields(cover)

        self.assertEqual(fields["pic_crop_235_1"], "0_0.180851_1_0.819149")
        self.assertEqual(fields["pic_crop_1_1"], "0.166667_0_0.833333_1")

    def test_cover_crop_fields_snap_near_full_frame_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cover = Path(temp_dir) / "cover.png"
            cover.write_bytes(_png_header(width=900, height=383))

            fields = _cover_crop_fields(cover)

        self.assertEqual(fields["pic_crop_235_1"], "0_0_1_1")
        self.assertEqual(fields["pic_crop_1_1"], "0.287222_0_0.712778_1")

    def test_cover_crop_fields_fall_back_for_non_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cover = Path(temp_dir) / "cover.jpg"
            cover.write_bytes(b"not a png")

            fields = _cover_crop_fields(cover)

        self.assertEqual(fields["pic_crop_235_1"], "0_0_1_1")
        self.assertEqual(fields["pic_crop_1_1"], "0_0_1_1")

    def test_wechat_safe_title_preserves_suffix_and_caps_length(self) -> None:
        title = "星河通信完成数千万元融资；2.8万台机器人获得示范区通行许可；云帆开发者大会公布工业智能体工具；远航汽车称今年交付取决于电池产能丨智能制造日报"

        safe_title = _wechat_safe_title(title)

        self.assertLessEqual(len(safe_title), 64)
        self.assertTrue(safe_title.endswith("丨智能制造日报"))
        self.assertIn("星河通信完成数千万元融资", safe_title)
        self.assertNotIn("远航汽车称今年交付取决于电池产能", safe_title)


@dataclass
class FakeResponse:
    payload: dict[str, object]

    def json(self) -> dict[str, object]:
        return self.payload


class FakeSession:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.last_data = b""
        self.last_url = ""

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.last_url = url
        self.last_data = kwargs.get("data", b"")  # type: ignore[assignment]
        return FakeResponse(self.payload)


def _png_header(*, width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


if __name__ == "__main__":
    unittest.main()
