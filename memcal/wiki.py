"""The wiki — markdown files on disk, Obsidian-compatible.

One page per entity, prose plus named slots. Pages are created lazily, when there's
finally something to put on one. The user edits these by hand, so every read hits disk.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from . import db

SECTIONS = ("people", "places", "projects", "preferences")
SLOT_RE = re.compile(r"^- \*\*(?P<slot>[^*]+)\*\*:\s*(?P<value>.*?)\s*(?:<!--\s*(?P<meta>.*?)\s*-->)?$")
QUESTION_RE = re.compile(r"^- \[ \]\s*(?P<q>.+?)\s*$")
ALIAS_RE = re.compile(r"^-\s+(?P<name>.+?)\s*$")

#: Heading text -> which part of the page the lines under it belong to.
HEADINGS = {"## facts": "facts", "## open questions": "questions",
            "## also known as": "aliases"}


@dataclass
class Page:
    slug: str
    section: str
    path: Path
    title: str = ""
    body: str = ""
    slots: dict[str, dict] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    #: Other names for the same entity. Every one of them resolves to this page, so
    #: nothing can open a second one for a name we already know is this person.
    aliases: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"# {self.title or self.slug}", ""]
        if self.body.strip():
            lines += [self.body.strip(), ""]
        if self.aliases:
            lines += ["## Also known as", ""]
            lines += [f"- {name}" for name in self.aliases]
            lines.append("")
        if self.slots:
            lines += ["## Facts", ""]
            for slot, info in self.slots.items():
                meta = " ".join(x for x in (info.get("source"), info.get("ts")) if x)
                suffix = f"  <!-- {meta} -->" if meta else ""
                lines.append(f"- **{slot}**: {info.get('value','')}{suffix}")
            lines.append("")
        if self.questions:
            lines += ["## Open questions", ""]
            lines += [f"- [ ] {q}" for q in self.questions]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _section_for(slug: str, wiki_dir: Path) -> str:
    for section in SECTIONS:
        if (wiki_dir / section / f"{slug}.md").exists():
            return section
    return "people"


def path_for(wiki_dir: Path, slug: str, section: str | None = None) -> Path:
    slug = canonical(wiki_dir, slug)
    section = section or _section_for(slug, wiki_dir)
    return wiki_dir / section / f"{slug}.md"


def exists(wiki_dir: Path, slug: str) -> bool:
    slug = canonical(wiki_dir, slug)
    return any((wiki_dir / s / f"{slug}.md").exists() for s in SECTIONS)


def _files(wiki_dir: Path) -> list[Path]:
    found: list[Path] = []
    for section in SECTIONS:
        folder = wiki_dir / section
        if folder.is_dir():
            found.extend(sorted(folder.glob("*.md")))
    return found


def list_pages(wiki_dir: Path) -> list[str]:
    return sorted({p.stem for p in _files(wiki_dir)})


#: Shortest given name that may stand in for a longer one. Below this a two-letter
#: diminutive would offer itself as every page sharing those letters.
_SHORT_NAME_MIN = 3


def _variant(one: list[str], other: list[str]) -> bool:
    """Same surname, and one given name is the other shortened."""
    if len(one) < 2 or len(other) < 2 or one[-1] != other[-1]:
        return False
    short, long = sorted((one[0], other[0]), key=len)
    return (len(short) >= _SHORT_NAME_MIN and short != long
            and long.startswith(short))


def near_pages(wiki_dir: Path, slug: str) -> list[str]:
    """Existing pages that may already be the person this slug names.

    Suggestive, never decisive: the caller offers these to the model, which judges
    with the conversation in front of it.
    """
    # A shortened name reads as absent; suggest variants to prevent duplicate pages.
    want = [t for t in db.slugify(slug).split("-") if t]
    if not want:
        return []
    return [page for page in list_pages(wiki_dir)
            if _variant(want, [t for t in page.split("-") if t])]


#: How many slot names one page contributes to the brief's index of the wiki.
INDEX_SLOTS_PER_PAGE = 4


def slot_index(wiki_dir: Path, *, per_page: int = INDEX_SLOTS_PER_PAGE) -> dict[str, list[str]]:
    """{slug: [slot name, ...]} — not which pages exist, but what each one can answer."""
    index: dict[str, list[str]] = {}
    for slug in list_pages(wiki_dir):
        page = read(wiki_dir, slug)
        index[slug] = list(page.slots)[:per_page] if page else []
    return index


# --------------------------------------------------------------- aliases ----
# One person has one page. Aliases are stored on the canonical page and resolved on every
# lookup, preventing duplicate pages when a name changes or appears in another form.

_ALIAS_CACHE: dict[Path, tuple[tuple, dict[str, str]]] = {}


def _signature(wiki_dir: Path) -> tuple:
    """Cheap proof the wiki has not changed. The user edits these by hand between calls."""
    return tuple((str(p), p.stat().st_mtime_ns) for p in _files(wiki_dir))


def alias_map(wiki_dir: Path) -> dict[str, str]:
    """{alias slug: canonical slug} across the whole wiki."""
    signature = _signature(wiki_dir)
    cached = _ALIAS_CACHE.get(wiki_dir)
    if cached and cached[0] == signature:
        return cached[1]

    real = {p.stem for p in _files(wiki_dir)}
    mapping: dict[str, str] = {}
    for path in _files(wiki_dir):
        page = parse(path, path.stem, path.parent.name)
        for name in page.aliases:
            other = db.slugify(name)
            # A name that owns a page of its own is not an alias — it is a second page,
            # and quietly hiding it would strand whatever is written on it. `merge`
            # exists for that case and is the only thing allowed to resolve it.
            if other and other != page.slug and other not in real:
                mapping.setdefault(other, page.slug)
    _ALIAS_CACHE[wiki_dir] = (signature, mapping)
    return mapping


def canonical(wiki_dir: Path, slug: str) -> str:
    """The slug that actually holds this entity's page.

    Chains are followed so an alias of an alias still lands, and a cycle stops rather
    than hanging — a hand-edited wiki can always contain one.
    """
    slug = db.slugify(slug)
    mapping = alias_map(wiki_dir)
    seen = {slug}
    while slug in mapping:
        slug = mapping[slug]
        if slug in seen:
            break
        seen.add(slug)
    return slug


def aliases_of(wiki_dir: Path, slug: str) -> list[str]:
    page = read(wiki_dir, slug)
    return list(page.aliases) if page else []


# ------------------------------------------------------- one self-page ----
# Stable agent-facing reference for the user. The brief, page reads, notes, and
# dream all resolve `me` through here so one established page is reused.
#
# Identity dictionary: `identity.me_names()` plus explicit wiki aliases
# (`alias_map`/`canonical`). No new settings, no name-fragment inference.
# Matching is exact slug equality — at least as strict as `identity.is_me`,
# so a namesake's page never counts as self.
#
# Convention:
#   - one candidate -> its canonical slug string (reuse it).
#   - none -> the literal string "me". The first explicit note or
#     evidence-backed dream fact creates people/me.md through the existing
#     ensure/set_slot write path.
#   - multiple distinct candidates -> raise `SelfAmbiguous` with `.candidates`.
#     Never merge, pick a winner, or create a third page.
# Reads must NOT `ensure()` the result: a read alone never creates a page.

class SelfAmbiguous(Exception):
    """Two or more distinct pages both look like the user's own page."""

    def __init__(self, candidates) -> None:
        self.candidates = sorted(candidates)
        super().__init__(f"ambiguous self page: {', '.join(self.candidates)}")


def _self_candidates(conn, wiki_dir: Path) -> list[str]:
    """Canonical self-page slugs on disk, sorted. No creation, no raising."""
    from . import identity

    found: set[str] = set()
    me_canon = canonical(wiki_dir, "me")
    if exists(wiki_dir, me_canon):
        found.add(me_canon)
    for name in identity.me_names(conn):
        raw = (name or "").strip()
        if not raw:
            continue
        slug = db.slugify(raw)
        if slug == "untitled":
            continue
        canon = canonical(wiki_dir, slug)
        if exists(wiki_dir, canon):
            found.add(canon)
    return sorted(found)


def self_slug(conn, wiki_dir: Path) -> str:
    """Resolve the user's own page to one canonical slug.

    Returns the single candidate's slug, or the literal `"me"` when no
    candidate exists on disk. Raises `SelfAmbiguous` when the literal `me`
    reference and the established self names resolve to different pages.
    Follows recorded aliases and dedupes canonical slugs. Read-only:
    callers must not `ensure()` this on a read path.
    """
    candidates = _self_candidates(conn, wiki_dir)
    if not candidates:
        return "me"
    if len(candidates) == 1:
        return candidates[0]
    raise SelfAmbiguous(candidates)


def resolve_self_page(conn, wiki_dir: Path, ref: str) -> str | None:
    """Map `ref` to `self_slug()` when it names the user, else None.

    Matches the literal `me` (case-insensitive), an established self name
    from `identity.me_names()` (exact slug equality, case-insensitive), or a
    recorded alias that canonicalizes onto a self candidate. Anything else
    returns None so the caller falls through to the normal canonical path.
    Propagates `SelfAmbiguous` from `self_slug()`; callers must surface the
    candidates and write nothing.
    """
    from . import identity

    raw = (ref or "").strip()
    if not raw:
        return None
    slug = db.slugify(raw)
    if slug == "me":
        return self_slug(conn, wiki_dir)
    me_slugs: set[str] = set()
    for name in identity.me_names(conn):
        cleaned = (name or "").strip()
        if not cleaned:
            continue
        other = db.slugify(cleaned)
        if other == "untitled":
            continue
        me_slugs.add(other)
    if slug in me_slugs:
        return self_slug(conn, wiki_dir)
    if canonical(wiki_dir, slug) in set(_self_candidates(conn, wiki_dir)):
        return self_slug(conn, wiki_dir)
    return None


def add_alias(wiki_dir: Path, slug: str, name: str, *, section: str = "people",
              conn=None, commit: bool = True) -> Page:
    """Record that `name` means the same entity as `slug`.

    Refuses when `name` already has a page of its own: that is two pages with two sets
    of facts, and pointing one at the other without folding the facts across would drop
    them silently. Use `merge`.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("an alias needs a name")
    target = canonical(wiki_dir, slug)
    other = db.slugify(name)
    if other == target:
        raise ValueError(f"{name!r} is already the name of that page")
    if exists(wiki_dir, other) and db.slugify(other) in set(list_pages(wiki_dir)):
        raise ValueError(f"{other!r} has its own page — merge it instead of aliasing it")
    with _wiki_write(conn, wiki_dir, commit=commit):
        page = (_page_for_write(conn, wiki_dir, target, section) if conn is not None
                else read(wiki_dir, target) or ensure(wiki_dir, target, section=section))
        if not any(db.slugify(a) == other for a in page.aliases):
            page.aliases.append(name)
            if conn is None:
                write(wiki_dir, page)
            else:
                _stage_page(conn, wiki_dir, page)
        return page


def merge(wiki_dir: Path, keep: str, drop: str, *, source: str = "merge") -> Page:
    """Fold one page into another and leave an alias behind.

    The survivor keeps its wording; a merge only adds missing facts, never overwrites.
    """
    keep_slug, drop_slug = canonical(wiki_dir, keep), db.slugify(drop)
    if keep_slug == drop_slug:
        raise ValueError("cannot merge a page into itself")
    survivor = read(wiki_dir, keep_slug)
    doomed = read(wiki_dir, drop_slug)
    if survivor is None:
        raise ValueError(f"no page {keep_slug!r} to merge into")
    if doomed is None:                       # already gone; just make sure it resolves
        return add_alias(wiki_dir, keep_slug, drop)

    for slot, info in doomed.slots.items():
        survivor.slots.setdefault(slot, {**info, "source": info.get("source") or source})
    for question in doomed.questions:
        if question not in survivor.questions:
            survivor.questions.append(question)
    if doomed.body.strip():
        survivor.body = (survivor.body + "\n\n" + doomed.body.strip()).strip()
    for name in [*doomed.aliases, doomed.title or drop_slug]:
        if name and not any(db.slugify(a) == db.slugify(name) for a in survivor.aliases):
            if db.slugify(name) != keep_slug:
                survivor.aliases.append(name)

    doomed.path.unlink(missing_ok=True)
    _ALIAS_CACHE.pop(wiki_dir, None)
    write(wiki_dir, survivor)
    return survivor


def read(wiki_dir: Path, slug: str) -> Page | None:
    slug = canonical(wiki_dir, slug)
    for section in SECTIONS:
        path = wiki_dir / section / f"{slug}.md"
        if path.exists():
            return parse(path, slug, section)
    return None


def parse(path: Path, slug: str, section: str) -> Page:
    return _parse_content(path.read_text(encoding="utf-8"), path, slug, section)


def _parse_content(content: str, path: Path, slug: str, section: str) -> Page:
    page = Page(slug=slug, section=section, path=path)
    current = "body"
    body: list[str] = []
    for line in content.splitlines():
        if line.startswith("# ") and not page.title:
            page.title = line[2:].strip()
            continue
        low = line.strip().lower()
        if low.startswith("## "):
            current = HEADINGS.get(low, "body")
            if current == "body":
                body.append(line)
            continue
        if current == "aliases":
            m = ALIAS_RE.match(line.strip())
            if m:
                page.aliases.append(m.group("name").strip())
                continue
        if current == "facts":
            m = SLOT_RE.match(line.strip())
            if m:
                meta = (m.group("meta") or "").split()
                page.slots[m.group("slot").strip()] = {
                    "value": m.group("value").strip(),
                    "source": meta[0] if meta else None,
                    "ts": meta[1] if len(meta) > 1 else None,
                }
                continue
        elif current == "questions":
            m = QUESTION_RE.match(line.strip())
            if m:
                page.questions.append(m.group("q").strip())
                continue
        # Preserve unrecognized lines as prose; the wiki is hand-edited.
        body.append(line)
    page.body = "\n".join(body).strip()
    return page


def write(wiki_dir: Path, page: Page) -> Path:
    _write_rendered(page.path, page.render())
    _ALIAS_CACHE.pop(wiki_dir, None)
    return page.path


def _write_rendered(path: Path, content: str) -> None:
    """Publish one page without ever exposing a partly-written markdown file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # `replace` is durable only after the directory entry is flushed too. Some
        # filesystems do not support opening a directory; the replacement itself is
        # still atomic there, which is the important visible property.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class WikiWriteConflict(RuntimeError):
    """A page changed after its next snapshot was committed to the outbox."""


def _file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _text_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def recover(conn, wiki_dir: Path) -> list[Path]:
    """Publish snapshots left in the SQLite outbox by an interrupted write."""
    if conn.in_transaction:
        raise RuntimeError("recover wiki pages only between SQLite transactions")
    rows = conn.execute(
        "SELECT id, path, content, expected_hash"
        " FROM wiki_pending_writes ORDER BY id").fetchall()
    published: list[Path] = []
    for row in rows:
        stored = Path(row["path"])
        path = stored if stored.is_absolute() else wiki_dir / stored
        if not path.resolve().is_relative_to(wiki_dir.resolve()):
            raise ValueError(f"pending wiki path escapes wiki directory: {stored}")
        actual = _file_hash(path)
        desired = _text_hash(row["content"])
        if actual != desired:
            if actual != row["expected_hash"]:
                raise WikiWriteConflict(
                    f"wiki page changed while publication was pending: {stored}")
            _write_rendered(path, row["content"])
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM wiki_pending_writes WHERE id = ?", (row["id"],))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        published.append(path)
    if published:
        _ALIAS_CACHE.pop(wiki_dir, None)
    return published


@contextmanager
def _wiki_write(conn, wiki_dir: Path, *, commit: bool):
    """Give standalone callers a transaction; join a caller-owned one when requested."""
    if conn is None:
        yield
        return
    if not commit:
        if not conn.in_transaction:
            raise RuntimeError("a staged wiki write needs a caller-owned transaction")
        yield
        return
    if conn.in_transaction:
        conn.commit()
    recover(conn, wiki_dir)
    conn.execute("BEGIN")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    recover(conn, wiki_dir)


def _page_for_write(conn, wiki_dir: Path, slug: str, section: str) -> Page:
    target = canonical(wiki_dir, slug)
    path = path_for(wiki_dir, target, section)
    relative = str(path.relative_to(wiki_dir))
    pending = conn.execute(
        "SELECT content FROM wiki_pending_writes WHERE path = ? ORDER BY id DESC LIMIT 1",
        (relative,),
    ).fetchone()
    if pending:
        return _parse_content(pending["content"], path, target, section)
    return read(wiki_dir, target) or Page(
        slug=target, section=section, path=path,
        title=target.replace("-", " ").title(),
    )


def _stage_page(conn, wiki_dir: Path, page: Page) -> None:
    relative = str(page.path.relative_to(wiki_dir))
    pending = conn.execute(
        "SELECT id FROM wiki_pending_writes WHERE path = ? ORDER BY id DESC LIMIT 1",
        (relative,),
    ).fetchone()
    if pending:
        conn.execute("UPDATE wiki_pending_writes SET content = ? WHERE id = ?",
                     (page.render(), pending["id"]))
        return
    conn.execute(
        "INSERT INTO wiki_pending_writes(path, content, expected_hash) VALUES(?, ?, ?)",
        (relative, page.render(), _file_hash(page.path)),
    )


def ensure(wiki_dir: Path, slug: str, *, title: str | None = None, section: str = "people") -> Page:
    slug = canonical(wiki_dir, slug)
    page = read(wiki_dir, slug)
    if page:
        return page
    page = Page(slug=slug, section=section, path=path_for(wiki_dir, slug, section),
                title=title or slug.replace("-", " ").title())
    write(wiki_dir, page)
    return page


def set_slot(wiki_dir: Path, slug: str, slot: str, value: str, *,
             source: str | None = None, section: str = "people",
             conn=None, inferred: bool = False, commit: bool = True) -> Page:
    """Fill a named slot."""
    with _wiki_write(conn, wiki_dir, commit=commit):
        page = (_page_for_write(conn, wiki_dir, slug, section) if conn is not None
                else read(wiki_dir, slug))
        if page is None:
            normalized = canonical(wiki_dir, slug)
            page = Page(slug=normalized, section=section,
                        path=path_for(wiki_dir, normalized, section),
                        title=normalized.replace("-", " ").title())
        name = slot.strip()
        previous = (page.slots.get(name) or {}).get("value")
        page.slots[name] = {
            "value": value.strip(),
            "source": source or "memcal",
            "ts": db.today().isoformat(),
        }
        if not inferred:
            page.questions = [q for q in page.questions if slot.lower() not in q.lower()]
        if conn is None:
            _write_rendered(page.path, page.render())
            _ALIAS_CACHE.pop(wiki_dir, None)
        elif (previous or "") != value.strip():
            record_slot_change(conn, page.slug, name, previous, value.strip(), source=source)
            _stage_page(conn, wiki_dir, page)
        return page


def slot_claimed_by_another(conn, page: str, slot: str, source: str | None) -> bool:
    """Has another writer already set this slot? Mirrors `events._claimed_by_another`."""
    if conn is None:
        return False
    return conn.execute(
        "SELECT 1 FROM slot_history WHERE page = ? AND slot = ?"
        "   AND coalesce(source, '') <> ? LIMIT 1",
        (page, slot, source or "memcal")).fetchone() is not None


def record_slot_change(conn, page: str, slot: str, old: str | None, new: str | None,
                       *, source: str | None = None) -> None:
    conn.execute(
        "INSERT INTO slot_history(page, slot, old_value, new_value, source, changed_at)"
        " VALUES(?,?,?,?,?,?)", (page, slot, old, new, source, db.now()))


def slot_history(conn, page: str, slot: str | None = None) -> list:
    """What this page used to say. Newest last, the way `event_history` reads."""
    if slot:
        return conn.execute(
            "SELECT * FROM slot_history WHERE page = ? AND slot = ? ORDER BY id",
            (page, slot)).fetchall()
    return conn.execute(
        "SELECT * FROM slot_history WHERE page = ? ORDER BY id", (page,)).fetchall()


def add_question(wiki_dir: Path, slug: str, question: str, section: str = "people", *,
                 conn=None, commit: bool = True) -> Page:
    with _wiki_write(conn, wiki_dir, commit=commit):
        page = (_page_for_write(conn, wiki_dir, slug, section) if conn is not None
                else read(wiki_dir, slug) or ensure(wiki_dir, slug, section=section))
        if question not in page.questions:
            page.questions.append(question)
            if conn is None:
                write(wiki_dir, page)
            else:
                _stage_page(conn, wiki_dir, page)
        return page


def append_body(wiki_dir: Path, slug: str, text: str, section: str = "people") -> Page:
    page = read(wiki_dir, slug) or ensure(wiki_dir, slug, section=section)
    page.body = (page.body + "\n\n" + text.strip()).strip()
    write(wiki_dir, page)
    return page


# Slot taxonomy per entity type. Empty slots are curiosity, never assertions.
SLOTS = {
    "people": ("how we know each other", "where they live", "birthday",
               "partner or family", "work", "what they're into"),
    "places": ("address", "why we go", "who with"),
    "projects": ("who hosts", "where", "how often", "who comes"),
    "preferences": (),
}

MAX_NEW_PAGES_PER_RUN = 12


def autocreate(conn, wiki_dir: Path, *, limit: int = MAX_NEW_PAGES_PER_RUN) -> list[str]:
    """Legacy migration helper: open a page for anyone on an actual memcal row."""
    existing = set(list_pages(wiki_dir))
    created: list[str] = []
    for slug, name in sorted(page_worthy(conn).items()):
        # Aliases resolve to existing pages; never reopen them as new pages.
        slug = canonical(wiki_dir, slug)
        if slug in existing or len(created) >= limit:
            continue
        write(wiki_dir, ensure(wiki_dir, slug, title=name, section="people"))
        created.append(slug)
    return created


def page_worthy(conn) -> dict[str, str]:
    """{slug: name} for everyone on a recent memcal row.

    Single source for page creation and pruning survival, so the pair cannot churn.
    """
    from . import identity

    # Use `db.today` so the pinned test and `--as-of` clock applies here too.
    since = (db.today() - timedelta(days=60)).isoformat()
    wanted: dict[str, str] = {}
    for row in conn.execute(
        "SELECT subject, participants FROM events WHERE date >= ?", (since,)
    ):
        names = db.jload(row["participants"], [])
        if row["subject"]:
            names.append(row["subject"])
        for name in names:
            if not name or name == "me" or identity.is_me(conn, name):
                continue
            # Create pages only for handles that resolve to a name.
            named = identity.resolve(conn, name) if _is_handle(name) else None
            if _is_handle(name) and not named:
                continue
            wanted.setdefault(db.slugify(named or name), named or name)
    return wanted


def _is_handle(name: str) -> bool:
    """A phone number or address, rather than something anyone would call a person."""
    text = (name or "").strip()
    return bool(text) and ("@" in text or re.fullmatch(r"[+\d][\d\s().-]{6,}", text) is not None)


def is_boilerplate(question: str) -> bool:
    """Was this question generated from the slot taxonomy rather than a message?"""
    text = (question or "").strip().rstrip("?").lower()
    _, _, tail = text.partition(":")
    tail = (tail or text).strip()
    return any(tail == slot for slots in SLOTS.values() for slot in slots)


def prune_empty(wiki_dir: Path, *, keep: set[str] | None = None) -> list[str]:
    """Drop boilerplate questions, then delete pages left holding nothing."""
    keep = keep or set()
    removed: list[str] = []
    for slug in list_pages(wiki_dir):
        if slug in keep:
            continue
        page = read(wiki_dir, slug)
        if not page:
            continue
        real = [q for q in page.questions if not is_boilerplate(q)]
        # Aliases are content: deleting the page re-opens the duplicate it closed.
        if page.slots or (page.body or "").strip() or real or page.aliases:
            if len(real) != len(page.questions):
                page.questions = real
                write(wiki_dir, page)
            continue
        page.path.unlink(missing_ok=True)
        removed.append(slug)
    return removed


def retire_obsolete_series(conn, wiki_dir: Path) -> list[str]:
    """Archive generated project pages whose series was renamed or removed.

    The replacement page owns the current history. A page with any hand-written fact,
    prose, alias, or real question is left alone; only `ensure_series` output qualifies.
    """
    retired: list[str] = []
    folder = wiki_dir / "projects"
    if not folder.is_dir():
        return retired
    for path in sorted(folder.glob("*.md")):
        page = parse(path, path.stem, "projects")
        if conn.execute(
                "SELECT 1 FROM events WHERE series = ? LIMIT 1", (page.slug,)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM series WHERE slug = ? LIMIT 1", (page.slug,)).fetchone():
            continue
        if page.aliases or any(not is_boilerplate(q) for q in page.questions):
            continue
        if any((info.get("source") or "") != "memcal" for info in page.slots.values()):
            continue
        remainder = re.sub(r"Recent instances:\n(?:- .*\n?)*", "", page.body).strip()
        if remainder:
            continue
        destination = wiki_dir / ".retired" / "projects" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            stamp = re.sub(r"[^0-9]", "", db.now())
            destination = destination.with_name(f"{path.stem}-{stamp}{path.suffix}")
        os.replace(path, destination)
        retired.append(page.slug)
    if retired:
        _ALIAS_CACHE.pop(wiki_dir, None)
    return retired


def ensure_series(conn, wiki_dir: Path, series: str, *, title: str | None = None) -> Page:
    """A recurring thing gets one page; its instances stay as memcal rows."""
    slug = db.slugify(series)
    page = read(wiki_dir, slug)
    if page is None:
        page = ensure(wiki_dir, slug, title=title or series.replace("-", " ").title(),
                      section="projects")
        page.questions = [f"{q}?" for q in SLOTS["projects"]]
    rows = conn.execute(
        "SELECT key, date, location FROM events WHERE series = ? ORDER BY date DESC LIMIT 6", (series,)
    ).fetchall()
    if rows:
        seen = [f"- {r['date']}" + (f" — {r['location']}" if r["location"] else "") for r in rows]
        body = "Recent instances:\n" + "\n".join(seen)
        page.body = re.sub(r"Recent instances:\n(?:- .*\n?)*", "", page.body).strip()
        page.body = (page.body + "\n\n" + body).strip()
    write(wiki_dir, page)
    return _fill_where(conn, wiki_dir, slug, series, rows) or page


def _fill_where(conn, wiki_dir: Path, slug: str, series: str, rows) -> Page | None:
    """Derive where a repeating thing happens, without overwriting another source."""
    from . import series as series_mod           # series imports wiki
    rule = series_mod.get(conn, slug)
    # The rule owns the location; instances are the fallback for undeclared series.
    where = (rule.location if rule and rule.location else
             next((r["location"] for r in rows if r["location"]), None))
    if not where:
        return None
    # A re-derivation may restate itself and may not overrule another source.
    if slot_claimed_by_another(conn, slug, "where", "memcal"):
        return None
    current = read(wiki_dir, slug)
    if current and (current.slots.get("where") or {}).get("value") == where:
        return current
    updated = set_slot(wiki_dir, slug, "where", where, source="memcal",
                       section="projects", conn=conn, inferred=True)
    source_row = next((row for row in rows if row["location"] == where), None)
    if source_row is not None:
        from . import trace
        archive_ids = [row["id"] for row in trace.source_rows(
            conn, "event", source_row["key"], context=0) if row["evidence"]]
        trace.stamp(conn, kind="wiki", ref=f"{slug}.where", verb="derived",
                    entity=f"event:{source_row['key']}", archive_ids=archive_ids)
        conn.commit()
    return updated


def link_series(conn, wiki_dir: Path) -> list[str]:
    """Find repeats and give them a series page. Two poker games are two rows, one page."""
    linked: list[str] = []
    rows = conn.execute(
        "SELECT id, title, date, series, coalesce(origin, source) AS came_from FROM events"
    ).fetchall()
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["series"] or db.slugify(row["title"]), []).append(row)
    for series, members in groups.items():
        if len(members) < 2 or not series:
            continue
        # A series needs two distinct days; same-day duplicates are one occasion.
        if len({str(m["date"]) for m in members}) < 2:
            continue
        # Skip repetitions owned solely by a subscribed feed; one user mention
        # makes the repetition theirs.
        if all(str(m["came_from"] or "").startswith("ical:subscribed") for m in members):
            continue
        for member in members:
            if member["series"] != series:
                conn.execute("UPDATE events SET series = ? WHERE id = ?", (series, member["id"]))
        conn.commit()
        ensure_series(conn, wiki_dir, series)
        linked.append(series)
    return linked


def context_for(wiki_dir: Path, slugs: list[str], max_chars: int = 4000) -> str:
    """Wiki pages for the entities in a bundle, as the model sees them."""
    chunks = []
    seen: set[str] = set()
    for slug in slugs:
        # Two participants may be two names for one person; render their page once.
        if canonical(wiki_dir, slug) in seen:
            continue
        seen.add(canonical(wiki_dir, slug))
        page = read(wiki_dir, slug)
        if page:
            chunks.append(page.render().strip())
    joined = "\n\n---\n\n".join(chunks)
    return joined[:max_chars]


def mentioned_pages(wiki_dir: Path, text: str, *, limit: int = 3) -> list[Page]:
    """Recall material pages by name mention, without a model call."""
    haystack = (text or "").casefold()
    if not haystack:
        return []
    found: list[tuple[int, Page]] = []
    for slug in list_pages(wiki_dir):
        page = read(wiki_dir, slug)
        if not page or not is_material(page):
            continue
        full_names = [page.title, *page.aliases, slug.replace("-", " ")]
        # Short fragments match only as recorded aliases; otherwise common
        # syllables become global triggers.
        title_parts = [part for name in (page.title, slug.replace("-", " "))
                       for part in re.findall(r"[\w'-]+", name or "")
                       if len(part) >= 3]
        names = [*full_names, *title_parts]
        hits = [name for name in names if name and re.search(
            rf"(?<!\w){re.escape(name.casefold())}(?!\w)", haystack)]
        if hits:
            found.append((max(len(name) for name in hits), page))
    return [page for _score, page in sorted(found, key=lambda pair: -pair[0])[:limit]]


def search_facts(wiki_dir: Path, query: str, limit: int = 10) -> list[dict]:
    """Bounded case-insensitive substring scan over stored wiki facts.

    Scans page names/slugs, aliases, fact labels, fact values, and body
    prose — all facts, not the brief index's first four. No new persisted
    index, no model call. Returns at most `limit` pages in sorted slug order;
    `len(result) == limit` means truncation is possible. Empty query returns
    `[]`, as does no match; the tool layer distinguishes the latter ("no
    matching wiki fact") from "the user never told us".

    Each hit is `{slug, section, matched, facts, provenance}` where `matched`
    names the fields that hit (`name`, `alias`, `label`, `value`, `body`),
    `facts` is the page's full slot dict, and `provenance` holds
    `{slot: {source, ts}}` for the slots whose label or value matched.
    """
    needle = (query or "").strip().casefold()
    if not needle or limit <= 0:
        return []
    hits: list[dict] = []
    for slug in list_pages(wiki_dir):
        page = read(wiki_dir, slug)
        if not page:
            continue
        matched: list[str] = []
        if needle in slug.casefold() or needle in (page.title or "").casefold():
            matched.append("name")
        if any(needle in (alias or "").casefold() for alias in page.aliases):
            matched.append("alias")
        label_hit, value_hit = False, False
        matched_slots: list[str] = []
        for slot, info in page.slots.items():
            on_label = needle in slot.casefold()
            on_value = needle in str(info.get("value") or "").casefold()
            if on_label:
                label_hit = True
            if on_value:
                value_hit = True
            if on_label or on_value:
                matched_slots.append(slot)
        if label_hit:
            matched.append("label")
        if value_hit:
            matched.append("value")
        if needle in (page.body or "").casefold():
            matched.append("body")
        if not matched:
            continue
        hits.append({
            "slug": page.slug,
            "section": page.section,
            "matched": matched,
            "facts": {slot: {"value": info.get("value", ""),
                             "source": info.get("source"),
                             "ts": info.get("ts")}
                      for slot, info in page.slots.items()},
            "provenance": {slot: {"source": (page.slots[slot] or {}).get("source"),
                                  "ts": (page.slots[slot] or {}).get("ts")}
                           for slot in matched_slots},
        })
        if len(hits) >= limit:
            break
    return hits


def is_material(page: Page) -> bool:
    """Whether a page has earned its file and prompt space."""
    return bool(page.slots or page.aliases or page.questions or (page.body or "").strip())


def encounter_summary(conn, page: Page, *, limit: int = 6) -> dict:
    """Past in-person rows for this page, projected from events."""
    names = {db.slugify(page.slug), db.slugify(page.title)}
    names |= {db.slugify(alias) for alias in page.aliases}
    matched = []
    for row in conn.execute(
        """SELECT * FROM events
            WHERE status != 'declined' AND (status = 'happened' OR date < ?)
            ORDER BY date DESC, id DESC""", (db.today().isoformat(),)
    ):
        people = db.jload(row["participants"], [])
        people.append(row["subject"])
        if not any(db.slugify(name or "") in names for name in people):
            continue
        matched.append(row)
    kinds = Counter((row["series"] or db.slugify(row["title"])) for row in matched)
    labels = {}
    for row in matched:
        labels.setdefault(row["series"] or db.slugify(row["title"]), row["title"])
    return {
        "count": len(matched),
        "by_activity": [{"activity": labels[key], "count": count}
                        for key, count in kinds.most_common(8)],
        "recent": [{
            # Include keys so each encounter can be opened.
            "key": row["key"], "date": row["date"], "title": row["title"],
            "location": row["location"],
            "with": [name for name in db.jload(row["participants"], [])
                     if db.slugify(name) not in names],
        } for row in matched[:limit]],
    }


def profile(conn, wiki_dir: Path, slug: str, *, context: int = 0) -> dict | None:
    """A stored page plus computed encounters and exact source lines for its facts."""
    page = read(wiki_dir, slug)
    if not page:
        return None
    from . import trace
    sources = {
        slot: trace.source_rows(conn, "wiki", f"{page.slug}.{slot.lower()}",
                                context=context)
        for slot in page.slots
    }
    return {
        "slug": page.slug,
        "title": page.title or page.slug,
        "section": page.section,
        # Same slot list the brief index prints, so the two cannot disagree.
        "answers": list(page.slots),
        "facts": [{"slot": name, **info} for name, info in page.slots.items()],
        "aliases": list(page.aliases),
        "open_questions": list(page.questions),
        "page": page.render(),
        "encounters": encounter_summary(conn, page),
        "sources": {slot: rows for slot, rows in sources.items() if rows},
        # Distinguish quoted lines from neighbouring context.
        "narrow": {slot: trace.citations(conn, "wiki", f"{page.slug}.{slot.lower()}")["narrow"]
                   for slot in page.slots},
    }
