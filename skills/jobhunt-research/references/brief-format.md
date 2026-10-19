# company_research result

```json
{
  "task_id": "research-bluefin-analytics-1a2b3c4d",
  "company": "Bluefin Analytics",
  "searches_used": 3,
  "fetches_used": 4,
  "subagents_used": false,
  "sources": ["https://bluefin.example/about", "https://news.example/2026/bluefin-series-b"],
  "what_they_do": [{"text": "Churn forecasting for subscription businesses.",
                    "url": "https://bluefin.example/about",
                    "quote": "builds churn forecasting software for subscription businesses"}],
  "stage_funding": [{"text": "Series B, $40M (2026).", "url": "https://news.example/2026/bluefin-series-b",
                     "quote": "raised a $40 million Series B led by Harbor Ventures", "date": "2026-03"}],
  "size": [], "recent_news": [], "engineering": [], "early_career": [],
  "interview_process": [], "red_flags": [],
  "user_angle": [{"text": "Python and SQL, as the team uses.", "evidence_ref": "skills.languages",
                  "evidence_quote": "Languages: Python, Java, HTML/CSS, SQL",
                  "fact_url": "https://bluefin.example/about"}],
  "not_found": ["interview_process"]
}
```

| Field | Max facts |
|---|---|
| what_they_do (required) | 3 |
| stage_funding | 3 |
| size | 2 |
| recent_news (each needs `date` YYYY-MM or YYYY-MM-DD) | 5 |
| engineering, red_flags | 5 |
| early_career, interview_process | 3 |
| user_angle | 3 |

Ingest rejects: over budget, `subagents_used: true`, a fact URL missing from `sources`, a quote
over 25 words or not on its page, a news item without a date, a `user_angle` quote that isn't
the profile line. Facts from pages that no longer load are dropped from the brief, not kept.
