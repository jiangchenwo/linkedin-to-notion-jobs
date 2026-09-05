"""Filter rules. Each returns the rule name on rejection, None on pass.
card_stage runs the card rules in order and returns the first rejection.
Detail-stage rules are added in Phase 2."""

import re
from datetime import date, timedelta

from .extract import canon
from .models import Card

# Ported verbatim from analyze_linkedin_jobs.py:123-126.
SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|distinguished|head|vp|architect)\b",
    re.I,
)

# Ported verbatim from daily_ai_jobs.py:43-46.
NEW_GRAD_TITLE_RE = re.compile(
    r"\b(new grad|new graduate|early career|entry[- ]level|university grad|graduate engineer)\b",
    re.I,
)


def date_window(card: Card, run_date: date) -> str | None:
    """Reject cards older than 2 days (14 for new-grad titles)."""
    limit_days = 14 if NEW_GRAD_TITLE_RE.search(card.title) else 2
    cutoff = run_date - timedelta(days=limit_days)
    try:
        posted = date.fromisoformat(card.date_posted)
    except (ValueError, TypeError):
        return None
    return "date_window" if posted < cutoff else None


def title_seniority(card: Card) -> str | None:
    return "title_seniority" if SENIOR_TITLE_RE.search(card.title) else None


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
