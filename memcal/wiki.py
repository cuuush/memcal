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

    Which slug survives is the user's call, not the passport's — the user ended the session
    that prompted this with "Well I call them robbie it's easier", so `robbie` is the page
    and the legal name is a slot on it. The survivor's own wording always wins: a merge
    may add what was missing, never overwrite what the user already confirmed here.
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
        # Anything the parser doesn't recognize is prose the user typed. Keep it — the user edits
        # these by hand, and a lossy round trip would eat their notes.
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
    """Has anyone but this writer already set this slot on this page?

    `events._claimed_by_another` asked exactly this of `event_history`, and
    `slot_history` is the wiki's `event_history` — so the guard transfers whole. It is
    the question any re-derived value has to ask before restating itself, and the wiki
    had no equivalent: `ensure_series` re-derived `where` on every nightly pass and
    overwrote a correction the user had made, with no history row to say it ever
    existed.
    """
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


# The slot taxonomy per entity type. §12 leaves this open; these are a starting point,
# and they are only ever *questions* — an empty slot is curiosity, never an assertion.
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
        # An alias is someone we already have. Without this the wiki heals the
        # duplicate on merge and then re-opens it on the very next run, because the
        # rows still carry both names.
        slug = canonical(wiki_dir, slug)
        if slug in existing or len(created) >= limit:
            continue
        write(wiki_dir, ensure(wiki_dir, slug, title=name, section="people"))
        created.append(slug)
    return created


def page_worthy(conn) -> dict[str, str]:
    """{slug: name} for everyone standing on a memcal row lately.

    Both halves of the wiki's lifecycle read this: it decides who gets a page, and it
    decides whose empty page survives pruning. Splitting those two judgements is what
    would make the pair churn — creating Pat's page every run and deleting it again
    on the next, because the user is worth a page but has no facts on it yet.
    """
    from . import identity

    # `db.today()`, not SQLite's `now`: the clock is pinned under test and by
    # `--as-of`, and a wiki that reads the wall clock decides who matters on a
    # different day from the one everything else in the pass is reasoning about.
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
    """Was this question generated from the slot taxonomy rather than from a message?

    "Katie: birthday?" is a form field. "mom: Who is Bailey, and is their birthday June
    26?" came from reading their mail. Only the first kind is safe to throw away.
    """
    text = (question or "").strip().rstrip("?").lower()
    _, _, tail = text.partition(":")
    tail = (tail or text).strip()
    return any(tail == slot for slots in SLOTS.values() for slot in slots)


def prune_empty(wiki_dir: Path, *, keep: set[str] | None = None) -> list[str]:
    """Drop boilerplate questions, then delete any page left holding nothing.

    A page with no facts, no body and no real question is a slug charged to every
    prompt forever for the privilege of repeating a name already in the handles table.
    """
    keep = keep or set()
    removed: list[str] = []
    for slug in list_pages(wiki_dir):
        if slug in keep:
            continue
        page = read(wiki_dir, slug)
        if not page:
            continue
        real = [q for q in page.questions if not is_boilerplate(q)]
        # Aliases are content: this page is the only place recording that two names are
        # one person, and deleting it re-opens the duplicate it was created to close.
        if page.slots or (page.body or "").strip() or real or page.aliases:
            if len(real) != len(page.questions):
                page.questions = real
                write(wiki_dir, page)
            continue
        page.path.unlink(missing_ok=True)
        removed.append(slug)
    return removed


def ensure_series(conn, wiki_dir: Path, series: str, *, title: str | None = None) -> Page:
    """A recurring thing gets one page; its instances stay as memcal rows.

    "Where was poker last time" should be a page read, not an archive search.
    """
    slug = db.slugify(series)
    page = read(wiki_dir, slug)
    if page is None:
        page = ensure(wiki_dir, slug, title=title or series.replace("-", " ").title(),
                      section="projects")
        page.questions = [f"{q}?" for q in SLOTS["projects"]]
    rows = conn.execute(
        "SELECT date, location FROM events WHERE series = ? ORDER BY date DESC LIMIT 6", (series,)
    ).fetchall()
    if rows:
        seen = [f"- {r['date']}" + (f" — {r['location']}" if r["location"] else "") for r in rows]
        body = "Recent instances:\n" + "\n".join(seen)
        page.body = re.sub(r"Recent instances:\n(?:- .*\n?)*", "", page.body).strip()
        page.body = (page.body + "\n\n" + body).strip()
    write(wiki_dir, page)
    return _fill_where(conn, wiki_dir, slug, series, rows) or page


def _fill_where(conn, wiki_dir: Path, slug: str, series: str, rows) -> Page | None:
    """Say where a repeating thing happens, without ever overwriting somebody."""
    from . import series as series_mod           # series imports wiki
    rule = series_mod.get(conn, slug)
    # Invariant 12: the rule owns where it happens. Instances are the fallback, for a
    # series observed before it was ever declared.
    where = (rule.location if rule and rule.location else
             next((r["location"] for r in rows if r["location"]), None))
    if not where:
        return None
    # Invariant 13, via the guard `events.upsert` already uses: a re-derivation may
    # restate itself and may not overrule anyone else.
    if slot_claimed_by_another(conn, slug, "where", "memcal"):
        return None
    return set_slot(wiki_dir, slug, "where", where, source="memcal",
                    section="projects", conn=conn, inferred=True)


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
        # A series is a thing that happens *again*, so it needs two days. Same title,
        # same day is one occasion counted twice, and counting it as a repeat is how
        # every duplicated calendar block came to have a page asking who hosts it:
        # "Break", "Lunch", "Rest 10 min", "Math", and Improv 101 as six series of one.
        # The duplicates are fixed at the source; this is the invariant that makes a
        # duplicate anywhere unable to invent a project again.
        if len({str(m["date"]) for m in members}) < 2:
            continue
        # A page here asks "who hosts?", "where?", "how often?", "who comes?" — the
        # questions you would ask about a thing the user organises. A subscribed calendar
        # repeating an annual entry is not that: Easter 2026 and Easter 2027 are two
        # rows, so `projects/easter.md` was opened wanting to know who hosts Easter.
        # Along with Passover, Ashura, Good Friday, Tax Day and Independence Day.
        #
        # The test is *whose* repetition it is. If nothing but a feed ever mentioned it,
        # the repetition belongs to the feed's publishing schedule. One chat message
        # about poker is enough to make poker their again.
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
    """Material pages whose title, slug, or nickname appears in a user turn.

    This is the cheap dynamic-recall path for Hermes. Saying "Quinn" can pull their
    page into that turn without a model call or an archive search. Only pages that
    already hold something are candidates, so a common first name cannot conjure an
    empty contact card into every conversation.
    """
    haystack = (text or "").casefold()
    if not haystack:
        return []
    found: list[tuple[int, Page]] = []
    for slug in list_pages(wiki_dir):
        page = read(wiki_dir, slug)
        if not page or not is_material(page):
            continue
        full_names = [page.title, *page.aliases, slug.replace("-", " ")]
        # A page titled "Quinn Brooks" should appear when a normal conversation
        # says "Quinn". Keep short title fragments out (Al, Q, Li) unless they were
        # deliberately recorded as aliases; otherwise common syllables become global
        # memory triggers.
        title_parts = [part for name in (page.title, slug.replace("-", " "))
                       for part in re.findall(r"[\w'-]+", name or "")
                       if len(part) >= 3]
        names = [*full_names, *title_parts]
        hits = [name for name in names if name and re.search(
            rf"(?<!\w){re.escape(name.casefold())}(?!\w)", haystack)]
        if hits:
            found.append((max(len(name) for name in hits), page))
    return [page for _score, page in sorted(found, key=lambda pair: -pair[0])[:limit]]


def is_material(page: Page) -> bool:
    """Whether a page has earned its file and prompt space."""
    return bool(page.slots or page.aliases or page.questions or (page.body or "").strip())


def encounter_summary(conn, page: Page, *, limit: int = 6) -> dict:
    """Past in-person rows involving this page, computed from events for free.

    The wiki should not duplicate an encounter ledger by hand. Events already know
    that poker happened with Robbie; this projection answers "how many times?" and
    "when was the last one?" whenever the page is opened.
    """
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
            # The key so the page can open the event itself, not merely describe it —
            # "Poker night, 3 times" is a fact you can now follow to each of the three.
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
        # What this page is useful for, in its own words rather than a description of
        # pages in general. It is the same list the brief's index prints, so "the
        # brief said this page knows X" and "the page knows X" cannot disagree.
        "answers": list(page.slots),
        "facts": [{"slot": name, **info} for name, info in page.slots.items()],
        "aliases": list(page.aliases),
        "open_questions": list(page.questions),
        "page": page.render(),
        "encounters": encounter_summary(conn, page),
        "sources": {slot: rows for slot, rows in sources.items() if rows},
        # Which slots point at the lines that made them, and which point at a whole
        # conversation because nothing could be narrowed. `source_rows` recovers the
        # spool bundle for anything written before line-level citation existed and
        # marks every line of it `evidence: true`, so without this a reader cannot
        # tell a quote from a neighbour — and `casey-morgan.education` reads
        # as though "computer science" was stated by "Yooooo how's it going".
        "narrow": {slot: trace.citations(conn, "wiki", f"{page.slug}.{slot.lower()}")["narrow"]
                   for slot in page.slots},
    }
