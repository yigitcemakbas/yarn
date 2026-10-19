# What Finnhub actually serves loom (recorded 2026-10-19)

These facts come from 155 recorded calls (`tests/contract/finnhub.har`) mapped with REA's api-surface view. yarn matches them, and `tests/contract/test_finnhub_contract.py` enforces them. Several are not what loom's code assumes.

## Interface

- **Auth:** `?token=` or the `X-Finnhub-Token` header.
  - Missing key: `401 {"error": "Please use an API key."}`
  - Unknown key: `401 {"error": "Invalid API key"}`
- **Rate limit:**
  - Over the limit: `429 {"error": "Too many requests. Please try again later."}`, with no `Retry-After` header.
  - Every response carries `x-ratelimit-limit` (60), `x-ratelimit-remaining` and `x-ratelimit-reset` (epoch seconds).
- **Unknown symbols** answer `200` with `[]`, or `{"earningsCalendar": []}` for the calendar. Never 404.
- **Missing `symbol`:** `422 {"error": "Missing parameter symbol"}`.
- **Malformed date:** `422 {"error": "Wrong date format. Please use 2022-01-15 format for from and to params."}`.

## `/company-news`

- At most **250 items**, newest first.
- For a large company that covers only about 5 days. Loom asks for 60 days and receives AAPL news from 14 to 19 October only.
- With no `from`/`to`, the window is today (UTC).
- `from` later than `to` returns `[]`.
- Sources are mostly Yahoo (70%), then Benzinga, Seeking Alpha, ChartMill and CNBC.
- `url` is a `finnhub.io/api/news?id=…` redirect.
- `summary` is empty on about 1% of items.
- Every item has `category: "company"` and `related` = the requested symbol.

## `/news`

- At most 100 items, newest first.
- An unknown or missing `category` is treated as `general`.
- `minId` returns items with `id > minId`.
- Items under `general` are labelled `category: "business"` or `"top news"`.
- General-news URLs are Google News RSS redirects.

## `/calendar/earnings`

- For one symbol, the free tier returns only about **two rows**: the latest report and the next one. That holds even for loom's −400/+120-day window, and a window starting in 2015 returns nothing. Loom's beat/miss record therefore rests on almost no history today.
- With no `from`/`to`, the window is the last 28 days.
- Sorted by date descending, then by symbol.
- `hour` is `bmo`, `amc`, `dmh` or `""` (unknown).
- Far-future rows often carry a placeholder `amc` even for companies that always report before the open.
- `epsActual` is the adjusted (non-GAAP) figure. The docs say "Estimates are sourced from both sell-side and buy-side analysts".
- Estimates have four decimals, which points to Finnhub's own average.
- `international=true` returns `401` ("premium users").

### Unconfirmed upcoming dates

When no company has announced a date, Finnhub projects one. Of 35 such rows on 2026-10-19:

- 25 were exactly last year's report date for the same quarter plus **363 days**.
- 6 were +364.

yarn uses the same rule (`earnings.PROJECTION_DAYS`), and a company confirmation replaces it.

Finnhub's `hour` on those rows is not last year's session. It is usually `"amc"`, even for companies that report before the open: 9 of 12 such rows. yarn keeps last year's actual hour instead. This is a deliberate deviation: copying the placeholder would show loom users "after market close" for companies that never report then.

### Confirmed upcoming dates: yarn's sources

The company's own IR calendar matched Finnhub on 15 of 16 dates.

- **Q4 Inc event feeds:** 16 of 30 companies.
- **IR events pages:** 6 of 30.

The one disagreement is ExxonMobil. Its IR calendar lists the 3Q 2026 call on October 30; Finnhub has October 23. yarn follows the company.

News-derived dates count only when the announcing sentence names the company. Without that rule, every news-derived date checked was wrong, because news is linked to every company it mentions.

## `/stock/recommendation`

- The four most recent completed months, newest first. On 19 October the newest period is 1 September.
- One ticker returned a duplicated period (`2026-07-01` twice), a Finnhub data quirk yarn does not reproduce.

## EDGAR quirk found along the way

The submissions API's `acceptanceDateTime` is labelled UTC but is inconsistent across filers:

- For Netflix and Intel it is true UTC.
- For Apple, Meta, JPMorgan and PepsiCo it is shifted by the Eastern offset twice.

The filing's SEC header (`<ACCEPTANCE-DATETIME>`, Eastern time) is authoritative, and yarn uses it for `hour`.
