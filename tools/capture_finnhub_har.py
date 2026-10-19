"""Record the Finnhub endpoints loom uses as a HAR 1.2 file plus golden fixtures.

This captures the *contract* yarn must satisfy: paths, parameters, status
codes, headers and body shapes, including edge cases. It is run by hand during
development with loom's existing free key; yarn itself never calls Finnhub.

The token is redacted everywhere it would otherwise be written.

    python3 tools/capture_finnhub_har.py --key-file ../loom/secrets/finnhub_api_key
"""

import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

BASE = "https://finnhub.io/api/v1"
REDACTED = "REDACTED"

TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "BAC", "XOM",
    "CVX", "JNJ", "PFE", "UNH", "WMT", "KO", "PEP", "DIS", "NFLX", "INTC",
    "AMD", "CRM", "ORCL", "BA", "CAT", "GE", "F", "VZ", "PLTR", "SOFI",
]

# Seconds between calls: the free tier allows 60 a minute.
PACE = 1.1


def _case_name(*parts: str) -> str:
    return "_".join(p for p in parts if p).replace("/", "-")


class Recorder:
    def __init__(self, token: str, out_dir: Path):
        self.token = token
        self.out_dir = out_dir
        self.entries: list[dict] = []
        self.client = httpx.Client(timeout=30.0)

    def call(self, endpoint: str, case: str, params: dict, *, token: str | None = "default",
             header_token: bool = False, pace: bool = True) -> httpx.Response:
        sent = dict(params)
        headers = {"User-Agent": "yarn-contract-capture/0.1"}
        tok = self.token if token == "default" else token
        if tok is not None:
            if header_token:
                headers["X-Finnhub-Token"] = tok
            else:
                sent["token"] = tok
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        resp = self.client.get(f"{BASE}{endpoint}", params=sent, headers=headers)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self._record(endpoint, case, sent, headers, resp, started, elapsed_ms)
        if pace:
            time.sleep(PACE)
        return resp

    def _redact(self, value: str) -> str:
        return value.replace(self.token, REDACTED) if self.token else value

    def _record(self, endpoint, case, params, headers, resp, started, elapsed_ms):
        shown_params = {k: (REDACTED if k == "token" else v) for k, v in params.items()}
        shown_headers = {k: (REDACTED if k.lower() == "x-finnhub-token" else v) for k, v in headers.items()}
        url = f"{BASE}{endpoint}?{urlencode(shown_params)}" if shown_params else f"{BASE}{endpoint}"
        text = self._redact(resp.text)
        resp_headers = [{"name": k, "value": self._redact(v)} for k, v in resp.headers.items()]
        self.entries.append({
            "startedDateTime": started.isoformat(),
            "time": round(elapsed_ms, 1),
            "request": {
                "method": "GET",
                "url": url,
                "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": v} for k, v in shown_headers.items()],
                "queryString": [{"name": k, "value": str(v)} for k, v in shown_params.items()],
                "cookies": [],
                "headersSize": -1,
                "bodySize": 0,
            },
            "response": {
                "status": resp.status_code,
                "statusText": resp.reason_phrase,
                "httpVersion": resp.http_version,
                "headers": resp_headers,
                "cookies": [],
                "content": {
                    "size": len(resp.content),
                    "mimeType": resp.headers.get("content-type", ""),
                    "text": text,
                },
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": len(resp.content),
            },
            "cache": {},
            "timings": {"send": 0, "wait": round(elapsed_ms, 1), "receive": 0},
            "comment": case,
        })
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        fixture = {
            "case": case,
            "endpoint": endpoint,
            "params": shown_params,
            "token_location": "header" if "X-Finnhub-Token" in shown_headers else ("query" if "token" in shown_params else "none"),
            "captured_at": started.isoformat(),
            "elapsed_ms": round(elapsed_ms, 1),
            "status": resp.status_code,
            "headers": dict((h["name"], h["value"]) for h in resp_headers),
            "body": body,
        }
        path = self.out_dir / endpoint.strip("/").replace("/", "-") / f"{case}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")

    def write_har(self, path: Path):
        har = {"log": {
            "version": "1.2",
            "creator": {"name": "yarn-contract-capture", "version": "0.1"},
            "pages": [],
            "entries": self.entries,
        }}
        path.write_text(json.dumps(har, indent=1, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "tests" / "contract"))
    ap.add_argument("--skip-burst", action="store_true", help="skip the 429 rate-limit probe")
    args = ap.parse_args()

    token = Path(args.key_file).read_text().strip()
    out = Path(args.out)
    rec = Recorder(token, out / "fixtures")
    today = date.today()

    # What loom requests, per ticker.
    for t in TICKERS:
        rec.call("/company-news", _case_name("loom", t), {
            "symbol": t, "from": (today - timedelta(days=60)).isoformat(), "to": today.isoformat()})
        rec.call("/calendar/earnings", _case_name("loom", t), {
            "from": (today - timedelta(days=400)).isoformat(),
            "to": (today + timedelta(days=120)).isoformat(), "symbol": t})
        rec.call("/stock/recommendation", _case_name("loom", t), {"symbol": t})

    for cat in ("general", "forex", "merger", "crypto"):
        rec.call("/news", _case_name("loom", cat), {"category": cat})

    # Edge cases.
    rec.call("/company-news", "unknown_symbol", {"symbol": "ZZZZZQ", "from": (today - timedelta(days=30)).isoformat(), "to": today.isoformat()})
    rec.call("/company-news", "lowercase_symbol", {"symbol": "aapl", "from": (today - timedelta(days=3)).isoformat(), "to": today.isoformat()})
    rec.call("/company-news", "from_after_to", {"symbol": "AAPL", "from": today.isoformat(), "to": (today - timedelta(days=10)).isoformat()})
    rec.call("/company-news", "range_over_1y", {"symbol": "AAPL", "from": (today - timedelta(days=800)).isoformat(), "to": today.isoformat()})
    rec.call("/company-news", "old_window", {"symbol": "AAPL", "from": (today - timedelta(days=400)).isoformat(), "to": (today - timedelta(days=380)).isoformat()})
    rec.call("/company-news", "missing_symbol", {"from": (today - timedelta(days=3)).isoformat(), "to": today.isoformat()})
    rec.call("/company-news", "missing_dates", {"symbol": "AAPL"})
    rec.call("/company-news", "bad_date_format", {"symbol": "AAPL", "from": "2026/01/01", "to": "2026/01/05"})
    rec.call("/company-news", "header_token", {"symbol": "MSFT", "from": (today - timedelta(days=2)).isoformat(), "to": today.isoformat()}, header_token=True)
    rec.call("/company-news", "bad_token", {"symbol": "AAPL", "from": (today - timedelta(days=2)).isoformat(), "to": today.isoformat()}, token="invalidtoken123")
    rec.call("/company-news", "no_token", {"symbol": "AAPL", "from": (today - timedelta(days=2)).isoformat(), "to": today.isoformat()}, token=None)

    general = rec.call("/news", "general_for_minid", {"category": "general"})
    try:
        items = general.json()
        mid = sorted(i["id"] for i in items)[len(items) // 2] if items else 0
    except Exception:
        mid = 0
    rec.call("/news", "minid", {"category": "general", "minId": mid})
    rec.call("/news", "invalid_category", {"category": "notacategory"})
    rec.call("/news", "missing_category", {})

    rec.call("/calendar/earnings", "all_symbols_one_day", {"from": today.isoformat(), "to": today.isoformat()})
    rec.call("/calendar/earnings", "all_symbols_default", {})
    rec.call("/calendar/earnings", "unknown_symbol", {"symbol": "ZZZZZQ", "from": (today - timedelta(days=400)).isoformat(), "to": (today + timedelta(days=120)).isoformat()})
    rec.call("/calendar/earnings", "international_flag", {"symbol": "AAPL", "from": (today - timedelta(days=100)).isoformat(), "to": today.isoformat(), "international": "true"})
    rec.call("/calendar/earnings", "long_history", {"symbol": "AAPL", "from": "2015-01-01", "to": today.isoformat()})

    rec.call("/stock/recommendation", "unknown_symbol", {"symbol": "ZZZZZQ"})
    rec.call("/stock/recommendation", "lowercase_symbol", {"symbol": "msft"})
    rec.call("/stock/recommendation", "missing_symbol", {})

    if not args.skip_burst:
        # Rate-limit behaviour: fire unpaced until the first 429, at most 80 calls.
        for i in range(80):
            r = rec.call("/stock/recommendation", f"burst_{i:02d}", {"symbol": "AAPL"}, pace=False)
            if r.status_code == 429:
                break

    out.mkdir(parents=True, exist_ok=True)
    rec.write_har(out / "finnhub.har")
    print(f"recorded {len(rec.entries)} entries")


if __name__ == "__main__":
    main()
