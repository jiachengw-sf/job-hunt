# Contributing to jobhunt

Thanks for helping. The most useful contributions are **new job sources** and **ATS adapters**,
Workday first. This guide covers setup, the rules every change keeps, and step-by-step recipes
for both. [AGENTS.md](AGENTS.md) has the full module map and is also what AI coding agents read.

## Setup

```bash
git clone https://github.com/OWNER/jobhunt.git
cd jobhunt
uv sync
uv run playwright install chromium
```

Every change must pass these, the same checks CI runs:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python -m jobhunt.devtools.pii_scan
```

The tests are offline. They run against synthetic fixtures and four synthetic personas in
`tests/personas/`. The browser tests skip when no Chromium is installed; install it, because
adapter work needs them. To try the CLI without touching your own data:

```bash
export JOBHUNT_HOME=$(mktemp -d)
jobhunt doctor --fix --offline --human
```

## Rules every change keeps

These come from the project's non-negotiables. Read the list in [AGENTS.md](AGENTS.md); a PR that
breaks one won't be merged.

- **Nothing submits or sends.** There is no submit or send command and there won't be. Adapters
  click only through `prefill/guard.py:safe_click`, and a test fails on any other click, key
  press or submit call in `prefill/`.
- **No logins, account creation, CAPTCHA solving, stealth plugins or proxies.**
- **Public, documented sources only.** No LinkedIn or Indeed scraping or automation. All traffic
  goes through `jobhunt.http.HttpClient`, which throttles per host, sends conditional GETs and
  backs off on 429 and 5xx.
- **Never guess.** A posting date comes from the source or stays null. A field that can't be
  found, matched or read back is highlighted for the user, never filled with a best guess.
- **No real personal data, anywhere.** Fixtures are synthetic: companies, people, boards and
  questions. Emails use `example.com`, `example.org` or `example.net`. Phone numbers are
  `NXX-555-01XX`. The personal-data scan fails CI otherwise. You can add your own name and email
  to `.pii-denylist` (gitignored) so the scan catches them too.
- **Anything that works for only one persona is a bug.** Tests that touch fit, forms or pre-fill
  run over all four personas.
- **One place for each concern.** Paths live in `paths.py`, config in `config.py`, locking in
  `locking.py`. Don't add parallel copies.
- **CLI contract.** New commands use `@agent_command(cli, "name", output=Model, writes=...)` and
  return a `Result`. The contract tests check every registered command automatically: one JSON
  envelope, exit codes, `--help`, `--dry-run` on writers, the 8,000-character cap.
- **Schema changes** add `src/jobhunt/store/migrations/NNNN_name.sql`. Never edit a shipped
  migration.

## Adding a job source

A source turns one public feed into `Posting`s. Discovery handles dedupe, closures, events and
yields for you.

1. **Check the feed.** It must be public and documented (or a public file such as Simplify's
   `listings.json`), and readable without signing in. Note which field is the posting date. If
   there isn't one, `posted_at` stays `None`.
2. **Write the class** in `src/jobhunt/discovery/sources.py`:
   - subclass `Source` and set `kind` and `priority` (direct ATS data outranks aggregators when
     postings merge)
   - `url()` returns the feed URL; `parse(data)` returns `Posting`s with `ats`, `board_token` and
     `ats_job_id` filled whenever the posting lives on a known ATS, so the dedupe key is
     `ats:board:job_id`
   - override `fetch()` only if the feed needs more than one request (per-job pages, paging).
     Return `complete=False` when you couldn't read everything, so nothing is wrongly closed.
   - add it to `SOURCE_KINDS`, and to `plan_sources` in `discovery/pipeline.py` if watchlist
     companies can use it
3. **Teach the resolver** (`discovery/resolver.py`) its URL shapes, if it's an ATS. That lets
   `jobhunt add <url>` and watchlist careers URLs find it.
4. **Fixtures.** Add a synthetic company to `tests/fixtures/sources/make_fixtures.py`, in both the
   `base` and `update` snapshots (with at least one closure and one change), and regenerate:
   `uv run python tests/fixtures/sources/make_fixtures.py`. `FixtureTransport` serves
   `<dir>/<host>/<path>`, so the file path mirrors the real URL.
5. **Tests** in `tests/test_discovery.py`: parsing (including the date and pay fields), the dedupe
   key, a second fetch creating no duplicates, closures, and a 304 on a conditional re-fetch.
6. **Live check.** Run `jobhunt fetch --source <id>` once against the real feed with a throwaway
   `JOBHUNT_HOME`, and put the yields in your PR description.

## Adding an ATS adapter

Supporting an ATS end to end takes three pieces. Each is useful alone: a form reader without
pre-fill still classifies jobs (Ready, Needs answers, Essay, Manual) and powers hand-off packets.

### 1. Form reader (`src/jobhunt/forms/<ats>.py`)

Read the application form into a `FormSpec` (`forms/model.py`): every field's label, type,
options, limits and whether it's required. Use the cheapest read that works: a JSON API
(Greenhouse), server-rendered HTML (Lever), or a headless page load that only reads (Ashby).
The reader never clicks, types or posts. Dispatch to it from `forms/inspect.py`.

### 2. Pre-fill adapter (`src/jobhunt/prefill/adapters/<ats>.py`)

Subclass `Adapter` (`prefill/adapters/base.py`) and register it in `ADAPTERS`:

- `ats`: the resolver's name for it
- `allow`: the non-GET endpoints a fill needs (resume upload, autocomplete). The route guard
  blocks every other non-GET request while filling.
- `submit`: the endpoints that submit. They are always blocked during the fill, even if `allow`
  matches.
- `ready(page)`, `set_field`, `read_field`, `anchor` (where to draw the highlight) and
  `confirmed(page)` (the confirmation page after the human submits)
- `upload_first = True` if the ATS parses the resume and overwrites fields

Values come only from `answers.json`, which code builds and validates before a tab opens. Each
field ends with a `FieldOutcome` status. Envelopes carry field ids, labels and statuses, never
values.

### 3. Fixtures and tests

- Add synthetic forms to `tests/fixtures/forms/make_forms.py`, using all three templates: A
  (standard questions, so Ready), B (plus a required essay) and C (plus an unknown question and
  a new wording). Add a page per job under `pages/`, and a `js/<ats>.js` that copies the ATS's
  widgets and quirks (custom dropdowns, typeaheads, upload parsing). Add the endpoint responses
  the page calls as `<path>.POST.json`. Regenerate with
  `uv run python tests/fixtures/forms/make_forms.py`.
- Add your ATS to the parametrized guard tests in `tests/test_prefill_guard.py`. They prove the
  submit controls are refused, no submit request leaves during the fill, and the human can still
  submit afterwards.
- Add it to `tests/test_prefill_adapters.py`, which fills every fixture form for every persona,
  reads every value back and checks it stopped before Submit. Also extend `tests/test_forms.py`
  for the reader.
- **Live testing never clicks a submit control or lets a submit request through.** Fill a real
  posting, check the read-back, close the tab.

### Workday: what's known

Workday is the biggest gap. About a third of the rows in Simplify's internship list point to
`*.myworkdayjobs.com`. The resolver already recognizes it, so those jobs are Manual and get
hand-off packets. Questions to settle in an issue before writing code:

- **Discovery.** Career sites load jobs from a JSON endpoint per tenant and site. It's public, but
  is it documented enough for the "public, documented sources" rule? Until that's agreed, Workday
  jobs arrive through Simplify and `jobhunt add <url>`.
- **Accounts.** Most Workday tenants make candidates sign in or create an account before applying.
  jobhunt never logs in or creates accounts. An adapter would have to open the tab, let the
  user sign in themselves, and start filling only after that. Design this hand-over explicitly.
- **Multi-page wizard.** Applications span several steps (My Information, My Experience,
  Application Questions, Voluntary Disclosures, Review). Moving between steps uses "Save and
  Continue" buttons, and the guard must tell those apart from the final Submit, both by control
  and by endpoint.
- **Resume parsing** fills My Experience after upload, so this is an `upload_first` ATS, and
  parsed values must be read back and flagged `autofilled`.
- **Tenant variation.** Field ids and question sets differ per employer, so match on labels the
  way the other adapters do, and expect `not_found` to be common at first.

## Pull requests

- Keep commits small and logical: one feature or fix plus its tests. Use conventional-style
  messages (`feat(discovery): ...`, `fix(prefill): ...`, `docs: ...`, `test: ...`, `chore: ...`).
- Every commit passes the four checks above.
- Update `PROGRESS.md` when you finish something a user would notice, and `AGENTS.md` when the
  module layout or a rule changes.
- Describe how you tested it, including any live run, with yields or read-back rates, never with
  personal data.
