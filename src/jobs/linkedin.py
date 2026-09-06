"""LinkedIn guest endpoints: pure HTML parsers and a thin httpx client.

No login, no browser. The guest search and jobPosting endpoints return HTML
fragments. Pagination step is derived at runtime from the first page's card
count; measured live 2026-09-05 as 10 (start=0, 10, 25 returned disjoint
10-card pages). f_E=2,3,4 did not change the result set, so it is kept as a
harmless hint. In past_24h nothing falls out of the date window, so pagination
is bounded by max_pages and by a shared seen-set that stops a keyword once a
page adds no new id (the keywords overlap heavily)."""

import logging
import random
import re
import time
from html import unescape

import httpx
from bs4 import BeautifulSoup

from . import profile

from .models import Card, Detail

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_URN_RE = re.compile(r"urn:li:jobPosting:(\d+)")
_CRITERIA_FIELDS = {
    "Seniority level": "seniority_level",
    "Employment type": "employment_type",
    "Job function": "job_function",
    "Industries": "industries",
}


class Blocked(Exception):
    """LinkedIn returned a bot-block signal (429/403/999/authwall/5xx)."""


class BudgetExhausted(Exception):
    """The per-run request cap was reached."""


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text()).strip() if node else ""


def parse_cards(html: str, keyword: str) -> list[Card]:
    """Parse a guest search fragment into Cards. Returns [] for empty or
    card-less markup; never raises."""
    if not html:
        return []
    cards: list[Card] = []
    soup = BeautifulSoup(html, "html.parser")
    for li in soup.find_all("li"):
        urn_node = li.find(attrs={"data-entity-urn": True})
        if urn_node is None:
            continue
        m = _URN_RE.search(urn_node["data-entity-urn"])
        if not m:
            continue
        job_id = m.group(1)
        time_node = li.find("time")
        date_posted = time_node.get("datetime", "") if time_node else ""
        if not date_posted:
            log.warning("card %s has no datetime; skipping", job_id)
            continue
        cards.append(
            Card(
                job_id=job_id,
                title=_text(li.find("h3", class_="base-search-card__title")),
                company=_text(li.find("h4", class_="base-search-card__subtitle")),
                location=_text(li.find("span", class_="job-search-card__location")),
                date_posted=date_posted,
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                keyword=keyword,
            )
        )
    return cards


def _description_text(markup) -> str:
    """Flatten the description markup to text with newlines at block
    boundaries and blank-line runs collapsed."""
    for br in markup.find_all("br"):
        br.replace_with("\n")
    for block in markup.find_all(["p", "li", "div"]):
        block.append("\n")
    text = unescape(markup.get_text())
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_detail(html: str, job_id: str) -> Detail:
    """Parse a jobPosting fragment into a Detail. Missing elements become "".
    Never raises."""
    detail = Detail(job_id=job_id)
    if not html:
        return detail
    soup = BeautifulSoup(html, "html.parser")
    for header in soup.find_all("h3", class_="description__job-criteria-subheader"):
        field = _CRITERIA_FIELDS.get(_text(header))
        if not field:
            continue
        value = header.find_next("span", class_="description__job-criteria-text")
        setattr(detail, field, _text(value))
    detail.posted_text = _text(soup.find("span", class_="posted-time-ago__text"))
    detail.location = _text(soup.find("span", class_="topcard__flavor--bullet"))
    markup = soup.find("div", class_="show-more-less-html__markup")
    if markup is not None:
        detail.description_html = markup.decode_contents()
        detail.description_text = _description_text(markup)
    return detail


def load_keywords(path: str = "data/keywords.toml") -> tuple[list[str], dict]:
    """The keyword list and search config only; the full profile (title,
    relevance, and area lists) loads via profile.load()."""
    prof = profile.load(path)
    return list(prof.keywords), prof.search


class GuestClient:
    # ponytail: fixed pause window and single global request cap; make them
    # per-keyword if LinkedIn starts throttling per query.
    def __init__(
        self,
        pause: tuple[float, float] = (3.0, 6.0),
        request_cap: int = 400,
        block_limit: int = 3,
        client: httpx.Client | None = None,
        search_config: dict | None = None,
    ):
        self.pause = pause
        self.request_cap = request_cap
        self.block_limit = block_limit
        self.search_config = search_config or load_keywords()[1]
        self._client = client or httpx.Client(
            headers=DEFAULT_HEADERS, follow_redirects=False, timeout=30.0
        )
        self._requests = 0
        self._consecutive_blocks = 0

    def close(self) -> None:
        self._client.close()

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        for attempt in range(3):
            if self._requests >= self.request_cap:
                raise BudgetExhausted(f"request cap {self.request_cap} reached")
            self._requests += 1
            time.sleep(random.uniform(*self.pause))
            resp = self._client.get(url, params=params)
            status = resp.status_code
            if status == 200:
                self._consecutive_blocks = 0
                return resp
            blocked = status in (429, 403, 999)
            if 300 <= status < 400:
                loc = resp.headers.get("location", "").lower()
                if any(x in loc for x in ("authwall", "login", "signup")):
                    blocked = True
            if blocked:
                self._consecutive_blocks += 1
                if self._consecutive_blocks >= self.block_limit:
                    raise Blocked(f"blocked: HTTP {status}")
                time.sleep(min(120, 15 * 2**attempt))
                continue
            if 500 <= status < 600:
                time.sleep(min(120, 15 * 2**attempt))
                continue
            raise Blocked(f"unexpected HTTP {status}")
        raise Blocked("blocked after 3 attempts")

    def _search_params(self, keyword: str, date_window: str, start: int) -> dict:
        """The guest-search query. Every f_* key in [search] is forwarded (so
        f_JT, f_E, and any added f_WT etc. reach LinkedIn) except the two
        f_TPR_* window tokens, one of which becomes f_TPR for this run."""
        cfg = self.search_config
        params = {
            "keywords": keyword,
            "location": cfg["location"],
            "f_TPR": cfg[f"f_TPR_{date_window}"],
            "sortBy": cfg["sortBy"],
            "start": start,
        }
        params.update(
            {k: v for k, v in cfg.items() if k.startswith("f_") and not k.startswith("f_TPR")}
        )
        return params

    def search(self, keyword: str, date_window: str, start: int) -> list[Card]:
        resp = self._get(SEARCH_URL, self._search_params(keyword, date_window, start))
        return parse_cards(resp.text, keyword)

    def search_all(
        self,
        keyword: str,
        date_window: str,
        run_date,
        max_pages: int,
        seen: set[str] | None = None,
    ) -> list[Card]:
        """Page through a keyword's results, returning only cards whose job_id
        is not already in `seen` (pass one shared set across keywords to skip
        the heavy overlap between them). Stops on an empty page, on a page that
        adds no new id, on a page where every card is outside the date window
        (results are newest first), or after max_pages."""
        from .filters import date_window as date_window_rule

        results: list[Card] = []
        if seen is None:
            seen = set()
        step: int | None = None
        start = 0
        for _ in range(max_pages):
            cards = self.search(keyword, date_window, start)
            if not cards:
                break
            if step is None:
                step = len(cards)
            new_on_page = 0
            for card in cards:
                if card.job_id not in seen:
                    seen.add(card.job_id)
                    results.append(card)
                    new_on_page += 1
            if new_on_page == 0:
                break
            if all(date_window_rule(c, run_date) for c in cards):
                break
            start += step
        return results

    def detail(self, job_id: str) -> tuple[Detail, str]:
        """Fetch a jobPosting page. Returns (parsed Detail, raw HTML body) so
        the caller can persist the raw body."""
        resp = self._get(DETAIL_URL.format(job_id=job_id))
        return parse_detail(resp.text, job_id), resp.text
