# Tidying the draft profile

`jobhunt resume add` drafts `profile.yaml` from the resume. The draft has no name, email, phone
or address: those go only to the local form. School names are replaced with `[school]` in
anything the AI reads.

Fix only what the resume supports:

- One achievement per bullet. Keep the user's numbers exactly; never add one.
- Dates as `YYYY-MM`; `end: null` for a current role.
- `skills.*` lists only what the resume names (languages, tools, data, cloud...).
- `education[].graduation` as `YYYY-MM`. Class standing goes in the form, not here.
- `headline`: one plain line the user agrees with.

Use `jobhunt profile set 'experience[0].bullets[1]' "Text"` for each change, then show the
result with `jobhunt profile show --human` and let the user approve before
`jobhunt profile confirm`. If confirm fails (exit 4), its errors name the field to fix.
