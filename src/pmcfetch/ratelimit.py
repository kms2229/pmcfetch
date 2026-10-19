"""Process-wide request pacing.

Sleeping inside each worker thread (``time.sleep(random.uniform(0.1, 0.5))``)
bounds nothing globally: with 10 workers that is still ~30 requests/second in
the worst case. This limiter serialises the *spacing* across all threads so
``--workers`` and politeness are independent knobs.
"""

from __future__ import annotations

import random
import threading
import time


class MinIntervalLimiter:
    """Allow at most one acquisition per ``min_interval`` seconds, across threads."""

    def __init__(self, min_interval: float = 0.0, *, jitter: float = 0.0) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        if jitter < 0:
            raise ValueError("jitter must be >= 0")
        self.min_interval = float(min_interval)
        self.jitter = float(jitter)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self, *, sleep=time.sleep, now=time.monotonic) -> float:
        """Block until the next slot is due. Returns seconds actually slept."""
        if self.min_interval <= 0 and self.jitter <= 0:
            return 0.0
        with self._lock:
            t = now()
            wait = max(0.0, self._next_at - t)
            spacing = self.min_interval + (random.uniform(0.0, self.jitter) if self.jitter else 0.0)
            self._next_at = max(t, self._next_at) + spacing
        if wait > 0:
            sleep(wait)
        return wait


class NullLimiter:
    """No pacing. Useful in tests and for single-shot fetches."""

    min_interval = 0.0
    jitter = 0.0

    def acquire(self, *, sleep=time.sleep, now=time.monotonic) -> float:  # noqa: ARG002
        return 0.0


__all__ = ["MinIntervalLimiter", "NullLimiter"]
