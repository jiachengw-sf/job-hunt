-- Milestone 5: ingest of judgment results.

-- What an ingested judgment task decided that gates can't recompute: an unclear gate resolved
-- from the posting (pass/fail) or a dealbreaker checked (hit/clear). Each carries the posting
-- quote ingest verified. Cleared when the posting or profile changes (tasks.basis_hash).
CREATE TABLE judgments (
    job_id   INTEGER NOT NULL REFERENCES jobs(id),
    kind     TEXT NOT NULL CHECK (kind IN ('gate', 'dealbreaker')),
    key      TEXT NOT NULL,
    result   TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'hit', 'clear')),
    quote    TEXT,
    task_id  TEXT NOT NULL,
    at       TEXT NOT NULL,
    PRIMARY KEY (job_id, kind, key)
);

-- The posting + profile a task was built from. An ingested result stays in force while the
-- job's current basis matches; when it changes, the AI rows are dropped and a new task written.
ALTER TABLE tasks ADD COLUMN basis_hash TEXT;

-- Why the AI judged an `inferred` requirement the way it did (shown with the requirement).
ALTER TABLE requirements ADD COLUMN note TEXT;
