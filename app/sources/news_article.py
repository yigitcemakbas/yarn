"""Fetching an article's text, to build a Finnhub-style `summary` (ported from loom).

yarn needs the lead of an article when its feed entry carries no description.
The conduct rules are loom's: robots.txt is checked against the publisher after
any redirect is resolved, and a body that cannot be had is never approximated.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from app.sources.base_scraper import BaseScraper, refuse_private

logger = logging.getLogger(__name__)

# Below this, what came back is a teaser, a cookie wall or a nav bar rather than
# an article. Set against the thing being replaced: the stub it would displace
# is around 250 characters, so a "body" near that length is no improvement and
# risks being worse, because it reads as article text without being any.
MIN_BODY_CHARS = 600

# Aggregator links chain occasionally; more than this is a loop.
_MAX_REDIRECT_HOPS = 4

# Containers that hold an article, in descending order of how explicitly they
# say so. Semantic markup first, then the class names publishers converged on.
_BODY_SELECTORS = (
    "[itemprop='articleBody']",
    "article",
    "main article",
    "[class*='article-body']",
    "[class*='article__body']",
    "[class*='story-body']",
    "[class*='post-content']",
    "[class*='entry-content']",
    "[data-component='text-block']",
    "main",
)

# Page furniture. Removed before extraction rather than filtered after, because
# once it is concatenated into the body text there is no reliable way to tell a
# promo box from a paragraph.
_NOISE = ",".join((
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form",
    "iframe", "figure", "figcaption", "svg", "button",
    "[class*='related']", "[class*='recommend']", "[class*='newsletter']",
    "[class*='subscribe']", "[class*='promo']", "[class*='advert']",
    "[class*='social']", "[class*='share']", "[class*='comment']",
    "[class*='paywall']", "[class*='cookie']", "[class*='consent']",
    "[id*='related']", "[id*='advert']",
))

# Phrases that mean the page returned a sales pitch rather than the article.
# Checked against the *extracted* text, so a publisher who merely mentions
# subscriptions in a footer is unaffected: the footer is already gone.
_PAYWALL_MARKERS = (
    "subscribe to continue", "already a subscriber", "subscribe now to read",
    "this article is for subscribers", "create an account to read",
    "sign in to read", "become a member to read", "to continue reading",
    "unlock this article", "subscription required",
)

_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class ArticleBody:
    """What was retrieved, and how, so the caller can record it honestly."""

    text: Optional[str]
    # One of: fetched, blocked_by_robots, paywalled, too_short, unavailable.
    outcome: str
    # The publisher's own URL, after the aggregator's redirect was resolved.
    resolved_url: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.outcome == "fetched" and bool(self.text)


class NewsArticleScraper(BaseScraper):
    """Resolves an aggregator link to its publisher and extracts the article.

    Inherits the politeness, not just the plumbing: per-domain rate limiting, an
    honest User-Agent that identifies Loom rather than impersonating a browser,
    and robots.txt obeyed before any body request.
    """

    # Slower than the default. A news run touches many publishers once each
    # rather than one publisher many times, so the per-domain interval rarely
    # binds; this is deliberate headroom for the case where a single outlet
    # supplies a burst of a company's coverage.
    def __init__(self, *, request_interval: float = 2.0, **kwargs):
        super().__init__(request_interval=request_interval, **kwargs)

    def resolve(self, url: str) -> Optional[str]:
        """Follow the aggregator's redirect to the publisher's own URL.

        Reads the `Location` header off the redirect rather than following it.
        Following meant a second request to the publisher before robots.txt had
        been consulted, which is the wrong order on principle, and it failed in
        practice too: Yahoo Finance rejects the bodyless follow-up, so every
        article behind their redirect came back unresolvable.

        A couple of hops are allowed, because aggregators chain; beyond that it
        is a loop and there is no article behind the link.
        """
        current = url
        try:
            with httpx.Client(
                headers={"User-Agent": self.user_agent}, timeout=20.0,
                follow_redirects=False,
                event_hooks={"request": [refuse_private]},
            ) as client:
                for _ in range(_MAX_REDIRECT_HOPS):
                    # GET, not HEAD, and the distinction is not cosmetic.
                    # Finnhub answers HEAD with `Location: /`, pointing at its
                    # own front page, and answers GET with the publisher's URL.
                    # Using HEAD made every article look like a dead link and
                    # produced a confident, wrong diagnosis that the ids had
                    # expired. A 302 carries no body, so GET costs no more here.
                    response = client.get(current)
                    location = response.headers.get("location")
                    if not location:
                        break
                    candidate = urljoin(current, location)
                    if urlparse(candidate).netloc != urlparse(url).netloc:
                        return candidate
                    current = candidate
        except Exception:
            logger.info("Could not resolve news URL %s", url)
            return None

        # Still on the aggregator's own domain: an expired link, not an article.
        logger.info("No publisher behind %s; the link has probably expired.", url)
        return None

    def body_for(self, url: str) -> ArticleBody:
        """The article behind an aggregator link. Never raises."""
        resolved = self.resolve(url)
        if resolved is None:
            return ArticleBody(None, "unavailable")

        if not self.robots.can_fetch(resolved):
            logger.info("robots.txt disallows the article at %s", resolved)
            return ArticleBody(None, "blocked_by_robots", resolved)

        html = self.fetch_html(resolved)
        if not html:
            return ArticleBody(None, "unavailable", resolved)

        text = extract_body(html)
        if text is None:
            return ArticleBody(None, "too_short", resolved)
        if is_paywalled(text):
            # Deliberately not stored. A teaser reads like an article and would
            # be quoted as one.
            return ArticleBody(None, "paywalled", resolved)
        return ArticleBody(text, "fetched", resolved)


def extract_body(html: str) -> Optional[str]:
    """The article text, or None when the page did not yield one.

    Tries explicit containers before falling back to paragraph density. The
    fallback exists because a meaningful share of publishers mark up an article
    with no semantic container at all, and the alternative to a heuristic there
    is storing nothing for them.
    """
    tree = HTMLParser(html)
    for node in tree.css(_NOISE):
        node.decompose()

    for selector in _BODY_SELECTORS:
        container = tree.css_first(selector)
        if container is None:
            continue
        text = _clean(container.text(separator="\n", strip=True))
        if len(text) >= MIN_BODY_CHARS:
            return text

    # Fallback: the paragraphs themselves. Short ones are captions, bylines and
    # standfirsts, which are not the article and dilute what is.
    paragraphs = [
        _clean(node.text(strip=True))
        for node in tree.css("p")
    ]
    joined = "\n\n".join(p for p in paragraphs if len(p) > 80)
    return joined if len(joined) >= MIN_BODY_CHARS else None


def is_paywalled(text: str) -> bool:
    """Whether what came back is a sales pitch rather than the article.

    Checked on the first part of the text only. A publisher who closes an
    article with a subscription appeal has still given Loom the article, and
    rejecting it over the footer would discard a body that is entirely usable.
    """
    head = text[:1200].lower()
    return any(marker in head for marker in _PAYWALL_MARKERS)


def _clean(text: str) -> str:
    text = _WHITESPACE.sub(" ", text or "")
    return _BLANK_LINES.sub("\n\n", text).strip()


__all__ = [
    "MIN_BODY_CHARS",
    "ArticleBody",
    "NewsArticleScraper",
    "extract_body",
    "is_paywalled",
]
