"""GDELT DOC 2.0: which articles the world's news sites published, and when.

GDELT's data is released for unrestricted use, commercial included. It gives
an article's URL, title, publisher domain, social image and first-seen time;
the summary is taken from the publisher's own page description afterwards.

GDELT asks for at most one request every five seconds. That is the budget the
news scheduler plans around: per-company queries for warm tickers and a
handful of market-news queries, never a burst.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.sources.rate_limit import limiter, set_interval

logger = logging.getLogger(__name__)

DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250

set_interval(DOC_API, 6.0)


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    domain: str
    image: str
    seen_at: datetime


# Publisher domains to display names, matching how Finnhub labels `source`.
_SOURCE_NAMES = {
    "finance.yahoo.com": "Yahoo", "yahoo.com": "Yahoo", "cnbc.com": "CNBC", "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg", "marketwatch.com": "MarketWatch", "wsj.com": "WSJ", "ft.com": "Financial Times",
    "seekingalpha.com": "SeekingAlpha", "benzinga.com": "Benzinga", "fool.com": "Motley Fool",
    "investors.com": "Investor's Business Daily", "barrons.com": "Barron's", "forbes.com": "Forbes",
    "businessinsider.com": "Business Insider", "coindesk.com": "CoinDesk", "cointelegraph.com": "Cointelegraph",
    "forexlive.com": "Forexlive", "fxstreet.com": "FXStreet", "apnews.com": "AP", "nytimes.com": "New York Times",
    "theverge.com": "The Verge", "techcrunch.com": "TechCrunch", "247wallst.com": "24/7 Wall St.",
    "zacks.com": "Zacks", "investing.com": "Investing.com", "thestreet.com": "TheStreet", "aol.com": "AOL",
    "morningstar.com": "Morningstar", "insidermonkey.com": "Insider Monkey", "prnewswire.com": "PRNewswire",
    "businesswire.com": "BusinessWire", "globenewswire.com": "GlobeNewswire",
}


def source_name(domain: str) -> str:
    d = domain.lower().removeprefix("www.")
    if d in _SOURCE_NAMES:
        return _SOURCE_NAMES[d]
    parent = ".".join(d.split(".")[-2:])
    return _SOURCE_NAMES.get(parent, parent)


def _parse_seen(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


class GdeltUnavailable(Exception):
    """GDELT did not answer (rate limit, outage). Distinct from an empty result."""


def search(query: str, *, start: datetime | None = None, end: datetime | None = None,
           timespan: str | None = None, client: httpx.Client | None = None) -> list[Article]:
    """Articles matching a GDELT query, newest first.

    Raises GdeltUnavailable when GDELT did not answer, so a caller does not
    mistake an outage for "no news" and skip ahead.
    """
    params = {"query": f"{query} sourcelang:english", "mode": "artlist", "format": "json",
              "maxrecords": str(MAX_RECORDS), "sort": "datedesc"}
    if start is not None:
        params["startdatetime"] = start.strftime("%Y%m%d%H%M%S")
        params["enddatetime"] = (end or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    elif timespan:
        params["timespan"] = timespan
    own = client is None
    client = client or httpx.Client(timeout=30.0, headers={"User-Agent": settings.scraper_user_agent})
    try:
        for attempt in range(3):
            limiter.acquire(DOC_API)
            resp = client.get(DOC_API, params=params)
            # GDELT signals its rate limit with a plain-text 200 as often as a 429.
            if resp.status_code == 429 or resp.text.startswith("Please limit requests"):
                time.sleep(10 * (attempt + 1))
                continue
            break
        if resp.status_code != 200 or resp.text.startswith("Please limit requests"):
            raise GdeltUnavailable(f"GDELT returned {resp.status_code} for {query!r}")
        try:
            payload = resp.json()
        except ValueError:
            # GDELT answers malformed or over-broad queries with a plain-text note.
            logger.info("GDELT: %s", resp.text[:200])
            return []
    except httpx.HTTPError as exc:
        raise GdeltUnavailable(str(exc)) from exc
    finally:
        if own:
            client.close()
    out = []
    for a in payload.get("articles", []):
        if not a.get("url") or not a.get("title"):
            continue
        out.append(Article(url=a["url"], title=a["title"], domain=a.get("domain") or urlparse(a["url"]).netloc,
                           image=a.get("socialimage") or "", seen_at=_parse_seen(a["seendate"])))
    return out


def company_query(name_alias: str, ticker: str) -> str:
    """Articles that name the company in a markets context, or its ticker.

    The context terms keep "apple" the fruit and "target" the verb out.
    """
    # GDELT accepts one flat OR group; nested groups and very short terms
    # (most tickers) are refused, so the ticker itself is not a search term.
    return f'"{name_alias}" (stock OR shares OR earnings OR analyst OR investors OR revenue)'


def rating_query(name_alias: str) -> str:
    """Broker rating actions naming the company."""
    return (f'"{name_alias}" (upgraded OR downgraded OR upgrades OR downgrades OR initiates OR initiated '
            f'OR reiterates OR reiterated OR "price target" OR overweight OR underweight OR outperform)')


def earnings_date_query(name_alias: str) -> str:
    """Companies announcing when they will report."""
    return (f'"{name_alias}" ("to report" OR "to announce" OR "will report" OR "will announce" OR "to release" '
            f'OR "conference call" OR "earnings call" OR webcast)')


MARKET_QUERIES = {
    "general": '(stocks OR "wall street" OR "stock market" OR "S&P 500" OR "Federal Reserve" OR inflation OR "treasury yields")',
    "forex": '(forex OR "exchange rate" OR "the dollar" OR "U.S. dollar" OR euro OR yen OR "currency markets")',
    "crypto": '(bitcoin OR ethereum OR crypto OR cryptocurrency OR stablecoin)',
    "merger": '(merger OR "to acquire" OR acquisition OR "takeover bid" OR "buyout" OR "agreed to buy")',
}
