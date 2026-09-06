"""Filter rules. Each returns the rule name on rejection, None on pass.
card_stage runs the card rules in order and returns the first rejection;
detail_stage does the same for the detail-stage rules. Thresholds and
vocabularies all come from the search profile (data/keywords.toml)."""

from datetime import date, timedelta

from . import profile
from .extract import canon, source_type
from .models import Card, Detail

PROFILE = profile.load()


def date_window(card: Card, run_date: date) -> str | None:
    """Reject cards past the freshness window (a longer window for titles
    matching long_window_title_terms)."""
    limit_days = (
        PROFILE.long_window_max_age_days
        if PROFILE.long_window_title_re.search(card.title)
        else PROFILE.max_age_days
    )
    cutoff = run_date - timedelta(days=limit_days)
    try:
        posted = date.fromisoformat(card.date_posted)
    except (ValueError, TypeError):
        return None
    return "date_window" if posted < cutoff else None


def title_seniority(card: Card) -> str | None:
    return "title_seniority" if PROFILE.exclude_title_re.search(card.title) else None


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


def employment_type(detail: Detail) -> str | None:
    """Reject a posting whose employment type is not in employment_types (when
    that list is non-empty), or whose body carries an excluded term."""
    et = (detail.employment_type or "").strip()
    types = PROFILE.employment_types
    if types and et and et not in types:
        return "employment_type"
    if PROFILE.exclude_description_re.search(detail.description_text):
        return "employment_type"
    return None


def relevance(card: Card, detail: Detail) -> str | None:
    """Reject weak relevance: needs two core skills, or one core skill with a
    relevance term in both the title and the body (classify_posting logic)."""
    title = card.title or ""
    text = detail.description_text or ""
    combined = f"{title}\n{text}"
    core = [
        name
        for name, pat in PROFILE.skills
        if name in PROFILE.core_skills and pat.search(combined)
    ]
    both = bool(PROFILE.relevance_re.search(title)) and bool(PROFILE.relevance_re.search(text))
    relevant = (both and len(core) >= 1) or len(core) >= 2
    return None if relevant else "relevance"


def min_years_cap(min_years_lower: int | None) -> str | None:
    cap = PROFILE.max_min_years
    if cap is None or min_years_lower is None:
        return None
    return "min_years_cap" if min_years_lower > cap else None


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
        lambda: relevance(card, detail),
        lambda: min_years_cap(min_years_lower),
        lambda: source_quality(card.company, companies),
        lambda: thin_posting(detail),
    ):
        rejected = rule()
        if rejected:
            return rejected
    return None
