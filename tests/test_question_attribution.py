"""Question speaker context must come from the question's cited messages."""

import unittest

from tests._support import Base
from memcal import archive, db, todos
from memcal.dream import apply, bundle, propose


class TestQuestionAttributionUsesCitedMessages(Base):
    def setUp(self):
        super().setUp()
        db.set_today("2026-10-20T12:00:00-04:00")

    def line(self, text, *, person="Aster", from_me=False, thread="dm"):
        archive_id = archive.append(
            self.conn, channel="imessage", external_id=str(
                self.conn.execute("SELECT count(*) FROM archive").fetchone()[0]),
            ts=db.now(), thread=thread, text=text, person=person, from_me=from_me)
        return self.conn.execute(
            "SELECT * FROM archive WHERE id=?", (archive_id,)).fetchone()

    def ask(self, source, text, cites):
        diff = {"questions": [{"action": "ask", "text": text, "cites": cites}]}
        propose._resolve_cites(source, diff)
        apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by="dream:test")
        return diff

    def test_unrelated_group_context_cannot_name_a_self_contained_question(self):
        unrelated = self.line("We want to go with everyone to a workshop.",
                              person="Bram", thread="group")
        invitation = self.line("Want to go to a cafe with Cedar and me?", from_me=True)
        acceptance = self.line("Yes, I'd love to!")
        venue = self.line("We should go to Lantern Cafe")
        source = bundle.Bundle(entity="person:Aster", items=[
            unrelated, invitation, acceptance, venue], context_ids={unrelated["id"]})
        text = "When do you want to go to Lantern Cafe with Aster and Cedar?"
        diff = self.ask(source, text, ["L2", "L3", "L4"])

        question = todos.open_questions(self.conn)[0]
        self.assertEqual(question["text"], text)
        self.assertEqual({row[0] for row in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='question' AND ref=?",
            (question["key"],))}, {invitation["id"], acceptance["id"], venue["id"]})

        # Replaying the same proposal with a different context order is a no-op.
        source.items.reverse()
        apply.apply_diffs(self.conn, self.cfg, [(source, diff)], written_by="dream:test")
        self.assertEqual(len(todos.open_questions(self.conn)), 1)
        self.assertEqual(todos.open_questions(self.conn)[0]["text"], text)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM provenance WHERE kind='question'").fetchone()[0], 1)

    def test_a_literal_question_uses_its_cited_speaker(self):
        unrelated = self.line("When am I coming over again?", person="Bram", thread="group")
        relevant = self.line("When am I coming over again?")
        source = bundle.Bundle(entity="person:Aster", items=[unrelated, relevant])
        self.ask(source, "When am I coming over again?", ["L2"])
        self.assertEqual(todos.open_questions(self.conn)[0]["text"],
                         "Aster asked: When am I coming over again?")

    def test_an_inferred_question_uses_its_cited_speaker(self):
        unrelated = self.line("When am I coming over again?", person="Bram", thread="group")
        relevant = self.line("I am coming over again")
        source = bundle.Bundle(entity="person:Aster", items=[unrelated, relevant])
        self.ask(source, "When am I coming over again?", ["L2"])
        self.assertEqual(todos.open_questions(self.conn)[0]["text"],
                         "From Aster's messages: When am I coming over again?")

    def test_outgoing_evidence_cannot_be_attributed_to_the_recipient(self):
        unrelated = self.line("When are we visiting the cafe?", person="Bram", thread="group")
        relevant = self.line("I want to visit the cafe", from_me=True)
        source = bundle.Bundle(entity="person:Aster", items=[unrelated, relevant])
        text = "When are we visiting the cafe?"
        self.ask(source, text, ["L2"])
        self.assertEqual(todos.open_questions(self.conn)[0]["text"], text)

    def test_unusable_citations_do_not_fall_back_to_the_bundle_speaker(self):
        source = bundle.Bundle(entity="person:Aster", items=[
            self.line("When am I coming over again?")])
        text = "When am I coming over again?"
        for cites in ([], [999]):
            with self.subTest(cites=cites):
                self.assertEqual(apply._standalone_question(source, text, cite_ids=cites), text)

    def test_legacy_uncited_literal_question_still_keeps_its_context(self):
        source = bundle.Bundle(entity="person:Aster", items=[
            self.line("When am I coming over again?")])
        self.assertEqual(apply._standalone_question(source, "When am I coming over again?"),
                         "Aster asked: When am I coming over again?")


if __name__ == "__main__":
    unittest.main()
