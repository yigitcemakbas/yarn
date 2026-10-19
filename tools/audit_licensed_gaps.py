"""Step 0b: can analyst estimates or recommendation counts be reproduced legally for free?

For each candidate public page that shows consensus EPS/revenue or rating
counts, run one REA browser scenario (`rea capture-browser-scenario`, launch
mode, response bodies retained) and record:

  - which hosts actually served data (XHR/fetch/document responses)
  - vendor attribution strings found in those responses
  - whether Finnhub's exact recorded values for the reference ticker appear

Raw REA Evidence is written to --raw-dir (outside the repo: it holds
third-party page content); only the per-candidate summary is committed, to
docs/audits/estimates-and-recommendations.json.

Nothing here feeds yarn's data. It is a provenance and licensing audit.
"""

import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

REA = ["npx", "-y", "rea-agents@6.7.0"]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NODE24 = os.path.expanduser("~/.local/share/node24/current/bin")

VENDORS = [
    "LSEG", "Refinitiv", "I/B/E/S", "IBES", "FactSet", "Zacks", "S&P Global", "Capital IQ",
    "Morningstar", "Visible Alpha", "Vara Research", "TipRanks", "Estimize", "Wall Street Horizon",
    "Benzinga", "Finnhub", "Financial Modeling Prep", "Bloomberg", "MarketBeat", "Thomson Reuters",
    "Yahoo", "Barchart", "Xignite", "Intrinio", "Quandl", "Nasdaq Data Link", "Polygon", "Tiingo",
    "Seeking Alpha", "Dow Jones", "Investing.com", "WallStreetZen", "Koyfin",
]

CANDIDATES = [
    # (name, url, what it shows)
    ("yahoo_analysis", "https://finance.yahoo.com/quote/{T}/analysis/", "estimates+recommendations"),
    ("nasdaq_earnings", "https://www.nasdaq.com/market-activity/stocks/{t}/earnings", "estimates"),
    ("nasdaq_analyst", "https://www.nasdaq.com/market-activity/stocks/{t}/analyst-research", "recommendations"),
    ("marketwatch_estimates", "https://www.marketwatch.com/investing/stock/{t}/analystestimates", "estimates+recommendations"),
    ("zacks_estimates", "https://www.zacks.com/stock/quote/{T}/detailed-earning-estimates", "estimates"),
    ("seekingalpha_estimates", "https://seekingalpha.com/symbol/{T}/earnings/estimates", "estimates"),
    ("cnbc_earnings", "https://www.cnbc.com/quotes/{T}?tab=earnings", "estimates"),
    ("wsj_ratings", "https://www.wsj.com/market-data/quotes/{T}/research-ratings", "estimates+recommendations"),
    ("tipranks_forecast", "https://www.tipranks.com/stocks/{t}/forecast", "recommendations"),
    ("estimize", "https://www.estimize.com/{t}", "estimates"),
    ("marketbeat_forecast", "https://www.marketbeat.com/stocks/NASDAQ/{T}/forecast/", "recommendations"),
    ("stockanalysis_forecast", "https://stockanalysis.com/stocks/{t}/forecast/", "estimates+recommendations"),
    ("finviz_quote", "https://finviz.com/quote.ashx?t={T}", "recommendations"),
    ("investing_consensus", "https://www.investing.com/equities/apple-computer-inc-consensus-estimates", "estimates"),
    ("morningstar_quote", "https://www.morningstar.com/stocks/xnas/{t}/quote", "estimates"),
    ("macrotrends", "https://www.macrotrends.net/stocks/charts/{T}/apple/eps-earnings-per-share-diluted", "estimates"),
    ("apple_ir", "https://investor.apple.com/investor-relations/default.aspx", "company IR"),
    ("finnhub_docs_recommendation", "https://finnhub.io/docs/api/recommendation-trends", "provenance of Finnhub's own data"),
    ("finnhub_docs_earnings_calendar", "https://finnhub.io/docs/api/earnings-calendar", "provenance of Finnhub's own data"),
]


def run_capture(url: str) -> dict:
    scenario = {
        "browser": {"mode": "launch", "executable_path": CHROME},
        "start_url": {"url": url},
        "actions": [{"action": "wait_for_timeout", "step_id": "settle", "duration_ms": 8000}],
        "capture": {
            "network": {"response_body": True, "header_values": True},
            # Page documents carrying server-rendered data exceed the 64 KiB default.
            "limits": {
                "max_network_body_bytes": 8 * 1024 * 1024,
                "max_network_body_allocation_bytes": 96 * 1024 * 1024,
                "max_total_event_allocation_bytes": 128 * 1024 * 1024,
                "max_event_allocation_bytes": 16 * 1024 * 1024,
                "max_network_transactions": 2048,
            },
        },
    }
    env = {**os.environ, "PATH": f"{NODE24}:{os.environ.get('PATH', '')}"}
    proc = subprocess.run([*REA, "capture-browser-scenario", json.dumps(scenario), "--format", "json"],
                          capture_output=True, text=True, timeout=300, env=env)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": "unparseable", "stderr": proc.stderr[-2000:], "stdout": proc.stdout[-2000:]}


def _bodies(evidence: dict):
    """Yield (url, resource_type, status, text) for responses with retained bodies."""
    items = evidence.get("normalized_result", {}).get("events", {}).get("items", [])
    meta = {}
    for ev in items:
        if ev.get("kind") == "response":
            meta[ev.get("transaction_id")] = (ev.get("url", {}).get("url"), ev.get("resource_type"), ev.get("status"))
    for ev in items:
        if ev.get("kind") != "network-content" or ev.get("phase") != "response":
            continue
        body = ev.get("body") or {}
        if body.get("state") != "captured" or "content" not in body:
            continue
        raw = base64.b64decode(body["content"]) if body.get("encoding") == "base64" else body["content"].encode()
        text = raw.decode("utf-8", errors="replace")
        url, rtype, status = meta.get(ev.get("transaction_id"), (None, None, None))
        if rtype in ("image", "font", "media", "stylesheet"):
            continue
        yield url, rtype, status, text


def summarize(name, url, shows, evidence, finnhub_values):
    if "error" in evidence:
        return {"candidate": name, "url": url, "shows": shows, "captured": False,
                "error": {k: evidence.get(k) for k in ("error", "code", "message")}}
    nr = evidence["normalized_result"]
    items = nr.get("events", {}).get("items", [])
    responses = [e for e in items if e.get("kind") == "response"]
    final_status = next((e.get("status") for e in responses if e.get("resource_type") == "document"), None)
    data_hosts = sorted({urlparse(e["url"]["url"]).netloc for e in responses
                         if e.get("resource_type") in ("xhr", "fetch") and e.get("url", {}).get("url")})
    vendor_hits, value_hits = {}, {}
    for burl, rtype, status, text in _bodies(evidence):
        for v in VENDORS:
            if re.search(r"(?<![A-Za-z])" + re.escape(v) + r"(?![A-Za-z])", text):
                vendor_hits.setdefault(v, set()).add(urlparse(burl or "").netloc)
        for label, needles in finnhub_values.items():
            for n in needles:
                if n in text:
                    value_hits.setdefault(label, set()).add(f"{n} @ {urlparse(burl or '').netloc}")
    return {
        "candidate": name, "url": url, "shows": shows, "captured": True,
        "evidence_id": evidence.get("evidence_id"),
        "document_status": final_status,
        "responses": len(responses),
        "data_hosts": data_hosts,
        "vendor_attributions": {k: sorted(v) for k, v in sorted(vendor_hits.items())},
        "finnhub_value_matches": {k: sorted(v) for k, v in value_hits.items()},
        "completeness": nr.get("completeness", {}).get("status"),
    }


def finnhub_reference(fixtures: Path, ticker: str) -> dict:
    earn = json.loads((fixtures / "calendar-earnings" / f"loom_{ticker}.json").read_text())["body"]["earningsCalendar"]
    rec = json.loads((fixtures / "stock-recommendation" / f"loom_{ticker}.json").read_text())["body"]
    nxt = sorted(earn, key=lambda e: e["date"])[0]
    eps = nxt["epsEstimate"]
    rev = nxt["revenueEstimate"]
    latest = rec[0]
    return {
        "reference": {"earnings": nxt, "recommendation": latest},
        "needles": {
            "epsEstimate_exact": [repr(eps)],
            "epsEstimate_2dp": [f"{eps:.2f}"],
            "revenueEstimate_exact": [str(rev)],
            "revenueEstimate_billions_2dp": [f"{rev / 1e9:.2f}B", f"{rev / 1e9:.2f} B", f"{rev / 1e9:.2f}"],
            "recommendation_counts_json": [
                f'"strongBuy":{latest["strongBuy"]},"buy":{latest["buy"]},"hold":{latest["hold"]}',
            ],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    raw = Path(args.raw_dir)
    raw.mkdir(parents=True, exist_ok=True)
    ref = finnhub_reference(root / "tests" / "contract" / "fixtures", args.ticker)
    out_path = root / "docs" / "audits" / "estimates-and-recommendations.json"
    existing = json.loads(out_path.read_text()) if out_path.exists() else {"candidates": []}
    results = {r["candidate"]: r for r in existing.get("candidates", [])}

    for name, tmpl, shows in CANDIDATES:
        if args.only and name not in args.only:
            continue
        url = tmpl.format(T=args.ticker.upper(), t=args.ticker.lower())
        print(f"capturing {name}: {url}", flush=True)
        try:
            ev = run_capture(url)
        except subprocess.TimeoutExpired:
            ev = {"error": "timeout"}
        (raw / f"{name}.json").write_text(json.dumps(ev))
        results[name] = summarize(name, url, shows, ev, ref["needles"])
        print("   ", json.dumps({k: results[name].get(k) for k in ("document_status", "data_hosts", "vendor_attributions", "finnhub_value_matches", "error")})[:600], flush=True)

    out = {"ticker": args.ticker, "finnhub_reference": ref["reference"], "needles": ref["needles"],
           "candidates": [results[n] for n, _, _ in CANDIDATES if n in results]}
    out_path.write_text(json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
