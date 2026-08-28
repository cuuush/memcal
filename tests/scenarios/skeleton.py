"""Define the synthetic temporal benchmark scenario."""

from __future__ import annotations

DAY1 = "2026-08-03"   # Monday
DAY2 = "2026-08-04"   # Tuesday
DAY3 = "2026-08-05"   # Wednesday
DAY4 = "2026-08-06"   # Thursday

#: Every fake day in order, so nothing has to name two of them and be wrong about the
#: rest. Days 3 and 4 carry almost no new traffic: their job is that time passed, which
#: a Monday/Tuesday story cannot express at all — a question cannot be asked on day 1
#: and answered by day-3 evidence, a plan's day cannot arrive and go by with nobody
#: saying anything, and a calendar cannot be re-scanned.
DAYS = (DAY1, DAY2, DAY3, DAY4)

#: name -> (handle, groupme user id, whatsapp jid). "me" is the owner.
CAST = {
    "me":            ("+19178889999", "88000", "19178889999@s.whatsapp.net"),
    "Jordan Lee": ("+19175550001", "88001", "19175550001@s.whatsapp.net"),
    "Alex Rivera":  ("+19175550002", "88002", "19175550002@s.whatsapp.net"),
    "Cameron Ortiz":    ("+19175550003", "88003", "19175550003@s.whatsapp.net"),
    "Harper":        ("+19175550004", "88004", "19175550004@s.whatsapp.net"),
    "Riley Morgan": ("+19175550005", "88005", "19175550005@s.whatsapp.net"),
    "Rowan Vale":    ("+19175550006", "88006", "19175550006@s.whatsapp.net"),
    "Alex Chen":     ("+19175550007", "88007", "19175550007@s.whatsapp.net"),
    "Skyler Reed":   ("+19175550008", "88008", "19175550008@s.whatsapp.net"),
    "Devon Park":    ("+19175550009", "88009", "19175550009@s.whatsapp.net"),
    "Bailey Stone":   ("+19175550010", "88010", "19175550010@s.whatsapp.net"),
    "Mom":           ("+19175550011", "88011", "19175550011@s.whatsapp.net"),
    "Jose":          ("+19175550012", "88012", "19175550012@s.whatsapp.net"),
    "Quinn Brooks": ("+19175550013", "88013", "19175550013@s.whatsapp.net"),
    "Rae":           ("+19175550014", "88014", "19175550014@s.whatsapp.net"),
    "Morgan":        ("+19175550015", "88015", "19175550015@s.whatsapp.net"),
    "Nadia Okoro":   ("+19175550016", "88016", "19175550016@s.whatsapp.net"),
    "Sasha Kim":     ("+19175550017", "88017", "19175550017@s.whatsapp.net"),
    # Beat 54. A WhatsApp **LID**: fifteen digits, not a phone number, matching no
    # contact and carrying no push name. The readable key is for the answer key only —
    # `NAMELESS` is what stops the fixture shipping a name the real feed never sends.
    "Unnameable Neighbour": ("+261516951601296", "88018",
                             "261516951601296@lid"),
}

#: Cast whose sources supply **no display name at all**, so the fixtures ship the id and
#: nothing else. Every fixture in the repo used to give every participant a name, which
#: is a shape the real feed has never sent — the same reason beat 53 exists one field
#: over. Without this the LID case cannot be written down.
NAMELESS = frozenset({"Unnameable Neighbour"})

#: GroupMe's own announcement channel. Not a person, and the display name it arrives
#: under is the app's. Beat 55.
SYSTEM_SENDER = "GroupMe"

#: Who is in which group, so participant lists and rosters are real.
GROUPS = {
    ("gm", "poker crew"):    ["me", "Jordan Lee", "Alex Rivera", "Cameron Ortiz"],
    ("gm", "brunch sunday"): ["me", "Skyler Reed", "Devon Park"],
    ("gm", "beer garden"):   ["me", "Cameron Ortiz", "Alex Rivera"],
    ("gm", "block party"):   ["me", "Devon Park", "Bailey Stone"],
    ("gm", "smash bros"):    ["me", "Jordan Lee", "Alex Rivera", "Cameron Ortiz",
                              "Riley Morgan", SYSTEM_SENDER],
    ("gm", "board game night"): ["me", "Jose", "Quinn Brooks"],
    ("gm", "emoji noise"):   ["me", "Jose", "Quinn Brooks"],
    ("gm", "rave chat"):     ["me", "Jordan Lee", "Alex Rivera", "Cameron Ortiz",
                              "Riley Morgan", "Jose"],
    ("wa", "dinner thu"):    ["me", "Alex Rivera", "Cameron Ortiz", "Riley Morgan"],
    ("wa", "morgan family"): ["me", "Riley Morgan", "Mom"],
    ("wa", "doggo park"):    ["me", "Rae", "Unnameable Neighbour"],
}

# --------------------------------------------------------------------------------
# The tone guide the writers work to. Kept here rather than in the prompt so every
# batch is written against the same one.
# --------------------------------------------------------------------------------
TONE = {
    "gm": "group chat. lowercase, fast, fragments, people talk over each other",
    "wa": "group chat, slightly more punctuated than groupme",
    "bb": "one-to-one texting. short, casual, some typos, occasional autocorrect",
    "em": "real email prose, appropriate to the sender",
    # Two speech acts, not one, and the generator was told about only the first. Every
    # agent fixture line was them stating a fact to be remembered, so "go and do this for
    # me" had no representation in the corpus at all and the benchmark could not see the
    # class of defect in #57. Delegation carries `text=` inline instead of prose.
    "agent": "them talking to an assistant: stating things to be remembered, and "
             "handing it work to go and do. complete sentences",
}

# --------------------------------------------------------------------------------
# SIGNAL — the numbered challenges. Order within a thread is chronological.
# --------------------------------------------------------------------------------

SIGNAL = [
    # -- 1/3. Poker: established Fri 8pm at Jordan's, then moved to Sat, then moved
    #    house to 42 Example Street. Three fields on one row, changed across two days.
    dict(id="pk01", day=1, time="18:12", src="gm", thread="poker crew", who="Jordan Lee",
         beat="poker.established",
         say="proposes poker at their place this friday, around 8. casual, first message of the thread"),
    dict(id="pk02", day=1, time="18:14", src="gm", thread="poker crew", who="Alex Rivera",
         beat="poker.established", say="in. one word or close to it"),
    dict(id="pk03", day=1, time="18:15", src="gm", thread="poker crew", who="Cameron Ortiz",
         beat="poker.established",
         say="asks what the buy-in is. logistics chatter, no new date"),
    dict(id="pk04", day=1, time="18:16", src="gm", thread="poker crew", who="Jordan Lee",
         beat="poker.established", say="answers the buy-in, says 40 like always"),
    dict(id="pk05", day=1, time="18:31", src="gm", thread="poker crew", who="me",
         beat="poker.established", say="says the user's in, friday works"),
    dict(id="pk06", day=1, time="18:33", src="gm", thread="poker crew", who="Cameron Ortiz",
         beat=None, say="jokes about losing money last time. no plan content"),

    dict(id="pk10", day=2, time="11:04", src="bb", thread="Jordan Lee", who="Jordan Lee",
         beat="poker.date-moved",
         say="tells them the game is moving to saturday instead of friday, same time. "
             "gives a throwaway reason (someone can't do friday). does NOT restate the date "
             "as a number - says 'saturday'"),
    dict(id="pk11", day=2, time="11:09", src="bb", thread="Jordan Lee", who="me",
         beat="poker.date-moved", say="fine by them, short acknowledgement"),
    dict(id="pk12", day=2, time="11:22", src="bb", thread="Jordan Lee", who="Jordan Lee",
         beat="poker.location-moved",
         say="follow-up: it's actually at Alex's place now, not their. gives the address "
             "42 Example Street. mentions their apartment is getting painted or similar"),
    dict(id="pk13", day=2, time="11:24", src="bb", thread="Jordan Lee", who="me",
         beat="poker.location-moved", say="acknowledges, asks if there's parking"),
    dict(id="pk14", day=2, time="11:26", src="bb", thread="Jordan Lee", who="Jordan Lee",
         beat=None, say="says just take the train, no new plan content"),

    # -- 10. The decoy. Someone late to the group thread reads the old plan back.
    #    The row must not walk backwards to Friday.
    dict(id="pk20", day=2, time="19:40", src="gm", thread="poker crew", who="Cameron Ortiz",
         beat="poker.backwards-decoy",
         say="confused, asks wasn't it friday? the user missed the message about the move"),
    dict(id="pk21", day=2, time="19:44", src="gm", thread="poker crew", who="Alex Rivera",
         beat="poker.backwards-decoy",
         say="corrects them, it's saturday now and at their place"),

    # -- 13. Jordan's address slot, stated day 1 and corrected day 2.
    dict(id="cn01", day=1, time="21:02", src="bb", thread="Jordan Lee", who="Jordan Lee",
         beat="jordan.moved-eastwood",
         say="mentions in passing that the user finally moved, the user's in Eastwood now"),
    dict(id="cn02", day=1, time="21:05", src="bb", thread="Jordan Lee", who="me",
         beat=None, say="asks how the move went"),
    dict(id="cn03", day=2, time="11:31", src="bb", thread="Jordan Lee", who="Jordan Lee",
         beat="jordan.moved-riverton",
         say="correction: the eastwood lease fell through, the user's actually in Riverton. "
             "annoyed about it"),

    # -- 2/6/11. Thursday dinner: time moves, roster changes, and the same evening is
    #    referenced from three streams on day 2.
    dict(id="dn01", day=1, time="12:40", src="wa", thread="dinner thu", who="Alex Rivera",
         beat="dinner.established",
         say="proposes dinner thursday at 7. names a restaurant loosely ('that ramen place')"),
    dict(id="dn02", day=1, time="12:44", src="wa", thread="dinner thu", who="Cameron Ortiz",
         beat="dinner.established", say="in"),
    dict(id="dn03", day=1, time="12:51", src="wa", thread="dinner thu", who="me",
         beat="dinner.established", say="in, 7 works"),
    dict(id="dn04", day=1, time="13:02", src="wa", thread="dinner thu", who="Riley Morgan",
         beat=None, say="asks if it's the one on 9th. logistics, no new plan"),

    dict(id="dn10", day=2, time="10:15", src="wa", thread="dinner thu", who="Alex Rivera",
         beat="dinner.time-moved",
         say="asks to push thursday to 8:30, the user can't get there for 7"),
    dict(id="dn11", day=2, time="10:18", src="wa", thread="dinner thu", who="me",
         beat="dinner.time-moved", say="agrees, 8:30 is fine"),
    dict(id="dn12", day=2, time="10:41", src="wa", thread="dinner thu", who="Cameron Ortiz",
         beat="dinner.roster-changed",
         say="the user's out for thursday, something came up. suggests Riley take their spot"),
    dict(id="dn13", day=2, time="10:47", src="wa", thread="dinner thu", who="Riley Morgan",
         beat="dinner.roster-changed", say="says the user will come then"),

    # -- 9. The decoy that must not move the beer garden. Vague, no noun, no referent,
    #    said by someone who IS in the beer garden plan, in a different thread.
    dict(id="dn20", day=2, time="10:52", src="wa", thread="dinner thu", who="Cameron Ortiz",
         beat="beergarden.decoy",
         say="vague throwaway: says they should just do the thing next weekend instead. "
             "CRITICAL - names no event, no place, no day. it is deliberately ambiguous "
             "and refers to nothing identifiable"),

    # -- 11. Same Thursday dinner, from two more streams on day 2.
    dict(id="dn30", day=2, time="15:20", src="bb", thread="Alex Rivera", who="Alex Rivera",
         beat="dinner.third-stream",
         say="DM: confirms the user booked the table for thursday at 8:30, asks them to confirm headcount"),
    dict(id="dn31", day=2, time="15:26", src="bb", thread="Alex Rivera", who="me",
         beat="dinner.third-stream", say="says four of them"),

    # -- 4. Climbing: mentioned day 1, confirmed day 2.
    dict(id="cl01", day=1, time="20:10", src="bb", thread="Riley Morgan", who="me",
         beat="climbing.mentioned",
         say="floats it loosely - the user might do the climbing gym wednesday. non-committal"),
    dict(id="cl02", day=1, time="20:14", src="bb", thread="Riley Morgan", who="Riley Morgan",
         beat="climbing.mentioned", say="says maybe, depends on work"),
    dict(id="cl10", day=2, time="17:05", src="bb", thread="Riley Morgan", who="Riley Morgan",
         beat="climbing.confirmed", say="asks if the user's still on for climbing tomorrow"),
    dict(id="cl11", day=2, time="17:31", src="bb", thread="Riley Morgan", who="me",
         beat="climbing.confirmed", say="yes, locked in. definite"),

    # -- 8/19. Movie Tuesday the 11th. The user tells the assistant the user is going before the user
    #    tells Riley (see ACTIONS ac01), so the row exists, confirmed and written by
    #    `live`, before any dream pass has read a word of it. Cancelled on day 2.
    dict(id="mv01", day=1, time="20:20", src="bb", thread="Riley Morgan", who="Riley Morgan",
         beat="movie.established",
         say="suggests the movie next tuesday, the 11th. names it as tuesday the 11th"),
    dict(id="mv015", day=1, time="20:21", src="agent", thread="conversation", who="me",
         beat="movie.agent-confirmed",
         say="tells their assistant the user is going to the movie with Riley on tuesday the "
             "11th and to put it on the calendar. plain and deliberate"),
    dict(id="mv02", day=1, time="20:22", src="bb", thread="Riley Morgan", who="me",
         beat="movie.established", say="agrees"),
    dict(id="mv10", day=2, time="17:35", src="bb", thread="Riley Morgan", who="Riley Morgan",
         beat="movie.cancelled",
         say="the tuesday movie is off - that theater is closed for renovation. "
             "explicitly cancels it, offers no replacement date"),

    # -- 5. Brunch: settled publicly day 1, declined privately day 2, and the group
    #    thread carries on afterwards as though nothing happened.
    dict(id="br01", day=1, time="16:02", src="gm", thread="brunch sunday", who="Skyler Reed",
         beat="brunch.established", say="proposes brunch sunday, 11am. names a place"),
    dict(id="br02", day=1, time="16:05", src="gm", thread="brunch sunday", who="Devon Park",
         beat="brunch.established", say="in, enthusiastic"),
    dict(id="br03", day=1, time="16:20", src="gm", thread="brunch sunday", who="me",
         beat="brunch.established", say="in"),
    dict(id="br04", day=1, time="16:22", src="gm", thread="brunch sunday", who="Skyler Reed",
         beat="brunch.established", say="says she'll book for three"),

    dict(id="br10", day=2, time="13:10", src="bb", thread="Skyler Reed", who="me",
         beat="brunch.declined-privately",
         say="private DM: the user can't make sunday anymore. asks her not to say anything to "
             "the group yet. slightly awkward, gives a vague reason"),
    dict(id="br11", day=2, time="13:14", src="bb", thread="Skyler Reed", who="Skyler Reed",
         beat="brunch.declined-privately", say="says no worries, she won't say anything"),
    # Later, louder, public, and wrong. Must not overwrite the private decline.
    dict(id="br20", day=2, time="18:45", src="gm", thread="brunch sunday", who="Devon Park",
         beat="brunch.public-decoy",
         say="cheerful group message looking forward to sunday, says see everyone there. "
             "she does not know the user dropped out"),
    dict(id="br21", day=2, time="18:50", src="gm", thread="brunch sunday", who="Skyler Reed",
         beat="brunch.public-decoy", say="reacts noncommittally, keeps their secret"),

    # -- 9 (the target). Beer garden, settled and confirmed on day 1. Nothing on day 2
    #    touches it. Cameron's vague "next weekend" in the dinner thread is the only
    #    thing that could pull it, and it must not.
    dict(id="bg01", day=1, time="17:10", src="gm", thread="beer garden", who="Cameron Ortiz",
         beat="beergarden.established",
         say="proposes the beer garden saturday afternoon, 3pm"),
    dict(id="bg02", day=1, time="17:12", src="gm", thread="beer garden", who="Alex Rivera",
         beat="beergarden.established", say="in, says the user will be there"),
    dict(id="bg03", day=1, time="17:19", src="gm", thread="beer garden", who="me",
         beat="beergarden.established", say="confirms the user's coming saturday"),
    dict(id="bg04", day=1, time="17:25", src="gm", thread="beer garden", who="Cameron Ortiz",
         beat="beergarden.established", say="says the user will grab a table, done deal"),

    # -- 7. BBQ: GroupMe on day 1, moved by a Partiful email on day 2.
    dict(id="bq01", day=1, time="09:30", src="gm", thread="block party", who="Devon Park",
         beat="bbq.established",
         say="reminds the chat about the block party bbq on saturday the 15th at 2pm"),
    dict(id="bq02", day=1, time="09:41", src="gm", thread="block party", who="Bailey Stone",
         beat=None, say="asks what to bring"),
    dict(id="bq03", day=1, time="09:44", src="gm", thread="block party", who="Devon Park",
         beat=None, say="says just drinks"),

    # -- 12. The EZ-Pass to-do and its wake condition.
    dict(id="ez01", day=1, time="08:05", src="agent", thread="conversation", who="me",
         beat="ezpass.opened",
         say="tells their assistant the user needs to give Rowan back their EZ-Pass when Rowan is "
             "back from Italy. states it as something to remember"),
    dict(id="ez10", day=2, time="07:12", src="bb", thread="Rowan Vale", who="Rowan Vale",
         beat="ezpass.woken",
         say="just landed back from italy, jet lagged. says nothing about the ezpass"),
    dict(id="ez11", day=2, time="07:40", src="bb", thread="Rowan Vale", who="me",
         beat=None, say="welcomes them back, asks how the trip was"),

    # -- 56. Work handed off. The doer is the assistant, so nothing is owed after the
    #    session ends and no to-do may be opened — while ez01 above and sh02 below are
    #    the same grammar in the same bundle and must still open theirs, because the
    #    doer in those is them. Inline text: this is a regression case, not prose.
    dict(id="dl01", day=1, time="08:12", src="agent", thread="conversation", who="me",
         beat="delegated.handed-off",
         text="go ahead and file the vet insurance claim for Comet, the receipts are "
              "all in my email. just handle it and tell me when it's done"),

    # -- 20/21. The Bowery show. The whole beat the agent stream was missing: an
    #    invitation arrives, the user settles it with their assistant (ACTIONS ac02 + ac03 —
    #    a row and a to-do, both typed, both model-free), the user answers the friend a
    #    minute later, and the next day the same plan comes back at them from that
    #    friend's own mouth. Nothing on day 2 is news, and the pass has to know it.
    dict(id="sh01", day=1, time="17:45", src="bb", thread="Cameron Ortiz", who="Cameron Ortiz",
         beat="show.invited",
         say="has a spare ticket to a show at Bowery Ballroom on friday, asks if the user wants it"),
    dict(id="sh02", day=1, time="17:52", src="agent", thread="conversation", who="me",
         beat="show.agent-settled",
         say="tells their assistant: yes to Cameron's show friday, 8pm at Bowery Ballroom, "
             "put it on the calendar, and remind them to venmo Cameron for the ticket"),
    dict(id="sh03", day=1, time="17:55", src="bb", thread="Cameron Ortiz", who="me",
         beat="show.invited", say="tells Cameron the user's in and will venmo them for it"),
    dict(id="sh10", day=2, time="12:10", src="bb", thread="Cameron Ortiz", who="Cameron Ortiz",
         beat="show.day2-echo",
         say="friday reminder: doors at 8 for the bowery show, the user will meet them there. "
             "says nothing new — same day, same time, same place"),
    dict(id="sh11", day=2, time="12:14", src="bb", thread="Cameron Ortiz", who="me",
         beat="show.day2-echo", say="short acknowledgement, see you then"),
    dict(id="sh12", day=2, time="18:22", src="agent", thread="conversation", who="me",
         beat="show.agent-closed",
         say="tells their assistant the user sent Cameron the money for the ticket, so that is "
             "done. states it as finished, not as a plan"),

    # -- 14. The mom trap. Bailey is a family friend who mentions their mother.
    #    Nothing may conclude that Bailey IS their mother.
    dict(id="db01", day=1, time="14:20", src="bb", thread="Bailey Stone", who="Bailey Stone",
         beat="mom-trap",
         say="warm, chatty. mentions she ran into their mom at the farmers market on "
             "sunday and she says hi. she is a family friend, NOT their mother, and nothing "
             "in the message should say what she is to them"),
    dict(id="db02", day=1, time="14:35", src="bb", thread="Bailey Stone", who="me",
         beat=None, say="friendly reply, says the user should call her"),
    dict(id="mm01", day=2, time="09:50", src="bb", thread="Mom", who="Mom",
         beat="mom-trap",
         say="their actual mother, about her birthday dinner later this month. warm, "
             "a little guilt-trippy. no specific date given"),

    # -- 15. The Comet trap. Standing says Comet is their dog.
    dict(id="sn01", day=2, time="12:05", src="bb", thread="Harper", who="Harper",
         beat="comet-trap",
         say="asks how Comet held up after the trail ride yesterday. the phrasing makes "
             "a horse sound plausible. nobody says what Comet is"),

    # -- 22–26. Literal regression records. Exact wording matters in these cases, so
    #    they live here instead of in a prose batch. Adding the next production failure
    #    should take one record, one integration answer, and one check.
    dict(id="rx01", day=1, time="14:51", src="gm", thread="board game night", who="Jose",
         beat="reaction-context", text="👀"),
    dict(id="rx02", day=1, time="14:57", src="gm", thread="board game night",
         who="Quinn Brooks", beat="reaction-context",
         text="I mean, I'm free most of early August"),
    dict(id="rx03", day=1, time="15:08", src="gm", thread="board game night", who="Jose",
         beat="reaction-context",
         text="board game night at my place Saturday August 8? I can host"),

    dict(id="as01", day=1, time="10:18", src="wa", thread="doggo park", who="Rae",
         beat="aspca-context",
         text="ASPCA mobile clinic at our Doggo Park run Wednesday 10–3, "
              "129th Street entrance"),

    # -- 54. The group has a member nobody can name.
    #    A WhatsApp LID: fifteen digits, no push name, matching no contact. The plan is
    #    real and the proposer is unnameable, and both halves have to survive.
    dict(id="nn01", day=1, time="09:05", src="wa", thread="doggo park",
         who="Unnameable Neighbour", beat="nameless.proposes",
         text="Anyone up for the park Saturday morning? I'll be there from 9 with Biscuit"),
    dict(id="nn02", day=1, time="09:11", src="wa", thread="doggo park", who="Rae",
         beat="nameless.proposes",
         text="Yes! 9 works, see you at the 129th entrance"),

    # -- 55. The platform is not a person, and sometimes it is quoting one.
    #    Both lines arrive on `groupme:system` under the display name "GroupMe". The
    #    first is pure noise; the second carries the plan. A filter that drops the
    #    handle drops the plan with it.
    dict(id="sy01", day=1, time="19:40", src="gm", thread="smash bros",
         who=SYSTEM_SENDER, beat="platform.not-a-person",
         text="A message was deleted."),
    dict(id="sy02", day=2, time="20:40", src="gm", thread="smash bros",
         who=SYSTEM_SENDER, beat="platform.quotes-a-person",
         text="Riley Morgan edited to: \u201cactually let\u2019s make smash 7pm Sunday "
              "at mine\u201d"),

    dict(id="qu01", day=1, time="16:10", src="bb", thread="Quinn Brooks",
         who="Quinn Brooks", beat="wiki-facts",
         text="Alamo Drafthouse is my favorite movie theater. My favorite Pokemon set "
              "is Team Rocket."),
    dict(id="qu02", day=1, time="16:14", src="bb", thread="Quinn Brooks", who="me",
         beat="transient-permission",
         text="You can borrow my car Friday so you can drive Katie to Medieval Times."),
    dict(id="qu04", day=1, time="16:18", src="bb", thread="Quinn Brooks",
         who="Quinn Brooks", beat="wiki-relationship",
         text="Katie is my sister—everyone calls her Kat."),
    # Counterweight to the one-trip permission above. A structural filter that rejects
    # every sentence containing "can borrow" fixes qu02 by throwing away a real durable
    # policy too. The benchmark deliberately asks for both judgements.
    dict(id="qu03", day=2, time="16:14", src="bb", thread="Quinn Brooks", who="me",
         beat="durable-permission",
         text="For future reference, Quinn can borrow my car anytime the user needs it."),

    # Encounter history is a projection of real rows, not prose copied onto the wiki.
    # A reschedule elsewhere in this corpus must not make one poker night count twice.
    dict(id="hx01", day=1, time="08:31", src="agent", thread="conversation", who="me",
         beat="encounter-history",
         text="I played poker with Quinn at Robbie's house on June 5."),
    dict(id="hx02", day=1, time="08:32", src="agent", thread="conversation", who="me",
         beat="encounter-history",
         text="I got dinner with Quinn at Xi'an Famous Foods on June 20."),
    dict(id="hx03", day=1, time="08:33", src="agent", thread="conversation", who="me",
         beat="encounter-history",
         text="I went to board game night with Quinn on July 18."),

    # A visually identical reaction with no topic around it is the negative half of
    # challenge 22. Context should rescue Jose's first eyes, not every pair of eyes.
    dict(id="rx10", day=1, time="11:02", src="gm", thread="emoji noise", who="Jose",
         beat="reaction-no-context", text="👀"),

    dict(id="mq01", day=2, time="09:56", src="bb", thread="Mom", who="Mom",
         beat="standalone-question", text="When am I coming over again?"),

    # -- 35. Generic event language creates a row, but a room roster is not an
    #    attendance list. The distinctive name lets a later DM find and move it.
    dict(id="ne01", day=1, time="19:02", src="gm", thread="rave chat",
         who="Jordan Lee", beat="generic-group-event",
         text="hey guys there's a party tomorrow at 9 at Elsewhere called Neon Garden. "
              "I can't go but figured some of you might want it"),
    dict(id="ne02", day=1, time="19:04", src="gm", thread="rave chat",
         who="Cameron Ortiz", beat="generic-group-event",
         text="not me, working tomorrow"),
    dict(id="ne10", day=2, time="10:02", src="bb", thread="Jose",
         who="Jose", beat="generic-cross-channel-update",
         text="Neon Garden party moved from tonight to tomorrow, Wednesday August 5. "
              "still can't make it"),

    # -- 36. The row already exists without source, attendees or an end date. A
    #    distinctive group-chat mention must enrich that same row. Only Alex explicitly
    #    says the user is attending; the other four room members remain retrieval context.
    dict(id="el01", day=1, time="19:12", src="gm", thread="rave chat",
         who="Alex Rivera", beat="existing-group-event",
         text="Elements Music Festival is Friday August 7 through Sunday August 9 at "
              "Cedar Falls. Casey and I are going; that doesn't mean everyone in this "
              "chat is going"),

    # -- 37. A confirmed plan can still have an unresolved prerequisite.
    dict(id="tk01", day=2, time="10:35", src="bb", thread="Morgan",
         who="Morgan", beat="ticket-reese-proof",
         text="got our Spider-Man tickets. AMC Lincoln Square Monday Aug 10 at 7:40pm, "
              "seats H8 and H9"),

    # -- 16. Junk that must produce nothing, both days.
    dict(id="jk01", day=1, time="22:40", src="bb", thread="Harper", who="Harper",
         beat="junk.affection", say="says i love you, misses them. pure affection"),
    dict(id="jk02", day=1, time="22:41", src="bb", thread="Harper", who="me",
         beat="junk.affection", say="says it back"),
    dict(id="jk03", day=2, time="23:10", src="bb", thread="Harper", who="Harper",
         beat="junk.affection", say="goodnight, misses them again"),
    dict(id="jk04", day=1, time="11:11", src="bb", thread="262966", who="Amazon",
         beat="junk.shortcode",
         say="automated SMS shortcode: a package is arriving tomorrow, with a tracking "
             "link. reads exactly like a real Amazon delivery text"),
    dict(id="jk05", day=2, time="11:11", src="bb", thread="262966", who="Amazon",
         beat="junk.shortcode", say="another one: package delivered, left at the door"),
    dict(id="jk06", day=2, time="16:00", src="bb", thread="Alex Chen", who="Alex Chen",
         beat="junk.opinion",
         say="work colleague being unsolicited about their career, says the user should be "
             "looking for something better. an opinion about them, never a stored fact"),

    # ----------------------------------------------------------------------------
    # Days 3 and 4. Deliberately thin: what these days are for is that time passed,
    # not that more was said. Every line below serves a beat; there is no day-3 or
    # day-4 filler, because the filler that matters is the two days already behind it
    # still sitting in the store.
    # ----------------------------------------------------------------------------

    # -- 41. A question asked on day 1 is answered by day-3 evidence. The row has no
    #    location on day 1 and memcal asks for it; on day 3 Devon simply sends it.
    dict(id="hw01", day=1, time="19:15", src="bb", thread="Devon Park", who="Devon Park",
         beat="housewarming.no-address",
         say="their housewarming is saturday the 15th. the user will send the address later, "
             "the user hasn't got the keys yet. does NOT give an address"),
    dict(id="hw02", day=1, time="19:17", src="bb", thread="Devon Park", who="me",
         beat=None, say="says the user will be there, asks what to bring"),
    dict(id="hw03", day=3, time="12:40", src="bb", thread="Devon Park", who="Devon Park",
         beat="housewarming.address-arrives",
         say="sends the address for the housewarming: 55 Linden Avenue. nothing else "
             "changes, same day, same plan"),

    # -- 44. A recurring appointment's owner moves it, and the row follows without
    #    anyone telling memcal. e125 did this correctly on the live store and it is
    #    here so it stays correct.
    dict(id="ph01", day=1, time="09:30", src="bb", thread="Nadia Okoro",
         who="Nadia Okoro", beat="physio.booked",
         say="physio clinic confirming their session for wednesday the 12th at 5pm. "
             "brief, professional, mentions it is the weekly slot"),
    dict(id="ph02", day=3, time="10:05", src="bb", thread="Nadia Okoro",
         who="Nadia Okoro", beat="physio.moved",
         say="clinic has to move their physio to wednesday the 19th, same time. "
             "apologises. does not change anything else"),
    dict(id="ph03", day=3, time="10:09", src="bb", thread="Nadia Okoro", who="me",
         beat=None, say="says that's fine"),

    # -- 45. The invitation the calendar carries with no location, settled in a DM the
    #    same evening. Day 3's rescan re-derives it and must learn nothing.
    dict(id="jk30", day=1, time="21:40", src="bb", thread="Morgan", who="Morgan",
         beat="jack30.settled",
         say="asks if the user's going to jack's 30th on the 22nd. she is definitely going"),
    dict(id="jk31", day=1, time="21:43", src="bb", thread="Morgan", who="me",
         beat="jack30.settled",
         say="yes, the user's going, definitely. no hedging at all"),

    # -- 46. The oblique obligation, buried in a busy thread about nothing. Every other
    #    obligation in this corpus is stated plainly in a quiet thread, so nothing here
    #    tested the case the real corpus is made of.
    dict(id="ob01", day=3, time="20:02", src="gm", thread="smash bros", who="Alex Rivera",
         beat=None, say="complaining about lag in the last game"),
    dict(id="ob02", day=3, time="20:03", src="gm", thread="smash bros", who="Cameron Ortiz",
         beat=None, say="blames their wifi, says the user's getting a new router"),
    dict(id="ob03", day=3, time="20:04", src="gm", thread="smash bros", who="me",
         beat="oblique.obligation",
         say="mid-banter, as an aside: the user still owes devon the deposit and has to send "
             "it before saturday. buried in the middle of a sentence about the game, "
             "never announced as a task"),
    dict(id="ob04", day=3, time="20:05", src="gm", thread="smash bros", who="Jordan Lee",
         beat=None, say="ignores it completely, asks who is up next"),
    dict(id="ob05", day=3, time="20:07", src="gm", thread="smash bros", who="Alex Rivera",
         beat=None, say="calls next game, trash talk"),

    # -- 49. An obligation accepted in a work DM, in among standup chatter.
    #    These six lines were filler until 2026-08-05, when gpt-5.6-terra
    #    extracted the ticket as a to-do and the answer key called that a fault.
    #    The traffic is unchanged; what changed is that it is now graded.
    dict(id="wk01", day=1, time="09:20", src="bb", thread="Alex Chen", who="Alex Chen",
         beat=None, text='hey did you see the standup recording',
         say="see BEATS 49"),
    dict(id="wk02", day=1, time="09:23", src="bb", thread="Alex Chen", who="me",
         beat='work.obligation-asked', text='ya just watched',
         say="see BEATS 49"),
    dict(id="wk03", day=1, time="09:25", src="bb", thread="Alex Chen", who="Alex Chen",
         beat=None, text='ok cool, i need you to look at this jira ticket when you get a sec',
         say="see BEATS 49"),
    dict(id="wk04", day=1, time="09:31", src="bb", thread="Alex Chen", who="me",
         beat=None, text='which one',
         say="see BEATS 49"),
    dict(id="wk05", day=1, time="09:32", src="bb", thread="Alex Chen", who="Alex Chen",
         beat='work.obligation-asked', text='sec-2847. also sent you a link in slack',
         say="see BEATS 49"),
    dict(id="wk06", day=1, time="09:35", src="bb", thread="Alex Chen", who="me",
         beat='work.obligation-accepted', text='got it, will check it out',
         say="see BEATS 49"),

    # -- 50. Stakes. The user does not forget this one because forgetting it costs a
    #    deposit and lets a person down — which is the whole reason memcal exists.
    dict(id="tt01", day=1, time="15:02", src="bb", thread="Sasha Kim", who="Sasha Kim",
         beat="tattoo.booked",
         say="tattoo artist confirming their session for tuesday the 18th at 2pm. "
             "mentions the 100 dollar deposit is non-refundable and she needs "
             "48 hours notice to move it. warm but businesslike"),
    dict(id="tt02", day=1, time="15:06", src="bb", thread="Sasha Kim", who="me",
         beat="tattoo.booked", say="confirms, says the user will be there"),
    # -- 57. An explicit later amendment carries a full replacement weekday and time.
    # It must update the same commitment beat 50 established, rather than add another.
    dict(id="tt10", day=2, time="10:18", src="bb", thread="Sasha Kim", who="Sasha Kim",
         beat="tattoo.explicit-amendment",
         say="provider asks to move the appointment and offers thursday august 20th at "
             "4:15pm instead. ordinary conversational wording; do not explain that this "
             "is an update or contrast it with a new booking"),


    # -- 47. A thing that happens *inside* another thing. Three live rows were all
    #    called "Elements" and read as three unrelated plans on one weekend.
    dict(id="el30", day=3, time="13:20", src="gm", thread="rave chat", who="Alex Rivera",
         beat="elements.breakfast",
         say="says a few of them are doing breakfast at elements saturday morning, "
             "9ish, before the sets start. names it as part of the festival weekend"),
    dict(id="el31", day=3, time="13:22", src="gm", thread="rave chat", who="me",
         beat="elements.breakfast", say="the user's in for breakfast"),
]

# --------------------------------------------------------------------------------
# CALENDAR — Calendar.app's own answer, one complete snapshot per day.
#
# The largest coverage hole the corpus had: 114 of 127 live rows come from the iCal
# and Partiful connectors and neither ran here at all, so RSVP inference and rescan
# behaviour had zero coverage in the free deterministic gate. `ical.ingest_snapshot`
# takes a plain list of item dicts, so no JXA and no Calendar.app is involved.
#
# `days` lists the days an item is on the calendar, because a snapshot is a statement
# about the whole calendar and disappearance is how Partiful expresses a decline.
#
# **`calendar_key` is not derived from `calendar_name`, and that is the point.** Every
# fixture in the repo computed `calendar_uid` as `f"calendar-{name.lower()}"`, so a
# calendar *rename* was not expressible — which is exactly why the benchmark could not
# have caught the most expensive bug in the project. Here the key is stable and the
# name moves, so day 3 renames a calendar and day 4 proves nothing duplicated.
# --------------------------------------------------------------------------------

CALENDAR = [
    # 45. A Partiful invitation with no location, so the RSVP inference reads it as
    # "not replied": opportunity/mentioned. The DM on day 1 settles it. On day 3 the
    # calendar is renamed, every revision changes, and the connector re-derives from
    # the same absent location it read the first time.
    dict(id="cl01", uid="PARTIFUL-JACK30", days=(1, 2, 3, 4),
         calendar={1: "Partiful", 2: "Partiful", 3: "Partiful Invites",
                   4: "Partiful Invites"},
         calendar_key="cal-invites-8821", writable=False,
         title="Jack's 30th", date="2026-08-22", time="19:00", hours=4,
         location="", url="https://partiful.com/e/jacks30",
         beat="jack30.rescan"),

    # 52. The other half of the join-link beat, and the half a model is not involved
    # in: the appointment is on the calendar, its location is the word "Online",
    # and the only thing that says how to attend is the description. The JXA has always
    # lifted `description`; `_normalized` threw it away, so it reached nothing.
    # Present on all four days so the day-3 rename proves a rescan cannot re-derive the
    # link back out of the row.
    dict(id="cl05", uid="PERSONAL-TUTORING", days=(1, 2, 3, 4),
         calendar={1: "Personal", 2: "Personal", 3: "Home", 4: "Home"},
         calendar_key="cal-personal-4410", writable=True,
         title="Tutoring appointment", date="2026-08-12", time="12:00", hours=1,
         location="Online", url="",
         description="Join Zoom Meeting\nhttps://us02web.zoom.example/j/8842119",
         beat="tutoring.link-from-calendar"),

    # 52, the half with no URL in it. "what if this exact same thing happens but with
    # something that ISN'T a url" — the connector was not dropping links, it was
    # dropping the description, and a buzzer number is lost exactly as completely.
    dict(id="cl06", uid="PERSONAL-BLOODWORK", days=(1, 2, 3, 4),
         calendar={1: "Personal", 2: "Personal", 3: "Home", 4: "Home"},
         calendar_key="cal-personal-4410", writable=True,
         title="Bloodwork", date="2026-08-06", time="09:00", hours=1,
         location="Riverton Labs", url="",
         description="Suite 300, ring buzzer 4. Bring your insurance card.",
         beat="tutoring.detail-from-calendar"),

    # The same rename, over a plain personal event nothing else touches. Its whole job
    # is to prove the rename produced no second row.
    dict(id="cl02", uid="PERSONAL-DENTIST", days=(1, 2, 3, 4),
         calendar={1: "Personal", 2: "Personal", 3: "Home", 4: "Home"},
         calendar_key="cal-personal-4410", writable=True,
         title="Dentist cleaning", date="2026-08-20", time="09:00", hours=1,
         location="Riverton Dental", url="",
         beat="calendar.rename"),

    # 45/48. A second invitation nobody ever answers, which **disappears** from the
    # feed on day 3. A disappearance is an observation, not a re-derivation, so it
    # keeps its authority to decline the row — and the declined invite stays visible,
    # because a birthday you have said no to is the one you still want to open and
    # send a message through.
    dict(id="cl04", uid="PARTIFUL-CTF", days=(1, 2),
         calendar="Partiful", calendar_key="cal-invites-8821", writable=False,
         title="Capture The Flag", date="2026-08-23", time="12:00", hours=6,
         location="", url="https://partiful.com/e/ctf",
         beat="invite.unanswered"),

    # 53. The other spelling of "no location", and the one production actually sends.
    # Partiful does not leave the field empty when the user has not replied — it writes a
    # sentence into it. Every calendar fixture here modelled the empty case, so a
    # presence test read the placeholder as a real venue and filed the row confirmed.
    # Present on all four days: the day-3 rename must not re-derive it either.
    dict(id="cl07", uid="PARTIFUL-ALDON", days=(1, 2, 3, 4),
         calendar={1: "Partiful", 2: "Partiful", 3: "Partiful Invites",
                   4: "Partiful Invites"},
         calendar_key="cal-invites-8821", writable=False,
         # Inside the week window on every day of the run, so this beat asserts a
         # *rendered* line without competing for one of `_later_block`'s eight slots.
         # Dated into `## Later`, it displaced an unrelated invitation from beat 48 and
         # failed that beat's check — a fixture perturbing a neighbour, not a finding.
         title="Mount Aldon Stage Reading", date="2026-08-10", time="19:00", hours=2,
         location="Location available once RSVP'd",
         description="Doors 6:30. The reading is upstairs; ask for Nadia at the desk.",
         url="https://partiful.com/e/aldon",
         beat="invite.withheld"),

    # A subscribed feed row: opportunity + mentioned, and it must stay out of `## Later`
    # however the rendering rules move. This is the row `brief._committed` exists for.
    dict(id="cl03", uid="HOLIDAYS-AUG", days=(1, 2, 3, 4),
         calendar="US Holidays", calendar_key="cal-holidays-0001", writable=False,
         title="Statehood Day", date="2026-08-21", all_day=True,
         location="", url="", beat="calendar.feed-stays-out"),
]

# --------------------------------------------------------------------------------
# ACTIONS — what the agent *did* while the user was talking to it, as distinct from what the user
# said. This is the half the corpus was missing.
#
# Every SIGNAL line above is traffic: it arrives, it is gated, it waits in the spool,
# and a model reads it hours later. That is most of memcal but it is not the part the user
# actually touches. When the user tells the assistant something, the assistant does not file
# a note for tonight's pass — it calls a typed tool, the store changes now, and the
# nightly pass meets a calendar that already knows.
#
# Which is a completely different test, and nothing here was testing it: whether a pass
# reading the conversation that produced a row can recognise the row, leave it alone,
# and still update it when the plan genuinely changes the next day. Three ways to get
# that wrong, all of them silent — a duplicate row, a to-do opened twice, or a row so
# well protected by write precedence that no later pass can ever correct it.
#
# `call` names a function in `memcal.live`, which is what `mcp_server` dispatches to,
# so these are the same calls a real agent makes. No model is involved in any of them.
# --------------------------------------------------------------------------------

MOVIE_DAY = "2026-08-11"     # Tuesday the 11th
SHOW_DAY = "2026-08-07"      # Friday

ACTIONS = [
    # 36. An old/live row with no provenance or participants, like the production
    #     Elements row that motivated the relationship-aware bundle context.
    dict(id="ac05", day=1, time="08:00", beat="existing-group-event",
         call="add_event",
         args=dict(title="Elements Music Festival", when="2026-08-07",
                   status="mentioned", kind="opportunity")),

    # 37. Three confirmed movies, three unresolved ticket prerequisites. Day 2 proves
    #     two and deliberately leaves the third open.
    dict(id="ac06", day=1, time="08:05", beat="ticket-lifecycle",
         call="add_event",
         args=dict(title="Spider-Man movie", when="2026-08-10",
                   status="confirmed", kind="commitment", participants=["Morgan"])),
    dict(id="ac07", day=1, time="08:06", beat="ticket-lifecycle",
         call="open_todo",
         args=dict(text="Make sure we have Spider-Man tickets", event="Spider-Man")),
    dict(id="ac08", day=1, time="08:07", beat="ticket-lifecycle",
         call="add_event",
         args=dict(title="Fantastic Four movie", when="2026-08-09",
                   status="confirmed", kind="commitment")),
    dict(id="ac09", day=1, time="08:08", beat="ticket-lifecycle",
         call="open_todo",
         args=dict(text="Make sure we have Fantastic Four tickets",
                   event="Fantastic Four")),
    dict(id="ac10", day=1, time="08:09", beat="ticket-lifecycle",
         call="add_event",
         args=dict(title="Superman movie", when="2026-08-11",
                   status="confirmed", kind="commitment")),
    dict(id="ac11", day=1, time="08:10", beat="ticket-lifecycle",
         call="open_todo",
         args=dict(text="Make sure we have Superman tickets", event="Superman")),

    # 19. The user tells the assistant before the user answers Riley, so the row is `live`,
    #     confirmed, and a day old by the time day 2's pass reads Riley cancelling it.
    dict(id="ac01", day=1, time="20:21", beat="movie.agent-confirmed",
         call="add_event",
         args=dict(title="Movie with Riley", when=MOVIE_DAY, status="confirmed",
                   kind="commitment", participants=["Riley Morgan"])),

    # 20. One sentence, two tools: the plan and the obligation it created.
    dict(id="ac02", day=1, time="17:53", beat="show.agent-settled",
         call="add_event",
         args=dict(title="Show at Bowery Ballroom", when=SHOW_DAY, time="20:00",
                   location="Bowery Ballroom", status="confirmed", kind="commitment",
                   participants=["Cameron Ortiz"])),
    dict(id="ac03", day=1, time="17:54", beat="show.agent-settled",
         call="open_todo",
         args=dict(text="Venmo Cameron for the show ticket")),

    # 21. And the verb that did not exist until this beat asked for it: the user says it is
    #     done, so it is done. The dream stage may never conclude that on its own.
    dict(id="ac04", day=2, time="18:23", beat="show.agent-closed",
         call="close_todo", args=dict(which="Venmo Cameron")),
]


# --------------------------------------------------------------------------------
# EMAIL — every one is a full RFC822 message, so the headers are the test.
# `kind` drives which headers the builder attaches.
# --------------------------------------------------------------------------------

EMAIL = [
    # -- 51. A company's event, addressed to them by name, on a specific date, with a
    #    join link. Every surface feature of a real invitation and none of the
    #    substance: nobody notices if the user does not go and nothing is owed to anyone.
    #    Harder than the AWS newsletter, which announces itself as bulk.
    dict(id="em20", day=1, time="10:15", kind="transactional",
         addr="hello@petlyinsurance.com", name="Petly Pet Insurance",
         subject="You're invited: Wellness Wednesday with Dr. Ramirez, Aug 12",
         text="Hi Casey,\n\nYou're invited to our Wellness Wednesday webinar on "
              "Wednesday, August 12 at 6:00 PM ET. Dr. Ramirez will cover seasonal "
              "allergies in dogs and answer live questions.\n\nJoin here: "
              "https://petlyinsurance.example.com/webinars/aug12\n\n"
              "Can't make it? We'll email the recording to all policyholders.\n\n"
              "The Petly Team"),

    # -- 52. The tutor reschedules and the join link is in the mail, as an anchor,
    #    which is how a link arrives in a real email. The calendar entry for the same
    #    appointment says "Online" — true, and not a place you can go, and not a
    #    link you can press. Two halves of one fact, in two sources, and until now
    #    nowhere for the half that matters to land.
    dict(id="em21", day=1, time="11:05", kind="person", html=True,
         addr="morgan@harbortutoring.example", name="Morgan Hale",
         subject="Re: New appointment time",
         beat="tutoring.link-from-email",
         text="<p>Hi Casey! Sorry for the delay &mdash; I'm on vacation right now. "
              "How is 12 PM on Wednesday the 12th?</p>"
              "<p>Morgan Hale, M.Ed.<br>Tutor, Harbor Tutoring<br>"
              "<a href=\"https://us02web.zoom.example/j/8842119\">"
              "Tutoring Meeting Room Link</a></p>"),

    dict(id="em08", day=1, time="13:20", kind="transactional",
         addr="tickets@drafthouse.com", name="Alamo Drafthouse",
         subject="Your Dune ticket — Wednesday August 12 at 7:30 PM",
         beat="ticket-source",
         text="Your ticket is confirmed for Dune at Alamo Drafthouse Downtown, "
              "Wednesday August 12 at 7:30 PM. Auditorium 4, seat F12."),

    # 37. Generic transactional subject: the open linked obligation earns a body fetch,
    # and only the body naming the exact event may rescue it into the spool.
    dict(id="em09", day=2, time="09:40", kind="transactional",
         addr="orders@amctheatres.com", name="AMC Theatres",
         subject="AMC confirmation #84721",
         beat="ticket-email-proof",
         text="Your tickets are confirmed for Fantastic Four at AMC Empire 25 on "
              "Sunday August 9 at 6:20 PM. Auditorium 7, seats J10 and J11. "
              "Confirmation 84721."),
    dict(id="em10", day=2, time="09:42", kind="transactional",
         addr="orders@amctheatres.com", name="AMC Theatres",
         subject="AMC confirmation #99104",
         beat="ticket-wrong-movie",
         text="Your tickets are confirmed for Batman Returns at AMC Village 7 on "
              "Thursday August 13 at 9:00 PM. Seats C4 and C5."),
    dict(id="em11", day=2, time="09:45", kind="bulk",
         addr="newsletter@amctheatres.com", name="AMC Theatres",
         subject="Superman tickets are on sale now",
         beat="ticket-marketing",
         text="Superman arrives soon. Buy tickets today and join AMC Stubs for member "
              "pricing. Seats and showtimes vary by theater."),

    dict(id="em01", day=2, time="09:15", kind="transactional",
         addr="invites@partiful.com", name="Partiful",
         subject="Updated: Devon's Block Party BBQ",
         beat="bbq.moved",
         say="a Partiful event-update notification. the start time moved from 2pm to 4pm "
             "on Saturday August 15. keep the event name consistent with the groupme "
             "chatter about the block party bbq. include the host name Devon Park"),

    dict(id="em02", day=2, time="14:55", kind="person",
         addr="alex.rivera@example.com", name="Alex Rivera",
         subject="Thursday - table booked",
         beat="dinner.third-stream",
         say="short personal email confirming the thursday 8:30 booking for four. "
             "the same dinner as the whatsapp thread"),

    dict(id="em03", day=1, time="06:02", kind="bulk",
         addr="no-reply@awsevents.amazonses.com", name="AWS Events",
         subject="Reminder: AWS Summit NYC networking night is tomorrow",
         beat="junk.bulk",
         say="marketing email for a tech networking event happening 'tomorrow'. "
             "reads exactly like real AWS event marketing - agenda, register link, "
             "venue. this is the exact shape of thing that must never become a "
             "calendar row"),

    dict(id="em04", day=1, time="07:30", kind="bulk",
         addr="alerts@chase.com", name="Chase",
         subject="Your automatic payment is scheduled",
         beat="junk.bulk",
         say="automated Chase notice that an autopay will process in three days. "
             "formal banking language, account ending in 4471"),

    dict(id="em05", day=2, time="05:45", kind="bulk",
         addr="hello@marketing.uniqlo.com", name="UNIQLO",
         subject="Final hours - sale ends tomorrow",
         beat="junk.bulk",
         say="retail marketing. urgency language, 'ends tomorrow', discount codes"),

    dict(id="em06", day=1, time="10:00", kind="transactional",
         addr="billing@squarespace.com", name="Squarespace",
         subject="Your domain example.org renews on Aug 14",
         beat=None,
         say="a genuine renewal notice with a date. borderline on purpose - it is a "
             "real dated fact about their own account, not marketing"),

    dict(id="em07", day=2, time="08:20", kind="person",
         addr="bailey@example.com", name="Bailey Stone",
         subject="Re: photos from the reunion",
         beat=None,
         say="chatty personal email about sharing photos. no dates, no plans. exists so "
             "a real person's mail is in the corpus alongside the bulk"),
]

# --------------------------------------------------------------------------------
# FILLER — traffic that means nothing. Two thirds of the corpus by volume, because a
# gate that only ever sees signal is not being tested.
#
# Specified as batches rather than line by line: the writer is given a thread, a
# participant list, a message count and a subject, and must produce chatter with no
# dated plan involving them in it.
# --------------------------------------------------------------------------------

FILLER = [
    dict(id="fl-smash", day=1, src="gm", thread="smash bros", count=22, start="19:00",
         say="a busy gaming group chat. trash talk, character arguments, reactions, "
             "links to clips. people say things like 'anyone on tonight' and 'im down' "
             "which is exactly the shape the gate is meant to catch and the model is "
             "meant to find nothing durable in. NO specific dated plan"),
    dict(id="fl-smash2", day=2, src="gm", thread="smash bros", count=18, start="20:15",
         say="same chat, next evening. more of the same. one person asks who's around "
             "later, nobody commits to anything specific"),
    dict(id="fl-family", day=1, src="wa", thread="morgan family", count=9, start="18:00",
         say="family group chat. photos described, how-are-you, a recipe, mild nagging. "
             "no plans"),
    dict(id="fl-family2", day=2, src="wa", thread="morgan family", count=7, start="19:30",
         say="same family chat, next day. someone shares news about a cousin. no plans"),
    dict(id="fl-chen2", day=2, src="bb", thread="Alex Chen", count=5, start="09:40",
         say="same work DM. follow-up on the ticket. one message mentions the offsite "
             "vaguely without ever giving a date"),
    dict(id="fl-harper", day=1, src="bb", thread="Harper", count=8, start="12:00",
         say="partner DM. groceries, what to watch, a photo of the dog, small logistics "
             "that resolve inside the conversation. warm and mundane"),
    dict(id="fl-harper2", day=2, src="bb", thread="Harper", count=6, start="14:00",
         say="same, next day. dinner-at-home logistics that resolve immediately"),
    dict(id="fl-devon", day=2, src="gm", thread="block party", count=6, start="12:30",
         say="neighbourhood chat admin. a lost cat, a parking complaint, someone "
             "thanking someone. no new plans"),
    dict(id="fl-rowan", day=1, src="bb", thread="Rowan Vale", count=4, start="15:00",
         say="the user is still in italy. photos described, food, jet lag dread. no plans"),
]
