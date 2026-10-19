"""Confirmed earnings dates: IR calendars, 8-K announcements, newswires and news."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.models import Company, EarningsEvent
from app.pipelines import earnings_dates
from app.pipelines.earnings_dates import call_hour, confirm_date
from app.pipelines.news import parse_announcement, record_earnings_date, store_article
from app.sources import ir_events
from app.sources.ir_events import ET, candidate_hosts, ir_links, is_earnings_event, parse_q4_events, parse_q4_time

FIXTURES = Path(__file__).parent.parent / "fixtures"
TODAY = date(2026, 10, 19)


def _event(db, ticker, fy, fq, when, status, hour="", source=None):
    db.add(EarningsEvent(ticker=ticker, fiscal_year=fy, fiscal_quarter=fq, date=when, hour=hour,
                         date_status=status, date_source=source))
    db.commit()


# ---- IR calendars (Q4) ---------------------------------------------------------------------


def test_q4_fixtures_parse_to_eastern_calls():
    apple = parse_q4_events((FIXTURES / "q4_events_apple.json").read_text())
    cat = parse_q4_events((FIXTURES / "q4_events_caterpillar.json").read_text())
    assert [(e.start_et.replace(tzinfo=None), call_hour(e.start_et)) for e in apple] == [(datetime(2026, 11, 2, 17, 0), "amc")]
    assert [(e.start_et.replace(tzinfo=None), call_hour(e.start_et)) for e in cat] == [(datetime(2026, 10, 29, 8, 30), "bmo")]


@pytest.mark.parametrize("start,zone,expected", [
    ("10/21/2026 13:30:00", "PST", datetime(2026, 10, 21, 16, 30)),  # Q4 writes PST all year
    ("10/27/2026 10:30:00", "EST", datetime(2026, 10, 27, 10, 30)),
    ("01/28/2027 10:00:00", "CT", datetime(2027, 1, 28, 11, 0)),
    ("10/20/2026 20:45:00", "UTC", datetime(2026, 10, 20, 16, 45)),
])
def test_q4_time_zones(start, zone, expected):
    assert parse_q4_time(start, zone).replace(tzinfo=None) == expected


@pytest.mark.parametrize("title,expected", [
    ("Q3 2026 Earnings Call", True),
    ("FY 26 Fourth Quarter Results", True),
    ("3Q 2026 Caterpillar Inc. Earnings Conference Call", True),
    ("Netflix Third Quarter 2026 Earnings Interview", True),
    ("Goldman Sachs Communacopia + Technology Conference", False),
    ("2027 Annual Meeting of Shareholders", False),
    ("Investor Day 2026", False),
])
def test_earnings_event_titles(title, expected):
    assert is_earnings_event(title) is expected


def test_not_a_q4_feed():
    assert parse_q4_events("<html>Access Denied</html>") is None
    assert parse_q4_events('{"something": []}') is None


def test_candidate_hosts_from_release_text():
    text = ("Apple will provide live streaming of its Q4 2026 financial results conference call at "
            "https://investor.apple.com/investor-relations/default.aspx. Press contact: media.help@apple.com")
    assert candidate_hosts(text)[0] == "investor.apple.com"
    boeing = "For more information, visit www.boeing.com. Investor Relations: Eric Hill (312) 544-2140"
    assert candidate_hosts(boeing)[:4] == ["investor.boeing.com", "investors.boeing.com", "ir.boeing.com",
                                           "shareholder.boeing.com"]
    assert "abc.xyz" in candidate_hosts("Alphabet's investor relations site is at abc.xyz/investor")
    assert ir_links("visit www.amazon.com/ir for the webcast") == ["https://www.amazon.com/ir"]
    assert not any("sec.gov" in h for h in candidate_hosts("filed at www.sec.gov and www.prnewswire.com"))


def test_refresh_ir_calendar_discovers_and_confirms(db, monkeypatch):
    db.add(Company(ticker="CAT", cik="0000018230", name="Caterpillar Inc", aliases="caterpillar", warm=True))
    _event(db, "CAT", 2026, 2, date(2026, 8, 5), "reported", "bmo", "8-k")
    _event(db, "CAT", 2026, 3, date(2026, 10, 28), "projected", "bmo", "projection")
    payload = (FIXTURES / "q4_events_caterpillar.json").read_text()

    class FakeCalendars(ir_events.IrCalendars):
        def fetch_html(self, url):
            return payload if url.startswith("https://investors.caterpillar.com/feed/") else None

    monkeypatch.setattr(earnings_dates, "_latest_release_text", lambda c: "Caterpillar news at caterpillar.com")
    company = db.query(Company).one()
    assert earnings_dates.refresh_ir_calendar(db, company, FakeCalendars(), now=datetime(2026, 10, 19, 12)) == 1
    ev = db.query(EarningsEvent).filter_by(fiscal_quarter=3).one()
    assert (ev.date, ev.hour, ev.date_status, ev.date_source) == (date(2026, 10, 29), "bmo", "confirmed", "ir-calendar:q4")
    assert (company.ir_host, company.ir_platform) == ("investors.caterpillar.com", "q4")


# ---- confirm_date ------------------------------------------------------------------------------


def test_call_hours():
    assert call_hour(datetime(2026, 10, 29, 8, 30, tzinfo=ET)) == "bmo"
    assert call_hour(datetime(2026, 11, 2, 17, 0, tzinfo=ET)) == "amc"
    assert call_hour(datetime(2026, 10, 29, 13, 0, tzinfo=ET)) == ""


def test_less_authoritative_source_does_not_override(db):
    _event(db, "AAPL", 2026, 4, date(2026, 11, 2), "confirmed", "amc", "ir-calendar:q4")
    assert confirm_date(db, "AAPL", date(2026, 10, 30), "", "news", today=TODAY) is False
    assert db.query(EarningsEvent).one().date == date(2026, 11, 2)


def test_more_authoritative_source_overrides(db):
    _event(db, "AAPL", 2026, 4, date(2026, 10, 30), "confirmed", "", "news")
    assert confirm_date(db, "AAPL", date(2026, 11, 2), "amc", "ir-calendar:q4", today=TODAY) is True
    ev = db.query(EarningsEvent).one()
    assert (ev.date, ev.hour, ev.date_source) == (date(2026, 11, 2), "amc", "ir-calendar:q4")


def test_reported_quarter_is_never_touched(db):
    _event(db, "JPM", 2026, 3, date(2026, 10, 13), "reported", "bmo", "8-k")
    assert confirm_date(db, "JPM", date(2026, 10, 14), "bmo", "ir-calendar:q4", today=date(2026, 10, 12)) is False
    assert db.query(EarningsEvent).one().date == date(2026, 10, 13)


def test_confirmation_creates_next_quarter_without_projection(db):
    _event(db, "XOM", 2026, 2, date(2026, 7, 31), "reported", "bmo", "8-k")
    assert confirm_date(db, "XOM", date(2026, 10, 30), "bmo", "newswire", today=TODAY) is True
    ev = db.query(EarningsEvent).filter_by(fiscal_quarter=3).one()
    assert (ev.fiscal_year, ev.date, ev.date_status) == (2026, date(2026, 10, 30), "confirmed")


def test_past_dates_are_ignored(db):
    _event(db, "KO", 2026, 3, date(2026, 10, 20), "projected")
    assert confirm_date(db, "KO", date(2026, 10, 1), "", "news", today=TODAY) is False


# ---- newswire / news / 8-K text ------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Microsoft to announce fiscal 2027 first-quarter results on October 28", (date(2026, 10, 28), "")),
    ("Ford will report its third-quarter 2026 results on Wednesday, Oct. 28, after the U.S. stock market closes.",
     (date(2026, 10, 28), "amc")),
    ("Chevron will hold its third quarter 2026 earnings conference call on Friday, October 30, 2026, "
     "before the market opens.", (date(2026, 10, 30), "bmo")),
    ("Tesla will post its financial results for the third quarter of 2026 after market close on Wednesday, "
     "October 21, 2026.", (date(2026, 10, 21), "amc")),
    ("Shares rose 3% on Tuesday as investors cheered the results.", None),
])
def test_parse_announcement(text, expected):
    assert parse_announcement(text, today=TODAY) == expected


def test_newswire_release_confirms_with_newswire_source(db):
    db.add(Company(ticker="F", cik="0000037996", name="Ford Motor Co", aliases="ford|ford motor", warm=True))
    _event(db, "F", 2026, 3, date(2026, 10, 22), "projected", "amc", "projection")
    store_article(db, url="https://www.prnewswire.com/news-releases/ford-q3.html",
                  headline="Ford to Release Third-Quarter 2026 Financial Results",
                  summary="Ford will report its third-quarter 2026 results on Wednesday, Oct. 28, after the U.S. "
                          "stock market closes.", source="PRNewswire", image="", category="company",
                  published_at=datetime(2026, 10, 1, 12), origin="feed:prnewswire-earnings", summary_basis="feed",
                  tickers=["F"])
    db.commit()
    ev = db.query(EarningsEvent).one()
    assert (ev.date, ev.hour, ev.date_source) == (date(2026, 10, 28), "amc", "newswire")


def test_announcement_about_another_company_is_ignored(db):
    db.add(Company(ticker="MSFT", cik="0000789019", name="Microsoft Corp", aliases="microsoft", warm=True))
    _event(db, "MSFT", 2027, 1, date(2026, 10, 27), "projected", "amc", "projection")
    record_earnings_date(db, "AI stocks to watch: Microsoft, Innodata",
                         "INNODATA INC. (Nasdaq:INOD) today announced that it will report Third Quarter 2026 results "
                         "after the market close on Thursday, November 5, 2026.", ["MSFT"])
    db.commit()
    assert db.query(EarningsEvent).one().date == date(2026, 10, 27)


def test_news_report_uses_news_source(db):
    db.add(Company(ticker="INTC", cik="0000050863", name="Intel Corp", aliases="intel", warm=True))
    _event(db, "INTC", 2026, 3, date(2026, 10, 22), "projected")
    record_earnings_date(db, "Intel to report Q3 results on October 29", "", ["INTC"])
    db.commit()
    assert db.query(EarningsEvent).one().date_source == "news"


def test_edgar_announcement(db, monkeypatch):
    db.add(Company(ticker="SOFI", cik="0001818874", name="SoFi Technologies, Inc.", aliases="sofi", warm=True))
    _event(db, "SOFI", 2026, 2, date(2026, 7, 28), "reported", "bmo", "8-k")
    _event(db, "SOFI", 2026, 3, date(2026, 10, 28), "projected", "bmo", "projection")
    hits = {"hits": {"hits": [
        {"_id": "0001818874-26-000101:ex991.htm", "_source": {"ciks": ["0001818874"], "items": ["7.01", "9.01"]}},
        {"_id": "0001818874-26-000090:release.htm", "_source": {"ciks": ["0001818874"], "items": ["2.02", "9.01"]}},
    ]}}

    class Resp:
        def json(self):
            return hits

    monkeypatch.setattr(earnings_dates.sec, "_get", lambda url: Resp())
    monkeypatch.setattr(earnings_dates.sec, "fetch_html", lambda url: (
        "<html><body>SoFi will host a conference call on Tuesday, October 27, 2026 at 8:00 a.m. ET, before the "
        "market opens, to discuss its third quarter results.</body></html>"))
    assert earnings_dates.edgar_announcements(db, today=TODAY) == 1
    ev = db.query(EarningsEvent).filter_by(fiscal_quarter=3).one()
    assert (ev.date, ev.hour, ev.date_source) == (date(2026, 10, 27), "bmo", "8-k-announcement")


def test_fixture_files_are_q4_shaped():
    for name in ("apple", "caterpillar"):
        assert "GetEventListResult" in json.loads((FIXTURES / f"q4_events_{name}.json").read_text())


# ---- IR events pages ---------------------------------------------------------------------------


def test_parse_event_page_lines():
    from app.sources.ir_events import parse_event_page

    text = "\n".join([
        "Upcoming Events", "Q3 2026 Intel Corporation Earnings Conference Call", "Oct 29, 2026", "2:00 PM PT",
        "Past Events", "Q2 2026 Intel Corporation Earnings Conference Call", "Jul 23, 2026",
        "Morgan Stanley Technology Conference", "Mar 3, 2027",
    ])
    events = parse_event_page(text)
    assert [(e.start_et.replace(tzinfo=None), call_hour(e.start_et)) for e in events] == [
        (datetime(2026, 10, 29, 17, 0), "amc"), (datetime(2026, 7, 23, 12, 0), "")]


def test_parse_event_page_date_before_title():
    from app.sources.ir_events import parse_event_page

    text = "\n".join([
        "Oct 30, 2026", "3Q 2026 Earnings Call", "8:30 AM ET",
        "Sep 9, 2026", "Barclays CEO Energy-Power Conference",
        "Jul 31, 2026", "2Q 2026 Earnings Call",
    ])
    events = parse_event_page(text)
    assert [(e.start_et.date(), call_hour(e.start_et)) for e in events] == [
        (date(2026, 10, 30), "bmo"), (date(2026, 7, 31), "")]


def test_parse_event_page_walmart_layout():
    from app.sources.ir_events import parse_event_page

    text = "\n".join([
        "Upcoming Events", "Nov", "19", "Nov. 19, 2026", "|", "7:00 a.m.", "US/Central", "FY2027 Q3 Earnings Release",
        "Feb", "18", "Feb. 18, 2027", "|", "7:00 a.m.", "US/Central", "FY2027 Q4 Earnings Release",
        "Past Events", "Sep. 16, 2026", "|", "11:00 a.m.", "US/Central", "Investment Community Meeting",
        "Aug. 20, 2026", "|", "7:00 a.m.", "US/Central", "FY2027 Q2 Earnings Release",
    ])
    events = parse_event_page(text)
    assert [(e.start_et.replace(tzinfo=None), call_hour(e.start_et)) for e in events] == [
        (datetime(2026, 11, 19, 8, 0), "bmo"), (datetime(2027, 2, 18, 8, 0), "bmo"), (datetime(2026, 8, 20, 8, 0), "bmo")]


def test_parse_event_page_without_time_leaves_hour_unstated():
    from app.sources.ir_events import parse_event_page

    events = parse_event_page("AMD Fiscal Third Quarter 2026 Financial Results\nNov 3, 2026")
    assert len(events) == 1 and events[0].start_et.date() == date(2026, 11, 3) and call_hour(events[0].start_et) == ""


def test_discovery_falls_back_to_events_page(db, monkeypatch):
    db.add(Company(ticker="KO", cik="0000021344", name="Coca-Cola Co", aliases="coca cola", warm=True))
    _event(db, "KO", 2026, 2, date(2026, 7, 21), "reported", "bmo", "8-k")
    _event(db, "KO", 2026, 3, date(2026, 10, 20), "projected", "bmo", "projection")
    page = ("<html><body><div>Events</div><div>3rd Quarter 2026 Earnings Call</div>"
            "<div>Oct 27, 2026</div><div>8:30 AM ET</div></body></html>")

    class FakeCalendars(ir_events.IrCalendars):
        def fetch_html(self, url):
            if url == "https://investors.coca-colacompany.com/news-events/events":
                return page
            return None

        def resolve_host(self, url):
            return None

    monkeypatch.setattr(earnings_dates, "_latest_release_text",
                        lambda c: "More at investors.coca-colacompany.com")
    company = db.query(Company).one()
    assert earnings_dates.refresh_ir_calendar(db, company, FakeCalendars(), now=datetime(2026, 10, 19, 12)) == 1
    ev = db.query(EarningsEvent).filter_by(fiscal_quarter=3).one()
    assert (ev.date, ev.hour, ev.date_source) == (date(2026, 10, 27), "bmo", "ir-calendar:page")
    assert company.ir_platform == "page"
