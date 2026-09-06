# Runbook

Operations for the daily LinkedIn to Notion job sync. All commands run from the
repository root with `uv`.

## Scheduled task (Claude Desktop)

1. Open Claude Desktop.
2. Follow https://code.claude.com/docs/en/desktop-scheduled-tasks to create a
   task:
   - name: `daily-jobs`
   - schedule: daily at 08:00 local
   - working directory: this repository
   - prompt: `/daily-jobs`
3. The run's summary appears in Claude Desktop under the "Scheduled" session
   list, as the driver's final message.
4. On a partial or failed run, a macOS notification appears titled
   `Daily jobs: partial` or `Daily jobs: failed` with the created/updated/failed
   counts and the log path.

## Fallback without the Desktop app (headless)

Run the driver from a terminal:

```
claude -p "/daily-jobs" --allowedTools "Bash,Agent" --max-turns 25
```

To schedule it with launchd, save this as
`~/Library/LaunchAgents/com.chenjiang.daily-jobs.plist` (replace `/Users/YOU`
with your home path) and load it with
`launchctl load ~/Library/LaunchAgents/com.chenjiang.daily-jobs.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.chenjiang.daily-jobs</string>
  <key>WorkingDirectory</key><string>/Users/YOU/Github/job-listings-scraper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>claude -p "/daily-jobs" --allowedTools "Bash,Agent" --max-turns 25</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key><string>/Users/YOU/Github/job-listings-scraper/data/logs/launchd.err</string>
  <key>StandardOutPath</key><string>/Users/YOU/Github/job-listings-scraper/data/logs/launchd.out</string>
</dict>
</plist>
```

## Manual run (no Claude)

Run the whole pipeline in one process. The haiku step is skipped; unresolved
jobs keep their defaults.

```
uv run jobs daily
```

Or run the four steps by hand, with the haiku step done inside a normal Claude
Code session:

```
uv run jobs fetch
uv run jobs extract
# in a Claude Code session in this repo:
/daily-jobs
```

`/daily-jobs` spawns one haiku subagent when `extract` reported
`unresolved > 0`, applies its output, then syncs.

## Recovery

1. Partial run (exit 1): rerun `uv run jobs sync`. Confirmed writes are
   skipped; only unfinished ops replay.
2. Blocked by LinkedIn: nothing to do. The next run uses the past-week window
   automatically when the last success is more than 36 hours old.
3. Schema error on sync: run `uv run jobs auth check`, then compare the live
   properties against the table in `docs/plans/.../overview.md`. Fix the Notion
   property, do not change the code.
4. Token invalid: `uv run jobs auth set-token`, paste a fresh token.
5. Keychain unreadable from the scheduled task: put the token in `.env` as
   `NOTION_TOKEN=...`, then `chmod 600 .env`. The client reads `.env` only when
   the Keychain returns nothing.

## Inspection

```
uvx datasette data/jobs.sqlite3 -m datasette/metadata.json
```

Open the `daily_jobs` and `rejections_by_day` canned queries. For a quick count
of today's rejections by rule:

```
sqlite3 data/jobs.sqlite3 \
  "select rule, count(*) from rejections where date(created_at)=date('now') group by rule order by 2 desc"
```

## Cache maintenance

1. Notion was edited outside the pipeline (rows deleted, keys changed):
   `uv run jobs resync` reconciles the cache against Notion.
2. Rebuild the cache from scratch: `uv run jobs bootstrap --force`.

## Stopping

Delete the scheduled task in Claude Desktop (or
`launchctl unload ~/Library/LaunchAgents/com.chenjiang.daily-jobs.plist` for the
launchd fallback).
