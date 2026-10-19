"""Reported revenue, EPS and guidance from an earnings press release (8-K EX-99.1).

The press release lands on EDGAR minutes after the numbers are public, weeks
before the 10-Q's XBRL. It is the only free primary source that can match
Finnhub's same-day `epsActual` / `revenueActual`.

Finnhub reports EPS on the adjusted (non-GAAP) basis the market reacts to,
falling back to GAAP when a company reports no adjusted figure. So both are
read: GAAP diluted EPS from the income statement, adjusted EPS from the
release's own wording or reconciliation table.

The text arrives one table cell per line (see sec.html_to_text), so a row is
its label followed by its cells, current period first.
"""

import re
from dataclasses import dataclass

_NUM = re.compile(r"^\(?-?\$?\s*\(?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\)?$")
_FOOTNOTE = re.compile(r"^\(\d\)$|^\([a-z]\)$|^\*+$|^\d(?:,\d)+$|^\d$")

_REVENUE_LABELS = [
    r"total net sales", r"total net revenues?", r"total revenues?(?:, net)?", r"net revenues?",
    r"total revenues? and other income", r"revenues?, net", r"net sales", r"revenues?", r"total net revenue",
    r"total revenues?, net of interest expense", r"revenue",
]
_EPS_HEADER = re.compile(r"(?i)(earnings|income|net income|loss) per (common )?share|per share data|EPS")
_UNITS = re.compile(r"(?i)\((?:\$\s*)?in (millions|thousands|billions)|(?:dollars|amounts) in (millions|thousands|billions)|\$ in (millions|thousands|billions)")

_ADJ_EPS_SENTENCE = re.compile(
    r"(?i)(?:adjusted|non-gaap|core|operating)\s+(?:diluted\s+)?(?:earnings per (?:diluted\s+)?share|EPS)"
    r"(?:\s*\([^)]*\))?[\s\d,*]{0,8}(?:of|was|were|totaled|to|came in at|:)?\s*(?:a\s+(?:record\s+)?)?\$\s*(-?\d+\.\d{2})(?!\d\d)"
    r"|adjusted\s+(?:net\s+)?earnings\s+(?:of|were|was)\s+\$\s*(-?\d+\.\d{2})\d?\s+per\s+(?:diluted\s+)?share"
)
# "Revenues of $112.7 Billion", "Net revenue was $50.9 billion", "sales growth of 5.9% to $25.4 Billion"
_HEADLINE_REVENUE = re.compile(
    r"(?i)\b(?:reported\s+|total\s+|net\s+|consolidated\s+|quarterly\s+|managed\s+)?(?:revenues?|net revenue|net sales|sales)"
    r"(?:\s+\d{1,2}(?:,\d{1,2})*)?"
    r"(?:\s+growth\s+of\s+[\d.]+%\s+to|\s+of|\s+was|\s+were|\s+totaled|\s+(?:increased|rose|grew|decreased|declined|fell)[^$]{0,40}?to)"
    r"\s+\$\s*(\d+(?:\.\d+)?)\s*(billion|million)"
)
_GAAP_EPS_SENTENCE = re.compile(
    r"(?i)(?:diluted\s+)?(?:earnings per (?:diluted\s+)?share|EPS)(?:\s*\([^)]*\))?\s*(?:of|was|were|totaled|:)\s*\$\s*(-?\d+\.\d{2})(?!\d\d)"
)
_GUIDE_RANGE = re.compile(
    r"(?i)(?P<what>revenue|net sales|sales|(?:adjusted |non-gaap )?(?:diluted )?(?:eps|earnings per share))[^.$]{0,80}?"
    r"(?:between|in the range of|range of|of|to be)\s+\$\s*(?P<lo>\d+(?:\.\d+)?)\s*(?P<lo_unit>billion|million)?\s*"
    r"(?:to|and|-|–)\s*\$\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<hi_unit>billion|million)?"
)
_GUIDE_CONTEXT = re.compile(r"(?i)\b(outlook|guidance|expects?|anticipates?|forecasts?|projects?)\b")
_NEXT_PERIOD = re.compile(r"(?i)\b(next quarter|fourth quarter|third quarter|second quarter|first quarter|q[1-4]|fiscal (?:year )?\d{4}|full[- ]year)\b")
_FULL_YEAR = re.compile(r"(?i)\b(full[- ]year|fiscal (?:year )?\d{4}|for (?:the )?(?:fiscal )?year|annual)\b")


@dataclass
class ReleaseFigures:
    revenue: int | None = None
    eps_gaap: float | None = None
    eps_adjusted: float | None = None
    revenue_guidance_mid: int | None = None
    eps_guidance_mid: float | None = None


def _to_number(token: str) -> float | None:
    t = token.strip().replace("—", "").replace("–", "")
    if not t or t in ("$", "%"):
        return None
    m = _NUM.match(t)
    if not m:
        return None
    value = float(m.group(1).replace(",", "") + (m.group(2) or ""))
    return -value if t.startswith("(") or t.startswith("-") else value


def _first_number_after(lines: list[str], i: int, *, max_ahead: int = 6) -> float | None:
    for tok in lines[i + 1:i + 1 + max_ahead]:
        tok = tok.strip()
        if _FOOTNOTE.match(tok) or tok == "$":
            continue
        n = _to_number(tok)
        if n is not None:
            return n
        if tok and not tok.startswith("$"):
            return None
    return None


def _unit_multiplier(lines: list[str], i: int) -> int:
    for j in range(i, max(-1, i - 60), -1):
        m = _UNITS.search(lines[j])
        if m:
            unit = next(g for g in m.groups() if g).lower()
            return {"thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}[unit]
    return 1_000_000


def find_revenue(lines: list[str]) -> int | None:
    for label in _REVENUE_LABELS:
        pat = re.compile(rf"(?i)^{label}:?$")
        for i, line in enumerate(lines):
            if pat.match(line.strip()):
                value = _first_number_after(lines, i)
                if value is not None and value > 0:
                    return int(round(value * _unit_multiplier(lines, i)))
    return None


def find_gaap_eps(lines: list[str]) -> float | None:
    for i, line in enumerate(lines):
        label = line.strip().lower()
        if label in ("diluted", "diluted earnings per share", "earnings per share - diluted",
                     "net income per share - diluted", "diluted eps", "diluted net income per share",
                     "diluted earnings per common share", "net income per diluted share",
                     "earnings per diluted share", "diluted (in dollars per share)"):
            context = " ".join(lines[max(0, i - 8):i])
            if label == "diluted" and not _EPS_HEADER.search(context):
                continue
            value = _first_number_after(lines, i)
            if value is not None and abs(value) < 1000 and "." in next(
                (t for t in lines[i + 1:i + 7] if _to_number(t) is not None), ""
            ):
                return value
    return None


def find_adjusted_eps(text: str) -> float | None:
    m = _ADJ_EPS_SENTENCE.search(text)
    if not m:
        return None
    return float(m.group(1) or m.group(2))


_OUTLOOK_CUE = re.compile(r"(?i)guidance|outlook|expects?|expected|estimated|anticipat|forecast|full[- ]year|fiscal (?:year )?\d{4}|for the year")


def _in_outlook(text: str, pos: int) -> bool:
    """True when the figure at `pos` sits in a forward-looking sentence."""
    start = max(text.rfind(". ", 0, pos), text.rfind("•", 0, pos), pos - 200, 0)
    return bool(_OUTLOOK_CUE.search(text[start:pos]))


def find_headline_revenue(text: str, head_chars: int = 4000) -> int | None:
    """The consolidated revenue the release leads with, made exact from its tables.

    The headline rounds ("$50.9 billion"); the statements carry the exact
    figure ("50,907" in millions). The first table number that rounds to the
    headline is taken as the exact value.
    """
    # The largest figure the headline section names: the consolidated total is
    # never smaller than a segment's, and banks' managed revenue (the basis
    # the market quotes) is never smaller than reported.
    head = text[:head_chars]
    matches = [m for m in _HEADLINE_REVENUE.finditer(head) if not _in_outlook(head, m.start())]
    if not matches:
        return None

    def _scaled(m):
        return float(m.group(1)) * (1_000_000_000 if m.group(2).lower() == "billion" else 1_000_000)

    m = max(matches, key=_scaled)
    headline = _scaled(m)
    decimals = len(m.group(1).split(".")[1]) if "." in m.group(1) else 0
    tolerance = 0.5 * 10 ** (-decimals) * (1_000_000_000 if m.group(2).lower() == "billion" else 1_000_000)
    for tok in re.findall(r"\$?\s*(\d{1,3}(?:,\d{3})+)(?!\.\d)", text):
        value = int(tok.replace(",", ""))
        for mult in (1_000_000, 1_000):
            if abs(value * mult - headline) <= tolerance:
                return value * mult
    return int(round(headline))


def find_gaap_eps_sentence(text: str) -> float | None:
    m = _GAAP_EPS_SENTENCE.search(text)
    return float(m.group(1)) if m else None


def find_guidance(text: str) -> tuple[int | None, float | None]:
    """Midpoints of next-quarter revenue and EPS guidance, when the release gives a range.

    Full-year ranges are skipped: Finnhub's estimate fields are per quarter.
    """
    revenue_mid = eps_mid = None
    for sentence in re.split(r"(?<=[.;])\s+", text):
        if not _GUIDE_CONTEXT.search(sentence) or _FULL_YEAR.search(sentence):
            continue
        for m in _GUIDE_RANGE.finditer(sentence):
            lo, hi = float(m.group("lo")), float(m.group("hi"))
            if hi < lo:
                continue
            what = m.group("what").lower()
            if "eps" in what or "per share" in what:
                if eps_mid is None and hi < 1000:
                    eps_mid = round((lo + hi) / 2, 4)
            else:
                unit = (m.group("hi_unit") or m.group("lo_unit") or "").lower()
                mult = {"billion": 1_000_000_000, "million": 1_000_000}.get(unit)
                if revenue_mid is None and mult:
                    revenue_mid = int(round((lo + hi) / 2 * mult))
    return revenue_mid, eps_mid


def parse(text: str) -> ReleaseFigures:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    flat = " ".join(lines)
    gaap = find_gaap_eps(lines)
    if gaap is None:
        gaap = find_gaap_eps_sentence(flat)
    revenue_mid, eps_mid = find_guidance(flat)
    revenue = find_headline_revenue(flat)
    table_revenue = find_revenue(lines)
    # A table total that disagrees with the headline is usually a segment or a
    # subtotal; the headline names the consolidated figure.
    if revenue is None or (table_revenue and abs(table_revenue - revenue) / revenue < 0.0005):
        revenue = table_revenue or revenue
    return ReleaseFigures(
        revenue=revenue,
        eps_gaap=gaap,
        eps_adjusted=find_adjusted_eps(flat),
        revenue_guidance_mid=revenue_mid,
        eps_guidance_mid=eps_mid,
    )
