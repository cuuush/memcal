"""Reduce raw stream text without removing meaning.

This is deterministic regex/slicing only. It removes quoted or boilerplate text before
model calls; relevance decisions belong to the gate.
"""

from __future__ import annotations

import re

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


def estimate_tokens(text: str) -> int:
    """A deliberately pessimistic token estimate.

    chars/4 is the usual rule of thumb and it is badly wrong for email: URLs, code,
    and punctuation-heavy layout tokenize closer to 2.5 chars per token. Packing
    decisions are made against this number, so it errs on the side of over-counting —
    an under-sized request is cheap, an over-sized one gets rejected.
    """
    if not text:
        return 0
    letters = sum(character.isalpha() or character.isspace() for character in text)
    density = letters / len(text)
    chars_per_token = 2.6 + 1.4 * density        # ~2.6 for dense junk, ~4.0 for prose
    return int(len(text) / chars_per_token) + 1
