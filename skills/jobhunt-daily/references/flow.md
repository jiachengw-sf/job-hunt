# Daily flow details

## Envelope

`{ok, data, error: {code, message, details}, hints: {next_actions, user_actions}, page}`.
Exit codes: 0 ok, 2 bad args, 3 config/onboarding, 4 validation, 5 unreachable, 6 not found,
7 locked, 8 needs the human, 9 doctor failed, 70 bug. Large output goes to `page.out_file`.
Paged lists return `page.next_cursor`; pass it back as `--cursor`.

## Useful reads

- `jobhunt jobs --limit 10` — top jobs by priority (gated-out jobs show their reason with
  `--gated`).
- `jobhunt show <id>` — one job: gates with evidence, requirements with the profile line that
  backs each, band, priority inputs, signals.
- `jobhunt tasks` — pending AI tasks by kind, with each file and result path.
- `jobhunt essays list`, `jobhunt outreach due`, `jobhunt digest --human`.

## Statuses

New → Prefilled → Applied → OA → Interviewing → Offer / Rejected / Withdrawn / Skipped.
The user sets them in the tracker or with `jobhunt mark <id> <Status>` (Skipped needs
`--reason domain|too_senior|location|pay|other`). When the user says they applied somewhere by
hand, run `jobhunt mark <id> Applied` for them.

## Being brief

A good daily summary fits on one screen: 3–5 jobs to act on, the questions in one batch, and
the exact next command for each thing waiting on the user.
