"""yarn's tables. Imported here so Base.metadata sees every model."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Company(Base):
    """One SEC filer with a listed ticker. The universe news and earnings attach to."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    cik: Mapped[str] = mapped_column(String(10), index=True)
    # The registrant this ticker belonged to before a reorganisation (a new
    # holding company takes a new CIK; its history stays under the old one).
    predecessor_cik: Mapped[str | None] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(32))
    # Name variants a headline would actually use ("Apple" for "Apple Inc.").
    aliases: Mapped[str] = mapped_column(Text, default="")
    # Last time each source was refreshed for this company. Null means cold.
    news_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    earnings_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ratings_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where the company publishes its event calendar: a Q4 feed host
    # (platform "q4"), an events page URL ("page"), or "none"; rechecked monthly.
    ir_host: Mapped[str | None] = mapped_column(String(255))
    ir_platform: Mapped[str | None] = mapped_column(String(16))
    ir_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ir_events_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warm: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsArticle(Base):
    """One article, press release or filing exhibit, deduplicated by canonical URL.

    `id` is the Finnhub-compatible integer id: a stable hash of the canonical
    URL, so the same article keeps its id across re-ingestion and `minId`
    paging works.
    """

    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    url: Mapped[str] = mapped_column(Text, unique=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(128))
    image: Mapped[str] = mapped_column(Text, default="")
    # Finnhub's `/news` categories: general, forex, crypto, merger. Company-only
    # items carry "company".
    category: Mapped[str] = mapped_column(String(32), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Which upstream produced it (gdelt, newswire:globenewswire, sec-8k, rss:cnbc).
    origin: Mapped[str] = mapped_column(String(64))
    summary_basis: Mapped[str] = mapped_column(String(32), default="feed")
    headline_hash: Mapped[int] = mapped_column(BigInteger, index=True)
    # 1 when the headline names a linked company: those are the items loom
    # keeps, so their summaries are fetched first.
    summary_priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    summarized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsArticleSymbol(Base):
    """Which tickers an article is about. Finnhub's `related` is the requested symbol."""

    __tablename__ = "news_article_symbols"

    article_id: Mapped[int] = mapped_column(ForeignKey("news_articles.id", ondelete="CASCADE"), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), primary_key=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_news_symbol_ticker_published", "ticker", "published_at"),)


class EarningsEvent(Base):
    """One fiscal quarter's report for one company: scheduled, confirmed or reported."""

    __tablename__ = "earnings_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    fiscal_year: Mapped[int] = mapped_column(Integer)
    fiscal_quarter: Mapped[int] = mapped_column(Integer)
    date: Mapped[date] = mapped_column(Date, index=True)
    # bmo / amc / dmh, or "" when unknown, exactly as Finnhub reports it.
    hour: Mapped[str] = mapped_column(String(3), default="")
    # reported, confirmed (company announced the date), projected (cadence).
    date_status: Mapped[str] = mapped_column(String(16))
    # What set the date: "8-k" (the release itself), "projection",
    # "ir-calendar:q4", "newswire", "news", "8-k-announcement".
    date_source: Mapped[str | None] = mapped_column(String(32))
    eps_actual: Mapped[float | None] = mapped_column(Float)
    eps_actual_gaap: Mapped[float | None] = mapped_column(Float)
    eps_basis: Mapped[str | None] = mapped_column(String(16))
    revenue_actual: Mapped[int | None] = mapped_column(BigInteger)
    eps_estimate: Mapped[float | None] = mapped_column(Float)
    revenue_estimate: Mapped[int | None] = mapped_column(BigInteger)
    estimate_basis: Mapped[str | None] = mapped_column(String(16))
    accession: Mapped[str | None] = mapped_column(String(32))
    # True once the press release itself was read; until then actuals may be
    # XBRL fills and the release is fetched again on the next pass.
    release_parsed: Mapped[bool] = mapped_column(default=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("ticker", "fiscal_year", "fiscal_quarter", name="uq_earnings_period"),)


class RatingAction(Base):
    """One broker rating action read from a headline: upgrade, downgrade, initiation, reiteration."""

    __tablename__ = "rating_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(12), index=True)
    firm: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))
    rating: Mapped[str] = mapped_column(String(64))
    # strongBuy / buy / hold / sell / strongSell
    bucket: Mapped[str] = mapped_column(String(16))
    price_target: Mapped[float | None] = mapped_column(Float)
    acted_on: Mapped[date] = mapped_column(Date, index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("news_articles.id", ondelete="SET NULL"))

    __table_args__ = (UniqueConstraint("ticker", "firm", "acted_on", name="uq_rating_action"),)


class IngestCursor(Base):
    """Where a stream ingester got to (the last GDELT GKG slot processed, say)."""

    __tablename__ = "ingest_cursors"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
