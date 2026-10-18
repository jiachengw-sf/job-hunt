-- Milestone 1: discovery. Per-source presence (closures by re-polling) and legitimacy signals.

-- Which sources list which jobs, with each source's own latest view of the posting. A job is
-- live while any source still lists it; closures come from a successful re-poll, never deletion.
CREATE TABLE source_jobs (
    source_id    TEXT NOT NULL REFERENCES sources(id),
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    present      INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)),
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    posting_json TEXT NOT NULL,
    PRIMARY KEY (source_id, job_id)
);
CREATE INDEX source_jobs_job ON source_jobs(job_id);

-- Legitimacy signals: shown as signals, never accusations. One current row per (job, kind).
CREATE TABLE signals (
    job_id       INTEGER NOT NULL REFERENCES jobs(id),
    kind         TEXT NOT NULL CHECK (kind IN ('old_posting', 'repost', 'evergreen_language', 'ai_instructions', 'missing_from_ats')),
    evidence     TEXT,                      -- the matched posting text, or the fact behind it
    details_json TEXT,
    at           TEXT NOT NULL,
    PRIMARY KEY (job_id, kind)
);

-- Cross-source merge: a Simplify row or a custom-domain link (?gh_jid=) may know the ATS job id
-- without the board token.
CREATE INDEX jobs_ats_job ON jobs(ats, ats_job_id);
