---
name: daily-jobs
description: Run the daily LinkedIn to Notion job sync. Use when asked to run the daily jobs pipeline or when invoked as /daily-jobs by the scheduled task.
allowed-tools: Bash, Read, Agent
---

Run the pipeline exactly as below. Do not edit files, do not read HTML,
do not retry a failed command, do not investigate failures. Every
`uv run jobs ...` command prints one JSON object as its last stdout line;
read values from that line only. Work from the repository root.

1. Run `uv run jobs fetch`. If the exit code is 2, run
   `uv run jobs notify` and stop with a one-paragraph message quoting the
   `errors` from `uv run jobs summary`. If the exit code is 1, continue
   (partial fetch).
2. Run `uv run jobs extract`. If the exit code is 2, notify and stop as
   in step 1. Note `unresolved` and `unresolved_path`.
3. If `unresolved` is greater than 0, spawn exactly one subagent with the
   Agent tool, `model: haiku`, and this prompt, filling in the paths:

   > Read the JSON file at <unresolved_path>. It is a list of job postings;
   > each has `posting_key`, `fields` (which of `location`, `min_years`
   > need a value), `title`, `company`, `card_locations`, and
   > `description_text`. For each posting, using only the text given,
   > produce an object `{"posting_key": ..., "location": ..., "min_years_signal": ...}`.
   > `location`: every city or region where the job is located, as
   > `City, ST` or `Remote (US)` strings joined by `; `; include the
   > `card_locations` values first; null if the text names no place.
   > `min_years_signal`: the minimum years of experience required, as
   > `"N+"`, `"N-M"`, or `"BS+N"` / `"MS+N"` / `"PhD+N"` when a degree is
   > paired with years; `"Not explicit"` if the text states no minimum.
   > Only fill the fields listed in `fields`; set the other to null. Never
   > invent a location or a number. Handle at most 40 postings; ignore
   > the rest. Write the list as JSON to <run_dir>/extractions.json using
   > the Write tool and reply with the count written. Do not read any
   > other file.

   When it finishes, run
   `uv run jobs apply-extractions --file <run_dir>/extractions.json`.
   If the subagent fails or the file is missing, skip this step and
   continue; defaults are acceptable.
4. Run `uv run jobs sync`. Exit 1 means partial; continue.
5. Run `uv run jobs summary` and post its output verbatim as your final
   message. If any command in steps 1-4 exited with a non-zero code, also
   run `uv run jobs notify` before posting.

`<run_dir>` is `data/runs/<run_id>` with `run_id` from the fetch JSON line.

Expected cost of a normal day: five to seven Bash calls and no subagent.
