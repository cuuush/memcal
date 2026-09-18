"""Dream names an otherwise-nameless sender when it draws a row from it.

The identity write itself is covered in test_whois; here the subject is the apply path
that turns a `threads` diff entry into a guess, and that the field survives routing.
"""

from __future__ import annotations

import re
import unittest
from collections import Counter

from _support import Base

from memcal import identity, llm
from memcal.dream import apply as apply_stage
from memcal.dream import propose
from memcal.dream.bundle import Bundle


def _row(channel, thread, handle, *, from_me=0, person=None, text="hi", rid=1):
    return {"id": rid, "channel": channel, "thread": thread, "handle": handle,
            "from_me": from_me, "person": person, "text": text}


class TestDreamNamesANamelessSender(Base):

    def _apply(self, bundle, threads):
        counts, log = Counter(), []
        apply_stage._apply_thread_names(self.conn, self.cfg, bundle,
                                        {"threads": threads}, counts=counts, log=log)
        return counts

    def test_names_a_lone_unresolved_sender(self):
        num = "+18005551212"
        bundle = Bundle(entity=f"thread:imessage:{num}",
                        items=[_row("imessage", num, num)])
        counts = self._apply(bundle, [{"channel": "imessage", "thread": num,
                                       "name": "Tire shop scheduling", "why": "reminder"}])
        self.assertEqual(counts["name:guessed"], 1)
        self.assertEqual(identity.resolve(self.conn, num), "Tire shop scheduling")

    def test_falls_back_to_the_bundle_thread_when_the_echo_is_off(self):
        num = "+18005551212"
        bundle = Bundle(entity=f"thread:imessage:{num}",
                        items=[_row("imessage", num, num)])
        # Model echoed a thread id it could not see exactly; the bundle's own thread wins.
        self._apply(bundle, [{"channel": "imessage", "thread": "guessed-wrong",
                              "name": "Dentist office", "why": ""}])
        self.assertEqual(identity.resolve(self.conn, num), "Dentist office")

    def test_skips_a_group_with_several_unnamed_handles(self):
        bundle = Bundle(entity="thread:imessage:grp",
                        items=[_row("imessage", "grp", "+1111"),
                               _row("imessage", "grp", "+2222", rid=2)])
        counts = self._apply(bundle, [{"channel": "imessage", "thread": "grp",
                                       "name": "Some group", "why": ""}])
        self.assertEqual(counts["name:guessed"], 0)
        self.assertIsNone(identity.resolve(self.conn, "+1111"))

    def test_does_not_touch_an_already_named_sender(self):
        num = "+18005551212"
        identity.link(self.conn, num, "Dad", source="contacts")
        bundle = Bundle(entity=f"thread:imessage:{num}",
                        items=[_row("imessage", num, num)])
        counts = self._apply(bundle, [{"channel": "imessage", "thread": num,
                                       "name": "Tire shop", "why": ""}])
        self.assertEqual(counts["name:guessed"], 0)
        self.assertEqual(identity.resolve(self.conn, num), "Dad")

    def test_the_off_switch_skips_naming(self):
        num = "+18005551212"
        self.cfg.dream_naming = False
        bundle = Bundle(entity=f"thread:imessage:{num}",
                        items=[_row("imessage", num, num)])
        self._apply(bundle, [{"channel": "imessage", "thread": num,
                              "name": "Tire shop", "why": ""}])
        self.assertIsNone(identity.resolve(self.conn, num))

    def test_routing_preserves_the_threads_field(self):
        bundle = Bundle(entity="thread:imessage:+18005551212",
                        items=[_row("imessage", "+18005551212", "+18005551212")])
        bid = propose.bundle_id(bundle.entity)
        payload = {"reviewed": [bid],
                   "diffs": [{"bundle": bid, "events": [], "todos": [], "wiki": [],
                              "questions": [], "series": [],
                              "threads": [{"channel": "imessage",
                                           "thread": "+18005551212",
                                           "name": "Tire shop", "why": ""}]}]}
        routed, _ = propose._route_v2([bundle], payload, [])
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0][1]["threads"][0]["name"], "Tire shop")


def _diff(bid, *, events=None, threads=None):
    return {"bundle": bid, "events": events or [], "todos": [], "wiki": [],
            "questions": [], "series": [], "threads": threads or []}


class TestNameGapEnforcement(unittest.TestCase):
    """The deterministic half: a row drawn from a nameless sender must be named."""

    def _bundle(self, entity, handles):
        items = [_row(entity.split(":")[1], entity.split(":", 2)[2], h, rid=i)
                 for i, h in enumerate(handles, 1)]
        return Bundle(entity=entity, items=items)

    def test_a_drawn_row_from_a_lone_nameless_sender_is_a_gap(self):
        b = self._bundle("thread:imessage:+18005551212", ["+18005551212"])
        bid = propose.bundle_id(b.entity)
        payload = {"diffs": [_diff(bid, events=[{"title": "Appt"}])]}
        self.assertIn(bid, propose._name_gaps([b], payload))

    def test_naming_it_closes_the_gap(self):
        b = self._bundle("thread:imessage:+18005551212", ["+18005551212"])
        bid = propose.bundle_id(b.entity)
        payload = {"diffs": [_diff(bid, events=[{"title": "Appt"}],
                                   threads=[{"channel": "imessage",
                                             "thread": "+18005551212",
                                             "name": "Tire shop", "why": ""}])]}
        self.assertEqual(propose._name_gaps([b], payload), {})

    def test_drawing_nothing_is_no_gap(self):
        b = self._bundle("thread:imessage:+18005551212", ["+18005551212"])
        bid = propose.bundle_id(b.entity)
        self.assertEqual(propose._name_gaps([b], {"diffs": [_diff(bid)]}), {})

    def test_a_group_is_not_a_gap(self):
        b = self._bundle("thread:imessage:grp", ["+1111", "+2222"])
        bid = propose.bundle_id(b.entity)
        payload = {"diffs": [_diff(bid, events=[{"title": "Party"}])]}
        self.assertEqual(propose._name_gaps([b], payload), {})

    def test_a_named_conversation_is_not_a_gap(self):
        b = Bundle(entity="thread:imessage:xyz",
                   items=[_row("imessage", "xyz", "+1", rid=1)])
        b.title = "Poker Crew"          # already reads as a name
        bid = propose.bundle_id(b.entity)
        payload = {"diffs": [_diff(bid, events=[{"title": "Poker"}])]}
        self.assertEqual(propose._name_gaps([b], payload), {})

    def test_repair_asks_for_and_fills_a_missing_name(self):
        b = self._bundle("thread:imessage:+18005551212", ["+18005551212"])
        bid = propose.bundle_id(b.entity)
        payload = {"diffs": [_diff(bid, events=[{"title": "Appt"}])]}

        class _Client:
            def complete(self, **kw):
                asked = re.search(r"BUNDLE ([0-9a-f]{6})",
                                  kw["turns"][-1]["content"]).group(1)
                return llm.Reply(
                    text="{}", data={"diffs": [{"bundle": asked, "threads": [
                        {"channel": "imessage", "thread": "+18005551212",
                         "name": "Tire shop scheduling", "why": "repair"}]}]},
                    usage=llm.Usage(calls=1), model="m",
                    generation_id="gen-r", finish_reason="stop")

        cfg = __import__("memcal.config", fromlist=["Config"]).Config(home=".")
        dummy = llm.Reply(text="{}", data=payload, usage=llm.Usage(calls=1),
                          model="m", generation_id="g0", finish_reason="stop")
        out, turn = propose._repair_thread_names(
            _Client(), cfg, "", "", [b], payload, dummy)
        self.assertIsNotNone(turn)
        names = out["diffs"][0]["threads"]
        self.assertEqual([n["name"] for n in names], ["Tire shop scheduling"])
        self.assertEqual(propose._name_gaps([b], out), {})   # gap now closed


if __name__ == "__main__":
    unittest.main()
