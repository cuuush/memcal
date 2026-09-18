"""Dream names an otherwise-nameless sender when it draws a row from it.

The identity write itself is covered in test_whois; here the subject is the apply path
that turns a `threads` diff entry into a guess, and that the field survives routing.
"""

from __future__ import annotations

import unittest
from collections import Counter

from _support import Base

from memcal import identity
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


if __name__ == "__main__":
    unittest.main()
