from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.prepare_demo_preview import prepare_demo_preview


class DemoPreviewTest(unittest.TestCase):
    def test_preview_removes_unresolved_image_sections_and_marks_synthetic_data(self) -> None:
        fragment = """<section><h1>Demo</h1></section>
<section style="margin:0"><img src="{{ARTICLE_INLINE_PRIMARY_1}}" alt="placeholder" /><p>caption</p></section>
<section><p>Visible copy</p></section>"""
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "article.html"
            output = Path(temp_dir) / "preview.html"
            source.write_text(fragment, encoding="utf-8")

            prepare_demo_preview(source, output)
            rendered = output.read_text(encoding="utf-8")

        self.assertNotIn("ARTICLE_INLINE_PRIMARY_1", rendered)
        self.assertNotIn("caption", rendered)
        self.assertIn("Visible copy", rendered)
        self.assertIn("OFFLINE SYNTHETIC DEMO", rendered)


if __name__ == "__main__":
    unittest.main()
