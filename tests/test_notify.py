from unittest.mock import patch

from jobs import notify
from jobs.models import RunSummary


def _summary():
    return RunSummary(
        run_id="2026-09-06T080000",
        started_at="2026-09-06T08:00:00",
        finished_at="2026-09-06T08:05:00",
        status="success",
        date_window="past_24h",
        cards_seen=255,
        cards_rejected_by_rule={"title_seniority": 40, "date_window": 10},
        details_fetched=130,
        details_rejected_by_rule={"ai_relevance": 29, "employment_type": 12},
        jobs_new=80,
        refreshes=15,
        unresolved=3,
        notion_created=80,
        notion_updated=15,
        notion_failed=0,
        errors=["something broke"],
        log_path="data/logs/jobs.log",
    )


def test_format_summary_contains_run_id_status_and_counts():
    text = notify.format_summary(_summary())
    assert "2026-09-06T080000" in text
    assert "success" in text
    for count in ("255", "130", "80", "15", "3"):
        assert count in text
    assert "205" in text  # cards kept = 255 - 50 rejected
    assert "title_seniority" in text and "ai_relevance" in text
    assert "something broke" in text
    assert "data/logs/jobs.log" in text


def test_format_summary_accepts_a_plain_dict():
    text = notify.format_summary({"run_id": "r1", "status": "failed"})
    assert "r1" in text and "failed" in text


def test_send_notification_builds_osascript_argv_and_escapes_quotes():
    with patch("jobs.notify.subprocess.run") as run:
        ok = notify.send_notification("Daily jobs: success", 'a "b" c \\ d')
    assert ok is True
    args, kwargs = run.call_args
    argv = args[0]
    assert argv[0] == "osascript" and argv[1] == "-e"
    script = argv[2]
    assert '\\"b\\"' in script  # inner double-quotes escaped for AppleScript
    assert 'with title "Daily jobs: success"' in script
    assert kwargs.get("timeout") == 10 and kwargs.get("check") is False


def test_send_notification_swallows_failure():
    with patch("jobs.notify.subprocess.run", side_effect=FileNotFoundError("no osascript")):
        assert notify.send_notification("t", "b") is False
