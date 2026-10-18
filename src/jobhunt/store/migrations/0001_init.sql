-- jobhunt store, schema v1. SQLite is the source of truth; only the CLI writes it.
-- Timestamps are ISO-8601 UTC strings. *_json columns hold JSON text.

CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY,
    dedupe_key      TEXT NOT NULL UNIQUE,      -- "ats:board_token:job_id", else "url:<canonical url>"
    ats             TEXT,                      -- greenhouse | lever | ashby | workday | ... | unknown
    board_token     TEXT,
    ats_job_id      TEXT,
    canonical_url   TEXT,
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    locations_json  TEXT NOT NULL DEFAULT '[]',  -- one role, many locations: never split into rows
    work_mode       TEXT,
    employment_type TEXT,
    description     TEXT,                      -- plain text, HTML stripped
    description_hash TEXT,
    simhash         TEXT,                      -- for repost detection
    pay_json        TEXT,                      -- structured pay when the source exposes it
    posted_at       TEXT,                      -- from the source only; NULL if unknown. Never inferred.
    first_seen      TEXT NOT NULL,             -- when jobhunt first saw it
    last_seen       TEXT NOT NULL,
    last_checked    TEXT,
    live            INTEGER NOT NULL DEFAULT 1 CHECK (live IN (0, 1)),
    closed_at       TEXT,
    deadline        TEXT,
    raw_json        TEXT
);
CREATE INDEX jobs_company ON jobs(company);
CREATE INDEX jobs_live ON jobs(live);

-- Every URL a job is known by (Simplify apply link, direct ATS link, ...). Cross-source merges keep all.
CREATE TABLE job_urls (
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    url        TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (job_id, url)
);

-- Append-only event log. Triggers below forbid UPDATE and DELETE.
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER REFERENCES jobs(id),
    kind         TEXT NOT NULL CHECK (kind IN ('appeared', 'changed', 'disappeared', 'reappeared', 'status_changed', 'note')),
    at           TEXT NOT NULL,
    source_id    TEXT,
    run_id       INTEGER REFERENCES runs(id),
    details_json TEXT
);
CREATE INDEX events_job ON events(job_id);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TABLE sources (
    id             TEXT PRIMARY KEY,           -- e.g. "greenhouse:acme", "simplify:Summer2027-Internships"
    kind           TEXT NOT NULL,
    config_json    TEXT NOT NULL DEFAULT '{}',
    baseline_done  INTEGER NOT NULL DEFAULT 0,  -- silent first run: no alerts until the baseline exists
    etag           TEXT,
    last_modified  TEXT,
    last_fetch_at  TEXT,
    empty_runs     INTEGER NOT NULL DEFAULT 0   -- consecutive runs that returned nothing
);

CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    stats_json  TEXT                            -- gate drops by reason, class counts, read-back rates
);

CREATE TABLE source_yields (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    source_id   TEXT NOT NULL REFERENCES sources(id),
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    gated_out   INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    error_detail TEXT,
    PRIMARY KEY (run_id, source_id)
);

CREATE TABLE gate_results (
    job_id    INTEGER NOT NULL REFERENCES jobs(id),
    gate      TEXT NOT NULL,                   -- work_auth, sponsorship, grad_window, degree, yoe, ...
    result    TEXT NOT NULL CHECK (result IN ('pass', 'pass_unverified', 'unclear', 'fail')),
    evidence  TEXT,                            -- the matched posting text
    reason    TEXT,
    at        TEXT NOT NULL,
    PRIMARY KEY (job_id, gate)
);

CREATE TABLE requirements (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id),
    text        TEXT NOT NULL,                 -- verbatim quote from the posting
    importance  TEXT NOT NULL CHECK (importance IN ('must', 'preferred')),
    type        TEXT NOT NULL CHECK (type IN ('skill', 'experience', 'degree', 'eligibility', 'location', 'other')),
    status      TEXT CHECK (status IN ('met', 'partial', 'unmet')),
    tier        TEXT CHECK (tier IN ('stated', 'inferred', 'none')),
    evidence    TEXT,                          -- verified resume/profile line when tier = stated
    source      TEXT NOT NULL DEFAULT 'code' CHECK (source IN ('code', 'ai'))
);
CREATE INDEX requirements_job ON requirements(job_id);

CREATE TABLE fits (
    job_id           INTEGER PRIMARY KEY REFERENCES jobs(id),
    relevance        REAL,                     -- recall score; never shown as "fit"
    band             TEXT CHECK (band IN ('strong', 'good', 'stretch', 'not_a_fit')),
    preference_score REAL,
    priority         REAL,
    narrative_json   TEXT,
    disagreement     TEXT,                     -- AI flag shown to the user; never changes band
    computed_at      TEXT NOT NULL
);

CREATE TABLE companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    brief_json  TEXT,
    brief_at    TEXT
);

-- Applications and the tracker. Columns marked (user) are owned by the user: the agent sets them
-- only on creation and the sheet's values win on sync.
CREATE TABLE applications (
    job_id          INTEGER PRIMARY KEY REFERENCES jobs(id),
    class           TEXT CHECK (class IN ('ready', 'needs_answers', 'essay', 'manual')),
    form_spec_json  TEXT,
    essay_prompts_json TEXT,
    legitimacy_json TEXT,
    status          TEXT NOT NULL DEFAULT 'New',  -- (user) state machine in states.yml
    applied_on      TEXT,                          -- (user)
    referral_contact TEXT,                         -- (user)
    next_step       TEXT,                          -- (user)
    follow_up_on    TEXT,                          -- (user)
    outcome         TEXT,                          -- (user)
    notes           TEXT,                          -- (user)
    skip_reason     TEXT,
    updated_at      TEXT NOT NULL
);

-- Judgment tasks handed to the AI layer through ~/.jobhunt/work/.
CREATE TABLE tasks (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    job_id       INTEGER REFERENCES jobs(id),
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ingested', 'rejected', 'expired')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    ingested_at  TEXT
);
