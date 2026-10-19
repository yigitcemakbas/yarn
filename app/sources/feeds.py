"""RSS feeds yarn reads: newswires (press releases) and central banks.

Each feed records why yarn may use it. Press-release wires distribute issuer
announcements for republication; central-bank press releases are public
statements (the Federal Reserve's are US-government works). Feeds marked
`enabled=False` refused automated access when last checked (2026-10-19).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import httpx

from app.config import settings
from app.sources.rate_limit import limiter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    # Finnhub `/news` category for market news; "company" for issuer releases.
    category: str
    source: str
    licence: str
    enabled: bool = True


FEEDS = [
    Feed("prnewswire-all", "https://www.prnewswire.com/rss/news-releases-list.rss", "company", "PRNewswire",
         "issuer press releases distributed for republication"),
    Feed("prnewswire-financial", "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
         "company", "PRNewswire", "issuer press releases distributed for republication"),
    Feed("prnewswire-earnings", "https://www.prnewswire.com/rss/financial-services-latest-news/earnings-list.rss",
         "company", "PRNewswire", "issuer press releases distributed for republication"),
    Feed("prnewswire-ma", "https://www.prnewswire.com/rss/financial-services-latest-news/acquisitions-mergers-and-takeovers-list.rss",
         "merger", "PRNewswire", "issuer press releases distributed for republication"),
    Feed("businesswire-all", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRWA==", "company", "BusinessWire",
         "issuer press releases distributed for republication"),
    Feed("globenewswire-public", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
         "company", "GlobeNewswire", "issuer press releases distributed for republication", enabled=False),
    Feed("accessnewswire", "https://www.accessnewswire.com/newsroom/api/rss", "company", "Accesswire",
         "issuer press releases distributed for republication", enabled=False),
    Feed("federalreserve", "https://www.federalreserve.gov/feeds/press_all.xml", "forex", "Federal Reserve",
         "US government work, public domain"),
    Feed("ecb", "https://www.ecb.europa.eu/rss/press.html", "forex", "ECB",
         "ECB press releases, reuse permitted with attribution"),
]


@dataclass(frozen=True)
class FeedItem:
    feed: Feed
    title: str
    url: str
    summary: str
    published_at: datetime
    image: str


def _published(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _image(entry) -> str:
    for media in entry.get("media_content") or []:
        if media.get("url"):
            return media["url"]
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image"):
            return link.get("href", "")
    return ""


def fetch(feed: Feed, client: httpx.Client | None = None) -> list[FeedItem]:
    """One poll of one feed. Never raises: a broken feed costs one cycle."""
    own = client is None
    client = client or httpx.Client(timeout=20.0, headers={"User-Agent": settings.scraper_user_agent}, follow_redirects=True)
    try:
        limiter.acquire(feed.url)
        resp = client.get(feed.url)
        if resp.status_code != 200:
            logger.info("Feed %s returned %s", feed.name, resp.status_code)
            return []
        parsed = feedparser.parse(resp.content)
    except Exception:
        logger.warning("Feed %s failed", feed.name, exc_info=True)
        return []
    finally:
        if own:
            client.close()
    items = []
    for e in parsed.entries:
        title, link = (e.get("title") or "").strip(), (e.get("link") or "").strip()
        if not title or not link:
            continue
        summary = feedparser_html_to_text(e.get("summary") or "")
        items.append(FeedItem(feed=feed, title=title, url=link, summary=summary,
                              published_at=_published(e), image=_image(e)))
    return items


def feedparser_html_to_text(raw: str) -> str:
    from selectolax.parser import HTMLParser

    if "<" not in raw:
        return " ".join(raw.split())
    tree = HTMLParser(raw)
    return " ".join((tree.text(separator=" ") if tree.body is None else tree.body.text(separator=" ")).split())
