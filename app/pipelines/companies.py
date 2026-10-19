"""The company universe, from SEC's filer directory, with the names headlines use."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Company
from app.sources import sec

# Legal-form words a headline never uses: "Apple Inc." is "Apple" (from loom).
_CORPORATE_SUFFIXES = re.compile(
    r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|"
    r"group|technologies|technology|llc|lp|nv|sa|ag|se|trust)\b\.?",
    re.IGNORECASE,
)
_SKIP_SUFFIXES = ("-W", "-WS", "-U", "-R", "-RT", "-P")


def core_name(company_name: str) -> str:
    """'Apple Inc.' -> 'apple'. The form a headline would actually use."""
    cleaned = _CORPORATE_SUFFIXES.sub(" ", company_name)
    cleaned = re.sub(r"[^\w\s&]", " ", cleaned)
    core = " ".join(cleaned.split()).lower()
    return core if len(core) >= 3 else company_name.strip().lower()


def aliases_for(name: str) -> list[str]:
    core = core_name(name)
    out = {core}
    # "JPMorgan Chase & Co" is "JPMorgan" in a headline; "Bank of America
    # Corporation" stays whole. Take the first word only when it is distinctive.
    first = core.split()[0] if core.split() else core
    if len(first) >= 5 and first not in {"first", "american", "united", "general", "national", "global", "international"}:
        out.add(first)
    return sorted(out)


def sync_directory(db: Session) -> int:
    """Insert or update every listed SEC filer. Returns rows touched."""
    rows = sec.fetch_directory()
    existing = {c.ticker: c for c in db.execute(select(Company)).scalars()}
    touched = 0
    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        if not ticker or len(ticker) > 12 or any(ticker.endswith(s) for s in _SKIP_SUFFIXES):
            continue
        cik10 = str(row["cik"]).zfill(10)
        company = existing.get(ticker)
        if company is None:
            company = Company(ticker=ticker, cik=cik10, name=row.get("name") or ticker,
                              exchange=row.get("exchange"), aliases="|".join(aliases_for(row.get("name") or ticker)))
            db.add(company)
            existing[ticker] = company
            touched += 1
        elif company.cik != cik10 or company.name != row.get("name"):
            if company.cik != cik10:
                company.predecessor_cik = company.cik
            company.cik, company.name, company.exchange = cik10, row.get("name") or ticker, row.get("exchange")
            company.aliases = "|".join(aliases_for(company.name))
            touched += 1
    for ticker, old_cik in settings.cik_predecessor_map.items():
        company = existing.get(ticker)
        if company is not None and company.cik != old_cik and company.predecessor_cik != old_cik:
            company.predecessor_cik = old_cik
            touched += 1
    db.commit()
    return touched


def is_about(headline: str, company: Company) -> bool:
    """True when the headline names the company (loom's rule: headline only)."""
    if not headline:
        return False
    low = headline.lower()
    for alias in (company.aliases or "").split("|"):
        if alias and re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", low):
            return True
    return re.search(rf"\b{re.escape(company.ticker)}\b", headline) is not None
