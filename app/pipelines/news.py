"""News ingestion: articles in, Finnhub-shaped rows out.

Three inputs feed one table:

  GDELT        publisher articles, per company (company news) and per topic
               (market news)
  newswires    issuer press releases (PR Newswire, Business Wire), matched to
               companies by their "(NASDAQ: XYZ)" exchange tags, or by name
  regulators   central-bank press releases for the forex category

Every stored headline also passes through two extractors: broker rating
actions (for /stock/recommendation) and earnings-date announcements (which
turn a projected earnings date into a confirmed one).
"""

import hashlib
import html
from concurrent.futures import ThreadPoolExecutor
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Company, NewsArticle, NewsArticleSymbol, RatingAction
from app.pipelines import companies as company_names
from app.pipelines import earnings_dates
from app.pipelines.recommendations import parse_headline
from app.sources import feeds, gdelt
from app.sources.base_scraper import BaseScraper
from app.sources.news_article import extract_body, is_paywalled

logger = logging.getLogger(__name__)

_TRACKING_PARAMS = re.compile(r"^(utm_|mc_|cmpid|ncid|guccounter|.*ref$|yptr|soc_)", re.IGNORECASE)
_EXCHANGE_TAG = re.compile(r"\((?:NASDAQ|NYSE(?:\s+American|\s+Arca)?|Nasdaq(?:GS|GM|CM)?|Cboe BZX|OTCQX|OTCQB)\s*:\s*([A-Z][A-Z.\-]{0,6})\)")


def canonical_url(url: str) -> str:
    parts = urlparse(url.strip())
    query = "&".join(q for q in parts.query.split("&") if q and not _TRACKING_PARAMS.match(q.split("=")[0]))
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", "", query, ""))


def article_id(url: str) -> int:
    """Stable positive int63 id from the canonical URL (Finnhub ids are integers)."""
    return int.from_bytes(hashlib.sha256(canonical_url(url).encode()).digest()[:8], "big") & ((1 << 62) - 1)


def _headline_hash(headline: str) -> int:
    norm = re.sub(r"[^a-z0-9]+", " ", headline.lower()).strip()
    return int.from_bytes(hashlib.sha256(norm.encode()).digest()[:8], "big") & ((1 << 62) - 1)


def clean_title(title: str) -> str:
    """GDELT tokenises titles ("Apple $1 , 999 iPhone"); undo the common splits."""
    t = html.unescape(title)
    t = re.sub(r"\s+([,.;:!?%)])", r"\1", t)
    t = re.sub(r"([($])\s+", r"\1", t)
    t = re.sub(r"(\d),\s+(\d{3})", r"\1,\2", t)
    t = re.sub(r"\s+'\s*s\b", "'s", t)
    t = re.sub(r"\s+’\s*s\b", "’s", t)
    return " ".join(t.split())


# Loom keeps a news item only when its summary has at least this many
# characters; Finnhub's summaries usually do.
SUMMARY_MIN_CHARS = 120
SUMMARY_MAX_CHARS = 500
SUMMARY_WORKERS = 8


class PageDescriptions(BaseScraper):
    """A Finnhub-style summary for an article, from the publisher's own page.

    The publisher's description (og:description, then meta description) comes
    first. When that is shorter than loom's threshold, the article's opening
    sentences from the same page complete it. robots.txt is honoured and a
    page that cannot be read leaves the summary empty, never approximated.
    """

    def describe(self, url: str) -> tuple[str, str]:
        """(summary, og:title). Empty strings when the page cannot be read or forbids it."""
        page = self.fetch_html(url)
        if not page:
            return "", ""
        tree = HTMLParser(page)

        def meta(*names: str) -> str:
            for n in names:
                node = tree.css_first(f'meta[property="{n}"]') or tree.css_first(f'meta[name="{n}"]')
                if node is not None and node.attributes.get("content"):
                    return " ".join(html.unescape(node.attributes["content"]).split())
            return ""

        summary = meta("og:description", "description", "twitter:description")
        if len(summary) < SUMMARY_MIN_CHARS:
            lead = _lead(page)
            if lead and not lead.startswith(summary[:40]):
                summary = f"{summary} {lead}".strip() if summary else lead
            elif lead:
                summary = lead
        return _trim(summary), meta("og:title", "twitter:title")


def _lead(page: str) -> str:
    body = extract_body(page)
    if not body or is_paywalled(body):
        return ""
    return " ".join(body.split())[: SUMMARY_MAX_CHARS * 2]


def _trim(text: str) -> str:
    """At most SUMMARY_MAX_CHARS, cut at a sentence end where one is near."""
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    cut = text[:SUMMARY_MAX_CHARS]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end >= SUMMARY_MIN_CHARS else cut.rsplit(" ", 1)[0] + "..."


def fill_summaries(db: Session, *, budget: int, describer: "PageDescriptions | None" = None) -> int:
    """Describe stored articles that have no summary yet, newest first."""
    describer = describer or PageDescriptions()
    pending = db.execute(
        select(NewsArticle).where(NewsArticle.summary_basis == "none")
        .order_by(NewsArticle.published_at.desc()).limit(budget)
    ).scalars().all()
    # Pages are fetched in parallel across publishers; the shared per-host
    # limiter still spaces requests to any one domain.
    with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as pool:
        described = list(pool.map(lambda a: _safe_describe(describer, a.url), pending))
    for a, (summary, og_title) in zip(pending, described):
        if og_title and len(og_title) > 10:
            a.headline = html.unescape(og_title)
        a.summary = summary
        a.summary_basis = "publisher-page" if summary else "unavailable"
        if summary:
            tickers = list(db.execute(select(NewsArticleSymbol.ticker).where(NewsArticleSymbol.article_id == a.id)).scalars())
            if tickers:
                record_earnings_date(db, a.headline, summary, tickers)
    db.commit()
    return len(pending)


def _safe_describe(describer: "PageDescriptions", url: str) -> tuple[str, str]:
    try:
        return describer.describe(url)
    except Exception:
        logger.info("Could not describe %s", url, exc_info=True)
        return "", ""


def store_article(db: Session, *, url: str, headline: str, summary: str, source: str, image: str,
                  category: str, published_at: datetime, origin: str, summary_basis: str,
                  tickers: list[str]) -> int | None:
    date_source = "newswire" if origin.startswith("feed:") else "news"
    """Upsert one article and its symbols. Returns its id, or None when it duplicates another headline."""
    aid = article_id(url)
    hh = _headline_hash(headline)
    dup = db.execute(
        select(NewsArticle.id).where(NewsArticle.headline_hash == hh, NewsArticle.id != aid,
                                     NewsArticle.published_at >= published_at - timedelta(days=2))
    ).first()
    if dup is not None:
        aid = dup[0]
    else:
        db.execute(insert(NewsArticle).values(
            id=aid, url=url, headline=headline, summary=summary, source=source, image=image, category=category,
            published_at=published_at, origin=origin, summary_basis=summary_basis, headline_hash=hh,
        ).on_conflict_do_nothing(index_elements=[NewsArticle.id]))
    for t in tickers:
        db.execute(insert(NewsArticleSymbol).values(article_id=aid, ticker=t, published_at=published_at)
                   .on_conflict_do_nothing())
    if tickers:
        record_rating_action(db, aid, headline, tickers, published_at.date())
        record_earnings_date(db, headline, summary, tickers, source=date_source)
    return aid


def record_rating_action(db: Session, aid: int, headline: str, tickers: list[str], acted_on: date) -> None:
    parsed = parse_headline(headline)
    if parsed is None:
        return
    for t in tickers:
        db.execute(insert(RatingAction).values(
            ticker=t, firm=parsed.firm, action=parsed.action, rating=parsed.rating, bucket=parsed.bucket,
            price_target=parsed.price_target, acted_on=acted_on, article_id=aid,
        ).on_conflict_do_nothing(constraint="uq_rating_action"))


_MONTHS = (r"January|February|March|April|May|June|July|August|September|October|November|December|"
           r"Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sept?\.?|Oct\.?|Nov\.?|Dec\.?")
_WHEN = (rf"(?:on\s+)?(?:(?:Mon|Tues|Wednes|Thurs|Fri)day,?\s+)?(?P<month>{_MONTHS})\s+(?P<day>\d{{1,2}})"
         rf"(?:st|nd|rd|th)?(?:,\s*(?P<year>\d{{4}}))?")
_DATE_ANNOUNCE = re.compile(
    # "will report its fourth quarter results on Thursday, October 30, 2026"
    rf"(?i)\b(?:to|will)\s+(?:report|announce|release|issue|discuss|publish|post)\b[^.]{{0,160}}?"
    rf"(?:results|earnings)\b[^.]{{0,160}}?\b{_WHEN}"
    # "will host a conference call on Wednesday, November 18, 2026 ... to discuss its results"
    rf"|\b(?:to|will)\s+(?:host|hold)\s+(?:a|an|its)\s+(?:[\w-]+\s+){{0,6}}(?:conference call|webcast|earnings call)"
    rf"[^.]{{0,40}}?\b{_WHEN.replace('(?P<month>', '(?P<month2>').replace('(?P<day>', '(?P<day2>').replace('(?P<year>', '(?P<year2>')}"
)
_AMC = re.compile(r"(?i)after (?:the )?(?:u\.s\. )?(?:stock )?market(?:s)? (?:close|closes)|after the close")
_BMO = re.compile(r"(?i)before (?:the )?(?:u\.s\. )?(?:stock )?market(?:s)? (?:open|opens)|prior to (?:the )?market open|pre-market")


def parse_announcement(text: str, *, today: date, subject: list[str] | None = None) -> tuple[date, str] | None:
    """(date, hour) a company says it will report on, from announcement text.

    With `subject` (the company's names and ticker), the announcing sentence
    itself must name the company. News is linked to every company it
    mentions, so without this an article about Innodata's results date that
    mentions Microsoft would move Microsoft's date.
    """
    m = None
    for candidate in _DATE_ANNOUNCE.finditer(text):
        if subject is None or _names_subject(text, candidate.start(), subject):
            m = candidate
            break
    if not m:
        return None
    month = (m.group("month") or m.group("month2") or "").rstrip(".")
    if month.lower().startswith("sept"):
        month = "Sep"
    day = m.group("day") or m.group("day2")
    year_raw = m.group("year") or m.group("year2")
    year = int(year_raw) if year_raw else today.year
    when = None
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            when = datetime.strptime(f"{month} {day} {year}", fmt).date()
            break
        except ValueError:
            continue
    if when is None:
        return None
    if not year_raw and when < today - timedelta(days=60):
        when = when.replace(year=year + 1)
    hour = "amc" if _AMC.search(text) else ("bmo" if _BMO.search(text) else "")
    return when, hour


def _names_subject(text: str, pos: int, subject: list[str]) -> bool:
    start = max(text.rfind(". ", 0, pos) + 1, pos - 160, 0)
    window = text[start:pos + 40]
    low = window.lower()
    for name in subject:
        if name.isupper() and len(name) <= 6:
            if re.search(rf"\b{re.escape(name)}\b", window):
                return True
        elif name and re.search(rf"(?<![\w]){re.escape(name.lower())}(?![\w])", low):
            return True
    return False


def record_earnings_date(db: Session, headline: str, summary: str, tickers: list[str], *, source: str = "news") -> None:
    """Confirm an upcoming earnings date from a company's announcement, as reported in text."""
    text = f"{headline}. {summary}"
    today = datetime.now(timezone.utc).date()
    for t in tickers:
        company = db.execute(select(Company).where(Company.ticker == t)).scalars().first()
        subject = [a for a in (company.aliases or "").split("|") if a] + [t] if company else [t]
        found = parse_announcement(text, today=today, subject=subject)
        if found is not None:
            earnings_dates.confirm_date(db, t, found[0], found[1], source)


def reconfirm_from_stored(db: Session, *, days: int = 60) -> int:
    """Re-run date extraction over recent stored articles (after the extractor changes)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(NewsArticle, NewsArticleSymbol.ticker)
        .join(NewsArticleSymbol, NewsArticleSymbol.article_id == NewsArticle.id)
        .where(NewsArticle.published_at >= since)
    ).all()
    for article, ticker in rows:
        source = "newswire" if article.origin.startswith("feed:") else "news"
        record_earnings_date(db, article.headline, article.summary or "", [ticker], source=source)
    db.commit()
    return len(rows)


def ingest_company_gdelt(db: Session, company: Company, *, since: datetime | None) -> int:
    """One GDELT pass for one company. Summaries are filled afterwards by fill_summaries."""
    alias = (company.aliases or company_names.core_name(company.name)).split("|")[0]
    start = since or datetime.now(timezone.utc) - timedelta(days=60)
    articles = gdelt.search(gdelt.company_query(alias, company.ticker), start=start)
    stored = 0
    for a in articles:
        url = a.url
        if db.execute(select(NewsArticleSymbol.article_id).where(
                NewsArticleSymbol.article_id == article_id(url), NewsArticleSymbol.ticker == company.ticker)).first():
            continue
        headline = clean_title(a.title)
        summary, basis = "", "none"
        store_article(db, url=url, headline=headline, summary=summary, source=gdelt.source_name(a.domain),
                      image=a.image, category="company", published_at=a.seen_at, origin="gdelt",
                      summary_basis=basis, tickers=[company.ticker])
        stored += 1
    company.news_refreshed_at = datetime.now(timezone.utc)
    db.commit()
    return stored


def ingest_company_ratings(db: Session, company: Company) -> int:
    """Rating-action headlines for one company: since the last sweep, or GDELT's full window the first time."""
    alias = (company.aliases or company_names.core_name(company.name)).split("|")[0]
    start = (company.ratings_refreshed_at - timedelta(hours=2)) if company.ratings_refreshed_at \
        else datetime.now(timezone.utc) - timedelta(days=90)
    stored = 0
    for a in gdelt.search(gdelt.rating_query(alias), start=start):
        headline = clean_title(a.title)
        if parse_headline(headline) is None:
            continue
        store_article(db, url=a.url, headline=headline, summary="", source=gdelt.source_name(a.domain),
                      image=a.image, category="company", published_at=a.seen_at, origin="gdelt-ratings",
                      summary_basis="none", tickers=[company.ticker])
        stored += 1
    # Earnings-date announcements: their summaries (filled later) usually
    # carry the date, so they are stored like any company news.
    for a in gdelt.search(gdelt.earnings_date_query(alias), start=start):
        if not _ANNOUNCEMENT_TITLE.search(a.title):
            continue
        store_article(db, url=a.url, headline=clean_title(a.title), summary="", source=gdelt.source_name(a.domain),
                      image=a.image, category="company", published_at=a.seen_at, origin="gdelt-dates",
                      summary_basis="none", tickers=[company.ticker])
        stored += 1
    company.ratings_refreshed_at = datetime.now(timezone.utc)
    db.commit()
    return stored


_ANNOUNCEMENT_TITLE = re.compile(r"(?i)(results|earnings|conference call|webcast)")


def ingest_market_gdelt(db: Session, category: str) -> int:
    articles = gdelt.search(gdelt.MARKET_QUERIES[category], timespan="1d")
    stored = 0
    stored_category = "business" if category == "general" else category
    for a in articles[:100]:
        if db.get(NewsArticle, article_id(a.url)) is not None:
            continue
        headline, summary, basis = clean_title(a.title), "", "none"
        store_article(db, url=a.url, headline=headline, summary=summary, source=gdelt.source_name(a.domain),
                      image=a.image, category=stored_category, published_at=a.seen_at, origin="gdelt",
                      summary_basis=basis, tickers=[])
        stored += 1
    db.commit()
    return stored


def tickers_in(text: str, universe: dict[str, Company], headline: str) -> list[str]:
    """Companies a press release is about: exchange tags first, then names in the headline."""
    tagged = [t.replace(".", "-") if t.replace(".", "-") in universe else t for t in _EXCHANGE_TAG.findall(text)]
    found = [t for t in dict.fromkeys(tagged) if t in universe]
    if found:
        return found
    return []


def ingest_feeds(db: Session, *, universe: dict[str, Company] | None = None) -> int:
    universe = universe or {c.ticker: c for c in db.execute(select(Company)).scalars()}
    stored = 0
    for feed in feeds.FEEDS:
        if not feed.enabled:
            continue
        for item in feeds.fetch(feed):
            if db.get(NewsArticle, article_id(item.url)) is not None:
                continue
            text = f"{item.title} {item.summary}"
            tickers = tickers_in(text, universe, item.title) if feed.category in ("company", "merger") else []
            category = feed.category
            if category == "company" and not tickers:
                # A release about no listed company is not company news.
                continue
            stored_category = "company" if category == "company" else category
            store_article(db, url=item.url, headline=html.unescape(item.title), summary=item.summary,
                          source=feed.source, image=item.image, category=stored_category,
                          published_at=item.published_at, origin=f"feed:{feed.name}",
                          summary_basis="feed", tickers=tickers)
            # An M&A release is also market news under "merger".
            stored += 1
        db.commit()
    return stored
