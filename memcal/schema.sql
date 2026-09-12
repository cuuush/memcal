-- memcal schema. Everything structured lives here; the wiki lives on disk as markdown.
-- Recency is resolved at write time (thesis 3): current values sit in the main tables,
-- superseded values move to *_history. Nothing is ever weighed at read time.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------- memcal ----
-- Instances only. Recurring things get a wiki page; two poker games are two rows
-- both pointing at series='poker-night'.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    key          TEXT UNIQUE NOT NULL,   -- dedup key: <series-or-title-slug>@<date>
    date         TEXT NOT NULL,          -- ISO yyyy-mm-dd; the day it starts
    until        TEXT,                   -- ISO yyyy-mm-dd; last day, for spans
    time         TEXT,                   -- free text: "~8pm", "19:30", NULL
    kind         TEXT NOT NULL,          -- commitment | availability | opportunity | observed
    subject      TEXT NOT NULL DEFAULT 'me',   -- who it is about
    title        TEXT NOT NULL,
    location     TEXT,
    status       TEXT NOT NULL DEFAULT 'mentioned',  -- mentioned|tentative|confirmed|declined|happened
    participants TEXT NOT NULL DEFAULT '[]',         -- json array of person names
    hosts        TEXT NOT NULL DEFAULT '[]',         -- invitation hosts, separate from attendees
    series       TEXT,                   -- wiki page slug for the recurring thing
    note         TEXT,
    source       TEXT,                   -- the most recent bundle to touch this row
    -- Immutable first source; `source` is the last writer. Separate columns.
    origin       TEXT,
    -- The row this one happens *inside*. References `events.id`, never `key`
    -- (keys embed dates and break on re-date); distinct from `series` to avoid
    -- false merges.
    part_of      INTEGER REFERENCES events(id) ON DELETE SET NULL,
    -- Reply endpoint for the invitation; forwardable, distinct from asking the host.
    rsvp_url     TEXT,
    -- How to attend; distinct from location and `rsvp_url`.
    join_url     TEXT,
    -- The scheduled day this occurrence replaces. A date, not a row id, because the
    -- replaced occurrence is a rule projection. A cancelled week is `instead_of`
    -- set with `status = 'declined'`.
    instead_of   TEXT,
    written_by   TEXT NOT NULL DEFAULT 'cli',        -- cli | live | dream:<model> | sweep
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    -- When the creation evidence was *said*, distinct from write time; floor for the
    -- per-field write guard. NULL for typed and pre-split rows.
    -- See `events._field_versions`.
    evidence_ts  TEXT
);
CREATE INDEX IF NOT EXISTS events_date_idx   ON events(date);
CREATE INDEX IF NOT EXISTS events_series_idx ON events(series);

CREATE TABLE IF NOT EXISTS event_history (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    -- `changed_at` is write time; `evidence_ts` is when the evidence was *said*.
    -- Authority comparisons use `evidence_ts`; `changed_at` orders the audit trail.
    changed_at TEXT NOT NULL,
    evidence_ts TEXT,
    written_by TEXT NOT NULL
);

-- ---------------------------------------------------------------- series ----
-- The *rule*, as against the occurrences it generates. One row per series holding
-- the rule in force *now*; a cadence change overwrites here and moves the old rule
-- to `series_history`. Ended series keep row and history.
CREATE TABLE IF NOT EXISTS series (
    slug         TEXT PRIMARY KEY,      -- matches events.series and the wiki page slug
    title        TEXT NOT NULL,
    cadence      TEXT,                  -- weekly | fortnightly | monthly, or NULL
    weekday      INTEGER,               -- 0=Mon .. 6=Sun, for weekly/fortnightly
    day_of_month INTEGER,               -- 1..31, for monthly
    time         TEXT,                  -- 'HH:MM'
    -- Canonical location/join_url; `events.SERIES_QUALITIES` reads here first.
    location     TEXT,
    join_url     TEXT,
    -- First day the current rule applies; prevents retro-dating past occurrences.
    effective_on TEXT NOT NULL,
    -- Last day it applies; NULL means standing. Set when the series ends.
    ends_on      TEXT,
    status       TEXT NOT NULL DEFAULT 'active',   -- active | ended
    source       TEXT,
    origin       TEXT,
    written_by   TEXT NOT NULL DEFAULT 'cli',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_history (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL REFERENCES series(slug) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    -- `changed_at` is write time; `evidence_ts` is when the evidence was *said*.
    -- Authority comparisons use `evidence_ts`; `changed_at` orders the audit trail.
    changed_at TEXT NOT NULL,
    evidence_ts TEXT,
    written_by TEXT NOT NULL
);

-- ---------------------------------------------------------------- to-dos ----
-- Closing is a conversational act; nothing here is ever closed by inference.
CREATE TABLE IF NOT EXISTS todos (
    id              INTEGER PRIMARY KEY,
    key             TEXT UNIQUE NOT NULL,
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | closed | dropped
    event_id        INTEGER REFERENCES events(id) ON DELETE SET NULL,
    subject         TEXT,                -- person it involves, if any
    due             TEXT,                -- ISO date, optional
    remind_at       TEXT,                -- ISO datetime: when to actually poke them
    reminded_at     TEXT,                -- so firing is idempotent
    reminder_uid    TEXT,                -- EventKit id, so it can be taken back off
    wake_condition  TEXT,                -- prose condition; surfaces when satisfied
    woke_at         TEXT,
    source          TEXT,
    written_by      TEXT NOT NULL DEFAULT 'cli',
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    updated_at      TEXT NOT NULL
);

-- -------------------------------------------------------------- standing ----
-- Retained temporarily for read compatibility while legacy rows are retired.
CREATE TABLE IF NOT EXISTS standing (
    id         INTEGER PRIMARY KEY,
    key        TEXT UNIQUE NOT NULL,
    kind       TEXT NOT NULL,            -- identity | preference | alias
    value      TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'permanent',   -- session | permanent
    hits       INTEGER NOT NULL DEFAULT 1,          -- repetitions -> promotion
    written_by TEXT NOT NULL DEFAULT 'cli',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- A retired S handle remains an address. The original row can leave the legacy table
-- without losing its label, evidence, or the place it moved to. Provenance and evidence
-- continue to use old_key, so no historical rows need to be rewritten.
CREATE TABLE IF NOT EXISTS standing_redirects (
    old_id           INTEGER PRIMARY KEY,
    old_key          TEXT UNIQUE NOT NULL,
    old_kind         TEXT NOT NULL,
    old_value        TEXT NOT NULL,
    old_scope        TEXT NOT NULL,
    old_written_by   TEXT NOT NULL,
    old_created_at   TEXT NOT NULL,
    destination_kind TEXT NOT NULL
                     CHECK(destination_kind IN ('wiki','identity','config','discarded')),
    destination_ref  TEXT,
    retired_at       TEXT NOT NULL,
    CHECK((destination_kind = 'discarded' AND destination_ref IS NULL)
          OR (destination_kind != 'discarded' AND destination_ref IS NOT NULL
              AND length(trim(destination_ref)) > 0))
);

-- ------------------------------------------------------------- questions ----
-- The "Ask about" block. Nosy mode without push infrastructure.
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,
    text        TEXT NOT NULL,
    about_event INTEGER REFERENCES events(id) ON DELETE SET NULL,
    about_todo  INTEGER REFERENCES todos(id) ON DELETE SET NULL,
    -- Last day the question text commits to; complements `about_event` for expiry
    -- when no event link exists.
    about_date  TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | answered | dropped
    answer      TEXT,
    -- Deferred rows survive the age limit until their wake condition holds.
    wake_condition TEXT,
    written_by  TEXT NOT NULL DEFAULT 'cli',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT '',
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS question_history (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TEXT NOT NULL,
    written_by  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS question_history_question_idx
    ON question_history(question_id, changed_at);

-- --------------------------------------------------------------- archive ----
-- Every raw item, appended, full-text indexed. Nothing lives only in a derived store.
CREATE TABLE IF NOT EXISTS archive (
    id          INTEGER PRIMARY KEY,
    stream      TEXT NOT NULL,           -- imessage | email | groupme | discord | agent | cli
    external_id TEXT NOT NULL,           -- stable id within the stream
    ts          TEXT NOT NULL,           -- ISO timestamp
    thread      TEXT,                    -- chat/thread identifier
    handle      TEXT,                    -- raw sender handle
    person      TEXT,                    -- resolved person, or NULL
    from_me     INTEGER NOT NULL DEFAULT 0,
    -- Addressee: `person` or `machine`. Distinct from `from_me` (authorship).
    addressed_to TEXT NOT NULL DEFAULT 'person',
    text        TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}',
    gated       INTEGER NOT NULL DEFAULT 0,
    gate_reason TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(stream, external_id)
);
CREATE INDEX IF NOT EXISTS archive_ts_idx     ON archive(ts);
CREATE INDEX IF NOT EXISTS archive_person_idx ON archive(person);
-- Supports per-thread bundling and diagnostics without a full archive scan.
CREATE INDEX IF NOT EXISTS archive_thread_idx ON archive(thread);

CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
    text, person, thread, content='archive', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS archive_ai AFTER INSERT ON archive BEGIN
    INSERT INTO archive_fts(rowid, text, person, thread)
    VALUES (new.id, new.text, coalesce(new.person,''), coalesce(new.thread,''));
END;
CREATE TRIGGER IF NOT EXISTS archive_ad AFTER DELETE ON archive BEGIN
    INSERT INTO archive_fts(archive_fts, rowid, text, person, thread)
    VALUES ('delete', old.id, old.text, coalesce(old.person,''), coalesce(old.thread,''));
END;
-- External-content FTS5 requires an explicit sync trigger on UPDATE; without it the
-- index and the table disagree. `integrity-check` validates the index only.
CREATE TRIGGER IF NOT EXISTS archive_au AFTER UPDATE ON archive BEGIN
    INSERT INTO archive_fts(archive_fts, rowid, text, person, thread)
    VALUES ('delete', old.id, old.text, coalesce(old.person,''), coalesce(old.thread,''));
    INSERT INTO archive_fts(rowid, text, person, thread)
    VALUES (new.id, new.text, coalesce(new.person,''), coalesce(new.thread,''));
END;

-- --------------------------------------------------------------- calendar ----
-- Current membership in an external calendar snapshot. The archive keeps every
-- revision; this ledger answers the different question "was this item still present
-- in the last complete poll?". `event_key` is minted once and follows a moved event.
CREATE TABLE IF NOT EXISTS calendar_items (
    identity       TEXT PRIMARY KEY,     -- calendar uid + event uid (+ recurring start)
    calendar_uid   TEXT NOT NULL,
    calendar_name  TEXT NOT NULL,
    event_uid      TEXT NOT NULL,
    event_key      TEXT NOT NULL,
    -- `db.utc_stamp`: UTC milliseconds with `Z` suffix; fixed point of the
    -- `toISOString()` string `ical._identity` hashes.
    starts_at      TEXT NOT NULL,
    subscribed     INTEGER NOT NULL DEFAULT 0,
    provider       TEXT NOT NULL DEFAULT 'ical', -- ical | partiful
    active         INTEGER NOT NULL DEFAULT 1,
    -- Fingerprint of the last observed Calendar.app state; equal means skip.
    revision       TEXT,
    -- Whether memcal created this event; prevents re-ingesting its own writes.
    published      INTEGER NOT NULL DEFAULT 0,
    -- Row state at publish time; different means the calendar copy needs an update.
    published_state TEXT,
    last_seen_at   TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calendar_items_provider_idx
    ON calendar_items(provider, active, starts_at);

-- ----------------------------------------------------------------- spool ----
-- Gated items waiting for the next dream pass. Watermark-driven, not queue-driven:
-- an item stays here until a pass claims it.
CREATE TABLE IF NOT EXISTS spool (
    id           INTEGER PRIMARY KEY,
    archive_id   INTEGER NOT NULL REFERENCES archive(id) ON DELETE CASCADE,
    entity       TEXT NOT NULL,          -- bundle key: person or thread
    added_at     TEXT NOT NULL,
    processed_at TEXT,
    run_id       INTEGER,
    -- normal | low. Automatic relevance orders this queue; only a person's explicit
    -- "no" keeps mail out of it entirely. See `gate.gate_email`.
    priority     TEXT NOT NULL DEFAULT 'normal',
    UNIQUE(archive_id)
);
CREATE INDEX IF NOT EXISTS spool_pending_idx ON spool(processed_at);
-- Which pass claimed a line; supports per-run views and requeue without a full scan.
CREATE INDEX IF NOT EXISTS spool_run_idx ON spool(run_id);

-- -------------------------------------------------------------- identity ----
-- Hash lookups, never model calls (thesis 5).
CREATE TABLE IF NOT EXISTS handles (
    handle     TEXT PRIMARY KEY,         -- normalized phone, email, or provider id
    person     TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'contacts',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unresolved (
    handle     TEXT PRIMARY KEY,
    stream     TEXT NOT NULL,
    seen_name  TEXT,
    sample     TEXT,
    count      INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- The gate's memory for email. "Evolves with me" is this table, not a model.
CREATE TABLE IF NOT EXISTS senders (
    address    TEXT PRIMARY KEY,
    decision   TEXT NOT NULL,            -- ignore | archive | process
    reason     TEXT,
    count      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Identity conclusions, kept apart from `handles` so each stays reversible.
-- `handles_moved` records which handles a merge carried, so a split restores them.
CREATE TABLE IF NOT EXISTS identity_assumptions (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'merge',     -- merge | name
    keep          TEXT NOT NULL,          -- the surviving spelling, or the guessed person
    also          TEXT NOT NULL,          -- the folded spelling, or the handle in question
    handles_moved TEXT NOT NULL DEFAULT '[]',
    why           TEXT,
    -- assumed: in effect. unsure: undecided. confirmed/split: the person's answer.
    state         TEXT NOT NULL DEFAULT 'assumed',
    source        TEXT NOT NULL DEFAULT 'model',
    created_at    TEXT NOT NULL,
    decided_at    TEXT
);

-- Handles with no human behind them. The mail is still filed; only the naming
-- question stops.
CREATE TABLE IF NOT EXISTS non_people (
    handle     TEXT PRIMARY KEY,
    label      TEXT,                      -- "Venmo", so its rows still read as something
    kind       TEXT NOT NULL DEFAULT 'service',
    why        TEXT,
    source     TEXT NOT NULL DEFAULT 'model',
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS top_tier (
    person     TEXT PRIMARY KEY,         -- these always pass the gate
    added_at   TEXT NOT NULL
);

-- ------------------------------------------------------------------ runs ----
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    mode         TEXT NOT NULL,          -- nightly | ondemand
    model        TEXT,
    bundles      INTEGER NOT NULL DEFAULT 0,
    items        INTEGER NOT NULL DEFAULT 0,
    diffs        INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0,
    -- `requests` is HTTP attempts including retries, `failed_calls` is completions
    -- that raised, and `wait_seconds` is backoff summed across threads.
    --
    -- Nullable: NULL is "not recorded". Readers print nothing for NULL.
    requests     INTEGER,
    failed_calls INTEGER,
    wait_seconds REAL,
    error        TEXT
);

-- ----------------------------------------------------------- generations ----
-- One row per model call, holding the id OpenRouter files it under: the only way
-- back to the stored prompt, completion, and reasoning.
CREATE TABLE IF NOT EXISTS generations (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    generation_id TEXT NOT NULL,         -- gen-... , the key at OpenRouter
    stage         TEXT NOT NULL,         -- propose | sweep | live | match
    label         TEXT,                  -- which bundles were in this call
    model         TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    -- HTTP requests this answer took. NULL is "not recorded".
    requests      INTEGER,
    created_at    TEXT NOT NULL,
    UNIQUE(generation_id)
);
CREATE INDEX IF NOT EXISTS generations_run_idx ON generations(run_id);

-- Removed location feature. `db._drop_empty_legacy_tables` drops these when empty
-- and leaves non-empty tables in place.

-- ----------------------------------------------------------------- threads --
-- A conversation as a thing in its own right. Counts are derived by
-- threads.refresh(); `label` and `participants` come from the source; `decision`
-- is the only human-written column and the point of the table.
CREATE TABLE IF NOT EXISTS threads (
    id           INTEGER PRIMARY KEY,
    stream       TEXT NOT NULL,
    thread       TEXT NOT NULL,          -- matches archive.thread
    label        TEXT,                   -- the source's display name, if it has one
    participants TEXT NOT NULL DEFAULT '[]',   -- json array of raw handles
    members      INTEGER NOT NULL DEFAULT 0,
    is_group     INTEGER NOT NULL DEFAULT 0,
    mine         INTEGER NOT NULL DEFAULT 0,   -- messages the user sent
    theirs       INTEGER NOT NULL DEFAULT 0,
    known        INTEGER NOT NULL DEFAULT 0,   -- speakers resolved to a contact
    mutuals      INTEGER NOT NULL DEFAULT 0,   -- of those, ones seen where the user does speak
    first_ts     TEXT,
    last_ts      TEXT,
    decision     TEXT,                   -- NULL = never asked | read | mute
    reason       TEXT,
    -- Platform mute state as evidence only; decides nothing unless `platform_mute` allows.
    platform_muted INTEGER NOT NULL DEFAULT 0,
    platform_note  TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE(stream, thread)
);
CREATE INDEX IF NOT EXISTS threads_decision_idx ON threads(decision);

-- Conversation membership keyed by platform handle; names are per-group observations.
-- `handles` joins the same person across platforms without rewriting history.
CREATE TABLE IF NOT EXISTS thread_members (
    stream       TEXT NOT NULL,
    thread       TEXT NOT NULL,
    handle       TEXT NOT NULL,
    seen_name    TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY(stream, thread, handle)
);
CREATE INDEX IF NOT EXISTS thread_members_handle_idx ON thread_members(handle);

-- Every per-conversation display name, preserved rather than overwritten.
CREATE TABLE IF NOT EXISTS thread_member_names (
    stream       TEXT NOT NULL,
    thread       TEXT NOT NULL,
    handle       TEXT NOT NULL,
    name         TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    seen_count   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(stream, thread, handle, name)
);
CREATE INDEX IF NOT EXISTS thread_member_names_handle_idx
    ON thread_member_names(handle);

-- GroupMe account names: stable identity evidence cached once globally.
-- Nicknames belong in `thread_member_names` above.
CREATE TABLE IF NOT EXISTS groupme_profiles (
    user_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Group rosters are fetched only after the gate passes, then snapshotted. A newly
-- gated unknown speaker invalidates the snapshot.
CREATE TABLE IF NOT EXISTS groupme_group_profile_sync (
    group_id        TEXT PRIMARY KEY,
    last_message_id TEXT,
    fetched_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------- collection --
-- One ingest pass with per-source counts. The queue view groups by pass.
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,          -- web | cli | nightly
    read        INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    passed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS collection_sources (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    stream        TEXT NOT NULL,
    read          INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    passed        INTEGER NOT NULL DEFAULT 0,
    muted         INTEGER NOT NULL DEFAULT 0,
    too_old       INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    note          TEXT,
    finished_at   TEXT NOT NULL,
    -- Terminal outcome of this source attempt: complete (exhausted), incomplete
    -- (more work remained), failed (fetch error), unavailable (preflight check
    -- failed, fetch never started), unknown (legacy row without outcome evidence).
    status        TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY(collection_id, stream)
);

-- ---------------------------------------------------------------- provenance --
-- Which model call wrote each row. One row per write; joins to `generations` for
-- the OpenRouter id back to prompt, reasoning, and completion.
CREATE TABLE IF NOT EXISTS provenance (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,        -- event | todo | question | standing | wiki
    ref           TEXT NOT NULL,        -- that row's unique key, or slug.slot for wiki
    verb          TEXT,                 -- opened | updated | asked | slot | …
    entity        TEXT,                 -- the bundle it came out of
    stage         TEXT,                 -- propose | sweep | live
    run_id        INTEGER,
    generation_id TEXT,                 -- gen-…, the key back to OpenRouter
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS provenance_ref_idx ON provenance(kind, ref);
CREATE INDEX IF NOT EXISTS provenance_gen_idx ON provenance(generation_id);

-- -------------------------------------------------------------- evidence --
-- Which original lines a write was reading. Many-to-many; `archive` stays the
-- source of truth, so links survive prompt and model changes.
CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,        -- event | todo | question | standing | wiki
    ref           TEXT NOT NULL,        -- the derived row's stable key
    archive_id    INTEGER NOT NULL REFERENCES archive(id) ON DELETE CASCADE,
    entity        TEXT,
    run_id        INTEGER,
    generation_id TEXT,
    attached_at   TEXT NOT NULL,
    UNIQUE(kind, ref, archive_id, generation_id)
);
CREATE INDEX IF NOT EXISTS evidence_ref_idx ON evidence(kind, ref);
CREATE INDEX IF NOT EXISTS evidence_archive_idx ON evidence(archive_id);

-- What has actually been reviewed for each typed fact: exactly which
-- collected observations were considered, one row each, including explicitly
-- valid no-change reviews. Collection health (`collection_sources`) and render
-- time say nothing here; only a stamp carrying evidence, or a reviewed bundle,
-- adds a row. A high-water mark would pretend a later citation covered the
-- earlier lines it skipped, so coverage is the set itself and holes stay holes.
CREATE TABLE IF NOT EXISTS reviewed_lines (
    kind         TEXT NOT NULL,        -- event | todo | question | wiki | …
    ref          TEXT NOT NULL,        -- that row's stable key
    archive_id   INTEGER NOT NULL REFERENCES archive(id) ON DELETE CASCADE,
    reviewed_at  TEXT NOT NULL,
    by_run       INTEGER,
    by_stage     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(kind, ref, archive_id)
);

-- -------------------------------------------------------------- slot history --
-- Prior wiki slot values, mirroring `event_history`. Kept in SQLite so user-edited
-- pages stay clean.
CREATE TABLE IF NOT EXISTS slot_history (
    id         INTEGER PRIMARY KEY,
    page       TEXT NOT NULL,        -- the page slug
    slot       TEXT NOT NULL,
    old_value  TEXT,                 -- NULL when the slot was empty, i.e. first write
    new_value  TEXT,
    source     TEXT,                 -- the bundle entity, or 'cli' / 'agent'
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS slot_history_page_idx ON slot_history(page, slot);

-- Durable outbox for atomically replacing a wiki file after its history commits.
CREATE TABLE IF NOT EXISTS wiki_pending_writes (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL,
    content       TEXT NOT NULL,
    expected_hash TEXT
);

-- ------------------------------------------------------------------ actions --
-- Completed assistant operations, written in the same transaction as the state
-- change they describe.
--
-- `op_id` identifies the operation for retry idempotence; `based_on` is the target's
-- prior state stamp, marking proposals formed against stale versions.
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY,
    op_id       TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,          -- event | todo | series | wiki
    ref         TEXT NOT NULL,          -- the stable target key. Survives title/date changes
    verb        TEXT NOT NULL,          -- inserted | updated | opened | closed | dropped | …
    surface     TEXT NOT NULL,          -- mcp | hermes | openclaw | cli | benchmark | unknown
    session     TEXT,                   -- the caller's session, when it has one
    fields      TEXT NOT NULL DEFAULT '{}',   -- {field: [old, new]} — what actually changed
    source_ids  TEXT NOT NULL DEFAULT '[]',   -- archive ids of the turn that caused it
    -- Why `source_ids` is empty, when it is.
    source_note TEXT,
    based_on    TEXT,                   -- the target's `updated_at` before this write
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_ref_idx ON actions(kind, ref);
CREATE INDEX IF NOT EXISTS actions_at_idx  ON actions(at);

-- --------------------------------------------------------- pending changes --
-- Observations that plainly change something whose target cannot yet be named.
-- Kept with evidence and retried as related evidence arrives; deliberately small.
CREATE TABLE IF NOT EXISTS pending_changes (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,          -- cancellation | move
    observation  TEXT NOT NULL,          -- what was said, in the source's own words
    -- When the observation was *said*, not filed. Late application compares
    -- against evidence time.
    observed_at  TEXT,
    -- What the observation named, kept as fields for exact identity checks on placement.
    subject_title TEXT,
    subject_date  TEXT,
    subject_time  TEXT,
    subject_location TEXT,
    -- Known target and who established it. Heuristics nominate; only stable
    -- identifiers or cited decisions authorise.
    target_key    TEXT,
    decided_by    TEXT,
    entity       TEXT,                   -- the bundle it came out of
    candidates   TEXT NOT NULL DEFAULT '[]',   -- event keys considered, when any were
    status       TEXT NOT NULL DEFAULT 'open', -- open | resolved | abandoned
    resolved_ref TEXT,                   -- the event key it turned out to be about
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(kind, observation)
);
CREATE INDEX IF NOT EXISTS pending_changes_status_idx ON pending_changes(status);

-- A stated relationship between two rows. `same_as` merges, `replaces` marks the
-- predecessor a cancellation target, `related` only forces co-evaluation.
CREATE TABLE IF NOT EXISTS event_links (
    id         INTEGER PRIMARY KEY,
    from_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    to_id      INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,          -- same_as | replaces | related
    written_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(from_id, to_id, kind)
);
CREATE INDEX IF NOT EXISTS event_links_from_idx ON event_links(from_id);
CREATE INDEX IF NOT EXISTS event_links_to_idx ON event_links(to_id);
