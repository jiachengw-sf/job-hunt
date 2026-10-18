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

## Milestone 1 — Discovery (next)

Sources (Simplify listings on `dev`, Greenhouse, Lever, Ashby), ATS resolver, `jobhunt add <url>`,
freshness fields, dedupe key, silent first run, conditional GETs + per-host throttle + Retry-After
(extend `http.py`), closures by re-polling, event log, legitimacy signals, per-source yields, offline
fixtures per persona. Confirm the Simplify `listings.json` path and schema live before coding.
