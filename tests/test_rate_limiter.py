import email.utils

import pytest

from biblindex_importer.rate_limiter import RateLimiter, parse_retry_after


@pytest.fixture
def limiter(clock):
    return RateLimiter(1.0, clock=clock, wall_clock=clock, sleep=clock.sleep)


def test_first_request_is_not_delayed(limiter, clock):
    limiter.wait()
    assert clock.sleeps == []
    assert limiter.stats.requests == 1


def test_requests_are_spaced_by_min_interval(limiter, clock):
    limiter.wait()
    clock.now += 0.3
    limiter.wait()
    assert clock.sleeps == [pytest.approx(0.7)]


def test_no_delay_when_enough_time_has_passed(limiter, clock):
    limiter.wait()
    clock.now += 5
    limiter.wait()
    assert clock.sleeps == []


def test_exhausted_quota_waits_for_reset_seconds(limiter, clock):
    limiter.max_reset_wait = None
    limiter.wait()
    limiter.update_from_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4"})
    limiter.wait()
    assert clock.sleeps == [pytest.approx(4.0)]


def test_exhausted_quota_understands_epoch_reset(limiter, clock):
    clock.now = 1_800_000_000.0
    limiter.max_reset_wait = None
    limiter.wait()
    limiter.update_from_headers(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(1_800_000_003)}
    )
    limiter.wait()
    assert clock.sleeps == [pytest.approx(3.0)]


def test_reset_wait_is_capped_by_default(limiter, clock):
    # BiblIndex announces ~8 s resets after every request but accepts 1 req/s.
    limiter.wait()
    limiter.update_from_headers({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "8"})
    limiter.wait()
    assert clock.sleeps == [pytest.approx(2.0)]


def test_retry_after_is_never_capped(limiter, clock):
    limiter.wait()
    limiter.register_throttled({"Retry-After": "30"})
    limiter.wait()
    assert clock.sleeps == [pytest.approx(30.0)]


def test_remaining_quota_does_not_block(limiter, clock):
    limiter.wait()
    limiter.update_from_headers({"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "60"})
    clock.now += 1
    limiter.wait()
    assert clock.sleeps == []


def test_throttled_response_honours_retry_after_and_slows_down(limiter, clock):
    limiter.wait()
    waited = limiter.register_throttled({"Retry-After": "7"})
    assert waited == 7
    assert limiter.min_interval == pytest.approx(1.5)
    assert limiter.stats.throttled == 1
    limiter.wait()
    assert clock.sleeps == [pytest.approx(7.0)]


def test_slowdown_is_capped(clock):
    limiter = RateLimiter(4.0, max_interval=5.0, clock=clock, wall_clock=clock, sleep=clock.sleep)
    limiter.register_throttled({})
    limiter.register_throttled({})
    assert limiter.min_interval == 5.0


def test_throttled_without_headers_waits_a_default(limiter, clock):
    assert limiter.register_throttled({}) == pytest.approx(2.0)


def test_parse_retry_after_seconds_and_dates():
    now = 1_800_000_000.0
    assert parse_retry_after("3", now) == 3
    assert parse_retry_after(None, now) is None
    assert parse_retry_after("garbage", now) is None
    date = email.utils.formatdate(now + 10, usegmt=True)
    assert parse_retry_after(date, now) == pytest.approx(10, abs=1)
