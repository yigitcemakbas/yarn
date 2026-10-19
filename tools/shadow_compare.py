"""Shadow comparison: Finnhub and yarn side by side, field by field.

Development only: it calls Finnhub with loom's existing free key, which is the
non-commercial use the free tier allows. yarn itself never calls Finnhub.

    python tools/shadow_compare.py --finnhub-key-file ../loom/secrets/finnhub_api_key \
        --yarn-url http://localhost:8080/api/v1 --yarn-key local-dev-key --out docs/shadow/latest.json

With --record, each run also appends first-seen observations to
docs/shadow/observations.jsonl, so repeated runs over the two-week shadow
period measure which service had each item first.

Exit criteria (plan, Verification 2): 100% match on earnings date / hour /
quarter and on actuals (±0.01, revenue exact), and latency no worse than
Finnhub's. Estimates and recommendation counts are reported, not gated: the
audit established they cannot match.
"""

import argparse
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

FINNHUB = "https://finnhub.io/api/v1"
# How headlines name each default ticker, for loom's "headline names the company" filter.
DEFAULT_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta",
    "TSLA": "Tesla", "JPM": "JPMorgan", "BAC": "Bank of America", "XOM": "Exxon", "CVX": "Chevron",
    "JNJ": "Johnson & Johnson", "PFE": "Pfizer", "UNH": "UnitedHealth", "WMT": "Walmart", "KO": "Coca-Cola",
    "PEP": "PepsiCo", "DIS": "Disney", "NFLX": "Netflix", "INTC": "Intel", "AMD": "AMD", "CRM": "Salesforce",
    "ORCL": "Oracle", "BA": "Boeing", "CAT": "Caterpillar", "GE": "GE", "F": "Ford", "VZ": "Verizon",
    "PLTR": "Palantir", "SOFI": "SoFi",
}
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "BAC", "XOM",
    "CVX", "JNJ", "PFE", "UNH", "WMT", "KO", "PEP", "DIS", "NFLX", "INTC",
    "AMD", "CRM", "ORCL", "BA", "CAT", "GE", "F", "VZ", "PLTR", "SOFI",
]


class Api:
    def __init__(self, base: str, token: str, pace: float):
        self.base, self.token, self.pace = base.rstrip("/"), token, pace
        self.client = httpx.Client(timeout=60.0)

    def get(self, path: str, **params):
        for attempt in range(4):
            try:
                r = self.client.get(f"{self.base}{path}", params={**params, "token": self.token})
            except httpx.TransportError:
                time.sleep(5 * (attempt + 1))
                continue
            time.sleep(self.pace)
            if r.status_code == 429:
                time.sleep(30)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"rate limited on {path}")


def _norm(headline: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", headline.lower()).split())


def _names_company(headline: str, name: str, ticker: str) -> bool:
    return name.lower() in headline.lower() or re.search(rf"\b{re.escape(ticker)}\b", headline) is not None


def compare_earnings(fh: list[dict], yn: list[dict]) -> dict:
    by_q = {(r["year"], r["quarter"]): r for r in yn}
    rows, checks = [], {"date": [0, 0], "hour": [0, 0], "eps_actual": [0, 0], "revenue_actual": [0, 0]}
    for f in fh:
        y = by_q.get((f["year"], f["quarter"]))
        row = {"year": f["year"], "quarter": f["quarter"], "finnhub": f, "yarn": y}
        if y is None:
            row["missing_in_yarn"] = True
            for k in checks:
                checks[k][1] += 1
        else:
            checks["date"][1] += 1
            checks["date"][0] += f["date"] == y["date"]
            if f["hour"]:
                checks["hour"][1] += 1
                checks["hour"][0] += f["hour"] == y["hour"]
            if f["epsActual"] is not None:
                checks["eps_actual"][1] += 1
                checks["eps_actual"][0] += y["epsActual"] is not None and abs(f["epsActual"] - y["epsActual"]) <= 0.01
            if f["revenueActual"] is not None:
                checks["revenue_actual"][1] += 1
                checks["revenue_actual"][0] += y["revenueActual"] == f["revenueActual"]
            if f["epsEstimate"] is not None and y.get("epsEstimate") is not None:
                row["eps_estimate_delta"] = round(y["epsEstimate"] - f["epsEstimate"], 4)
        rows.append(row)
    return {"checks": {k: {"matched": m, "of": n} for k, (m, n) in checks.items()}, "rows": rows}


def compare_news(fh: list[dict], yn: list[dict], name: str, ticker: str) -> dict:
    """Recall over the items loom keeps (headline names the company, summary ≥ 120 chars)."""
    kept = [i for i in fh if _names_company(i["headline"], name, ticker) and len(i.get("summary") or "") >= 120]
    yarn_heads = {_norm(i["headline"]): i for i in yn}
    matched, lead = 0, []
    for i in kept:
        y = yarn_heads.get(_norm(i["headline"]))
        if y is not None:
            matched += 1
            lead.append(i["datetime"] - y["datetime"])
    yarn_kept = [i for i in yn if _names_company(i["headline"], name, ticker) and len(i.get("summary") or "") >= 120]
    return {
        "finnhub_items": len(fh), "finnhub_kept_by_loom": len(kept),
        "yarn_items": len(yn), "yarn_kept_by_loom": len(yarn_kept),
        "headline_recall": round(matched / len(kept), 3) if kept else None,
        "median_yarn_lead_seconds": sorted(lead)[len(lead) // 2] if lead else None,
    }


def compare_recommendations(fh: list[dict], yn: list[dict]) -> dict:
    by_p = {r["period"]: r for r in yn}
    out = []
    for f in fh:
        y = by_p.get(f["period"])
        out.append({"period": f["period"],
                    "finnhub": [f[k] for k in ("strongBuy", "buy", "hold", "sell", "strongSell")],
                    "yarn": [y[k] for k in ("strongBuy", "buy", "hold", "sell", "strongSell")] if y else None,
                    "yarn_firms": y.get("firmsTracked") if y else 0})
    return {"periods": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--finnhub-key-file", required=True)
    ap.add_argument("--yarn-url", default="http://localhost:8080/api/v1")
    ap.add_argument("--yarn-key", required=True)
    ap.add_argument("--tickers", nargs="*", default=DEFAULT_TICKERS)
    ap.add_argument("--out", default="docs/shadow/latest.json")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--names", default="", help="JSON map ticker->headline name; defaults to ticker")
    args = ap.parse_args()

    fh = Api(FINNHUB, Path(args.finnhub_key_file).read_text().strip(), pace=1.1)
    yn = Api(args.yarn_url, args.yarn_key, pace=0.0)
    names = {**DEFAULT_NAMES, **(json.loads(args.names) if args.names else {})}
    today = date.today()
    window = {"from": (today - timedelta(days=400)).isoformat(), "to": (today + timedelta(days=120)).isoformat()}
    news_window = {"from": (today - timedelta(days=60)).isoformat(), "to": today.isoformat()}

    report = {"run_at": datetime.now(timezone.utc).isoformat(), "tickers": {}}
    totals = {"date": [0, 0], "hour": [0, 0], "eps_actual": [0, 0], "revenue_actual": [0, 0]}
    observations = []
    for t in args.tickers:
        name = names.get(t, t)
        e = compare_earnings(fh.get("/calendar/earnings", symbol=t, **window)["earningsCalendar"],
                             yn.get("/calendar/earnings", symbol=t, **window)["earningsCalendar"])
        for k, v in e["checks"].items():
            totals[k][0] += v["matched"]
            totals[k][1] += v["of"]
        fn, yn_news = fh.get("/company-news", symbol=t, **news_window), yn.get("/company-news", symbol=t, **news_window)
        n = compare_news(fn, yn_news, name, t)
        r = compare_recommendations(fh.get("/stock/recommendation", symbol=t), yn.get("/stock/recommendation", symbol=t))
        report["tickers"][t] = {"earnings": e, "news": n, "recommendations": r}
        if args.record:
            seen = datetime.now(timezone.utc).isoformat()
            observations += [{"seen": seen, "service": "finnhub", "ticker": t, "headline": _norm(i["headline"])} for i in fn]
            observations += [{"seen": seen, "service": "yarn", "ticker": t, "headline": _norm(i["headline"])} for i in yn_news]
        print(t, json.dumps(e["checks"]), "news recall", n["headline_recall"], flush=True)

    report["totals"] = {k: {"matched": m, "of": n, "rate": round(m / n, 3) if n else None} for k, (m, n) in totals.items()}
    # Upcoming quarters (no actual yet), split by what set yarn's date.
    by_source: dict[str, list[int]] = {}
    for v in report["tickers"].values():
        for row in v["earnings"]["rows"]:
            f, y = row["finnhub"], row["yarn"]
            if f["epsActual"] is not None or f["revenueActual"] is not None:
                continue
            src = (y or {}).get("dateSource") or ("missing" if y is None else "unlabelled")
            hit = by_source.setdefault(src, [0, 0])
            hit[1] += 1
            hit[0] += bool(y) and y["date"] == f["date"]
    report["upcoming_dates_by_source"] = {k: {"matched": m, "of": n, "rate": round(m / n, 3) if n else None}
                                          for k, (m, n) in sorted(by_source.items())}
    up_m, up_n = sum(m for m, _ in by_source.values()), sum(n for _, n in by_source.values())
    report["upcoming_dates"] = {"matched": up_m, "of": up_n, "rate": round(up_m / up_n, 3) if up_n else None}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    if args.record:
        with (out.parent / "observations.jsonl").open("a") as fh_obs:
            for o in observations:
                fh_obs.write(json.dumps(o) + "\n")
    print("TOTALS", json.dumps(report["totals"]))
    print("UPCOMING DATES", json.dumps(report["upcoming_dates"]), "BY SOURCE", json.dumps(report["upcoming_dates_by_source"]))


if __name__ == "__main__":
    main()
