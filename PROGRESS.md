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

## Milestone 2 — Fit engine + tracker ✅ (2026-10-18)

Done when the sync test proves user edits survive, `eval-fit` runs on the persona eval set, and
a gated-out job always shows its reason. Met:

- **Gates (Stage 0, `fit/gates.py`).**
  - Must preferences: blocklist, include/exclude title keywords, seniority (new grad ≈ entry),
    employment type, term, start/end window, country, work mode, places within a radius
    (approximate metro table), pay floor (hourly/annual normalized), avoided industries.
  - Knockouts (always on): sponsorship, citizenship / export control, clearance, graduation
    window / class of / class year (rising, graduate standing), degree (pursuing vs required, "or
    equivalent"), years of experience, spoken language.
  - Preferred/bonus sections are never knockouts.
  - Each gate gives `pass | pass_unverified | unclear | fail` with the matched text as evidence.
    Unclear gates route to the **user** (sensitive answers) or to a **judgment task**.
  - `gated_out` now fills `source_yields`. `jobs --gated` and `show` always print the reason.
- **Recall (Stage 1, `fit/recall.py`).** De-identified profile lines (school names redacted even
  inside employer names; no contact info or links) feed pure-Python BM25. With the `[recall]`
  extra, sentence embeddings (cached per job) are fused by RRF. Top K is kept, and watchlist
  companies always pass. This is relevance, never shown as fit.
- **Requirements (Stage 2, code part, `fit/requirements.py`).** Section splitter (must /
  preferred / other headers, inline headers, long paragraphs → sentences). Every item is a
  verbatim slice and is typed. `defaults/synonyms.yaml` (about 90 skills, case-sensitive aliases
  for Go and R) pre-matches skills as `met`/`partial` with tier `stated`, citing the exact
  profile line and its `ref`. Eligibility and degree items a passing gate matched are `met`.
  Everything else is left unassessed.
- **Scores (Stage 3, `fit/scoring.py`).** Band from coverage with the spec's hard rules (an unmet
  must caps the band at stretch; strong needs every must stated-met; a level mismatch caps it at
  stretch). It is `provisional` while anything is unjudged. There is a preference score (pay,
  location, work mode, term, keywords, industry) and a priority score (band, recall relevance,
  preference, freshness, deadline, referral, watchlist). All weights are in
  `defaults/config.yaml`.
- **Volume guard.** Flags a job when you already have 2 applications at that company in 90 days.
- **Judgment tasks (`fit/tasks.py`).** One posting per task for recalled jobs at or above
  `fit.priority_threshold`.
  - The posting is wrapped in `<untrusted_content>`, with delimiters and zero-width characters
    neutralized.
  - Tasks include the de-identified profile lines, unclear AI-route gates, dealbreakers, the
    computed band, and the result JSON schema (`RequirementEvidenceResult`).
  - Tasks are deduplicated by input hash. `ingest` is still Milestone 5.
- **Tracker (`tracker/`).**
  - `states.yml`: the agent only moves forward and never leaves a terminal state; the user can set
    anything; Skipped needs a reason.
  - XLSX (openpyxl, written atomically) and Google Sheets (`[sheets]` extra) share one grid
    interface. Updates are cell by cell, so your formatting, extra columns and row order survive.
    User-owned headers are tinted.
  - Three-way sync per user cell (sheet / store / last synced): sheet edits win, `jobhunt mark`
    changes are pushed, and when both changed the sheet wins and the conflict is reported.
    Invalid values are reported and not applied.
  - Tabs: Jobs, Companies, Runs. An open XLSX (`~$` / `.~lock` file) is skipped with a message.
- **Commands:** `gate`, `match`, `sync`, `run` (fetch → gate → recall → score → sync), `jobs`,
  `show`, `mark`, `eval-fit [--set DIR | --personal]`. Writers have `--dry-run`, and dry runs
  never write to the sheet. `doctor` reports the recall mode and checks Sheets.
- **Eval set (`evals/fit/`).** 40 synthetic postings × 4 personas with grades, bands, gate truth
  and per-requirement labels.
- Migration `0003_fit.sql`; `openpyxl` dependency; `[recall]` and `[sheets]` extras; `gspread` in
  the dev group so CI runs the Sheets test.
- **262 offline tests** (macOS, Python 3.11 and 3.13). ruff and the pii scan are clean.

`eval-fit` on the seed set (single annotator; regression floors are asserted in tests):

| metric | value |
|---|---|
| gate accuracy | 0.975 (0 wrongly gated) |
| recall nDCG@10 | 0.982 |
| final nDCG@10 | 0.959 |
| band exact / within one | 0.54 / 1.00 |
| requirements assessed by code | 0.69 (accuracy where assessed 1.00) |

The 5 missed gates are by design. Three need the user's answer: clearance not answered, Japanese
when no languages are listed, and a graduate student on an undergraduate class-year posting
(fixed for "Graduate" standing). Two need a judgment task: adtech mentioned only in the
description, and an on-call dealbreaker. Exact band agreement is low because judgment tasks
aren't run in eval, so bands stay provisional and capped below strong.

Live run (2026-10-18, persona `intl-sponsorship`, real SimplifyJobs feed):

- 4,791 fetched in 4s end to end, of which 2,202 were gated out: term 1,971, country 319,
  seniority 9.
- 224 were unclear, mostly listings tagged PhD-only.
- 2,589 rows went to `tracker.xlsx`; the top priorities were ML and distributed-systems
  internships.

### Notes and decisions

- **Biggest gap: Simplify rows have no posting text**, so requirements can't be extracted and
  bands are "unknown" for Simplify-only jobs. Recall relevance carries their ranking. The fix is
  to fetch the posting from the ATS (Greenhouse/Lever/Ashby) for recalled jobs. The Milestone 3
  form inspection reads those same endpoints, so it should store the description as a
  non-polling view that doesn't affect closures.
- JobBERT short-phrase matching is **not** implemented: synonyms only. It was planned behind the
  `[recall]` extra; deferred until it can be tested with the model installed. The embedding
  recall path is tested with a fake embedder, not with sentence-transformers itself.
- Free-text dealbreakers and industry mentions that appear only in the description are judged
  by the AI task, not by code.
- YAML gotcha found while building the eval set: `[Chicago, IL]` is two items, and `ON`/`NO`
  parse as booleans. User files are validated, so this errors instead of misreading. Quote such
  values.
- Simplify sometimes lists one role twice under different URLs (e.g. the same title in two rows).
  This is a Milestone 1 dedupe edge case and was left alone.
- Sheets sync is tested against a fake gspread client only (no live spreadsheet available).
- CI has still not run (no GitHub remote). Windows is untested locally.

## Milestone 3 — Onboarding + answer bank + classifier (next)

Resume → profile, the local form (preferences + questionnaire; sensitive answers only there),
bank matching with confirmed reuse, Greenhouse `?questions=true` classification, Lever/Ashby page
parsing, the four classes, coverage report, hand-off packet. Also store ATS posting descriptions
for recalled Simplify jobs (see the Milestone 2 notes).
