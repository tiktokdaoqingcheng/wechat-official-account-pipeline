from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path

import yaml

from src.config_loader import load_layered_yaml
from src.runtime_guard import (
    EXPECTED_CONFIG_VERSION,
    RuntimeWriteBlocked,
    config_fingerprint,
    require_wechat_write_access,
    resolve_runtime_identity,
)


class RuntimeGuardTest(unittest.TestCase):
    def test_local_runtime_fails_closed(self) -> None:
        identity = resolve_runtime_identity(
            {
                "runtime": {
                    "environment": "local",
                    "allow_wechat_writes": False,
                    "allowed_instance_ids": [],
                    "config_version": EXPECTED_CONFIG_VERSION,
                }
            },
            hostname="local-host",
        )

        self.assertFalse(identity.wechat_write_allowed)
        with self.assertRaises(RuntimeWriteBlocked):
            require_wechat_write_access(
                {
                    "runtime": {
                        "environment": "local",
                        "allow_wechat_writes": False,
                        "allowed_instance_ids": [],
                        "config_version": EXPECTED_CONFIG_VERSION,
                    }
                },
                operation="test",
                hostname="local-host",
            )

    def test_production_runtime_requires_allowlisted_instance_and_version(self) -> None:
        schedule = {
            "runtime": {
                "environment": "production",
                "allow_wechat_writes": True,
                "allowed_instance_ids": ["production-primary"],
                "config_version": EXPECTED_CONFIG_VERSION,
            }
        }

        allowed = resolve_runtime_identity(schedule, hostname="production-primary")
        rejected = resolve_runtime_identity(schedule, hostname="other-host")

        self.assertTrue(allowed.wechat_write_allowed)
        self.assertFalse(rejected.wechat_write_allowed)

    def test_layered_schedule_merges_base_and_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = root / "base.yaml"
            overlay = root / "production.yaml"
            base.write_text(
                yaml.safe_dump(
                    {
                        "runtime": {"environment": "local", "allow_wechat_writes": False},
                        "publishing": {"mode": "dry_run", "retry_count": 2},
                    },
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            overlay.write_text(
                yaml.safe_dump(
                    {
                        "base_config": "base.yaml",
                        "runtime": {"environment": "production", "allow_wechat_writes": True},
                        "publishing": {"mode": "auto_publish_low_risk"},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            config = load_layered_yaml(overlay)

        self.assertEqual(config["runtime"]["environment"], "production")
        self.assertTrue(config["runtime"]["allow_wechat_writes"])
        self.assertEqual(config["publishing"]["mode"], "auto_publish_low_risk")
        self.assertEqual(config["publishing"]["retry_count"], 2)
        self.assertIn("config_layers", config)
        self.assertEqual(len(config_fingerprint(config)), 64)

    def test_current_hostname_can_be_allowlisted(self) -> None:
        identity = resolve_runtime_identity(
            {
                "runtime": {
                    "environment": "production",
                    "allow_wechat_writes": True,
                    "allowed_instance_ids": [socket.gethostname()],
                    "config_version": EXPECTED_CONFIG_VERSION,
                }
            }
        )

        self.assertTrue(identity.wechat_write_allowed)


if __name__ == "__main__":
    unittest.main()
