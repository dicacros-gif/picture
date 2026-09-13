"""One runtime-only monotonic budget shared by a whole blog cycle.

Keep this object out of persisted requests/settings. A retry consumes the same
budget; creating a new object belongs only at the next scheduled cycle.
"""
from __future__ import annotations

import math
import threading
import time


class CycleDeadlineExceeded(RuntimeError):
    retryable = False
    code = "cycle_deadline_exceeded"

    def __init__(self, message="이번 회차의 작업 시간 한도에 도달했습니다. 완료된 자료를 보존하고 다음 예약에 이어갑니다.",
                 *, elapsed=None, limit_seconds=None, reserve_seconds=0):
        super().__init__(message)
        self.elapsed = elapsed
        self.limit_seconds = limit_seconds
        self.reserve_seconds = reserve_seconds


def _seconds(value, name, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (positive and value == 0)):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


class CycleBudget:
    def __init__(self, limit_seconds=3000, clock=time.monotonic):
        self.limit_seconds = _seconds(limit_seconds, "limit_seconds", positive=True)
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._clock = clock
        self._started = float(clock())
        if not math.isfinite(self._started):
            raise ValueError("clock must return a finite number")

    @property
    def elapsed(self):
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite number")
        return max(0.0, now - self._started)

    def remaining(self, reserve_seconds=0):
        reserve = _seconds(reserve_seconds, "reserve_seconds")
        return max(0.0, self.limit_seconds - self.elapsed - reserve)

    def _expired(self, reserve_seconds):
        return CycleDeadlineExceeded(elapsed=self.elapsed, limit_seconds=self.limit_seconds,
                                     reserve_seconds=reserve_seconds)

    def check(self, reserve_seconds=0):
        if self.remaining(reserve_seconds) <= 0:
            raise self._expired(reserve_seconds)

    def timeout(self, default, reserve_seconds=0):
        requested = _seconds(default, "default", positive=True)
        available = self.remaining(reserve_seconds)
        # The native CLI adapters require at least one second. Never round a
        # fractional remainder upwards or turn expiry into invalid_timeout.
        if available < 1:
            raise self._expired(reserve_seconds)
        return min(requested, available)

    def cancel_event(self, user_event=None, reserve_seconds=0):
        return BudgetCancelSignal(self, user_event, reserve_seconds)


class BudgetCancelSignal:
    """Event-like deadline/user cancellation without setting the user's event."""
    def __init__(self, budget, user_event=None, reserve_seconds=0):
        self.budget, self.user_event = budget, user_event
        self.reserve_seconds = _seconds(reserve_seconds, "reserve_seconds")
        self._cancelled = threading.Event()

    def set(self):
        self._cancelled.set()

    def is_set(self):
        return (self._cancelled.is_set()
                or (self.user_event is not None and self.user_event.is_set())
                or self.budget.remaining(self.reserve_seconds) <= 0)

    def wait(self, timeout=None):
        if timeout is not None:
            timeout = _seconds(timeout, "timeout")
        end = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            left = self.budget.remaining(self.reserve_seconds)
            if end is not None:
                left = min(left, max(0.0, end - time.monotonic()))
            if left <= 0:
                return self.is_set()
            self._cancelled.wait(min(.1, left))
        return True
