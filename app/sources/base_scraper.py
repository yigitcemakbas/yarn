"""Shared scraper infrastructure: robots.txt, rate limiting, honest identity.

Every scraper in the project goes through this class. The three rules it
enforces are not decoration, they are the difference between a research tool
and a nuisance:

1. **robots.txt is obeyed, not consulted.** `can_fetch` is checked before every
   request and a disallowed URL is skipped. The fetch returns None; the caller
   logs and moves on. There is no override flag, deliberately, an override that
   exists is an override that eventually gets used.
2. **One request per domain at a time, spaced.** Rate limiting is keyed by
   domain rather than by scraper, so two scrapers pointed at the same host
   still cooperate. It is delegated to `ingestion/rate_limit.py` so that
   scrapers and the SEC adapters share one view of how busy this process is
   being; a per-scraper limiter would have each source politely pacing itself
   while the process as a whole was not.
3. **The User-Agent identifies this tool honestly.** It never impersonates a
   browser. If a site wants to block yarn it must be able to, spoofing Chrome
   to evade a block is exactly the behaviour robots.txt exists to prevent.

A failure inside one scraper is contained here (`fetch_html` returns None
rather than raising) so a broken selector or a 503 on one source can never
take down an ingestion run for the others.
"""

import ipaddress
import logging
import socket
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.config import settings
from app.sources.rate_limit import DEFAULT_INTERVAL_SECONDS, limiter, set_interval

logger = logging.getLogger(__name__)


class PrivateAddressRefused(httpx.RequestError):
    """A scraper was pointed at somewhere that is not the public internet."""


def refuse_private(request: httpx.Request) -> None:
    """Refuse any request, redirects included, to a non-public address.

    Scrapers follow links Loom did not choose: a news feed's redirect names
    the publisher, and the publisher's server names the next hop. Without
    this, one hostile hop could point Loom at the cloud metadata service, the
    database's network or the backend itself, and the reply would be stored as
    an article and served through the API. Registered as an httpx request hook
    so it runs on every hop, not only the first.
    """
    url = request.url
    if url.scheme not in ("http", "https") or not url.host:
        raise PrivateAddressRefused(f"Refused non-web URL {url}", request=request)
    try:
        infos = socket.getaddrinfo(url.host, url.port or (443 if url.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise PrivateAddressRefused(f"Could not resolve {url.host}", request=request) from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise PrivateAddressRefused(f"Refused non-public address for {url.host}", request=request)

# Politeness gap between two requests to the same domain. Deliberately well
# above what the sites involved require: nothing here is latency-sensitive.
# The value now lives with the limiter, since it is one process-wide budget.
DEFAULT_REQUEST_INTERVAL_SECONDS = DEFAULT_INTERVAL_SECONDS

# robots.txt rarely changes; re-fetching it per request would itself be the
# kind of traffic it is meant to limit.
_ROBOTS_CACHE_SECONDS = 3600.0


class RobotsPolicy:
    """Per-domain robots.txt cache.

    A domain whose robots.txt cannot be fetched is treated as **allowed**,
    matching the convention every mainstream crawler follows (an unreachable
    robots.txt is an absent one, not a blanket denial). A domain whose
    robots.txt is fetched and *does* disallow the path is always refused.
    """

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._parsers: dict[str, tuple[RobotFileParser | None, float]] = {}
        self._lock = threading.Lock()

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        domain = f"{parsed.scheme}://{parsed.netloc}"

        parser = self._parser_for(domain)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _parser_for(self, domain: str) -> RobotFileParser | None:
        with self._lock:
            cached = self._parsers.get(domain)
            if cached is not None and (time.monotonic() - cached[1]) < _ROBOTS_CACHE_SECONDS:
                return cached[0]

        parser = self._load(domain)
        with self._lock:
            self._parsers[domain] = (parser, time.monotonic())
        return parser

    def _load(self, domain: str) -> RobotFileParser | None:
        url = f"{domain}/robots.txt"
        try:
            with httpx.Client(event_hooks={"request": [refuse_private]}) as client:
                resp = client.get(url, headers={"User-Agent": self.user_agent}, timeout=15.0)
        except Exception:
            logger.warning("robots.txt unreachable for %s, proceeding as unrestricted.", domain)
            return None

        if resp.status_code != 200:
            logger.info("robots.txt returned %s for %s, proceeding as unrestricted.", resp.status_code, domain)
            return None

        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


class BaseScraper:
    """Base for every scraping adapter. Not itself a DocumentSourceAdapter,
    a concrete scraper inherits from both this and `DocumentSourceAdapter`,
    so that "how to fetch politely" and "what to produce" stay separate
    concerns rather than one class doing both jobs.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        request_interval: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
    ):
        self.user_agent = user_agent or settings.scraper_user_agent
        self.request_interval = request_interval
        self.robots = RobotsPolicy(self.user_agent)
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent},
            timeout=30.0,
            follow_redirects=True,
            event_hooks={"request": [refuse_private]},
        )
        self._interval_registered = False

    def _throttle(self, url: str) -> None:
        """Wait for this domain's turn.

        The bookkeeping deliberately lives in the shared limiter rather than
        here. The previous version kept a per-instance dict guarded by a lock
        it held *while sleeping*, which was correct while ingestion was a
        single sequential loop and became the dominant bottleneck the moment it
        was not: every worker thread queued on that one lock, so a wait for
        one domain blocked requests to every other domain, and concurrency
        bought nothing.
        """
        if not self._interval_registered:
            # Registered on first use rather than in __init__, so a scraper
            # constructed but never run does not claim a budget, and so a
            # scraper that wants a slower cadence than the default gets it.
            if self.request_interval != DEFAULT_REQUEST_INTERVAL_SECONDS:
                set_interval(url, self.request_interval)
            self._interval_registered = True

        limiter.acquire(url)

    def fetch_html(self, url: str) -> str | None:
        """Fetch one URL, or None if robots.txt forbids it or the request fails.

        Never raises. Callers treat None as "this one is unavailable" and
        continue, which is what keeps one bad page from ending a batch.
        """
        if not self.robots.can_fetch(url):
            logger.info("robots.txt disallows %s, skipping.", url)
            return None

        self._throttle(url)
        try:
            resp = self._client.get(url)
        except Exception:
            logger.warning("Request failed for %s", url, exc_info=True)
            return None

        if resp.status_code != 200:
            logger.info("Fetch returned %s for %s", resp.status_code, url)
            return None
        return resp.text
