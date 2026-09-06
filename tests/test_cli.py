import json
import shutil
from datetime import date

import httpx
import pytest

from jobs import cache, cli
from jobs.linkedin import GuestClient, load_keywords

from conftest import REPO_DATA, read_fixture

_, SEARCH_CFG = load_keywords(str(REPO_DATA / "keywords.toml"))


def make_transport(detail_status=200):
    search_page = read_fixture("search_page.html")
    search_empty = read_fixture("search_empty.html")
    detail_page = read_fixture("detail_page.html")

    def handler(request):
        path = request.url.path
        if "seeMoreJobPostings" in path:
            start = request.url.params.get("start", "0")
            return httpx.Response(200, text=search_page if start == "0" else search_empty)
        if "jobPosting" in path:
            if detail_status != 200:
                return httpx.Response(detail_status, text="blocked")
            return httpx.Response(200, text=detail_page)
        return httpx.Response(404, text="")

    return httpx.MockTransport(handler)


def install_client(monkeypatch, detail_status=200, block_limit=3):
    transport = make_transport(detail_status)

    def factory(cfg):
        return GuestClient(
            client=httpx.Client(transport=transport),
            pause=(0, 0),
            block_limit=block_limit,
            search_config=cfg,
        )

    monkeypatch.setattr(cli, "build_client", factory)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    shutil.copy(REPO_DATA / "keywords.toml", d / "keywords.toml")
    shutil.copy(REPO_DATA / "companies.toml", d / "companies.toml")
    monkeypatch.setenv("JOBS_DATA_DIR", str(d))
    monkeypatch.setenv("JOBS_DB_PATH", str(d / "jobs.sqlite3"))
    monkeypatch.setattr(cli, "_today", lambda: date(2026, 9, 5))
    return d


def _last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_search_all_stops_when_page_adds_no_new_ids():
    client = GuestClient(
        client=httpx.Client(transport=make_transport()),
        pause=(0, 0),
        search_config=SEARCH_CFG,
    )
    seen: set[str] = set()
    first = client.search_all("AI engineer", "past_24h", date(2026, 9, 5), 10, seen=seen)
    assert len(first) == 10
    after_first = client._requests

    # A second keyword serving the same page adds no new id -> one request, stop.
    second = client.search_all("LLM engineer", "past_24h", date(2026, 9, 5), 10, seen=seen)
    assert second == []
    assert client._requests - after_first == 1


def test_fetch_success_end_to_end(data_dir, monkeypatch, capsys):
    install_client(monkeypatch)
    rc = cli.main(["fetch", "--date-window", "past_24h"])
    assert rc == 0

    summary = _last_json(capsys)
    assert summary["status"] == "success"
    assert summary["cards_seen"] == 10
    assert summary["refreshes"] == 0

    run_id = summary["run_id"]
    groups = json.loads((data_dir / "runs" / run_id / "cards.json").read_text())
    assert any(len(g["cards"]) == 2 for g in groups)
    for g in groups:
        assert g["posting_key"].startswith("linkedin:")
        assert g["posting_key"].split(":")[1].isdigit()

    detail_files = list((data_dir / "details").glob("*.html"))
    assert len(detail_files) == len(groups)
    assert summary["details_fetched"] == len(groups)

    refreshes = json.loads((data_dir / "runs" / run_id / "refreshes.json").read_text())
    assert refreshes == []

    conn = cache.connect(str(data_dir / "jobs.sqlite3"))
    rules = {r["rule"] for r in conn.execute("SELECT rule FROM rejections")}
    stages = {r["stage"] for r in conn.execute("SELECT stage FROM rejections")}
    conn.close()
    assert rules <= {"date_window", "title_seniority", "aggregator_company"}
    assert "title_seniority" in rules
    assert "aggregator_company" in rules
    assert stages == {"card"}


def _seed_job(data_dir, posting_key, job_ids, company, title, *, date_posted,
              last_seen, locations, notion_last_seen):
    from jobs.extract import sibling_key

    conn = cache.connect(str(data_dir / "jobs.sqlite3"))
    cache.insert_job_row(conn, {
        "posting_key": posting_key,
        "page_id": "pg-seed",
        "sibling_key": sibling_key(company, title),
        "title": title,
        "company": company,
        "locations": locations,
        "job_ids": job_ids,
        "date_posted": date_posted,
        "first_seen": "2026-07-01",
        "last_seen": last_seen,
        "notion_last_seen": notion_last_seen,
        "applied": 0,
        "neglected": 0,
        "origin": "bootstrap",
    })
    conn.close()


def test_fetch_known_id_newer_date_refreshes_without_detail(data_dir, monkeypatch, capsys):
    install_client(monkeypatch)
    _seed_job(
        data_dir, "linkedin:4000000001", ["4000000001"], "Acme Corp 1", "AI Engineer 1",
        date_posted="2026-09-01", last_seen="2026-09-01", locations=["New York, NY"],
        notion_last_seen="2026-09-01",
    )
    rc = cli.main(["fetch", "--date-window", "past_24h"])
    assert rc == 0
    summary = _last_json(capsys)
    assert summary["refreshes"] >= 1 and summary["details_skipped"] >= 1

    run_dir = data_dir / "runs" / summary["run_id"]
    refreshes = json.loads((run_dir / "refreshes.json").read_text())
    entry = next(r for r in refreshes if r["posting_key"] == "linkedin:4000000001")
    assert entry["date_posted"] == "2026-09-05"

    groups = json.loads((run_dir / "cards.json").read_text())
    assert all(g["detail_job_id"] != "4000000001" for g in groups)
    assert not (data_dir / "details" / "4000000001.html").exists()


def test_fetch_known_sibling_new_city_refreshes(data_dir, monkeypatch, capsys):
    install_client(monkeypatch)
    # a prior posting of the same role in a different city, seen 4 days ago
    _seed_job(
        data_dir, "linkedin:3999999999", ["3999999999"], "Acme Corp 1", "AI Engineer 1",
        date_posted="2026-09-05", last_seen="2026-09-01", locations=["Boston, MA"],
        notion_last_seen="2026-09-05",
    )
    rc = cli.main(["fetch", "--date-window", "past_24h"])
    assert rc == 0
    summary = _last_json(capsys)
    run_dir = data_dir / "runs" / summary["run_id"]
    refreshes = json.loads((run_dir / "refreshes.json").read_text())
    entry = next(r for r in refreshes if r["posting_key"] == "linkedin:3999999999")
    assert "New York, NY" in entry["new_locations"]
    assert "4000000001" in entry["new_job_ids"]


def test_fetch_known_sibling_stale_falls_back_to_detail(data_dir, monkeypatch, capsys):
    install_client(monkeypatch)
    # same role, but last seen far outside the 30-day window
    _seed_job(
        data_dir, "linkedin:3999999999", ["3999999999"], "Acme Corp 1", "AI Engineer 1",
        date_posted="2026-06-01", last_seen="2026-07-01", locations=["Boston, MA"],
        notion_last_seen="2026-07-01",
    )
    rc = cli.main(["fetch", "--date-window", "past_24h"])
    assert rc == 0
    summary = _last_json(capsys)
    run_dir = data_dir / "runs" / summary["run_id"]
    groups = json.loads((run_dir / "cards.json").read_text())
    known = next(g for g in groups if g["detail_job_id"] == "4000000001")
    assert known["detail_path"].endswith("4000000001.html")
    refreshes = json.loads((run_dir / "refreshes.json").read_text())
    assert all(r["posting_key"] != "linkedin:3999999999" for r in refreshes)


def test_fetch_partial_when_detail_blocked(data_dir, monkeypatch, capsys):
    install_client(monkeypatch, detail_status=429, block_limit=1)
    rc = cli.main(["fetch", "--date-window", "past_24h"])
    assert rc == 1

    summary = _last_json(capsys)
    assert summary["status"] == "partial"
    assert summary["blocked"] is True

    run_id = summary["run_id"]
    assert (data_dir / "runs" / run_id / "cards.json").exists()


def _card_dict(job_id, title, company, location):
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "date_posted": "2026-09-05",
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "keyword": "AI engineer",
    }


def test_extract_end_to_end(data_dir, capsys):
    details = data_dir / "details"
    details.mkdir(parents=True, exist_ok=True)

    def place(job_id, fixture):
        p = details / f"{job_id}.html"
        p.write_text(read_fixture(fixture))
        return str(p)

    groups = [
        {
            "sibling_key": "beta labs::machine learning engineer",
            "posting_key": "linkedin:4000000001",
            "cards": [
                _card_dict("4000000001", "Machine Learning Engineer", "Beta Labs", "Austin, TX"),
                _card_dict("4000000005", "Machine Learning Engineer", "Beta Labs", "Remote"),
            ],
            "detail_job_id": "4000000001",
            "detail_path": place("4000000001", "detail_junior.html"),
        },
        {
            "sibling_key": "beta labs::ml contractor",
            "posting_key": "linkedin:4000000010",
            "cards": [_card_dict("4000000010", "ML Engineer", "Beta Labs", "Remote")],
            "detail_job_id": "4000000010",
            "detail_path": place("4000000010", "detail_contract.html"),
        },
        {
            "sibling_key": "beta labs::ai engineer",
            "posting_key": "linkedin:4000000020",
            "cards": [_card_dict("4000000020", "AI Engineer", "Beta Labs", "New York, NY")],
            "detail_job_id": "4000000020",
            "detail_path": None,
        },
    ]
    run_id = "2026-09-05T000000"
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cards.json").write_text(json.dumps(groups))
    (data_dir / "runs" / "latest").write_text(run_id)

    rc = cli.main(["extract"])
    assert rc == 0

    summary = _last_json(capsys)
    assert summary["jobs"] == 1
    assert summary["rejected"] == 1
    assert summary["not_fetched"] == 1
    assert summary["rejected_by_rule"].get("employment_type") == 1
    assert summary["unresolved_path"].endswith("unresolved.json")

    jobs = json.loads((run_dir / "jobs.json").read_text())
    assert len(jobs) == 1
    assert jobs[0]["posting_key"] == "linkedin:4000000001"
    assert jobs[0]["job_ids"] == ["4000000001", "4000000005"]

    rejections = json.loads((run_dir / "rejections.json").read_text())
    assert rejections[0]["rule"] == "employment_type"
    assert set(rejections[0]) >= {"posting_key", "job_id", "title", "company", "rule"}

    assert json.loads((run_dir / "unresolved.json").read_text()) == []

    conn = cache.connect(str(data_dir / "jobs.sqlite3"))
    rows = list(conn.execute("SELECT stage, rule FROM rejections WHERE stage = 'detail'"))
    conn.close()
    assert any(r["rule"] == "employment_type" for r in rows)


def test_extract_missing_cards_exits_2(data_dir, capsys):
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    (data_dir / "runs" / "latest").write_text("nope")
    rc = cli.main(["extract"])
    assert rc == 2
    assert "error" in _last_json(capsys)


class _FakeNotion:
    def __init__(self, *a, **k):
        pass

    def check_schema(self):
        pass

    def query_by_key(self, key):
        return None


def test_sync_dry_run_plans_without_writing(data_dir, monkeypatch, capsys):
    import jobs.notion as notion_mod

    monkeypatch.setattr(notion_mod, "NotionClient", _FakeNotion)

    run_id = "2026-09-05T000000"
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "posting_key": "linkedin:4000000001",
        "job_ids": ["4000000001"],
        "sibling_key": "beta labs::ml engineer",
        "title": "ML Engineer",
        "company": "Beta Labs",
        "locations": ["Austin, TX"],
        "date_posted": "2026-09-05",
        "url": "https://www.linkedin.com/jobs/view/4000000001/",
    }
    (run_dir / "jobs.json").write_text(json.dumps([job]))
    (run_dir / "refreshes.json").write_text("[]")
    (data_dir / "runs" / "latest").write_text(run_id)

    rc = cli.main(["sync", "--dry-run"])
    assert rc == 0
    summary = _last_json(capsys)
    assert summary["planned_create"] == 1
    assert summary["created"] == 0
    assert not (run_dir / "summary.json").exists()

    conn = cache.connect(str(data_dir / "jobs.sqlite3"))
    n = conn.execute("SELECT count(*) AS n FROM sync_ops").fetchone()["n"]
    conn.close()
    assert n == 0


def test_apply_extractions_applies_and_skips(data_dir, capsys):
    run_id = "2026-09-05T000000"
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "runs" / "latest").write_text(run_id)
    jobs = [
        {"posting_key": "linkedin:1", "locations": ["United States"],
         "min_years_signal": "Not explicit", "min_years_lower": None,
         "unresolved": ["location", "min_years"]},
        {"posting_key": "linkedin:2", "locations": ["Remote"],
         "min_years_signal": "Not explicit", "min_years_lower": None,
         "unresolved": ["min_years"]},
    ]
    (run_dir / "jobs.json").write_text(json.dumps(jobs))

    items = [
        {"posting_key": "linkedin:1", "location": "Austin, TX; Remote", "min_years_signal": "3+"},
        {"posting_key": "linkedin:1", "location": "New York, NY", "extra": "x"},
        {"posting_key": "linkedin:2", "min_years_signal": "banana"},
        {"posting_key": "linkedin:999", "location": "Boston, MA"},
        {"posting_key": "linkedin:2", "location": "Boston, MA"},
    ]
    f = run_dir / "extractions.json"
    f.write_text(json.dumps(items))

    rc = cli.main(["apply-extractions", "--file", str(f)])
    assert rc == 0
    out = _last_json(capsys)
    assert out["applied"] == 1
    assert out["skipped_invalid"] == 2
    assert out["skipped_unknown"] == 2

    merged = {j["posting_key"]: j for j in json.loads((run_dir / "jobs.json").read_text())}
    j1 = merged["linkedin:1"]
    assert j1["locations"] == ["Austin, TX", "Remote"]
    assert j1["min_years_signal"] == "3+"
    assert j1["min_years_lower"] == 3
    assert j1["unresolved"] == []
    # linkedin:2 had only min_years unresolved and got no valid min_years signal
    assert merged["linkedin:2"]["unresolved"] == ["min_years"]


def test_apply_extractions_missing_file_exits_2(data_dir, capsys):
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    (data_dir / "runs" / "latest").write_text("r")
    rc = cli.main(["apply-extractions", "--file", str(data_dir / "nope.json")])
    assert rc == 2
    assert "error" in _last_json(capsys)


def _backfill_page(pid, source_type_name, area_opts, ext_id):
    def rt(v):
        return {"type": "rich_text", "rich_text": [{"text": {"content": v}, "plain_text": v}]}

    return {
        "id": pid,
        "properties": {
            "Source Type": {"type": "select", "select": {"name": source_type_name} if source_type_name else None},
            "Job Area": {"type": "multi_select", "multi_select": [{"name": a} for a in area_opts]},
            "External ID": rt(ext_id),
            "Posting Key": rt(f"linkedin:{ext_id}"),
            "Job Title": {"type": "title", "title": [{"text": {"content": "ML Engineer"}, "plain_text": "ML Engineer"}]},
        },
    }


def test_backfill_dry_run_then_live(data_dir, monkeypatch, capsys):
    import jobs.notion as notion_mod

    details = data_dir / "details"
    details.mkdir(parents=True, exist_ok=True)
    (details / "4000000002.html").write_text(read_fixture("detail_junior.html"))

    pages = [
        _backfill_page("pg1", "LinkedIn", [], "3999999998"),       # off-label source, no detail
        _backfill_page("pg2", "Direct company", [], "4000000002"),  # empty area, has detail
        _backfill_page("pg3", "Direct company", [], "3999999997"),  # empty area, no detail
    ]
    patches = []

    class Fake:
        def __init__(self, *a, **k):
            pass

        def check_schema(self):
            pass

        def query_all(self):
            return iter(pages)

        def update_page(self, page_id, props):
            patches.append((page_id, props))
            return {}

    monkeypatch.setattr(notion_mod, "NotionClient", Fake)

    rc = cli.main(["backfill", "--dry-run"])
    assert rc == 0
    out = _last_json(capsys)
    assert (out["source_type_updated"], out["job_area_updated"], out["skipped_no_detail"]) == (1, 1, 1)
    assert patches == []

    rc = cli.main(["backfill"])
    assert rc == 0
    assert len(patches) == 2
    patched = {pid for pid, _ in patches}
    assert patched == {"pg1", "pg2"}
    assert "Source Type" in dict(patches)["pg1"]
    assert "Job Area" in dict(patches)["pg2"]


def test_daily_dry_run_returns_zero_and_prints_unresolved(data_dir, monkeypatch, capsys):
    import jobs.notion as notion_mod

    install_client(monkeypatch)
    monkeypatch.setattr(notion_mod, "NotionClient", _FakeNotion)

    rc = cli.main(["daily", "--dry-run", "--date-window", "past_24h"])
    assert rc == 0
    out = _last_json(capsys)
    assert "unresolved" in out
    assert out["status"] in {"success", "partial"}


def test_daily_rejects_when_lock_held_by_live_pid(data_dir, capsys):
    import os
    import time

    runs = data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / ".lock").write_text(f"{os.getpid()} {time.time()}")

    rc = cli.main(["daily", "--dry-run"])
    assert rc == 2
    assert _last_json(capsys)["error"] == "another run is active"


def test_daily_overwrites_dead_pid_lock(data_dir, monkeypatch):
    import jobs.notion as notion_mod

    install_client(monkeypatch)
    monkeypatch.setattr(notion_mod, "NotionClient", _FakeNotion)
    runs = data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / ".lock").write_text("999999 1.0")  # PID above macOS PID_MAX, ancient start

    rc = cli.main(["daily", "--dry-run", "--date-window", "past_24h"])
    assert rc == 0
