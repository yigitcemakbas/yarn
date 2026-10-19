from datetime import date, datetime

import pytest

from app.pipelines import earnings, press_release
from app.pipelines.news import article_id, canonical_url, clean_title
from app.pipelines.recommendations import bucket_for, parse_headline
from app.sources.sec import ET, acceptance_from_header


@pytest.mark.parametrize("header,expected", [
    # The SEC header carries Eastern time; the submissions API's UTC label is unreliable.
    ("<ACCEPTANCE-DATETIME>20260716160319\n<TYPE>8-K", datetime(2026, 7, 16, 16, 3, 19)),
    ("<SEC-HEADER>\n<ACCEPTANCE-DATETIME>20261013063038", datetime(2026, 10, 13, 6, 30, 38)),
])
def test_acceptance_time_from_sec_header(header, expected):
    assert acceptance_from_header(header).replace(tzinfo=None) == expected
    assert acceptance_from_header("no header") is None


@pytest.mark.parametrize("et,expected", [
    (datetime(2026, 10, 13, 6, 30, tzinfo=ET), (date(2026, 10, 13), "bmo")),
    (datetime(2026, 7, 30, 16, 30, tzinfo=ET), (date(2026, 7, 30), "amc")),
    (datetime(2026, 7, 30, 12, 0, tzinfo=ET), (date(2026, 7, 30), "dmh")),
    # PepsiCo files the evening before a pre-market release: EDGAR dates it the next business day.
    (datetime(2026, 10, 7, 17, 57, tzinfo=ET), (date(2026, 10, 8), "bmo")),
    (datetime(2026, 10, 9, 18, 0, tzinfo=ET), (date(2026, 10, 12), "bmo")),
])
def test_report_hour(et, expected):
    assert earnings.report_hour(et) == expected


@pytest.mark.parametrize("headline,firm,bucket,target", [
    ("Morgan Stanley upgrades Nvidia to Overweight, PT $250", "Morgan Stanley", "buy", 250.0),
    ("Nvidia upgraded to Buy at Goldman Sachs", "Goldman Sachs", "buy", None),
    ("Apple downgraded to Neutral at Rosenblatt; price target $210", "Rosenblatt", "hold", 210.0),
    ("Barclays downgrades Intel to Underweight", "Barclays", "sell", None),
    ("JPMorgan reiterates Overweight rating on Microsoft, raises price target to $600", "JPMorgan", "buy", 600.0),
    ("BofA Securities raises Apple price target to $300, keeps Buy", "BofA", "buy", 300.0),
    ("Bank of America upgrades Apple to Buy", "BofA", "buy", None),
])
def test_rating_headlines(headline, firm, bucket, target):
    parsed = parse_headline(headline)
    assert parsed is not None
    assert (parsed.firm, parsed.bucket, parsed.price_target) == (firm, bucket, target)


@pytest.mark.parametrize("headline", [
    "Apple stock falls after iPhone sales miss",
    "Analysts upgrade outlook for chip stocks",
    "Investors keep buying Apple",
    "Nvidia leads chip rally with buy orders",
])
def test_not_rating_headlines(headline):
    assert parse_headline(headline) is None


def test_buckets():
    assert bucket_for("Strong Buy") == "strongBuy"
    assert bucket_for("Market Perform") == "hold"
    assert bucket_for("Underperform") == "sell"


def test_press_release_exact_revenue_from_headline_and_table():
    text = "\n".join([
        "Reported revenue of $50.2 billion and managed revenue 2 of $50.9 billion",
        "Total net revenue", "$", "50,907", "$", "47,120",
        "Earnings per share", "Diluted", "$", "5.57", "$", "4.85",
    ])
    figures = press_release.parse(text)
    assert figures.revenue == 50_907_000_000
    assert figures.eps_gaap == 5.57


def test_press_release_adjusted_eps_and_footnotes():
    text = "\n".join([
        "Third-Quarter reported sales growth of 5.9% to $25.4 Billion",
        "Strong performance results in the Company increasing 2026 guidance with estimated reported sales of $101.2 Billion",
        "(Dollars in Millions)", "Sales", "$25,399", "$23,993",
        "Adjusted EPS (diluted)", "1,4", "$2.59", "$2.80",
    ])
    figures = press_release.parse(text)
    assert figures.revenue == 25_399_000_000
    assert figures.eps_adjusted == 2.59


def test_adjusted_eps_ignores_trailing_footnote_digit():
    assert press_release.find_adjusted_eps("adjusted EPS of $1.082 for the quarter") == 1.08


def test_q4_is_annual_minus_nine_months():
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"start": "2025-09-28", "end": "2026-06-27", "val": 300, "fy": 2026, "fp": "Q3", "form": "10-Q", "accn": "a3"},
            {"start": "2026-03-29", "end": "2026-06-27", "val": 100, "fy": 2026, "fp": "Q3", "form": "10-Q", "accn": "a3"},
            {"start": "2025-09-28", "end": "2026-09-26", "val": 420, "fy": 2026, "fp": "FY", "form": "10-K", "accn": "k"},
        ]}},
    }}}
    hist = earnings.xbrl_history(facts)
    assert hist.by_fiscal(2026, 4).revenue == 120
    assert hist.by_fiscal(2026, 3).revenue == 100


def test_canonical_urls_and_ids():
    a = canonical_url("https://Example.com/story/?utm_source=x&id=7")
    assert a == "https://example.com/story?id=7"
    assert article_id("https://example.com/story?id=7&utm_medium=y") == article_id("https://example.com/story/?id=7")
    assert article_id("https://example.com/a") > 0


def test_clean_title():
    assert clean_title("Apple $1 , 999 iPhone bet just got its first answer") == "Apple $1,999 iPhone bet just got its first answer"
    assert clean_title("Stock Market Today , Oct . 16 : Apple Edges Higher") == "Stock Market Today, Oct. 16: Apple Edges Higher"


def test_announcement_confirms_projected_date(db):
    from datetime import timedelta

    from app.models import Company, EarningsEvent
    from app.pipelines.news import record_earnings_date

    db.add(Company(ticker="NVDA", cik="0001045810", name="NVIDIA CORP", aliases="nvidia", warm=True))
    today = date.today()
    target = today + timedelta(days=20)
    db.add(EarningsEvent(ticker="NVDA", fiscal_year=2027, fiscal_quarter=3, date=target + timedelta(days=1),
                         hour="", date_status="projected"))
    db.commit()
    record_earnings_date(db, "NVIDIA Announces Upcoming Event for Financial Community",
                         f"NVIDIA will host a conference call on {target:%A}, {target:%B} {target.day}, {target.year} "
                         "at 2 p.m. PT, after the market close, to discuss its results.", ["NVDA"])
    db.commit()
    ev = db.query(EarningsEvent).one()
    assert (ev.date, ev.date_status, ev.hour) == (target, "confirmed", "amc")


def test_build_events_from_recorded_filings():
    """A release read from its header and exhibits becomes a reported quarter with actuals."""
    from app.pipelines.earnings import ReadFiling, build_events

    submissions = {"cik": 320193, "filings": {"recent": {
        "form": ["8-K", "10-Q"], "accessionNumber": ["0000320193-26-000018", "0000320193-26-000020"],
        "filingDate": ["2026-07-30", "2026-08-01"], "reportDate": ["2026-07-30", "2026-06-27"],
        "acceptanceDateTime": ["2026-07-31T00:30:28.000Z", "2026-08-01T22:00:00.000Z"],
        "items": ["2.02,9.01", ""], "primaryDocument": ["a.htm", "q.htm"],
    }}}
    facts = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
        {"start": "2026-03-29", "end": "2026-06-27", "val": 2.02, "fy": 2026, "fp": "Q3", "form": "10-Q", "accn": "q"},
    ]}}}}}
    release = "\n".join(["Apple reports third quarter results", "Total net sales", "$", "109,417",
                         "Earnings per share", "Diluted", "$", "2.02"])

    def read(filing):
        return ReadFiling(accepted_et=datetime(2026, 7, 30, 16, 30, tzinfo=ET), text=release)

    events = build_events(submissions, facts, today=date(2026, 10, 19), read_filing=read)
    q3 = next(e for e in events if e.fiscal_quarter == 3)
    assert (q3.date, q3.hour, q3.fiscal_year, q3.eps_actual, q3.revenue_actual, q3.release_parsed) == (
        date(2026, 7, 30), "amc", 2026, 2.02, 109_417_000_000, True)


def test_second_pass_keeps_release_figures(db, monkeypatch):
    """A parsed release is not re-read, and XBRL's GAAP fill must not overwrite its adjusted EPS."""
    from app.models import EarningsEvent
    from app.pipelines import earnings as e

    submissions = {"cik": 77476, "filings": {"recent": {
        "form": ["8-K"], "accessionNumber": ["0000077476-26-000050"], "filingDate": ["2026-10-08"],
        "reportDate": ["2026-10-08"], "acceptanceDateTime": ["2026-10-08T01:57:44.000Z"],
        "items": ["2.02,9.01"], "primaryDocument": ["a.htm"],
    }}}
    facts = {"facts": {"us-gaap": {"EarningsPerShareDiluted": {"units": {"USD/shares": [
        {"start": "2026-06-14", "end": "2026-09-05", "val": 2.23, "fy": 2026, "fp": "Q3", "form": "10-Q", "accn": "q"},
    ]}}}}}
    release = "Core EPS $2.34\nNet revenue of $25.3 billion\n25,274"
    monkeypatch.setattr(e.sec, "fetch_submissions", lambda cik: submissions)
    monkeypatch.setattr(e.sec, "fetch_companyfacts", lambda cik: facts)
    reads = []
    monkeypatch.setattr(e, "read_filing", lambda f: reads.append(f) or e.ReadFiling(
        accepted_et=datetime(2026, 10, 7, 17, 57, tzinfo=ET), text=release))
    e.refresh_company(db, "PEP", "0000077476", today=date(2026, 10, 19))
    db.commit()
    e.refresh_company(db, "PEP", "0000077476", today=date(2026, 10, 19))
    db.commit()
    row = db.query(EarningsEvent).filter_by(ticker="PEP", fiscal_quarter=3).one()
    assert (row.eps_actual, row.eps_basis, row.date) == (2.34, "adjusted", date(2026, 10, 8))
    assert len(reads) == 1


def test_projection_matches_finnhub_convention():
    # NVIDIA reported FQ3 on 2025-11-19; Finnhub's unconfirmed date for FQ3 a year on was 2026-11-17.
    assert earnings.project_date(date(2025, 11, 19)) == date(2026, 11, 17)
    assert earnings.project_date(date(2026, 1, 29)) == date(2027, 1, 27)
    # Never a weekend.
    assert earnings.project_date(date(2025, 10, 26)).weekday() < 5
