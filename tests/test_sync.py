import json

from jobs import cache, notion, sync
from jobs.models import Job, Refresh


class FakeClient:
    """Records create/update calls; raises NotionError for keys/page_ids in
    `fail`. query_by_key returns nothing (no crashed-create recovery here)."""

    def __init__(self, fail=None):
        self.creates = []
        self.updates = []
        self.fail = set(fail or [])
        self._n = 0

    def _key(self, properties):
        rt = properties.get("Posting Key", {}).get("rich_text", [])
        return rt[0]["text"]["content"] if rt else None

    def create_page(self, properties):
        key = self._key(properties)
        if key in self.fail:
            raise notion.NotionError("create failed")
        self._n += 1
        pid = f"page-{self._n}"
        self.creates.append((key, properties))
        return pid

    def update_page(self, page_id, properties):
        if page_id in self.fail:
            raise notion.NotionError("update failed")
        self.updates.append((page_id, properties))
        return {"id": page_id}

    def query_by_key(self, key):
        return None


def conn_at(tmp_path):
    return cache.connect(str(tmp_path / "jobs.sqlite3"))


def write_run(run_dir, jobs=None, refreshes=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "jobs.json").write_text(json.dumps(jobs or []))
    (run_dir / "refreshes.json").write_text(json.dumps(refreshes or []))


def a_job(posting_key="linkedin:100", job_ids=None, date_posted="2026-09-05"):
    return Job(
        posting_key=posting_key,
        job_ids=job_ids or ["100"],
        sibling_key="beta labs::ml engineer",
        title="ML Engineer",
        company="Beta Labs",
        locations=["Austin, TX"],
        date_posted=date_posted,
        url="https://www.linkedin.com/jobs/view/100/",
        min_years_signal="2+",
    ).to_dict()


def seed_row(conn, posting_key="linkedin:100", page_id="pg-100", date_posted="2026-09-01",
             locations=None, job_ids=None, notion_last_seen="2026-09-01", applied=0):
    cache.insert_job_row(conn, {
        "posting_key": posting_key,
        "page_id": page_id,
        "sibling_key": "beta labs::ml engineer",
        "title": "ML Engineer",
        "company": "Beta Labs",
        "locations": locations or ["Austin, TX"],
        "job_ids": job_ids or ["100"],
        "date_posted": date_posted,
        "first_seen": "2026-08-20",
        "last_seen": date_posted,
        "notion_last_seen": notion_last_seen,
        "applied": applied,
        "neglected": 0,
        "origin": "bootstrap",
    })


def test_new_job_plans_create(tmp_path):
    conn = conn_at(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    write_run(run_dir, jobs=[a_job()])
    ops = sync.plan("r1", conn, "2026-09-05", run_dir)
    assert [o.op for o in ops] == ["create"]
    result = sync.apply(ops, FakeClient(), conn, "r1", "2026-09-05", dry_run=False)
    assert result["created"] == 1 and result["failed"] == 0
    row = cache.get_job(conn, "linkedin:100")
    assert row["page_id"] == "page-1" and row["origin"] == "pipeline"
    conn.close()


def test_known_job_newer_date_updates_date_posted(tmp_path):
    conn = conn_at(tmp_path)
    seed_row(conn, date_posted="2026-09-01")
    run_dir = tmp_path / "runs" / "r1"
    refresh = Refresh("linkedin:100", "pg-100", "2026-09-05", [], []).to_dict()
    write_run(run_dir, refreshes=[refresh])
    ops = sync.plan("r1", conn, "2026-09-05", run_dir)
    assert len(ops) == 1 and ops[0].op == "update"
    assert "Date Posted" in ops[0].properties
    conn.close()


def test_known_job_new_city_updates_location_and_ids(tmp_path):
    conn = conn_at(tmp_path)
    seed_row(conn, date_posted="2026-09-05", locations=["Austin, TX"], job_ids=["100"])
    run_dir = tmp_path / "runs" / "r1"
    refresh = Refresh("linkedin:100", "pg-100", "2026-09-05", ["Remote"], ["105"]).to_dict()
    write_run(run_dir, refreshes=[refresh])
    ops = sync.plan("r1", conn, "2026-09-05", run_dir)
    props = ops[0].properties
    assert "Location" in props and "External ID" in props
    assert props["Location"]["rich_text"][0]["text"]["content"] == "Austin, TX; Remote"
    conn.close()


def test_no_change_fresh_last_seen_is_no_op(tmp_path):
    conn = conn_at(tmp_path)
    seed_row(conn, date_posted="2026-09-05", notion_last_seen="2026-09-05")
    run_dir = tmp_path / "runs" / "r1"
    refresh = Refresh("linkedin:100", "pg-100", "2026-09-05", [], []).to_dict()
    write_run(run_dir, refreshes=[refresh])
    ops = sync.plan("r1", conn, "2026-09-05", run_dir)
    assert ops == []
    conn.close()


def test_applied_job_updates_without_applied(tmp_path):
    conn = conn_at(tmp_path)
    seed_row(conn, date_posted="2026-09-01", applied=1)
    run_dir = tmp_path / "runs" / "r1"
    refresh = Refresh("linkedin:100", "pg-100", "2026-09-05", [], []).to_dict()
    write_run(run_dir, refreshes=[refresh])
    ops = sync.plan("r1", conn, "2026-09-05", run_dir)
    assert "Applied" not in ops[0].properties
    conn.close()


def test_rerun_replays_only_failed_op(tmp_path):
    conn = conn_at(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    write_run(run_dir, jobs=[a_job("linkedin:100", ["100"]), a_job("linkedin:200", ["200"])])
    ops = sync.plan("r1", conn, "2026-09-05", run_dir)
    # first run: the 200 create fails, the 100 create confirms
    result = sync.apply(ops, FakeClient(fail={"linkedin:200"}), conn, "r1", "2026-09-05", dry_run=False)
    assert result["created"] == 1 and result["failed"] == 1

    ops2 = sync.plan("r1", conn, "2026-09-05", run_dir)
    assert [o.posting_key for o in ops2] == ["linkedin:200"]
    result2 = sync.apply(ops2, FakeClient(), conn, "r1", "2026-09-05", dry_run=False)
    assert result2["created"] == 1 and result2["failed"] == 0
    conn.close()


def test_dry_run_journals_nothing(tmp_path, capsys):
    conn = conn_at(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    write_run(run_dir, jobs=[a_job()])
    ops = sync.plan("r1", conn, "2026-09-05", run_dir, journal_ops=False)
    result = sync.apply(ops, FakeClient(), conn, "r1", "2026-09-05", dry_run=True)
    assert result["planned_create"] == 1
    rows = list(conn.execute("SELECT count(*) AS n FROM sync_ops"))
    assert rows[0]["n"] == 0
    assert "create linkedin:100" in capsys.readouterr().out
    conn.close()
