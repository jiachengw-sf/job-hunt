-- Milestone 3: application forms, classification and ATS posting views.

-- One cached form spec per job, read from the ATS (Greenhouse questions API, Lever /apply page,
-- Ashby hosted page). class is the application class computed from the spec and the answer bank.
CREATE TABLE form_specs (
    job_id        INTEGER PRIMARY KEY REFERENCES jobs(id),
    ats           TEXT NOT NULL,
    url           TEXT,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'unsupported', 'error', 'needs_browser', 'not_found')),
    error         TEXT,
    spec_json     TEXT,                   -- FormSpec: every field's label, type, required, options
    fetched_at    TEXT NOT NULL,
    class         TEXT CHECK (class IN ('ready', 'needs_answers', 'essay', 'manual')),
    details_json  TEXT,                   -- per-field resolution, reasons, always-human fields
    classified_at TEXT
);

-- The ATS's own description of a job, fetched while reading its form. A view, not a source: it
-- never counts as "listed", so it can't keep a job live or close it. Jobs whose sources carry no
-- posting text (Simplify) take their description from here.
CREATE TABLE posting_views (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs(id),
    ats         TEXT NOT NULL,
    url         TEXT,
    description TEXT,
    pay_json    TEXT,
    fetched_at  TEXT NOT NULL
);
