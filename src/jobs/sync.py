"""Plan and apply Notion writes, resumably.

`plan` turns a run's `jobs.json` (new jobs) and `refreshes.json` (known-job
updates) into a list of `Op`, skipping ops already confirmed for the run and
recovering a create whose request was sent but never confirmed. `apply` sends
each op, journals its state in `sync_ops`, and advances the cache only after
Notion confirms, so the cache keeps mirroring Notion even on a partial run."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import cache, notion
from .models import Job, Refresh

log = logging.getLogger(__name__)


@dataclass
class Op:
    posting_key: str
    op: str  # 'create' | 'update'
    properties: dict
    page_id: str | None = None
    title: str = ""
    company: str = ""
    job: Job | None = None  # for creates: upserted into the cache on confirm
    refresh: Refresh | None = None  # for refresh-updates: applied on confirm

    @property
    def fields(self) -> list[str]:
        return sorted(self.properties.keys())

    @property
    def sends_last_seen(self) -> bool:
        return "Last Seen" in self.properties


def _url_from_posting_key(posting_key: str) -> str:
    jid = posting_key.split(":", 1)[-1]
    return f"https://www.linkedin.com/jobs/view/{jid}/"


def _job_from_row(row: dict) -> Job:
    """Reconstruct a Job from a cache row for a refresh that must be created
    (bootstrap never saw it). The URL is derived from the posting key."""
    job_ids = json.loads(row["job_ids"]) if row.get("job_ids") else []
    return Job(
        posting_key=row["posting_key"],
        job_ids=job_ids,
        sibling_key=row["sibling_key"],
        title=row.get("title") or "",
        company=row.get("company") or "",
        locations=json.loads(row["locations"]) if row.get("locations") else [],
        date_posted=row["date_posted"],
        url=_url_from_posting_key(row["posting_key"]),
    )


def _confirmed(conn, run_id: str, posting_key: str) -> bool:
    op = cache.get_op(conn, run_id, posting_key)
    return op is not None and op["state"] == "confirmed"


def plan(run_id: str, conn, run_date: str, run_dir: Path, client=None,
         journal_ops: bool = True) -> list[Op]:
    """Build the create/update plan for a run. Journals each op as 'planned'
    unless `journal_ops` is False (a dry run). When `client` is given, a create
    left in 'sent' by a crashed prior run is resolved against Notion first."""
    ops: list[Op] = []

    jobs_path = run_dir / "jobs.json"
    jobs = json.loads(jobs_path.read_text()) if jobs_path.exists() else []
    for raw in jobs:
        job = Job.from_dict(raw)
        key = job.posting_key
        prior = cache.get_op(conn, run_id, key)
        if prior and prior["state"] == "confirmed":
            continue
        row = cache.get_job(conn, key)
        if row and row.get("page_id"):
            props = {"Last Seen": notion.date(run_date)}
            op = Op(key, "update", props, page_id=row["page_id"], title=job.title, company=job.company)
        elif prior and prior["state"] == "sent" and client is not None:
            page = client.query_by_key(key)
            if page:
                cache.journal(conn, run_id, key, "create", "confirmed", page_id=page["id"])
                cache.upsert_job(conn, job, run_date, "pipeline", page_id=page["id"])
                cache.set_notion_last_seen(conn, key, run_date)
                continue
            op = Op(key, "create", notion.build_create_properties(job, run_date),
                    title=job.title, company=job.company, job=job)
        else:
            op = Op(key, "create", notion.build_create_properties(job, run_date),
                    title=job.title, company=job.company, job=job)
        if journal_ops:
            cache.journal(conn, run_id, key, op.op, "planned", payload=op.properties, page_id=op.page_id)
        ops.append(op)

    refr_path = run_dir / "refreshes.json"
    refreshes = json.loads(refr_path.read_text()) if refr_path.exists() else []
    for raw in refreshes:
        refresh = Refresh.from_dict(raw)
        key = refresh.posting_key
        if _confirmed(conn, run_id, key):
            continue
        row = cache.get_job(conn, key)
        if row is None:
            log.warning("refresh for unknown job %s, skipping", key)
            continue
        if not row.get("page_id"):
            job = _job_from_row(row)
            op = Op(key, "create", notion.build_create_properties(job, run_date),
                    title=job.title, company=job.company, job=job)
        else:
            props = notion.build_update_properties(refresh, row, run_date)
            if not props:
                continue
            op = Op(key, "update", props, page_id=row["page_id"],
                    title=row.get("title", ""), company=row.get("company", ""), refresh=refresh)
        if journal_ops:
            cache.journal(conn, run_id, key, op.op, "planned", payload=op.properties, page_id=op.page_id)
        ops.append(op)

    return ops


def apply(ops: list[Op], client, conn, run_id: str, run_date: str, dry_run: bool) -> dict:
    """Send each op. Returns counts and a status ('success' or 'partial')."""
    planned_create = sum(1 for o in ops if o.op == "create")
    planned_update = sum(1 for o in ops if o.op == "update")

    if dry_run:
        for o in ops:
            if o.op == "create":
                print(f"create {o.posting_key} {o.title} @ {o.company}")
            else:
                print(f"update {o.posting_key} fields={o.fields}")
        return {
            "planned_create": planned_create,
            "planned_update": planned_update,
            "created": 0,
            "updated": 0,
            "failed": 0,
            "status": "success",
        }

    created = updated = failed = 0
    for o in ops:
        cache.journal(conn, run_id, o.posting_key, o.op, "sent",
                      payload=o.properties, page_id=o.page_id)
        try:
            if o.op == "create":
                page_id = client.create_page(o.properties)
                cache.journal(conn, run_id, o.posting_key, "create", "confirmed", page_id=page_id)
                if o.job is not None:
                    cache.upsert_job(conn, o.job, run_date, "pipeline", page_id=page_id)
                cache.set_notion_last_seen(conn, o.posting_key, run_date)
                created += 1
            else:
                client.update_page(o.page_id, o.properties)
                cache.journal(conn, run_id, o.posting_key, "update", "confirmed", page_id=o.page_id)
                if o.refresh is not None:
                    cache.apply_refresh(conn, o.refresh, run_date)
                if o.sends_last_seen:
                    cache.set_notion_last_seen(conn, o.posting_key, run_date)
                updated += 1
        except notion.NotionError as e:
            cache.journal(conn, run_id, o.posting_key, o.op, "failed",
                          payload=o.properties, page_id=o.page_id, error=str(e))
            log.warning("sync %s %s failed: %s", o.op, o.posting_key, e)
            failed += 1

    return {
        "planned_create": planned_create,
        "planned_update": planned_update,
        "created": created,
        "updated": updated,
        "failed": failed,
        "status": "partial" if failed else "success",
    }
