"""Earnings events for one company, from SEC filings alone.

Past events
  date and hour   the 8-K carrying Item 2.02 (results of operations). Its
                  Eastern acceptance time, from the filing's SEC header
                  (sec.filing_header), decides bmo / amc / dmh. A release filed after 17:30 ET is
                  dated the next business day by EDGAR and published before
                  that day's open, so it is bmo on its filing date.
  quarter, year   the fiscal period of the 10-Q / 10-K that reports it, from
                  XBRL; before that filing exists, the period after the last
                  one filed.
  actuals         the press release (press_release.parse), same day; checked
                  against XBRL once the 10-Q lands, and filled from XBRL when
                  the release could not be read.

Upcoming events
  date            the same fiscal quarter's report date a year earlier, plus
                  363 days (Finnhub's own convention, see PROJECTION_DAYS):
                  `dateStatus: "projected"`, until the company confirms it.

Estimates (licensed at origin; see docs/audits/estimates-and-recommendations.md)
  guidance        the midpoint the company itself gave for the quarter
  model           otherwise, a seasonal estimate from reported history
"""

import json
import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EarningsEvent
from app.pipelines import press_release
from app.sources import sec

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 800
PROJECT_QUARTERS = 2

_REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenuesNetOfInterestExpense",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
_EPS_CONCEPT = "EarningsPerShareDiluted"

_NON_RESULTS = re.compile(r"(?i)production,? (?:and |& )?deliver|deliveries (?:and|&) deployments|vehicle deliveries")

# "Third-Quarter 2026 Results", "3Q 2026 Earnings Release", "Q2 FY2027": the
# release names its own period, which places it when XBRL cannot (a new
# registrant after a reorganisation has no filing history under its CIK).
_ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_RELEASE_PERIOD = re.compile(
    r"(?i)\b(?:(?P<word>first|second|third|fourth)[- ]quarter(?:\s+(?:of\s+)?(?:fiscal\s+(?:year\s+)?)?(?P<y1>\d{4}))?"
    r"|(?:fiscal\s+(?:year\s+)?)?(?P<y2>\d{4})\s+(?P<word2>first|second|third|fourth)[- ]quarter"
    r"|(?P<n1>[1-4])Q\s?(?:FY\s?)?(?P<y3>\d{4})|Q(?P<n2>[1-4])\s?(?:FY\s?)?(?P<y4>\d{4}))"
)


def release_period(text: str) -> tuple[int, int] | None:
    """(fiscal year, quarter) a release says it reports, from its opening lines."""
    for m in _RELEASE_PERIOD.finditer(text[:1500]):
        quarter = _ORDINAL.get((m.group("word") or m.group("word2") or "").lower()) or int(m.group("n1") or m.group("n2") or 0)
        year = m.group("y1") or m.group("y2") or m.group("y3") or m.group("y4")
        if quarter and year:
            return int(year), quarter
    return None


_OPEN = time(9, 30)
_CLOSE = time(16, 0)
_EDGAR_CUTOFF = time(17, 30)


def report_hour(accepted_et: datetime) -> tuple[date, str]:
    """(report date, Finnhub hour code) from an 8-K's Eastern acceptance time."""
    t = accepted_et.time()
    d = accepted_et.date()
    if t >= _EDGAR_CUTOFF:
        nxt = d + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt, "bmo"
    if t < _OPEN:
        return d, "bmo"
    if t >= _CLOSE:
        return d, "amc"
    return d, "dmh"


@dataclass
class Period:
    end: date
    fy: int
    fq: int
    revenue: int | None = None
    eps: float | None = None


@dataclass
class XbrlHistory:
    periods: dict[date, Period] = field(default_factory=dict)

    def by_fiscal(self, fy: int, fq: int) -> Period | None:
        return next((p for p in self.periods.values() if p.fy == fy and p.fq == fq), None)

    def latest(self) -> Period | None:
        return max(self.periods.values(), key=lambda p: p.end, default=None)


def _duration_days(fact: dict) -> int | None:
    if not fact.get("start") or not fact.get("end"):
        return None
    return (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days


def xbrl_history(facts: dict) -> XbrlHistory:
    """Fiscal periods with quarterly revenue and diluted EPS, from companyfacts.

    A filing's `fy`/`fp` describe the filing, not each fact, so a period's
    fiscal label is taken from the 10-Q/10-K whose own period ends there: the
    latest `end` among that filing's facts.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    hist = XbrlHistory()

    filings: dict[str, tuple[date, int, str]] = {}
    for concept in (_EPS_CONCEPT, *_REVENUE_CONCEPTS):
        for unit_facts in gaap.get(concept, {}).get("units", {}).values():
            for f in unit_facts:
                if f.get("form") not in ("10-Q", "10-K") or not f.get("fy") or f.get("fp") not in ("Q1", "Q2", "Q3", "FY"):
                    continue
                end = date.fromisoformat(f["end"])
                prev = filings.get(f["accn"])
                if prev is None or end > prev[0]:
                    filings[f["accn"]] = (end, int(f["fy"]), f["fp"])
    for end, fy, fp in filings.values():
        hist.periods.setdefault(end, Period(end=end, fy=fy, fq=4 if fp == "FY" else int(fp[1])))

    def quarterly(concept: str) -> dict[date, float]:
        out: dict[date, float] = {}
        annual: dict[date, tuple[date, float]] = {}
        nine: dict[date, float] = {}
        for unit, unit_facts in gaap.get(concept, {}).get("units", {}).items():
            for f in unit_facts:
                days = _duration_days(f)
                if days is None:
                    continue
                end = date.fromisoformat(f["end"])
                if 80 <= days <= 100:
                    out.setdefault(end, f["val"])
                elif 350 <= days <= 380:
                    annual.setdefault(end, (date.fromisoformat(f["start"]), f["val"]))
                elif 260 <= days <= 285:
                    nine.setdefault(end, f["val"])
        # Q4 is reported only inside the annual figure: FY minus the nine months.
        for end, (start, fy_val) in annual.items():
            if end in out:
                continue
            nine_end = max((e for e in nine if start < e < end), default=None)
            if nine_end is not None:
                out[end] = fy_val - nine[nine_end]
        return out

    revenue: dict[date, float] = {}
    for concept in _REVENUE_CONCEPTS:
        for end, val in quarterly(concept).items():
            revenue.setdefault(end, val)
    eps = quarterly(_EPS_CONCEPT)
    for end, p in hist.periods.items():
        if end in revenue:
            p.revenue = int(revenue[end])
        if end in eps:
            p.eps = round(float(eps[end]), 4)
    return hist


# Finnhub's convention for unconfirmed dates, measured on 2026-10-19: of 35
# upcoming dates no company had yet announced, 25 were exactly the same
# quarter's report date a year earlier plus 363 days (6 were +364). yarn uses
# the same rule so an unconfirmed date matches Finnhub's; any company
# confirmation (pipelines/earnings_dates.py) replaces it.
PROJECTION_DAYS = 363


def project_date(year_ago: date) -> date:
    projected = year_ago + timedelta(days=PROJECTION_DAYS)
    if projected.weekday() == 5:
        projected -= timedelta(days=1)
    elif projected.weekday() == 6:
        projected += timedelta(days=1)
    return projected


def _next_fiscal(fy: int, fq: int) -> tuple[int, int]:
    return (fy + 1, 1) if fq == 4 else (fy, fq + 1)


def _plausible_revenue(value: int | None, reference: int | None) -> int | None:
    """Reject a release figure that cannot be the consolidated total (a segment, a guidance range)."""
    if value is None or not reference:
        return value
    ratio = value / reference
    return value if 0.6 <= ratio <= 1.8 else None


def _model_estimate(hist: XbrlHistory, fy: int, fq: int, attr: str) -> float | None:
    """Seasonal estimate: the same quarter last year, moved by the recent year-over-year change."""
    last_year = hist.by_fiscal(fy - 1, fq)
    if last_year is None or getattr(last_year, attr) is None:
        return None
    base = getattr(last_year, attr)
    changes = []
    p_fy, p_fq = fy, fq
    for _ in range(4):
        p_fq -= 1
        if p_fq == 0:
            p_fy, p_fq = p_fy - 1, 4
        cur, prior = hist.by_fiscal(p_fy, p_fq), hist.by_fiscal(p_fy - 1, p_fq)
        if cur and prior and getattr(cur, attr) is not None and getattr(prior, attr) is not None:
            changes.append((getattr(cur, attr), getattr(prior, attr)))
        if len(changes) == 2:
            break
    if not changes:
        return base
    if attr == "revenue":
        growth = statistics.median(c / p - 1 for c, p in changes if p)
        return base * (1 + max(-0.5, min(1.0, growth)))
    return base + statistics.median(c - p for c, p in changes)


@dataclass
class BuiltEvent:
    fiscal_year: int
    fiscal_quarter: int
    date: date
    hour: str
    date_status: str
    eps_actual: float | None = None
    eps_actual_gaap: float | None = None
    eps_basis: str | None = None
    revenue_actual: int | None = None
    eps_estimate: float | None = None
    revenue_estimate: int | None = None
    estimate_basis: str | None = None
    accession: str | None = None
    source_url: str | None = None
    release_parsed: bool = False


@dataclass
class ReadFiling:
    """What one 8-K said: its Eastern acceptance time and exhibit text.

    `text` is None when the release was deliberately not re-read (its figures
    are already stored), as opposed to "" for a release with no readable text.
    """

    accepted_et: datetime | None
    text: str | None


def build_events(submissions: dict, facts: dict | None, *, today: date,
                 read_filing=None, known: dict[str, tuple[date, str]] | None = None) -> list[BuiltEvent]:
    """Every earnings event for one company. Pure apart from `read_filing`.

    `read_filing(filing) -> ReadFiling` fetches the SEC header and exhibits;
    injected so tests run from recorded documents. `known` maps accessions
    already stored to their (date, hour), so they are not fetched again.
    """
    known = known or {}
    skipped: set[str] = set()
    hist = xbrl_history(facts) if facts else XbrlHistory()
    filings = sec.recent_filings(submissions)
    cutoff = today - timedelta(days=LOOKBACK_DAYS)
    releases = sorted(
        (f for f in filings if f.form == "8-K" and "2.02" in f.items and date.fromisoformat(f.filing_date) >= cutoff),
        key=lambda f: (f.filing_date, f.accession),
    )
    period_ends = sorted(hist.periods)
    events: dict[tuple[int, int], BuiltEvent] = {}
    guidance: dict[tuple[int, int], tuple[int | None, float | None]] = {}

    for filing in releases:
        if filing.accession in known:
            read = ReadFiling(accepted_et=None, text=None)
            report_date, hour = known[filing.accession]
        else:
            read = read_filing(filing) if read_filing else ReadFiling(accepted_et=None, text=None)
            if read_filing and read.text is None:
                # The fetch failed; leave this quarter as stored and try again next pass.
                skipped.add(filing.accession)
                continue
            if read.accepted_et is not None:
                report_date, hour = report_hour(read.accepted_et)
            else:
                report_date, hour = date.fromisoformat(filing.filing_date), ""
        figures = press_release.parse(read.text) if read.text else press_release.ReleaseFigures()
        if (read.text and figures.revenue is None and figures.eps_gaap is None and figures.eps_adjusted is None
                and _NON_RESULTS.search(read.text[:600])):
            # Item 2.02 also carries non-results updates (Tesla files its
            # quarterly production and deliveries under it).
            continue
        ended = [e for e in period_ends if e < report_date and (report_date - e).days <= 120]
        if ended:
            period = hist.periods[ended[-1]]
            fy, fq = period.fy, period.fq
        else:
            known = [hist.periods[e] for e in period_ends if e < report_date]
            stated = release_period(read.text) if read.text else None
            if stated is not None:
                fy, fq = stated
            elif known:
                fy, fq = _next_fiscal(known[-1].fy, known[-1].fq)
            else:
                continue
            period = None
        if (fy, fq) in events:
            if (report_date - events[(fy, fq)].date).days < 30:
                # An amended or second 2.02 for the same quarter: the first is the release.
                continue
            # The quarter it reports has no 10-Q yet, so XBRL still points at
            # the previous one: this release is the quarter after the latest.
            latest = max(events.values(), key=lambda e: e.date)
            fy, fq = _next_fiscal(latest.fiscal_year, latest.fiscal_quarter)
            period = None

        ev = BuiltEvent(fiscal_year=fy, fiscal_quarter=fq, date=report_date, hour=hour,
                        date_status="reported", accession=filing.accession, source_url=filing.index_url,
                        release_parsed=bool(figures.revenue or figures.eps_gaap is not None
                                            or figures.eps_adjusted is not None))
        prior = hist.by_fiscal(fy - 1, fq)
        ev.revenue_actual = _plausible_revenue(figures.revenue, prior.revenue if prior else None)
        ev.eps_actual_gaap = figures.eps_gaap
        if period is not None:
            if ev.revenue_actual is None and period.revenue is not None:
                ev.revenue_actual = period.revenue
            if ev.eps_actual_gaap is None:
                ev.eps_actual_gaap = period.eps
        if figures.eps_adjusted is not None:
            ev.eps_actual, ev.eps_basis = figures.eps_adjusted, "adjusted"
        elif ev.eps_actual_gaap is not None:
            ev.eps_actual, ev.eps_basis = ev.eps_actual_gaap, "gaap"
        events[(fy, fq)] = ev
        guidance[_next_fiscal(fy, fq)] = (figures.revenue_guidance_mid, figures.eps_guidance_mid)

    # Upcoming quarters, projected from last year's cadence.
    if events:
        last = max(events.values(), key=lambda e: e.date)
        fy, fq = last.fiscal_year, last.fiscal_quarter
        for _ in range(PROJECT_QUARTERS):
            fy, fq = _next_fiscal(fy, fq)
            year_ago = events.get((fy - 1, fq))
            if year_ago is None:
                break
            projected = project_date(year_ago.date)
            if projected < today:
                # The projected day passed with no release on file: no
                # defensible date to offer until the company announces one.
                break
            events[(fy, fq)] = BuiltEvent(fiscal_year=fy, fiscal_quarter=fq, date=projected,
                                          hour=year_ago.hour, date_status="projected")

    # Estimates: what was knowable before the report, on the same basis as
    # `epsActual` (adjusted where the company reports it), so a model estimate
    # and the actual it is compared with measure the same thing.
    model_hist = XbrlHistory(periods={
        e: Period(end=p.end, fy=p.fy, fq=p.fq, revenue=p.revenue, eps=p.eps) for e, p in hist.periods.items()
    })
    for ev in events.values():
        if ev.date_status != "reported":
            continue
        target = model_hist.by_fiscal(ev.fiscal_year, ev.fiscal_quarter)
        if target is None:
            target = Period(end=ev.date, fy=ev.fiscal_year, fq=ev.fiscal_quarter)
            model_hist.periods[ev.date] = target
        if ev.eps_actual is not None:
            target.eps = ev.eps_actual
        if ev.revenue_actual is not None:
            target.revenue = ev.revenue_actual
    for (fy, fq), ev in events.items():
        rev_guide, eps_guide = guidance.get((fy, fq), (None, None))
        if rev_guide is not None or eps_guide is not None:
            ev.revenue_estimate, ev.eps_estimate, ev.estimate_basis = rev_guide, eps_guide, "guidance"
            if ev.revenue_estimate is None:
                rev = _model_estimate(model_hist, fy, fq, "revenue")
                ev.revenue_estimate = int(rev) if rev is not None else None
            if ev.eps_estimate is None:
                eps = _model_estimate(model_hist, fy, fq, "eps")
                ev.eps_estimate = round(eps, 4) if eps is not None else None
            continue
        rev = _model_estimate(model_hist, fy, fq, "revenue")
        eps = _model_estimate(model_hist, fy, fq, "eps")
        if rev is not None or eps is not None:
            ev.revenue_estimate = int(rev) if rev is not None else None
            ev.eps_estimate = round(eps, 4) if eps is not None else None
            ev.estimate_basis = "model"
    return sorted(events.values(), key=lambda e: e.date)


def read_filing(filing: sec.Filing) -> ReadFiling:
    header = sec.filing_header(filing)
    if header is None:
        return ReadFiling(accepted_et=None, text=None)
    pages = [sec.fetch_html(u) for u in header.exhibit_urls]
    if any(p is None for p in pages):
        return ReadFiling(accepted_et=header.accepted_et, text=None)
    return ReadFiling(accepted_et=header.accepted_et, text="\n".join(sec.html_to_text(p) for p in pages))


def _merge_submissions(current: dict, predecessor: dict) -> dict:
    """The predecessor's filings appended to the current registrant's."""
    merged = json.loads(json.dumps(current))
    recent, old = merged["filings"]["recent"], predecessor.get("filings", {}).get("recent", {})
    n_current, n_old = len(recent.get("form", [])), len(old.get("form", []))
    for key in list(recent):
        recent[key] = list(recent[key]) + list(old.get(key, [None] * n_old))
    cik_current = str(current.get("cik", "")).zfill(10)
    cik_old = str(predecessor.get("cik", "")).zfill(10)
    recent["_cik"] = [cik_current] * n_current + [cik_old] * n_old
    return merged


def _merge_facts(current: dict | None, predecessor: dict | None) -> dict | None:
    if not predecessor:
        return current
    if not current:
        return predecessor
    merged = json.loads(json.dumps(current))
    gaap = merged.setdefault("facts", {}).setdefault("us-gaap", {})
    for concept, body in predecessor.get("facts", {}).get("us-gaap", {}).items():
        target = gaap.setdefault(concept, {"units": {}})
        for unit, facts in body.get("units", {}).items():
            target["units"].setdefault(unit, []).extend(facts)
    return merged


def _refreshed(db: Session, row: EarningsEvent) -> EarningsEvent:
    """The row as committed now: a date confirmed by another job since it was read must win."""
    db.refresh(row)
    return row


def refresh_company(db: Session, ticker: str, cik10: str, *, today: date, predecessor_cik: str | None = None) -> int:
    """Rebuild and upsert one company's events. Releases already stored are not re-read.

    After a reorganisation the ticker's history lives under the predecessor's
    CIK, so its filings and facts are read alongside the current registrant's.
    """
    submissions = sec.fetch_submissions(cik10)
    if submissions is None:
        return 0
    facts = sec.fetch_companyfacts(cik10)
    if predecessor_cik:
        old = sec.fetch_submissions(predecessor_cik)
        if old is not None:
            submissions = _merge_submissions(submissions, old)
            facts = _merge_facts(facts, sec.fetch_companyfacts(predecessor_cik))
    stored = [e for e in db.execute(select(EarningsEvent).where(EarningsEvent.ticker == ticker)).scalars()]
    # Releases whose figures are already held are not fetched again.
    known = {e.accession: (e.date, e.hour) for e in stored if e.accession and e.release_parsed}

    built = build_events(submissions, facts, today=today, read_filing=read_filing, known=known)
    existing = {(e.fiscal_year, e.fiscal_quarter): e for e in
                db.execute(select(EarningsEvent).where(EarningsEvent.ticker == ticker)).scalars()}
    for b in built:
        row = existing.get((b.fiscal_year, b.fiscal_quarter))
        confirmed_by = None
        if row is None:
            row = EarningsEvent(ticker=ticker, fiscal_year=b.fiscal_year, fiscal_quarter=b.fiscal_quarter)
            db.add(row)
        elif b.date_status == "projected" and _refreshed(db, row).date_status == "confirmed":
            # A company-announced date outranks a projection.
            b.date, b.hour, b.date_status = row.date, row.hour or b.hour, row.date_status
            confirmed_by = row.date_source
        # A release parsed on an earlier pass is not re-read, so this pass only
        # has XBRL fills for it: the stored release figures stand.
        if b.date_status == "reported" and b.accession in known and row.release_parsed:
            for attr in ("revenue_actual", "eps_actual", "eps_actual_gaap", "eps_basis"):
                if getattr(row, attr, None) is not None:
                    setattr(b, attr, getattr(row, attr))
        else:
            for attr in ("revenue_actual", "eps_actual", "eps_actual_gaap", "eps_basis"):
                if getattr(b, attr) is None and getattr(row, attr, None) is not None and b.date_status == "reported":
                    setattr(b, attr, getattr(row, attr))
        # An estimate is frozen once the quarter is reported.
        if row.eps_estimate is not None and b.date_status == "reported" and row.date_status != "reported":
            b.eps_estimate, b.revenue_estimate, b.estimate_basis = row.eps_estimate, row.revenue_estimate, row.estimate_basis
        if b.date_status == "reported" and b.accession in known:
            b.release_parsed = True
        for attr in ("date", "hour", "date_status", "eps_actual", "eps_actual_gaap", "eps_basis", "revenue_actual",
                     "eps_estimate", "revenue_estimate", "estimate_basis", "accession", "source_url", "release_parsed"):
            setattr(row, attr, getattr(b, attr))
        row.date_source = {"reported": "8-k", "projected": "projection"}.get(b.date_status) or confirmed_by
    return len(built)
