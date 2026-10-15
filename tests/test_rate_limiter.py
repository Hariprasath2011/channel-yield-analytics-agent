from app.auth.rate_limiter import LoginRateLimiter


def test_capacity_does_not_evict_active_failure_history():
    limiter = LoginRateLimiter(
        max_attempts=2,
        window_seconds=300,
        max_entries=2,
    )

    limiter.record_failure("user-a")
    limiter.record_failure("user-a")
    limiter.record_failure("user-b")

    # Both entries have active failures. A third key must not erase
    # another user's protection when the limiter is at capacity.
    limiter.record_failure("user-c")

    assert limiter.is_rate_limited("user-a")
