-- Milestone 5: drafts the user edits, then pastes or sends themselves.

-- One row per essay field (kind essay, key = the form field id) or outreach message (kind
-- referral / coffee_chat / followup, key e.g. "followup-1"). jobhunt never sends anything:
-- `sent` is set only when the user says they sent it.
CREATE TABLE drafts (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id),
    kind            TEXT NOT NULL CHECK (kind IN ('essay', 'referral', 'coffee_chat', 'followup')),
    key             TEXT NOT NULL,
    prompt          TEXT,                   -- the essay prompt, or who the message is for
    max_length      INTEGER,
    status          TEXT NOT NULL CHECK (status IN ('draft', 'reviewed', 'approved', 'sent', 'discarded')),
    original        TEXT NOT NULL,          -- as drafted, before the review pass
    text            TEXT NOT NULL,          -- current draft (after review edits)
    provenance_json TEXT,                   -- sentences and the source each one cites
    review_json     TEXT,                   -- the {old, new, reason} edits applied
    approved_text   TEXT,                   -- the user's final version
    edit_ratio      REAL,                   -- 0 = approved unchanged, 1 = rewritten
    task_id         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    approved_at     TEXT,
    sent_at         TEXT,
    UNIQUE (job_id, kind, key)
);
CREATE INDEX drafts_job ON drafts(job_id);
