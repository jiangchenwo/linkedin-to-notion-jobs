"""SQLite cache: the dedupe source of truth. Phase 1 owns runs, sightings, and
rejections; Phase 3 adds jobs and sync_ops."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .extract import sibling_key
from .models import Job, Refresh

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    date_window TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS sightings (
    job_id TEXT,
    run_id TEXT,
    date_posted TEXT,
    location TEXT,
    title TEXT,
    company TEXT,
    sibling_key TEXT,
    PRIMARY KEY (job_id, run_id)
);
CREATE TABLE IF NOT EXISTS rejections (
    run_id TEXT,
    job_id TEXT,
    posting_key TEXT,
    title TEXT,
    company TEXT,
    rule TEXT,
    stage TEXT,
    PRIMARY KEY (run_id, job_id, rule)
);
CREATE TABLE IF NOT EXISTS jobs (
    posting_key TEXT PRIMARY KEY,
    page_id TEXT,
    sibling_key TEXT NOT NULL,
    title TEXT,
    company TEXT,
    locations TEXT NOT NULL,
    job_ids TEXT NOT NULL,
    date_posted TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    notion_last_seen TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    neglected INTEGER NOT NULL DEFAULT 0,
    origin TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_sibling ON jobs(sibling_key, last_seen);
CREATE TABLE IF NOT EXISTS sync_ops (
    run_id TEXT,
    posting_key TEXT,
    op TEXT,
    state TEXT,
    payload TEXT,
    page_id TEXT,
    error TEXT,
    updated_at TEXT,
    PRIMARY KEY (run_id, posting_key)
);
"""


def _env_file(path: str = ".env") -> dict[str, str]:
    """Parse KEY=value lines from .env by hand (no dependency)."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str | None = None) -> sqlite3.Connection:
    if path is None:
        path = (
            os.environ.get("JOBS_DB_PATH")
            or _env_file().get("JOBS_DB_PATH")
            or "data/jobs.sqlite3"
        )
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def start_run(conn: sqlite3.Connection, run_id: str, date_window: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, started_at, status, date_window) "
        "VALUES (?, ?, 'running', ?)",
        (run_id, _now(), date_window),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection, run_id: str, status: str, summary: dict
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE run_id = ?",
        (_now(), status, json.dumps(summary), run_id),
    )
    conn.commit()


def last_success_finished_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT finished_at FROM runs WHERE status = 'success' "
        "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    return row["finished_at"] if row else None


def record_sighting(conn: sqlite3.Connection, card, run_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sightings "
        "(job_id, run_id, date_posted, location, title, company, sibling_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            card.job_id,
            run_id,
            card.date_posted,
            card.location,
            card.title,
            card.company,
            sibling_key(card.company, card.title),
        ),
    )
    conn.commit()


def record_rejection(
    conn: sqlite3.Connection, run_id: str, item, rule: str, stage: str
) -> None:
    job_id = getattr(item, "job_id", "")
    posting_key = getattr(item, "posting_key", "") or f"linkedin:{job_id}"
    conn.execute(
        "INSERT OR REPLACE INTO rejections "
        "(run_id, job_id, posting_key, title, company, rule, stage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            job_id,
            posting_key,
            getattr(item, "title", ""),
            getattr(item, "company", ""),
            rule,
            stage,
        ),
    )
    conn.commit()


def latest_sighting(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sightings WHERE job_id = ? ORDER BY run_id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


# --- jobs: the "already in Notion" source of truth (Phase 3) ---


def count_jobs(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]


def get_job(conn: sqlite3.Connection, posting_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM jobs WHERE posting_key = ?", (posting_key,)
    ).fetchone()
    return dict(row) if row else None


def all_jobs(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM jobs")]


def find_job_by_id(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """A known job carrying `job_id` in its comma/JSON id list, or None."""
    row = conn.execute(
        "SELECT jobs.* FROM jobs, json_each(jobs.job_ids) je "
        "WHERE je.value = ? LIMIT 1",
        (str(job_id),),
    ).fetchone()
    return dict(row) if row else None


def find_job_by_sibling(
    conn: sqlite3.Connection, sibling_key_value: str, since_date: str
) -> dict | None:
    """Newest known job for a sibling key whose last_seen is on or after
    `since_date` (YYYY-MM-DD); None outside the window."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE sibling_key = ? AND last_seen >= ? "
        "ORDER BY last_seen DESC LIMIT 1",
        (sibling_key_value, since_date),
    ).fetchone()
    return dict(row) if row else None


def upsert_job(
    conn: sqlite3.Connection,
    job: Job,
    run_date: str,
    origin: str,
    page_id: str | None = None,
) -> None:
    """Insert a new job row or refresh the mutable fields of an existing one.
    first_seen, applied, neglected, and a non-null page_id survive an update."""
    conn.execute(
        """
        INSERT INTO jobs (posting_key, page_id, sibling_key, title, company,
                          locations, job_ids, date_posted, first_seen, last_seen,
                          notion_last_seen, applied, neglected, origin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, ?)
        ON CONFLICT(posting_key) DO UPDATE SET
            page_id = COALESCE(excluded.page_id, jobs.page_id),
            sibling_key = excluded.sibling_key,
            title = excluded.title,
            company = excluded.company,
            locations = excluded.locations,
            job_ids = excluded.job_ids,
            date_posted = excluded.date_posted,
            last_seen = excluded.last_seen
        """,
        (
            job.posting_key,
            page_id,
            job.sibling_key,
            job.title,
            job.company,
            json.dumps(job.locations),
            json.dumps(job.job_ids),
            job.date_posted,
            run_date,
            run_date,
            origin,
        ),
    )
    conn.commit()


def insert_job_row(conn: sqlite3.Connection, row: dict) -> None:
    """Insert a bootstrap/resync row. `locations` and `job_ids` are lists."""
    conn.execute(
        """
        INSERT OR REPLACE INTO jobs
            (posting_key, page_id, sibling_key, title, company, locations,
             job_ids, date_posted, first_seen, last_seen, notion_last_seen,
             applied, neglected, origin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["posting_key"],
            row.get("page_id"),
            row["sibling_key"],
            row.get("title", ""),
            row.get("company", ""),
            json.dumps(row.get("locations", [])),
            json.dumps(row.get("job_ids", [])),
            row["date_posted"],
            row["first_seen"],
            row["last_seen"],
            row.get("notion_last_seen"),
            int(row.get("applied", 0)),
            int(row.get("neglected", 0)),
            row.get("origin", "bootstrap"),
        ),
    )
    conn.commit()


def delete_job(conn: sqlite3.Connection, posting_key: str) -> None:
    conn.execute("DELETE FROM jobs WHERE posting_key = ?", (posting_key,))
    conn.commit()


def apply_refresh(
    conn: sqlite3.Connection, refresh: Refresh, run_date: str
) -> None:
    """Merge a Refresh into its job row: append new cities and IDs (no dupes),
    set date_posted to the refresh value, bump last_seen. posting_key is kept."""
    row = conn.execute(
        "SELECT locations, job_ids FROM jobs WHERE posting_key = ?",
        (refresh.posting_key,),
    ).fetchone()
    if row is None:
        return
    locations = json.loads(row["locations"])
    have = {loc.lower() for loc in locations}
    for loc in refresh.new_locations:
        if loc.lower() not in have:
            locations.append(loc)
            have.add(loc.lower())
    job_ids = json.loads(row["job_ids"])
    have_ids = set(job_ids)
    for jid in refresh.new_job_ids:
        if jid not in have_ids:
            job_ids.append(jid)
            have_ids.add(jid)
    job_ids = sorted(job_ids, key=int)
    conn.execute(
        "UPDATE jobs SET locations = ?, job_ids = ?, date_posted = ?, "
        "last_seen = ? WHERE posting_key = ?",
        (
            json.dumps(locations),
            json.dumps(job_ids),
            refresh.date_posted,
            run_date,
            refresh.posting_key,
        ),
    )
    conn.commit()


def mark_seen(conn: sqlite3.Connection, posting_key: str, run_date: str) -> None:
    """Bump only last_seen. Fetch records that a known job was seen today; its
    Notion-mirrored fields (locations, ids, date_posted) advance only when a
    sync confirms, via apply_refresh."""
    conn.execute(
        "UPDATE jobs SET last_seen = ? WHERE posting_key = ?", (run_date, posting_key)
    )
    conn.commit()


def set_page_id(conn: sqlite3.Connection, posting_key: str, page_id: str) -> None:
    conn.execute(
        "UPDATE jobs SET page_id = ? WHERE posting_key = ?", (page_id, posting_key)
    )
    conn.commit()


def set_notion_last_seen(
    conn: sqlite3.Connection, posting_key: str, value: str
) -> None:
    conn.execute(
        "UPDATE jobs SET notion_last_seen = ? WHERE posting_key = ?",
        (value, posting_key),
    )
    conn.commit()


# --- sync_ops: the resumable journal of Notion writes (Phase 3) ---


def journal(
    conn: sqlite3.Connection,
    run_id: str,
    posting_key: str,
    op: str,
    state: str,
    payload: dict | None = None,
    page_id: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO sync_ops
            (run_id, posting_key, op, state, payload, page_id, error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            posting_key,
            op,
            state,
            json.dumps(payload) if payload is not None else None,
            page_id,
            error,
            _now(),
        ),
    )
    conn.commit()


def get_op(
    conn: sqlite3.Connection, run_id: str, posting_key: str
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sync_ops WHERE run_id = ? AND posting_key = ?",
        (run_id, posting_key),
    ).fetchone()
    return dict(row) if row else None


def pending_ops(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """Ops for a run that still need work: planned, sent, or failed."""
    rows = conn.execute(
        "SELECT * FROM sync_ops WHERE run_id = ? "
        "AND state IN ('planned', 'sent', 'failed')",
        (run_id,),
    )
    return [dict(r) for r in rows]
