# Test personas

Synthetic users. All development, CI, fixtures and demos use these; nothing here is a real person.
Anything that works for only one persona is a bug.

Rules for persona data (enforced by the personal-data scan in CI):

- emails use `example.com` / `.example` / `.test` domains
- phone numbers use the reserved `NXX-555-01XX` range (555 is the exchange)
- schools, employers and addresses are fictional

| Persona | Exercises |
|---|---|
| `first-year-intern` | class-year gates, Simplify internship feed, short resume |
| `intl-sponsorship` | work-authorization and sponsorship gates, "pass — unverified" handling, two countries |
| `newgrad-analyst` | graduation timing, entry-level filters, full-time, pay floor |
| `senior-backend` | years-of-experience gates, level mismatch, remote-only, no Simplify feed |

Each folder holds `resume.md` (the source document), plus `profile.yaml`, `preferences.yaml` and
`answers.yaml` as onboarding would produce them. To use a persona as a data home:

```bash
JOBHUNT_HOME=$(mktemp -d) && cp tests/personas/senior-backend/*.yaml "$JOBHUNT_HOME" && jobhunt doctor --fix --offline
```
