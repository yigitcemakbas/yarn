"""One process-wide rate limit per host, shared by every source (ported from loom).

Ingestion used to be a single sequential loop, so "be polite to SEC" could be
expressed as `time.sleep(0.2)` after each request and that was genuinely
enough. Two things break the moment tickers are fetched concurrently.

The first is correctness. A sleep after a request paces one thread. Four
threads each sleeping 0.2s between their own requests produce twenty requests
a second in aggregate, not five, and SEC answers sustained abuse with a
block that lasts far longer than the run. Rate limiting has to be a property
of the process, not of a call site.

The second is that the old pattern also throttled the wrong thing. Sleeping
*after* a response means the interval is added to the request's own latency,
so a 300ms fetch plus a 200ms sleep gives two requests a second when five were
allowed. A limiter reserves the next slot and waits only for as long as that
slot is actually in the future, which is both correct under concurrency and
faster in the ordinary case.

Hosts are keyed by netloc because that is the unit the other side rate limits
on. sec.gov and data.sec.gov are deliberately collapsed onto one bucket: they
are one operator with one fair-access policy, and three adapters (filings,
Form 4, ticker lookup) hit them from different code paths with no idea the
others exist.
"""

import logging
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# SEC publishes a fair-access ceiling of 10 requests/second. Half of it, which
# is what the sequential code already targeted, leaves room for the browser
# traffic of a developer reading filings while a run is in flight.
SEC_INTERVAL_SECONDS = 0.2

# Scraped sites get a deliberately generous gap: nothing here is
# latency-sensitive, and a research tool that is invisible in a site's logs is
# a research tool that keeps working.
DEFAULT_INTERVAL_SECONDS = 1.5

# Every SEC hostname resolves to this bucket, so the ceiling is enforced across
# adapters rather than per adapter.
_SEC_BUCKET = "sec.gov"

_HOST_INTERVALS: dict[str, float] = {
    _SEC_BUCKET: SEC_INTERVAL_SECONDS,
}


def bucket_for(url_or_host: str) -> str:
    """The rate-limit bucket a URL belongs to."""
    host = urlparse(url_or_host).netloc or url_or_host
    host = host.split("@")[-1].split(":")[0].lower()
    if host == _SEC_BUCKET or host.endswith("." + _SEC_BUCKET):
        return _SEC_BUCKET
    return host


def set_interval(host: str, seconds: float) -> None:
    """Override one host's gap. Used by scrapers that know a site wants a
    slower cadence than the default."""
    _HOST_INTERVALS[bucket_for(host)] = seconds


class HostRateLimiter:
    """Blocks the caller until its host's next slot is free.

    The reservation is taken under the lock and the waiting is done outside it.
    Holding the lock across the sleep would serialise every host behind
    whichever one is currently waiting, turning a per-host limit into a global
    one and erasing the point of running adapters concurrently.
    """

    def __init__(self, default_interval: float = DEFAULT_INTERVAL_SECONDS):
        self.default_interval = default_interval
        self._lock = threading.Lock()
        self._next_free: dict[str, float] = {}

    def interval_for(self, bucket: str) -> float:
        return _HOST_INTERVALS.get(bucket, self.default_interval)

    def acquire(self, url_or_host: str) -> float:
        """Reserve the next slot for this host. Returns the seconds waited,
        which callers ignore and tests assert on."""
        bucket = bucket_for(url_or_host)
        interval = self.interval_for(bucket)

        with self._lock:
            now = time.monotonic()
            earliest = self._next_free.get(bucket, now)
            start = max(now, earliest)
            self._next_free[bucket] = start + interval

        # Slept in a loop rather than once, because time.sleep is allowed to
        # return early and measurably does, by a few milliseconds. One sleep
        # left the observed gap at 0.195s against a 0.200s budget, which is
        # harmless on its own and is exactly the kind of slow drift that turns
        # a comfortable margin into a rate-limit block over a long run.
        waited = 0.0
        while True:
            remaining = start - time.monotonic()
            if remaining <= 0:
                return waited
            time.sleep(remaining)
            waited += remaining

    def reset(self) -> None:
        """Drop all reservations. For tests, not for production code."""
        with self._lock:
            self._next_free.clear()


# The single instance every adapter shares. Imported rather than constructed,
# because a per-adapter limiter is exactly the bug this module exists to fix.
limiter = HostRateLimiter()
