"""In-process rate limiting for the chat endpoints.

A sliding-window limiter keyed by caller identity (user id or client IP).
Per-process only: with several workers each process keeps its own window, so
the effective limit is `workers × limit`. For a shared limit use Redis.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple


class RateLimiter:
    """Sliding-window limiter: at most `limit` events per `window` seconds."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = max(int(limit), 0)
        self.window = max(float(window), 0.0)
        self._lock = threading.Lock()
        self._events: Dict[str, Deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window > 0

    def allow(self, key: str) -> Tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        if not self.enabled:
            return True, 0.0
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry = max(0.0, self.window - (now - events[0]))
                return False, round(retry, 1)
            events.append(now)
        return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "keys": len(self._events),
                "events": sum(len(events) for events in self._events.values()),
            }
