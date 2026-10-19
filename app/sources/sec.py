"""SEC EDGAR: the filer directory, submissions, XBRL company facts and 8-K exhibits.

Everything here is public-domain data from sec.gov, fetched through the shared
per-host limiter (SEC's fair-access ceiling is per process, not per caller).
"""

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from app.config import settings
from app.sources.rate_limit import limiter

logger = logging.getLogger(__name__)

DIRECTORY_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

ET = ZoneInfo("America/New_York")

_DOCUMENT_ENTRY_RE = re.compile(r"<TYPE>([^\r\n<]+)\s*<SEQUENCE>([^\r\n<]+)\s*<FILENAME>([^\r\n<]+)")
_EXHIBIT_99_RE = re.compile(r"^EX-99(\.\d+)?$", re.IGNORECASE)
_NOISE_SELECTOR = 'script, style, ix\\:header, [style*="display:none"], [style*="display: none"]'

_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={"User-Agent": settings.sec_edgar_user_agent},
            timeout=30.0,
            follow_redirects=True,
        )
    return _client


def _get(url: str) -> httpx.Response | None:
    try:
        limiter.acquire(url)
        resp = client().get(url)
    except httpx.HTTPError:
        logger.warning("SEC: request failed for %s", url, exc_info=True)
        return None
    if resp.status_code != 200:
        logger.info("SEC: %s returned %s", url, resp.status_code)
        return None
    # SEC's fair-access block page can arrive with a 200.
    if "Request Rate Threshold Exceeded" in resp.text[:2000] or "Undeclared Automated Tool" in resp.text[:2000]:
        logger.warning("SEC: rate threshold page for %s", url)
        return None
    return resp


def fetch_directory() -> list[dict]:
    resp = _get(DIRECTORY_URL)
    if resp is None:
        return []
    payload = resp.json()
    return [dict(zip(payload["fields"], row)) for row in payload["data"]]


def fetch_submissions(cik10: str) -> dict | None:
    resp = _get(SUBMISSIONS_URL.format(cik10=cik10))
    return resp.json() if resp is not None else None


def fetch_companyfacts(cik10: str) -> dict | None:
    resp = _get(COMPANYFACTS_URL.format(cik10=cik10))
    return resp.json() if resp is not None else None


_ACCEPTED_RE = re.compile(r"<ACCEPTANCE-DATETIME>(\d{14})")


def acceptance_from_header(header: str) -> datetime | None:
    """Eastern acceptance time from a filing's SEC header.

    The submissions API's `acceptanceDateTime` cannot be used for this: it is
    labelled UTC, and measured against the filing index pages it is true UTC
    for some filers (Netflix, Intel: 20:03Z for 16:03 ET) and shifted by the
    Eastern offset a second time for others (Apple, Meta, JPMorgan, PepsiCo:
    00:30Z for 16:30 ET). The SEC header carries the acceptance time in
    Eastern, which is what the index page displays.
    """
    m = _ACCEPTED_RE.search(header)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=ET)


@dataclass(frozen=True)
class Filing:
    cik10: str
    accession: str
    form: str
    filing_date: str
    report_date: str
    items: tuple[str, ...]
    primary_document: str

    @property
    def base_url(self) -> str:
        return f"{ARCHIVES_BASE}/{int(self.cik10)}/{self.accession.replace('-', '')}"

    @property
    def index_url(self) -> str:
        return f"{self.base_url}/{self.accession}-index.htm"


def recent_filings(submissions: dict) -> list[Filing]:
    cik10 = str(submissions.get("cik", "")).zfill(10)
    recent = submissions.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        # A merged predecessor's filings sit under its own CIK in the archive.
        cik_for_row = recent.get("_cik", [cik10] * len(recent["form"]))[i] or cik10
        out.append(Filing(
            cik10=cik_for_row,
            accession=recent["accessionNumber"][i],
            form=form,
            filing_date=recent["filingDate"][i],
            report_date=recent["reportDate"][i] or "",
            items=tuple(x.strip() for x in (recent["items"][i] or "").split(",") if x.strip()),
            primary_document=recent["primaryDocument"][i] or "",
        ))
    return out


_CURRENT_FEED = "https://www.sec.gov/cgi-bin/browse-edgar"
_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_TITLE_RE = re.compile(r"^\s*(?P<form>.+?)\s+-\s+(?P<name>.*?)\s+\((?P<cik>\d{10})\)")


def current_filings(form_type: str = "8-K", count: int = 100) -> list[tuple[str, str]]:
    """(cik10, form) for filings EDGAR accepted most recently, newest first.

    One request covers the whole market, so polling it every minute costs the
    same for one company as for a thousand (loom's sec_realtime, condensed).
    """
    import xml.etree.ElementTree as ET

    resp = _get(f"{_CURRENT_FEED}?action=getcurrent&type={form_type}&company=&dateb=&owner=include&count={count}&output=atom")
    if resp is None:
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return []
    out = []
    for entry in root.findall("a:entry", _ATOM):
        m = _TITLE_RE.match(entry.findtext("a:title", default="", namespaces=_ATOM) or "")
        if m:
            out.append((m.group("cik"), m.group("form").strip()))
    return out


@dataclass(frozen=True)
class FilingHeader:
    accepted_et: datetime | None
    exhibit_urls: list[str]


def filing_header(filing: Filing) -> FilingHeader | None:
    """Acceptance time and EX-99 exhibits (release first, then supplements) from the SEC header.

    Some issuers (Bank of America) put the consolidated figures in images
    inside EX-99.1 and the text in EX-99.2/99.3, so all EX-99s are listed.
    """
    resp = _get(f"{filing.base_url}/{filing.accession}-index-headers.html")
    if resp is None:
        return None
    header = html.unescape(resp.text)
    urls = [
        f"{filing.base_url}/{filename.strip()}"
        for exhibit_type, _seq, filename in _DOCUMENT_ENTRY_RE.findall(header)
        if _EXHIBIT_99_RE.match(exhibit_type.strip()) and filename.strip().lower().endswith((".htm", ".html", ".txt"))
    ]
    return FilingHeader(accepted_et=acceptance_from_header(header), exhibit_urls=urls)


def fetch_html(url: str) -> str | None:
    resp = _get(url)
    return resp.text if resp is not None else None


def html_to_text(raw_html: str) -> str:
    tree = HTMLParser(raw_html)
    if tree.body is None:
        return ""
    for node in tree.css(_NOISE_SELECTOR):
        node.decompose()
    return tree.body.text(separator="\n", strip=True)
