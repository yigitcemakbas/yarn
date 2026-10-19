"""The four Finnhub endpoints loom calls, served from yarn's own tables.

Every behaviour here is measured against Finnhub and recorded in
tests/contract/contract.json:

  - key order inside each item, integers vs floats, "" vs null
  - default windows when dates are omitted
  - item caps (250 company news, 100 market news)
  - sort orders
  - the exact 422 messages
  - unknown symbols answering 200 with an empty list

Fields Finnhub does not have are appended after Finnhub's own keys, so a
Finnhub-shaped parser sees exactly what it expects first.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import EarningsEvent, NewsArticle, NewsArticleSymbol
from app.pipelines import recommendations, warmup

router = APIRouter(prefix="/api/v1")

COMPANY_NEWS_MAX_ITEMS = 250
MARKET_NEWS_MAX_ITEMS = 100
MARKET_NEWS_CATEGORIES = ("general", "forex", "crypto", "merger")
# With no `from`, Finnhub's earnings calendar starts four weeks back.
EARNINGS_DEFAULT_LOOKBACK_DAYS = 28
RECOMMENDATION_MONTHS = 4

MISSING_SYMBOL = {"error": "Missing parameter symbol"}
WRONG_DATE = {"error": "Wrong date format. Please use 2022-01-15 format for from and to params."}


class _BadDate(ValueError):
    pass


def _parse_date(raw: str | None, default: date) -> date:
    if raw is None or raw == "":
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _BadDate(raw) from exc


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end, time.max, tzinfo=timezone.utc),
    )


def _news_item(article: NewsArticle, related: str, category: str | None = None) -> dict:
    return {
        "category": category or article.category,
        "datetime": int(article.published_at.timestamp()),
        "headline": article.headline,
        "id": article.id,
        "image": article.image or "",
        "related": related,
        "source": article.source,
        "summary": article.summary or "",
        "url": article.url,
    }


@router.get("/company-news")
def company_news(request: Request, db: Session = Depends(get_db)):
    params = request.query_params
    symbol = (params.get("symbol") or "").strip().upper()
    if not symbol:
        return JSONResponse(MISSING_SYMBOL, status_code=422)
    today = datetime.now(timezone.utc).date()
    try:
        start = _parse_date(params.get("from"), today)
        end = _parse_date(params.get("to"), today)
    except _BadDate:
        return JSONResponse(WRONG_DATE, status_code=422)
    if start > end:
        return JSONResponse([])

    warmup.ensure_warm(db, symbol)
    lo, hi = _day_bounds(start, end)
    rows = db.execute(
        select(NewsArticle)
        .join(NewsArticleSymbol, NewsArticleSymbol.article_id == NewsArticle.id)
        .where(NewsArticleSymbol.ticker == symbol)
        .where(NewsArticleSymbol.published_at >= lo, NewsArticleSymbol.published_at <= hi)
        .order_by(NewsArticleSymbol.published_at.desc(), NewsArticle.id.desc())
        .limit(COMPANY_NEWS_MAX_ITEMS)
    ).scalars()
    # Finnhub labels every company-news item "company", whatever else it is.
    return JSONResponse([_news_item(a, symbol, "company") for a in rows])


@router.get("/news")
def market_news(request: Request, db: Session = Depends(get_db)):
    params = request.query_params
    category = (params.get("category") or "general").lower()
    # Finnhub answers an unknown category with general news rather than an error.
    if category not in MARKET_NEWS_CATEGORIES:
        category = "general"
    try:
        min_id = int(params.get("minId") or 0)
    except ValueError:
        min_id = 0

    stmt = select(NewsArticle).where(NewsArticle.category == _stored_category(category))
    if min_id:
        stmt = stmt.where(NewsArticle.id > min_id)
    rows = db.execute(
        stmt.order_by(NewsArticle.published_at.desc(), NewsArticle.id.desc()).limit(MARKET_NEWS_MAX_ITEMS)
    ).scalars()
    return JSONResponse([_news_item(a, "") for a in rows])


def _stored_category(category: str) -> str:
    # Finnhub labels general-news items "business" (and some "top news"); the
    # other categories carry their own name.
    return "business" if category == "general" else category


def _earnings_item(e: EarningsEvent) -> dict:
    return {
        "symbol": e.ticker,
        "date": e.date.isoformat(),
        "hour": e.hour or "",
        "quarter": e.fiscal_quarter,
        "year": e.fiscal_year,
        "epsEstimate": e.eps_estimate,
        "epsActual": e.eps_actual,
        "revenueEstimate": e.revenue_estimate,
        "revenueActual": e.revenue_actual,
        # yarn's additions, after Finnhub's own keys.
        "dateStatus": e.date_status,
        "dateSource": e.date_source,
        "estimateBasis": e.estimate_basis,
        "epsBasis": e.eps_basis,
        "epsActualGaap": e.eps_actual_gaap,
    }


@router.get("/calendar/earnings")
def earnings_calendar(request: Request, db: Session = Depends(get_db)):
    params = request.query_params
    today = datetime.now(timezone.utc).date()
    try:
        start = _parse_date(params.get("from"), today - timedelta(days=EARNINGS_DEFAULT_LOOKBACK_DAYS))
        end = _parse_date(params.get("to"), today)
    except _BadDate:
        return JSONResponse(WRONG_DATE, status_code=422)

    stmt = select(EarningsEvent).where(EarningsEvent.date >= start, EarningsEvent.date <= end)
    symbol = (params.get("symbol") or "").strip().upper()
    if symbol:
        warmup.ensure_warm(db, symbol)
        stmt = stmt.where(EarningsEvent.ticker == symbol)
    rows = db.execute(stmt.order_by(EarningsEvent.date.desc(), EarningsEvent.ticker.asc())).scalars()
    return JSONResponse({"earningsCalendar": [_earnings_item(e) for e in rows]})


@router.get("/stock/recommendation")
def recommendation_trends(request: Request, db: Session = Depends(get_db)):
    symbol = (request.query_params.get("symbol") or "").strip().upper()
    if not symbol:
        return JSONResponse(MISSING_SYMBOL, status_code=422)
    warmup.ensure_warm(db, symbol)
    today = datetime.now(timezone.utc).date()
    return JSONResponse(recommendations.monthly_trend(db, symbol, today=today, months=RECOMMENDATION_MONTHS))
