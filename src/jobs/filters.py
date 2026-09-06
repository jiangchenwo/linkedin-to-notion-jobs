"""Filter rules. Each returns the rule name on rejection, None on pass.
card_stage runs the card rules in order and returns the first rejection;
detail_stage does the same for the detail-stage rules."""

import re
from datetime import date, timedelta

from . import profile
from .extract import SKILL_PATTERNS, canon, source_type
from .models import Card, Detail

PROFILE = profile.load()


def date_window(card: Card, run_date: date) -> str | None:
    """Reject cards older than 2 days (14 for new-grad titles)."""
    limit_days = 14 if PROFILE.new_grad_title_re.search(card.title) else 2
    cutoff = run_date - timedelta(days=limit_days)
    try:
        posted = date.fromisoformat(card.date_posted)
    except (ValueError, TypeError):
        return None
    return "date_window" if posted < cutoff else None


def title_seniority(card: Card) -> str | None:
    return "title_seniority" if PROFILE.senior_title_re.search(card.title) else None


def aggregator_company(card: Card, aggregators: set[str]) -> str | None:
    """`aggregators` must already be canon()'d."""
    return "aggregator_company" if canon(card.company) in aggregators else None


def card_stage(card: Card, run_date: date, aggregators: set[str]) -> str | None:
    for rule in (
        lambda: date_window(card, run_date),
        lambda: title_seniority(card),
        lambda: aggregator_company(card, aggregators),
    ):
        rejected = rule()
        if rejected:
            return rejected
    return None


# --- detail-stage rules ---

CONTRACT_RE = re.compile(
    r"\b(contract|temporary|temp to perm|contract to full)\b", re.I
)


def employment_type(detail: Detail) -> str | None:
    et = (detail.employment_type or "").strip()
    if et and et != "Full-time":
        return "employment_type"
    if CONTRACT_RE.search(detail.description_text):
        return "employment_type"
    return None


def ai_relevance(card: Card, detail: Detail) -> str | None:
    """Reject weak AI/ML relevance: needs two core AI terms, or one core term
    with AI signals in both the title and the body (classify_posting logic)."""
    title = card.title or ""
    text = detail.description_text or ""
    combined = f"{title}\n{text}"
    core = [
        name
        for name, pat in SKILL_PATTERNS.items()
        if name in PROFILE.core_skills and re.search(pat, combined, re.I)
    ]
    both_ai = bool(PROFILE.relevance_re.search(title)) and bool(
        PROFILE.relevance_re.search(text)
    )
    relevant = (both_ai and len(core) >= 1) or len(core) >= 2
    return None if relevant else "ai_relevance"


def min_years_cap(min_years_lower: int | None) -> str | None:
    return "min_years_cap" if (min_years_lower is not None and min_years_lower > 6) else None


def source_quality(company: str, companies: dict) -> str | None:
    """Reject aggregators and confidential/blank employers. Exclusion depends
    only on the company name, so no description text is needed here."""
    verdict = source_type(company, "", companies)
    return "source_quality" if verdict.startswith("excluded_") else None


def thin_posting(detail: Detail) -> str | None:
    return "thin_posting" if len(detail.description_text) < 400 else None


def detail_stage(
    card: Card, detail: Detail, min_years_lower: int | None, companies: dict
) -> str | None:
    for rule in (
        lambda: employment_type(detail),
        lambda: ai_relevance(card, detail),
        lambda: min_years_cap(min_years_lower),
        lambda: source_quality(card.company, companies),
        lambda: thin_posting(detail),
    ):
        rejected = rule()
        if rejected:
            return rejected
    return None
