"""The model-free layer: what a *perfect* propose stage would have returned."""

from __future__ import annotations

import sqlite3

from memcal import archive, brief, llm, wiki
from memcal.config import Config
from memcal.dream import apply as apply_stage
from memcal.dream import bundle as bundle_stage
from memcal.dream import merge as merge_stage

EMPTY = {"events": [], "todos": [], "wiki": [], "questions": []}

# Mail bundles are keyed by the conversation's root Message-ID, not by the sender:
# `proton.conversation_key` follows In-Reply-To/References, and two unrelated mails from
# one address are two conversations. The ids below are the fixtures' own headers.

FRI, SAT, SUN = "2026-08-07", "2026-08-08", "2026-08-09"
THU, WED, TUE_NEXT, SAT_15 = "2026-08-06", "2026-08-05", "2026-08-11", "2026-08-15"


def _diff(**kw) -> dict:
    return {**EMPTY, **kw}


# --------------------------------------------------------------------------------
# Day 1 — establish. Keys are omitted on purpose: these rows are new, and letting
# `upsert` mint the key is what the real pass does.
# --------------------------------------------------------------------------------

DAY1 = {
    "thread:groupme:poker crew": _diff(events=[{
        "title": "Poker at Jordan's", "date": FRI, "time": "20:00", "kind": "commitment",
        "status": "confirmed", "location": "Jordan's place", "subject": "me",
        "participants": ["Jordan Lee", "Alex Rivera", "Cameron Ortiz"],
        "series": "poker-night",
    }]),
    "thread:whatsapp:dinner thu": _diff(events=[{
        "title": "Ramen dinner", "date": THU, "time": "19:00", "kind": "commitment",
        "status": "confirmed", "subject": "me",
        "participants": ["Alex Rivera", "Cameron Ortiz", "Riley Morgan"],
    }]),
    "thread:groupme:brunch sunday": _diff(events=[{
        "title": "Brunch", "date": SUN, "time": "11:00", "kind": "commitment",
        "status": "confirmed", "subject": "me",
        "participants": ["Skyler Reed", "Devon Park"],
    }]),
    "thread:groupme:beer garden": _diff(events=[{
        "title": "Beer garden", "date": SAT, "time": "15:00", "kind": "commitment",
        "status": "confirmed", "subject": "me",
        "participants": ["Cameron Ortiz", "Alex Rivera"],
    }]),
    "thread:groupme:block party": _diff(events=[{
        "title": "Block party BBQ", "date": SAT_15, "time": "14:00", "kind": "commitment",
        "status": "mentioned", "subject": "me", "participants": ["Devon Park"],
    }]),
    "thread:groupme:board game night": _diff(events=[{
        "title": "Board game night at Jose's", "date": SAT, "kind": "opportunity",
        "status": "mentioned", "location": "Jose's place", "subject": "me",
        "participants": ["Jose", "Quinn Brooks"],
    }]),
    "thread:groupme:rave chat": _diff(events=[
        {
            "title": "Neon Garden party", "date": "2026-08-04", "time": "21:00",
            "kind": "opportunity", "status": "mentioned", "subject": "me",
            "location": "Elsewhere", "participants": [],
        },
        {
            "key": "elements-music-festival@2026-08-07",
            "title": "Elements Music Festival", "date": "2026-08-07",
            "until": "2026-08-09", "kind": "commitment", "status": "confirmed",
            "subject": "me", "location": "Cedar Falls",
            "participants": ["Alex Rivera"],
        },
    ], questions=["Are you going to the Neon Garden party?"]),
    "thread:whatsapp:doggo park": _diff(events=[{
        "title": "ASPCA mobile clinic at Doggo Park", "date": WED, "time": "10:00",
        "until": WED, "kind": "opportunity", "status": "mentioned",
        "location": "Doggo Park run, 129th Street entrance", "subject": "me",
        "participants": [],
    }, {
        # 54. Proposed by a WhatsApp LID nothing can ever name, agreed to by Rae. The
        # plan survives its proposer; the numeral is not among the participants and Rae
        # is, so "drop the whole line" cannot satisfy this.
        "title": "Dog park meet-up", "date": SAT, "time": "09:00",
        "kind": "opportunity", "status": "mentioned",
        "location": "Doggo Park, 129th Street entrance", "subject": "me",
        "participants": ["Rae"],
    }]),
    "person:Quinn Brooks": _diff(wiki=[
        {"page": "quinn-brooks", "section": "people",
         "slot": "favorite movie theater", "value": "Alamo Drafthouse",
         "question": "", "alias": ""},
        {"page": "quinn-brooks", "section": "people",
         "slot": "favorite Pokemon set", "value": "Team Rocket",
         "question": "", "alias": ""},
        {"page": "quinn-brooks", "section": "people",
         "slot": "sister", "value": "Katie",
         "question": "", "alias": ""},
        {"page": "katie", "section": "people",
         "slot": "brother", "value": "Quinn Brooks",
         "question": "", "alias": "Kat"},
    ]),
    "person:Riley Morgan": _diff(events=[
        {"title": "Climbing gym", "date": WED, "kind": "commitment", "status": "mentioned",
         "subject": "me", "participants": ["Riley Morgan"]},
        {"title": "Movie", "date": TUE_NEXT, "kind": "commitment", "status": "mentioned",
         "subject": "me", "participants": ["Riley Morgan"]},
    ]),
    "person:Jordan Lee": _diff(wiki=[{
        "page": "jordan-lee", "section": "people", "slot": "where they live",
        "value": "Eastwood", "question": "", "alias": "",
    }]),
    "thread:agent:conversation": _diff(
        events=[
            {"title": "Poker with Quinn at Robbie's", "date": "2026-06-05",
             "kind": "observed", "status": "happened", "subject": "me",
             "participants": ["Quinn Brooks"], "location": "Robbie's house",
             "series": "poker-night"},
            {"title": "Dinner with Quinn", "date": "2026-06-20",
             "kind": "observed", "status": "happened", "subject": "me",
             "participants": ["Quinn Brooks"], "location": "Xi'an Famous Foods"},
            {"title": "Board game night with Quinn", "date": "2026-07-18",
             "kind": "observed", "status": "happened", "subject": "me",
             "participants": ["Quinn Brooks"]},
        ],
        todos=[{
            "op": "open", "key": "todo:ezpass-rowan",
            "text": "Give Rowan back their EZ-Pass", "subject": "Rowan Vale", "due": "",
            "wake_condition": "Rowan is back from Italy",
        }]),
    "thread:email:em08.1785777600@drafthouse.com": _diff(events=[{
        "title": "Dune at Alamo Drafthouse", "date": "2026-08-12", "time": "19:30",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "location": "Alamo Drafthouse Downtown", "participants": [],
    }]),
    # 41. The row lands with no address, because there is not one yet, and the missing
    # field becomes a question rather than a guess. Day 3 answers it.
    "person:Devon Park": _diff(
        events=[{
            "title": "Devon's housewarming", "date": SAT_15, "time": "18:00",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "participants": ["Devon Park"],
        }],
        # No date in the wording, deliberately: a question that names a thing *and*
        # the day it happens is a calendar row wearing a question mark, and
        # `_dated_occasion` correctly writes it as one instead. The missing field here
        # is the address, and that is all this should ask for.
        questions=["Where is Devon's housewarming?"]),
    # 50. Stakes. The deposit and the notice window are why this row matters, so they
    # belong on it — the brief can then say what it costs to miss.
    "person:Sasha Kim": _diff(events=[{
        "title": "Tattoo session with Sasha", "date": "2026-08-18", "time": "14:00",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "participants": ["Sasha Kim"],
        "note": "$100 deposit, non-refundable. 48 hours notice to move it",
    }]),
    # 51. The pet insurer's webinar has no entry here at all, which is the answer.
    # 49. The user was asked to do something and said the user would. No date is named anywhere,
    # so it is a to-do and never a row.
    "person:Alex Chen": _diff(todos=[{
        "op": "open", "key": "todo:jira-sec-2847",
        "text": "Look at Jira ticket SEC-2847 for Alex Chen", "subject": "Alex Chen",
        "due": "", "wake_condition": "",
    }]),
    # 44. The weekly physio slot, on a series so the move can be seen to keep it.
    "person:Nadia Okoro": _diff(events=[{
        "title": "Physio session", "date": "2026-08-12", "time": "17:00",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "series": "physio", "location": "Riverton PT", "participants": [],
    }]),
    # 45. The invitation is already on the calendar as opportunity/mentioned, because
    # the feed row carries no location and that is all the RSVP inference has to go on.
    # This is the conversation settling it, onto the row the connector wrote.
    "person:Morgan": _diff(events=[{
        "title": "Jack's 30th", "date": "2026-08-22", "time": "19:00",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "participants": ["Morgan"],
    }]),
}

# --------------------------------------------------------------------------------
# Day 2 — modify. These carry no `key` either, which is deliberate: the whole question
# is whether `find_match` reunites a change with the row it belongs to the way it has
# to when a real model omits the key, which it often does.
# --------------------------------------------------------------------------------

DAY2 = {
    "thread:groupme:smash bros": _diff(events=[{
        "title": "Smash", "date": SUN, "time": "19:00",
        "kind": "opportunity", "status": "mentioned", "subject": "me",
        "participants": ["Riley Morgan"],
    }]),
    # 1 + 3. Moved to Saturday, and moved house. One row, two fields.
    "person:Jordan Lee": _diff(
        events=[
            {
                "title": "Poker at Jordan's", "date": SAT, "time": "20:00",
                "kind": "commitment", "status": "confirmed",
                "location": "42 Example Street", "subject": "me",
                "series": "poker-night",
                "participants": ["Jordan Lee", "Alex Rivera"],
            },
        ],
        wiki=[{"page": "jordan-lee", "section": "people", "slot": "where they live",
               "value": "Riverton", "question": "", "alias": ""}],
    ),
    # 57. An explicit replacement appointment must update both date and time on the row
    # day 1 created. The key *is* supplied, because that is what a model reading this
    # conversation is shown and told to do: the row is in the bundle's ALREADY ON THE
    # CALENDAR block, and the instruction beside it is "return it with that exact key".
    #
    # It used to be omitted, and matching on the title alone stood in for a reschedule.
    # That is the same signal an independent second booking at the same provider sends —
    # exact title, no key, a different day inside the window — so honouring it here also
    # merged two real appointments into one (`collision` family 6). Cross-date wording
    # nominates a candidate; only a key or a series says which row moved.
    "person:Sasha Kim": _diff(events=[{
        "key": "tattoo-session-with-sasha@2026-08-18",
        "title": "Tattoo session with Sasha", "date": "2026-08-20", "time": "16:15",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "participants": ["Sasha Kim"],
        "note": "$100 deposit, non-refundable. 48 hours notice to move it",
    }]),
    # 2 + 6 + 9. Time moved, Riley in, and Cameron's vague "next weekend" correctly
    # produces nothing at all — which is the entire point of the decoy.
    "thread:whatsapp:dinner thu": _diff(events=[{
        "title": "Ramen dinner", "date": THU, "time": "20:30", "kind": "commitment",
        "status": "confirmed", "subject": "me",
        "participants": ["Alex Rivera", "Riley Morgan"],
    }]),
    # 11. The same dinner from a second stream. Merge has to collapse this.
    "person:Alex Rivera": _diff(events=[{
        "title": "Ramen dinner", "date": THU, "time": "20:30", "kind": "commitment",
        "status": "confirmed", "subject": "me", "participants": ["Alex Rivera"],
    }]),
    # 4 + 8. Climbing confirmed; the movie is off.
    "person:Riley Morgan": _diff(events=[
        {"title": "Climbing gym", "date": WED, "kind": "commitment",
         "status": "confirmed", "subject": "me", "participants": ["Riley Morgan"]},
        {"title": "Movie", "date": TUE_NEXT, "kind": "commitment", "status": "declined",
         "subject": "me", "participants": ["Riley Morgan"]},
    ]),
    # 5. The private decline. The public "see everyone sunday" in the group bundle
    # deliberately proposes nothing — a perfect model knows it is not news.
    "person:Skyler Reed": _diff(events=[{
        "title": "Brunch", "date": SUN, "time": "11:00", "kind": "commitment",
        "status": "declined", "subject": "me",
        "participants": ["Skyler Reed", "Devon Park"],
    }]),
    # 7. The Partiful email moving a row the GroupMe thread created.
    "thread:email:em01.1785849300@partiful.com": _diff(events=[{
        "title": "Block party BBQ", "date": SAT_15, "time": "16:00",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "participants": ["Devon Park"],
    }]),
    # 12. The wake condition is satisfied by ingestion, not by this diff — a perfect
    # model raises the question and never closes the to-do.
    "person:Rowan Vale": _diff(
        questions=["Rowan is back from Italy — did you get them their EZ-Pass back?"]),
    "person:Mom": _diff(questions=["When am I coming over again?"]),
    "person:Quinn Brooks": _diff(wiki=[{
        "page": "quinn-brooks", "section": "people", "slot": "car permission",
        "value": "Can borrow the user's car anytime the user needs it",
        "question": "", "alias": "",
    }]),
    # 35. The same contract one beat over: a cross-channel DM saying the party moved
    # names the row it is moving, because the pass was shown that row.
    "person:Jose": _diff(events=[{
        "key": "neon-garden-party@2026-08-04",
        "title": "Neon Garden party", "date": "2026-08-05", "time": "21:00",
        "kind": "opportunity", "status": "mentioned", "subject": "me",
        "location": "Elsewhere", "participants": [],
    }]),
    "person:Morgan": _diff(
        events=[{
            "key": "spider-man-movie@2026-08-10",
            "title": "Spider-Man movie", "date": "2026-08-10", "time": "19:40",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "location": "AMC Lincoln Square", "participants": ["Morgan"],
            "note": "Seats H8 and H9",
        }],
        todos=[{
            "op": "close", "key": "todo:make-sure-we-have-spider-man-tickets",
            "text": "Make sure we have Spider-Man tickets", "subject": "",
            "due": "", "wake_condition": "",
            "event_key": "spider-man-movie@2026-08-10",
        }],
    ),
    "thread:email:em09.1785850800@amctheatres.com": _diff(
        events=[{
            "key": "fantastic-four-movie@2026-08-09",
            "title": "Fantastic Four movie", "date": "2026-08-09", "time": "18:20",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "location": "AMC Empire 25", "participants": [],
            "note": "Auditorium 7, seats J10 and J11, confirmation 84721",
        }],
        todos=[{
            "op": "close", "key": "todo:make-sure-we-have-fantastic-four-tickets",
            "text": "Make sure we have Fantastic Four tickets", "subject": "",
            "due": "", "wake_condition": "",
            "event_key": "fantastic-four-movie@2026-08-09",
        }],
    ),
}


# --------------------------------------------------------------------------------
# Days 3 and 4 — elapsed time. Almost nothing is written here, and that is the shape
# of the days themselves: what has to be right is what the *store* does while nobody
# is saying anything.
# --------------------------------------------------------------------------------

DAY3 = {
    # 41. The address arrives two days after memcal asked for it. Nothing else moves.
    "person:Devon Park": _diff(events=[{
        "title": "Devon's housewarming", "date": SAT_15, "time": "18:00",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "location": "55 Linden Avenue", "participants": ["Devon Park"],
    }]),
    # 44. The clinic moves the appointment. Same series, same time, new date — and
    # nobody told memcal anything except the clinic.
    "person:Nadia Okoro": _diff(events=[{
        "title": "Physio session", "date": "2026-08-19", "time": "17:00",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "series": "physio", "location": "Riverton PT", "participants": [],
    }]),
    # 47. A row that happens inside another row's span and says so in its own name.
    "thread:groupme:rave chat": _diff(events=[{
        "title": "Breakfast at Elements", "date": SAT, "time": "09:00",
        "kind": "commitment", "status": "confirmed", "subject": "me",
        "participants": ["Alex Rivera"],
    }]),
    # 46. The obligation nobody announced, inside a thread about a video game.
    "thread:groupme:smash bros": _diff(todos=[{
        "op": "open", "key": "todo:devon-deposit",
        "text": "Send Devon the deposit before Saturday", "subject": "Devon Park",
        "due": "", "wake_condition": "",
    }]),
}

#: 42/43. Day 4 writes nothing at all. Every day-4 check is about what the store did
#: on its own: a question retiring with its subject, an unattached obligation not
#: retiring with anything, and a calendar rescan that learned nothing.
DAY4: dict[str, dict] = {}

TABLES = {1: DAY1, 2: DAY2, 3: DAY3, 4: DAY4}


class _DistinctArbiter:
    """Resolve the corpus's intentionally similar occasions without a model call."""

    def complete(self, **_kwargs) -> llm.Reply:
        return llm.Reply(text="", data={
            "same_event": False,
            "pending_targets": [],
            "observation_targets": [],
            "why": "the integration oracle defines these as distinct occasions",
        })


def apply_day(conn: sqlite3.Connection, cfg: Config, day: int) -> str:
    """One fake day through Merge -> apply -> render, with no model anywhere."""
    table = TABLES[day]
    bundles = bundle_stage.build(conn, limit=cfg.item_budget,
                                 per_entity=cfg.items_per_entity)
    proposals = [(b, table.get(b.entity, EMPTY), None) for b in bundles]

    # Model quality belongs to the live layer. The integration oracle already defines
    # the similar-looking rows here as different occasions.
    proposals, resolved = merge_stage.merge_all(
        _DistinctArbiter(), cfg, proposals, conn=conn)

    from memcal import db, todos
    before_apply = db.now()
    counts, log = apply_stage.apply_diffs(conn, cfg, proposals,
                                          written_by=f"dream:integration-day{day}",
                                          stage="propose")
    # The `_dream` half of a read: every bundle here was considered in full —
    # oracle diffs for some, an explicitly empty diff for the rest — so the
    # facts evidenced in its conversations are reviewed through what was read.
    # Without this the deterministic layer asserts a store production never
    # exhibits: spool marked read with review marks standing still and hints
    # over everything.
    from memcal import activity  # noqa: PLC0415
    for bundle in bundles:
        activity.advance_thread(conn, bundle.items, by_stage="dream",
                                commit=False)
    archive.spool_mark(conn, [sid for b in bundles for sid in b.spool_ids], None)

    for todo in todos.check_wakes(conn, bundle_stage.all_text(bundles),
                                  since=before_apply):
        todos.ask(conn, f"{todo.text} — {todo.wake_condition} now looks true. Still open?",
                  key=f"q:wake:{todo.key}", about_todo=todo.id, written_by="dream")

    wiki.prune_empty(cfg.wiki_dir)
    wiki.link_series(conn, cfg.wiki_dir)

    # The tail of `run.py`, in `run.py`'s order. Without it the integration layer stops
    # at "did apply keep the answer", and everything the *store* does while nobody is
    # saying anything — a question retiring with its subject, a lapsed row becoming a
    # question, a link being re-scored — happens only in the model layer and is graded
    # nowhere. That is most of what days 3 and 4 exist to test.
    #
    # `ical.publish_pending` is deliberately not here. It is the one step that writes
    # outside this process, and no benchmark may enable it.
    from memcal import events, pending                              # noqa: PLC0415
    from memcal.dream import sweep as sweep_stage                    # noqa: PLC0415
    events.mark_past_happened(conn)
    pending.retry(conn, ask=lambda text, key: todos.ask(
        conn, text, key=key, written_by="dream"))
    events.link_contained(conn)
    todos.relink_questions(conn)
    todos.expire_questions(conn)
    sweep_stage.reconcile_backward_window(conn, cfg)

    brief.write(conn, cfg)
    matched = sum(1 for b in bundles if b.entity in table)
    return (f"integration day {day} — {len(bundles)} bundles, "
            f"{matched} carried a diff, "
            f"{sum(counts.values())} writes"
            + (f"; merge: {'; '.join(resolved)}" if resolved else ""))
