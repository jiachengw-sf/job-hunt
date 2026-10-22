-- Application tracking: one record per job you actually applied to (a job you haven't applied to
-- has none). The posting's details are copied in at apply time, because postings change and get
-- taken down. Status, referral contact, next step, follow-up date, outcome and notes stay in
-- `applications` (the tracker's user-owned columns); the status history is the append-only
-- `status_changed` events. No passwords or one-time codes are ever stored.
CREATE TABLE application_records (
    id                  INTEGER PRIMARY KEY,           -- the app id
    job_id              INTEGER NOT NULL UNIQUE REFERENCES jobs(id),  -- never two per job
    job_key             TEXT NOT NULL,                 -- jobs.dedupe_key
    -- what (as posted when you applied)
    company             TEXT NOT NULL,
    title               TEXT NOT NULL,
    team                TEXT,
    locations_json      TEXT NOT NULL DEFAULT '[]',
    work_mode           TEXT,
    employment_type     TEXT,
    term                TEXT,
    pay_json            TEXT,
    ats                 TEXT,
    ats_job_id          TEXT,
    posting_url         TEXT,
    -- when (UTC, ISO 8601; shown in your local time zone)
    applied_at          TEXT NOT NULL,
    applied_at_approx   INTEGER NOT NULL DEFAULT 0 CHECK (applied_at_approx IN (0, 1)),
    -- how
    method              TEXT NOT NULL CHECK (method IN ('prefilled', 'manual', 'referral', 'other')),
    recorded_via        TEXT NOT NULL CHECK (recorded_via IN ('prefill', 'cli', 'sheet', 'chat')),
    resume_file         TEXT,
    resume_sha256       TEXT,
    submitted_json      TEXT NOT NULL DEFAULT '[]',    -- labels and kinds only, never values
    -- proof
    confirmation_id     TEXT,
    screenshot_path     TEXT,
    jd_snapshot_path    TEXT,
    folder              TEXT,
    -- where to check status
    status_check_url    TEXT,
    status_check_method TEXT NOT NULL DEFAULT 'email'
                        CHECK (status_check_method IN ('portal', 'email', 'none')),
    login_email         TEXT,
    status_check_note   TEXT,
    -- contacts you gave
    recruiter_name      TEXT,
    recruiter_email     TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Interview and deadline dates for an application (UTC).
CREATE TABLE application_dates (
    id       INTEGER PRIMARY KEY,
    app_id   INTEGER NOT NULL REFERENCES application_records(id),
    kind     TEXT NOT NULL CHECK (kind IN ('interview', 'deadline')),
    at       TEXT NOT NULL,
    label    TEXT,
    UNIQUE (app_id, kind, at)
);
CREATE INDEX application_dates_at ON application_dates(at);
