"""Reduce raw stream text without removing meaning.

This is deterministic regex/slicing only. It removes quoted or boilerplate text before
model calls; relevance decisions belong to the gate.
"""

from __future__ import annotations

import re
import string
import unicodedata

# "On Mon, Aug 1, 2024 at 4:52 PM Casey <casey@x.com> wrote:" and its many dialects.
QUOTE_HEADER = re.compile(
    r"^\s*(?:"
    r"On\s+.{0,80}?\s+(?:wrote|sent):"
    r"|_{5,}"
    r"|-{2,}\s*(?:Original Message|Forwarded message|Reply above this line).{0,40}-{0,2}"
    r"|From:\s*.{0,80}?\s*(?:Sent|Date):"
    r"|\s*(?:El|Le|Am)\s+.{0,60}?\s+(?:escribió|a écrit|schrieb):"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

SIGNATURE = re.compile(
    r"^\s*(?:--\s*$|Sent from (?:my )?[\w ]{0,24}|Get Outlook for \w+|"
    r"This email and any attachments|CONFIDENTIALITY NOTICE)",
    re.IGNORECASE | re.MULTILINE,
)

FOOTER = re.compile(
    r"^\s*(?:"
    r"(?:To\s+)?[Uu]nsubscribe\b.{0,120}"
    r"|You(?:'re| are) receiving this\b.{0,160}"
    r"|(?:Manage|Update) your (?:email )?(?:preferences|subscription)\b.{0,120}"
    r"|©\s*\d{4}.{0,120}"
    r"|View (?:this email )?in (?:your )?browser\b.{0,80}"
    r"|Privacy Policy\s*[|·•]\s*Terms\b.{0,80}"
    r")$",
    re.IGNORECASE | re.MULTILINE,
)

URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
IMAGE_ALT = re.compile(r"\[(?:image|cid):[^\]]*\]", re.IGNORECASE)
TRACKING_PIXEL = re.compile(r"^\s*\[?\s*\]?\s*$", re.MULTILINE)
# Runs of layout punctuation that survive HTML stripping: "| | |", "- - -", "===".
LAYOUT_NOISE = re.compile(r"^[\s|+*_=~·•\-]{3,}$", re.MULTILINE)
WHITESPACE = re.compile(r"[ \t ]{2,}")
BLANK_RUN = re.compile(r"\n{3,}")

#: Unicode Specials are placeholders rather than visible message content.
SPECIALS = re.compile("[￰-￿]")

#: Non-rendering categories; unassigned characters remain visible content.
INVISIBLE = frozenset({"Cc", "Cf"})


def spoken_text(text: str) -> str:
    """What is left of a raw body once placeholders go, or nothing if nobody spoke.

    A body that keeps no visible character is not a message. That is wider than a list
    of known-bad codepoints and narrower than "it has no letters": an emoji or a lone
    "?" is visible and stays a message, which `gate.is_reaction` then judges, while a
    bare U+FFFD or a stray zero-width space is not and becomes nothing.
    """
    body = SPECIALS.sub(" ", text or "").strip()
    for ch in body:
        if not ch.isspace() and unicodedata.category(ch) not in INVISIBLE:
            return body
    return ""


def shorten_url(match: re.Match) -> str:
    """A URL's information is its host and maybe its path — never its query string.

    Tracking links can run to several hundred characters of base64. Keeping the host
    lets the model tell a Partiful invite from a Databricks newsletter; keeping the
    rest just costs money.
    """
    url = match.group(0)
    host = re.sub(r"^https?://(?:www\.)?", "", url).split("/")[0]
    if not host:
        # A hostless match ("https:///x", a bare "www."): splitting on an empty
        # separator raises, and one malformed link in one marketing email was enough
        # to abort the whole email stream. Nothing here is worth a stream.
        return f"<{url[:40]}>"
    path = url.split(host, 1)[-1].split("?")[0].rstrip("/")
    if len(path) > 40:
        path = path[:40] + "…"
    return f"<{host}{path}>"


def strip_quotes(text: str) -> str:
    """Cut everything from the first quoted-reply marker onward.

    The reply is the new information; the chain below it is a copy of messages we
    already hold. Cutting at the marker also removes the nested chains beneath it.
    """
    earliest = None
    match = QUOTE_HEADER.search(text)
    if match:
        earliest = match.start()

    # A run of three or more '>' lines is a quote block even without a header.
    lines = text.splitlines()
    run = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            run += 1
            if run >= 3:
                offset = len("\n".join(lines[: index - run + 1]))
                earliest = offset if earliest is None else min(earliest, offset)
                break
        else:
            run = 0
    return text[:earliest] if earliest is not None else text


def clean_email(text: str, *, limit: int = 900) -> str:
    """Subject + the part of the body that is actually this person's words."""
    if not text:
        return ""
    body = strip_quotes(text)
    body = FOOTER.sub("", body)
    signature = SIGNATURE.search(body)
    if signature and signature.start() > 40:
        body = body[: signature.start()]
    body = IMAGE_ALT.sub("", body)
    body = URL.sub(shorten_url, body)
    body = LAYOUT_NOISE.sub("", body)
    body = WHITESPACE.sub(" ", body)
    body = BLANK_RUN.sub("\n\n", body).strip()
    return truncate(body, limit)


def clean_message(text: str, *, limit: int = 600) -> str:
    """Chat messages are already short; only URLs and pasted walls need work."""
    if not text:
        return ""
    body = URL.sub(shorten_url, text)
    body = WHITESPACE.sub(" ", body)
    return truncate(BLANK_RUN.sub("\n\n", body).strip(), limit)


def truncate(text: str, limit: int) -> str:
    """Cut on a sentence or line boundary when one is near, so nothing ends mid-word."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    for boundary in ("\n", ". ", "! ", "? "):
        cut = window.rfind(boundary)
        if cut > limit * 0.6:
            return window[: cut + len(boundary)].rstrip() + " …"
    return window.rstrip() + " …"


#: Per-character token costs fitted from `tests/token_calibration.json`.
TOKEN_WEIGHTS = {"letters": 0.40, "spaces": 0.01, "digits": 0.47,
                 "marks": 0.78, "wide": 1.00}

_DROP_LETTERS = str.maketrans("", "", string.ascii_letters)
_DROP_SPACES = str.maketrans("", "", string.whitespace)
_DROP_DIGITS = str.maketrans("", "", string.digits)


def character_classes(text: str) -> dict[str, int]:
    """Counts each character of `text` into exactly one class, ASCII first."""
    plain = text.encode("ascii", "ignore").decode("ascii")
    letters = len(plain) - len(plain.translate(_DROP_LETTERS))
    spaces = len(plain) - len(plain.translate(_DROP_SPACES))
    digits = len(plain) - len(plain.translate(_DROP_DIGITS))
    return {"letters": letters, "spaces": spaces, "digits": digits,
            "marks": len(plain) - letters - spaces - digits,
            "wide": len(text) - len(plain)}


def estimate_tokens(text: str) -> int:
    """A token estimate that over-counts, weighted per character class.

    chars/4 is the usual rule of thumb and it under-counts real email by ~20%: URLs,
    dates, and punctuation-heavy layout tokenize far below four characters a token.
    Packing, trimming, and cost estimates all price against this number, so the
    weights are fitted to over-count every call in `tests/token_calibration.json`.
    Over the 162 calls there it runs 1.17x the provider's own count, never under.
    """
    if not text:
        return 0
    counts = character_classes(text)
    weighted = sum(counts[name] * weight for name, weight in TOKEN_WEIGHTS.items())
    # The floor bounds whitespace-heavy input.
    return max(int(weighted), len(text) // 20) + 1
