---
name: jobhunt-setup
description: Set up jobhunt, the local job-hunt agent, for a new user, or fix their setup. Use when the user wants to start using jobhunt, onboard, add or replace a resume, change job preferences or screening answers, connect the tracker spreadsheet, turn on notifications, or schedule automatic runs. Not for the daily job review (jobhunt-daily) or for filling applications (jobhunt-apply).
license: MIT
compatibility: Needs the jobhunt CLI on PATH (uv tool install or pipx install from a jobhunt checkout).
---

# jobhunt setup

Walk the user through onboarding in about 15 minutes. The `jobhunt` CLI does all the work; you
run commands, read their JSON, explain results in plain words, and ask the user only what the CLI
can't know. Read [the CLI contract](references/cli-contract.md) once before starting.

## Ground rules

- Every command prints one JSON envelope. Check `ok`. Relay every `hints.user_actions` item to
  the user **verbatim**: only the human can do those. Follow `hints.next_actions` yourself.
- **Sensitive answers never go through chat**: work authorization, visa or sponsorship,
  citizenship, salary, EEO/demographics, disability, veteran status, background checks. They are
  entered only in the local form (`jobhunt setup form`). If the user starts typing one to you,
  stop them politely and point to the form.
- Never invent profile facts. Tidy what the resume says; ask the user about anything unclear.
- Don't run `jobhunt prefill` here, and never submit anything.

## Steps

1. **Check the machine.** `jobhunt doctor --fix`. Read each non-ok check and its `fix`. Fix what
   you can by running commands; relay the rest. Exit code 9 means checks failed: keep going only
   with problems that don't block the next step (a missing browser only blocks pre-fill).
2. **Resume → profile.** Ask for the resume file path. Run `jobhunt resume add <path>`. It writes
   a redacted text and a draft profile (no contact details). Open the draft
   (`jobhunt profile show`), and tidy it with `jobhunt profile set <path> <value>` only where the
   resume clearly says so: split merged bullets, fix dates, fill skills the resume lists. Show the
   user the result and ask them to correct anything wrong. When they agree, run
   `jobhunt profile confirm`. See [profile tips](references/profile.md).
3. **Preferences and screening answers.** Run `jobhunt setup form`. It returns a local URL at
   once. Give the user the URL; they fill roles, locations, pay, dealbreakers and the screening
   questions there. Poll `jobhunt setup form --status` every minute or so until it says done (or
   the user says they finished). Don't ask them to read their answers back to you.
4. **Watchlist (optional).** Ask for companies they especially want. Add each to
   `~/.jobhunt/watchlist.yaml` (`companies: [{name, careers_url}]`; jobhunt finds the ATS from
   the URL). Single postings can be added any time with `jobhunt add <url>`.
5. **First run and coverage.** `jobhunt run` (no AI needed; the first run per source is a silent
   baseline). Then `jobhunt coverage` and explain: how many required form fields their answers
   cover, the most common missing questions, and that pre-fill works on Greenhouse, Lever and
   Ashby only. Other ATSs get a hand-off packet.
6. **Tracker.** By default the tracker is `~/.jobhunt/tracker.xlsx`. For Google Sheets, follow
   [tracker setup](references/tracker.md). Run `jobhunt sync` and tell them which columns are
   theirs (Status, Applied on, Referral contact, Next step, Follow-up on, Outcome, Notes): their
   edits there always win.
7. **Notifications (optional).** See [notifications](references/notifications.md). After they
   edit `config.yaml`, run `jobhunt notify test` and relay the result.
8. **Schedule.** Offer to run discovery automatically every 4 hours:
   `jobhunt schedule install --dry-run` shows the exact entry; after the user says yes, run
   `jobhunt schedule install` and then `jobhunt schedule status`.
9. **Finish.** Run `jobhunt doctor` again and summarize: what's set up, what's left, and how to
   use it daily ("say *run my job hunt*").

## If something fails

- Exit 3: config or onboarding is incomplete. The error names the file and field.
- Exit 7: another jobhunt process holds the lock. Wait and retry once.
- Exit 5: a source or service was unreachable. Retry later; never work around it.
