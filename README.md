# yarn

yarn is loom's in-house replacement for the Finnhub API. It serves the endpoints loom uses at Finnhub's paths, with Finnhub's parameters, authentication, JSON shapes, error bodies and rate-limit headers. Loom switches by pointing at a different base URL. The data comes from sources loom may use commercially, at no data cost.

| Endpoint | yarn's source | Parity |
|---|---|---|
| `/api/v1/company-news` | GDELT's 15-minute GKG stream (every article whose headline names a warm company), GDELT searches (backfill, mention-level), PR Newswire and Business Wire releases; summaries from the publisher's page description | Same shape; different article set (see `tools/shadow_compare.py`, `tools/news_freshness.py`) |
| `/api/v1/news?category=` | GDELT topic queries, central-bank press releases, newswire M&A releases | Same shape; different article set |
| `/api/v1/calendar/earnings` | SEC 8-K Item 2.02 filings, press-release exhibits, XBRL company facts; upcoming dates from company IR calendars (Q4 feeds and events pages), 8-K announcements and newswire releases | Dates, hour, fiscal quarter and actuals measured against Finnhub (see Verification). Estimates differ (see below) |
| `/api/v1/stock/recommendation` | broker rating actions read from headlines | Same shape; partial coverage |

**Licensed-at-origin fields.** Analyst consensus (`epsEstimate`, `revenueEstimate`) and recommendation counts come from Finnhub's own licensed analyst panel. No free source can reproduce them; the REA-based audit that established this is in [`docs/audits/estimates-and-recommendations.md`](docs/audits/estimates-and-recommendations.md).

yarn serves its best legal values in the same fields and adds fields saying what they are. Finnhub-shaped parsers ignore the additions.

- `estimateBasis`: `"guidance"` or `"model"`
- `dateStatus`: `"reported"`, `"confirmed"` or `"projected"`
- `dateSource`: what set the date, in order of authority:
  - `"ir-calendar:q4"` / `"ir-calendar:page"`: the company's own investor-relations calendar
  - `"8-k-announcement"`
  - `"newswire"`
  - `"news"`
  - `"projection"`: last year's cadence
  - `"8-k"`: the release itself, once reported
- `epsBasis`: `"adjusted"` or `"gaap"`
- `firmsTracked`: how many firms a recommendation month rests on
- `yarnBasis`: where the recommendation counts come from

## Run

```bash
cp .env.example .env
```

Then set your SEC contact email and `WARM_TICKERS` in `.env`, and put yarn's API keys (comma-separated) in `secrets/api_keys`.

```bash
docker compose up -d --build
```

To join loom's network, so loom reaches `http://yarn-api:8080/api/v1`:

```bash
docker compose -f docker-compose.yml -f docker-compose.loom.yml up -d
```

- `yarn-api` serves requests from the database and never waits on an upstream.
- `yarn-worker` keeps the database fresh:
  - EDGAR's live 8-K feed every minute
  - newswires every 2 minutes
  - GDELT company news in a 1-minute rotation
  - market news every 10 minutes
- A ticker nobody has asked for yet is warmed on first request.
- Company names that headlines use (`aliases`) are derived from SEC names, plus brand overrides in `app/pipelines/companies.py` (`BRAND_ALIASES`) and the `ALIAS_OVERRIDES` setting.

## The contract

The contract was recorded from Finnhub itself and is checked by `tests/contract/test_finnhub_contract.py`.

- `tests/contract/finnhub.har`: 155 recorded calls, including edge cases (bad token, missing params, unknown symbols, a 429), with the key redacted.
- `tests/contract/rea/api_surface.json`: REA's api-surface view of the HAR (path templates, query parameters).
- `tests/contract/contract.json`: key order, types, nullability, caps, sort orders, default windows and error bodies, derived by `tools/derive_contract.py`.

## Verification

```bash
.venv/bin/python -m pytest tests -q
```

The shadow comparison against Finnhub runs in development only, using loom's free key:

```bash
.venv/bin/python tools/shadow_compare.py --finnhub-key-file ../loom/secrets/finnhub_api_key --yarn-key <key> --record
```

The latest report is written to `docs/shadow/latest.json`.
