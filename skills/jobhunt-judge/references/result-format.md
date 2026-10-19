# requirement_evidence result

Write to the task's `result_path` (`~/.jobhunt/work/results/<task_id>.json`):

```json
{
  "task_id": "req-12-ab12cd34ef",
  "job_id": 12,
  "requirements": [
    {"id": 0, "text": "3+ years of Go", "status": "met", "tier": "stated",
     "evidence_ref": "experience[0].bullets[0]",
     "evidence_quote": "Lead a team of 4 engineers owning the shipment-tracking API (Go, PostgreSQL, Kafka)"},
    {"id": 1, "text": "Experience with Kubernetes", "status": "partial", "tier": "inferred",
     "note": "Runs services on AWS ECS, a related container platform"},
    {"id": 2, "text": "Rust in production", "status": "unmet", "tier": "none"}
  ],
  "gates": [{"gate": "work_mode", "result": "pass", "quote": "fully remote within the US"}],
  "dealbreakers": [{"dealbreaker": "Required on-site presence", "hit": false}],
  "narrative": {
    "reasons": [{"text": "Owns a Go API at scale", "evidence_ref": "experience[0].bullets[0]",
                 "evidence_quote": "Lead a team of 4 engineers owning the shipment-tracking API (Go, PostgreSQL, Kafka)"}],
    "gaps": ["No Rust"],
    "pitch": "Go and Kafka at high throughput, with zero-downtime migrations.",
    "why_wrong": "The Rust requirement may matter more than its wording suggests."
  },
  "band_disagreement": null
}
```

Field rules (ingest enforces all of them):

| Field | Rule |
|---|---|
| `requirements[].id` | a task requirement id; `null` only when `extract_requirements` is true |
| `requirements[].text` | verbatim from the posting |
| `status` / `tier` | `met`/`partial` need `stated` or `inferred`; `none` means `unmet`; `stated` can't be `unmet` |
| `evidence_ref` | a `ref` from `profile_lines` |
| `evidence_quote` | that line's exact text (or a verbatim part of it) |
| stated skill match | the cited line must name the skill (otherwise use `inferred`) |
| `note` | required for `inferred` |
| `gates[]` | one per `unclear_gates` item; `quote` verbatim from the posting |
| `dealbreakers[]` | one per task dealbreaker; `hit: true` needs a posting `quote` |

Quotes are compared after normalizing case, whitespace, curly quotes and dashes, and nothing
else. Shorter exact quotes are safer than long ones.
