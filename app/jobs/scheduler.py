"""The worker's schedule. Runs in `yarn-worker`, never in the API process.

Cadences are set by freshness targets, not convenience:

  EDGAR live 8-K feed   60 s   an earnings release is served within ~2 min
  newswire feeds        2 min  press releases, central banks
  GDELT GKG stream      5 min  every article GDELT saw, matched to all warm
                               companies at once (15-minute files)
  GDELT company search  1 min  backfill for newly warm companies, and a top-up
                               every 3 h, inside GDELT's 1-request-per-6s budget
  GDELT market news     10 min general / forex / crypto / merger
  article summaries     1 min  publisher page descriptions, newest first
  rating actions        daily  per warm company, rotating every 2 min
  IR event calendars    12 h   per warm company (Q4 feeds), confirms upcoming dates
  8-K announcements     30 min dates announced under Items 7.01 / 8.01
  earnings rebuild      6 h    every warm company (projections, XBRL fills)
  SEC directory         daily  new listings, renamed companies
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select

from app.config import settings
from app.db.session import SessionLocal
from app.models import Company
from app.pipelines import companies, earnings, earnings_dates, news
from app.sources import gdelt, ir_events, sec

logger = logging.getLogger(__name__)

GDELT_COMPANIES_PER_MINUTE = 8
# Page fetches per summary pass. Each is a request to a publisher, so this
# bounds what yarn costs them; per-domain spacing comes from the shared limiter.
SUMMARY_FETCHES_PER_PASS = 120


def sync_directory() -> None:
    with SessionLocal() as db:
        n = companies.sync_directory(db)
        for t in settings.warm_ticker_list:
            c = db.execute(select(Company).where(Company.ticker == t)).scalars().first()
            if c is not None and not c.warm:
                c.warm = True
        db.commit()
        logger.info("Directory: %d companies added or updated.", n)


# The cold start, the EDGAR poll and the 6-hour rebuild can all ask for the
# same company at once; one refresh at a time keeps their writes from racing.
_earnings_lock = threading.Lock()


def refresh_earnings(company_ids: list[int] | None = None) -> None:
    with _earnings_lock:
        _refresh_earnings(company_ids)


def _refresh_earnings(company_ids: list[int] | None) -> None:
    today = datetime.now(timezone.utc).date()
    with SessionLocal() as db:
        stmt = select(Company).where(Company.warm.is_(True))
        if company_ids is not None:
            stmt = stmt.where(Company.id.in_(company_ids))
        for c in db.execute(stmt.order_by(Company.earnings_refreshed_at.asc().nullsfirst())).scalars().all():
            try:
                earnings.refresh_company(db, c.ticker, c.cik, today=today, predecessor_cik=c.predecessor_cik)
                c.earnings_refreshed_at = datetime.now(timezone.utc)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Earnings refresh failed for %s", c.ticker)


def cold_start() -> None:
    """Warm companies never refreshed: earnings first (SEC is fast), news follows in rotation."""
    with SessionLocal() as db:
        ids = list(db.execute(select(Company.id).where(Company.warm.is_(True),
                                                       Company.earnings_refreshed_at.is_(None))).scalars())
    if ids:
        refresh_earnings(ids)


def poll_edgar() -> None:
    """Rebuild a warm company's earnings the minute it files an 8-K."""
    filed = {cik for cik, form in sec.current_filings("8-K") if form.startswith("8-K")}
    if not filed:
        return
    with SessionLocal() as db:
        ids = list(db.execute(select(Company.id).where(Company.warm.is_(True), Company.cik.in_(filed))).scalars())
    if ids:
        refresh_earnings(ids)


def poll_feeds() -> None:
    with SessionLocal() as db:
        n = news.ingest_feeds(db)
    logger.info("Feeds: %d new items.", n)


# The GKG stream carries every company's news continuously; per-company
# searches backfill a newly warm company and top up the rest every few hours.
COMPANY_SEARCH_REFRESH_HOURS = 3


def gkg_stream() -> None:
    with SessionLocal() as db:
        try:
            n = news.ingest_gkg(db)
        except Exception:
            db.rollback()
            logger.exception("GDELT GKG ingest failed")
            return
    logger.info("GKG: %d articles linked to warm companies.", n)


def rotate_company_news() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COMPANY_SEARCH_REFRESH_HOURS)
    with SessionLocal() as db:
        due = db.execute(
            select(Company).where(Company.warm.is_(True),
                                  (Company.news_refreshed_at.is_(None)) | (Company.news_refreshed_at < cutoff))
            .order_by(Company.news_refreshed_at.asc().nullsfirst()).limit(GDELT_COMPANIES_PER_MINUTE)
        ).scalars().all()
        for c in due:
            since = c.news_refreshed_at - timedelta(minutes=30) if c.news_refreshed_at else None
            try:
                news.ingest_company_gdelt(db, c, since=since)
            except gdelt.GdeltUnavailable:
                db.rollback()
                logger.info("GDELT unavailable; company news resumes next minute at %s.", c.ticker)
                return
            except Exception:
                db.rollback()
                logger.exception("GDELT pass failed for %s", c.ticker)


def rating_sweep() -> None:
    """Daily per company; companies never swept go first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    with SessionLocal() as db:
        due = db.execute(
            select(Company).where(Company.warm.is_(True),
                                  (Company.ratings_refreshed_at.is_(None)) | (Company.ratings_refreshed_at < cutoff))
            .order_by(Company.ratings_refreshed_at.asc().nullsfirst()).limit(4)
        ).scalars().all()
        for c in due:
            try:
                news.ingest_company_ratings(db, c)
            except gdelt.GdeltUnavailable:
                db.rollback()
                return
            except Exception:
                db.rollback()
                logger.exception("Rating sweep failed for %s", c.ticker)


IR_COMPANIES_PER_PASS = 6


def ir_calendars() -> None:
    """Company IR event calendars, each company about twice a day; never-checked companies first."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=earnings_dates.IR_REFRESH_HOURS)
    calendars = ir_events.IrCalendars()
    # No earnings lock: discovery waits on IR hosts for minutes, and the
    # earnings refresh re-reads a row before overwriting a confirmed date.
    with SessionLocal() as db:
        due = db.execute(
            select(Company).where(Company.warm.is_(True),
                                  (Company.ir_events_refreshed_at.is_(None)) | (Company.ir_events_refreshed_at < cutoff))
            .order_by(Company.ir_events_refreshed_at.asc().nullsfirst()).limit(IR_COMPANIES_PER_PASS)
        ).scalars().all()
        for c in due:
            try:
                earnings_dates.refresh_ir_calendar(db, c, calendars)
            except Exception:
                db.rollback()
                logger.exception("IR calendar failed for %s", c.ticker)


def edgar_announcements() -> None:
    with SessionLocal() as db:
        n = earnings_dates.edgar_announcements(db)
    logger.info("8-K announcements: %d dates confirmed.", n)


def summaries() -> None:
    with SessionLocal() as db:
        n = news.fill_summaries(db, budget=SUMMARY_FETCHES_PER_PASS)
    logger.info("Summaries: %d articles described.", n)


def market_news() -> None:
    with SessionLocal() as db:
        for category in ("general", "forex", "crypto", "merger"):
            try:
                news.ingest_market_gdelt(db, category)
            except gdelt.GdeltUnavailable:
                db.rollback()
                return
            except Exception:
                db.rollback()
                logger.exception("Market news failed for %s", category)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with SessionLocal() as db:
        empty = db.execute(select(func.count(Company.id))).scalar() == 0
    if empty or settings.warm_ticker_list:
        sync_directory()

    sched = BlockingScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    now = datetime.now(timezone.utc)
    sched.add_job(cold_start, "interval", seconds=30, next_run_time=now)
    sched.add_job(poll_edgar, "interval", seconds=60, next_run_time=now + timedelta(seconds=20))
    sched.add_job(poll_feeds, "interval", minutes=2, next_run_time=now + timedelta(seconds=10))
    sched.add_job(gkg_stream, "interval", minutes=5, next_run_time=now + timedelta(seconds=15))
    sched.add_job(rotate_company_news, "interval", minutes=1, next_run_time=now + timedelta(seconds=40))
    sched.add_job(market_news, "interval", minutes=10, next_run_time=now + timedelta(minutes=1))
    sched.add_job(summaries, "interval", minutes=1, next_run_time=now + timedelta(seconds=50))
    sched.add_job(rating_sweep, "interval", minutes=2, next_run_time=now + timedelta(seconds=70))
    sched.add_job(ir_calendars, "interval", minutes=2, next_run_time=now + timedelta(seconds=45))
    sched.add_job(edgar_announcements, "interval", minutes=30, next_run_time=now + timedelta(minutes=2))
    sched.add_job(refresh_earnings, "interval", hours=6, next_run_time=now + timedelta(hours=6))
    sched.add_job(sync_directory, "cron", hour=6, minute=15)
    sched.start()


if __name__ == "__main__":
    main()
