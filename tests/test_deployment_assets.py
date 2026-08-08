from __future__ import annotations

import unittest
from pathlib import Path


class DeploymentAssetsTest(unittest.TestCase):
    def test_publish_service_never_blindly_restarts_submission(self) -> None:
        unit = Path("deploy/systemd/wechat-official-account-automation.service").read_text(encoding="utf-8")

        self.assertIn("SuccessExitStatus=2", unit)
        self.assertNotIn("Restart=on-failure", unit)
        self.assertIn("TimeoutStartSec=240", unit)

    def test_prepare_service_retries_and_publish_wrapper_uses_publish_only_stage(self) -> None:
        prepare_unit = Path("deploy/systemd/wechat-content-prepare.service").read_text(encoding="utf-8")
        prepare_wrapper = Path("deploy/bin/run-content-prepare.sh").read_text(encoding="utf-8")
        publish_wrapper = Path("deploy/bin/run-daily-auto-publish.sh").read_text(encoding="utf-8")

        self.assertIn("Restart=on-failure", prepare_unit)
        self.assertIn("--stage prepare", prepare_wrapper)
        self.assertIn("--stage publish", publish_wrapper)

    def test_release_switch_refuses_both_active_production_stages(self) -> None:
        switch_script = Path("deploy/bin/switch-release.sh").read_text(encoding="utf-8")

        self.assertIn("wechat-official-account-automation.service", switch_script)
        self.assertIn("wechat-content-prepare.service", switch_script)


if __name__ == "__main__":
    unittest.main()
