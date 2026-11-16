"""Bound per-instance work without changing normal public command access."""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    last_notice: float = float("-inf")


class RequestBudget:
    def __init__(
        self, capacity: int = 20, refill_per_second: float = 1.0,
        *, max_users: int = 10000, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0 or max_users < 1:
            raise ValueError("Invalid request budget")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.max_users = max_users
        self._clock = clock
        self._users: OrderedDict[int, _Bucket] = OrderedDict()

    def allow(self, user_id: int) -> bool:
        now = self._clock()
        bucket = self._users.get(user_id)
        if bucket is None:
            while len(self._users) >= self.max_users:
                self._users.popitem(last=False)
            bucket = _Bucket(float(self.capacity), now)
            self._users[user_id] = bucket
        else:
            self._users.move_to_end(user_id)
            bucket.tokens = min(self.capacity, bucket.tokens + max(0.0, now - bucket.updated_at) * self.refill_per_second)
            bucket.updated_at = now
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    def should_notify(self, user_id: int) -> bool:
        bucket = self._users.get(user_id)
        now = self._clock()
        if bucket is None or now - bucket.last_notice < 5.0:
            return False
        bucket.last_notice = now
        return True
