"""Notion REST client and property mapping (Notion-Version 2025-09-03).

The old pipeline talked to Notion over a hosted MCP that timed out; this is a
thin httpx client against the data-source query and page endpoints. The property
names and types in `EXPECTED_SCHEMA` are fixed by the existing database; nothing
here creates or alters a property, and `check_schema` refuses to run against a
schema that drifted."""

import logging
import os
import time
from collections.abc import Iterator

import httpx

from .cache import _env_file
from .extract import SOURCE_TYPE_LABELS
from .extract import sibling_key as _sibling_key
from .models import Job

# The four readable Source Type option names check_schema asserts are present.
SOURCE_TYPE_OPTIONS = set(SOURCE_TYPE_LABELS.values())

log = logging.getLogger(__name__)

BASE_URL = "https://api.notion.com"
NOTION_VERSION = "2025-09-03"
KEYRING_SERVICE = "job-listings-scraper"

# name -> Notion property "type". Extra properties in the live schema are
# ignored; every row here must be present with this type or check_schema raises.
EXPECTED_SCHEMA = {
    "Job Title": "title",
    "Company": "rich_text",
    "Location": "rich_text",
    "Work Mode": "select",
    "Seniority": "select",
    "Priority Score": "number",
    "H1B Sponsorship": "select",
    "Source Type": "select",
    "Job Area": "multi_select",
    "Minimum Years Signal": "rich_text",
    "Minimum / Basic Qualifications": "rich_text",
    "Preferred Qualifications": "rich_text",
    "Parsed Skills": "rich_text",
    "Requirement Signal": "rich_text",
    "Status": "formula",
    "Applied": "checkbox",
    "Neglected": "checkbox",
    "Unavailable": "date",
    "Job URL": "url",
    "External ID": "rich_text",
    "Posting Key": "rich_text",
    "Source": "select",
    "Date Posted": "date",
    "First Seen": "date",
    "Last Seen": "date",
}


class AuthError(Exception):
    """No token, or Notion rejected it."""


class NotionError(Exception):
    """A non-retryable Notion API error (4xx); carries the API message."""


class SchemaError(Exception):
    """The live data-source schema does not match EXPECTED_SCHEMA."""


# --- property value builders (one per Notion type) ---


def title(v):
    return {"title": [{"text": {"content": (v or "")[:2000]}}]}


def text(v):
    return {"rich_text": [{"text": {"content": v[:2000]}}]} if v else {"rich_text": []}


def select(v):
    return {"select": {"name": v}} if v else {"select": None}


def multi_select(values):
    return {"multi_select": [{"name": v} for v in values]}


def number(v):
    return {"number": v}


def checkbox(v):
    return {"checkbox": bool(v)}


def date(v):
    return {"date": {"start": v}} if v else {"date": None}


def url(v):
    return {"url": v or None}


def build_create_properties(job: Job, run_date: str) -> dict:
    """Every "written on create" property from the overview property table."""
    return {
        "Job Title": title(job.title),
        "Company": text(job.company),
        "Location": text("; ".join(job.locations)),
        "Work Mode": select(job.work_mode),
        "Seniority": select(job.seniority),
        "Priority Score": number(job.priority_score),
        "H1B Sponsorship": select(job.h1b_sponsorship),
        "Source Type": select(SOURCE_TYPE_LABELS.get(job.source_type, "Direct company")),
        "Job Area": multi_select(job.areas),
        "Minimum Years Signal": text(job.min_years_signal),
        "Minimum / Basic Qualifications": text(job.minimum_qualifications[:1800]),
        "Preferred Qualifications": text(job.preferred_qualifications[:1800]),
        "Parsed Skills": text(job.parsed_skills),
        "Requirement Signal": text(job.requirement_signal),
        "Applied": checkbox(False),
        "Neglected": checkbox(False),
        "Job URL": url(job.url),
        "External ID": text(",".join(job.job_ids)),
        "Posting Key": text(job.posting_key),
        "Source": select("LinkedIn"),
        "Date Posted": date(job.date_posted),
        "First Seen": date(run_date),
        "Last Seen": date(run_date),
    }


def build_update_properties(refresh, job_row: dict, run_date: str) -> dict:
    """The changed subset of an existing row: Date Posted when the repost date
    advanced, Location/External ID when a city or ID was appended, Last Seen
    when any of those changed or Notion's Last Seen is unset or over 7 days old.
    Empty when nothing is due, so the caller can skip the write. Never touches
    Applied, Neglected, Unavailable, First Seen, Posting Key. `job_row` is the
    cache row as it currently mirrors Notion."""
    props: dict = {}
    if refresh.date_posted and refresh.date_posted > (job_row.get("date_posted") or ""):
        props["Date Posted"] = date(refresh.date_posted)
    if refresh.new_locations:
        current = _json_list(job_row.get("locations"))
        merged = current + [
            loc for loc in refresh.new_locations
            if loc.lower() not in {c.lower() for c in current}
        ]
        props["Location"] = text("; ".join(merged))
    if refresh.new_job_ids:
        current_ids = _json_list(job_row.get("job_ids"))
        merged_ids = sorted(set(current_ids) | set(refresh.new_job_ids), key=int)
        props["External ID"] = text(",".join(merged_ids))
    if props or _last_seen_stale(job_row.get("notion_last_seen"), run_date):
        props["Last Seen"] = date(run_date)
    return props


def _last_seen_stale(notion_last_seen: str | None, run_date: str) -> bool:
    import datetime as _dt

    if not notion_last_seen:
        return True
    try:
        seen = _dt.date.fromisoformat(notion_last_seen[:10])
        today = _dt.date.fromisoformat(run_date[:10])
    except ValueError:
        return True
    return (today - seen).days > 7


def _json_list(value) -> list[str]:
    import json

    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


# --- page -> cache row (bootstrap/resync) ---


def _plain(prop: dict | None, kind: str) -> str:
    if not prop:
        return ""
    parts = prop.get(kind) or []
    return "".join(p.get("plain_text") or p.get("text", {}).get("content", "") for p in parts).strip()


def _date_value(prop: dict | None) -> str:
    if prop and prop.get("date"):
        return prop["date"].get("start") or ""
    return ""


def _checkbox_value(prop: dict | None) -> int:
    return 1 if (prop and prop.get("checkbox")) else 0


def page_to_row(page: dict, run_date: str | None = None) -> dict | None:
    """Map a Notion page to a `jobs` cache row. Returns None (and logs) when the
    page has no Posting Key, which is the only field the cache cannot derive."""
    import datetime as _dt

    run_date = run_date or _dt.date.today().isoformat()
    props = page.get("properties", {})
    posting_key = _plain(props.get("Posting Key"), "rich_text")
    if not posting_key:
        log.warning("skipping page %s: no Posting Key", page.get("id"))
        return None
    title_v = _plain(props.get("Job Title"), "title")
    company = _plain(props.get("Company"), "rich_text")
    locations = [p.strip() for p in _plain(props.get("Location"), "rich_text").split(";") if p.strip()]
    ext = [p.strip() for p in _plain(props.get("External ID"), "rich_text").split(",") if p.strip()]
    if not ext:
        digits = "".join(c for c in posting_key if c.isdigit())
        ext = [digits] if digits else []
    first_seen = _date_value(props.get("First Seen")) or run_date
    date_posted = _date_value(props.get("Date Posted")) or first_seen
    last_seen = _date_value(props.get("Last Seen")) or first_seen
    return {
        "posting_key": posting_key,
        "page_id": page.get("id"),
        "sibling_key": _sibling_key(company, title_v),
        "title": title_v,
        "company": company,
        "locations": locations,
        "job_ids": ext,
        "date_posted": date_posted,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "notion_last_seen": last_seen,
        "applied": _checkbox_value(props.get("Applied")),
        "neglected": _checkbox_value(props.get("Neglected")),
        "origin": "bootstrap",
    }


# --- REST client ---


class NotionClient:
    def __init__(self, token: str | None = None, data_source_id: str | None = None,
                 client: httpx.Client | None = None, pace: float = 0.35):
        self.token = token or _read_token()
        if not self.token:
            raise AuthError("no Notion token; run: uv run jobs auth set-token")
        self.data_source_id = data_source_id or _read_env("NOTION_DATA_SOURCE_ID")
        if not self.data_source_id:
            raise AuthError("NOTION_DATA_SOURCE_ID is not set (.env)")
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=30.0)
        self._pace = pace

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        rate_tries = 0
        server_tries = 0
        while True:
            if self._pace:
                time.sleep(self._pace)
            resp = self._client.request(method, path, headers=self._headers(), json=json)
            code = resp.status_code
            if code == 200:
                return resp.json()
            if code == 429 and rate_tries < 5:
                rate_tries += 1
                retry_after = float(resp.headers.get("Retry-After", 5) or 5)
                time.sleep(retry_after)
                continue
            if 500 <= code < 600 and server_tries < 3:
                time.sleep(2 ** server_tries)
                server_tries += 1
                continue
            message = _error_message(resp)
            if code in (401, 403):
                raise AuthError(f"{code}: {message}")
            raise NotionError(f"{code}: {message}")

    def me(self) -> dict:
        return self._request("GET", "/v1/users/me")

    def get_schema(self) -> dict:
        data = self._request("GET", f"/v1/data_sources/{self.data_source_id}")
        return data.get("properties", {})

    def check_schema(self) -> None:
        props = self.get_schema()
        missing, mismatched = [], []
        for name, want in EXPECTED_SCHEMA.items():
            if name not in props:
                missing.append(name)
            elif props[name].get("type") != want:
                mismatched.append(f"{name}: want {want}, got {props[name].get('type')}")
        st = props.get("Source Type", {})
        if st.get("type") == "select":
            have = {o.get("name") for o in st.get("select", {}).get("options", [])}
            missing_opts = sorted(SOURCE_TYPE_OPTIONS - have)
        else:
            missing_opts = []
        if missing or mismatched or missing_opts:
            parts = []
            if missing:
                parts.append("missing: " + ", ".join(missing))
            if mismatched:
                parts.append("type mismatch: " + "; ".join(mismatched))
            if missing_opts:
                parts.append("Source Type missing options: " + ", ".join(missing_opts))
            raise SchemaError(" | ".join(parts))

    def query_all(self) -> Iterator[dict]:
        cursor = None
        seen = 0
        while True:
            body: dict = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/v1/data_sources/{self.data_source_id}/query", json=body)
            for row in data.get("results", []):
                seen += 1
                yield row
            if seen >= 10000:
                log.warning("query_all reached 10,000 rows; results may be truncated")
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    def query_by_key(self, posting_key: str) -> dict | None:
        body = {
            "page_size": 1,
            "filter": {"property": "Posting Key", "rich_text": {"equals": posting_key}},
        }
        data = self._request("POST", f"/v1/data_sources/{self.data_source_id}/query", json=body)
        results = data.get("results", [])
        return results[0] if results else None

    def create_page(self, properties: dict) -> str:
        body = {"parent": {"data_source_id": self.data_source_id}, "properties": properties}
        data = self._request("POST", "/v1/pages", json=body)
        return data["id"]

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/v1/pages/{page_id}", json={"properties": properties})


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json().get("message", resp.text)
    except ValueError:
        return resp.text


def _read_token() -> str | None:
    try:
        import keyring

        tok = keyring.get_password(KEYRING_SERVICE, "token")
        if tok:
            return tok
    except Exception as e:  # keyring backend unavailable (e.g. headless)
        log.warning("keyring unavailable, falling back to env: %s", e)
    return _read_env("NOTION_TOKEN")


def _read_env(key: str) -> str | None:
    return os.environ.get(key) or _env_file().get(key)


def set_token(token: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, "token", token)
