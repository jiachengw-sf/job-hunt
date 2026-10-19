# outreach_draft result

```json
{
  "task_id": "outreach-4-referral-1a2b3c4d5e",
  "job_id": 4,
  "kind": "referral",
  "subject": "Software Engineering Intern at Northwind",
  "sentences": [
    {"text": "Hi Sam,", "source": null},
    {"text": "I'm applying for the Software Engineering Intern, Summer 2027 role at Northwind.",
     "source": "posting", "quote": "Software Engineering Intern, Summer 2027"},
    {"text": "I built a shuttle-tracking web app in Python and Flask.", "source": "projects[0].tech"},
    {"text": "Would you be open to referring me, or to a quick chat about the team?", "source": null},
    {"text": "Thanks either way!", "source": null, "new_paragraph": true}
  ]
}
```

- `kind` must match the task (`referral`, `coffee_chat` or `followup`).
- Greetings, asks and sign-offs have `source: null` and state no facts.
- The joined text must fit `max_chars` (600 by default).
- Use the recipient's name only if the user gave it in `recipient`.
