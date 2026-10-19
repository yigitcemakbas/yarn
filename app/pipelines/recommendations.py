"""Monthly recommendation counts from broker rating actions read in headlines.

Finnhub's counts come from its own licensed analyst panel, and the Step 0b
audit (docs/audits/estimates-and-recommendations.md) found no free source that
reproduces them. yarn builds the same five-bucket shape from what it can see:
rating actions reported in headlines ("Morgan Stanley upgrades Nvidia to
Overweight, PT $250").

For each month, a firm counts once, in the bucket of its most recent rating
issued in the twelve months before that month ended. Coverage is partial (only
actions that make headlines), so each row says how many firms it rests on.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RatingAction

# A firm's rating stays current this long after it was issued.
CARRY_FORWARD_DAYS = 365

# Broker vocabulary to Finnhub's five buckets. Longest phrases first, so
# "strong buy" wins over "buy" and "market perform" over "perform".
_BUCKETS: list[tuple[str, str]] = [
    ("strong buy", "strongBuy"), ("conviction buy", "strongBuy"), ("top pick", "strongBuy"),
    ("strong sell", "strongSell"),
    ("sector outperform", "buy"), ("market outperform", "buy"), ("outperform", "buy"),
    ("overweight", "buy"), ("accumulate", "buy"), ("add", "buy"), ("positive", "buy"), ("buy", "buy"),
    ("sector perform", "hold"), ("market perform", "hold"), ("peer perform", "hold"),
    ("equal weight", "hold"), ("equal-weight", "hold"), ("in-line", "hold"), ("in line", "hold"),
    ("sector weight", "hold"), ("market weight", "hold"), ("neutral", "hold"), ("hold", "hold"),
    ("perform", "hold"),
    ("underperform", "sell"), ("underweight", "sell"), ("reduce", "sell"), ("negative", "sell"),
    ("sell", "sell"),
]

_ACTIONS = r"(?P<action>upgrades?|downgrades?|initiates?|initiated|starts?|resumes?|reiterates?|maintains?|reaffirms?|assumes?|raises?|lifts?|lowers?|cuts?|boosts?)"
_RATING = r"(?P<rating>strong buy|strong sell|conviction buy|top pick|sector outperform|market outperform|sector perform|market perform|peer perform|equal[- ]weight|sector weight|market weight|in[- ]line|outperform|underperform|overweight|underweight|accumulate|neutral|positive|negative|reduce|perform|hold|buy|sell|add)"
_TARGET = r"(?:.*?(?:price target|PT|target)\D{0,12}\$(?P<target>\d[\d,]*(?:\.\d+)?))?"

# "Morgan Stanley upgrades Nvidia to Overweight, PT $250"
# "Nvidia upgraded to Buy at Goldman Sachs"
# "Goldman Sachs initiates coverage of Apple with Buy rating"
_FIRM_FIRST = re.compile(
    rf"^(?P<firm>[A-Z][\w&.,' -]{{1,60}}?)\s+{_ACTIONS}\b.*?\b(?:to|at|with|as|an?)\s+(?:an?\s+)?['\"]?{_RATING}\b{_TARGET}",
    re.IGNORECASE,
)
_SUBJECT_FIRST = re.compile(
    rf"(?P<action>upgraded|downgraded|initiated|reiterated|maintained|started)\s+(?:to|at|with|as)\s+(?:an?\s+)?['\"]?{_RATING}\b.*?\b(?:at|by)\s+(?P<firm>[A-Z][\w&.' -]{{1,60}}?)(?:[;,.:]|\s+-|\s+on\b|$){_TARGET}",
    re.IGNORECASE,
)
# "JPMorgan reiterates Overweight rating on Microsoft"
# "BofA Securities raises Apple price target to $300, keeps Buy"
_FIRM_KEEPS = re.compile(
    rf"^(?P<firm>[A-Z][\w&.,' -]{{1,60}}?)\s+(?:(?P<action>reiterates?|maintains?|reaffirms?)\s+(?:its\s+|an?\s+)?['\"]?{_RATING}\b"
    rf"|.*?\b(?:keeps|maintains|reiterates)\s+(?:an?\s+)?['\"]?{_RATING.replace('rating>', 'rating2>')}\b)",
    re.IGNORECASE,
)
_ANY_TARGET = re.compile(r"(?:price target|PT|target)\D{0,12}\$(\d[\d,]*(?:\.\d+)?)", re.IGNORECASE)

# Headlines that look like rating actions but are not a firm's rating.
_NOT_A_FIRM = re.compile(r"^(analysts?|wall street|investors?|traders?|shares?|stock|the)\b", re.IGNORECASE)


# One firm, several spellings. Counting "BofA" and "BofA Securities" as two
# firms would double their vote.
_FIRM_SUFFIX = re.compile(
    r"\s+(securities|capital markets|capital|global markets|research|& co\.?|and company|llc|inc\.?|group|partners|equity research)$",
    re.IGNORECASE,
)
_FIRM_ALIASES = {
    "bank of america": "BofA", "bofa": "BofA", "bofa global research": "BofA", "merrill lynch": "BofA",
    "jp morgan": "JPMorgan", "j.p. morgan": "JPMorgan", "jpmorgan chase": "JPMorgan",
    "goldman": "Goldman Sachs", "morgan stanley": "Morgan Stanley", "citigroup": "Citi", "citi": "Citi",
    "ubs": "UBS", "deutsche": "Deutsche Bank", "db": "Deutsche Bank", "rbc": "RBC", "rbc capital": "RBC",
    "td cowen": "TD Cowen", "cowen": "TD Cowen", "kbw": "KBW", "keefe bruyette": "KBW",
    "evercore isi": "Evercore ISI", "evercore": "Evercore ISI", "wells fargo": "Wells Fargo",
    "bernstein": "Bernstein", "ab bernstein": "Bernstein", "sanford c. bernstein": "Bernstein",
    "jefferies": "Jefferies", "barclays": "Barclays", "hsbc": "HSBC", "mizuho": "Mizuho",
    "piper sandler": "Piper Sandler", "raymond james": "Raymond James", "stifel": "Stifel",
    "wedbush": "Wedbush", "oppenheimer": "Oppenheimer", "needham": "Needham", "truist": "Truist",
    "bmo": "BMO", "bmo capital": "BMO", "scotiabank": "Scotiabank", "loop": "Loop Capital",
    "loop capital": "Loop Capital", "melius": "Melius", "rosenblatt": "Rosenblatt", "bernstein societe generale": "Bernstein",
}


def canonical_firm(name: str) -> str:
    n = " ".join(name.replace("’", "'").split()).strip(" ,.'\"-")
    prev = None
    while prev != n:
        prev, n = n, _FIRM_SUFFIX.sub("", n).strip()
    return _FIRM_ALIASES.get(n.lower(), n)


def bucket_for(rating: str) -> str | None:
    r = rating.lower().strip()
    for phrase, bucket in _BUCKETS:
        if r == phrase:
            return bucket
    return None


@dataclass(frozen=True)
class ParsedAction:
    firm: str
    action: str
    rating: str
    bucket: str
    price_target: float | None


def _normalise_action(word: str) -> str:
    w = word.lower()
    for stem, name in (("upgrad", "upgrade"), ("downgrad", "downgrade"), ("initiat", "init"), ("start", "init"),
                       ("resum", "init"), ("assum", "init")):
        if w.startswith(stem):
            return name
    return "main"


def parse_headline(headline: str) -> ParsedAction | None:
    """A rating action from one headline, or None when the headline is not one."""
    text = " ".join(headline.split())
    for pattern in (_FIRM_FIRST, _SUBJECT_FIRST):
        m = pattern.search(text)
        if not m:
            continue
        firm = canonical_firm(m.group("firm"))
        if _NOT_A_FIRM.match(firm) or len(firm) < 2:
            continue
        bucket = bucket_for(m.group("rating").replace("-", " ") if m.group("rating").lower().startswith("equal") else m.group("rating"))
        if bucket is None:
            continue
        target = m.group("target")
        return ParsedAction(
            firm=firm,
            action=_normalise_action(m.group("action")),
            rating=m.group("rating").title(),
            bucket=bucket,
            price_target=float(target.replace(",", "")) if target else None,
        )
    m = _FIRM_KEEPS.search(text)
    if m:
        firm = canonical_firm(m.group("firm"))
        rating = m.group("rating") or m.group("rating2")
        bucket = bucket_for(rating)
        if bucket and not _NOT_A_FIRM.match(firm):
            target = _ANY_TARGET.search(text)
            return ParsedAction(
                firm=firm,
                action="main",
                rating=rating.title(),
                bucket=bucket,
                price_target=float(target.group(1).replace(",", "")) if target else None,
            )
    return None


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _previous_month(d: date) -> date:
    return _month_start(_month_start(d) - timedelta(days=1))


def monthly_trend(db: Session, ticker: str, *, today: date, months: int) -> list[dict]:
    """Finnhub's `/stock/recommendation` rows, newest first.

    Finnhub's newest period is the last completed month, so on 19 October it
    is 1 September.
    """
    periods = []
    p = _previous_month(today)
    for _ in range(months):
        periods.append(p)
        p = _previous_month(p)

    earliest = periods[-1] - timedelta(days=CARRY_FORWARD_DAYS)
    actions = db.execute(
        select(RatingAction)
        .where(RatingAction.ticker == ticker, RatingAction.acted_on >= earliest)
        .order_by(RatingAction.acted_on.asc())
    ).scalars().all()
    if not actions:
        return []

    rows = []
    for period in periods:
        # The month is counted as of its end.
        month_end = _month_start(period + timedelta(days=32)) - timedelta(days=1)
        latest: dict[str, RatingAction] = {}
        for a in actions:
            if month_end - timedelta(days=CARRY_FORWARD_DAYS) < a.acted_on <= month_end:
                latest[a.firm.lower()] = a
        counts = {"strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}
        for a in latest.values():
            counts[a.bucket] += 1
        rows.append({
            "symbol": ticker,
            "period": period.isoformat(),
            **counts,
            "firmsTracked": len(latest),
            "yarnBasis": "headline-rating-actions",
        })
    if all(r["firmsTracked"] == 0 for r in rows):
        return []
    return rows
