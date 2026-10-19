# What the user reviews in each tab

`jobhunt prefill --status` returns the batch `status` (`starting`, `filling`,
`waiting_for_you`, `done`, `stopped`) and one entry per job. Each job's `review` has:

- `status`: `ready_for_review` (filled; the user's turn), `invalid` (answers.json failed
  validation; see `problems`) or `failed` (see `error`).
- `filled` of `to_fill`: fields jobhunt set and read back equal.
- `needs_you`: fields outlined on the page, each with `label`, `required` and `status`:
  - `left`: yours to answer (attestations, consents, unreviewed EEO, unmatched questions,
    essays not approved yet)
  - `recommend`: tick it yourself (Lever radios and checkboxes; the value is shown on the page)
  - `mismatch`: typed but the page shows something else: check it
  - `not_found`, `no_option`, `error`, `refused`: jobhunt couldn't set it: fill it yourself
  - `file_rejected`: the form doesn't take the resume's file type
  - `autofilled`: the ATS filled it from the resume: check it
- `blocked_requests`: requests the submit guard held back while filling (normal).
- `screenshot`: a picture of the filled form.

A job whose tab the user submitted turns `applied`; jobhunt saw the confirmation and marked it
Applied with the date. If a confirmation is missed, `jobhunt mark <id> Applied` fixes it.
Forms with a CAPTCHA on Submit: the user solves it; jobhunt never touches it.
