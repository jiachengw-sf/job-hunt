-- Milestone 2: fit engine and tracker.

-- fits: one row per job, recomputed by `jobhunt match`. relevance is the recall score and is
-- never shown as "fit". band is computed by code; provisional = some evidence is not judged yet.
ALTER TABLE fits ADD COLUMN gate_status TEXT CHECK (gate_status IN ('passed', 'unclear', 'failed'));
ALTER TABLE fits ADD COLUMN gate_reasons_json TEXT;     -- failing / unclear gates with reasons
ALTER TABLE fits ADD COLUMN recall_rank INTEGER;         -- 1 = most relevant; NULL = not ranked
ALTER TABLE fits ADD COLUMN recalled INTEGER NOT NULL DEFAULT 0 CHECK (recalled IN (0, 1));
ALTER TABLE fits ADD COLUMN provisional INTEGER NOT NULL DEFAULT 1 CHECK (provisional IN (0, 1));
ALTER TABLE fits ADD COLUMN qualification_json TEXT;     -- score, coverage, rules applied
ALTER TABLE fits ADD COLUMN preference_json TEXT;        -- per-component preference scores
ALTER TABLE fits ADD COLUMN priority_json TEXT;          -- per-component priority inputs
ALTER TABLE fits ADD COLUMN flags_json TEXT;             -- volume guard, pay missing, ...

-- requirements: order within the posting, the section it came from, and the profile line that
-- backs a `stated` match (ingest verifies the text against that line).
ALTER TABLE requirements ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE requirements ADD COLUMN section TEXT;
ALTER TABLE requirements ADD COLUMN evidence_ref TEXT;   -- e.g. "experience[0].bullets[1]"

-- tasks: where the task file lives and what it was built from (no duplicate pending tasks).
ALTER TABLE tasks ADD COLUMN input_hash TEXT;
ALTER TABLE tasks ADD COLUMN path TEXT;
CREATE INDEX tasks_job_kind ON tasks(job_id, kind);

-- Tracker sync: the last value pushed to / pulled from the sheet for each user-owned cell. A
-- three-way compare (sheet, store, last synced) tells a sheet edit from a `jobhunt mark`.
CREATE TABLE tracker_sync (
    job_id    INTEGER NOT NULL REFERENCES jobs(id),
    col       TEXT NOT NULL,
    value     TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (job_id, col)
);

-- Cached embeddings for recall (only with the optional [recall] extra).
CREATE TABLE embeddings (
    job_id    INTEGER NOT NULL REFERENCES jobs(id),
    model     TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    vector    BLOB NOT NULL,              -- float32 little-endian
    PRIMARY KEY (job_id, model)
);
