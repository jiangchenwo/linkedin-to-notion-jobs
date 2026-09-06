"""Run summary text and the macOS notification. No network, no Notion."""

import logging
import subprocess

log = logging.getLogger("jobs")


def format_summary(summary) -> str:
    """Render a run summary (a RunSummary or its dict) as plain text, one fact
    per line. `cards kept` and rejection counts are derived; a RunSummary carries
    no details_skipped, so that line reads 0 unless the dict supplies one."""
    s = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)
    card_rej = s.get("cards_rejected_by_rule") or {}
    detail_rej = s.get("details_rejected_by_rule") or {}
    seen = s.get("cards_seen", 0)
    kept = seen - sum(card_rej.values())

    lines = [
        f"status: {s.get('status', '')}  run_id: {s.get('run_id', '')}",
        f"date window: {s.get('date_window', '')}",
        f"cards seen: {seen}, kept: {kept}",
    ]
    for rule, n in sorted(card_rej.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  card rejected {rule}: {n}")
    for rule, n in sorted(detail_rej.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  detail rejected {rule}: {n}")
    lines += [
        f"details fetched: {s.get('details_fetched', 0)}, skipped: {s.get('details_skipped', 0)}",
        f"jobs new: {s.get('jobs_new', 0)}, refreshed: {s.get('refreshes', 0)}",
        f"unresolved: {s.get('unresolved', 0)}",
        f"Notion created: {s.get('notion_created', 0)}, "
        f"updated: {s.get('notion_updated', 0)}, failed: {s.get('notion_failed', 0)}",
    ]
    for err in s.get("errors") or []:
        lines.append(f"error: {str(err)[:200]}")
    lines.append(f"log: {s.get('log_path', '')}")
    return "\n".join(lines)


def _esc(s: str) -> str:
    """Escape a Python string for an AppleScript double-quoted literal. Backslash
    first, or the escapes double up."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, body: str) -> bool:
    """Post a macOS notification via osascript. Never raises: logs and returns
    False if osascript is missing, times out, or errors."""
    script = f'display notification "{_esc(body)}" with title "{_esc(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
        return True
    except Exception as e:  # noqa: BLE001 - a failed notification must not fail the run
        log.warning("notification failed: %s", e)
        return False
