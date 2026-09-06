import copy
import json
from pathlib import Path

import httpx
import pytest

from jobs import notion
from jobs.models import Job, Refresh

FIXTURES = Path(__file__).parent / "fixtures" / "notion"
DATA_SOURCE_ID = "00000000-0000-0000-0000-000000000000"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def make_client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=notion.BASE_URL)
    return notion.NotionClient(token="secret", data_source_id=DATA_SOURCE_ID, client=http, pace=0)


def schema_handler(schema=None):
    schema = schema if schema is not None else load("data_source.json")

    def handler(request):
        if request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json=schema)
        return httpx.Response(404, json={"message": "not found"})

    return handler


def test_check_schema_passes_on_fixture():
    client = make_client(schema_handler())
    client.check_schema()  # does not raise


def test_check_schema_reports_missing_property():
    schema = load("data_source.json")
    del schema["properties"]["Posting Key"]
    client = make_client(schema_handler(schema))
    with pytest.raises(notion.SchemaError, match="Posting Key"):
        client.check_schema()


def test_check_schema_reports_type_mismatch():
    schema = load("data_source.json")
    schema["properties"]["Date Posted"]["type"] = "rich_text"
    client = make_client(schema_handler(schema))
    with pytest.raises(notion.SchemaError, match="Date Posted"):
        client.check_schema()


def test_query_all_follows_cursor():
    page = load("query_page.json")
    last = load("query_last.json")

    def handler(request):
        if request.url.path.endswith("/query"):
            body = json.loads(request.content)
            return httpx.Response(200, json=last if body.get("start_cursor") == "cursor-page-2" else page)
        return httpx.Response(404, json={"message": "no"})

    client = make_client(handler)
    rows = list(client.query_all())
    assert len(rows) == 5


def test_create_page_sends_expected_body():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["method"] = request.method
        return httpx.Response(200, json={"id": "new-page-id"})

    client = make_client(handler)
    page_id = client.create_page({"Job Title": notion.title("x")})
    assert page_id == "new-page-id"
    assert captured["method"] == "POST"
    assert captured["body"]["parent"]["data_source_id"] == DATA_SOURCE_ID
    assert "Job Title" in captured["body"]["properties"]


def test_update_page_sends_patch_with_only_given_properties():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["method"] = request.method
        return httpx.Response(200, json={"id": "pg"})

    client = make_client(handler)
    client.update_page("pg", {"Last Seen": notion.date("2026-09-05")})
    assert captured["method"] == "PATCH"
    assert list(captured["body"]["properties"]) == ["Last Seen"]


def test_retry_on_429_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "rate"})
        return httpx.Response(200, json={"id": "ok"})

    client = make_client(handler)
    assert client.create_page({}) == "ok"
    assert calls["n"] == 2


def test_400_raises_notion_error_with_message():
    def handler(request):
        return httpx.Response(400, json={"message": "body failed validation"})

    client = make_client(handler)
    with pytest.raises(notion.NotionError, match="body failed validation"):
        client.create_page({})


def test_page_to_row_maps_and_skips_keyless():
    page = load("query_page.json")["results"][0]
    row = notion.page_to_row(page, "2026-09-05")
    assert row["posting_key"] == "linkedin:4000000001"
    assert row["job_ids"] == ["4000000001", "4000000005"]
    assert row["locations"] == ["Austin, TX", "Remote"]
    assert row["company"] == "Beta Labs"
    assert row["origin"] == "bootstrap"
    assert row["notion_last_seen"] == "2026-09-05"

    orphan = load("query_last.json")["results"][1]
    assert notion.page_to_row(orphan, "2026-09-05") is None


def _job():
    return Job(
        posting_key="linkedin:4000000001",
        job_ids=["4000000001", "4000000005"],
        sibling_key="beta labs::machine learning engineer",
        title="Machine Learning Engineer",
        company="Beta Labs",
        locations=["Austin, TX", "Remote"],
        date_posted="2026-09-05",
        url="https://www.linkedin.com/jobs/view/4000000001/",
        work_mode="Remote",
        seniority="Junior",
        priority_score=2,
        h1b_sponsorship="Unknown",
        source_type="direct_company",
        min_years_signal="2+",
    )


def test_build_create_properties_wraps_every_field():
    p = notion.build_create_properties(_job(), "2026-09-05")
    assert p["Job Title"]["title"][0]["text"]["content"] == "Machine Learning Engineer"
    assert p["Company"]["rich_text"][0]["text"]["content"] == "Beta Labs"
    assert p["Location"]["rich_text"][0]["text"]["content"] == "Austin, TX; Remote"
    assert p["Work Mode"]["select"]["name"] == "Remote"
    assert p["Source Type"]["rich_text"][0]["text"]["content"] == "direct_company"
    assert p["Priority Score"]["number"] == 2
    assert p["Applied"]["checkbox"] is False
    assert p["Job URL"]["url"] == "https://www.linkedin.com/jobs/view/4000000001/"
    assert p["External ID"]["rich_text"][0]["text"]["content"] == "4000000001,4000000005"
    assert p["Posting Key"]["rich_text"][0]["text"]["content"] == "linkedin:4000000001"
    assert p["Source"]["select"]["name"] == "LinkedIn"
    assert p["Date Posted"]["date"]["start"] == "2026-09-05"
    assert p["First Seen"]["date"]["start"] == "2026-09-05"
    assert p["Last Seen"]["date"]["start"] == "2026-09-05"


def test_build_update_properties_only_changed_fields():
    row = {"date_posted": "2026-09-01", "locations": json.dumps(["Austin, TX"]),
           "job_ids": json.dumps(["4000000001"])}
    # newer date, a new city, and a new id
    refresh = Refresh(posting_key="linkedin:4000000001", page_id="pg",
                      date_posted="2026-09-05", new_locations=["Remote"],
                      new_job_ids=["4000000005"])
    p = notion.build_update_properties(refresh, row, "2026-09-05")
    assert p["Date Posted"]["date"]["start"] == "2026-09-05"
    assert p["Location"]["rich_text"][0]["text"]["content"] == "Austin, TX; Remote"
    assert p["External ID"]["rich_text"][0]["text"]["content"] == "4000000001,4000000005"
    assert p["Last Seen"]["date"]["start"] == "2026-09-05"
    assert "Applied" not in p and "First Seen" not in p and "Posting Key" not in p


def test_build_update_properties_same_date_omits_date_posted():
    row = {"date_posted": "2026-09-05", "locations": json.dumps(["Austin, TX"]),
           "job_ids": json.dumps(["4000000001"])}
    refresh = Refresh(posting_key="linkedin:4000000001", page_id="pg",
                      date_posted="2026-09-05", new_locations=["Remote"], new_job_ids=[])
    p = notion.build_update_properties(refresh, row, "2026-09-06")
    assert "Date Posted" not in p
    assert "Location" in p
    assert "External ID" not in p
    assert p["Last Seen"]["date"]["start"] == "2026-09-06"
