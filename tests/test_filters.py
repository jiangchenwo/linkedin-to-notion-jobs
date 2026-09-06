from datetime import date

from jobs import filters, profile
from jobs.extract import canon
from jobs.filters import (
    aggregator_company,
    card_stage,
    date_window,
    detail_stage,
    employment_type,
    min_years_cap,
    relevance,
    source_quality,
    thin_posting,
    title_seniority,
)
from jobs.linkedin import parse_detail
from jobs.models import Card, Detail

from conftest import read_fixture

RUN_DATE = date(2026, 9, 5)
COMPANIES = {"aggregator": {canon("Robert Half")}, "startup": set(), "reliable": set()}


def detail_from(name):
    return parse_detail(read_fixture(name), "1")


def make_card(title="AI Engineer", company="Acme Corp", date_posted="2026-09-05"):
    return Card(
        job_id="4000000001",
        title=title,
        company=company,
        location="New York, NY",
        date_posted=date_posted,
        url="https://www.linkedin.com/jobs/view/4000000001/",
        keyword="AI engineer",
    )


def test_date_window_rejects_old_and_keeps_fresh():
    assert date_window(make_card(date_posted="2026-09-05"), RUN_DATE) is None
    assert date_window(make_card(date_posted="2026-09-03"), RUN_DATE) is None
    assert date_window(make_card(date_posted="2026-09-01"), RUN_DATE) == "date_window"


def test_date_window_new_grad_gets_fourteen_days():
    old = "2026-08-25"  # 11 days back
    assert date_window(make_card(title="AI Engineer", date_posted=old), RUN_DATE) == "date_window"
    assert date_window(make_card(title="New Grad AI Engineer", date_posted=old), RUN_DATE) is None


def test_title_seniority():
    assert title_seniority(make_card(title="Senior AI Engineer")) == "title_seniority"
    assert title_seniority(make_card(title="Staff ML Engineer")) == "title_seniority"
    assert title_seniority(make_card(title="AI Engineer")) is None


def test_aggregator_company():
    aggregators = {canon("Robert Half")}
    assert aggregator_company(make_card(company="Robert Half"), aggregators) == "aggregator_company"
    assert aggregator_company(make_card(company="Acme Corp"), aggregators) is None


def test_card_stage_returns_first_rejecting_rule():
    aggregators = {canon("Robert Half")}
    # senior title AND old date -> date_window runs first
    card = make_card(title="Senior AI Engineer", date_posted="2026-08-01")
    assert card_stage(card, RUN_DATE, aggregators) == "date_window"
    # senior title, fresh date -> title_seniority
    assert card_stage(make_card(title="Senior AI Engineer"), RUN_DATE, aggregators) == "title_seniority"
    # clean card passes
    assert card_stage(make_card(), RUN_DATE, aggregators) is None


def test_employment_type():
    assert employment_type(detail_from("detail_contract.html")) == "employment_type"
    assert employment_type(detail_from("detail_junior.html")) is None


def test_relevance():
    assert (
        relevance(make_card(title="Backend Software Engineer"), detail_from("detail_not_ai.html"))
        == "relevance"
    )
    assert (
        relevance(make_card(title="Machine Learning Engineer"), detail_from("detail_junior.html"))
        is None
    )


def test_min_years_cap():
    assert min_years_cap(7) == "min_years_cap"
    assert min_years_cap(6) is None
    assert min_years_cap(None) is None


def test_source_quality():
    assert source_quality("Robert Half", COMPANIES) == "source_quality"
    assert source_quality("Confidential Jobs", COMPANIES) == "source_quality"
    assert source_quality("Beta Labs", COMPANIES) is None


def test_thin_posting():
    assert thin_posting(detail_from("detail_thin.html")) == "thin_posting"
    assert thin_posting(detail_from("detail_junior.html")) is None


_BASE_TOML = r"""
keywords = ["engineer"]

[search]
location = "United States"
sortBy = "DD"
f_TPR_past_24h = "r86400"
f_TPR_past_week = "r604800"

[filters]
exclude_title_terms = ["senior"]
{extra_filters}

[priority]
"New Grad" = 1
"Internship" = 2
"Junior" = 2
"Entry Level" = 2
"Associate" = 3
"Unknown" = 3
"Senior" = 6

[relevance]
terms = ["ai"]
core_skills = ["Python"]

[skills]
"Python" = '\bpython\b'

[areas]
default = "General"

[[areas.map]]
label = "Backend"
pattern = 'backend'
"""


def _profile_with(tmp_path, extra_filters):
    p = tmp_path / "keywords.toml"
    p.write_text(_BASE_TOML.format(extra_filters=extra_filters))
    return profile.load(str(p))


def test_employment_type_accepts_a_profiles_own_label(tmp_path, monkeypatch):
    prof = _profile_with(tmp_path, 'employment_types = ["Internship"]')
    monkeypatch.setattr(filters, "PROFILE", prof)
    intern = Detail(job_id="1", employment_type="Internship", description_text="An internship.")
    assert employment_type(intern) is None
    full_time = Detail(job_id="1", employment_type="Full-time", description_text="A role.")
    assert employment_type(full_time) == "employment_type"


def test_min_years_cap_disabled_when_max_min_years_omitted(tmp_path, monkeypatch):
    prof = _profile_with(tmp_path, "")  # no max_min_years
    monkeypatch.setattr(filters, "PROFILE", prof)
    assert min_years_cap(10) is None


def test_detail_stage_returns_first_rejecting_rule():
    # contract employment AND a thin, non-AI body: employment_type runs first
    detail = Detail(job_id="1", employment_type="Contract", description_text="short")
    assert detail_stage(make_card(title="AI Engineer"), detail, None, COMPANIES) == "employment_type"
    # full-time, AI-relevant, but asks for 8 years: min_years_cap after the earlier passes
    junior = detail_from("detail_junior.html")
    assert detail_stage(make_card(title="Machine Learning Engineer"), junior, 8, COMPANIES) == "min_years_cap"
    # clean detail passes every rule
    assert detail_stage(make_card(title="Machine Learning Engineer"), junior, 2, COMPANIES) is None
