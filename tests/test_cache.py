from jobs import cache
from jobs.models import Card


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


def test_tables_exist_after_connect(tmp_path):
    conn = cache.connect(str(tmp_path / "jobs.sqlite3"))
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "sightings", "rejections"} <= names
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
