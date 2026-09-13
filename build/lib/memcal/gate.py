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
# Bare hours count as times ("we playing at 8?").
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

# Someone else's availability state.
AVAILABILITY_RE = re.compile(
    r"\b(?:free|around|available|down for|in town|busy|out of town|away|back (?:on|in|from))\b",
    re.IGNORECASE,
)

# Durable entity facts for wiki slots; no temporal token required.
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

# Task-scam detector; runs before PASS_ALL_STREAMS. Requires three independent
# traits to avoid flagging legitimate recruiters.
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
# Anchored at the start: a bare "update" elsewhere is usually a newsletter,
# not a plan change.
_CHANGE = (r"cancel+ed|postponed|rescheduled|new (?:date|time|location)|venue change"
           r"|has been moved|reminder:|^\s*(?:updated?|changed?|revised|moved)\s*:")

SUBJECT_EVENT_RE = re.compile(
    "|".join(f"(?:{p})" for p in (_APPOINTMENT, _DELIVERY, _ORDER, _MONEY, _INVITE, _CHANGE)),
    re.IGNORECASE,
)

# Pitch phrases in a subject override event phrases.
SUBJECT_PITCH_RE = re.compile(
    r"\b(?:\d{1,3}% off|% off|sale|deal|deals|coupon|promo code|save (?:up to |big|now)"
    r"|clearance|bogo|free shipping|shop (?:now|the)|best sellers|new arrivals"
    r"|limited time offer|unsubscribe|newsletter|digest|briefing|top stories"
    r"|recommended for you|trending|you may (?:also )?like|price drop|back in stock)\b",
    re.IGNORECASE,
)


def subject_is_event(subject: str) -> bool:
    """Does this subject report something that happens, rather than sell something?

    The pitch test runs first and wins.
    """
    text = (subject or "").strip()
    if not text or SUBJECT_PITCH_RE.search(text):
        return False
    return bool(SUBJECT_EVENT_RE.search(text))

# Addresses that cannot hold a conversation. Free to detect, and a permanent decision.
# Separators vary, so treat . - _ + alike.
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

# "Do not reply" anywhere in the local part means it. Remaining tokens never appear
# in personal addresses, so substring match is safe.
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
    """Is this host a bulk-sending subdomain of a brand's real domain?

    Only labels above the registrable domain are examined.
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


# Streams read in full, no content test at all. Short replies carry no temporal
# token of their own, so filtering them loses answers.
PASS_ALL_STREAMS = frozenset(("imessage",))

# Anyone in Contacts, one-to-one. A saved contact outranks every content test.
# Group chats still require a content signal.
KNOWN_CONTACT = "known-contact"


def is_reaction(text: str) -> bool:
    """A short emoji/punctuation reaction, which only has meaning beside a thread."""
    body = (text or "").strip()
    return bool(body) and len(body) <= 12 and not any(ch.isalnum() for ch in body)


#: Processing priority, which is a different question from whether to process at all.
#: `low` still gets read; it is read after everything else and within a bounded share of
#: each pass, so a decade of retail mail cannot crowd out this evening's messages.
PRIORITIES = ("normal", "low")


@dataclass
class Verdict:
    passed: bool
    reason: str
    #: Only meaningful when `passed`. See PRIORITIES.
    priority: str = "normal"
    #: A *person* said no to this, as opposed to an automatic ranking. Excluded
    #: senders skip fetch and storage; see `identity.sender_blocked`.
    excluded: bool = False

    def __bool__(self) -> bool:
        return self.passed

    @property
    def low(self) -> bool:
        return self.passed and self.priority == "low"


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
    """Gate one message. Full-stream and known-contact passes apply before any regex."""
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
        # Directives to a person assert a commitment; directives to a machine do not.
        # Both pass.
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
    """Key on the sender with free signals, to prioritize rather than exclude.

    Automatic conclusions set a priority; only an explicit human decision excludes.
    See `identity.sender_blocked`.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}

    # Explicit blocks are final and checked before the subject.
    if identity.sender_blocked(conn, address):
        identity.bump_sender(conn, address)
        row = identity.sender_row(conn, address)
        return Verdict(False, f"blocked:{row['source']}", excluded=True)

    known = identity.sender_decision(conn, address)
    if known == "process":
        identity.bump_sender(conn, address)
        return Verdict(True, "sender-table:process")

    # First-time mail from a Contact; checked before bulk tests.
    if not known and identity.resolve(conn, address):
        identity.set_sender(conn, address, "process", KNOWN_CONTACT)
        return Verdict(True, KNOWN_CONTACT)

    # Tier one, above the subject: list-posting headers prove bulk delivery
    # regardless of subject. See LIST_POSTING_HEADERS.
    #
    # Read off this very message, so each newsletter is judged by its own headers.
    labels = {l.lower() for l in (gmail_labels or [])}
    if labels & set(BULK_CATEGORIES):
        identity.set_sender(conn, address, "archive",
                           f"gmail-category:{','.join(sorted(labels))}")
        return Verdict(True, "gmail-category", priority="low")
    if (any(h in headers for h in LIST_POSTING_HEADERS)
            or headers.get("precedence", "").lower() == "bulk"):
        identity.set_sender(conn, address, "archive", "list-posting")
        return Verdict(True, "bulk-headers", priority="low")

    # Subject before address tests: an unreplyable address can still carry an event.
    if subject_is_event(subject):
        identity.bump_sender(conn, address)
        return Verdict(True, "subject-event")

    if known:
        # Prior gate conclusion, now as a ranking.
        identity.bump_sender(conn, address)
        return Verdict(True, f"sender-table:{known}", priority="low")

    # Tier two, below the subject: bulk or automated mail without list headers.
    if any(h in headers for h in BULK_HEADERS):
        identity.set_sender(conn, address, "archive", "bulk-headers")
        return Verdict(True, "bulk-headers", priority="low")
    if headers.get("auto-submitted", "").lower() not in ("", "no") or "x-autoreply" in headers:
        identity.set_sender(conn, address, "archive", "auto-submitted")
        return Verdict(True, "auto-submitted", priority="low")
    if is_automated(address):
        # Unreplyable automated address: decide once, then lookup.
        identity.set_sender(conn, address, "archive", "automated-address")
        return Verdict(True, "automated-address", priority="low")

    # Unknown, non-bulk, replyable: treat as human; default stays permissive.
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

    A person beats a thread, except in a group chat where the thread is the subject.
    """
    subject = None if is_group else person
    return bundle_entity(subject, thread if (is_group or not subject) else None, stream)
