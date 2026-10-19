"""Centralized configuration.

The only module that reads environment variables or secret files. Everything
else imports `settings` from here.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRETS_DIR = "/run/secrets" if Path("/run/secrets").is_dir() else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        secrets_dir=_SECRETS_DIR,
    )

    database_url: str = "postgresql+psycopg://yarn:yarn@localhost:5433/yarn"

    # Comma-separated API keys accepted on `?token=` or `X-Finnhub-Token`.
    # Loom gets its own key; an empty list refuses every request, exactly as
    # Finnhub does without a key.
    api_keys: str = ""

    # Requests per minute per key, reported in the same `x-ratelimit-*`
    # headers Finnhub sends. Finnhub's free tier is 60; loom is the only
    # client, so the default is generous.
    rate_limit_per_minute: int = 600

    # SEC refuses a User-Agent without an email-shaped token, or with a URL.
    sec_edgar_user_agent: str = "yarn market-data yarn@example.com"
    scraper_user_agent: str = "yarn market-data yarn@example.com"

    scheduler_enabled: bool = True
    expose_api_docs: bool = False

    # Tickers kept warm regardless of traffic. Loom's followed and focus
    # companies belong here; any other ticker is warmed on first request.
    warm_tickers: str = ""

    # Ticker reorganisations that happened before yarn first saw the ticker,
    # as TICKER:OLDCIK pairs. Later CIK changes are recorded automatically.
    cik_predecessors: str = "XOM:0000034088"

    # IR calendars discovery cannot infer from a company's releases, as
    # TICKER=platform:location pairs separated by commas (Intel's IR site is
    # intc.com, a domain its releases never name).
    ir_calendar_overrides: str = "INTC=page:https://www.intc.com/news-events/ir-calendar"

    # How far back the GDELT GKG stream starts on a fresh database, in hours.
    # Each 15-minute file is about 5.5 MB, so a day of catch-up is ~500 MB.
    gkg_backfill_hours: int = 24

    # Extra headline names, as TICKER=alias;alias pairs separated by commas;
    # added to companies.BRAND_ALIASES.
    alias_overrides: str = ""

    @property
    def alias_override_map(self) -> dict[str, list[str]]:
        out = {}
        for pair in self.alias_overrides.split(","):
            if "=" in pair:
                ticker, names = pair.split("=", 1)
                out[ticker.strip().upper()] = [n.strip().lower() for n in names.split(";") if n.strip()]
        return out

    @property
    def ir_calendar_override_map(self) -> dict[str, tuple[str, str]]:
        out = {}
        for pair in self.ir_calendar_overrides.split(","):
            if "=" in pair and ":" in pair.split("=", 1)[1]:
                ticker, rest = pair.split("=", 1)
                platform, location = rest.split(":", 1)
                out[ticker.strip().upper()] = (platform.strip(), location.strip())
        return out

    @property
    def cik_predecessor_map(self) -> dict[str, str]:
        out = {}
        for pair in self.cik_predecessors.split(","):
            if ":" in pair:
                t, cik = pair.split(":", 1)
                out[t.strip().upper()] = cik.strip().zfill(10)
        return out

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def warm_ticker_list(self) -> list[str]:
        return [t.strip().upper() for t in self.warm_tickers.split(",") if t.strip()]


settings = Settings()
