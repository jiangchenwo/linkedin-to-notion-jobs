from jobs import cache
from jobs.models import Card, Job, Refresh


def make_card(job_id="4000000001"):
    return Card(
        job_id=job_id,
        title="AI Engineer",
        company="Acme Corp",
        location="New York, NY",
        date_posted="2026-09-05",
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        keyword="AI engineer",
    )


def make_job(posting_key="linkedin:100", job_ids=("100", "200"), date_posted="2026-09-05"):
    return Job(
        posting_key=posting_key,
        job_ids=list(job_ids),
        sibling_key="beta labs::ml engineer",
        title="ML Engineer",
        company="Beta Labs",
        locations=["Austin, TX"],
        date_posted=date_posted,
        url="https://www.linkedin.com/jobs/view/100/",
    )


def test_tables_exist_after_connect(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "sightings", "rejections", "jobs", "sync_ops"} <= names
    conn.close()


def test_find_job_by_id_matches_any_id_in_list(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    cache.upsert_job(conn, make_job(job_ids=("100", "200")), "2026-09-05", "pipeline", page_id="pg")
    assert cache.find_job_by_id(conn, "200")["posting_key"] == "linkedin:100"
    assert cache.find_job_by_id(conn, "999") is None
    conn.close()


def test_find_job_by_sibling_respects_window(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    cache.upsert_job(conn, make_job(), "2026-08-01", "pipeline")
    # last_seen 2026-08-01: inside a window starting 2026-07-15, outside 2026-08-15
    assert cache.find_job_by_sibling(conn, "beta labs::ml engineer", "2026-07-15") is not None
    assert cache.find_job_by_sibling(conn, "beta labs::ml engineer", "2026-08-15") is None
    conn.close()


def test_apply_refresh_appends_without_dupes_and_keeps_key(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    cache.upsert_job(conn, make_job(job_ids=("100", "200")), "2026-09-05", "pipeline", page_id="pg")
    # a new sibling with a lower id and a new city, plus a duplicate city
    refresh = Refresh("linkedin:100", "pg", "2026-09-06",
                      new_locations=["austin, tx", "Remote"], new_job_ids=["50", "200"])
    cache.apply_refresh(conn, refresh, "2026-09-06")
    row = cache.get_job(conn, "linkedin:100")
    import json
    assert json.loads(row["job_ids"]) == ["50", "100", "200"]
    assert json.loads(row["locations"]) == ["Austin, TX", "Remote"]
    assert row["date_posted"] == "2026-09-06"
    assert row["posting_key"] == "linkedin:100"  # unchanged despite lower id
    conn.close()


def test_journal_and_pending_ops_transitions(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    cache.journal(conn, "r1", "linkedin:100", "create", "planned", payload={"a": 1})
    assert len(cache.pending_ops(conn, "r1")) == 1
    cache.journal(conn, "r1", "linkedin:100", "create", "confirmed", page_id="pg")
    assert cache.pending_ops(conn, "r1") == []
    assert cache.get_op(conn, "r1", "linkedin:100")["state"] == "confirmed"
    conn.close()


def test_record_sighting_is_idempotent(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    card = make_card()
    cache.record_sighting(conn, card, "run1")
    cache.record_sighting(conn, card, "run1")
    count = conn.execute(
        "SELECT count(*) AS n FROM sightings WHERE job_id=? AND run_id=?",
        (card.job_id, "run1"),
    ).fetchone()["n"]
    assert count == 1
    conn.close()


def test_last_success_finished_at(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    assert cache.last_success_finished_at(conn) is None
    cache.start_run(conn, "run1", "past_24h")
    cache.finish_run(conn, "run1", "success", {"ok": True})
    assert cache.last_success_finished_at(conn) is not None
    conn.close()
