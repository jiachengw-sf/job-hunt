# Progress

Start every milestone in a fresh session: reread the build spec and this file, propose a short plan,
get approval, build, test, run with a persona, show the output, commit, update this file.

## Milestone 0 — Skeleton ✅ (2026-10-18)

Done when `jobhunt doctor` and `jobhunt schema` work and the CI personal-data check passes. Met:

- Package layout (`src/jobhunt`, hatchling, `uv tool install` / `pipx`), Click CLI, MIT license.
- CLI contract: one JSON envelope per command, distinct exit codes (0–9, 70), `--human`, `--out`,
  `--dry-run` on writers, 8K output cap with spill-to-file, cursor pagination helper, JSON-envelope
  usage errors, no prompts. `@agent_command` registers each command's schemas.
- `jobhunt schema` (index / one command / `--all`).
- `jobhunt doctor` with `--fix`, `--offline`, `--dry-run`. Checks: Python, install method, data
  dir, config (validation + placeholders), store version, onboarding files, browser (Chrome /
  Playwright Chromium), skills per detected AI tool, tracker, source reachability. Every non-ok
  check names its fix. Read-only unless `--fix`; `--fix` holds the file lock.
- Single modules for paths (`$JOBHUNT_HOME`), config (defaults deep-merged with user layer), locking.
- Models (extra keys rejected): config, profile, preferences (Must/Nice with bare-value shorthand),
  answers (EEO defaults to decline; sensitive fields default unset), watchlist.
- SQLite store, migration `0001_init.sql`: jobs (`posted_at` nullable vs `first_seen` required,
  unique `dedupe_key`), job_urls, append-only events (enforced by triggers), sources (baseline,
  ETag), runs, source_yields, gate_results, requirements, fits, companies, applications (user-owned
  tracker columns), tasks. DB and data dirs are owner-only.
- Four personas in `tests/personas/` plus `*.example.yaml` templates.
- `jobhunt.devtools.pii_scan` + CI (ubuntu/macos/windows × 3.11/3.13): ruff, pytest, scan, CLI smoke.
- 81 offline tests, passing on 3.11 and 3.13 locally (macOS).

Verified run (persona `intl-sponsorship`, live network): `doctor --fix` → 13 ok, 2 expected
warnings (Playwright not installed; skills not yet written), all four ATS endpoints reachable.

### Notes and decisions

- Placeholders are `CHANGE_ME`, `<...>` or `TODO`; files containing them fail onboarding, so an
  unedited example can't pass.
- Phone numbers in fixtures must be `NXX-555-01XX` (555 is the exchange). The scanner caught an
  early persona mistake here.
- Users can add their own real name/email to a local denylist (`.pii-denylist`, gitignored, or
  `~/.jobhunt/pii-denylist.txt`) so the scanner also catches those.
- CI has not run yet: there is no GitHub remote. Windows is untested locally.
- `states.yml` (tracker state machine) is deferred to Milestone 2, where the tracker is built.

## Milestone 1 — Discovery ✅ (2026-10-18)

Done when `jobhunt fetch` works for every persona offline against fixtures, a live run logs
yields, and a re-run creates no duplicates. Met:

- `jobhunt/discovery/`: a `Source` plugin interface with Simplify, Greenhouse, Lever (US + EU)
  and Ashby sources, plus a `manual` source that re-polls jobs added by URL.
  - Simplify reads `.github/scripts/listings.json` on `dev`. The path and schema were confirmed
    live on 2026-10-18. Only `active && is_visible` rows are used, `date_posted` is the posting
    date, and `sponsorship` and `date_updated` are ignored.
  - Greenhouse uses `first_published` as the posting date and `pay_input_ranges` for pay.
  - Lever uses `createdAt`.
  - Ashby: `publishedAt` is metadata only (`posted_at` stays null) and `isListed: false` is
    skipped.
- ATS resolver: Greenhouse (boards / job-boards / embed / API / `?gh_jid=`), Lever, Ashby
  (`/application`, `?ashby_jid=`). About 30 unsupported ATSs are recognized by name; unknown
  hosts are `unknown`. Canonical URLs strip tracking params.
- Dedupe key is `ats:board:job_id`, else `url:<canonical>`, never company+title.
  - Cross-source merges keep every URL and each source's own view (`source_jobs`). Merged fields
    prefer direct ATS over manual over Simplify.
  - A `?gh_jid=` or Simplify link merges into a board job by `(ats, job_id)`.
  - Aggregator duplicate rows collapse, with their locations unioned.
- `posted_at` (source only, never inferred) is kept separate from `first_seen`.
- Silent first run per source: baseline `appeared` events carry `baseline: true` and are left
  out of `new_jobs`.
- Closures come from re-polling: a job missing from a complete, successful fetch is withdrawn
  from that source, and goes `live=0` + `closed_at` when no source lists it. Nothing is ever
  deleted. A job that comes back gets a `reappeared` event.
  - A source that suddenly returns nothing holds closures until it has been empty
    `empty_run_warn_after` (3) runs in a row, then warns.
- Append-only events: appeared / changed (per-source field diff: title, locations,
  description, pay, work mode, type, deadline) / disappeared / reappeared.
- `http.py` adds `HttpClient`:
  - a 1s gap per host
  - ETag / Last-Modified conditional GETs, with 304 → `not_modified`
  - jittered exponential backoff on 429/5xx and network errors
  - `Retry-After` honored (seconds or HTTP date), but a value over 120s gives up for the run
  - gzip
  - an injectable transport; `FixtureTransport` serves `<dir>/<host>/<path>` with content
    ETags.
- Legitimacy signals (table `signals`, one current row per job and kind):
  - `old_posting` (over 60 days; undated postings are never flagged)
  - `repost` (same company + normalized title, SimHash distance ≤ 3, seen in an earlier run
    under another ID)
  - `evergreen_language`
  - `ai_instructions`, which also becomes a `user_actions` hint; the text is never followed
  - `missing_from_ats` (a watched board was fetched and doesn't list a job another source
    still shows). This one was pulled forward from the spec's ghost-job list because it fell out
    of per-source presence.
- Per-source yields (`fetched/new/gated_out/errors`) go to `source_yields` every run, and a
  `runs` row stores the totals. Coverage of live jobs by ATS is reported, with unsupported ATSs
  marked.
- Commands:
  - `jobhunt fetch [--source ID] [--fixtures DIR] [--dry-run]`
  - `jobhunt add <url> [--company] [--title] [--fixtures DIR] [--dry-run]`

  Dry runs work on an in-memory copy of the DB. Writes hold the file lock.
- Config: `discovery` section (Simplify repo names and branch, signal thresholds). Optional
  `watchlist.yaml` loader. Each persona has a `watchlist.yaml`; `senior-backend` has no Simplify
  feed.
- Migration `0002_discovery.sql`: `source_jobs`, `signals`, and an `(ats, ats_job_id)` index.
- Fixtures: `tests/fixtures/sources/{base,update}` are synthetic, regenerated by
  `make_fixtures.py`, and cover each edge case. CI smoke now runs `fetch --fixtures` twice.
- 161 offline tests pass (macOS, Python 3.13). ruff and the pii scan are clean.

Verified live run (2026-10-18; persona `intl-sponsorship` plus a temporary watchlist of three
real public boards, one each for Greenhouse, Lever and Ashby):

- First run: 4,842 fetched (Simplify 4,792 active rows), 4,840 new, all baseline, 1.7s.
- Re-run: every source returned 304.
- Re-run with ETags cleared: 4,842 fetched, 0 new, 0 changed.
- Job, event and URL counts were identical across all three runs, and yields were logged for
  every source on every run.
- Signals were real:
  - an "Open Applications … Talent Pipeline" posting
  - a "general interest pool" role
  - two Simplify rows the employer's own Lever board no longer lists
- `add` on a stale Simplify Lever link returned not_found (the ATS gives 404). On live
  Greenhouse links it merged into the existing job and took `first_published` as `posted_at`.

### Notes and decisions

- `Summer2026-Internships` and `Summer2027-Internships` return the same file on `dev` (the
  repo was likely renamed). Repo names live in config, so next season is a config change.
- Simplify is about 37% Workday and 21% unknown hosts among active rows. Workday remains the
  top candidate adapter (Milestone 6 docs).
- Some Ashby job URLs use a slug the posting API doesn't know (404 on the board). `add` then
  asks for `--company`/`--title` and keys the job by URL instead of reporting a network failure.
- All Simplify rows are stored, including other terms and roles; filtering happens in the
  Milestone 2 gates (decided at plan approval). `gated_out` is 0 until then.
- Closures from Simplify alone come from `active` turning false. A job that Simplify still lists
  but the employer's board dropped stays live with a `missing_from_ats` signal.
- Windows is still untested locally, and CI has not run (no GitHub remote yet).

## Milestone 2 — Fit engine + tracker (next)

Gates (they fill `gated_out`), recall, requirement extraction and code pre-match, scores, band
rules, priority, volume guard, Sheets/XLSX sync with ownership, `states.yml`.
