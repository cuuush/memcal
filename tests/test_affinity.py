"""Deciding which conversations are about the same thing, without asking a model.

Everything here is a pure function over rows, so unlike the outcome benchmarks it is
exactly reproducible: same input, same answer, every run, no cost. That is the reason
this layer is worth having tests for rather than only benchmark numbers — the stochastic
part of the pipeline can then be measured against a grouping that is known to be stable.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import dates  # noqa: E402
from memcal.dream import affinity  # noqa: E402
from memcal.dream.affinity import (  # noqa: E402
    Fragment, ambient_tokens, common_tokens, fragments, group, related, score_pairs,
)


class Row(dict):
    """Enough of a `sqlite3.Row` for the fragment reader."""

    def __getitem__(self, key):
        return self.get(key)


_NEXT_ID = [1000]


def line(text, ts, *, person=None, handle="", stream="imessage", reason=None):
    _NEXT_ID[0] += 1
    return Row(id=_NEXT_ID[0], text=text, ts=ts, person=person, handle=handle,
               stream=stream, gate_reason=reason)


class Bundle:
    def __init__(self, entity, items, title=""):
        self.entity, self.items, self.title = entity, items, title

    def render(self, *_a, **_k):
        return "\n".join(str(i["text"]) for i in self.items)


def frag(entity, when, tokens, *, who=(), origin="imessage:x"):
    return Fragment(entity=entity, archive_id=1, when=when,
                    tokens=frozenset(tokens), who=frozenset(who), origin=origin)


class TestALineNamingTwoOccasions(unittest.TestCase):
    """The failure that decides the unit of analysis.

    "I doing poker sat night and beerhall Sunday" is one message about two different
    evenings. Read as a single unit it carries the words of both and the dates of both,
    so it links the poker conversation to the beer conversation and a transitive grouping
    welds them permanently — the same shape that once chained ramen Thursday, poker
    Friday and a beer garden Saturday into one cluster on nothing but shared attendees.
    """

    def setUp(self):
        self.bundle = Bundle("person:reese", [
            line("I doing poker sat night and beerhall Sunday",
                 "2026-07-28T15:35:22-04:00"),
        ])

    def test_one_line_with_two_days_yields_two_fragments(self):
        found = fragments(self.bundle)
        self.assertEqual(len(found), 2, [f.when for f in found])

    def test_each_fragment_lands_on_its_own_day(self):
        found = sorted(fragments(self.bundle), key=lambda f: f.when or "")
        self.assertEqual([f.when for f in found], ["2026-08-01", "2026-08-02"])

    def test_each_fragment_keeps_only_its_own_subject(self):
        found = {f.when: f.tokens for f in fragments(self.bundle)}
        self.assertIn("poker", found["2026-08-01"])
        self.assertIn("beerhall", found["2026-08-02"])


class TestWordsThatIdentifyNothing(unittest.TestCase):
    """Two opposite ways a shared word can be meaningless, and both are needed.

    `ambient_tokens` catches a word one voice stamps across the year — Partiful appends
    "| Partiful" to every title it exports, which linked a 30th birthday to a
    capture-the-flag game a day apart. `common_tokens` catches the opposite: a word
    everybody uses about everything. Neither catches the other's case, and using spread
    alone once suppressed `beer`, `garden`, `poker`, `movie`, `rider` and `alliance` —
    every word that was actually doing the linking.
    """

    def test_a_platform_tag_from_one_voice_across_the_year_is_ambient(self):
        frags = [frag("e%d" % i, day, {"partiful", "party"}, origin="ical:partiful")
                 for i, day in enumerate(("2026-04-11", "2026-06-12", "2026-08-22"))]
        self.assertIn("partiful", ambient_tokens(frags))

    def test_a_word_many_people_say_is_not_ambient_however_far_apart(self):
        frags = [frag("e%d" % i, day, {"beer"}, origin="imessage:%d" % i)
                 for i, day in enumerate(("2026-04-11", "2026-06-12", "2026-08-22"))]
        self.assertNotIn("beer", ambient_tokens(frags))

    def test_a_word_used_in_most_conversations_identifies_nothing(self):
        by_entity = {f"e{i}": [frag(f"e{i}", "2026-08-02", {"dinner", "poker"})]
                     for i in range(20)}
        common = common_tokens(by_entity)
        self.assertIn("dinner", common)

    def test_a_word_in_only_a_couple_of_conversations_survives(self):
        by_entity = {f"e{i}": [frag(f"e{i}", "2026-08-02", {"dinner"})]
                     for i in range(20)}
        by_entity["rare"] = [frag("rare", "2026-08-02", {"dinner", "nitehawk"})]
        self.assertNotIn("nitehawk", common_tokens(by_entity))


class TestTheUserIsNotOneVoice(unittest.TestCase):
    """Every message the user sent has an empty handle.

    Falling through to `stream:` for those collapses their half of every conversation in
    the store into a single speaker. `ambient_tokens` then sees `beer` and `garden` said
    by "one voice" over two months and suppresses exactly the words that link — which is
    what happened the first time this was run over the real corpus.
    """

    def test_an_unattributed_line_is_credited_to_its_conversation(self):
        a = fragments(Bundle("person:marco", [
            line("beer garden next Sunday in Astoria", "2026-07-25T12:08:37-04:00")]))
        b = fragments(Bundle("person:anders", [
            line("beer garden next Sunday sounds good", "2026-07-25T13:00:00-04:00")]))
        self.assertNotEqual(a[0].origin, b[0].origin)

    def test_so_a_word_the_user_repeats_is_not_suppressed(self):
        frags = []
        for i, day in enumerate(("2026-06-01T10:00:00-04:00",
                                 "2026-07-01T10:00:00-04:00",
                                 "2026-08-01T10:00:00-04:00")):
            frags += fragments(Bundle(f"person:p{i}", [line("beer garden today", day)]))
        self.assertNotIn("garden", ambient_tokens(frags))


class TestRelatednessNeedsMoreThanAWord(unittest.TestCase):
    def test_a_shared_word_on_an_agreeing_day_links(self):
        a = frag("a", "2026-08-02", {"beer", "garden"})
        b = frag("b", "2026-08-02", {"beer", "bohemian"})
        self.assertTrue(related(a, b, frozenset(), 3))

    def test_a_shared_word_a_month_apart_does_not(self):
        a = frag("a", "2026-07-12", {"capture", "flag", "trojan"})
        b = frag("b", "2026-08-23", {"capture", "flag", "trojan"})
        self.assertFalse(related(a, b, frozenset(), 3))

    def test_a_weekly_fixture_does_not_absorb_next_week(self):
        a = frag("a", "2026-08-01", {"poker"})
        b = frag("b", "2026-08-08", {"poker"})
        self.assertFalse(related(a, b, frozenset(), 3))

    def test_undated_fragments_need_two_shared_words(self):
        a = frag("a", None, {"movie"})
        b = frag("b", None, {"movie"})
        self.assertFalse(related(a, b, frozenset(), 3))
        c = frag("c", None, {"movie", "nitehawk"})
        d = frag("d", None, {"movie", "nitehawk"})
        self.assertTrue(related(c, d, frozenset(), 3))

    def test_a_suppressed_word_cannot_carry_a_link(self):
        a = frag("a", "2026-08-22", {"partiful"})
        b = frag("b", "2026-08-23", {"partiful"})
        self.assertFalse(related(a, b, frozenset({"partiful"}), 3))

    def test_a_bundle_is_never_related_to_itself(self):
        a = frag("same", "2026-08-02", {"beer"})
        b = frag("same", "2026-08-02", {"beer"})
        self.assertFalse(related(a, b, frozenset(), 3))


class TestScoringIsNotVolume(unittest.TestCase):
    """A busy conversation must not outrank a real match by being large.

    Scored as fragment *pairs*, a group chat with hundreds of dated lines ranked above
    every genuine pair in the corpus — it linked to everything simply by mentioning many
    things on many days. Counting distinct `(day, word)` subjects cannot be inflated by
    repetition, which is the same reason corroboration is counted over origins.
    """

    def test_repetition_does_not_raise_a_score(self):
        chatty = Bundle("person:loud", [
            line("poker on saturday", "2026-07-27T10:00:00-04:00") for _ in range(20)])
        quiet = Bundle("person:diego", [
            line("poker on saturday", "2026-07-27T11:00:00-04:00")])
        other = Bundle("person:julian", [
            line("poker on saturday", "2026-07-27T12:00:00-04:00")])
        scores = score_pairs([chatty, quiet, other])
        self.assertEqual(len(set(scores.values())), 1, scores)


class TestGroupingStaysBounded(unittest.TestCase):
    """Relatedness is transitive and connectivity is not what is wanted.

    A links B on poker, B links C on a beer garden, and a connected-components pass puts
    all three in one request and keeps going until half the corpus is in it. Growing from
    the strongest pair and stopping at the budgets `pack` already enforces keeps the
    useful property without the blob.
    """

    def setUp(self):
        self.bundles = [
            Bundle("a", [line("poker saturday", "2026-07-27T10:00:00-04:00")]),
            Bundle("b", [line("poker saturday and beer garden sunday",
                              "2026-07-27T11:00:00-04:00")]),
            Bundle("c", [line("beer garden sunday", "2026-07-27T12:00:00-04:00")]),
        ]

    def test_a_bridging_conversation_grows_a_group_but_a_bounded_one(self):
        # Reading all three together is fine and is the intended behaviour: grouping only
        # decides what is read at once. What must not happen is unbounded growth, so the
        # guarantee under test is the cap, not the absence of transitivity.
        groups, _left = group(self.bundles, max_bundles=2, max_tokens=10_000,
                              cost=lambda b: 10)
        self.assertTrue(all(len(g) <= 2 for g in groups),
                        [[b.entity for b in g] for g in groups])

    def test_the_bundle_cap_is_respected(self):
        many = [Bundle(f"e{i}", [line("beer garden sunday", "2026-07-27T10:00:00-04:00")])
                for i in range(10)]
        groups, _left = group(many, max_bundles=3, max_tokens=10_000, cost=lambda b: 10)
        self.assertTrue(all(len(g) <= 3 for g in groups))

    def test_the_token_budget_is_respected(self):
        many = [Bundle(f"e{i}", [line("beer garden sunday", "2026-07-27T10:00:00-04:00")])
                for i in range(6)]
        groups, _left = group(many, max_bundles=6, max_tokens=25, cost=lambda b: 10)
        self.assertTrue(all(len(g) <= 2 for g in groups))

    def test_everything_unrelated_falls_through_untouched(self):
        alone = [Bundle("x", [line("dentist on tuesday", "2026-07-27T10:00:00-04:00")]),
                 Bundle("y", [line("haircut on friday", "2026-07-27T10:00:00-04:00")])]
        groups, leftovers = group(alone, max_bundles=6, max_tokens=10_000,
                                  cost=lambda b: 10)
        self.assertEqual(groups, [])
        self.assertEqual(len(leftovers), 2)


class TestTheRealCasesFromTheStore(unittest.TestCase):
    """The three pairs the corpus is known to contain, and one it must not invent."""

    def test_the_riders_email_and_the_organisers_text_link(self):
        email = Bundle("thread:email:hannah@ridersalliance.org", [
            line("Join us Thursday, July 30 at Nitehawk Cinema in Prospect Park",
                 "2026-07-13T13:58:07-04:00", handle="hannah@ridersalliance.org",
                 stream="email")])
        text = Bundle("thread:imessage:+15165002353", [
            line("We're hosting a movie night next Thursday, July 30 at Nitehawk",
                 "2026-07-23T13:08:20-04:00", handle="+15165002353")])
        self.assertTrue(score_pairs([email, text]))

    def test_two_ticket_confirmations_are_not_one_occasion(self):
        a = frag("a", "2026-07-16", {"order", "confirmation", "ticket", "festival"})
        b = frag("b", "2026-07-17", {"order", "confirmation", "ticket", "festival"})
        # They share only the shape of being a ticket, which every vendor mail has, so
        # document frequency removes it and nothing is left to link on.
        common = common_tokens({f"e{i}": [frag(f"e{i}", "2026-07-16",
                                               {"order", "confirmation", "ticket",
                                                "festival"})] for i in range(20)})
        self.assertFalse(related(a, b, common, 3))


class TestCodeDoesTheCalendarArithmetic(unittest.TestCase):
    """`dates.resolve`, which is the piece kept from the mentions experiment.

    "beer garden saturday? like 3", said on a Monday, landed on the following Sunday in
    two runs out of four. Code has the message's timestamp and cannot miscount.
    """

    def test_a_weekday_resolves_against_the_day_it_was_said(self):
        self.assertEqual(dates.resolve("saturday", date(2026, 7, 27)), "2026-08-01")

    def test_next_weekday_skips_a_week(self):
        self.assertEqual(dates.resolve("next sunday", date(2026, 7, 25)), "2026-08-02")

    def test_the_riders_phrasing_lands_on_the_right_thursday(self):
        self.assertEqual(dates.resolve("next Thursday", date(2026, 7, 23)), "2026-07-30")

    def test_an_unrecognised_phrase_declines_rather_than_guesses(self):
        self.assertIsNone(dates.resolve("sometime soon", date(2026, 7, 27)))

    def test_a_weekday_said_on_that_weekday_means_today(self):
        self.assertEqual(dates.resolve("saturday", date(2026, 8, 1)), "2026-08-01")

    def test_the_weekday_of_a_date_is_reported_for_the_audit(self):
        self.assertEqual(dates.weekday_of("2026-08-01"), "saturday")


class TestAnOrdinalDayKeepsItsMonth(unittest.TestCase):

    said = date(2026, 8, 11)

    def test_an_ordinal_suffix_does_not_lose_the_month(self):
        self.assertEqual(dates.resolve("party on January 22nd", self.said), "2027-01-22")
        self.assertEqual(dates.resolve("dinner September 25th", self.said), "2026-09-25")

    def test_the_day_may_come_first(self):
        self.assertEqual(dates.resolve("the 25th of September", self.said), "2026-09-25")
        self.assertEqual(dates.resolve("25 December", self.said), "2026-12-25")

    def test_a_bare_ordinal_still_means_the_next_one(self):
        # The branch is right when there is no month; it was only ever wrong as a
        # fallback for a month phrase that failed to parse.
        self.assertEqual(dates.resolve("dinner on the 3rd", self.said), "2026-09-03")
        self.assertEqual(dates.resolve("the 22nd", self.said), "2026-08-22")

    def test_a_month_with_a_day_it_cannot_read_declines(self):
        # "September 2026" names no day. Answering it from the ordinal branch, or from
        # the weekday sitting next to it, is inventing one.
        self.assertIsNone(dates.resolve("sometime in September 2026", self.said))
        self.assertIsNone(dates.resolve("early September", self.said))

    def test_a_weekday_beside_a_month_and_day_still_reads_the_date(self):
        """The decoy, and the case RIGHT_CONTEXT was widened for in the first place:
        "Join us Thursday, July 30 at Nitehawk" is a date, not a Thursday."""
        self.assertEqual(dates.resolve("Join us Thursday, July 30 at", date(2026, 7, 20)),
                         "2026-07-30")


class TestAYearSomebodyWroteDownIsNotAGuess(unittest.TestCase):

    def test_a_stated_year_is_used_as_stated(self):
        self.assertEqual(dates.resolve("the meeting is on September 25, 2026",
                                       date(2027, 2, 9)), "2026-09-25")
        self.assertEqual(dates.resolve("the conference is March 3, 2028",
                                       date(2026, 12, 1)), "2028-03-03")

    def test_a_year_further_out_than_next_year_survives(self):
        # `anchor.year, anchor.year + 1` could not express this at all.
        self.assertEqual(dates.resolve("tickets for July 4, 2027", date(2026, 8, 14)),
                         "2027-07-04")

    def test_a_stated_year_in_the_past_is_not_rolled_forward(self):
        # "remember January 10, 2026?" is a memory, not a commitment. Rolling it to 2027
        # invents a future occasion out of a past one, which is the expensive direction.
        self.assertEqual(dates.resolve("remember January 10, 2026?", date(2026, 8, 14)),
                         "2026-01-10")

    def test_the_year_survives_being_found_in_a_sentence(self):
        # `claims` is what production calls; the phrase it cuts has to still carry it.
        phrases = dates.claims("the 30th precinct meeting is on September 25, 2026")
        self.assertTrue(phrases)
        self.assertEqual(dates.resolve(phrases[0], date(2027, 2, 9)), "2026-09-25")

    def test_a_bare_month_and_day_still_means_the_next_one(self):
        self.assertEqual(dates.resolve("September 25", date(2026, 8, 14)), "2026-09-25")


class TestAnIsoDateIsFoundAndNotOnlyParsed(unittest.TestCase):
    """`resolve` read `2026-09-25` perfectly and `claims` never handed it one.

    `PHRASE_RE` — the thing that *finds* the phrases worth resolving — contained no
    digits at all, so a line whose only date is ISO produced no candidate. 370 lines in
    the live archive contain one. Nothing was wrong; a whole form was simply invisible,
    which is why no test caught it: every date test wrote the forms that worked.

    `MONTH_OR_ISO_RE` already knew about ISO, so the module disagreed with itself. Both
    are built from one pattern now.
    """

    def test_an_iso_date_in_a_sentence_becomes_a_claim(self):
        phrases = dates.claims("the appointment is on 2026-09-25, bring the form")
        self.assertTrue(phrases, "an ISO date is a date")
        self.assertEqual(dates.resolve(phrases[0], date(2026, 8, 14)), "2026-09-25")

    def test_the_two_patterns_agree_about_what_states_a_day(self):
        for text in ("meet 2026-09-25", "meet September 25"):
            self.assertTrue(dates.MONTH_OR_ISO_RE.search(text), text)
            self.assertTrue(dates.claims(text), text)


class TestAnAbbreviatedMonthIsStillAMonth(unittest.TestCase):
    """`Sept 25` and `Oct 3` produced no date at all — 72 lines in the live archive.

    This one failed *safely*: no phrase, no claim, and None means ask. It is here
    because `_ABBREV` covers weekday abbreviations and stops there, so the month half
    reads as unfinished rather than decided. An abbreviation only counts with a day
    number behind it, unlike the weekday ones: `mar` and `sep` are ordinary words in a
    way `weds` is not, and a bare one is not worth a candidate.
    """

    said = date(2026, 8, 14)

    def test_an_abbreviated_month_with_a_day_resolves(self):
        self.assertEqual(dates.resolve("dinner Sept 25", self.said), "2026-09-25")
        self.assertEqual(dates.resolve("dinner Oct 3", self.said), "2026-10-03")
        self.assertEqual(dates.resolve("party Jan 22nd", self.said), "2027-01-22")

    def test_it_is_found_in_a_sentence_too(self):
        phrases = dates.claims("the show is on Dec 12 at the hall")
        self.assertTrue(phrases)
        self.assertEqual(dates.resolve(phrases[0], self.said), "2026-12-12")

    def test_a_bare_abbreviation_is_not_a_date(self):
        # "we may go", "mar" in a name. A weekday abbreviation earns a candidate on its
        # own because it is a whole claim; a month without a day is not one.
        self.assertIsNone(dates.resolve("we can go in Sept", self.said))
        self.assertEqual(dates.claims("we can go in Sept"), [])


if __name__ == "__main__":
    unittest.main()
