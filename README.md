# job-listings-scraper

A daily scraper that pulls early-career AI-engineer jobs from LinkedIn and syncs
them to a Notion database. Every deterministic step is a Python CLI (`uv run
jobs ...`); a local SQLite cache is the dedupe source of truth, so a normal day
reads nothing from Notion.

The pipeline it replaces relied on a logged-in browser profile and Notion's
hosted MCP, and failed roughly two runs in three. This version uses LinkedIn's
public guest endpoints (no login, no browser) and the Notion REST API.

## How it works

```
jobs fetch     guest search per keyword -> card filters -> group siblings by
               company+title -> known jobs become refreshes, new ones get their
               detail HTML fetched into data/details/
jobs extract   parse detail HTML -> detail filters -> extract every Notion
               field -> jobs.json, rejections.json, unresolved.json
jobs sync      create new pages, update reposted/relocated ones via Notion REST;
               writes are journaled in the cache and resume after a partial run
```

A job posted in several cities appears once, with every city in `Location` and
every LinkedIn ID in `External ID`. `Date Posted` tracks the latest repost.

It reads LinkedIn's public guest pages politely: 3 to 6 seconds between requests
and back-off on 429/403/999. Notion access uses an internal integration token,
kept in the macOS Keychain, over the REST API.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

1. Create a Notion internal integration (Read/Update/Insert content) and connect
   it to the target database.
2. Store the token in the macOS Keychain:
   ```
   uv run jobs auth set-token
   uv run jobs auth check          # {"ok": true, "bot_name": "..."}
   ```
3. `cp .env.example .env` and set `NOTION_DATA_SOURCE_ID`.
4. Load the existing database into the cache once:
   ```
   uv run jobs bootstrap
   ```

## Commands

| Command | Does |
|---|---|
| `jobs fetch [--date-window past_24h\|past_week]` | Search, filter, group, fetch new details |
| `jobs extract` | Parse details, filter, extract fields |
| `jobs sync [--dry-run]` | Create and update Notion pages; `--dry-run` prints the plan |
| `jobs apply-extractions --file PATH` | Merge the haiku extraction output into `jobs.json` |
| `jobs daily [--dry-run]` | Run fetch, extract, and sync in one process (no haiku step) |
| `jobs summary` | Print the last run's summary as plain text |
| `jobs notify` | Send the macOS notification for the last run |
| `jobs bootstrap [--force]` | Load the whole Notion database into the cache |
| `jobs resync` | Reconcile the cache against Notion (added/removed/changed) |
| `jobs auth set-token \| check` | Manage the Keychain token |

For the scheduled run and the haiku fallback, Claude Code drives the CLI through
the `daily-jobs` skill (`.claude/skills/daily-jobs/SKILL.md`). See
[docs/runbook.md](docs/runbook.md) for the scheduled task, manual runs, and
recovery.

Each command prints progress to stderr and one JSON object as the last line of
stdout. `fetch`, `sync`, and `daily` hold a single-flight lock. Exit codes:
0 success, 1 partial (kept what it had, resumable), 2 failed.

## Inspecting the cache

```
uvx datasette data/jobs.sqlite3 -m datasette/metadata.json
```

Ships the `daily_jobs` and `rejections_by_day` canned queries.

## Tests

```
uv run pytest
```

## Layout

```
src/jobs/       linkedin, filters, extract, cache, notion, sync, cli
data/           keywords.toml, companies.toml (committed); cache and run
                artifacts (gitignored)
tests/          scrubbed HTML and JSON fixtures
```

Keyword and company lists live in `data/*.toml`. The cache, `.env`, raw detail
HTML, and per-run files are gitignored.

## License

MIT. See [LICENSE](LICENSE).
