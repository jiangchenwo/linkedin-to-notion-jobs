"""Dataclasses shared across the pipeline. Field names are used verbatim in the
JSON files under data/runs/<run_id>/, so renaming a field is a data migration."""

from dataclasses import dataclass, field, fields, asdict


class _DictMixin:
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        """Build from a dict, ignoring unknown keys so a run file written by an
        older phase still loads after new fields are added."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Card(_DictMixin):
    job_id: str
    title: str
    company: str
    location: str
    date_posted: str  # YYYY-MM-DD from the <time datetime="..."> attribute
    url: str
    keyword: str


@dataclass
class Detail(_DictMixin):
    job_id: str
    seniority_level: str = ""
    employment_type: str = ""
    job_function: str = ""
    industries: str = ""
    posted_text: str = ""
    location: str = ""
    description_html: str = ""
    description_text: str = ""


@dataclass
class Job(_DictMixin):
    posting_key: str
    job_ids: list[str]  # sorted ascending (numeric)
    sibling_key: str
    title: str
    company: str
    locations: list[str]
    date_posted: str
    url: str
    work_mode: str = "Unknown"
    seniority: str = "Unknown"
    priority_score: int = 3
    h1b_sponsorship: str = "Unknown"
    source_type: str = ""
    areas: list[str] = field(default_factory=list)
    min_years_signal: str = "Not explicit"
    min_years_lower: int | None = None
    minimum_qualifications: str = ""
    preferred_qualifications: str = ""
    parsed_skills: str = ""
    requirement_signal: str = ""
    unresolved: list[str] = field(default_factory=list)


@dataclass
class Refresh(_DictMixin):
    posting_key: str
    page_id: str | None
    date_posted: str
    new_locations: list[str] = field(default_factory=list)
    new_job_ids: list[str] = field(default_factory=list)


@dataclass
class RunSummary(_DictMixin):
    run_id: str
    started_at: str
    finished_at: str = ""
    status: str = "success"  # success | partial | failed
    date_window: str = ""
    cards_seen: int = 0
    cards_rejected_by_rule: dict[str, int] = field(default_factory=dict)
    details_fetched: int = 0
    details_rejected_by_rule: dict[str, int] = field(default_factory=dict)
    jobs_new: int = 0
    refreshes: int = 0
    unresolved: int = 0
    notion_created: int = 0
    notion_updated: int = 0
    notion_failed: int = 0
    errors: list[str] = field(default_factory=list)
    log_path: str = ""
