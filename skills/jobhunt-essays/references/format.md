# Essay task results

## essay_draft

```json
{
  "task_id": "essay-2-0a1b2c3d4e",
  "job_id": 2,
  "essays": [
    {"field_id": "question_100205",
     "sentences": [
       {"text": "Northwind's warehouse robots are what drew me to this role.",
        "source": "posting", "quote": "robots that sort parcels in our warehouses"},
       {"text": "I built a web app that shows live campus shuttle arrivals from a public feed.",
        "source": "projects[0].bullets[0]"},
       {"text": "That is the kind of real-time problem I want to keep working on.",
        "source": null, "new_paragraph": false}
     ]}
  ]
}
```

- `source`: a `ref` from the task's `sources`, or `"posting"` with an exact `quote`.
- `source: null` only for connective sentences: no numbers, no skills or tools, no roles.
- A sentence may not contain a number, a known skill, or a leadership word (led, managed,
  founded, mentored...) that its source lacks.
- Draft every required prompt. `new_paragraph: true` starts a paragraph.

## essay_review

```json
{"task_id": "review-2-5f6a7b8c9d", "job_id": 2,
 "edits": [{"field_id": "question_100205", "old": "drew me to this role",
            "new": "made me want to apply", "reason": "plainer"}]}
```

- `old` must appear exactly once in that draft's current text. Make it longer if it repeats.
- `new` may not add numbers, skills or leadership claims that `old` and the draft's sources
  don't back.
- Edits apply in order. Empty `edits` is fine. One review round only.

Drafts land in `~/.jobhunt/work/essays/<job>/<field>.txt`. If the user already edited a file,
the review is written beside it as `<field>.reviewed.txt` instead of overwriting their work.
