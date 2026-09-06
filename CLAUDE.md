# linkedin-to-notion-jobs

A daily pipeline that fetches early-career AI-engineer jobs from LinkedIn's
public guest endpoints and syncs them to a Notion database. Every deterministic
step is a Python CLI; a local SQLite cache is the dedupe source of truth.

- Runtime: Python 3.12 via `uv`. Run everything with `uv run`.
- Command list: `uv run jobs --help`. Contract: each command prints progress to
  stderr and one JSON object as the last stdout line.
- Driver: the `daily-jobs` skill (`.claude/skills/daily-jobs/SKILL.md`) runs the
  CLI on a schedule and spawns one haiku subagent per run for unresolved fields.
- Plan and reports: `docs/plans/2026-09-05-linkedin-notion-scraper/`
  (overview, phase files, reports). `docs/` is gitignored, so they live on disk.

## Rules

- Never write `Applied`, `Neglected`, `Unavailable`, or `First Seen` to Notion
  after a page is created.
- Never add a Notion property or change the schema. `check_schema` asserts the
  live schema before any write; `EXPECTED_SCHEMA` in `notion.py` is the record
  of the expected shape.
- To retarget the scraper at a different title or field, edit
  `data/keywords.toml` (seniority, relevance, and area lists) and
  `companies.toml`; never `EXPECTED_SCHEMA` or the Notion schema.
- Python never calls an LLM. The only model call is the haiku subagent the skill
  spawns; the CLI stays deterministic and testable.
- Fixtures under `tests/fixtures/` must be scrubbed: no real company names, job
  IDs, or descriptions.
- The Notion data source ID and integration token are never committed. The ID
  lives in `.env`; the token lives in the macOS Keychain (`.env` `NOTION_TOKEN`
  is the fallback).
