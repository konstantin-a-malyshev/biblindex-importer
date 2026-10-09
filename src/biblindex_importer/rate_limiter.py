"""Client-side pacing for the BiblIndex rate limit (1 request/second per account).

The limiter combines three signals:

* a fixed minimum interval between requests (with a safety margin),
* the server's ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` headers, so we
  pause before the server has to refuse us. BiblIndex reports ``remaining=0`` with
  a reset ~8 s away after *every* request, yet accepts the next one a second later
  (measured 2026-10-09), so header waits are capped at ``max_reset_wait``,
* ``429 Too Many Requests`` responses: we honour ``Retry-After`` and slow down
  permanently for the rest of the run, so one violation does not become many.
"""

from __future__ import annotations

import email.utils
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger(__name__)

# X-RateLimit-Reset values above this are Unix timestamps, below are seconds.
_EPOCH_THRESHOLD = 1_000_000_000


@dataclass
class RateLimitStats:
    requests: int = 0
    throttled: int = 0  # 429 responses received
    waited_seconds: float = 0.0


class RateLimiter:
    def __init__(
        self,
        min_interval: float,
        *,
        backoff_factor: float = 1.5,
        max_interval: float = 10.0,
        max_reset_wait: float | None = 2.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self.backoff_factor = backoff_factor
        self.max_interval = max_interval
        self.max_reset_wait = max_reset_wait
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._last_request: float | None = None
        self._blocked_until: float = 0.0
        self.stats = RateLimitStats()

    def wait(self) -> None:
        """Block until the next request may be sent."""
        now = self._clock()
        ready_at = self._blocked_until
        if self._last_request is not None:
            ready_at = max(ready_at, self._last_request + self.min_interval)
        delay = ready_at - now
        if delay > 0:
            self._sleep(delay)
            self.stats.waited_seconds += delay
        self._last_request = self._clock()
        self.stats.requests += 1

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        """Pause until the window resets when the server says none are left.

        The pause is capped at ``max_reset_wait`` (``None`` = no cap); a real
        refusal still arrives as a 429, which ``register_throttled`` honours fully.
        """
        remaining = _parse_float(headers.get("X-RateLimit-Remaining"))
        if remaining is None or remaining > 0:
            return
        reset_in = self._seconds_until_reset(headers.get("X-RateLimit-Reset"))
        if reset_in is None:
            reset_in = self.min_interval
        if self.max_reset_wait is not None:
            reset_in = min(reset_in, self.max_reset_wait)
        self._block_for(reset_in)

    def register_throttled(self, headers: Mapping[str, str]) -> float:
        """Record a 429 response; returns how long we will wait before retrying."""
        self.stats.throttled += 1
        retry_after = parse_retry_after(headers.get("Retry-After"), self._wall_clock())
        if retry_after is None:
            retry_after = self._seconds_until_reset(headers.get("X-RateLimit-Reset"))
        if retry_after is None:
            retry_after = self.min_interval * 2
        old = self.min_interval
        self.min_interval = min(self.min_interval * self.backoff_factor, self.max_interval)
        log.warning(
            "Rate limit exceeded (429). Waiting %.1fs; slowing down from %.2fs to %.2fs "
            "between requests.",
            retry_after,
            old,
            self.min_interval,
        )
        self._block_for(retry_after)
        return retry_after

    def _block_for(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + max(seconds, 0.0))

    def _seconds_until_reset(self, value: str | None) -> float | None:
        reset = _parse_float(value)
        if reset is None:
            return None
        if reset > _EPOCH_THRESHOLD:
            return max(reset - self._wall_clock(), 0.0)
        return max(reset, 0.0)


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Parse a Retry-After header: either delay-seconds or an HTTP date."""
    if not value:
        return None
    seconds = _parse_float(value)
    if seconds is not None:
        return max(seconds, 0.0)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(when.timestamp() - now, 0.0)


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
