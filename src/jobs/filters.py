"""Filter rules. Each returns the rule name on rejection, None on pass.
card_stage runs the card rules in order and returns the first rejection;
detail_stage does the same for the detail-stage rules."""

import re
from datetime import date, timedelta

from .extract import SKILL_PATTERNS, canon, source_type
from .models import Card, Detail

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


# --- detail-stage rules ---

# Ported verbatim from analyze_linkedin_jobs.py:133-139.
AI_RE = re.compile(
    r"\b(ai|artificial intelligence|machine learning|ml\b|genai|generative ai|llm|"
    r"rag|retrieval|embedding|vector|agentic|agent\b|prompt|computer vision|"
    r"deep learning|nlp|pytorch|tensorflow|sagemaker|bedrock|model serving|mlops)\b",
    re.I,
)
CONTRACT_RE = re.compile(
    r"\b(contract|temporary|temp to perm|contract to full)\b", re.I
)

# The subset of SKILL_PATTERNS that classify_posting treats as core AI evidence
# (analyze_linkedin_jobs.py:579-590).
CORE_AI_SKILLS = {
    "LLM/GenAI",
    "RAG/retrieval",
    "Model serving/MLOps",
    "PyTorch",
    "TensorFlow",
    "NLP",
    "Computer vision/OCR",
    "Agents/workflows",
    "Evals/testing",
}


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
        if name in CORE_AI_SKILLS and re.search(pat, combined, re.I)
    ]
    both_ai = bool(AI_RE.search(title)) and bool(AI_RE.search(text))
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
