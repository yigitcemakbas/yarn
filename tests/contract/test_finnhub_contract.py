"""yarn's responses against Finnhub's recorded contract (tests/contract/contract.json).

The contract was recorded from Finnhub itself (tools/capture_finnhub_har.py),
mapped with REA's api-surface view and measured by tools/derive_contract.py.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import Company, EarningsEvent, RatingAction
from app.pipelines.news import store_article

KEY = {"token": "test-key"}
TODAY = datetime.now(timezone.utc).date()


def _finnhub_keys(contract, path):
    return contract["endpoints"][path]["item_schema"]["key_orders"][0]["keys"]


def _seed_company(db, ticker="AAPL"):
    c = Company(ticker=ticker, cik="0000320193", name="Apple Inc.", exchange="Nasdaq", aliases="apple", warm=True)
    db.add(c)
    db.commit()
    return c


def _seed_news(db, n, ticker="AAPL", category="company", start=None):
    start = start or datetime.now(timezone.utc)
    for i in range(n):
        store_article(db, url=f"https://example.com/{ticker}/{category}/{i}", headline=f"Apple story number {i}",
                      summary="s" * 130, source="Example", image="", category=category,
                      published_at=start - timedelta(minutes=i), origin="test", summary_basis="feed",
                      tickers=[ticker] if category == "company" else [])
    db.commit()


# ---- auth and rate limit -----------------------------------------------------------


def test_missing_token_matches_finnhub(client, contract):
    r = client.get("/api/v1/company-news", params={"symbol": "AAPL"})
    assert r.status_code == contract["auth"]["no_token"]["status"]
    assert r.json() == contract["auth"]["no_token"]["body"]


def test_bad_token_matches_finnhub(client, contract):
    r = client.get("/api/v1/company-news", params={"symbol": "AAPL", "token": "nope"})
    assert r.status_code == contract["auth"]["bad_token"]["status"]
    assert r.json() == contract["auth"]["bad_token"]["body"]


def test_header_token_accepted(client):
    r = client.get("/api/v1/stock/recommendation", params={"symbol": "AAPL"}, headers={"X-Finnhub-Token": "test-key"})
    assert r.status_code == 200


def test_rate_limit_headers_and_429(client, contract, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    from app.compat.finnhub import auth

    monkeypatch.setattr(auth, "window", auth.MinuteWindow())
    r1 = client.get("/api/v1/stock/recommendation", params={"symbol": "AAPL", **KEY})
    for h in contract["rate_limit"]["headers"]:
        assert h in r1.headers
    assert r1.headers["x-ratelimit-limit"] == "2"
    client.get("/api/v1/stock/recommendation", params={"symbol": "AAPL", **KEY})
    r3 = client.get("/api/v1/stock/recommendation", params={"symbol": "AAPL", **KEY})
    assert r3.status_code == 429
    assert r3.json() == contract["rate_limit"]["status_429"]["body"]
    assert "retry-after" not in {k.lower() for k in r3.headers}


# ---- /company-news ----------------------------------------------------------------------


def test_company_news_shape(client, db, contract):
    _seed_company(db)
    _seed_news(db, 3)
    r = client.get("/api/v1/company-news", params={"symbol": "AAPL", "from": (TODAY - timedelta(days=2)).isoformat(),
                                                   "to": TODAY.isoformat(), **KEY})
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 3
    path = "/api/v1/company-news"
    for item in items:
        assert list(item.keys()) == _finnhub_keys(contract, path)
        assert isinstance(item["datetime"], int) and isinstance(item["id"], int)
        assert item["category"] == "company" and item["related"] == "AAPL"
    assert [i["datetime"] for i in items] == sorted((i["datetime"] for i in items), reverse=True)


def test_company_news_cap(client, db, contract):
    _seed_company(db)
    _seed_news(db, 260)
    r = client.get("/api/v1/company-news", params={"symbol": "AAPL", "from": (TODAY - timedelta(days=5)).isoformat(),
                                                   "to": TODAY.isoformat(), **KEY})
    assert len(r.json()) == contract["endpoints"]["/api/v1/company-news"]["items_per_response"]["max"]


@pytest.mark.parametrize("case,params", [
    ("missing_symbol", {"from": "2026-10-01", "to": "2026-10-02"}),
    ("bad_date_format", {"symbol": "AAPL", "from": "2026/01/01", "to": "2026/01/05"}),
])
def test_company_news_errors(client, db, contract, case, params):
    expected = contract["endpoints"]["/api/v1/company-news"]["error_responses"][case]
    r = client.get("/api/v1/company-news", params={**params, **KEY})
    assert r.status_code == expected["status"]
    assert r.json() == expected["body"]


def test_company_news_empty_cases(client, db):
    _seed_company(db)
    _seed_news(db, 2)
    unknown = client.get("/api/v1/company-news", params={"symbol": "ZZZZZQ", "from": "2026-01-01", "to": TODAY.isoformat(), **KEY})
    assert unknown.status_code == 200 and unknown.json() == []
    inverted = client.get("/api/v1/company-news", params={"symbol": "AAPL", "from": TODAY.isoformat(),
                                                          "to": (TODAY - timedelta(days=10)).isoformat(), **KEY})
    assert inverted.status_code == 200 and inverted.json() == []


def test_company_news_defaults_to_today_and_uppercases(client, db):
    _seed_company(db)
    _seed_news(db, 2)
    _seed_news(db, 2, start=datetime.now(timezone.utc) - timedelta(days=3))
    r = client.get("/api/v1/company-news", params={"symbol": "aapl", **KEY})
    assert r.status_code == 200
    assert all(datetime.fromtimestamp(i["datetime"], timezone.utc).date() == TODAY for i in r.json())


# ---- /news --------------------------------------------------------------------------------


def test_market_news_shape_and_unknown_category(client, db, contract):
    _seed_news(db, 120, category="business")
    path = "/api/v1/news"
    r = client.get(path, params={"category": "general", **KEY})
    items = r.json()
    assert len(items) == contract["endpoints"][path]["items_per_response"]["max"]
    assert list(items[0].keys()) == _finnhub_keys(contract, path)
    assert client.get(path, params={"category": "notacategory", **KEY}).json() == items
    assert client.get(path, params=KEY).json() == items


def test_market_news_min_id(client, db):
    _seed_news(db, 10, category="business")
    items = client.get("/api/v1/news", params={"category": "general", **KEY}).json()
    pivot = sorted(i["id"] for i in items)[4]
    after = client.get("/api/v1/news", params={"category": "general", "minId": pivot, **KEY}).json()
    assert after and all(i["id"] > pivot for i in after)


# ---- /calendar/earnings ---------------------------------------------------------------------


def _seed_earnings(db):
    db.add_all([
        EarningsEvent(ticker="AAPL", fiscal_year=2026, fiscal_quarter=4, date=TODAY + timedelta(days=10), hour="amc",
                      date_status="projected", eps_estimate=2.0, revenue_estimate=115_000_000_000, estimate_basis="model"),
        EarningsEvent(ticker="AAPL", fiscal_year=2026, fiscal_quarter=3, date=TODAY - timedelta(days=80), hour="amc",
                      date_status="reported", eps_actual=2.02, revenue_actual=109_417_000_000, eps_basis="gaap"),
        EarningsEvent(ticker="MSFT", fiscal_year=2027, fiscal_quarter=1, date=TODAY - timedelta(days=3), hour="",
                      date_status="reported", eps_actual=4.1, revenue_actual=90_000_000_000, eps_basis="gaap"),
    ])
    db.commit()


def test_earnings_shape(client, db, contract):
    _seed_company(db)
    _seed_earnings(db)
    path = "/api/v1/calendar/earnings"
    r = client.get(path, params={"symbol": "AAPL", "from": (TODAY - timedelta(days=400)).isoformat(),
                                 "to": (TODAY + timedelta(days=120)).isoformat(), **KEY})
    body = r.json()
    assert list(body.keys()) == ["earningsCalendar"]
    rows = body["earningsCalendar"]
    assert [x["date"] for x in rows] == sorted((x["date"] for x in rows), reverse=True)
    finnhub_keys = _finnhub_keys(contract, path)
    for row in rows:
        assert list(row.keys())[: len(finnhub_keys)] == finnhub_keys
        assert isinstance(row["quarter"], int) and isinstance(row["year"], int)
        assert row["revenueActual"] is None or isinstance(row["revenueActual"], int)


def test_earnings_default_window_is_four_weeks(client, db):
    _seed_earnings(db)
    rows = client.get("/api/v1/calendar/earnings", params=KEY).json()["earningsCalendar"]
    assert [r["symbol"] for r in rows] == ["MSFT"]


def test_earnings_unknown_symbol(client, db):
    _seed_earnings(db)
    r = client.get("/api/v1/calendar/earnings", params={"symbol": "ZZZZZQ", **KEY})
    assert r.status_code == 200 and r.json() == {"earningsCalendar": []}


# ---- /stock/recommendation ----------------------------------------------------------------


def test_recommendation_shape(client, db, contract):
    _seed_company(db)
    month_ago = TODAY.replace(day=1) - timedelta(days=10)
    db.add_all([
        RatingAction(ticker="AAPL", firm="Morgan Stanley", action="upgrade", rating="Overweight", bucket="buy", acted_on=month_ago),
        RatingAction(ticker="AAPL", firm="Barclays", action="downgrade", rating="Underweight", bucket="sell", acted_on=month_ago),
    ])
    db.commit()
    path = "/api/v1/stock/recommendation"
    rows = client.get(path, params={"symbol": "AAPL", **KEY}).json()
    assert len(rows) == contract["endpoints"][path]["items_per_response"]["max"]
    finnhub_keys = _finnhub_keys(contract, path)
    assert list(rows[0].keys())[: len(finnhub_keys)] == finnhub_keys
    previous_month = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=1)
    assert rows[0]["period"] == previous_month.isoformat()
    assert rows[0]["buy"] == 1 and rows[0]["sell"] == 1


def test_recommendation_errors(client, db, contract):
    expected = contract["endpoints"]["/api/v1/stock/recommendation"]["error_responses"]["missing_symbol"]
    r = client.get("/api/v1/stock/recommendation", params=KEY)
    assert r.status_code == expected["status"] and r.json() == expected["body"]
    assert client.get("/api/v1/stock/recommendation", params={"symbol": "ZZZZZQ", **KEY}).json() == []


def test_finnhub_date_is_iso(db):
    assert date.fromisoformat(TODAY.isoformat()) == TODAY
