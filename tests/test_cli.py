import json
import shutil
from datetime import date

import httpx
import pytest

from jobs import cache, cli
from jobs.linkedin import GuestClient

from conftest import REPO_DATA, read_fixture


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


def test_fetch_partial_when_detail_blocked(data_dir, monkeypatch, capsys):
    install_client(monkeypatch, detail_status=429, block_limit=1)
    rc = cli.main(["fetch", "--date-window", "past_24h"])
    assert rc == 1

    summary = _last_json(capsys)
    assert summary["status"] == "partial"
    assert summary["blocked"] is True

    run_id = summary["run_id"]
    assert (data_dir / "runs" / run_id / "cards.json").exists()
