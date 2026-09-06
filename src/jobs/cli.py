"""Command-line entry point. Every subcommand prints human-readable progress to
stderr and exactly one JSON object as the last line of stdout. Phase 1
implements `fetch`; the rest are stubs until later phases."""

import argparse
import io
import json
import logging
import os
import re
import sys
import tomllib
import traceback
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import cache, notify, sync
from .extract import areas, build_job, canon, min_years, sibling_key
from .filters import card_stage, detail_stage
from .linkedin import Blocked, BudgetExhausted, GuestClient, load_keywords, parse_detail
from .models import Card, Refresh

log = logging.getLogger("jobs")


def _today() -> date:
    return date.today()


def _data_dir() -> Path:
    return Path(os.environ.get("JOBS_DATA_DIR", "data"))


def _runs_dir() -> Path:
    return _data_dir() / "runs"


def _details_dir() -> Path:
    return _data_dir() / "details"


def _logs_dir() -> Path:
    return _data_dir() / "logs"


def _lock_path() -> Path:
    return _runs_dir() / ".lock"


def build_client(search_config: dict) -> GuestClient:
    """Factory seam; tests monkeypatch this to inject a mocked transport."""
    return GuestClient(search_config=search_config)


def setup_logging() -> Path:
    _logs_dir().mkdir(parents=True, exist_ok=True)
    log_path = _logs_dir() / "jobs.log"
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(fmt)
        root.addHandler(stream)
        root.addHandler(file_handler)
        root.setLevel(logging.INFO)
    return log_path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock() -> bool:
    """Return True if the lock was taken. False if another run is active
    (PID alive and younger than 2 h)."""
    _runs_dir().mkdir(parents=True, exist_ok=True)
    lock = _lock_path()
    if lock.exists():
        try:
            pid_s, start_s = lock.read_text().split()
            pid, start = int(pid_s), float(start_s)
        except (ValueError, IndexError):
            pid, start = -1, 0.0
        import time

        if _pid_alive(pid) and (time.time() - start) < 2 * 3600:
            return False
    import time

    lock.write_text(f"{os.getpid()} {time.time()}")
    return True


def release_lock() -> None:
    _lock_path().unlink(missing_ok=True)


def _load_companies() -> dict[str, set[str]]:
    """The three company lists, canon()'d. `aggregator` also drives the card
    filter; `startup`/`reliable` set the Source Type label in extraction."""
    with open(_data_dir() / "companies.toml", "rb") as f:
        data = tomllib.load(f)
    return {
        name: {canon(c) for c in data.get(name, [])}
        for name in ("aggregator", "startup", "reliable")
    }


def _load_aggregators() -> set[str]:
    return _load_companies()["aggregator"]


def _decide_window(conn, requested: str | None) -> str:
    if requested:
        return requested
    last = cache.last_success_finished_at(conn)
    if last is None:
        return "past_week"
    try:
        stale = datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(
            hours=36
        )
    except ValueError:
        stale = True
    return "past_week" if stale else "past_24h"


def _group_siblings(cards):
    groups: dict[str, list] = {}
    for c in cards:
        groups.setdefault(sibling_key(c.company, c.title), []).append(c)
    out = []
    for key, cs in groups.items():
        cs_sorted = sorted(cs, key=lambda c: int(c.job_id))
        out.append({"sibling_key": key, "cards": cs_sorted, "lowest": cs_sorted[0].job_id})
    return out


def _build_refresh(known: dict, cards, run_date_iso: str) -> Refresh:
    """A Refresh capturing what this run's cards add to a known job: cities and
    IDs not already recorded, and the newest repost date."""
    known_locs = json.loads(known["locations"])
    have_locs = {loc.lower() for loc in known_locs}
    known_ids = set(json.loads(known["job_ids"]))
    new_locations: list[str] = []
    for c in cards:
        if c.location and c.location.lower() not in have_locs:
            new_locations.append(c.location)
            have_locs.add(c.location.lower())
    new_job_ids = [c.job_id for c in cards if c.job_id not in known_ids]
    date_posted = max([known["date_posted"], *[c.date_posted for c in cards]])
    return Refresh(
        posting_key=known["posting_key"],
        page_id=known["page_id"],
        date_posted=date_posted,
        new_locations=new_locations,
        new_job_ids=new_job_ids,
    )


def _refresh_is_meaningful(known: dict, refresh: Refresh, run_date) -> bool:
    """Worth an entry in refreshes.json: the date advanced, a city or ID was
    appended, or Notion's Last Seen is unset or older than 7 days."""
    if refresh.date_posted > known["date_posted"]:
        return True
    if refresh.new_locations or refresh.new_job_ids:
        return True
    nls = known["notion_last_seen"]
    if not nls:
        return True
    try:
        return (run_date - date.fromisoformat(nls[:10])) > timedelta(days=7)
    except ValueError:
        return True


def cmd_fetch(args) -> int:
    conn = cache.connect()
    run_date = _today()
    date_window = _decide_window(conn, args.date_window)
    held = getattr(args, "_locked", False)

    if not held and not acquire_lock():
        print(json.dumps({"error": "another run is active"}))
        return 2

    try:
        run_id = args.run_id or datetime.now().strftime("%Y-%m-%dT%H%M%S")
        cache.start_run(conn, run_id, date_window)
        run_dir = _runs_dir() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (_runs_dir() / "latest").write_text(run_id)

        keywords, search_config = load_keywords(str(_data_dir() / "keywords.toml"))
        aggregators = _load_aggregators()
        max_pages = int(search_config.get("max_pages", 20))
        client = build_client(search_config)

        blocked = False
        errors: list[str] = []
        merged: dict[str, object] = {}
        seen_ids: set[str] = set()
        try:
            for keyword in keywords:
                for card in client.search_all(
                    keyword, date_window, run_date, max_pages, seen=seen_ids
                ):
                    merged.setdefault(card.job_id, card)
        except (Blocked, BudgetExhausted) as e:
            blocked = True
            errors.append(str(e))
            log.warning("search blocked for run %s: %s", run_id, e)

        cards_seen = len(merged)
        kept = []
        rejected_by_rule: dict[str, int] = {}
        for card in merged.values():
            rule = card_stage(card, run_date, aggregators)
            if rule:
                cache.record_rejection(conn, run_id, card, rule, "card")
                rejected_by_rule[rule] = rejected_by_rule.get(rule, 0) + 1
            else:
                kept.append(card)

        groups = _group_siblings(kept)
        run_date_iso = run_date.isoformat()
        window_start = (run_date - timedelta(days=30)).isoformat()
        details_fetched = 0
        details_skipped = 0
        refresh_count = 0
        refreshes_out: list[dict] = []
        groups_out = []
        for g in groups:
            cards = g["cards"]
            lowest = cards[0]
            known = None
            for c in cards:
                known = cache.find_job_by_id(conn, c.job_id)
                if known:
                    break
            if known is None:
                known = cache.find_job_by_sibling(conn, g["sibling_key"], window_start)
            if known is not None:
                details_skipped += 1
                refresh = _build_refresh(known, cards, run_date_iso)
                if _refresh_is_meaningful(known, refresh, run_date):
                    refreshes_out.append(refresh.to_dict())
                    refresh_count += 1
                cache.mark_seen(conn, known["posting_key"], run_date_iso)
                continue
            path = _details_dir() / f"{lowest.job_id}.html"
            detail_path = None
            if not blocked and (
                args.max_details is None or details_fetched < args.max_details
            ):
                try:
                    _, raw = client.detail(lowest.job_id)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(raw)
                    details_fetched += 1
                    detail_path = str(path)
                except (Blocked, BudgetExhausted) as e:
                    blocked = True
                    errors.append(str(e))
                    log.warning("detail blocked for run %s: %s", run_id, e)
            groups_out.append(
                {
                    "sibling_key": g["sibling_key"],
                    "posting_key": f"linkedin:{lowest.job_id}",
                    "cards": [c.to_dict() for c in cards],
                    "detail_job_id": lowest.job_id,
                    "detail_path": detail_path,
                }
            )

        for card in kept:
            cache.record_sighting(conn, card, run_id)

        (run_dir / "cards.json").write_text(json.dumps(groups_out, indent=2))
        (run_dir / "refreshes.json").write_text(json.dumps(refreshes_out, indent=2))

        status = "partial" if blocked else "success"
        summary = {
            "run_id": run_id,
            "date_window": date_window,
            "cards_seen": cards_seen,
            "cards_kept": len(kept),
            "refreshes": refresh_count,
            "details_fetched": details_fetched,
            "details_skipped": details_skipped,
            "blocked": blocked,
            "status": status,
            "cards_rejected_by_rule": rejected_by_rule,
            "errors": errors,
        }
        cache.finish_run(conn, run_id, status, summary)
        print(json.dumps({k: summary[k] for k in (
            "run_id", "date_window", "cards_seen", "cards_kept", "refreshes",
            "details_fetched", "details_skipped", "blocked", "status",
        )}))
        return 1 if blocked else 0
    except Exception as e:  # noqa: BLE001 - surface any crash as a failed run
        log.error("fetch failed: %s\n%s", e, traceback.format_exc())
        try:
            cache.finish_run(conn, args.run_id or "unknown", "failed", {"error": str(e)})
        except Exception:
            pass
        print(json.dumps({"error": str(e), "status": "failed"}))
        return 2
    finally:
        if not held:
            release_lock()
        conn.close()


def _resolve_run_id(args) -> str | None:
    if getattr(args, "run_id", None):
        return args.run_id
    latest = _runs_dir() / "latest"
    return latest.read_text().strip() if latest.exists() else None


def cmd_extract(args) -> int:
    run_id = _resolve_run_id(args)
    if not run_id:
        print(json.dumps({"error": "no run id and no data/runs/latest"}))
        return 2
    run_dir = _runs_dir() / run_id
    try:
        groups_raw = json.loads((run_dir / "cards.json").read_text())
    except (OSError, ValueError) as e:
        print(json.dumps({"error": f"cannot read cards.json: {e}"}))
        return 2

    conn = cache.connect()
    try:
        companies = _load_companies()
        run_date = _today().isoformat()
        jobs_out: list[dict] = []
        rejections_out: list[dict] = []
        unresolved_out: list[dict] = []
        rejected_by_rule: dict[str, int] = {}
        not_fetched = 0

        for g in groups_raw:
            detail_path = g.get("detail_path")
            if not detail_path:
                not_fetched += 1
                continue
            cards = [Card.from_dict(c) for c in g["cards"]]
            lowest = sorted(cards, key=lambda c: int(c.job_id))[0]
            posting_key = g.get("posting_key") or f"linkedin:{lowest.job_id}"
            try:
                html = Path(detail_path).read_text()
            except OSError as e:
                log.warning("detail file %s unreadable, skipping: %s", detail_path, e)
                continue
            detail = parse_detail(html, lowest.job_id)

            _, lower = min_years(detail)
            rule = detail_stage(lowest, detail, lower, companies)
            if rule:
                cache.record_rejection(conn, run_id, lowest, rule, "detail")
                rejections_out.append(
                    {
                        "posting_key": posting_key,
                        "job_id": lowest.job_id,
                        "title": lowest.title,
                        "company": lowest.company,
                        "rule": rule,
                    }
                )
                rejected_by_rule[rule] = rejected_by_rule.get(rule, 0) + 1
                continue

            job = build_job(
                {"sibling_key": g["sibling_key"], "cards": cards},
                detail,
                companies,
                run_date,
            )
            jobs_out.append(job.to_dict())
            if job.unresolved:
                unresolved_out.append(
                    {
                        "posting_key": job.posting_key,
                        "fields": job.unresolved,
                        "title": job.title,
                        "company": job.company,
                        "card_locations": [c.location for c in cards],
                        "description_text": detail.description_text[:6000],
                    }
                )

        (run_dir / "jobs.json").write_text(json.dumps(jobs_out, indent=2))
        (run_dir / "rejections.json").write_text(json.dumps(rejections_out, indent=2))
        unresolved_path = run_dir / "unresolved.json"
        unresolved_path.write_text(json.dumps(unresolved_out, indent=2))

        summary = {
            "run_id": run_id,
            "jobs": len(jobs_out),
            "rejected": len(rejections_out),
            "rejected_by_rule": rejected_by_rule,
            "not_fetched": not_fetched,
            "unresolved": len(unresolved_out),
            "unresolved_path": str(unresolved_path),
        }
        print(json.dumps(summary))
        return 0
    finally:
        conn.close()


def _build_run_summary(conn, run_id, run_dir, sync_result, run_date) -> dict:
    """Merge the fetch summary (from the runs table), the extract outputs (from
    run files), and the sync counts into a RunSummary-shaped dict."""
    from .models import RunSummary

    row = conn.execute(
        "SELECT started_at, date_window, summary_json FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    fetch = {}
    if row and row["summary_json"]:
        try:
            fetch = json.loads(row["summary_json"])
        except ValueError:
            fetch = {}

    def _count(name):
        p = run_dir / name
        try:
            return len(json.loads(p.read_text()))
        except (OSError, ValueError):
            return 0

    details_rejected: dict[str, int] = {}
    try:
        for r in json.loads((run_dir / "rejections.json").read_text()):
            rule = r.get("rule", "")
            details_rejected[rule] = details_rejected.get(rule, 0) + 1
    except (OSError, ValueError):
        pass

    summary = RunSummary(
        run_id=run_id,
        started_at=(row["started_at"] if row else "") or "",
        finished_at=datetime.now(timezone.utc).isoformat(),
        status=sync_result["status"],
        date_window=fetch.get("date_window", "") if isinstance(fetch, dict) else "",
        cards_seen=fetch.get("cards_seen", 0) if isinstance(fetch, dict) else 0,
        cards_rejected_by_rule=fetch.get("cards_rejected_by_rule", {}) if isinstance(fetch, dict) else {},
        details_fetched=fetch.get("details_fetched", 0) if isinstance(fetch, dict) else 0,
        details_rejected_by_rule=details_rejected,
        jobs_new=_count("jobs.json"),
        refreshes=_count("refreshes.json"),
        unresolved=_count("unresolved.json"),
        notion_created=sync_result["created"],
        notion_updated=sync_result["updated"],
        notion_failed=sync_result["failed"],
        errors=fetch.get("errors", []) if isinstance(fetch, dict) else [],
        log_path=str(_logs_dir() / "jobs.log"),
    )
    return summary.to_dict()


def cmd_sync(args) -> int:
    from .notion import AuthError, NotionClient, NotionError, SchemaError

    run_id = _resolve_run_id(args)
    if not run_id:
        print(json.dumps({"error": "no run id and no data/runs/latest"}))
        return 2
    run_dir = _runs_dir() / run_id
    run_date = _today().isoformat()
    held = getattr(args, "_locked", False)

    if not args.dry_run and not held and not acquire_lock():
        print(json.dumps({"error": "another run is active"}))
        return 2

    conn = cache.connect()
    try:
        client = NotionClient()
        client.check_schema()
        ops = sync.plan(
            run_id, conn, run_date, run_dir,
            client=None if args.dry_run else client,
            journal_ops=not args.dry_run,
        )
        result = sync.apply(ops, client, conn, run_id, run_date, args.dry_run)
        if not args.dry_run:
            merged = _build_run_summary(conn, run_id, run_dir, result, run_date)
            (run_dir / "summary.json").write_text(json.dumps(merged, indent=2))
            cache.finish_run(conn, run_id, result["status"], merged)
        out = {"run_id": run_id}
        out.update({k: result[k] for k in (
            "planned_create", "planned_update", "created", "updated", "failed", "status")})
        print(json.dumps(out))
        return {"success": 0, "partial": 1}.get(result["status"], 2)
    except (AuthError, SchemaError, NotionError) as e:
        log.error("sync failed: %s", e)
        print(json.dumps({"error": str(e), "status": "failed"}))
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("sync failed: %s\n%s", e, traceback.format_exc())
        print(json.dumps({"error": str(e), "status": "failed"}))
        return 2
    finally:
        conn.close()
        if not args.dry_run and not held:
            release_lock()


def cmd_bootstrap(args) -> int:
    from .notion import AuthError, NotionClient, NotionError, SchemaError, page_to_row

    conn = cache.connect()
    try:
        if cache.count_jobs(conn) > 0 and not args.force:
            print(json.dumps({"error": "jobs table is not empty; use --force to reload"}))
            return 2
        client = NotionClient()
        run_date = _today().isoformat()
        rows_read = rows_inserted = rows_skipped = 0
        for page in client.query_all():
            rows_read += 1
            row = page_to_row(page, run_date)
            if row is None:
                rows_skipped += 1
                continue
            cache.insert_job_row(conn, row)
            rows_inserted += 1
        print(json.dumps({
            "rows_read": rows_read,
            "rows_inserted": rows_inserted,
            "rows_skipped_no_key": rows_skipped,
        }))
        return 0
    except (AuthError, SchemaError, NotionError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    finally:
        conn.close()


def cmd_resync(args) -> int:
    from .notion import AuthError, NotionClient, NotionError, page_to_row

    conn = cache.connect()
    try:
        client = NotionClient()
        run_date = _today().isoformat()
        notion_rows: dict[str, dict] = {}
        for page in client.query_all():
            row = page_to_row(page, run_date)
            if row:
                notion_rows[row["posting_key"]] = row
        cache_rows = {r["posting_key"]: r for r in cache.all_jobs(conn)}

        added = [k for k in notion_rows if k not in cache_rows]
        removed = [k for k, r in cache_rows.items() if r.get("page_id") and k not in notion_rows]
        changed = []
        for k, nr in notion_rows.items():
            cr = cache_rows.get(k)
            if cr is None:
                continue
            if (bool(cr["applied"]) != bool(nr["applied"])
                    or bool(cr["neglected"]) != bool(nr["neglected"])
                    or cr["date_posted"] != nr["date_posted"]
                    or (cr["page_id"] or "") != (nr["page_id"] or "")):
                changed.append(k)

        for k in added:
            cache.insert_job_row(conn, notion_rows[k])
        for k in removed:
            if cache_rows[k].get("origin") == "bootstrap":
                cache.delete_job(conn, k)
        for k in changed:
            cache.insert_job_row(conn, notion_rows[k])

        print(json.dumps({"added": len(added), "removed": len(removed), "changed": len(changed)}))
        return 0
    except (AuthError, NotionError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    finally:
        conn.close()


def _detail_for_page(props: dict) -> Path | None:
    """First data/details/<job_id>.html that exists, resolving job IDs from
    External ID and falling back to the digits of Posting Key."""
    from .notion import _plain

    ids = [p.strip() for p in _plain(props.get("External ID"), "rich_text").split(",") if p.strip()]
    if not ids:
        digits = "".join(c for c in _plain(props.get("Posting Key"), "rich_text") if c.isdigit())
        ids = [digits] if digits else []
    for job_id in ids:
        path = _details_dir() / f"{job_id}.html"
        if path.exists():
            return path
    return None


def cmd_backfill(args) -> int:
    """One-off: relabel Source Type rows off the four canonical labels to
    'Direct company', and fill Job Area from a local detail page when the row's
    Job Area is empty. Touches no other property."""
    from .notion import (
        AuthError, NotionClient, NotionError, SchemaError,
        SOURCE_TYPE_OPTIONS, multi_select, select,
    )

    try:
        client = NotionClient()
        client.check_schema()
    except (AuthError, SchemaError, NotionError) as e:
        print(json.dumps({"error": str(e)}))
        return 2

    scanned = source_type_updated = job_area_updated = skipped_no_detail = failed = 0
    candidates = 0
    for page in client.query_all():
        scanned += 1
        props = page.get("properties", {})
        changed: dict = {}
        try:
            cur_st = (props.get("Source Type", {}).get("select") or {}).get("name")
            if cur_st is not None and cur_st not in SOURCE_TYPE_OPTIONS:
                changed["Source Type"] = select("Direct company")
                source_type_updated += 1

            cur_area = props.get("Job Area", {}).get("multi_select") or []
            if not cur_area:
                detail_path = _detail_for_page(props)
                if detail_path is None:
                    if not changed:
                        skipped_no_detail += 1
                else:
                    job_id = detail_path.stem
                    detail = parse_detail(detail_path.read_text(), job_id)
                    title = _page_title(props)
                    changed["Job Area"] = multi_select(areas(title, detail.description_text))
                    job_area_updated += 1

            if not changed:
                continue
            candidates += 1
            log.info("backfill %s: %s", page.get("id"), ", ".join(changed))
            if not args.dry_run:
                client.update_page(page["id"], changed)
        except (NotionError, OSError) as e:
            failed += 1
            log.warning("backfill failed for %s: %s", page.get("id"), e)
        if args.limit and candidates >= args.limit:
            break

    print(json.dumps({
        "scanned": scanned,
        "source_type_updated": source_type_updated,
        "job_area_updated": job_area_updated,
        "skipped_no_detail": skipped_no_detail,
        "failed": failed,
    }))
    return 1 if failed else 0


def _page_title(props: dict) -> str:
    from .notion import _plain

    return _plain(props.get("Job Title"), "title")


def cmd_auth(args) -> int:
    from .notion import AuthError, NotionClient, NotionError, set_token

    if args.action == "set-token":
        import getpass

        token = getpass.getpass("Notion integration token: ")
        if not token:
            print(json.dumps({"stored": False, "error": "empty token"}))
            return 2
        set_token(token)
        print(json.dumps({"stored": True}))
        return 0

    try:
        client = NotionClient()
        me = client.me()
        print(json.dumps({"ok": True, "bot_name": me.get("name")}))
        return 0
    except (AuthError, NotionError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


_LOCATION_CHARS_RE = re.compile(r"^[A-Za-z0-9 ,.;:/&()'\-]+$")
_MIN_YEARS_RE = re.compile(r"^(\d{1,2}\+|\d{1,2}-\d{1,2}|(BS|MS|PhD)\+\d{1,2}|Not explicit)$")


def _lower_from_signal(signal: str) -> int | None:
    """The lower bound a min-years signal implies: the first integer in the
    string, or None for 'Not explicit'."""
    if signal == "Not explicit":
        return None
    m = re.search(r"\d+", signal)
    return int(m.group()) if m else None


def _valid_extraction_item(item) -> bool:
    """A haiku extractions item is valid when it is an object keyed only by
    posting_key / location / min_years_signal, carries a string posting_key and
    at least one of the two value fields, and each present value is null or a
    well-formed string (location charset and part lengths; min-years pattern)."""
    if not isinstance(item, dict):
        return False
    if set(item) - {"posting_key", "location", "min_years_signal"}:
        return False
    if not isinstance(item.get("posting_key"), str) or not item["posting_key"]:
        return False
    if "location" not in item and "min_years_signal" not in item:
        return False
    for key in ("location", "min_years_signal"):
        if key in item and not (item[key] is None or isinstance(item[key], str)):
            return False
    loc = item.get("location")
    if isinstance(loc, str):
        if len(loc) > 300 or not _LOCATION_CHARS_RE.match(loc):
            return False
        parts = [p.strip() for p in loc.split(";") if p.strip()]
        if not parts or any(not (3 <= len(p) <= 60) for p in parts):
            return False
    mys = item.get("min_years_signal")
    if isinstance(mys, str) and not _MIN_YEARS_RE.match(mys):
        return False
    return True


def cmd_apply_extractions(args) -> int:
    run_id = _resolve_run_id(args)
    if not run_id:
        print(json.dumps({"error": "no run id and no data/runs/latest"}))
        return 2
    try:
        items = json.loads(Path(args.file).read_text())
    except (OSError, ValueError, TypeError) as e:
        print(json.dumps({"error": f"cannot read --file: {e}"}))
        return 2
    if not isinstance(items, list):
        print(json.dumps({"error": "--file must be a JSON list"}))
        return 2

    run_dir = _runs_dir() / run_id
    jobs_path = run_dir / "jobs.json"
    try:
        jobs = json.loads(jobs_path.read_text())
    except (OSError, ValueError):
        jobs = []
    by_key = {j["posting_key"]: j for j in jobs}

    applied = skipped_invalid = skipped_unknown = 0
    for item in items:
        if not _valid_extraction_item(item):
            log.warning("apply-extractions: invalid item %r", item)
            skipped_invalid += 1
            continue
        job = by_key.get(item["posting_key"])
        if job is None:
            skipped_unknown += 1
            continue
        unresolved = list(job.get("unresolved") or [])
        did = False
        loc = item.get("location")
        if loc is not None and "location" in unresolved:
            job["locations"] = [p.strip() for p in loc.split(";") if p.strip()]
            unresolved.remove("location")
            did = True
        mys = item.get("min_years_signal")
        if mys is not None and "min_years" in unresolved:
            job["min_years_signal"] = mys
            job["min_years_lower"] = _lower_from_signal(mys)
            unresolved.remove("min_years")
            did = True
        job["unresolved"] = unresolved
        if did:
            applied += 1
        else:
            skipped_unknown += 1

    jobs_path.write_text(json.dumps(jobs, indent=2))
    print(json.dumps({
        "run_id": run_id,
        "applied": applied,
        "skipped_invalid": skipped_invalid,
        "skipped_unknown": skipped_unknown,
    }))
    return 0


def _call_capturing(fn, ns) -> tuple[int, dict]:
    """Run a subcommand in-process, capturing its stdout JSON line so `daily`
    can read the numbers without the intermediate lines reaching the terminal."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(ns)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    result = {}
    if lines:
        try:
            result = json.loads(lines[-1])
        except ValueError:
            result = {}
    return rc, result


def cmd_daily(args) -> int:
    if not acquire_lock():
        print(json.dumps({"error": "another run is active"}))
        return 2
    try:
        run_id = getattr(args, "run_id", None) or datetime.now().strftime("%Y-%m-%dT%H%M%S")
        window = getattr(args, "date_window", None)
        dry_run = getattr(args, "dry_run", False)

        fetch_ns = argparse.Namespace(
            run_id=run_id, date_window=window, max_details=None, _locked=True
        )
        rc, result = _call_capturing(cmd_fetch, fetch_ns)
        if rc != 0:
            print(json.dumps(result or {"run_id": run_id, "status": "failed"}))
            return rc

        rc, extract_result = _call_capturing(cmd_extract, argparse.Namespace(run_id=run_id))
        if rc != 0:
            print(json.dumps(extract_result or {"run_id": run_id, "status": "failed"}))
            return rc
        unresolved = extract_result.get("unresolved", 0)

        sync_ns = argparse.Namespace(run_id=run_id, dry_run=dry_run, _locked=True)
        rc, sync_result = _call_capturing(cmd_sync, sync_ns)
        out = {"run_id": run_id}
        for k in ("planned_create", "planned_update", "created", "updated", "failed", "status"):
            if k in sync_result:
                out[k] = sync_result[k]
        out["unresolved"] = unresolved
        print(json.dumps(out))
        return rc
    finally:
        release_lock()


def _load_or_build_summary(run_id: str, run_dir: Path) -> dict | None:
    """The run's summary.json if sync wrote one; otherwise a summary built from
    the fetch/extract artifacts with status 'failed'; None if the run never got
    past reading nothing."""
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except ValueError:
            pass
    if (run_dir / "cards.json").exists():
        conn = cache.connect()
        try:
            return _build_run_summary(
                conn, run_id, run_dir,
                {"status": "failed", "created": 0, "updated": 0, "failed": 0},
                _today().isoformat(),
            )
        finally:
            conn.close()
    return None


def cmd_summary(args) -> int:
    run_id = _resolve_run_id(args)
    if not run_id:
        print(json.dumps({"error": "no run id and no data/runs/latest"}))
        return 2
    summary = _load_or_build_summary(run_id, _runs_dir() / run_id)
    if summary is None:
        print(json.dumps({"error": f"no summary for run {run_id}"}))
        return 2
    print(notify.format_summary(summary))
    return 0


def cmd_notify(args) -> int:
    run_id = _resolve_run_id(args)
    run_dir = _runs_dir() / run_id if run_id else None
    summary = _load_or_build_summary(run_id, run_dir) if run_dir else None
    if summary is None:
        summary = {"status": "failed", "log_path": str(_logs_dir() / "jobs.log")}
    status = summary.get("status", "failed")
    body = (
        f"{summary.get('notion_created', 0)} new, "
        f"{summary.get('notion_updated', 0)} updated, "
        f"{summary.get('notion_failed', 0)} failed. "
        f"Log: {summary.get('log_path', '')}"
    )
    sent = notify.send_notification(f"Daily jobs: {status}", body)
    print(json.dumps({"sent": sent}))
    return 0


def main(argv=None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="jobs")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--date-window", choices=["past_24h", "past_week"])
    p_fetch.add_argument("--run-id")
    p_fetch.add_argument("--max-details", type=int)
    p_fetch.set_defaults(func=cmd_fetch)

    p_extract = sub.add_parser("extract")
    p_extract.add_argument("--run-id")
    p_extract.set_defaults(func=cmd_extract)

    p_apply = sub.add_parser("apply-extractions")
    p_apply.add_argument("--file", required=True)
    p_apply.add_argument("--run-id")
    p_apply.set_defaults(func=cmd_apply_extractions)

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--run-id")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_daily = sub.add_parser("daily")
    p_daily.add_argument("--dry-run", action="store_true")
    p_daily.add_argument("--date-window", choices=["past_24h", "past_week"])
    p_daily.add_argument("--run-id")
    p_daily.set_defaults(func=cmd_daily)

    p_bootstrap = sub.add_parser("bootstrap")
    p_bootstrap.add_argument("--force", action="store_true")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_backfill = sub.add_parser("backfill")
    p_backfill.add_argument("--dry-run", action="store_true")
    p_backfill.add_argument("--limit", type=int)
    p_backfill.set_defaults(func=cmd_backfill)
    sub.add_parser("resync").set_defaults(func=cmd_resync)

    p_auth = sub.add_parser("auth")
    p_auth.add_argument("action", choices=["set-token", "check"])
    p_auth.set_defaults(func=cmd_auth)

    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--run-id")
    p_summary.set_defaults(func=cmd_summary)

    p_notify = sub.add_parser("notify")
    p_notify.add_argument("--run-id")
    p_notify.set_defaults(func=cmd_notify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
