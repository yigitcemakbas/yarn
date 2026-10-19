# Audit: can yarn reproduce Finnhub's estimates and recommendation counts for free?

**Decision: no.** No free, legally redistributable source reproduces Finnhub's `epsEstimate`, `revenueEstimate` or `/stock/recommendation` counts. yarn ships the labelled substitutes described in the plan (Phase 1, items 3 and 4).

- Date: 2026-10-19
- Reference ticker: AAPL
- Tools: REA 6.7.0 (`capture-browser-scenario`, launch mode, response bodies retained) and EDGAR full-text search
- Machine-readable results: [`estimates-and-recommendations.json`](estimates-and-recommendations.json)
- Runner: [`tools/audit_licensed_gaps.py`](../../tools/audit_licensed_gaps.py)

## Loom does consume these fields

- `epsEstimate` drives loom's surprise calculation in `backend/app/ingestion/facts/earnings_calendar.py` (`_to_fact`). `revenueEstimate` is stored on the `earnings_event` fact.
- `/stock/recommendation` fills `AnalystConsensus`. That is read by `services/outside_views._analysts()`, served at `api/routes/evidence.py:1300`, and labelled in `mcp-server/loom_mcp.py:519`.

## Finnhub reference values (recorded 2026-10-19, `tests/contract/fixtures`)

| Field | Finnhub |
|---|---|
| AAPL FQ4 2026 `epsEstimate` (report 2026-11-02 amc) | **2.0225** |
| AAPL FQ4 2026 `revenueEstimate` | **115,330,851,831** |
| AAPL recommendation, period 2026-09-01 | **12 / 22 / 15 / 3 / 1** (53 analysts) |

## Step 1: provenance

Finnhub's own documentation, captured from `finnhub.io/docs/api/earnings-calendar`, describes the estimates:

> "Estimates are sourced from both sell-side and buy-side analysts."

The same page notes that EPS and revenue in this endpoint are non-GAAP (adjusted).

So Finnhub's consensus is **Finnhub's own blend over its own analyst panel, including buy-side contributors**. Any other aggregator averages a different panel, so an exact match is not expected even in principle. The captures confirm it.

Earlier automated matching flagged "LSEG" on Finnhub's docs page. That was a false positive: the word is a PostgreSQL type keyword inside a syntax-highlighting bundle.

## Steps 2 and 3: candidates

"Exact match" means the same value as Finnhub to ±0.01, or the same five counts.

| Candidate | Captured? | Vendor behind the numbers (from captured payloads) | Exact match? |
|---|---|---|---|
| Yahoo Finance analysis | yes (200) | unattributed on page | **No**. Revenue avg 113.63B vs 115.33B. No rating-count payload in the page |
| StockAnalysis forecast | yes (200) | `estimatesSource:"spg"`, "S&P Global Market Intelligence" and "TipRanks" | **No**. Next-quarter EPS 1.978 vs 2.0225. Strong-buy 23 of 48 analysts vs 12 of 53 |
| MarketBeat forecast | yes (200) | MarketBeat's own panel | **No**. "Out of the 42 analysts, 2 sell, 14 hold, 25 buy, 1 strong buy" |
| CNBC earnings | yes (200) | CNBC GraphQL (`earningsForPast…`) | **No**. Prior quarter `epsEstimatedValue` 1.889, different methodology |
| Estimize | yes (200) | own crowd mean plus `wallstreetMean`. Page credits I/B/E/S, Thomson Reuters, Zacks | **No**. Wall Street mean for FQ3 was 1.89; crowd means differ |
| Zacks detailed estimates | blocked (403) | Zacks (licensed vendor) | not verifiable |
| Seeking Alpha estimates | blocked (403, PerimeterX) | S&P Global (licensed) | not verifiable |
| MarketWatch estimates | blocked (401, captcha) | FactSet (licensed) | not verifiable |
| WSJ research ratings | blocked (401, captcha) | FactSet (licensed) | not verifiable |
| TipRanks forecast | blocked (Cloudflare challenge) | TipRanks (licensed) | not verifiable |
| Finviz quote | blocked (Cloudflare challenge) | — | not verifiable |
| Investing.com consensus | blocked (403) | — | not verifiable |
| Morningstar quote | blocked (403) | Morningstar (licensed) | not verifiable |
| Nasdaq earnings and analyst pages | capture failed | Zacks (licensed) | not verifiable |
| Macrotrends | blocked (Cloudflare challenge) | — | not verifiable |
| Apple investor relations | blocked (Cloudflare challenge) | Apple publishes no consensus page | n/a |
| SEC filings (EDGAR full-text search) | yes | companies' own 8-Ks | **No**. AAPL, MSFT, NVDA and JPM have zero 8-K mentions of "consensus estimate(s)" or "analyst consensus" since 2024. Across all filers, 7 such 8-Ks in Q3 2026 |
| GDELT / newswire headlines | n/a | numbers quoted in news come from LSEG or FactSet | **No**. Per-article, not a consensus series |

The blocked sites are paywalled or bot-protected, and their published terms reserve the data. Each also draws on a different licensed panel (FactSet, S&P Global, Zacks, TipRanks, Morningstar). None could be the free, redistributable, exact source the audit looks for.

## Step 4: decision

No candidate passed all three tests: free, legally redistributable, and an exact match. The terms of use of the accessible candidates were therefore not decisive; none matched on values in the first place.

yarn will:

1. **Serve `epsEstimate` / `revenueEstimate` in the same fields:**
   - from the company's own guidance midpoint when it gives one
   - otherwise from yarn's seasonal model on reported history

   `estimateBasis: "guidance" | "model"` says which. Finnhub-shaped parsers ignore the extra field.
2. **Serve `/stock/recommendation` in the same shape**, from broker rating actions read in yarn's own news headlines:
   - carried forward 12 months per firm
   - mapped into the five buckets
   - `firmsTracked` and `yarnBasis` added

   Counts will usually be lower than Finnhub's, because coverage is limited to actions that make headlines.
3. **Never pull these fields** from Finnhub, Yahoo or any of the vendors above.

A paid estimates feed can replace either substitute later without an API change. That would be the only route to matching a vendor's consensus, and it would match that vendor's numbers, not Finnhub's.
