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

## Milestone 3 — Onboarding + answer bank + classifier ✅ (2026-10-18)

Done when a new persona finishes onboarding in under 15 minutes and the coverage report runs on 30
live postings. Met (see "Verified runs" below), with one caveat: the onboarding run was scripted,
not a person at the keyboard.

- **Resume → profile (`onboarding/resume.py`, `profile.py`).**
  - `resume add` reads PDF (pypdf), DOCX (python-docx), Markdown or text. It copies the file to
    `~/.jobhunt/resume/` and drafts `work/onboarding/profile.draft.yaml` by section (education,
    experience with dates, projects, skills).
  - Contact details found on the resume go only to the form's pre-fill (`contact.json`,
    owner-only). The AI tool reads a redacted copy without contact lines or the name.
  - `profile confirm` refuses drafts that still have placeholders. `profile set` edits one value
    by path (`experience[0].title`, `--append`, `[a, b]`, `null` removes the key) and validates
    before writing. `profile show`.
  - The drafts reproduce every persona's schools, employers and titles from their synthetic PDF
    and DOCX resumes (`tests/fixtures/resumes/make_resumes.py`).
- **Local form (`onboarding/form.py`, `server.py`, `jobhunt setup form`).**
  - One page for `preferences.yaml` and `answers.yaml`. It is declared once, and that declaration
    drives pre-fill, rendering and parsing.
  - Every preference has a Must box (default Nice), and every field is optional.
  - Work-authorization blocks follow the target countries, and US EEO appears when the US is a
    target. EEO defaults to Decline; until the user ticks "fill these for me" (`eeo.confirmed`),
    EEO blocks are left to them on every application.
  - "Questions from your matches" lists `new_questions.yaml`: answer them, or confirm a
    suggested match.
  - Saves merge over the existing files and validate against the same models. Opening and saving
    unchanged is lossless for every persona.
  - The server binds 127.0.0.1 only. It needs a one-time token, checks Host and Origin, redirects
    after save, and exits after an idle hour. `setup form` returns the URL at once; `--status`
    and `--stop` manage it.
  - Every control has a distinct, linked label.
- **Answer bank (`bank/`).**
  - `keys.py` holds about 60 built-in canonical questions and where each answer lives (profile or
    a typed answers section). Unanswered resolves to None, never a guess.
  - Sponsorship "now or in the future" combines the two typed answers. "Worked here before?"
    needs a known employer list. GPA respects the disclosure threshold.
  - `match.py` tries, in order: the ATS field name, a wording the user confirmed, a curated
    wording (`defaults/question_synonyms.yaml`), then the user's own bank question. Anything only
    similar (token and trigram overlap) is a candidate the user confirms once.
  - Company names normalize to `[Company]`, so one confirmation carries to every employer.
  - Selects match the form's own option texts, with several accepted texts per answer. Between
    two lookalike options ("Yes, now" / "Yes, in the future") nothing is chosen. EEO "Decline"
    finds the form's own decline wording.
  - Sensitive labels (work authorization and visa status, EEO, pay, criminal history, ...) never
    go through chat. Commands: `answers show | add | edit | confirm | questions`; `add` refuses
    sensitive questions and points to the form.
- **Form readers (`forms/`).** Every reader is read-only.
  - Greenhouse: `jobs/{id}?questions=true`, including the location, compliance and demographic
    blocks, plus the page-only Hispanic/Latino question before Race (`shown_if`).
  - Lever: the server-rendered `/apply` page. Card templates are authoritative for custom
    questions, and the CAPTCHA on Submit is noted.
  - Ashby: the hosted page builds its form in JavaScript (an assumption in the plan was wrong
    here), so it is loaded in headless Chrome or Chromium. jobhunt reads the `ApiJobPosting`
    response the page itself fetches. A route guard allows only GETs and the page's two
    read-only queries; nothing is clicked. It needs the `[browser]` extra; without it, Ashby jobs
    are Manual with a fix hint.
  - Form specs are cached a week in `form_specs` (migration `0004_forms.sql`).
- **Classifier (`forms/classify.py`).**
  - Each field resolves as answered, unanswered, candidate, unknown, option mismatch, too long
    (character limits are checked), essay, human-only, or skipped (optional).
  - Classes: Manual (form unreadable or unsupported), Essay (required free text or cover
    letter), Needs answers, Ready.
  - Always-human flags: attestations and policy questions, consents, unreviewed EEO, Ashby
    knockout questions, and Lever radios and checkboxes.
  - Output carries where a value comes from, never the value.
- **Commands.**
  - `inspect [JOB…] [--top N]` defaults to recalled jobs at or above `fit.priority_threshold`.
    It re-classifies the other cached forms locally (no network) and rebuilds
    `new_questions.yaml` grouped by normalized wording.
  - `coverage [--sample 30]` reports the required-field share answered, the top unanswered
    questions, classes, and the ATS mix ("N% of your matches are on Workday: hand-off
    packets").
  - `packet <job>` writes an owner-only local page: apply link, fit, every known answer with copy
    buttons and character counts, essay prompts, and the human-only fields. Values appear only
    there.
  - `run` now classifies the top `forms.max_per_run` jobs (`--no-inspect` skips it).
- **Milestone 2 gap closed.** Posting text read with a form is stored in `posting_views`, a view
  rather than a source. Job views use it when the sources carry none, and fit is recomputed.
  - In the fixtures, the Simplify-only job now gets requirements and a band.
  - A test proves the view never keeps a job live after its sources close it.
- **CLI.** Sub-command groups (`jobhunt profile set`; `jobhunt schema profile set`), and outputs
  serialize by alias. `http` takes an `Accept` header, and `FixtureTransport` serves `.html`
  pages.
- **Fixtures.** `tests/fixtures/forms/make_forms.py` builds a synthetic overlay of Greenhouse,
  Lever and Ashby forms in three templates (Ready / Essay / Needs answers).
- **378 offline tests** (macOS, Python 3.13). ruff and the pii scan are clean, and the eval-fit
  floors still pass.

Verified runs (2026-10-18):

- **Live coverage.**
  - Setup: persona `intl-sponsorship`, the live Simplify feed, and a temporary watchlist of four
    real boards (Greenhouse, Lever, two Ashby). 6,127 fetched; 181 recalled.
  - `coverage --sample 30` took 37s: 27 forms read (25 Greenhouse, 2 Lever, 3 Ashby through the
    browser). The 3 Manual are links on a custom domain with no Greenhouse board token.
  - 59% of required fields were covered (225 of 382), plus 13 required essays. Classes: 21 needs
    answers, 6 essay, 3 manual.
  - Top unanswered questions: remote-work plans, location-specific work authorization, target
    countries, location-specific sponsorship (offered as a candidate for
    `work_auth.sponsorship`), and WhatsApp opt-in.
  - ATS mix of matches: 74% Greenhouse, 10% unrecognized, 5% Workday, 4% Lever, 3% Ashby.
- **New-persona onboarding (`newgrad-analyst`, from its DOCX resume).** Scripted run, with the
  form filled in through the browser pane:
  - `resume add`, tidy the draft (headline, skill groups), `profile confirm`, `setup form`
    (contact and education pre-filled; about 30 fields typed), Save, `doctor`, first `run`,
    `coverage`.
  - About 3 minutes end to end; the form itself took under a minute.
  - A person would be slower; the estimate is well under 15 minutes, but a timed run with a real
    user is still owed.

### Notes and decisions

- Live forms ask many location-dependent questions ("authorized to work in the location(s) you
  selected", "countries you anticipate working in"). They stay Needs answers by design: the
  answer depends on the job. Answering them once per wording in the form fixes it.
- Forms ask about AI note-takers and recording consent. These are flagged as attestations only
  when their wording matches; some live variants (e.g. "we use BrightHire to record") still show
  as unanswered questions.
- Ashby via the browser costs about 1 to 3s per job; one browser is shared per run.
- `[Company]` templating only applies to the job's own company name. Other employer names in a
  question stay literal.
- Embedding similarity for labels hooks in (`Matcher.embed`) but isn't wired to the `[recall]`
  model yet; token and trigram similarity plus confirmation carry it.
- Approved essay text in packets is a placeholder until Milestone 5.
- Playwright is in the dev dependency group, so CI installs the package, but no browser binary;
  the browser path is exercised only live. CI has still not run (no GitHub remote), and Windows
  is untested.

## Milestone 4 — Pre-fill ✅ (2026-10-18)

Done when a batch of fixture and live forms fills correctly, stops before Submit, read-back
matches, and the guard tests pass. Met (see "Verified runs" below). The one caveat is that live
forms were filled field by field through a scratch harness, because none of the sampled live
postings was Ready for a test persona. The full `jobhunt prefill` flow ran on fixture forms,
both headless and headed.

- **Values (`prefill/values.py`).** For each Ready job, code builds `answers.json` from the
  classifier's resolution, the profile and the bank (`classify.fill_value`). The model never
  picks a value.
  - Each field gets an action: `fill`, `recommend` (Lever radios and checkboxes), `human`
    (attestations, consents, unreviewed EEO, essays), or `skip` (optional and unanswered).
  - The validator checks the sheet against the form spec before a browser opens: required
    fields are present, choice values are among the form's own options, text fits its limit,
    files exist, and no question id comes from another job. Zero-width and bidi characters are
    stripped.
  - The file is owner-only under `work/prefill/<job>/`.
- **Submit guard, three layers (`prefill/guard.py`).**
  1. No command or option anywhere is named submit (contract test).
  2. `safe_click` refuses `type=submit`, untyped `<button>`s inside a form, and buttons or links
     saying submit / apply / send / finish / complete. Options are exempt, so an option labeled
     "Complete" still works. `safe_press` refuses Enter. A source test fails on any other click,
     key press or submit call in `prefill/`.
  3. `RouteGuard` (on the page, installed before the first request) blocks **every**
     non-GET/HEAD/OPTIONS request during the fill, except the adapter's allowlist. Known submit
     endpoints stay blocked even when an allow pattern matches. It is released only at handoff.
     This is stricter than the spec's "abort known submit endpoints" (decided at plan approval).
     Contexts block service workers, because `page.route` can't see their requests.
  - Tests prove a scripted `fetch` to the submit URL and a scripted `form.submit()` never reach
    the server during the fill, and that the human's own click does after handoff.
- **Browser (`prefill/browser.py`).** `launch_persistent_context`, headed, with real Chrome when
  installed (`prefill.browser: auto`) and Playwright's Chromium otherwise. Profile at
  `~/.jobhunt/browser-profile`. No stealth plugins, proxies, extra args or spoofed headers.
- **Adapters (`prefill/adapters/`).**
  - **Greenhouse:** react-select comboboxes (type, then click the exact option);
    `#candidate-location` counts only after clicking an option; the phone country is set
    before the number; per-job `question_*` ids with no label fallback (only demographic
    questions, which have no DOM id, are found by exact label); resume through `#resume`, with
    the `accept` list checked (a `.md` resume is reported, not uploaded); confirmation is
    `application_id=` in the URL.
  - **Lever:** text, textarea, native select and location (`/searchLocations` dropdown). The
    resume goes first and the engine waits for `/parseResume`, which autofills fields. Radios
    and checkboxes are recommended, never ticked. Confirmation is `/thanks` or the submitted
    text.
  - **Ashby:** fields located by `[data-field-path]` (there is no `<form>`). The resume goes
    first and the engine waits for `ApiAutofillApplicationFormWithUploadedResume`, then fills
    or overwrites. Yes/no buttons (`aria-pressed`), radio and checkbox groups by label, and a
    location combobox. Confirmation is the success container or the submitted text.
  - Location answers pick an option only when exactly one names the same city and region
    (state abbreviations or names); otherwise the field is left for you.
  - Every value is read back after all writes. Phone numbers compare by digits, files by name
    on the page, choices by exact text.
  - Fields that are missing, unmatched, refused or rejected, plus empty optional fields the ATS
    autofilled from the resume, come back as statuses. Nothing is guessed.
- **Review (`prefill/review.py`).** Fields that need you are outlined: red for required
  problems, amber for review. Each gets a note next to it: a plain `<div>`, never a form
  control, showing the recommended or expected value on your own screen only. A banner says
  jobhunt never submits. A full-page screenshot goes to `work/screens/` (owner-only).
  `TabReview` returns `ready_for_review` with filled vs. to-fill counts and the needs-you list:
  ids, labels and statuses, never values.
- **Driver (`prefill/driver.py`) and `jobhunt prefill`.**
  - The command re-classifies, validates and writes each `answers.json`. It then starts a
    detached driver and returns at once, as `setup form` does.
  - The driver fills up to `prefill.batch_size` (5) tabs, marks each job **Prefilled**, releases
    that tab's guard, then watches the open tabs.
  - When a tab shows the confirmation after the human submits, the job is marked **Applied**
    (`agent_advance`) with `applied_on` set to today. A tab closed without submitting stays
    Prefilled.
  - Command flags: `--status`, `--stop`, `--batch`, and `--dry-run` (builds and validates
    without a browser). `--fixtures DIR` (offline pages via `FixtureRouter`) and `--headless`
    are for testing. Only one batch runs at a time (a second start exits 7). Store writes take
    the file lock only for each update.
  - New config: `prefill.browser`, `parse_wait_s`, `watch_hours`.
- **Fixtures.** `make_forms.py` also builds `tests/fixtures/forms/pages/`: a synthetic browser
  page for every Greenhouse, Lever and Ashby fixture job. Each is built from the same questions
  as the form specs, with small scripts (`js/`) that copy the live widgets and quirks
  (react-select, location autocomplete, phone country, S3 upload, resume parse that overwrites
  fields, Ashby draft autosave, JS or form-POST submit), plus the endpoint responses.
  Greenhouse template A gained a location question.
- **CI** installs Playwright Chromium, so the browser tests run there (they skip without a
  browser), and smoke-tests `prefill --dry-run`.
- **498 offline tests** (macOS, Python 3.13), about 80s including the browser tests. ruff and
  the pii scan are clean.

Verified runs (2026-10-18):

- **Fixture batch, every persona.** All 12 persona × ATS combinations (Greenhouse, Lever and
  Ashby template-A forms) fill with every `fill` field read back equal. No submit request
  reaches the fixture server during the fill. After handoff the human's click submits, and
  confirmation is detected.
  - The real detached driver (headless) fills a batch, marks the jobs Prefilled, and `--stop`
    leaves them Prefilled.
  - The in-process test submits one tab and closes another: Applied with the date vs. still
    Prefilled.
- **Headed, real Chrome (`first-year-intern`, fixtures).** `jobhunt prefill` opened 4 tabs
  (2 Greenhouse, 1 Lever, 1 Ashby). All reached `ready_for_review` with 7/7, 8/8, 9/9 and 9/9
  read back. Ashby knockout questions were filled and flagged, Lever radios recommended, EEO
  left to the user. `--stop` closed the browser; the jobs stayed Prefilled. 11 POSTs reached
  the fixture server (uploads, parses, autosaves), and none was a submit.
- **Live forms (synthetic `intl-sponsorship` with its synthetic PDF resume; resume uploads
  approved at plan time).** 5 real postings: 2 Greenhouse, 1 Lever, 2 Ashby. Inspected live:
  none was Ready, since each had company-specific questions. A scratch harness ran the same
  adapters and guard headless on their answered fields, then closed each tab without
  submitting.
  - After the fixes below: 8/8, 6/6, 7/7, 6/6 and 6/6 filled and read back equal, including
    phone country, location autocomplete on all three ATSs, and resume uploads.
  - Requests that left: Greenhouse's S3 upload; Lever's `parseResume`, hCaptcha config and
    Cloudflare check; Ashby's reads, upload chain and draft autosave. Blocked: only analytics.
    **No submit control was clicked, and no request went to a submit endpoint.**
  - With the persona's fictional town, location was correctly left for the user on every ATS
    rather than guessed.

### Notes and decisions

- **Live findings, fixed and covered by fixtures/tests:**
  - Greenhouse ignores input until the page hydrates, and its phone-country list opens only
    once you type.
  - Ashby saves drafts server-side (`ApiSetFormValue`, and `ApiSetFormValueToFile` attaches the
    upload). Blocking them left the resume unattached, and could break the human's submit.
    They are allowed; they are not submits.
  - CAPTCHA and bot-check widgets (hCaptcha config, reCAPTCHA reload, Cloudflare challenge
    platform) are allowed to load so the human's own submit works. jobhunt never solves or
    touches them.
- **Fixed: Greenhouse Hispanic/Latino.** The API lists only the EEO `race` question; the page
  first asks "Are you Hispanic/Latino?" (`hispanic_ethnicity`: Yes / No / Decline To Self
  Identify) and shows Race only after No. The reader now adds that field before Race, and Race
  carries `shown_if`. answers.json skips Race when the filled answer hides it, and unreviewed
  EEO leaves both to the user, with the Hispanic question outlined.
- **Open:** other page-only fields the APIs don't list would still go unflagged. A check for
  page fields missing from the spec isn't built.
- **Not verified live:** the confirmation pages, since nothing may be submitted.
  - Greenhouse's `application_id=` comes from the spec.
  - Lever's `/thanks` and Ashby's success container are educated guesses with text fallbacks.
  - Watch the first real submissions; if a confirmation is missed, the job stays Prefilled and
    `jobhunt mark <job> Applied` fixes it.
- Ashby's resume autofill didn't fire for the minimal synthetic PDF on the live forms; the
  engine waits up to `parse_wait_s` and then fills anyway.
- Essay text from approved drafts is Milestone 5. Until then essay fields are always the
  user's, and Essay-class jobs aren't pre-filled.
- Playwright's sync API allows one instance per thread, so tests that run the real driver use a
  subprocess, and in-process tests share one session browser.
- Windows is untested, and CI has still not run (no GitHub remote).

## Milestone 5 — Skills (2026-10-19) — met in Claude Code; Codex not verified

Done when the full daily flow works for a persona in **both** Claude Code and Codex and the
trigger evals pass in both. **Claude Code: met. Codex: not installed on this machine, so
nothing was run in Codex** (the Codex paths, sidecar and eval adapter are built from its docs
but untested).

- **`jobhunt ingest`** (`ingest/`): validates every result in `work/results/` against its task
  kind and against code truth (the stored posting, the profile lines, a gate re-run, the
  preferences), not the task file. Rejections keep the task pending (attempts + 1) and write
  `<task_id>.errors.json` with a JSON path and fix per problem; exit 4. Accepted results move
  to `results/ingested/` with their task file. `jobhunt tasks` lists pending work.
  - `requirement_evidence`: every requirement text, gate quote and dealbreaker quote must be in
    the posting; every `evidence_quote` must be the cited profile line; `stated` needs the line
    to name the skill; `none` means `unmet`; `inferred` needs a note; every task requirement,
    unclear AI gate and dealbreaker must be answered. Applied as AI requirement rows plus
    `judgments` (resolved gates, dealbreaker hits as a failing `dealbreaker` gate), then the job
    is refit, so bands stop being provisional. A changed posting or profile (`basis_hash`)
    drops them and writes a new task; an unchanged one is never re-tasked.
  - Postings where code finds no requirements now ask the AI to extract them (found while
    building this: every base-fixture task had no requirements and couldn't add any).
- **Research** (`jobhunt research`): cached brief (14 days) or a task with the hard budget (5
  searches, 8 fetches, no subagents). Ingest checks the counts, quote length (≤ 25 words),
  that each URL was a listed source, and **re-fetches each cited page once** to find the quote.
  Facts from pages that no longer load are dropped, never kept unverified. Briefs feed the
  tracker's Company brief column and Companies tab.
- **Essays** (`jobhunt essays list|draft|review|show|approve`): drafts are sentences that each
  cite a profile line, base answer, brief fact or posting quote; ingest rejects any number,
  known skill or leadership word the source lacks, strips hidden/zero-width Unicode and
  enforces the form's character limit. One review pass (a fresh-context task) returns
  `{old, new, reason}` edits that code applies (each `old` exactly once, no new claims) and
  never overwrites a file the user already edited. Drafts land in `work/essays/` marked
  "draft — edit before use"; `approve` reads the user's text, records the edit ratio and
  nudges when unchanged. Approved essays fill answers.json and the hand-off packet, so Essay
  jobs can be pre-filled.
- **Outreach** (`jobhunt outreach due|draft|show|mark-sent`): referral, coffee-chat and
  follow-up drafts with the same grounding and a 600-character limit. Follow-ups are due 7 days
  after applying or the last follow-up, at most 2 (config). **There is no send code path**
  (a test checks the modules and the command list); `mark-sent` only records what the user
  sent.
- **Notifications** (`jobhunt digest`, `jobhunt notify test`, and a notify step in
  `jobhunt run`): the terminal digest (Ready, questions, Essay prompts and deadlines, Manual,
  closures, legitimacy flags, follow-ups, referral questions, pending AI tasks); optional ntfy
  (topic or own server), Discord (mentions off) and SMTP to the user's own address (password
  from an env var). Fast alerts: one urgent push per new non-baseline posting at or above
  `alert_priority`, once per job; the digest is pushed daily by default. Every attempt is
  logged; failures are reported.
- **Scheduler** (`jobhunt schedule install|status|remove`): launchd LaunchAgent, a marked
  crontab line, or a named Task Scheduler task calling a `.cmd` wrapper. Idempotent, touches
  only its own entry, `--dry-run` prints it. `python -m jobhunt` works as a fallback.
- **Skills** (`skills/`): the seven skills with open-spec frontmatter only (descriptions
  316–434 characters, bodies 39–63 lines, detail in `references/`). A test lints them and
  checks that every backticked `jobhunt …` command exists.
- **`jobhunt skills install`**: copies into `~/.claude/skills` / `~/.agents/skills` (or the
  project-level `.claude/skills` / `.agents/skills`). Checked against the current docs:
  Claude Code's copy of jobhunt-apply gets `disable-model-invocation: true`; Codex's copy gets
  `agents/openai.yaml` with `policy.allow_implicit_invocation: false`. Marker files make
  re-runs no-ops and protect same-named skills the user wrote. The wheel ships the skills.
  `doctor` warns if apply could run unasked.
- **AGENTS.md** gained the new modules and an "AI task rules" section; CLAUDE.md points to it.
- **`JOBHUNT_FIXTURES`** serves offline snapshots when an AI tool drives `jobhunt run`.
- 646 offline tests (from 498), ruff and the personal-data scan clean at every commit.

Verified runs:

- **Trigger evals, Claude Code** (`evals/triggers/results/claude-code.md`): 7 skills × 20
  queries (half near-misses) × 3 runs = 420 headless runs, default model, Skill tool only.
  **140/140 queries pass** (≥ 2 of 3 runs right); 2 single runs missed (a setup scheduling
  request and a judge ingest-rejection request each answered once without loading the skill).
  Implicit pre-fill requests never loaded jobhunt-apply; `/jobhunt-apply` always ran it.
- **Daily flow, Claude Code** (`evals/daily/results/claude-code.md`): "Run my job hunt." for
  synthetic `intl-sponsorship` on offline fixtures. All 9 checks pass: the daily skill ran the
  pipeline, did all 10 judgment tasks through the judge skill (fixing its ingest rejections),
  showed the digest, batched the questions and sent the sponsorship one to the local form,
  offered `/jobhunt-apply` without starting it, and synced. It ignored the AI-aimed text in a
  fixture posting and told the user. Nothing was pre-filled, applied or sent. 154 s, ≈ $0.89.
- **Codex: not tested, by decision (2026-10-19).** The maintainer doesn't use Codex and won't
  test it; the README says so. The install paths, the explicit-only sidecar and the harnesses'
  `--tool codex` mode are built from Codex's docs and have never run. Anyone who wants numbers
  can run `uv run python -m jobhunt.devtools.trigger_eval --tool codex` and
  `uv run python -m jobhunt.devtools.daily_e2e --tool codex`.

### Notes and decisions

- Quote matching is exact after normalization (NFKC, hidden characters removed, curly quotes
  and dashes unified, case folded, whitespace collapsed). Nothing fuzzy.
- Grounding catches numbers, leadership words and skills **listed in `synonyms.yaml`**; an
  unlisted skill name (e.g. Haskell) isn't caught. Adding aliases there widens the check.
- An `inferred` must-have still scores as `met_inferred` (Milestone 2 weights) and blocks
  `strong`; ingest doesn't rewrite the AI's status.
- A dealbreaker hit becomes a failing `dealbreaker` gate, so the job is gated out with the
  quote as evidence.
- Explicit invocation leaves no marker in Claude Code's stream-json, so trigger evals score
  `/jobhunt-apply` by the reply following the skill (it mentions pre-fill). Implicit triggers
  are scored by the Skill tool call.
- A full 420-run eval used most of a 5-hour usage window: the runner stops at 80% (it reached
  100% once with 5 runs in flight) and `--resume` finishes the rest. Saved results keep model
  replies only for explicit invocations (one reply named a Homebrew formula with an @ sign,
  which the personal-data scan read as an email).
- The daily harness pre-approves `jobhunt` and file tools, but the user's own Claude Code
  permission settings apply on top: the agent also ran read-only `cat` and `python3`.
- `notify test` sends without recording; ntfy.sh topics are public, so the setup skill says to
  pick an unguessable name.
- Windows (Task Scheduler) is untested, and CI has still not run (no GitHub remote).

## Milestone 6 — Calibrate + share ✅ (2026-10-19)

Done when calibrate works on persona tracker data with tests, the docs take a new user from
install to a first daily run, and the demo GIF exists. Met:

- **`jobhunt calibrate`** (`fit/calibrate.py`, read-only).
  - Reads each job's highest status from the append-only `status_changed` events, so an
    Interviewing → Rejected job still counts as an interview.
  - Per band, for applied jobs: interview and offer rates over **replies only**. No-reply
    applications (including withdrawn ones) are counted and shown, but never enter a rate.
  - Labels each band `separating | flat | inverted | insufficient_data`. A band is compared with
    the next band down that has data; the lowest compares upward.
  - Suggestions: a flat or inverted pair raises the upper band's threshold. How the lower band's
    interviewed jobs scored their must-haves can suggest raising `met_inferred` or `partial`, or
    sends the user to finish judgment tasks or add a profile line. Skip reasons among
    strong/good jobs (`too_senior`, `pay`, `location`, `domain`) suggest
    `level_mismatch_years` or preference weights.
  - The suggestions come back as a YAML `config_snippet`; the user pastes it into config. There
    is no apply flag.
  - The thresholds are visible in config: `calibration.min_responses` (5), `flat_margin`,
    `skip_min`, `skip_share`, `step`.
  - The daily skill runs it weekly and leaves config to the user.
- **README** for first-time users: an honest coverage table (pre-fill on Greenhouse, Lever and
  Ashby; Workday and others get hand-off packets), install, onboarding, the first daily run,
  daily use, bands and calibration, the tracker, troubleshooting by exit code, and uninstall.
- **CONTRIBUTING.md**: setup, the rules every change keeps, recipes for a new source and a new
  ATS adapter (form reader, pre-fill adapter, fixtures, guard tests), and the open questions
  for Workday (is its discovery endpoint documented enough, the sign-in hand-over, the
  multi-step wizard vs. Submit, resume parsing).
- **Docs test**: every `jobhunt …` command, flag and local link in README, CONTRIBUTING and
  AGENTS exists.
- **Demo GIF** (`docs/demo.gif`, 0.8 MB) recorded with VHS from `docs/demo/demo.tape`. It runs
  the real commands for synthetic `intl-sponsorship` on offline fixtures, in a scratch `HOME`
  and `JOBHUNT_HOME` (`docs/demo/setup.sh`): doctor, run, ingest + match, digest, mark,
  calibrate. Onboarding files and the judgment stand-in (the tests' `judge_all`) happen off
  screen, and the screen says so. A test checks that the tape uses a synthetic persona and a
  scratch home.
- **Publish prep**:
  - `pii_scan --history` scans every blob reachable from any ref, plus every commit message;
    the whole history is clean (460 blobs, 54 commits at the time).
  - CI checks out the full history for that scan and adds `ruff format --check`.
  - The version comes from one place (`jobhunt/__init__.py`, now 0.1.0; `schema` had reported a
    stale 0.0.1). Classifiers and keywords added; the demo GIF is left out of the sdist.
  - `.gitignore` covers project-level skill installs, `.env` and a stray tracker.
  - A wheel built and installed into a clean venv runs `doctor --fix`, `skills install` and
    `schema`.
  - Checked: MIT LICENSE, the four `*.example.yaml` files, no absolute home paths or username
    in any tracked file or past blob, largest blob is the demo GIF.
- **Install-aware hints** (`install.py`): every "reinstall with the X extra" or "upgrade"
  message builds its command from how jobhunt was actually installed. It reads PEP 610
  `direct_url.json` and covers uv tool or pipx installs from PyPI, a git URL, a local checkout or
  a wheel, plus `uv sync` for a dev checkout. Before this, the hints assumed PyPI, where jobhunt
  isn't published.
- **Windows readiness (static).** Every text read and write names `encoding="utf-8"`, and ruff's
  PLW1514 enforces it. A packet test no longer asserts POSIX mode bits on Windows. Locking
  already used `msvcrt` there.
- 685 offline tests (from 646). ruff, format and both personal-data scans are clean at every
  commit. The full suite also passes on **Python 3.11.14** (685 passed), and CI's own steps
  (locked sync, ruff, both scans, the CLI smoke script) pass locally on macOS.

Verified (2026-10-19): the README walkthrough, run from a freshly built 0.1.0 wheel in a clean
venv with a scratch `HOME`. Persona `newgrad-analyst`, offline fixtures, no `[browser]` extra.
- `doctor --fix` passes with 0 failing checks.
- `skills install` puts 7 skills in `~/.claude/skills`, with jobhunt-apply explicit-only.
- Unedited `*.example.yaml` files fail onboarding, naming each placeholder field.
- With persona files: `run` (5 jobs), `coverage` (93% of required fields), `digest` (1 Ready),
  `calibrate`, `schedule install --dry-run` (launchd) and `tracker.xlsx` all work.

### Still owed or untested

- ~~CI has never run~~ Done after publishing (2026-10-19): CI is green on Linux, macOS and
  Windows with Python 3.11 and 3.13. The first runs found and fixed: `os.kill(pid, 0)` killing
  the form server and pre-fill driver on Windows (now one shared `procs.pid_alive`), the form
  server stalling on a reverse DNS lookup while binding (macOS runners), text files read without
  UTF-8, path-separator assumptions in tests, and a browser-test race. Guard tests cover the
  encoding and `os.kill` cases.
- **Windows real use is untested.** The suite passes there; the Task Scheduler entry and
  pre-fill with an installed Google Chrome have not been tried by a person. Headless real Chrome
  with a persistent profile hangs on GitHub's Windows runner, so the browser test uses bundled
  Chromium.
- **Onboarding has not been timed with a real person**. The 15-minute target is an estimate
  from persona runs.
- **Codex CLI is untested, by decision** (see Milestone 5).
- **The fit eval set has a single annotator.** The spec asks for two graders per pair, and the
  set is synthetic postings rather than real public ones. `eval-fit --personal` lets a user
  check against their own labels.
- **Not on PyPI.** The README installs from a clone, and the hints now follow the install
  source. Publishing there is optional; check whether the name is free first.
- Calibrate on persona fixtures has too few jobs for any band label. Labels are covered by unit
  tests on synthetic trackers.
- Cosmetic: with `priority_threshold: 0`, a first `jobhunt run` reports "0 new, 2 replaced"
  judgment tasks while 10 are pending. The run report shows the second match pass only.

### Known product gaps (from earlier milestones, still open)

- **No Workday adapter or other ATS adapters.** Pre-fill covers Greenhouse, Lever and Ashby;
  every other ATS gets a hand-off packet. Workday alone is about a third of Simplify's
  internship rows. CONTRIBUTING lists the open questions.
- **Simplify-only jobs** get a band only when jobhunt can read their posting text from a
  supported ATS. Others rank on recall relevance alone, with band "unknown".
- **Confirmation pages are educated guesses** for Lever and Ashby, since nothing may be
  submitted while testing. A missed confirmation leaves the job Prefilled;
  `jobhunt mark <id> Applied` fixes it.
- **Page-only form fields** that an ATS API doesn't list go unflagged; only Greenhouse's
  Hispanic/Latino question is handled.
- **Location-dependent screening questions** stay Needs answers until answered once per
  wording.
- **Grounding catches only skills listed in `synonyms.yaml`.** An unlisted skill name in a draft
  isn't caught.
- **Embeddings are wired but unproven.** Recall and label matching only run with a fake
  embedder in tests, and JobBERT phrase matching isn't built.
- **Live pre-fill was verified field by field.** No live posting was Ready for a test persona,
  so the full `jobhunt prefill` flow has only run on fixtures.

### Publishing to GitHub

Published on 2026-10-19 as https://github.com/jiachengw-sf/job-hunt (public, `main`). It was first
pushed as `jobhunt`, then renamed to `job-hunt` to match the maintainer's other repo names; GitHub
redirects the old URL. The Python package and command stay `jobhunt`. Remaining:

6. Optional: branch protection on `main`, private vulnerability reporting, and a `v0.1.0` tag.
   Then, if you want `uv tool install jobhunt` to work, run `uv build` and `uv publish` to PyPI.

## Application tracking + readable outputs ✅ (2026-10-21)

Spec: `application-tracking-prompt.md` (outside the repo). Done when, for a test persona,
pre-fill confirmation auto-creates an application with its folder, screenshot and JD snapshot;
a manual `jobhunt applied` for a Workday job shows the right "where to check"; a sheet status
change shows in the dashboard timeline after sync; `jobhunt dashboard` opens a readable page;
and `APPLICATIONS.md` reads cleanly. Met (see "Verified" below).

- **Store.** Migration `0009_application_records.sql`: `application_records` (one per job,
  `job_id` UNIQUE; the posting's details copied at apply time; `applied_at` UTC plus an
  approximate flag; method and how it was recorded; resume name and sha256; submitted answer
  *labels*; confirmation ID, screenshot, JD snapshot, folder; where to check; recruiter) and
  `application_dates` (interviews, deadlines). Status, referral, next step, follow-up date,
  outcome and notes stay in `applications`; the status history is the existing append-only
  `status_changed` events, now with an optional note. A test migrates a populated v8 database
  without losing anything.
- **Recording** (`tracker/applications.py`), always exactly one record per job:
  - pre-fill: when the driver sees the confirmation it screenshots the page, reads Greenhouse's
    `application_id` from the URL, and records with the values it filled (`submitted_answers.md`
    is written by code; sensitive answers are grouped and never shown to a model)
  - `jobhunt applied <job id | job key | URL> [--at …] [--method] [--referral] [--portal]
    [--login-email] [--check-note] [--resume] [--note] [--via cli|chat]`; an unknown URL exits 6
    pointing to `jobhunt add`
  - `jobhunt mark <id> Applied` (or later) on a job without a record
  - the sheet: Status Applied or later with no record → recorded at sync time, flagged
    "time approximate"
  - a repeat is a no-op that names the existing record; one too many applications to a company
    in the window is recorded with the volume-guard warning
- **Where to check** (`defaults/status_portals.yml`, `tracker/portals.py`), checked read-only on
  2026-10-21:
  - Greenhouse → MyGreenhouse (`my.greenhouse.io`, which redirects to its sign-in; Greenhouse's
    support article says applications to Greenhouse-hosted jobs appear there, with
    active/inactive status only if the employer turns it on)
  - Workday → that tenant's Candidate Home, derived from the posting URL
    (`…myworkdayjobs.com/<site>/userHome`; on two live tenants it redirects to that site's sign-in)
  - Lever, Ashby, iCIMS, SmartRecruiters, Taleo, SuccessFactors and anything unknown → `email`,
    with a note saying jobhunt doesn't know a portal. No portal URL is guessed.
- **Times** (`tracker/timefmt.py`): stored UTC, shown like "Wed, Oct 21, 2026 · 9:42 PM PDT" in
  `tracking.timezone` or the computer's zone, each instant with its own DST offset. `--at`
  reads "now", "today 3pm", "this afternoon", "yesterday 21:30", weekday names, "Oct 21 9:42pm",
  ISO and "N days ago"; a date without a time is approximate; anything else is refused. `tzdata`
  is a Windows-only dependency (zoneinfo has no database there).
- **Commands:** `apps` (bare `jobhunt apps` runs `apps list`; the framework gained
  `default_subcommand`), `apps export --csv|--xlsx`, `app show`, `app status` (any status, with
  `--note` and `--at`), `app update` (portal, sign-in email, check note, interviews, deadlines,
  recruiter, next step, follow-up date, outcome, notes), `dashboard [--no-open]`. `--human` gives
  aligned tables and a timeline.
- **Views** (`tracker/views.py`, `tracker/dashboard.py`), regenerated after `run`, `sync`,
  `mark`, `applied`, `app status|update` and a pre-fill confirmation; a failure is logged and
  never fails the command:
  - `~/.jobhunt/dashboard.html`: one self-contained page (summary tiles, needs attention, a
    filterable and sortable table, a detail dialog with the timeline, where to check and links to
    the folder's files). Light and dark, usable at phone width (checked in a browser at 375 px).
    Data is embedded as escaped JSON and rendered with `textContent`.
  - `~/.jobhunt/APPLICATIONS.md`: summary, needs attention, a table per status, newest first
    (snapshot-tested).
  - `~/.jobhunt/applications/<date>_<company>_<role>/`: README (every field and the timeline),
    `job_description.md` (never overwritten), `submitted_answers.md`, `confirmation.png`, and a
    copy of the resume.
  - Flags, never status changes: follow-up due (existing `followup_state`, or your date) and
    likely no response (`tracking.stale_after_days`, 21). Interviews on a closed application
    don't need attention.
- **Sheet "Applications" tab** (XLSX and Google Sheets): one row per application; Status, Next
  step and Notes sync back with the same three-way rule (your edits win; an edit there is also
  pushed to the Jobs tab in the same sync). Frozen header, filters, widths, a Status dropdown,
  status colors, overdue follow-ups in red, clickable "Where to check". On Sheets the rules are
  added once when jobhunt creates the tab, so your own changes to them stay.
- **Skills:** new `jobhunt-track` (record "I applied…", status news, "where do I check…", "what
  haven't I heard back from?", the dashboard; one-line confirmations; never reads
  `submitted_answers.md`). `jobhunt-daily` starts the digest with a three-line application
  summary and offers the dashboard. Eight skills now; `doctor` and `skills install` count them.
- **743 offline tests** (from 685), ruff, format and the personal-data scan clean at every
  commit.

Verified (2026-10-21, offline fixtures):

- Pre-fill (`intl-sponsorship`, headless browser on the fixture pages): the human's submit on a
  Greenhouse tab creates one record with the confirmation ID, a MyGreenhouse link, the resume's
  hash, and a folder with README, JD snapshot, answers, `confirmation.png` and the resume; the
  closed Lever tab gets none; seeing the confirmation again adds nothing.
- Fresh `newgrad-analyst` home: `jobhunt applied <Workday URL>` exits 6 asking for `jobhunt add`;
  after `add`, it records the Workday Candidate Home link and how to check. Setting Status to
  Interviewing in the Applications tab and syncing shows "Applied → Interviewing · you, in the
  sheet" in the dashboard timeline and `app show`. That run caught a bug the tests had missed (the
  first row of a tab created empty landed on row 3), now fixed with a regression test.
- Trigger evals, Claude Code: jobhunt-track 20/20 queries, 60 runs, no single miss. The other
  seven skills kept their earlier runs (`--resume`); they were not re-run with eight installed.

### Notes and decisions

- One record per job (`job_key`): applying twice to the same posting can't be recorded twice.
  Applying to another job at the same company is a new record.
- `jobhunt mark` keeps working and now creates a record too; `jobhunt applied` records more
  (time, method, portal).
- The sheet path uses the sync time, not the "Applied on" date, as the spec asks; the record is
  flagged approximate.
- What pre-fill records as sent is what jobhunt filled; you may have changed fields before
  submitting, and the file says so. Manual records list only essays you approved for that job.
- Lever and Ashby confirmation IDs aren't captured (no known ID on their confirmation pages).
- Not verified: a live MyGreenhouse or Workday sign-in (no account was used), the Sheets
  formatting against a real spreadsheet (fake client only), and Windows in real use.
