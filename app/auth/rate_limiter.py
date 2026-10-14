"""In-memory rate limiter for failed login attempts to prevent brute force attacks."""
from __future__ import annotations

import threading
import time
from typing import Dict, List


class LoginRateLimiter:
    """Thread-safe sliding-window rate limiter for failed login attempts.

    Features:
      - Atomic concurrent updates with a mutex lock.
      - Sliding window time tracking.
      - Bounded memory with global sweep when exceeding max_entries.
      - Immediate reset upon successful login.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 300,
        max_entries: int = 10_000,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._failures: Dict[str, List[float]] = {}

    def _prune(self, key: str, now: float) -> List[float]:
        cutoff = now - self.window_seconds
        attempts = [t for t in self._failures.get(key, []) if t > cutoff]
        if attempts:
            self._failures[key] = attempts
        else:
            self._failures.pop(key, None)
        return attempts

    def _sweep(self, now: float) -> None:
        """Clean up expired entries to strictly bound memory consumption."""
        cutoff = now - self.window_seconds
        stale_keys = [
            k for k, timestamps in self._failures.items()
            if not timestamps or max(timestamps) <= cutoff
        ]
        for k in stale_keys:
            self._failures.pop(k, None)

    def is_rate_limited(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            attempts = self._prune(key, now)
            return len(attempts) >= self.max_attempts

    def record_failure(self, key: str) -> int:
        """Record a failed login attempt. Returns current attempt count in window."""
        now = time.time()
        with self._lock:
            if len(self._failures) >= self.max_entries:
                self._sweep(now)
                # If still at capacity after sweep, evict oldest entry
                if len(self._failures) >= self.max_entries:
                    self._failures.pop(next(iter(self._failures)), None)

            attempts = self._prune(key, now)
            attempts.append(now)
            self._failures[key] = attempts
            return len(attempts)

    def record_success(self, key: str) -> None:
        """Clear failed attempts upon successful authentication."""
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        """Reset all rate limiter state (used in tests)."""
        with self._lock:
            self._failures.clear()


login_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300, max_entries=10_000)
