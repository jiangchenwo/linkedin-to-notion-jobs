# linkedin-to-notion-jobs

A daily pipeline that pulls early-career jobs from LinkedIn's public guest
endpoints (no login, no browser) and syncs them to a Notion database via REST.
A local SQLite cache is the dedupe source of truth, so a normal day reads
nothing from Notion. Every deterministic step is a Python CLI (`uv run jobs
...`); a Claude Code scheduled task drives the optional haiku fallback that
fills unresolved fields.

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

---

## 1. What it does

LinkedIn's public guest search returns job cards without a login. The pipeline
fetches cards for each keyword, filters out senior titles at that stage, groups
city siblings into one record, fetches the detail page for each new job, parses
the HTML into structured fields, and writes to Notion via the REST API. The
SQLite cache tracks every job ID and every Notion write, so reruns are
idempotent and a partial sync resumes where it stopped.

```
LinkedIn guest search  →  SQLite cache  →  Notion REST API
(public, no login)       (dedupe/journal)   (internal integration)
```

---

## 2. Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

1. `uv sync`
2. Duplicate the Notion database template into your own workspace:
   **[Duplicate this Notion database template](https://app.notion.com/p/3d3bf3e89e9e80fb8bf2f7da2afda52e?v=04cbf3e89e9e83edb2a388bc7975a5c3&source=copy_link)**
3. Create a Notion internal integration with Read, Update, and Insert content
   permissions and connect it to your duplicated database.
4. Store the token in the macOS Keychain:
   ```
   uv run jobs auth set-token
   uv run jobs auth check          # {"ok": true, "bot_name": "..."}
   ```
5. `cp .env.example .env` and set `NOTION_DATA_SOURCE_ID` to your database ID.
6. Run the first pass:
   ```
   uv run jobs fetch
   uv run jobs extract
   uv run jobs sync
   ```

`jobs bootstrap` is only needed when adopting a database that already has rows.
A fresh empty template does not need it.

---

## 3. Configure the search

Everything target-specific lives in `data/keywords.toml`. Retargeting the
pipeline at a different title or field is a change to this file, not the Python.
The file answers three questions.

### What to ask LinkedIn: `keywords` and `[search]`

`keywords` is the list of query strings, in priority order. LinkedIn is queried
with `sortBy=DD` (newest first) and a shared seen-set stops a keyword once a
page adds no new ID, so early-career terms lead and broad catch-alls trail.

`[search]` is forwarded to LinkedIn's guest search. Every `f_*` key becomes a
query parameter (`max_pages` is the exception, a pipeline knob):

| Key | Values |
|---|---|
| `location` | free text, as typed into LinkedIn (`"United States"`, `"Remote"`, `"Berlin, Germany"`) |
| `f_JT` | job type: `F` full-time, `P` part-time, `C` contract, `T` temporary, `I` internship, `O` other; comma-join for several. Must agree with `filters.employment_types`. |
| `f_E` | experience: `1` internship, `2` entry, `3` associate, `4` mid-senior, `5` director, `6` executive; comma-join. LinkedIn applies it loosely; the title and years filters do the real work. |
| `f_WT` | work mode: `1` on-site, `2` remote, `3` hybrid; comma-join. Optional; omit for all. |
| `sortBy` | `DD` newest first, `R` relevance. Keep `DD`; the seen-set assumes it. |
| `max_pages` | integer; pages of 10 per keyword. |
| `f_TPR_past_24h`, `f_TPR_past_week` | `r<seconds>` window tokens; the pipeline picks one per run. |

### What to throw away: `[filters]`

Card stage, before any detail is fetched:

- `exclude_title_terms`: a card whose title carries one of these is dropped.
- `max_age_days`: reject cards older than this (default 2).
- `long_window_title_terms`: titles matching one get `long_window_max_age_days`
  (default 14) instead. `[]` turns the wider window off.

Detail stage, after the posting body is fetched:

- `employment_types`: LinkedIn's label must be in this list; `[]` accepts every
  type. Keep it in step with `f_JT`.
- `exclude_description_terms`: reject a body carrying one of these.
- `max_min_years`: reject when the parsed minimum years exceed this; omit to
  disable.

### How to label what is left: `[relevance]`, `[skills]`, `[areas]`, `[priority]`

- `[relevance]`: the gate on the parsed detail. A posting passes with two
  `core_skills` hits, or one plus a `terms` hit in both the title and body.
  `terms` match case-insensitively with word boundaries; `core_skills` are keys
  of `[skills]`.
- `[skills]`: the skill vocabulary, one `name = 'regex'` per line. It fills the
  Notion `Parsed Skills` field (first 12 hits, so order matters), backs
  `core_skills`, and marks requirement lines.
- `[areas]`: the Notion `Job Area` multi-select. `default` is the fallback.
  Each `[[areas.map]]` has a `label` (the Notion option) and a raw `pattern`;
  areas are tried in order, at most 3 tagged, a pattern matching the title or
  hitting the description twice.
- `[priority]`: one `Priority Score` per Seniority label, 1 best. All seven
  labels are required.

### Common changes

- **A different title in the same field**: edit `keywords`.
- **Senior roles**: move junior terms into `exclude_title_terms`, set
  `long_window_title_terms = []`, omit `max_min_years`, set `f_E = "4,5"`, and
  invert `[priority]` (Senior 1). See `data/examples/senior-backend.toml`.
- **Internships or contract work**: set `f_JT` and `employment_types` together
  (`"I"` / `["Internship"]`, or `"C"` / `["Contract"]`). See
  `data/examples/remote-internship.toml`.
- **Remote only**: add `f_WT = "2"`.
- **Another country**: change `location`. The haiku fallback writes `Remote
  (US)` for US-remote roles; adjust that expectation for another region.
- **A field outside AI** (data, backend, ...): rewrite `[relevance]`,
  `[skills]`, and `[areas]` together. See `data/examples/data-engineer.toml`.

`Job Area` labels become Notion multi-select options the first time a job is
written with one, so a new label needs no schema edit. After switching fields,
delete the old AI options in the Notion UI so they stop showing on the board.

### Check your edit

`uv run jobs profile` compiles the file and prints the summary; it exits 2
naming the section and key on a bad edit. Then `uv run jobs fetch` (set
`max_pages = 1` for a quick first look) and `uv run jobs summary` show the
rejection counts by rule.

### `companies.toml`

Holds three employer lists: `aggregators`, `startups`, and `reliable`.
Aggregators are excluded outright (staffing agencies, job boards). Startups and
reliable employers only influence the `Source Type` Notion label: a company on
neither list gets `"Direct company"` or `"Startup signal"` from signal
heuristics; listed companies get `"Startup (listed)"` or `"Reliable (listed)"`.

---

## 4. Notion setup

**[Duplicate this Notion database template](https://app.notion.com/p/3d3bf3e89e9e80fb8bf2f7da2afda52e?v=04cbf3e89e9e83edb2a388bc7975a5c3&source=copy_link)**

The link above opens the published database with "Duplicate as template"
enabled, so it copies into your own workspace as a fresh, empty database.

### Property schema

The pipeline asserts the live schema before every sync. The 26 expected
properties are:

| Property | Type | Value / notes |
|---|---|---|
| Job Title | title | lowest LinkedIn job ID's title |
| Company | rich_text | company name |
| Location | rich_text | `"; ".join(cities)`; updated when a city is appended |
| Work Mode | select | Remote, Hybrid, On-site, Unknown |
| Seniority | select | New Grad, Internship, Junior, Entry Level, Associate, Senior, Unknown |
| Priority Score | number | 1 to 6; see table below |
| H1B Sponsorship | select | Yes, No, Unknown |
| Source Type | select | Startup (listed), Reliable (listed), Startup signal, Direct company |
| Job Area | multi\_select | up to 3 labels from `[areas]`; `default` fallback |
| Minimum Years Signal | rich\_text | extracted from posting |
| Minimum / Basic Qualifications | rich\_text | max 1800 chars |
| Preferred Qualifications | rich\_text | max 1800 chars |
| Parsed Skills | rich\_text | named skills from `[skills]` |
| Requirement Signal | rich\_text | summary signal string |
| Status | formula | Notion-owned; the pipeline never writes this |
| Applied | checkbox | set `false` on create; never written again |
| Neglected | checkbox | set `false` on create; never written again |
| Unavailable | date | never written by the pipeline |
| Job URL | url | canonical LinkedIn URL |
| External ID | rich\_text | comma-joined LinkedIn IDs; updated when an ID is appended |
| Posting Key | rich\_text | dedup key (company + normalized title) |
| Source | select | always `"LinkedIn"` |
| Date Posted | date | updated when a newer repost date is found |
| First Seen | date | run date at create; never updated |
| Last Seen | date | run date; updated each run the job appears |

**Seniority mapping** (first match wins):

| Condition | Seniority |
|---|---|
| title matches `\b(new grad\|new graduate\|university grad\|graduate engineer\|early career)\b` | New Grad |
| title matches `\bintern(ship)?\b` | Internship |
| title matches `\b(junior\|jr\.?\|entry[- ]level)\b` | Junior |
| LinkedIn seniority == "Internship" | Internship |
| LinkedIn seniority == "Entry level" | Entry Level |
| LinkedIn seniority == "Associate" | Associate |
| LinkedIn seniority in ("Director", "Executive") | Senior |
| `min_years_lower` ≤ 2 | Junior |
| `min_years_lower` ≤ 4 | Entry Level |
| `min_years_lower` ≤ 6 | Associate |
| LinkedIn seniority == "Mid-Senior level" and `min_years_lower` is None | Associate |
| LinkedIn seniority == "Mid-Senior level" (so `min_years_lower` > 6) | Senior |
| otherwise | Unknown |

**Priority Score**: one per Seniority label, set in `[priority]` (1 best). The
shipped default:

| Seniority | Score |
|---|---|
| New Grad | 1 |
| Internship, Junior, Entry Level | 2 |
| Associate, Unknown | 3 |
| Senior | 6 |

### Schema validation

`check_schema` fetches the live schema and checks every property in the table
above. It raises `SchemaError` naming missing properties, type mismatches, and
any `Source Type` select options that are absent, then the run exits 2.

The pipeline never creates or changes a Notion property. To retarget the
scraper, edit `data/keywords.toml`. Do not edit `EXPECTED_SCHEMA` or the Notion
schema directly.

---

## 5. Running daily

### Option A: Claude Code scheduled task (Claude Desktop)

1. Open Claude Desktop.
2. Follow https://code.claude.com/docs/en/desktop-scheduled-tasks to create a
   routine:
   - Name: `daily-jobs`
   - Schedule: daily at 08:00 local
   - Working folder: this repository (accept the trust prompt)
   - Allowed tools: Bash, Read, Agent
   - Prompt:
     ```
     Read .claude/skills/daily-jobs/SKILL.md and follow its steps exactly.
     Work from the repository root. Do nothing else.
     ```
3. Point the prompt at the skill file rather than typing `/daily-jobs`. A
   routine does not resolve the project slash-command and does not read
   `~/.claude/skills/`, so invoking `/daily-jobs` there fails with "Unknown
   command". Reading the file directly needs no skill registration.
4. The run summary appears in Claude Desktop under the routine's session.
5. A macOS notification appears on partial or failed runs, with created/updated/
   failed counts and the log path.

### Option B: launchd (headless Claude Code)

Save this as `~/Library/LaunchAgents/com.example.daily-jobs.plist`, replacing
`/Users/YOU` with your home path, then load it:

```
launchctl load ~/Library/LaunchAgents/com.example.daily-jobs.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.daily-jobs</string>
  <key>WorkingDirectory</key><string>/Users/YOU/linkedin-to-notion-jobs</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>claude -p "/daily-jobs" --allowedTools "Bash,Agent" --max-turns 25</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key><string>/Users/YOU/linkedin-to-notion-jobs/data/logs/launchd.err</string>
  <key>StandardOutPath</key><string>/Users/YOU/linkedin-to-notion-jobs/data/logs/launchd.out</string>
</dict>
</plist>
```

To stop: `launchctl unload ~/Library/LaunchAgents/com.example.daily-jobs.plist`.

### Option C: plain cron (no Claude Code)

```
uv run jobs daily
```

Or step by step:

```
uv run jobs fetch
uv run jobs extract
uv run jobs sync
```

Without the skill driving the run, the haiku fallback never runs. Unresolved
`Location` and `Minimum Years` fields stay blank.

---

## 6. Troubleshooting

**429 / 999 / 403 from LinkedIn.** Nothing to do. The next run automatically
uses the past-week window when the last success is more than 36 hours old.

**Schema mismatch on sync.** `check_schema` names every drifted property. Fix
the Notion property in the UI; do not change the code.

**Partial sync (exit 1).** Rerun `uv run jobs sync`. Confirmed writes are
skipped; only unfinished operations replay.

**Token invalid.** Run `uv run jobs auth set-token` and paste a fresh token.

**Keychain unreadable from the scheduled task.** Put the token in `.env` as
`NOTION_TOKEN=secret_...`, then `chmod 600 .env`. The client reads `.env` only
when the Keychain returns nothing. The keyring service name is
`job-listings-scraper`.

**"Unknown command /daily-jobs" in a routine.** A Claude Desktop routine does
not resolve project slash-commands. Use the prompt in section 5A above, which
reads the skill file directly.

**Notion was edited outside the pipeline** (rows deleted, keys changed): run
`uv run jobs resync` to reconcile the cache against Notion. To rebuild the cache
from scratch: `uv run jobs bootstrap --force`.

---

## 7. Inspecting the cache

```
uvx datasette data/jobs.sqlite3 -m datasette/metadata.json
```

Opens the `daily_jobs` and `rejections_by_day` canned queries.

---

## 8. LinkedIn terms

This pipeline is for personal use against LinkedIn's public guest endpoints. The
3 to 6 second delay between requests and the 429/403/999 back-off are deliberate
and hardcoded. Keep them.

---

## 9. How it works

### Fetch

`jobs fetch` runs each keyword through LinkedIn's guest search, newest first.
A shared seen-set stops a keyword early once a page adds no new ID. Cards are
filtered by title (`exclude_title_terms`), freshness (`max_age_days`, longer for
`long_window_title_terms`), and a dedup check against the cache. Remaining new cards get
their detail HTML fetched into `data/details/`. Known jobs receive a refresh
(updated location or repost date) without re-fetching the detail.

### Extract

`jobs extract` parses the detail HTML for every job in the current run. It
applies detail-level filters (relevance gate, minimum-years cap) and extracts
every Notion field into `jobs.json`. Jobs that fail a filter go into
`rejections.json`. Jobs with unresolved fields go into `unresolved.json`. When
the `daily-jobs` skill drives the run, it spawns one haiku subagent to resolve
those fields and calls `jobs apply-extractions` to merge the output before sync.

### Sync

`jobs sync` reads `jobs.json`, calls `check_schema`, then plans creates and
updates. Every write is journaled in the cache before it is sent; a partial run
resumes from the journal on the next call. Sibling merging (same company and
normalized title) happens in the plan step: the group gets one Notion page with
all city locations and all LinkedIn IDs.

### Commands

| Command | Does |
|---|---|
| `jobs fetch [--date-window past_24h\|past_week]` | Search, filter, group, fetch new details |
| `jobs extract` | Parse details, filter, extract fields |
| `jobs sync [--dry-run]` | Create and update Notion pages; `--dry-run` prints the plan |
| `jobs apply-extractions --file PATH` | Merge haiku extraction output into `jobs.json` |
| `jobs daily [--dry-run]` | Run fetch, extract, and sync in one process (no haiku step) |
| `jobs profile [--path PATH]` | Compile `keywords.toml` and print the summary; exit 2 on a bad edit |
| `jobs summary` | Print the last run's summary as plain text |
| `jobs notify` | Send the macOS notification for the last run |
| `jobs bootstrap [--force]` | Load the whole Notion database into the cache |
| `jobs resync` | Reconcile the cache against Notion (added/removed/changed) |
| `jobs backfill [--dry-run] [--limit N]` | One-off: relabel Source Type and fill Job Area on existing rows |
| `jobs auth set-token \| check` | Manage the Keychain token |

Each command prints progress to stderr and one JSON object as the last line of
stdout. `fetch`, `sync`, and `daily` hold a single-flight lock so concurrent
runs are rejected immediately.

Exit codes: 0 success, 1 partial (resumable), 2 failed.

### Layout

```
src/jobs/       linkedin, filters, extract, cache, notion, sync, cli
data/           keywords.toml, companies.toml (committed); cache and run
                artifacts (gitignored)
tests/          scrubbed HTML and JSON fixtures
```

---

## License

MIT. See [LICENSE](LICENSE).
