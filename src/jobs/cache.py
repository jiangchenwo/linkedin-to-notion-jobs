"""SQLite cache: the dedupe source of truth. Phase 1 owns runs, sightings, and
rejections; Phase 3 adds jobs and sync_ops."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .extract import sibling_key

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
