"""Example RSS/Atom source plugin using the shared delivery gate."""

from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from memcal.sources import Source, SourceError, deliver, register, set_watermark, watermark


@register
class RSSSource(Source):
    name = "rss"
    description = "Items from RSS/Atom feeds listed in RSS_FEEDS"
    secrets = ()          # nothing secret; `check` below verifies configuration instead
    in_all = True         # include me in `memcal ingest all`
    order = 70            # run after the chatty sources

    # ------------------------------------------------------------------ config --
    def feeds(self, cfg) -> list[str]:
        raw = cfg.secret("RSS_FEEDS", "rssfeeds") or ""
        return [url.strip() for url in raw.replace("\n", ",").split(",") if url.strip()]

    def check(self, cfg):
        feeds = self.feeds(cfg)
        if not feeds:
            return False, "set RSS_FEEDS=<comma-separated urls> in .env"
        return True, f"{len(feeds)} feed(s) configured"

    # ------------------------------------------------------------------- fetch --
    def fetch(self, conn, cfg, report, limit):
        feeds = self.feeds(cfg)
        if not feeds:
            raise SourceError("no feeds configured (RSS_FEEDS)")

        budget = limit
        for url in feeds:
            if budget <= 0:
                break
            seen = watermark(conn, f"rss.{url}")
            newest = seen
            for entry in self._entries(url)[:budget]:
                budget -= 1
                if entry["id"] <= seen:
                    continue
                newest = max(newest, entry["id"])
                deliver(
                    conn, report,
                    stream=self.name,
                    external_id=entry["id"],
                    ts=entry["ts"],
                    text=entry["text"],
                    thread=entry["feed"],
                    handle=None,          # a feed has no person behind it
                    person=None,
                    from_me=False,
                    meta={"link": entry["link"], "feed_url": url},
                )
            if newest != seen:
                set_watermark(conn, f"rss.{url}", newest)

    # ------------------------------------------------------------------- parse --
    def _entries(self, url: str) -> list[dict]:
        request = urllib.request.Request(url, headers={"User-Agent": "memcal-rss/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except Exception as exc:
            raise SourceError(f"cannot fetch {url}: {exc}") from exc
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise SourceError(f"{url} is not valid XML: {exc}") from exc

        feed_title = _text(root, ".//{*}title") or url
        out: list[dict] = []
        for node in root.iter():
            if not node.tag.endswith(("}item", "}entry", "item", "entry")):
                continue
            title = _text(node, "{*}title") or ""
            summary = _text(node, "{*}description") or _text(node, "{*}summary") or ""
            link = _text(node, "{*}link") or (node.find("{*}link").get("href")
                                              if node.find("{*}link") is not None else "")
            guid = _text(node, "{*}guid") or _text(node, "{*}id") or link or title
            stamp = (_text(node, "{*}pubDate") or _text(node, "{*}updated")
                     or _text(node, "{*}published"))
            body = f"{title}\n{_strip_html(summary)}".strip()
            if body:
                out.append({"id": str(guid), "feed": feed_title, "link": link,
                            "ts": _to_iso(stamp), "text": body[:1500]})
        return out


def _text(node, path: str) -> str:
    found = node.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def _strip_html(html: str) -> str:
    return re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _to_iso(stamp: str | None) -> str:
    if stamp:
        for parse in (parsedate_to_datetime, datetime.fromisoformat):
            try:
                value = parse(stamp)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.astimezone().isoformat(timespec="seconds")
            except (TypeError, ValueError):
                continue
    return datetime.now().astimezone().isoformat(timespec="seconds")
