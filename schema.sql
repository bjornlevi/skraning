-- All timestamps are stored as UTC text: "YYYY-MM-DD HH:MM:SS".
-- This is the latest version. When changing it, bump user_version below and
-- SCHEMA_VERSION in db.py, and add the upgrade statements to MIGRATIONS there.
PRAGMA user_version = 3;

CREATE TABLE IF NOT EXISTS events (
    id                      INTEGER PRIMARY KEY,
    slug                    TEXT    NOT NULL UNIQUE,
    name                    TEXT    NOT NULL,
    description             TEXT    NOT NULL DEFAULT '',
    image                   TEXT,
    starts_at               TEXT    NOT NULL,
    ends_at                 TEXT    NOT NULL,
    location                TEXT    NOT NULL DEFAULT '',
    organizer_name          TEXT    NOT NULL,
    organizer_email         TEXT    NOT NULL,
    admin_code_hash         TEXT    NOT NULL UNIQUE,
    status                  TEXT    NOT NULL DEFAULT 'pending',  -- pending | published | cancelled
    listed                  INTEGER NOT NULL DEFAULT 1,
    registration_opens_at   TEXT,
    registration_closes_at  TEXT,
    max_per_person          INTEGER,                     -- NULL = no limit (overlaps are never allowed)
    created_at              TEXT    NOT NULL,
    verified_at             TEXT
);

CREATE TABLE IF NOT EXISTS queues (
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    image        TEXT,
    capacity     INTEGER,            -- NULL = unlimited
    starts_at    TEXT    NOT NULL,   -- within the event's start/end
    ends_at      TEXT    NOT NULL,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    is_open      INTEGER NOT NULL DEFAULT 1,
    -- Optional game runner, who gets their own code to edit this game
    runner_name              TEXT,
    runner_email             TEXT,
    runner_code_hash         TEXT,
    runner_reminder_sent_at  TEXT
);
CREATE INDEX IF NOT EXISTS queues_event ON queues(event_id);
CREATE UNIQUE INDEX IF NOT EXISTS queues_runner_code ON queues(runner_code_hash);

CREATE TABLE IF NOT EXISTS registrations (
    id                   INTEGER PRIMARY KEY,
    queue_id             INTEGER NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
    event_id             INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name                 TEXT    NOT NULL,
    email                TEXT    NOT NULL,
    access_code_hash     TEXT    NOT NULL UNIQUE,
    created_at           TEXT    NOT NULL,
    verified_at          TEXT,       -- NULL until the emailed link is used
    cancelled_at         TEXT,
    last_notified_state  TEXT,       -- 'confirmed' | 'waitlisted'
    reminder_sent_at     TEXT,
    paid_at              TEXT        -- set by the organizer: payment received
);
CREATE INDEX IF NOT EXISTS registrations_queue ON registrations(queue_id);
CREATE INDEX IF NOT EXISTS registrations_event_email ON registrations(event_id, email);

-- Simple sliding-window rate limiting (e.g. emails triggered per IP).
CREATE TABLE IF NOT EXISTS rate_hits (
    key         TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_hits_key ON rate_hits(key, created_at);
