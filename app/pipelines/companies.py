"""The company universe, from SEC's filer directory, with the names headlines use.

A company's `aliases` are the names a headline would call it, most
headline-like first. The first alias is what yarn searches news for, and every
alias counts when deciding whether a headline names the company:

  "JPMORGAN CHASE & CO"       -> jpmorgan, jpmorgan chase
  "ADVANCED MICRO DEVICES INC" -> amd (its ticker spells its initials), advanced micro devices
  "BANK OF AMERICA CORP /DE/"  -> bank of america, bofa (SEC's state suffix dropped)
  "Alphabet Inc."              -> google, alphabet (a brand override)

A first word is an alias only when it is distinctive: "Bank", "General" or
"Johnson" alone would match half the news.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Company
from app.sources import sec

# Legal-form words a headline never uses: "Apple Inc." is "Apple" (from loom).
_CORPORATE_SUFFIXES = re.compile(
    r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|"
    r"group|technologies|technology|llc|lp|nv|sa|ag|se|trust|platforms|communications)\b\.?",
    re.IGNORECASE,
)
# SEC's state-of-incorporation and other filer suffixes: "/DE/", "/NEW/".
_SEC_SUFFIX = re.compile(r"\s*/[A-Z]{2,}/?\s*$|\s*/[A-Z]{2,}/\s*")
_SKIP_SUFFIXES = ("-W", "-WS", "-U", "-R", "-RT", "-P")

# First words too common to stand for one company on their own.
_GENERIC_FIRST = {
    "first", "american", "united", "general", "national", "global", "international", "advanced", "bank", "new",
    "the", "royal", "southern", "northern", "western", "eastern", "pacific", "atlantic", "digital", "energy",
    "capital", "financial", "health", "home", "life", "power", "johnson", "walt", "citizens", "peoples", "central",
    "community", "federal", "mutual", "standard", "universal", "applied", "consolidated", "continental",
    "diversified", "public", "regional", "republic", "security", "union", "west", "east", "north", "south",
    "data", "great", "golden", "silver", "blue", "green", "red", "black", "white", "us", "usa", "america",
}

# Where headlines use a brand rather than the filer's name. Operators extend
# this through ALIAS_OVERRIDES ("TICKER=alias;alias,TICKER=alias").
BRAND_ALIASES = {
    "GOOGL": ["google", "alphabet"], "GOOG": ["google", "alphabet"],
    "META": ["meta", "facebook"], "DIS": ["disney"], "XOM": ["exxon", "exxon mobil", "exxonmobil"],
    "GE": ["ge aerospace", "general electric"], "KO": ["coca-cola", "coke"], "BAC": ["bank of america", "bofa"],
    "JPM": ["jpmorgan", "jp morgan", "jpmorgan chase"], "JNJ": ["johnson & johnson", "j&j"],
    "F": ["ford"], "PEP": ["pepsico", "pepsi"], "UNH": ["unitedhealth", "unitedhealthcare"], "BRK-B": ["berkshire hathaway", "berkshire"],
    "BRK-A": ["berkshire hathaway", "berkshire"], "WBA": ["walgreens"], "HD": ["home depot"],
    "LOW": ["lowe's", "lowes"], "MCD": ["mcdonald's", "mcdonalds"], "PG": ["procter & gamble", "p&g"],
    "T": ["at&t"], "MA": ["mastercard"], "V": ["visa"], "C": ["citigroup", "citi"], "GS": ["goldman sachs"],
    "MS": ["morgan stanley"], "WFC": ["wells fargo"],
}


def _strip_sec_suffix(name: str) -> str:
    return _SEC_SUFFIX.sub(" ", name).strip()


def core_name(company_name: str) -> str:
    """'Apple Inc.' -> 'apple', 'COCA COLA CO' -> 'coca cola'. Hyphens are kept: 'coca-cola'."""
    cleaned = _CORPORATE_SUFFIXES.sub(" ", _strip_sec_suffix(company_name))
    cleaned = re.sub(r"[^\w\s&'-]", " ", cleaned)
    core = " ".join(cleaned.split()).lower().strip(" &-")
    return core if len(core) >= 3 else company_name.strip().lower()


def _initials(core: str) -> str:
    return "".join(w[0] for w in core.replace("-", " ").split() if w[0].isalpha())


def aliases_for(name: str, ticker: str = "") -> list[str]:
    """The names a headline would call this company, most headline-like first."""
    out: list[str] = []
    overrides = {**BRAND_ALIASES, **settings.alias_override_map}
    out += overrides.get(ticker.upper(), [])
    core = core_name(name)
    words = core.split()
    if ticker and len(ticker) >= 3 and ticker.isalpha() and _initials(core).upper() == ticker.upper():
        out.append(ticker.lower())
    first = words[0] if words else core
    if len(words) > 1 and len(first) >= 4 and first not in _GENERIC_FIRST:
        out.append(first)
    out.append(core)
    return list(dict.fromkeys(a for a in out if a))


def _norm(text: str) -> str:
    return re.sub(r"[-‐-―]", " ", text)


def names_company(headline: str, company: Company) -> bool:
    """True when a headline names the company: an alias written as a proper noun, or its ticker.

    "Intel plant leak" names Intel; "no pause in US intel sharing" does not.
    Tickers of three or more letters count when written in capitals ("NVDA
    stock"); any ticker counts inside an exchange tag ("(NYSE: F)").
    """
    if not headline:
        return False
    text = _norm(headline)
    for alias in (company.aliases or "").split("|"):
        if not alias:
            continue
        for m in re.finditer(rf"(?<![\w]){re.escape(_norm(alias))}(?![\w])", text, re.IGNORECASE):
            if m.group(0)[0].isupper() or not m.group(0)[0].isalpha():
                return True
    ticker = company.ticker
    if len(ticker) >= 3 and re.search(rf"(?<![\w$]){re.escape(ticker)}(?![\w])", headline):
        return True
    return re.search(rf"(?:NASDAQ|NYSE|Nasdaq)\s*:\s*{re.escape(ticker)}\b", headline) is not None


def sync_directory(db: Session) -> int:
    """Insert or update every listed SEC filer, refreshing aliases. Returns rows touched."""
    rows = sec.fetch_directory()
    existing = {c.ticker: c for c in db.execute(select(Company)).scalars()}
    touched = 0
    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        if not ticker or len(ticker) > 12 or any(ticker.endswith(s) for s in _SKIP_SUFFIXES):
            continue
        cik10 = str(row["cik"]).zfill(10)
        name = row.get("name") or ticker
        aliases = "|".join(aliases_for(name, ticker))
        company = existing.get(ticker)
        if company is None:
            company = Company(ticker=ticker, cik=cik10, name=name, exchange=row.get("exchange"), aliases=aliases)
            db.add(company)
            existing[ticker] = company
            touched += 1
            continue
        if company.cik != cik10:
            company.predecessor_cik = company.cik
        if (company.cik, company.name, company.exchange, company.aliases) != (cik10, name, row.get("exchange"), aliases):
            company.cik, company.name, company.exchange, company.aliases = cik10, name, row.get("exchange"), aliases
            touched += 1
    for ticker, old_cik in settings.cik_predecessor_map.items():
        company = existing.get(ticker)
        if company is not None and company.cik != old_cik and company.predecessor_cik != old_cik:
            company.predecessor_cik = old_cik
            touched += 1
    db.commit()
    return touched
