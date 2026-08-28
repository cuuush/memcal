"""The gate — no model, ever.

Everything is archived; the gate decides what the dream pass even looks at.
Messages pass on a temporal token, a question mark, a first-person commitment verb,
or a top-tier sender. Email is a sender problem, not a content problem.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from . import identity

WEEKDAYS = r"mon|monday|tue|tues|tuesday|wed|weds|wednesday|thu|thur|thurs|thursday|fri|friday|sat|saturday|sun|sunday"
MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
COUNT = r"a|an|\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a few|couple(?: of)?"
RELATIVE = (r"tonight|tomorrow|tmr|tmrw|today|this (?:week|weekend|morning|afternoon|evening)|"
            r"next (?:week|weekend|month|" + WEEKDAYS + r")|later|"
            r"(?:in|for) (?:" + COUNT + r") (?:hour|hours|day|days|week|weeks|month|months|"
            r"min|mins|minute|minutes)")
# "at 8" is a time even with no am/pm — that's the GroupMe case ("we playing at 8?").
CLOCK = (r"\b\d{1,2}\s?(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\bnoon\b|\bmidnight\b|"
         r"\b(?:at|by|around|til|until|after|before)\s+\d{1,2}(?::\d{2})?\b")
DATEISH = r"\b\d{1,2}/\d{1,2}\b|\b(?:" + MONTHS + r")\.?\s+\d{1,2}\b"

TEMPORAL_RE = re.compile(
    r"\b(?:" + WEEKDAYS + r")\b|\b(?:" + RELATIVE + r")\b|" + CLOCK + r"|" + DATEISH,
    re.IGNORECASE,
)

# First-person commitment verbs: what the user says the user will do, or is doing.
COMMIT_RE = re.compile(
    r"\b(?:i'?m|im|i am|we'?re|we are|i'?ll|ill|i will|we'?ll|we will|i can|i should|"
    r"i need to|i have to|i gotta|i'?ve got|lets|let'?s|i'?d like to|"
    r"i owe|i'?ll get|remind me|dont forget|don'?t forget)\b",
    re.IGNORECASE,
)

# Someone else's state — the availability rows that make lateral connections work.
AVAILABILITY_RE = re.compile(
    r"\b(?:free|around|available|down for|in town|busy|out of town|away|back (?:on|in|from))\b",
    re.IGNORECASE,
)

# Durable entity facts. No temporal token, but exactly what a wiki slot is for, so
# they get their own cheap signal rather than being lost as "no-signal".
ATTRIBUTE_RE = re.compile(
    r"\b(?:favou?rite|obsessed with|allergic to|birthday|turns \d+|lives? (?:in|on|at)|"
    r"moved (?:to|in)|works? (?:at|for)|new job|got (?:a|an) (?:dog|cat|puppy|kitten)|"
    r"engaged|married|hates?|can'?t stand|loves? (?!you\b)\w+)\b",
    re.IGNORECASE,
)

INVITE_RE = re.compile(
    r"\b(?:invite|invited|rsvp|party|dinner|lunch|brunch|drinks|game|show|birthday|wedding|"
    r"tickets|reservation|meet ?up|hang|come over|you (?:in|coming|going))\b",
    re.IGNORECASE,
)

# Unsolicited "product optimization" jobs are a recurring text-message scam.  iMessage
# is otherwise read in full, so this has to run before PASS_ALL_STREAMS.  Require three
# independent traits rather than treating ordinary recruiter language as spam: a
# remote/flexible opening, merchant/product busywork, implausible daily compensation,
# artificial scarcity, or an instruction to text a different number.
TASK_SCAM_RES = (
    re.compile(r"\b(?:remote recruitment team|flexible online (?:opening|job|work))\b",
               re.IGNORECASE),
    re.compile(r"\b(?:merchants?|products?)\b.{0,50}\b(?:update|optimi[sz]e|boost|rate)\b"
               r"|\b(?:update|optimi[sz]e|boost|rate)\b.{0,50}\b(?:merchants?|products?)\b",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:pay range|earn(?:ings?)?)\b.{0,30}\$\s*\d+.{0,20}\b(?:daily|per day)\b",
               re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:only|just)\s+\d+\s+(?:positions?|openings?|slots?)\b",
               re.IGNORECASE),
    re.compile(r"\btext\s+[\"'“”]?(?:more info|info|yes)[\"'“”]?\s+to\s+\+?\d[\d ()-]{7,}\b",
               re.IGNORECASE),
)


def is_task_scam(text: str) -> bool:
    """High-confidence task/job scam, without rejecting normal recruiters."""
    body = text or ""
    return sum(bool(pattern.search(body)) for pattern in TASK_SCAM_RES) >= 3


# Bulk headers have different strengths. List-Id/List-Post prove list posting;
# List-Unsubscribe only proves unsubscribe support and must not override an event subject.
LIST_POSTING_HEADERS = ("list-id", "list-post")
BULK_HEADERS = ("list-unsubscribe", "list-id", "list-post", "precedence")
BULK_CATEGORIES = ("promotions", "social", "forums", "updates", "spam")

# ---------------------------------------------------------------- subjects --
#
# Subject lines are available before fetching the body, so use them to rescue actionable
# mail from noreply senders. Match commitment, delivery, order, money, and invitation
# phrases; do not use the temporal regex because newsletter weekdays are not events.
_APPOINTMENT = (r"appointment|appt|reschedul|your (?:booking|reservation|visit|session)"
                r"|check[- ]?in|confirmed for|scheduled for|you'?re booked")
_DELIVERY = (r"has shipped|was shipped|is on the way|are on the way|out for delivery"
             r"|delivered|has arrived|arriving|ready for pick[- ]?up|was picked up"
             r"|ready to collect|tracking number|shipment")
_ORDER = (r"your order|order (?:#|no\.?|number|confirm)|thank you for your (?:purchase|order)"
          r"|we had to cancel|order (?:was )?cancel|refund")
_MONEY = r"invoice|receipt for|payment (?:due|received|failed)|statement is ready|bill is due"
_INVITE = (r"save the date|you'?re invited|invitation to|rsvp|register (?:now|for|today)"
           r"|tickets? (?:are|for|on sale)|join us|webinar|gala|fundraiser"
           r"|doors open|starts (?:in|at|on)|last chance to register")
# "Updated: Devon's Block Party BBQ" is Partiful telling them the start time moved, and
# it was the only place that fact existed — the GroupMe thread that created the row
# never mentioned it again. Anchored at the start of the subject, because a bare
# "update" anywhere in one is a newsletter more often than a change of plan.
_CHANGE = (r"cancel+ed|postponed|rescheduled|new (?:date|time|location)|venue change"
           r"|has been moved|reminder:|^\s*(?:updated?|changed?|revised|moved)\s*:")

SUBJECT_EVENT_RE = re.compile(
    "|".join(f"(?:{p})" for p in (_APPOINTMENT, _DELIVERY, _ORDER, _MONEY, _INVITE, _CHANGE)),
    re.IGNORECASE,
)

# Said in a subject, these mean the opposite of an event however many event words ride
# along with them. "Last chance — 40% off tickets" is a sale, not a show.
SUBJECT_PITCH_RE = re.compile(
    r"\b(?:\d{1,3}% off|% off|sale|deal|deals|coupon|promo code|save (?:up to |big|now)"
    r"|clearance|bogo|free shipping|shop (?:now|the)|best sellers|new arrivals"
    r"|limited time offer|unsubscribe|newsletter|digest|briefing|top stories"
    r"|recommended for you|trending|you may (?:also )?like|price drop|back in stock)\b",
    re.IGNORECASE,
)


def subject_is_event(subject: str) -> bool:
    """Does this subject report something that happens, rather than sell something?

    The pitch test is checked first and wins. A retailer's "Last chance to register" is
    an event; its "Last chance — 40% off" is not, and the second phrasing is far more
    common in an archive of ten years of marketing mail.
    """
    text = (subject or "").strip()
    if not text or SUBJECT_PITCH_RE.search(text):
        return False
    return bool(SUBJECT_EVENT_RE.search(text))

# Addresses that cannot hold a conversation. Free to detect, and a permanent decision.
# Local parts that cannot hold a conversation. Separators vary — chase.com uses
# `no.reply.alerts@`, others use `no-reply` or `noreply` — so treat . - _ + alike.
_SEP = r"[._\-+]?"
_AUTOMATED_WORDS = (
    "no" + _SEP + "reply", "do" + _SEP + "not" + _SEP + "reply", "donotreply",
    "bounce", "bounces", "mailer" + _SEP + "daemon", "postmaster", "notification",
    "notifications", "alert", "alerts", "update", "updates", "news", "newsletter",
    "marketing", "promo", "promotions", "billing", "receipt", "receipts", "invoice",
    "orders", "order" + _SEP + "confirmation", "support", "help", "info", "hello",
    "team", "express", "account", "accounts", "security", "service", "services",
    "member", "members", "welcome", "digest", "mail", "email", "contact", "reply",
)
AUTOMATED_RE = re.compile(
    r"^(?:" + "|".join(_AUTOMATED_WORDS) + r")(?:" + _SEP + r"[a-z0-9]+)*@",
    re.IGNORECASE,
)

# An address that says "do not reply" anywhere in its local part means it, whatever
# prefix it carries. Anchoring at the start let `ads-account-noreply@google.com` and
# `system-noreply@nyuce.brightspace.com` through to the model at full price. The rest
# are words no person puts in their own address, so they are safe as substrings —
# `upcoming-invoice+acct_1onsbv@stripe.com` is not a correspondent.
UNREPLYABLE_RE = re.compile(
    r"(?:no" + _SEP + r"reply|do" + _SEP + r"not" + _SEP + r"reply|donotreply"
    r"|mailer" + _SEP + r"daemon|postmaster|invoice|receipt|newsletter"
    r"|unsubscribe|notification)", re.IGNORECASE)

# Labels that only ever appear in a bulk sender's subdomain. Matched exactly, because
# as substrings they are far too common to be safe ("e" is in everything).
_SENDING_LABELS = frozenset((
    "e", "em", "t", "mg", "trx", "mkt", "cta", "smtp", "ml", "cm", "mx", "et", "m", "s",
    "mail", "mailer", "email", "mails", "sendgrid", "mandrill", "mailgun", "reply",
    "sparkpost", "amazonses", "salesforce", "exacttarget", "sailthru", "braze",
))
# ...and tokens whose presence anywhere in a subdomain label gives it away:
# mail2., customer-mail., mynotifications., updates., em1.
_SENDING_TOKENS = ("mail", "news", "notif", "market", "campaign", "track", "click",
                   "link", "bounce", "unsub", "newsletter", "mktg", "promo", "update",
                   "alert", "offer", "deals")


def _sending_subdomain(host: str) -> bool:
    """Is this host a bulk-sending subdomain of some brand's real domain?

    `t.target.com` was caught, but `trx.mail2.disneyplus.com`, `et.geico.com` and
    `mynotifications.cvs.com` were not — the old pattern only looked at the label
    immediately after the `@`. Only labels *above* the registrable domain are examined,
    so `gmail.com` and `fidelity.com` are untouched.
    """
    labels = host.lower().split(".")
    for label in labels[:-2]:            # everything above example.com
        if label in _SENDING_LABELS:
            return True
        if any(token in label for token in _SENDING_TOKENS):
            return True
        if re.fullmatch(r"(?:e|em|t|m|mail|news|mx|ml)\d+", label):   # em1, mail2, m1
            return True
    return False


def is_automated(address: str) -> bool:
    """An address no human reads or answers. Free to decide, and decided once."""
    address = (address or "").strip().lower()
    if "@" not in address:
        return False
    local, _, host = address.partition("@")
    return bool(UNREPLYABLE_RE.search(local)
                or AUTOMATED_RE.search(address)
                or _sending_subdomain(host))


# Streams read in full, no content test at all.
#
# The gate exists to keep a decade of newsletters from being read at $5/M — that is an
# email problem. Texting is a few dozen lines a day, and there the gate was spending
# judgement it did not need to spend: 76% of iMessage was skipped, and the skipped half
# is where the answer kept turning out to be, because a reply carries no temporal token
# of its own ("yeah", "i'm down", "cant that night"). Cheaper to read all of it than to
# reconstruct it — which is what `add_thread_context` was already doing, badly.
PASS_ALL_STREAMS = frozenset(("imessage",))

# Anyone in Contacts, one-to-one. Naming someone is the strongest signal available and
# it is free, so it outranks every content test: if the user took the trouble to save their
# number, what they said to them directly is worth reading whether or not it contains a
# clock.
#
# Deliberately not extended to group chats. A named contact in the gamer chat is still
# a hundred lines a day of nothing, and there the content test is what earns its keep —
# "we playing at 8?" passes on the clock, as it always did.
KNOWN_CONTACT = "known-contact"


def is_reaction(text: str) -> bool:
    """A short emoji/punctuation reaction, which only has meaning beside a thread."""
    body = (text or "").strip()
    return bool(body) and len(body) <= 12 and not any(ch.isalnum() for ch in body)


@dataclass
class Verdict:
    passed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.passed


def gate_message(
    text: str,
    *,
    person: str | None = None,
    from_me: bool = False,
    top_tier: set[str] | None = None,
    stream: str | None = None,
    is_group: bool = False,
    addressed_to: str = "person",
) -> Verdict:
    """"hey" fails everything and costs nothing. "we playing at 8?" passes.

    Unless the stream is read in full, or the line is from someone the user has named in a
    one-to-one conversation — see PASS_ALL_STREAMS and KNOWN_CONTACT. Both are decided
    before any regex runs.
    """
    body = (text or "").strip()
    if not body:
        return Verdict(False, "empty")

    if is_task_scam(body):
        return Verdict(False, "task-scam")

    if stream in PASS_ALL_STREAMS:
        return Verdict(True, f"all-of:{stream}")
    if person and person != "me" and not is_group:
        return Verdict(True, KNOWN_CONTACT)

    if len(body) < 3 and not body.endswith("?"):
        return Verdict(False, "trivial")

    if from_me and COMMIT_RE.search(body):
        # Same words, two opposite meanings, and only the addressee separates them. To a
        # person, an imperative the user wrote is an obligation the user took on. To a machine it is
        # one the user handed off — "apply for 5 jobs pls" was done before the pass that filed
        # it as their. Both still pass: the agent stream is the highest-signal thing here
        # and the recall is not in question. What changes is that the verdict stops
        # *asserting* a commitment, so nothing downstream reads one off the reason.
        return Verdict(True, "directive" if addressed_to == "machine"
                       else "own-commitment")
    if TEMPORAL_RE.search(body):
        return Verdict(True, "temporal")
    if "?" in body:
        return Verdict(True, "question")
    if COMMIT_RE.search(body):
        return Verdict(True, "commitment-verb")
    if AVAILABILITY_RE.search(body):
        return Verdict(True, "availability")
    if INVITE_RE.search(body):
        return Verdict(True, "invitation")
    if ATTRIBUTE_RE.search(body):
        return Verdict(True, "attribute")
    if person and top_tier and person in top_tier:
        return Verdict(True, "top-tier-sender")
    return Verdict(False, "no-signal")


def gate_email(
    conn: sqlite3.Connection,
    *,
    address: str,
    subject: str = "",
    headers: dict | None = None,
    gmail_labels: list[str] | None = None,
) -> Verdict:
    """Keys on the sender, using signals that are free.

    An unknown bulk sender is denied once and never costs another token; an unknown
    human sender is passed through and recorded so the next one is a table lookup.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}

    # A no from them, or from the agent quoting them, is final and is checked before
    # everything — including the subject. "I don't care about AWS events" has to mean
    # the sender never costs another token, however the next subject line is worded.
    if identity.sender_blocked(conn, address):
        identity.bump_sender(conn, address)
        row = identity.sender_row(conn, address)
        return Verdict(False, f"blocked:{row['source']}")

    known = identity.sender_decision(conn, address)
    if known == "process":
        identity.bump_sender(conn, address)
        return Verdict(True, "sender-table:process")

    # Someone the user has in Contacts, mailing them for the first time. Decided before the bulk
    # tests because a friend's address can look like anything — including a `newsletter@`
    # local part at their own domain.
    if not known and identity.resolve(conn, address):
        identity.set_sender(conn, address, "process", KNOWN_CONTACT)
        return Verdict(True, KNOWN_CONTACT)

    # Tier one, above the subject: this message was *posted to a mailing list*. See the
    # note on LIST_POSTING_HEADERS — nothing a subject line says gets past it, because
    # "Reminder: AWS Summit NYC networking night is tomorrow" and "reminder: poker is
    # tomorrow" are lexically identical and the headers are the only difference.
    #
    # Read off this very message, so the second newsletter is blocked by its own headers
    # rather than by whether the table happened to learn the sender from the first.
    labels = {l.lower() for l in (gmail_labels or [])}
    if labels & set(BULK_CATEGORIES):
        identity.set_sender(conn, address, "ignore", f"gmail-category:{','.join(sorted(labels))}")
        return Verdict(False, "gmail-category")
    if (any(h in headers for h in LIST_POSTING_HEADERS)
            or headers.get("precedence", "").lower() == "bulk"):
        identity.set_sender(conn, address, "archive", "list-posting")
        return Verdict(False, "bulk-headers")

    # The subject, before the address tests and before honouring the gate's own earlier
    # guess. Being unable to reply to `noreply@e.headway.co` says nothing about whether
    # "your appointment with Harper is in 1 hour" belongs on a calendar.
    if subject_is_event(subject):
        identity.bump_sender(conn, address)
        return Verdict(True, "subject-event")

    if known:
        # The gate's own earlier conclusion, which the subject was just given a chance to
        # overturn. Still a lookup, still free.
        identity.bump_sender(conn, address)
        return Verdict(False, f"sender-table:{known}")

    # Tier two, below the subject: mass-sent or machine-sent, but not addressed to a
    # list. Anything whose subject reported a real event was already let through above.
    if any(h in headers for h in BULK_HEADERS):
        identity.set_sender(conn, address, "archive", "bulk-headers")
        return Verdict(False, "bulk-headers")
    if headers.get("auto-submitted", "").lower() not in ("", "no") or "x-autoreply" in headers:
        identity.set_sender(conn, address, "archive", "auto-submitted")
        return Verdict(False, "auto-submitted")
    if is_automated(address):
        # A machine that cannot be replied to. One decision, then a lookup forever.
        identity.set_sender(conn, address, "archive", "automated-address")
        return Verdict(False, "automated-address")

    # Unknown, non-bulk, replyable: treat as a human until told otherwise. A Partiful
    # invite from a stranger has to survive this, so the default stays permissive.
    identity.set_sender(conn, address, "process", "unknown-sender-default")
    return Verdict(True, "unknown-sender")


def bundle_entity(person: str | None, thread: str | None, stream: str) -> str:
    """Bundle key: group by entity or thread, across all streams.

    Splitting by source would separate the things that must be joined, so a person
    always wins over a thread when we know who it is.
    """
    if person:
        return f"person:{person}"
    if thread:
        return f"thread:{stream}:{thread}"
    return f"stream:{stream}"


def entity_for(*, person: str | None, thread: str | None, stream: str,
               is_group: bool) -> str:
    """The bundle key for one spooled item. The only place that choice is made.

    A person beats a thread — except in a group chat, where the thread *is* the
    subject. Four call sites used to decide this independently and two of them left the
    person in: one line of Alumni Chat, hand-queued from the gate view, filed itself
    under `person:parker shaw` and then sat in their personal bundle next to a 2019 DM,
    with nothing on it to say it came from a group of thirty people.
    """
    subject = None if is_group else person
    return bundle_entity(subject, thread if (is_group or not subject) else None, stream)
