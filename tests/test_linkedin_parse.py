import re

from jobs.extract import sibling_key
from jobs.linkedin import parse_cards, parse_detail

from conftest import read_fixture

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_parse_cards_returns_ten_complete_cards():
    cards = parse_cards(read_fixture("search_page.html"), "AI engineer")
    assert len(cards) == 10
    for c in cards:
        assert c.job_id and c.title and c.company and c.location
        assert DATE_RE.match(c.date_posted)
        assert c.url == f"https://www.linkedin.com/jobs/view/{c.job_id}/"
        assert c.keyword == "AI engineer"


def test_sibling_pair_shares_key_but_differs_in_id_and_location():
    cards = parse_cards(read_fixture("search_page.html"), "AI engineer")
    pair = [c for c in cards if c.company == "Beta Labs"]
    assert len(pair) == 2
    a, b = pair
    assert sibling_key(a.company, a.title) == sibling_key(b.company, b.title)
    assert a.job_id != b.job_id
    assert a.location != b.location


def test_repost_card_date_comes_from_datetime_not_text():
    cards = parse_cards(read_fixture("search_page.html"), "AI engineer")
    repost = next(c for c in cards if c.job_id == "3900000005")
    assert repost.date_posted == "2026-09-05"


def test_empty_inputs_return_no_cards():
    assert parse_cards("", "AI engineer") == []
    assert parse_cards(read_fixture("search_empty.html"), "AI engineer") == []


def test_parse_detail_extracts_all_fields():
    detail = parse_detail(read_fixture("detail_page.html"), "4000000003")
    assert detail.seniority_level == "Entry level"
    assert detail.employment_type == "Full-time"
    assert "Engineering" in detail.job_function
    assert detail.industries == "Software Development"
    assert detail.posted_text == "9 hours ago"
    assert detail.location == "Austin, TX"
    assert detail.description_html
    assert "\n" in detail.description_text
    assert "Build and maintain LLM-backed features" in detail.description_text
    assert "Design vector retrieval" in detail.description_text


def test_parse_detail_missing_elements_no_exception():
    detail = parse_detail("<html></html>", "1")
    assert detail.job_id == "1"
    assert detail.seniority_level == ""
    assert detail.employment_type == ""
    assert detail.location == ""
    assert detail.description_text == ""
    assert detail.description_html == ""
