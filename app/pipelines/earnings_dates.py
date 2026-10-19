"""Confirmed earnings dates: what the company itself says, replacing projections.

A projected date (same quarter last year plus 52 weeks) is often a few days
off. Finnhub's upcoming dates are company-confirmed, so yarn confirms them from
the company's own statements, in order of authority:

  ir-calendar:q4      the event on the company's investor-relations calendar (Q4 feed)
  ir-calendar:page    the same, read from the IR site's events page
  8-k-announcement    a date announced in an 8-K (Items 7.01 / 8.01)
  newswire            the company's press release on a wire
  news                a news article reporting the announcement

A source never overrides a more authoritative one, and nothing overrides a
reported quarter.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone

from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Company, EarningsEvent
from app.pipelines.earnings import _next_fiscal, read_filing
from app.sources import ir_events, sec

logger = logging.getLogger(__name__)

SOURCE_RANK = {"projection": 0, "news": 1, "newswire": 2, "8-k-announcement": 3,
               "ir-calendar:page": 4, "ir-calendar:q4": 5}
MATCH_WINDOW_DAYS = 45
IR_REDISCOVER_DAYS = 30
IR_REFRESH_HOURS = 12


def call_hour(call_et: datetime) -> str:
    """bmo / amc from an earnings call's Eastern start time; "" when the call says nothing.

    A release precedes its call. A morning call follows a pre-market release;
    a call at or after the close follows an after-market release. A call in
    the middle of the day does not say which, so it leaves the hour alone.
    """
    t = call_et.time()
    if t < time(12, 0):
        return "bmo"
    if t >= time(16, 0):
        return "amc"
    return ""


def confirm_date(db: Session, ticker: str, when: date, hour: str, source: str, *, today: date | None = None) -> bool:
    """Set an upcoming quarter's date from a company statement. Returns True when a row changed."""
    today = today or datetime.now(timezone.utc).date()
    if when < today - timedelta(days=1):
        return False
    rows = db.execute(select(EarningsEvent).where(EarningsEvent.ticker == ticker)).scalars().all()
    upcoming = [r for r in rows if r.date_status != "reported" and abs((r.date - when).days) <= MATCH_WINDOW_DAYS]
    if any(r.date_status == "reported" and abs((r.date - when).days) <= 20 for r in rows):
        # The statement describes a quarter already reported (a stale article).
        return False
    if upcoming:
        row = min(upcoming, key=lambda r: abs((r.date - when).days))
    else:
        # No projection to confirm (no year-ago quarter on file): the quarter
        # after the latest reported one, when the date fits a quarterly cadence.
        reported = [r for r in rows if r.date_status == "reported" and r.date < when]
        if not reported:
            return False
        last = max(reported, key=lambda r: r.date)
        if not 45 <= (when - last.date).days <= 140:
            return False
        fy, fq = _next_fiscal(last.fiscal_year, last.fiscal_quarter)
        if any((r.fiscal_year, r.fiscal_quarter) == (fy, fq) for r in rows):
            return False
        row = EarningsEvent(ticker=ticker, fiscal_year=fy, fiscal_quarter=fq, date=when, hour="",
                            date_status="projected", date_source="projection")
        db.add(row)
        # The session does not autoflush; a second announcement in the same pass must see this row.
        db.flush()
    current = SOURCE_RANK.get(row.date_source or ("projection" if row.date_status == "projected" else "news"), 0)
    if SOURCE_RANK.get(source, 0) < current:
        return False
    changed = (row.date, row.date_status, row.date_source) != (when, "confirmed", source) or (hour and row.hour != hour)
    row.date, row.date_status, row.date_source = when, "confirmed", source
    if hour:
        row.hour = hour
    return bool(changed)


def _latest_release_text(company: Company) -> str:
    submissions = sec.fetch_submissions(company.cik)
    if submissions is None:
        return ""
    releases = [f for f in sec.recent_filings(submissions) if f.form == "8-K" and "2.02" in f.items]
    if not releases and company.predecessor_cik:
        old = sec.fetch_submissions(company.predecessor_cik)
        releases = [f for f in sec.recent_filings(old or {}) if f.form == "8-K" and "2.02" in f.items]
    if not releases:
        return ""
    return read_filing(releases[0]).text or ""


def refresh_ir_calendar(db: Session, company: Company, calendars: ir_events.IrCalendars,
                        *, now: datetime | None = None) -> int:
    """Discover the company's IR feed when due, then confirm dates from its upcoming earnings events."""
    now = now or datetime.now(timezone.utc)
    override = settings.ir_calendar_override_map.get(company.ticker)
    if override and (company.ir_platform, company.ir_host) != override:
        company.ir_platform, company.ir_host = override
        company.ir_checked_at = now
    if company.ir_checked_at is None or company.ir_checked_at < now - timedelta(days=IR_REDISCOVER_DAYS):
        found = override or calendars.discover(_latest_release_text(company))
        company.ir_platform, company.ir_host = found if found else ("none", None)
        company.ir_checked_at = now
        db.commit()
    company.ir_events_refreshed_at = now
    if company.ir_platform not in ("q4", "page") or not company.ir_host:
        db.commit()
        return 0
    if company.ir_platform == "q4":
        events = calendars.q4_events(company.ir_host) or []
    else:
        events = calendars.page_events(company.ir_host)
    source = f"ir-calendar:{company.ir_platform}"
    confirmed = 0
    for ev in events:
        if ev.start_et.date() < now.date():
            continue
        if confirm_date(db, company.ticker, ev.start_et.date(), call_hour(ev.start_et), source, today=now.date()):
            confirmed += 1
    db.commit()
    return confirmed


# ---- 8-K announcements ------------------------------------------------------------------

_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
_FTS_PHRASES = ('"conference call"', '"earnings call"', '"will report"', '"to report"', '"will release"')


def edgar_announcements(db: Session, *, days: int = 3, today: date | None = None) -> int:
    """Dates announced in recent 8-Ks (Items 7.01 / 8.01) of warm companies."""
    from app.pipelines.news import parse_announcement

    today = today or datetime.now(timezone.utc).date()
    companies = {c.cik: c for c in db.execute(select(Company).where(Company.warm.is_(True))).scalars()}
    if not companies:
        return 0
    ciks = list(companies)
    seen: set[str] = set()
    confirmed = 0
    for i in range(0, len(ciks), 100):
        for phrase in _FTS_PHRASES:
            resp = sec._get(f"{_FTS_URL}?q={quote(phrase)}&forms=8-K&dateRange=custom"
                            f"&startdt={(today - timedelta(days=days)).isoformat()}&enddt={today.isoformat()}"
                            f"&ciks={','.join(ciks[i:i + 100])}")
            if resp is None:
                continue
            for hit in resp.json().get("hits", {}).get("hits", []):
                src = hit.get("_source", {})
                items = src.get("items") or []
                if "2.02" in items or not ({"7.01", "8.01"} & set(items)):
                    continue
                doc_id = hit.get("_id", "")
                if doc_id in seen or ":" not in doc_id:
                    continue
                seen.add(doc_id)
                accession, filename = doc_id.split(":", 1)
                cik = next((c for c in src.get("ciks", []) if c.zfill(10) in companies), None)
                if cik is None:
                    continue
                url = f"{sec.ARCHIVES_BASE}/{int(cik)}/{accession.replace('-', '')}/{filename}"
                page = sec.fetch_html(url)
                if not page:
                    continue
                found = parse_announcement(sec.html_to_text(page), today=today)
                if found and confirm_date(db, companies[cik.zfill(10)].ticker, found[0], found[1],
                                          "8-k-announcement", today=today):
                    confirmed += 1
    db.commit()
    return confirmed
