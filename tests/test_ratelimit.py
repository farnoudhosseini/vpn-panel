"""Brute-force protection: request throttling, lockouts and allowlists."""

from __future__ import annotations

from panel.core.ratelimit import FailureTracker, RateLimiter, ip_in_allowlist, normalise_ip


def test_requests_within_the_window_are_allowed() -> None:
    limiter = RateLimiter(limit=3, window_seconds=60)
    for _ in range(3):
        allowed, retry = limiter.check("1.2.3.4")
        assert allowed is True and retry == 0.0


def test_the_limit_is_per_key() -> None:
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.check("1.2.3.4")[0] is True
    assert limiter.check("5.6.7.8")[0] is True
    assert limiter.check("1.2.3.4")[0] is False


def test_exceeding_the_limit_reports_a_retry_delay() -> None:
    limiter = RateLimiter(limit=1, window_seconds=60)
    limiter.check("9.9.9.9")
    allowed, retry = limiter.check("9.9.9.9")
    assert allowed is False
    assert 0 < retry <= 60


def test_a_hard_block_wins_over_the_window() -> None:
    limiter = RateLimiter(limit=100, window_seconds=60)
    limiter.block("10.0.0.1", 30)
    allowed, retry = limiter.check("10.0.0.1")
    assert allowed is False and retry > 0
    assert limiter.is_blocked("10.0.0.1") > 0


def test_reset_clears_a_key() -> None:
    limiter = RateLimiter(limit=1, window_seconds=60)
    limiter.check("2.2.2.2")
    limiter.reset("2.2.2.2")
    assert limiter.check("2.2.2.2")[0] is True


def test_prune_drops_idle_buckets() -> None:
    limiter = RateLimiter(limit=5, window_seconds=1)
    limiter.check("3.3.3.3")
    assert limiter.prune(max_age=0) == 1


def test_failure_tracker_locks_after_the_threshold() -> None:
    tracker = FailureTracker(threshold=3, lock_seconds=60)
    assert tracker.is_locked("bob") == 0.0
    for _ in range(3):
        tracker.record_failure("bob")
    assert tracker.is_locked("bob") > 0


def test_failure_tracker_success_clears_the_counter() -> None:
    tracker = FailureTracker(threshold=2, lock_seconds=60)
    tracker.record_failure("carol")
    tracker.record_success("carol")
    assert tracker.is_locked("carol") == 0.0


def test_normalise_ip_strips_ports_and_brackets() -> None:
    assert normalise_ip("1.2.3.4:5678") == "1.2.3.4"
    assert normalise_ip("[2001:db8::1]:443") == "2001:db8::1"
    assert normalise_ip("") == "unknown"
    assert normalise_ip("not an ip") == "not an ip"


def test_allowlist_matches_addresses_and_networks() -> None:
    assert ip_in_allowlist("10.0.0.5", ["10.0.0.0/24"])
    assert ip_in_allowlist("192.168.1.7", ["192.168.1.7"])
    assert not ip_in_allowlist("8.8.8.8", ["10.0.0.0/24"])
    assert not ip_in_allowlist("8.8.8.8", [])


def test_allowlist_ignores_malformed_entries() -> None:
    assert ip_in_allowlist("10.0.0.5", ["nonsense", "10.0.0.0/24"]) is True
    assert ip_in_allowlist("10.0.0.5", ["nonsense"]) is False
