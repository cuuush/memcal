"""The one identity call, and what it is allowed to conclude.

No test here makes the call. Each hands `apply` a reply and asserts on what the store
does with it: what it writes, what it refuses, and what it can undo. The subject is the
handling of a wrong answer, not the odds of getting a right one.
"""

from __future__ import annotations

import unittest

from _support import Base

from memcal import db, identity, whois


def _archive(conn, *, handle, person, stream="whatsapp", thread="t", text="hi"):
    conn.execute(
        "INSERT INTO archive(stream, external_id, ts, text, handle, person, thread,"
        " from_me, created_at) VALUES(?,?,?,?,?,?,?,0,?)",
        (stream, f"{stream}-{handle}-{thread}-{text}", db.now(), text, handle, person,
         thread, db.now()))
    conn.commit()


class TestAMergeIsAnAssumptionNotAJudgement(Base):
    """"Assume they are the same unless something proves otherwise" only works if the
    assumption can be taken back, exactly, months later."""

    def setUp(self):
        super().setUp()
        identity.link(self.conn, "whatsapp:lid:88003", "Cam Ortiz",
                      source="whatsapp:profile")
        identity.link(self.conn, "+19175550003", "Cameron Ortiz", source="contacts")
        _archive(self.conn, handle="whatsapp:lid:88003", person="Cam Ortiz")
        _archive(self.conn, handle="+19175550003", person="Cameron Ortiz",
                 stream="imessage")

    def test_the_merge_takes_effect_immediately(self):
        number = whois.assume(self.conn, "Cameron Ortiz", "Cam Ortiz",
                              why="Nick is a diminutive of Nicholas")
        self.assertIsNotNone(number)
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"),
                         "Cameron Ortiz")
        left = {row["person"] for row in self.conn.execute(
            "SELECT DISTINCT person FROM archive")}
        self.assertEqual(left, {"Cameron Ortiz"},
                         "the folded spelling must be gone from the archive too")

    def test_a_split_puts_back_exactly_the_handles_it_moved(self):
        # A handle that already answered to the surviving name must not be dragged
        # across by the undo — the merge never moved it, so the split must not either.
        identity.link(self.conn, "cam.ortiz@example.com", "Cameron Ortiz",
                      source="contacts")
        number = whois.assume(self.conn, "Cameron Ortiz", "Cam Ortiz", why="")

        self.assertEqual(whois.split(self.conn, number), "Cam Ortiz")
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")
        self.assertEqual(identity.resolve(self.conn, "+19175550003"), "Cameron Ortiz")
        self.assertEqual(identity.resolve(self.conn, "cam.ortiz@example.com"),
                         "Cameron Ortiz")
        people = {row["person"] for row in self.conn.execute(
            "SELECT DISTINCT person FROM archive")}
        self.assertEqual(people, {"Cam Ortiz", "Cameron Ortiz"})

    def test_splitting_twice_is_not_an_error_and_changes_nothing(self):
        number = whois.assume(self.conn, "Cameron Ortiz", "Cam Ortiz", why="")
        whois.split(self.conn, number)
        self.assertIsNone(whois.split(self.conn, number))
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")

    def test_splitting_a_merge_chain_in_display_order_restores_every_name(self):
        identity.link(self.conn, "whatsapp:lid:88004", "Cami Ortiz",
                      source="whatsapp:profile")
        first = whois.assume(self.conn, "Cam Ortiz", "Cami Ortiz", why="")
        second = whois.assume(self.conn, "Cameron Ortiz", "Cam Ortiz", why="")

        whois.split(self.conn, first)
        whois.split(self.conn, second)

        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88004"), "Cami Ortiz")
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")

    def test_a_confirmed_merge_stops_being_listed(self):
        number = whois.assume(self.conn, "Cameron Ortiz", "Cam Ortiz", why="")
        self.assertEqual([r["id"] for r in whois.assumptions(self.conn)], [number])
        self.assertIn("Cameron Ortiz", whois.confirm(self.conn, number))
        self.assertEqual(whois.assumptions(self.conn), [])


class TestTheRailsRefuseWhatTheStoreCanDisprove(Base):
    """The prompt argues; these refuse. A hallucinated merge should not get through
    on the strength of a confident `why`."""

    def setUp(self):
        super().setUp()
        identity.set_me(self.conn, "Rowan Vale")
        identity.link(self.conn, "+19178889999", "Rowan Vale", source="contacts")
        identity.link(self.conn, "+19175550007", "Alex Chen", source="contacts")
        identity.link(self.conn, "+19175550002", "Alex Rivera", source="contacts")
        identity.link(self.conn, "groupme:9", "Jamie", source="groupme:profile")

    def test_two_address_book_cards_are_two_people(self):
        self.assertEqual(whois._refusal(self.conn, "Alex Rivera", "Alex Chen"),
                         "separate cards in Contacts")
        self.assertIsNone(whois.assume(self.conn, "Alex Rivera", "Alex Chen"))
        self.assertEqual(identity.resolve(self.conn, "+19175550007"), "Alex Chen")

    def test_nobody_is_merged_into_the_user(self):
        self.assertEqual(whois._refusal(self.conn, "Rowan Vale", "Jamie"),
                         "one of them is you")

    def test_a_name_that_does_not_exist_is_not_merged(self):
        refused = whois._refusal(self.conn, "Alex Rivera", "Somebody Invented")
        self.assertEqual(refused, "no such person: Somebody Invented")

    def test_apply_says_which_merges_it_refused_and_why(self):
        log = whois.apply(self.conn, whois.Resolution(same=[
            {"keep": "Alex Rivera", "also": "Alex Chen", "why": "both are Joe"}]))
        self.assertEqual(len(log), 1)
        self.assertIn("refused merge", log[0])
        self.assertIn("separate cards in Contacts", log[0])


class TestNamingFromTheWholeBoard(Base):
    def setUp(self):
        super().setUp()
        identity.link(self.conn, "+19175550016", "Nadia Okoro", source="contacts")

    def test_a_model_name_lands_on_a_handle_nothing_else_answers_for(self):
        identity.note_unresolved(self.conn, "whatsapp:lid:77", "whatsapp")
        self.conn.commit()
        log = whois.apply(self.conn, whois.Resolution(names=[
            {"handle": "whatsapp:lid:77", "person": "Nadia Okoro", "why": "same group"}]))
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:77"), "Nadia Okoro")
        self.assertTrue(any("named" in line for line in log))

    def test_contacts_still_outranks_the_model(self):
        """The call reads a whole board and is still a guess. Contacts is not."""
        log = whois.apply(self.conn, whois.Resolution(names=[
            {"handle": "+19175550016", "person": "Nadia Okora", "why": "spelling"}]))
        self.assertEqual(identity.resolve(self.conn, "+19175550016"), "Nadia Okoro")
        self.assertTrue(any("kept" in line for line in log))

    def test_a_name_shaped_like_an_encoding_artefact_is_not_adopted(self):
        identity.note_unresolved(self.conn, "whatsapp:lid:78", "whatsapp")
        self.conn.commit()
        whois.apply(self.conn, whois.Resolution(names=[
            {"handle": "whatsapp:lid:78", "person": "+GJXsntMGIAE=", "why": ""}]))
        self.assertIsNone(identity.resolve(self.conn, "whatsapp:lid:78"))


class TestAServiceStopsBeingAskedAbout(Base):
    """`venmo@venmo.com` sat at the head of the queue with 31 messages behind it. It is
    shaped exactly like a friend's address, so nothing that judges by shape could ever
    clear it, and an unanswerable question at the head of a queue stops the queue."""

    def test_the_ruling_survives_the_next_message_arriving(self):
        identity.note_unresolved(self.conn, "venmo@venmo.com", "email",
                                 sample="Upcoming changes to Venmo")
        self.conn.commit()
        whois.apply(self.conn, whois.Resolution(not_people=[
            {"handle": "venmo@venmo.com", "label": "Venmo", "kind": "service",
             "why": "payment receipts"}]))
        self.assertEqual(identity.unresolved(self.conn), [])

        identity.note_unresolved(self.conn, "venmo@venmo.com", "email")
        self.conn.commit()
        self.assertEqual(identity.unresolved(self.conn), [],
                         "the question came straight back on the next message")
        self.assertEqual(identity.non_person_label(self.conn, "venmo@venmo.com"), "Venmo")

    def test_a_handle_that_already_names_a_person_is_left_alone(self):
        identity.link(self.conn, "bailey@example.com", "Bailey Stone", source="contacts")
        self.assertFalse(whois.mark_not_person(self.conn, "bailey@example.com"))
        self.assertEqual(identity.resolve(self.conn, "bailey@example.com"), "Bailey Stone")


class TestTheBoardPutInFrontOfTheModel(Base):
    """One call sees the whole board; fifty calls each see one square. The payload has
    to actually carry what makes the answer possible."""

    def setUp(self):
        super().setUp()
        identity.link(self.conn, "whatsapp:lid:88003", "Cam Ortiz",
                      source="whatsapp:profile")
        identity.link(self.conn, "+19175550003", "Cameron Ortiz", source="contacts")
        _archive(self.conn, handle="whatsapp:lid:88003", person="Cam Ortiz",
                 thread="Family")
        _archive(self.conn, handle="+19175550003", person="Cameron Ortiz",
                 stream="imessage", thread="Family")
        identity.note_unresolved(self.conn, "whatsapp:lid:99", "whatsapp",
                                 sample="running late")
        self.conn.commit()

    def test_the_roster_carries_handles_volume_and_where_each_one_speaks(self):
        board = {row["person"]: row for row in whois.roster(self.conn)}
        self.assertIn("Cam Ortiz", board)
        self.assertEqual(board["Cam Ortiz"]["messages"], 1)
        self.assertIn("whatsapp:lid:88003 [whatsapp:profile]",
                      board["Cam Ortiz"]["handles"])
        self.assertIn("whatsapp/Family", board["Cam Ortiz"]["speaks_in"])
        self.assertIn("imessage/Family", board["Cameron Ortiz"]["speaks_in"])

    def test_the_unresolved_queue_and_the_roster_travel_together(self):
        payload = db.jload(whois.picture(self.conn), {})
        self.assertTrue(payload["roster"], "the roster is the context that answers it")
        self.assertEqual([r["handle"] for r in payload["unresolved"]],
                         ["whatsapp:lid:99"])

    def test_a_reply_cut_off_at_its_ceiling_applies_nothing(self):
        log = whois.apply(self.conn, whois.Resolution(truncated=True, same=[
            {"keep": "Cameron Ortiz", "also": "Cam Ortiz", "why": ""}]))
        self.assertIn("cut off", log[0])
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")


class TestADoubtIsRecordedRatherThanGuessed(Base):
    """The call is told to prefer "not sure" to a guess. That only helps if the doubt
    outlives the run: an unnamed handle is a question a person answers in seconds and
    nothing else in the system can answer at all."""

    def setUp(self):
        super().setUp()
        identity.link(self.conn, "whatsapp:lid:88003", "Cam Ortiz",
                      source="whatsapp:profile")
        identity.link(self.conn, "+19175550003", "Cameron Ortiz", source="contacts")
        _archive(self.conn, handle="whatsapp:lid:88003", person="Cam Ortiz")

    def test_an_unsure_merge_changes_nothing_and_is_listed_on_its_own(self):
        log = whois.apply(self.conn, whois.Resolution(unsure=[
            {"kind": "merge", "about": "Cam Ortiz", "guess": "Cameron Ortiz",
             "why": "same surname, but they share a group"}]))
        self.assertTrue(any("not sure" in line for line in log))
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")
        self.assertEqual(whois.assumptions(self.conn), [],
                         "a doubt must not appear as a merge already in effect")
        self.assertEqual([r["also"] for r in whois.assumptions(self.conn, "unsure")],
                         ["Cam Ortiz"])

    def test_confirming_a_doubt_is_when_the_merge_happens(self):
        whois.apply(self.conn, whois.Resolution(unsure=[
            {"kind": "merge", "about": "Cam Ortiz", "guess": "Cameron Ortiz", "why": ""}]))
        number = whois.assumptions(self.conn, "unsure")[0]["id"]
        self.assertIsNotNone(whois.confirm(self.conn, number))
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"),
                         "Cameron Ortiz")

    def test_a_confirmed_doubt_records_what_it_moved_so_it_can_still_be_undone(self):
        whois.apply(self.conn, whois.Resolution(unsure=[
            {"kind": "merge", "about": "Cam Ortiz", "guess": "Cameron Ortiz", "why": ""}]))
        number = whois.assumptions(self.conn, "unsure")[0]["id"]
        whois.confirm(self.conn, number)
        self.assertEqual(whois.split(self.conn, number), "Cam Ortiz")
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")

    def test_saying_no_to_a_doubt_settles_it_without_touching_anything(self):
        whois.apply(self.conn, whois.Resolution(unsure=[
            {"kind": "merge", "about": "Cam Ortiz", "guess": "Cameron Ortiz", "why": ""}]))
        number = whois.assumptions(self.conn, "unsure")[0]["id"]
        self.assertEqual(whois.split(self.conn, number), "Cam Ortiz")
        self.assertEqual(whois.assumptions(self.conn, "unsure"), [])
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")

    def test_an_unsure_name_links_the_handle_when_confirmed(self):
        identity.note_unresolved(self.conn, "whatsapp:lid:91", "whatsapp")
        self.conn.commit()
        whois.apply(self.conn, whois.Resolution(unsure=[
            {"kind": "name", "about": "whatsapp:lid:91", "guess": "Cameron Ortiz",
             "why": "speaks in the same group, never alongside them"}]))
        number = whois.assumptions(self.conn, "unsure")[0]["id"]
        self.assertIsNone(identity.resolve(self.conn, "whatsapp:lid:91"))
        whois.confirm(self.conn, number)
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:91"), "Cameron Ortiz")

    def test_the_same_doubt_twice_is_asked_once(self):
        for _ in range(2):
            whois.apply(self.conn, whois.Resolution(unsure=[
                {"kind": "merge", "about": "Cam Ortiz", "guess": "Cameron Ortiz",
                 "why": ""}]))
        self.assertEqual(len(whois.assumptions(self.conn, "unsure")), 1)

    def test_a_doubt_with_no_candidate_cannot_be_confirmed_into_a_merge(self):
        whois.apply(self.conn, whois.Resolution(unsure=[
            {"kind": "name", "about": "whatsapp:lid:92", "guess": "", "why": "no idea"}]))
        number = whois.assumptions(self.conn, "unsure")[0]["id"]
        self.assertIsNone(whois.confirm(self.conn, number))

    def test_the_rails_apply_to_a_persons_answer_too(self):
        """Confirming is the moment a doubt acts, so it is the moment the store gets to
        refuse — the same refusal it would give the call."""
        identity.set_me(self.conn, "Rowan Vale")
        identity.link(self.conn, "+19178889999", "Rowan Vale", source="contacts")
        whois.apply(self.conn, whois.Resolution(unsure=[
            {"kind": "merge", "about": "Cam Ortiz", "guess": "Rowan Vale", "why": ""}]))
        number = whois.assumptions(self.conn, "unsure")[0]["id"]
        self.assertIsNone(whois.confirm(self.conn, number))
        self.assertEqual(identity.resolve(self.conn, "whatsapp:lid:88003"), "Cam Ortiz")


if __name__ == "__main__":
    unittest.main()
