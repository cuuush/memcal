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
    series       TEXT,                   -- wiki page slug for the recurring thing
    note         TEXT,
    source       TEXT,                   -- the most recent bundle to touch this row
    -- Where the row came from *first*, set once and never updated. `source` is mutable,
    -- so it answers "who wrote this last" and was being read as "where did this come
    -- from" — which is how a visit by Avery came to display `Source: Cameron Ortiz`,
    -- the unrelated conversation that happened to amend it afterwards. Two questions,
    -- two columns.
    origin       TEXT,
    -- The row this one happens *inside*. Points at `events.id` and never at `key`,
    -- because a key embeds the date it was minted with, so re-dating a row would
    -- orphan everything naming it — the mistake `calendar_items.event_key` already
    -- made. Deliberately not `series`: `find_match_scored` reads `series`, and two
    -- same-series rows ten days apart would be *merged* rather than nested.
    part_of      INTEGER REFERENCES events(id) ON DELETE SET NULL,
    -- Where you reply to this invitation. It is not a link *about* the event, it is
    -- the fact that this is a thing you RSVP through — so it can be forwarded, which
    -- is a different act from having to ask the host yourself.
    rsvp_url     TEXT,
    -- How you *attend*. `location` answers where and `rsvp_url` answers how you reply;
    -- a online appointment, a work Zoom and a Meet link are none of those, and
    -- until this column existed a join link had no field to land in from any source.
    -- The calendar entry for a tutoring appointment read "Online" as its location,
    -- which is true, is not a place you can go, and is not a link you can press — while
    -- the link itself sat in the email that created the row and in the calendar event's
    -- own description. Both connectors were throwing it away, which is what a missing
    -- column looks like from the outside.
    join_url     TEXT,
    -- The scheduled day this row stands in for, as an ISO date. A series that meets
    -- Tuesdays and skips to Wednesday *this week only* is one occurrence contradicting
    -- its own rule, and until this column existed there was no way to say that: the row
    -- was simply a Wednesday, indistinguishable from the cadence having moved. It is
    -- what stops `series.roll_forward` re-materialising the Tuesday it replaces, and it
    -- is a date rather than a row id because the occurrence it replaces is a projection
    -- of the rule and has no row of its own. A cancelled week is `instead_of` set with
    -- `status = 'declined'` — the skip is recorded, not merely absent.
    instead_of   TEXT,
    written_by   TEXT NOT NULL DEFAULT 'cli',        -- cli | live | dream:<model> | sweep
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_date_idx   ON events(date);
CREATE INDEX IF NOT EXISTS events_series_idx ON events(series);

CREATE TABLE IF NOT EXISTS event_history (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    changed_at TEXT NOT NULL,
    written_by TEXT NOT NULL
);

-- ---------------------------------------------------------------- series ----
-- The *rule*, as against the occurrences it generates.
--
-- `events.series` has always been a wiki-page slug, and the prompt has always said "a
-- recurring thing is one row for the next occurrence". So the store recorded instances
-- of a schedule and never the schedule, and the sentence that actually arrives in an
-- email — "can we move to Tuesdays at 1pm going forward" — had nowhere to land. Every
-- consequence follows from that one absence: the Monday cadence cannot "go away",
-- because there is no Monday cadence to end, only rows in the past; a single week moved
-- to Wednesday cannot be an *exception*, because there is nothing for it to be an
-- exception to; and the join link survives only by scavenging whichever past row
-- happened to carry it.
--
-- One row per series, holding the rule in force *now*. Invariant 4: recency is resolved
-- at write time, so a cadence change overwrites here and the superseded rule moves to
-- `series_history` with the day it stopped applying. Nothing is weighed at read time and
-- nothing is deleted (invariant 7) — an ended series keeps its row and its history.
CREATE TABLE IF NOT EXISTS series (
    slug         TEXT PRIMARY KEY,      -- matches events.series and the wiki page slug
    title        TEXT NOT NULL,
    cadence      TEXT,                  -- weekly | fortnightly | monthly, or NULL
    weekday      INTEGER,               -- 0=Mon .. 6=Sun, for weekly/fortnightly
    day_of_month INTEGER,               -- 1..31, for monthly
    time         TEXT,                  -- 'HH:MM'
    -- The qualities, held where they are actually true rather than scavenged from
    -- whichever instance last carried them. `events.SERIES_QUALITIES` reads here first.
    location     TEXT,
    join_url     TEXT,
    -- The first day the rule in force applies. A change announced on the 7th for the
    -- 18th sets this to the 18th, which is what makes "going forward" expressible and
    -- what stops the change retro-dating occurrences that already happened.
    effective_on TEXT NOT NULL,
    -- The last day it applies; NULL means forever, which is the honest default for a
    -- standing appointment. Set when the user stops going.
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
    changed_at TEXT NOT NULL,
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
-- Always-true, always-relevant. Hard token cap enforced at render time.
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

-- ------------------------------------------------------------- questions ----
-- The "Ask about" block. Nosy mode without push infrastructure.
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,
    text        TEXT NOT NULL,
    about_event INTEGER REFERENCES events(id) ON DELETE SET NULL,
    about_todo  INTEGER REFERENCES todos(id) ON DELETE SET NULL,
    -- The last day the question's own words commit to, when they commit to one. A
    -- question dies with its subject, and the only route to a subject's day used to be
    -- `about_event` — so the rule reached 5 of 12 open questions and two of the seven it
    -- could not see were asking about a Sunday five days gone. This is what makes the
    -- rule total: the link answers when there is one, this answers when there is not.
    about_date  TEXT,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | answered | dropped
    answer      TEXT,
    written_by  TEXT NOT NULL DEFAULT 'cli',
    created_at  TEXT NOT NULL,
    answered_at TEXT
);

-- --------------------------------------------------------------- archive ----
-- Every raw item, appended, full-text indexed. Nothing lives only in a derived store.
CREATE TABLE IF NOT EXISTS archive (
    id          INTEGER PRIMARY KEY,
    stream      TEXT NOT NULL,           -- imessage | email | groupme | agent | cli
    external_id TEXT NOT NULL,           -- stable id within the stream
    ts          TEXT NOT NULL,           -- ISO timestamp
    thread      TEXT,                    -- chat/thread identifier
    handle      TEXT,                    -- raw sender handle
    person      TEXT,                    -- resolved person, or NULL
    from_me     INTEGER NOT NULL DEFAULT 0,
    -- What is on the other end: `person` or `machine`. `from_me` is a fact about
    -- authorship and says nothing about the addressee, and on the `agent` stream the
    -- two come apart -- see `Source.addressed_to`.
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
-- "who is this thread with" is asked per thread by bundling and per page by the web
-- diagnostic; without this it is a full scan of the archive every time.
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
-- An external-content FTS5 table serves a non-MATCH lookup from `archive` itself and a
-- MATCH from its own index, so an UPDATE with no trigger leaves the two disagreeing
-- while every obvious check says they agree. `UPDATE archive SET person` is not rare --
-- GroupMe's profile sync runs it over every row of a re-identified speaker -- and 121
-- rows naming `Rowan` were unfindable by that name while `SELECT ... WHERE person =
-- 'Rowan'` on this very table returned all of them. `integrity-check` passes throughout:
-- it validates the index against itself, not against the content.
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
    -- `db.utc_stamp`: UTC, milliseconds, `Z`. One notation, because three writers fill
    -- it and two readers compare it with `=` and `>=`. The milliseconds preserve
    -- — it has to be a fixed point of the `toISOString()` string `ical._identity` hashes
    -- for a recurring occurrence, which `ical._rebind` reads back out of this column.
    starts_at      TEXT NOT NULL,
    subscribed     INTEGER NOT NULL DEFAULT 0,
    provider       TEXT NOT NULL DEFAULT 'ical', -- ical | partiful
    active         INTEGER NOT NULL DEFAULT 1,
    -- A fingerprint of everything Calendar.app said about this event last time. Equal
    -- means nothing to do: no archive row, no gate, no upsert. Reading the calendar is
    -- expensive enough without re-deriving a row that has not moved.
    revision       TEXT,
    -- Set when memcal itself created this event in Calendar.app, so the next scan can
    -- tell its own writes from the user's and not read them back in as news.
    published      INTEGER NOT NULL DEFAULT 0,
    -- What the memcal row said when it was published. Equal means the calendar copy is
    -- current; different means the row moved and the copy has to be updated.
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
    UNIQUE(archive_id)
);
CREATE INDEX IF NOT EXISTS spool_pending_idx ON spool(processed_at);

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
    -- What the pass spent that no completion can account for. A run that is refused
    -- for an hour has no generations rows, no tokens and no dollars, and reported
    -- "0 calls · $0.0000" while making 76 requests over 56 minutes. `requests` is HTTP
    -- attempts including retries, `failed_calls` is completions that raised, and
    -- `wait_seconds` is backoff summed across threads — so it exceeds the wall clock
    -- and separates "a queue" from "a slow model".
    --
    -- Nullable on purpose: NULL is "not recorded", which is what every run before this
    -- column existed genuinely is. `NOT NULL DEFAULT 0` would have made twenty historical
    -- passes claim they issued no requests — a fresh instance of the exact defect these
    -- columns were added to end. Every surface that reads them prints nothing for NULL.
    requests     INTEGER,
    failed_calls INTEGER,
    wait_seconds REAL,
    error        TEXT
);

-- ----------------------------------------------------------- generations ----
-- One row per model call, holding the id OpenRouter files it under.
--
-- OpenRouter stores the full prompt, completion and reasoning for every call when
-- Input & Output Logging is enabled, readable at /api/v1/generation/content?id=...
-- There is no endpoint that enumerates them, so the id is the only way back to a
-- trace — and it arrives in the response and was previously dropped on the floor.
-- Keeping it costs 60 bytes and turns "why did it write that?" into a lookup.
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
    -- HTTP requests this one answer took. 1 on a healthy call; a reply retried into
    -- existence over twenty attempts used to be indistinguishable from it. NULL is
    -- "not recorded" — a row here only exists for a call that returned, so 1 is a safe
    -- floor and a false claim, and the floor is not worth the claim.
    requests      INTEGER,
    created_at    TEXT NOT NULL,
    UNIQUE(generation_id)
);
CREATE INDEX IF NOT EXISTS generations_run_idx ON generations(run_id);

-- The location tables are gone (2026-08-02). Find My was built and then blocked by
-- macOS encrypting its caches, so it never produced a row; the fallback chain — an iOS
-- Shortcut writing to iCloud Drive, a Mac Shortcut, a manual check-in — was a lot of
-- moving parts serving a feature nothing else in the store read.
--
-- `visits`, `places` and `location_samples` were left in place on existing databases,
-- on the grounds that deleting somebody's data to tidy a schema is not a trade worth
-- making. That is still true and it turned out not to apply: the feature never produced
-- a row, so what the rule preserved was three empty tables advertising a capability
-- nothing implements. `db._drop_empty_legacy_tables` drops them **when they are empty**,
-- on the next open, and leaves anything with a row in it exactly where it is.
--
-- What survived them was worse and is the reason this paragraph is longer than the
-- feature deserved: `meta.source.findmy.last_success` stayed behind, `freshness()`
-- builds its stream list out of records like that one, and the brief told the agent
-- "no findmy 9 days — this week may be incomplete" for nine days after Find My stopped
-- existing. A deleted component must not be able to keep asserting itself through what
-- it left behind.

-- ----------------------------------------------------------------- threads --
-- A conversation as a thing in its own right, rather than a string on every archive
-- row. Three problems all came from not having this:
--
--   * a group chat's bundle was titled `thread:imessage:9858b62c1615…`, because the
--     only name available was whatever the source put in `archive.thread`;
--   * two different group chats both called "Crystal Harbor" were indistinguishable in
--     the UI (they differed by a trailing space, which is worse than colliding);
--   * "I never post in the GroupMe dev chat and know nobody in it" is a fact about a
--     conversation, and there was nowhere to keep it — so every judgement about
--     whether a chat is worth reading had to be made per message, forever.
--
-- `mine`/`theirs`/`known`/`mutuals` are derived from the archive by threads.refresh();
-- `label` and `participants` are what the source told us. `decision` is the only
-- column a human writes, and it is the point of the table.
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
    -- What the platform itself says, kept strictly as evidence. GroupMe knows the user muted
    -- 19 of their 101 groups, and 15 of those are full of people the user talks to daily —
    -- muting there means "stop buzzing my phone", not "I don't care". So it is recorded
    -- and shown and never allowed to decide anything unless `platform_mute` says so.
    platform_muted INTEGER NOT NULL DEFAULT 0,
    platform_note  TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE(stream, thread)
);
CREATE INDEX IF NOT EXISTS threads_decision_idx ON threads(decision);

-- A conversation roster is not a list of display names. The durable member is the
-- platform handle (a phone number, GroupMe user id, WhatsApp jid, ...); names are
-- observations that may change per group or over time. `handles` joins the same person
-- across platforms, so resolving one of these handles immediately updates every roster
-- query without rewriting historical rows.
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

-- Preserve every per-conversation display name rather than overwriting the last one.
-- "DJ Pickle" in one GroupMe and "Alexander" in another are both useful evidence when
-- the user runs `memcal who groupme:123 Alexander Rivera`.
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

-- GroupMe exposes two different names for a member: a nickname scoped to one group,
-- and the account name returned by that group's detail endpoint.  Nicknames belong in
-- thread_member_names above.  Account names are stable-handle identity evidence and
-- are cached once globally so one person appearing in twenty groups is still one node.
CREATE TABLE IF NOT EXISTS groupme_profiles (
    user_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- The groups index deliberately omits memberships.  Fetch a full roster only after
-- that conversation has passed the gate, then keep a permanent snapshot.  Only a
-- newly gated unknown speaker invalidates it.  This also makes an existing store
-- self-healing: a group with gated archive rows but no snapshot is fetched on the next
-- ingest even when its message watermark is already caught up.
CREATE TABLE IF NOT EXISTS groupme_group_profile_sync (
    group_id        TEXT PRIMARY KEY,
    last_message_id TEXT,
    fetched_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------- collection --
-- One ingest pass. The `runs` table above is the *dream* pass; until this existed
-- there was no record that collection had happened at all: per-source counts lived in
-- an in-memory job object that died with the process, and the only durable trace was a
-- scattering of watermarks. So "when did email last run, and did it work?" had no
-- answer, and a Proton Bridge that had been closed for a week was indistinguishable
-- from a quiet inbox.
--
-- It is also what the queue view groups by. "Waiting for the next dream" could only
-- show *gated* items, because a skipped one is not in the spool at all — it exists
-- solely as `archive.gated = 0`, with nothing tying it to the pass that skipped it.
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
    PRIMARY KEY(collection_id, stream)
);

-- ---------------------------------------------------------------- provenance --
-- Which model call wrote this row. `written_by` says "dream:nightly", which names a
-- mode and not a call — so "where did *this* question come from?" had no answer, and
-- `Is "Shayla" a nickname for Harper?` looked like it arrived from nowhere.
--
-- One row per write, not one column on each table: the same to-do gets touched by
-- several passes over its life, and the interesting question is usually the whole
-- chain, not the last writer. Joins to `generations` for the OpenRouter id, which is
-- the way back to the prompt, the reasoning and the completion.
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
-- Provenance answers "which call wrote this?". Evidence answers the more useful
-- question: "which original lines was that call reading?"
--
-- Keep the link many-to-many. A row may be revised by several conversations, and one
-- conversation may contribute several lines. `archive` remains the source of truth;
-- this table is only an index back into it. A source link therefore survives prompt
-- rewrites, model changes, and the on-disk call trace being pruned.
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

-- -------------------------------------------------------------- slot history --
-- Events resolve recency at write time and push the old value into `event_history`.
-- Slots resolved recency at write time and pushed the old value into nothing: the wiki
-- is markdown on disk, `set_slot` replaces the line, and Jordan's Eastwood address was
-- simply gone the moment the lease fell through.
--
-- That asymmetry is the one place the store forgets something on purpose, and it is not
-- defensible for the same reason `event_history` exists — "when did this change, and
-- what did it say before" is the question asked of a memory system most often. Kept in
-- SQLite rather than in the page, because the page is a file the user hand-edits in Obsidian
-- and history there would be clutter the user has to read past forever.
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
