from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ExecutionBudget:
    total_seconds: float
    publish_reserve_seconds: float = 0.0
    max_provider_calls: int | None = None
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _started_at: float = field(init=False, repr=False)
    _provider_calls_used: int = field(default=0, init=False, repr=False)
    _provider_call_budget_exhausted: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.total_seconds = max(1.0, float(self.total_seconds))
        self.publish_reserve_seconds = max(0.0, float(self.publish_reserve_seconds))
        if self.max_provider_calls is not None:
            self.max_provider_calls = max(0, int(self.max_provider_calls))
        self._started_at = self.clock()

    def set_provider_call_limit(self, max_calls: int | None) -> None:
        self.max_provider_calls = None if max_calls is None else max(0, int(max_calls))
        self._provider_call_budget_exhausted = bool(
            self.max_provider_calls is not None
            and self._provider_calls_used >= self.max_provider_calls
        )

    def try_reserve_provider_call(self) -> bool:
        if self.max_provider_calls is not None and self._provider_calls_used >= self.max_provider_calls:
            self._provider_call_budget_exhausted = True
            return False
        self._provider_calls_used += 1
        if self.max_provider_calls is not None and self._provider_calls_used >= self.max_provider_calls:
            self._provider_call_budget_exhausted = True
        return True

    def provider_calls_used(self) -> int:
        return self._provider_calls_used

    def provider_calls_remaining(self) -> int | None:
        if self.max_provider_calls is None:
            return None
        return max(0, self.max_provider_calls - self._provider_calls_used)

    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self._started_at)

    def remaining_seconds(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed_seconds())

    def optional_seconds(self) -> float:
        return max(0.0, self.remaining_seconds() - self.publish_reserve_seconds)

    def can_start_optional(self, estimated_seconds: float = 0.0) -> bool:
        return self.optional_seconds() >= max(0.0, float(estimated_seconds))

    def can_start_required(self, estimated_seconds: float = 0.0) -> bool:
        return self.remaining_seconds() >= max(0.0, float(estimated_seconds))

    def request_timeout(
        self,
        configured_seconds: float,
        *,
        optional: bool = True,
        minimum_seconds: float = 1.0,
    ) -> float:
        available = self.optional_seconds() if optional else self.remaining_seconds()
        minimum = max(0.1, float(minimum_seconds))
        if available < minimum:
            return 0.0
        return max(minimum, min(max(minimum, float(configured_seconds)), available))

    def retry_delay_allowed(
        self,
        delay_seconds: float,
        *,
        optional: bool = True,
        minimum_after_delay: float = 1.0,
    ) -> bool:
        available = self.optional_seconds() if optional else self.remaining_seconds()
        return available >= max(0.0, float(delay_seconds)) + max(0.0, float(minimum_after_delay))

    def snapshot(self) -> dict[str, float | int | bool | None]:
        remaining = self.remaining_seconds()
        return {
            "total_seconds": round(self.total_seconds, 3),
            "publish_reserve_seconds": round(self.publish_reserve_seconds, 3),
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "remaining_seconds": round(remaining, 3),
            "deadline_exceeded": remaining <= 0,
            "max_provider_calls": self.max_provider_calls,
            "provider_calls_used": self.provider_calls_used(),
            "provider_calls_remaining": self.provider_calls_remaining(),
            "provider_call_budget_exhausted": self._provider_call_budget_exhausted,
        }
