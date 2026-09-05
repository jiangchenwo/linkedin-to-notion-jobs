from datetime import date

from jobs.extract import canon
from jobs.filters import aggregator_company, card_stage, date_window, title_seniority
from jobs.models import Card

RUN_DATE = date(2026, 9, 5)


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
