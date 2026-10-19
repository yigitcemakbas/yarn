"""Company investor-relations event calendars: the company's own published date.

Two readers share the confirmation logic in pipelines/earnings_dates.py:

  q4      Q4 Inc's JSON event feed (most precise: date, time and zone)
  page    the IR site's own events page, read as text: an earnings title
          with a date beside it, and a time when one is printed


Finnhub's upcoming earnings dates are company-confirmed. The most direct free
source of the same fact is the company's IR site, where it lists its next
earnings call. Many US issuers run their IR site on Q4 Inc's platform, which
serves the event list as JSON at `/feed/Event.svc/GetEventList` on the
company's own IR host. That is the feed the site's own pages call.

Checked against Finnhub on 2026-10-19: Apple (11/02), Meta (10/21), Netflix
(10/20), Caterpillar (10/29) and SoFi (10/27) all matched Finnhub's date, where
yarn's cadence projection had been one to seven days off.

Conduct: robots.txt is honoured for the feed path, requests identify yarn
honestly, and each host is asked at most a few times a day, with at least
ten seconds between requests to one host (the common `Crawl-delay`).
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.sources.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Q4 pages embed a public client key in their HTML and send it with this
# request. Measured on 2026-10-19, the feed returned the same events for the
# key on Boeing's page and for other well-formed keys, on every Q4 host tried,
# so one key is sent rather than scraping each site's page for its own.
_Q4_KEY = "BF185719B0464B3CB809D23926182246"
_Q4_EVENTS = (
    "https://{host}/feed/Event.svc/GetEventList?apiKey=" + _Q4_KEY +
    "&LanguageId=1&eventSelection=1&eventDateFilter=1&includeFinancialReports=false"
    "&includePresentations=false&includePressReleases=false&sortOperator=1&pageSize=25"
    "&pageNumber=0&tagList=&includeTags=true&year=-1&excludeSelection=1"
)

# "PT", "PST", "PDT", "Pacific" -> Los Angeles, and so on. Q4 sites write
# "EST" all year to mean Eastern, so the letter decides, not the suffix.
_ZONES = {"E": "America/New_York", "C": "America/Chicago", "M": "America/Denver", "P": "America/Los_Angeles"}

_EARNINGS_TITLE = re.compile(r"(?i)\b(earnings|results|quarter|q[1-4]|[1-4]q)\b")
_NOT_EARNINGS = re.compile(
    r"(?i)annual (?:general |shareholders?' |stockholders?' )?meeting|investor day|analyst day|capital markets day"
    r"|summit|forum|fireside|(?<!earnings )(?<!call )conference(?! call)|symposium|tour|presentation at"
)

# Hosts an earnings release names as its IR site, and the company domains it
# mentions (from which investor./investors./ir. hosts are guessed).
_IR_HOST = re.compile(
    r"(?i)\b((?:investors?|ir|stock|shareholders?|investor-relations)\.[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co))\b"
)
_DOMAIN = re.compile(r"(?i)\b(?:https?://)?(?:www\.)?([a-z0-9-]{2,}\.(?:com|net|org|io|co|xyz))\b")
# "www.amazon.com/ir": an IR link that redirects to the real IR host (ir.aboutamazon.com).
_IR_LINK = re.compile(r"(?i)\b((?:www\.)?[a-z0-9-]{2,}\.(?:com|net|org|io|co|xyz))/(ir|investors?|investor-relations)\b")
_SKIP_DOMAINS = re.compile(
    r"(?i)^(sec\.gov|prnewswire\.com|businesswire\.com|globenewswire\.com|nasdaq\.com|nyse\.com|"
    r"twitter\.com|x\.com|linkedin\.com|facebook\.com|youtube\.com|instagram\.com|gcs-web\.com|q4cdn\.com|"
    r"q4inc\.com|zoom\.us|webcaster\.com|edge\.media-server\.com|events\.q4inc\.com|gaap\.com)$"
)


@dataclass(frozen=True)
class IrEvent:
    title: str
    start_et: datetime


def ir_links(text: str) -> list[str]:
    """IR links a release names on the company's main domain, to be resolved through redirects."""
    return list(dict.fromkeys(f"https://{d.lower()}/{path.lower()}" for d, path in _IR_LINK.findall(text)
                              if not _SKIP_DOMAINS.match(d.lower().removeprefix("www."))))[:3]


def candidate_hosts(text: str) -> list[str]:
    """IR hosts worth probing, most likely first: those the release names, then guesses.

    Some IR sites sit on the bare domain (Alphabet's abc.xyz), so the domain
    itself is a candidate after the usual prefixes.
    """
    named = list(dict.fromkeys(h.lower() for h in _IR_HOST.findall(text)))
    domains = [d.lower() for d in _DOMAIN.findall(text)]
    domains = [d for d in dict.fromkeys(domains) if not _SKIP_DOMAINS.match(d)]
    guesses = [f"{prefix}.{d}" for d in domains[:3] for prefix in ("investor", "investors", "ir", "shareholder")]
    guesses += [h for d in domains[:2] for h in (d, f"www.{d}")]
    return list(dict.fromkeys(named + guesses))[:14]


def parse_q4_time(start: str, zone: str) -> datetime | None:
    """Q4's "MM/DD/YYYY HH:MM:SS" in the event's zone, as Eastern time."""
    try:
        naive = datetime.strptime(start.strip(), "%m/%d/%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    letter = (zone or "E").strip()[:1].upper()
    tz = ZoneInfo(_ZONES.get(letter, "America/New_York")) if letter in _ZONES else (
        ZoneInfo("UTC") if (zone or "").upper() in ("UTC", "GMT") else ET)
    return naive.replace(tzinfo=tz).astimezone(ET)


def is_earnings_event(title: str) -> bool:
    return bool(_EARNINGS_TITLE.search(title)) and not _NOT_EARNINGS.search(title)


def parse_q4_events(payload: str) -> list[IrEvent] | None:
    """Earnings events from a Q4 GetEventList response; None when it is not one."""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    rows = data.get("GetEventListResult") if isinstance(data, dict) else None
    if rows is None:
        return None
    out = []
    for row in rows:
        title = " ".join((row.get("Title") or "").split())
        start = parse_q4_time(row.get("StartDate") or "", row.get("TimeZone") or "")
        if title and start and is_earnings_event(title):
            out.append(IrEvent(title=title, start_et=start))
    return out


class IrCalendars(BaseScraper):
    def __init__(self, **kwargs):
        super().__init__(request_interval=10.0, **kwargs)

    def q4_events(self, host: str) -> list[IrEvent] | None:
        """Upcoming earnings events from a Q4 IR host, or None when the host has no Q4 feed."""
        payload = self.fetch_html(_Q4_EVENTS.format(host=host))
        if payload is None:
            return None
        return parse_q4_events(payload)

    def answers(self, host: str) -> bool:
        """Whether a host serves anything at all (its home page), so page probes skip dead guesses."""
        url = f"https://{host}/"
        if not self.robots.can_fetch(url):
            return False
        self._throttle(url)
        try:
            return self._client.get(url).status_code < 400
        except Exception:
            return False

    def resolve_host(self, url: str) -> str | None:
        """Where an IR link lands after redirects, honouring robots.txt for the link itself."""
        if not self.robots.can_fetch(url):
            return None
        self._throttle(url)
        try:
            resp = self._client.get(url)
        except Exception:
            logger.info("Could not resolve IR link %s", url, exc_info=True)
            return None
        return resp.url.host.lower() if resp.status_code < 400 or resp.url.host else None

    def discover(self, text: str) -> tuple[str, str] | None:
        """(platform, location) of the first event source found: a Q4 host, else an events page URL."""
        resolved = [h for h in (self.resolve_host(u) for u in ir_links(text)) if h]
        hosts = list(dict.fromkeys(resolved + candidate_hosts(text)))
        for host in hosts:
            if self.q4_events(host) is not None:
                return "q4", host
        for host in [h for h in hosts[:6] if self.answers(h)][:3]:
            for path in EVENT_PAGE_PATHS:
                url = f"https://{host}{path}"
                if self.page_events(url):
                    return "page", url
        return None

    def page_events(self, url: str) -> list[IrEvent]:
        page = self.fetch_html(url)
        return parse_event_page(_page_text(page)) if page else []


# ---- IR events pages ---------------------------------------------------------------------

EVENT_PAGE_PATHS = ("/news-events/ir-calendar", "/news-events/events", "/events-and-presentations", "/events")
_MONTH_DATE = re.compile(
    r"(?i)\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d\d)\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")
_TIME = re.compile(
    r"(?i)\b(\d{1,2}):(\d{2})\s*([ap])\.?\s?m\.?\s*(?:\(?\s*(E|C|M|P)[a-z]*\.?\s*[ST]?\s*\)?)?")


def _page_date(text: str):
    m = _MONTH_DATE.search(text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
        except ValueError:
            return None
    m = _NUMERIC_DATE.search(text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
        except ValueError:
            return None
    return None


_ZONE_WORD = re.compile(r"(?i)US/(Eastern|Central|Mountain|Pacific)|\b(E|C|M|P)(?:S|D)?T\b|\b(Eastern|Central|Mountain|Pacific)\b")


def _row_zone(span: str, default: ZoneInfo) -> ZoneInfo:
    m = _ZONE_WORD.search(span)
    if not m:
        return default
    letter = next(g for g in m.groups() if g)[0].upper()
    return ZoneInfo(_ZONES.get(letter, "America/New_York"))


def _pairings(lines: list[str], titles: list[int], dates: dict[int, object], orientation: str) -> dict[int, int]:
    """Title line -> its date line, taking dates only from the title's own side of the row."""
    out = {}
    title_set = set(titles)
    for i in titles:
        rng = range(i - 1, max(-1, i - 7), -1) if orientation == "before" else range(i + 1, min(len(lines), i + 7))
        for j in rng:
            if j in title_set:
                break
            if j in dates:
                out[i] = j
                break
    return out


def parse_event_page(text: str) -> list[IrEvent]:
    """Earnings events from an IR events page's text (one cell or line per row).

    Layouts differ: some pages print each row's date before its title
    (Walmart: date | time | zone | title), others after it (Intel). The page's
    orientation is the one that pairs its titles and dates most regularly: a
    list repeats one row shape, so the true pairing has near-constant gaps
    while the wrong one picks up neighbouring rows at varying distances. The
    call time and zone come from within the row. With no time, the event sits
    at noon Eastern, which call_hour reads as "no hour stated".
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    dates = {j: d for j, ln in enumerate(lines) if (d := _page_date(ln)) is not None}
    titles = [i for i, ln in enumerate(lines) if len(ln) <= 140 and is_earnings_event(ln) and _page_date(ln) is None]
    if not titles or not dates:
        return []

    def score(orientation: str):
        pairs = _pairings(lines, titles, dates, orientation)
        gaps = [abs(i - j) for i, j in pairs.items()]
        if not gaps:
            return (len(titles), 0.0, 0.0), pairs
        mean = sum(gaps) / len(gaps)
        variance = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        return (len(titles) - len(pairs), variance, mean), pairs

    (_, pairs) = min((score("before"), score("after")), key=lambda x: x[0])
    out: list[IrEvent] = []
    seen = set()
    for i, j in sorted(pairs.items()):
        day = dates[j]
        if day in seen:
            continue
        lo, hi = min(i, j), max(i, j)
        span = " | ".join(lines[lo:hi + 1] + ([lines[hi + 1]] if hi + 1 < len(lines) and hi + 1 not in dates else []))
        tz, hour, minute = ET, 12, 0
        tm = _TIME.search(span)
        if tm:
            hour = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "p" else 0)
            minute = int(tm.group(2))
            tz = ZoneInfo(_ZONES[tm.group(4).upper()]) if tm.group(4) else _row_zone(span, ET)
        seen.add(day)
        out.append(IrEvent(title=lines[i], start_et=datetime(day.year, day.month, day.day, hour, minute,
                                                            tzinfo=tz).astimezone(ET)))
    return out


def _page_text(page: str) -> str:
    from app.sources.sec import html_to_text

    return html_to_text(page)
