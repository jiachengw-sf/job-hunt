# The jobhunt CLI contract

Every `jobhunt` command prints exactly one JSON object on stdout:

```json
{"ok": true, "data": {...}, "error": null,
 "hints": {"next_actions": ["..."], "user_actions": ["..."]}, "page": null}
```

- `ok: false` comes with `error: {code, message, details}` and a non-zero exit code.
- `hints.next_actions`: what you (the AI) should do next.
- `hints.user_actions`: what only the human can do. Relay these verbatim.
- `page.next_cursor`: pass it as `--cursor` to get the next page.
- `page.out_file`: the output was too big and is in that file; read it from there.
- `--human` prints readable text (for showing the user), `--dry-run` previews any command that
  writes, `--help` explains any command, `jobhunt schema <command>` gives its JSON schemas.
- Commands never prompt. Ones that need the user (the setup form, the pre-fill browser) return
  at once with a URL or status and offer `--status` to poll.

| Exit | Meaning |
|---|---|
| 0 | success |
| 2 | bad flags or arguments |
| 3 | missing or invalid config, or onboarding not complete |
| 4 | an input or result file failed validation (fix it and retry) |
| 5 | a source or service could not be reached |
| 6 | the named job, task or file doesn't exist |
| 7 | another jobhunt process holds the lock |
| 8 | stopped on something only the human can do |
| 9 | `doctor` found failing checks |
| 70 | a bug in jobhunt: report it |

All data lives in `~/.jobhunt/` (or `$JOBHUNT_HOME`). Only the CLI writes it: never edit the
database or the tracker's agent columns yourself.
