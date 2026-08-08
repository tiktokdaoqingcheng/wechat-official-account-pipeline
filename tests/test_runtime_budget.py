from __future__ import annotations

import unittest

from src.runtime_budget import ExecutionBudget


class RuntimeBudgetTest(unittest.TestCase):
    def test_optional_budget_preserves_publish_reserve(self) -> None:
        current = [100.0]
        budget = ExecutionBudget(300, publish_reserve_seconds=120, clock=lambda: current[0])

        self.assertTrue(budget.can_start_optional(180))
        current[0] += 61
        self.assertFalse(budget.can_start_optional(120))
        self.assertTrue(budget.can_start_required(239))
        self.assertFalse(budget.can_start_required(240))
        self.assertEqual(budget.request_timeout(120), 119.0)
        self.assertEqual(budget.request_timeout(300, optional=False), 239.0)
        self.assertTrue(budget.retry_delay_allowed(30))
        self.assertFalse(budget.retry_delay_allowed(119))
        self.assertEqual(budget.snapshot()["remaining_seconds"], 239.0)

    def test_request_timeout_returns_zero_when_publish_reserve_must_be_preserved(self) -> None:
        current = [0.0]
        budget = ExecutionBudget(120, publish_reserve_seconds=120, clock=lambda: current[0])

        self.assertEqual(budget.request_timeout(60), 0.0)
        self.assertEqual(budget.request_timeout(60, optional=False), 60.0)

    def test_provider_call_budget_is_reserved_before_each_real_request(self) -> None:
        budget = ExecutionBudget(120, max_provider_calls=2, clock=lambda: 0.0)

        self.assertTrue(budget.try_reserve_provider_call())
        self.assertTrue(budget.try_reserve_provider_call())
        self.assertFalse(budget.try_reserve_provider_call())
        self.assertEqual(budget.provider_calls_used(), 2)
        self.assertEqual(budget.provider_calls_remaining(), 0)
        self.assertTrue(budget.snapshot()["provider_call_budget_exhausted"])


if __name__ == "__main__":
    unittest.main()
