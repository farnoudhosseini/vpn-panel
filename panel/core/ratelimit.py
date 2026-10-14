"""Brute-force protection and request throttling.

A process-local sliding window is enough for a single-instance control plane
and avoids a Redis dependency. Counters are keyed by client IP so a panel
restart only clears short-lived state.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .logging import get_logger

log = get_logger("ratelimit")


@dataclass
class _Window:
    hits: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0


class RateLimiter:
    """Sliding-window limiter with an optional hard block."""

    def __init__(self, *, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, limit)
        self.window = window_seconds
        self._lock = threading.Lock()
        self._buckets: dict[str, _Window] = defaultdict(_Window)

    def check(self, key: str) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        with self._lock:
            window = self._buckets[key]
            if window.blocked_until > now:
                return False, round(window.blocked_until - now, 2)
            cutoff = now - self.window
            while window.hits and window.hits[0] < cutoff:
                window.hits.popleft()
            if len(window.hits) >= self.limit:
                retry_after = round(self.window - (now - window.hits[0]), 2)
                return False, max(retry_after, 0.5)
            window.hits.append(now)
            return True, 0.0

    def block(self, key: str, seconds: float) -> None:
        with self._lock:
            self._buckets[key].blocked_until = time.monotonic() + seconds

    def is_blocked(self, key: str) -> float:
        now = time.monotonic()
        with self._lock:
            remaining = self._buckets[key].blocked_until - now
        return round(remaining, 2) if remaining > 0 else 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def prune(self, max_age: float = 3600.0) -> int:
        """Drop idle buckets; called by the maintenance task."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            for key in list(self._buckets):
                window = self._buckets[key]
                stale = not window.hits or (now - window.hits[-1]) > max_age
                if stale and window.blocked_until <= now:
                    del self._buckets[key]
                    removed += 1
        return removed


class FailureTracker:
    """Counts consecutive authentication failures per identity."""

    def __init__(self, *, threshold: int, lock_seconds: int) -> None:
        self.threshold = max(1, threshold)
        self.lock_seconds = max(1, lock_seconds)
        self._lock = threading.Lock()
        self._counts: dict[str, int] = defaultdict(int)
        self._blocked: dict[str, float] = {}

    def is_locked(self, identity: str) -> float:
        now = time.monotonic()
        with self._lock:
            until = self._blocked.get(identity, 0.0)
        remaining = until - now
        return round(remaining, 1) if remaining > 0 else 0.0

    def record_failure(self, identity: str) -> int:
        with self._lock:
            self._counts[identity] += 1
            count = self._counts[identity]
            if count >= self.threshold:
                self._blocked[identity] = time.monotonic() + self.lock_seconds
                self._counts[identity] = 0
                log.warning("identity %s locked for %ss", identity, self.lock_seconds)
        return count

    def record_success(self, identity: str) -> None:
        with self._lock:
            self._counts.pop(identity, None)
            self._blocked.pop(identity, None)


def normalise_ip(raw: str | None) -> str:
    if not raw:
        return "unknown"
    candidate = raw.strip()
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    # Strip an IPv4 port suffix such as 1.2.3.4:5678
    if candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return candidate[:64] or "unknown"


def ip_in_allowlist(ip_value: str, entries: list[str]) -> bool:
    """True when ``ip_value`` matches one of the allowlist entries."""
    if not entries:
        return False
    try:
        address = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            log.warning("ignoring malformed allowlist entry %r", entry)
    return False
