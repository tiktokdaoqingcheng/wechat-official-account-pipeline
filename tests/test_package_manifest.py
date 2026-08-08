from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.pipeline.package_manifest import freeze_content_package, verify_frozen_package


class PackageManifestTest(unittest.TestCase):
    def test_freeze_and_verify_content_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            article = root / "article.html"
            article.write_text("<section>stable</section>", encoding="utf-8")
            package_path = root / "content-package.json"
            package_path.write_text(
                json.dumps(
                    {
                        "schema_version": "content_package.v1",
                        "files": {"primary": {"article_html": str(article)}},
                    }
                ),
                encoding="utf-8",
            )

            manifest = freeze_content_package(
                package_path,
                config_fingerprint="config-hash",
                config_version="config-version",
                now=datetime(2026, 7, 10, tzinfo=timezone.utc),
            )
            verified = verify_frozen_package(
                package_path,
                manifest_path=manifest["path"],
                expected_config_fingerprint="config-hash",
                expected_config_version="config-version",
            )

        self.assertTrue(verified["ok"])
        self.assertEqual(verified["artifact_fingerprint"], manifest["artifact_fingerprint"])

    def test_verification_detects_changed_article(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            article = root / "article.html"
            article.write_text("<section>stable</section>", encoding="utf-8")
            package_path = root / "content-package.json"
            package_path.write_text(
                json.dumps({"schema_version": "content_package.v1", "files": {"article_html": str(article)}}),
                encoding="utf-8",
            )
            manifest = freeze_content_package(
                package_path,
                config_fingerprint="config-hash",
                config_version="config-version",
            )
            article.write_text("<section>changed</section>", encoding="utf-8")

            verified = verify_frozen_package(package_path, manifest_path=manifest["path"])

        self.assertFalse(verified["ok"])
        self.assertTrue(any("changed after freeze" in error for error in verified["errors"]))

    def test_verification_detects_config_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = root / "content-package.json"
            package_path.write_text(json.dumps({"schema_version": "content_package.v1", "files": {}}), encoding="utf-8")
            manifest = freeze_content_package(
                package_path,
                config_fingerprint="old-config",
                config_version="v1",
            )

            verified = verify_frozen_package(
                package_path,
                manifest_path=manifest["path"],
                expected_config_fingerprint="new-config",
                expected_config_version="v2",
            )

        self.assertFalse(verified["ok"])
        self.assertIn("effective config fingerprint changed after package generation", verified["errors"])
        self.assertIn("effective config version changed after package generation", verified["errors"])


if __name__ == "__main__":
    unittest.main()
