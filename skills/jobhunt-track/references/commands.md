# Tracking commands

Every command prints one JSON envelope:
`{ok, data, error: {code, message, details}, hints: {next_actions, user_actions}, page}`.
Relay `hints.user_actions` verbatim. Exit 6 means the job or application wasn't found (the
hint says how to add it); exit 2 is a bad flag or a time jobhunt couldn't read (ask the user
for a clearer time); exit 7 means another jobhunt run holds the lock (retry once after a
minute). Writers accept `--dry-run`.

## Ids

- Application ids come from `jobhunt apps` (`app_id`). The `app show`, `app status` and
  `app update` commands also take `job:<job id>`, a job key, or the posting URL.
- `jobhunt applied` takes a job id, job key or posting URL.

## Commands

- `jobhunt applied <job> [--at WHEN] [--method manual|referral|other] [--referral NAME]
  [--portal URL] [--check-method portal|email|none] [--login-email EMAIL]
  [--check-note TEXT] [--resume FILE] [--note TEXT] --via chat` — records once per job, sets
  Applied, snapshots the job description, and fills where to check from the ATS.
- `jobhunt apps [--status S]… [--attention]` — newest first, with a `summary`.
- `jobhunt app show <app>` — everything about one application, with its timeline. Submitted
  answers appear by label only.
- `jobhunt app status <app> <Status> [--note TEXT] [--at WHEN] [--reason R]` — any status, as
  the user; every change is kept in the timeline.
- `jobhunt app update <app> [--interview WHEN]… [--deadline WHEN]… [--label TEXT]
  [--recruiter-name N] [--recruiter-email E] [--next-step TEXT] [--follow-up-on YYYY-MM-DD]
  [--outcome TEXT] [--note TEXT] [--portal URL] [--login-email E] [--check-note TEXT]` —
  interview and deadline times may be in the future.
- `jobhunt apps export --csv|--xlsx [--to FILE]` — a spreadsheet of every application.
- `jobhunt dashboard` — regenerates `~/.jobhunt/dashboard.html` and `APPLICATIONS.md` and opens
  the dashboard (`--no-open` to just write them).

## Times

Stored in UTC, shown in the user's time zone (`tracking.timezone` in config, else the
computer's). `--at` reads "now", "today 3pm", "this afternoon", "yesterday 21:30", "monday",
"Oct 21 9:42pm", "2026-10-21 21:30" and "2 days ago". A date without a time is recorded as
approximate.

## What the views flag

- Follow-up due: 7 days after applying with no reply (at most 2 follow-ups), or the date the
  user set.
- Likely no response: 21 days in Applied with no update (`tracking.stale_after_days`). The
  status itself never changes without the user.
- Upcoming interviews and deadlines in the next week.
