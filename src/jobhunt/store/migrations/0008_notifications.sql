-- Milestone 5: digests and fast alerts, one row per channel attempt. The last digest's time is
-- where the next digest starts; an alert is sent at most once per job.
CREATE TABLE notifications (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL CHECK (kind IN ('digest', 'alert')),
    job_id   INTEGER REFERENCES jobs(id),
    channel  TEXT NOT NULL,               -- terminal | ntfy | discord | smtp
    status   TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    error    TEXT,
    at       TEXT NOT NULL
);
CREATE INDEX notifications_kind_at ON notifications(kind, at);
