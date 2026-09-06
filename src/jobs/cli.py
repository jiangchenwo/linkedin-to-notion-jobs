"""Command-line entry point. Every subcommand prints human-readable progress to
stderr and exactly one JSON object as the last line of stdout. Phase 1
implements `fetch`; the rest are stubs until later phases."""

import argparse
import json
import logging
import os
import sys
import tomllib
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import cache
from .extract import build_job, canon, min_years, sibling_key
from .filters import card_stage, detail_stage
from .linkedin import Blocked, BudgetExhausted, GuestClient, load_keywords, parse_detail
from .models import Card

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


def cmd_fetch(args) -> int:
    conn = cache.connect()
    run_date = _today()
    date_window = _decide_window(conn, args.date_window)

    if not acquire_lock():
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
        details_fetched = 0
        details_skipped = 0
        groups_out = []
        for g in groups:
            lowest = g["lowest"]
            lowest_card = g["cards"][0]
            path = _details_dir() / f"{lowest}.html"
            prior = cache.latest_sighting(conn, lowest)
            already = (
                prior is not None
                and prior["date_posted"] == lowest_card.date_posted
                and path.exists()
            )
            detail_path = None
            if already:
                details_skipped += 1
                detail_path = str(path)
            elif not blocked and (
                args.max_details is None or details_fetched < args.max_details
            ):
                try:
                    _, raw = client.detail(lowest)
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
                    "posting_key": f"linkedin:{lowest}",
                    "cards": [c.to_dict() for c in g["cards"]],
                    "detail_job_id": lowest,
                    "detail_path": detail_path,
                }
            )

        for card in kept:
            cache.record_sighting(conn, card, run_id)

        (run_dir / "cards.json").write_text(json.dumps(groups_out, indent=2))
        (run_dir / "refreshes.json").write_text("[]")

        status = "partial" if blocked else "success"
        summary = {
            "run_id": run_id,
            "date_window": date_window,
            "cards_seen": cards_seen,
            "cards_kept": len(kept),
            "refreshes": 0,
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


def _not_implemented(args) -> int:
    print(json.dumps({"error": "not implemented"}))
    return 2


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
    p_apply.add_argument("--file")
    p_apply.add_argument("--run-id")
    p_apply.set_defaults(func=_not_implemented)

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--run-id")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.set_defaults(func=_not_implemented)

    p_daily = sub.add_parser("daily")
    p_daily.add_argument("--dry-run", action="store_true")
    p_daily.add_argument("--date-window", choices=["past_24h", "past_week"])
    p_daily.set_defaults(func=_not_implemented)

    sub.add_parser("bootstrap").set_defaults(func=_not_implemented)
    sub.add_parser("resync").set_defaults(func=_not_implemented)

    p_auth = sub.add_parser("auth")
    p_auth.add_argument("action", choices=["set-token", "check"])
    p_auth.set_defaults(func=_not_implemented)

    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--run-id")
    p_summary.set_defaults(func=_not_implemented)

    p_notify = sub.add_parser("notify")
    p_notify.add_argument("--run-id")
    p_notify.set_defaults(func=_not_implemented)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
