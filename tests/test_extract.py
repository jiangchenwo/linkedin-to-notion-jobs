import pytest

from jobs import extract, profile
from jobs.extract import (
    AREAS,
    SOURCE_TYPE_LABELS,
    areas,
    build_job,
    canon,
    h1b,
    location,
    min_years,
    priority,
    seniority,
    skills,
    source_type,
    unresolved_fields,
    work_mode,
)
from jobs.linkedin import parse_detail
from jobs.models import Card, Detail, Job

from conftest import read_fixture


def make_card(job_id="4000000001", title="AI Engineer", company="Beta Labs", location="Austin, TX", date_posted="2026-09-05"):
    return Card(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        date_posted=date_posted,
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        keyword="AI engineer",
    )


def detail_from(name):
    return parse_detail(read_fixture(name), "1")


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("detail_junior.html", ("2+", 2)),
        ("detail_range_no_section.html", ("3-5", 3)),
        ("detail_degree_years.html", ("BS+3", 3)),
        ("detail_not_ai.html", ("Not explicit", None)),
    ],
)
def test_min_years(fixture, expected):
    assert min_years(detail_from(fixture)) == expected


def test_min_years_ignores_company_history():
    d = Detail(
        job_id="1",
        description_text="Beta Labs has 20 years of history. Minimum 2+ years of experience required.",
    )
    assert min_years(d) == ("2+", 2)


def test_location_from_sibling_cards():
    cards = [
        make_card(job_id="1", location="San Francisco, CA"),
        make_card(job_id="2", location="New York, NY"),
    ]
    d = Detail(job_id="1", description_text="No location line here.")
    assert location(cards, d) == ["San Francisco, CA", "New York, NY"]


def test_location_from_multicity_description():
    cards = [make_card(job_id="1", location="United States")]
    d = detail_from("detail_multicity.html")
    assert location(cards, d) == ["United States", "San Francisco, CA", "New York, NY"]


@pytest.mark.parametrize(
    "title,level,lower,expected",
    [
        ("New Grad AI Engineer", "Mid-Senior level", None, "New Grad"),
        ("AI Engineer Intern", "Entry level", None, "Internship"),
        ("Junior ML Engineer", "Mid-Senior level", 8, "Junior"),
        ("AI Engineer", "Internship", None, "Internship"),
        ("AI Engineer", "Entry level", None, "Entry Level"),
        ("AI Engineer", "Associate", None, "Associate"),
        ("AI Engineer", "Director", None, "Senior"),
        ("AI Engineer", "", 2, "Junior"),
        ("AI Engineer", "", 4, "Entry Level"),
        ("AI Engineer", "", 6, "Associate"),
        ("AI Engineer", "Mid-Senior level", None, "Associate"),
        ("AI Engineer", "Mid-Senior level", 7, "Senior"),
        ("AI Engineer", "", None, "Unknown"),
    ],
)
def test_seniority(title, level, lower, expected):
    card = make_card(title=title)
    d = Detail(job_id="1", seniority_level=level)
    assert seniority(card, d, lower) == expected


@pytest.mark.parametrize(
    "sen,score",
    [
        ("New Grad", 1),
        ("Internship", 2),
        ("Junior", 2),
        ("Entry Level", 2),
        ("Associate", 3),
        ("Unknown", 3),
        ("Senior", 6),
    ],
)
def test_priority(sen, score):
    assert priority(sen) == score


def test_work_mode_remote():
    cards = [make_card(location="Remote")]
    d = Detail(job_id="1", description_text="Work from anywhere in the US.")
    assert work_mode(cards, d) == "Remote"


def test_h1b_positive_and_negative():
    assert h1b(Detail(job_id="1", description_text="We offer visa sponsorship.")) == "Yes"
    assert h1b(Detail(job_id="1", description_text="We will not sponsor visas.")) == "No"
    assert h1b(Detail(job_id="1", description_text="A normal role.")) == "Unknown"


def test_skills_lists_matches():
    s = skills(detail_from("detail_junior.html"))
    assert "Python" in s
    assert "PyTorch" in s


_DBT_TOML = r"""
keywords = ["data engineer"]

[search]
location = "United States"
sortBy = "DD"
f_TPR_past_24h = "r86400"
f_TPR_past_week = "r604800"

[filters]
exclude_title_terms = ["senior"]

[priority]
"New Grad" = 1
"Internship" = 2
"Junior" = 2
"Entry Level" = 2
"Associate" = 3
"Unknown" = 3
"Senior" = 6

[relevance]
terms = ["etl"]
core_skills = ["dbt"]

[skills]
"dbt" = '\bdbt\b'
"Snowflake" = 'snowflake'

[areas]
default = "General Data"

[[areas.map]]
label = "Warehousing"
pattern = 'warehouse'
"""


def test_skills_reads_a_custom_profile_skill(tmp_path, monkeypatch):
    p = tmp_path / "keywords.toml"
    p.write_text(_DBT_TOML)
    monkeypatch.setattr(extract, "PROFILE", profile.load(str(p)))
    d = Detail(job_id="1", description_text="We build models in dbt on Snowflake.")
    assert skills(d) == "dbt, Snowflake"


def test_source_type_binding_constraint():
    companies = {"aggregator": {canon("Robert Half")}, "startup": {canon("OpenAI")}, "reliable": {canon("Amazon")}}
    assert source_type("Robert Half", "", companies) == "excluded_intermediary"
    assert source_type("Confidential", "", companies) == "excluded_unknown"
    assert source_type("OpenAI", "", companies) == "direct_startup"
    assert source_type("Amazon", "", companies) == "direct_reliable_company"
    # unrecognized company is never excluded
    assert source_type("Nimbus Robotics Inc.", "", companies) == "direct_company"


def test_areas_title_match_wins_alone():
    # a single title hit is enough; the description need not repeat it
    assert areas("Agentic AI Engineer", "We build web apps.") == ["Agents"]


def test_areas_description_needs_two_mentions():
    one = "We process one image per request."
    two = "Image pipelines for image classification."
    assert "Computer Vision" not in areas("Engineer", one)
    assert "Computer Vision" in areas("Engineer", two)


def test_areas_order_follows_areas_list_agents_first():
    desc = "Our agents call agents; llm and llm work; image and image work."
    result = areas("Engineer", desc)
    assert result[0] == "Agents"
    order = [label for label, _ in AREAS]
    assert result == sorted(result, key=order.index)


def test_areas_caps_at_three():
    desc = (
        "agents agents llm llm image image robot robot "
        "nlp nlp search search"
    )
    assert len(areas("Engineer", desc)) == 3


def test_areas_general_ai_fallback():
    assert areas("Software Engineer", "We build accounting software.") == ["General AI"]


def test_source_type_labels_cover_non_excluded_returns():
    companies = {"aggregator": set(), "startup": set(), "reliable": set()}
    non_excluded = {
        source_type("OpenAI", "series a funded", {"aggregator": set(), "startup": {canon("OpenAI")}, "reliable": set()}),
        source_type("Amazon", "", {"aggregator": set(), "startup": set(), "reliable": {canon("Amazon")}}),
        source_type("Acme", "we are a startup", companies),
        source_type("Acme Inc.", "", companies),
    }
    for value in non_excluded:
        assert value in SOURCE_TYPE_LABELS


def _job(locations, min_years_signal):
    return Job(
        posting_key="linkedin:1",
        job_ids=["1"],
        sibling_key="x",
        title="AI Engineer",
        company="Beta Labs",
        locations=locations,
        date_posted="2026-09-05",
        url="https://www.linkedin.com/jobs/view/1/",
        min_years_signal=min_years_signal,
    )


def test_unresolved_location():
    job = _job(["United States"], "2+")
    d = Detail(job_id="1", description_text="We have offices across the country.")
    assert unresolved_fields(job, d) == ["location"]


def test_unresolved_min_years():
    job = _job(["Austin, TX"], "Not explicit")
    d = Detail(job_id="1", description_text="Several years of experience preferred.")
    assert unresolved_fields(job, d) == ["min_years"]


def test_unresolved_none():
    job = _job(["Austin, TX"], "2+")
    d = Detail(job_id="1", description_text="An Austin role on our team.")
    assert unresolved_fields(job, d) == []


def test_build_job_two_card_group():
    cards = [
        make_card(job_id="4000000010", title="ML Engineer", location="Austin, TX", date_posted="2026-09-03"),
        make_card(job_id="4000000002", title="ML Engineer", location="Remote", date_posted="2026-09-05"),
    ]
    group = {"sibling_key": "beta labs::ml engineer", "cards": cards}
    companies = {"aggregator": set(), "startup": set(), "reliable": set()}
    job = build_job(group, detail_from("detail_junior.html"), companies, "2026-09-05")
    assert job.job_ids == ["4000000002", "4000000010"]
    assert job.posting_key == "linkedin:4000000002"
    assert job.date_posted == "2026-09-05"
    assert job.title == "ML Engineer"
    assert job.url == "https://www.linkedin.com/jobs/view/4000000002/"
    assert job.min_years_signal == "2+"
