from __future__ import annotations

import unittest

from src.analytics.incident_replay import run_incident_replay


class IncidentReplayTest(unittest.TestCase):
    def test_historical_incident_corpus_passes(self) -> None:
        report = run_incident_replay("tests/fixtures/incidents")

        self.assertEqual(report["status"], "ok", report["results"])
        self.assertGreaterEqual(report["total"], 5)
        self.assertEqual(report["failed"], 0)


if __name__ == "__main__":
    unittest.main()
