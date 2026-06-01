"""PoE2-specific news + patch-notes fetcher.

GGG's general RSS (pathofexile.com/news/rss) is mixed PoE1/PoE2 and mostly
PoE1. Instead we scrape the *PoE2-specific* forum sections:
  - Forum 2211: Early Access Announcements
  - Forum 2212: Early Access Patch Notes

Each thread row is parsed for title, link, timestamp, and author. Results
from both forums are merged, sorted newest-first, and cached 15 minutes.
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
import time
from typing import Any

import httpx


UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
      "Gecko/20100101 Firefox/140.0")
CACHE_TTL = 15 * 60
DEFAULT_WINDOW_DAYS = 28  # show last 4 weeks only

FORUMS: dict[int, str] = {
    2211: "Early Access Announcements",
    2212: "Early Access Patch Notes",
}

_lock = threading.Lock()
_cache: dict[str, Any] = {"items": None, "fetched_at": 0.0}


_PATCH_NOTE_HINTS = (
    "patch notes", "patch note", "hotfix", "update note",
    "changelog", "game updates",
)


def _is_patch_note(title: str, forum_id: int) -> bool:
    if forum_id == 2212:  # patch-notes forum: every thread counts
        return True
    t = title.lower()
    if any(h in t for h in _PATCH_NOTE_HINTS):
        return True
    if re.search(r"\b\d+\.\d+(?:\.\d+)?[a-z]?\b", title):
        return True
    return False


_TR_SPLIT_RE = re.compile(r"<tr[^>]*>", re.I)
_THREAD_LINK_RE = re.compile(
    r'href="(/forum/view-thread/(\d+))[^"#]*"[^>]*>([^<]{3,220})</a>',
    re.I,
)
_DATE_RE = re.compile(r'class="post_date"[^>]*>([^<]+)</span>', re.I)
_USER_RE = re.compile(r"/account/view-profile/([^/\"']+)/", re.I)


def _parse_forum_date(raw: str) -> _dt.datetime | None:
    """Parse e.g. ', Apr 19, 2026, 6:32:09 PM'."""
    s = raw.strip().lstrip(",").strip()
    # Common formats
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y %I:%M:%S %p",
                "%b %d, %Y, %I:%M %p"):
        try:
            return _dt.datetime.strptime(s, fmt).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
    return None


def fetch_forum(forum_id: int) -> list[dict]:
    """Scrape one PoE forum section's thread listing."""
    with httpx.Client(timeout=15, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        r = c.get(f"https://www.pathofexile.com/forum/view-forum/{forum_id}")
        r.raise_for_status()
        html = r.text

    items: list[dict] = []
    for row in _TR_SPLIT_RE.split(html):
        m = _THREAD_LINK_RE.search(row)
        if not m:
            continue
        _, tid, title = m.groups()
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        date_m = _DATE_RE.search(row)
        user_m = _USER_RE.search(row)
        dt = _parse_forum_date(date_m.group(1)) if date_m else None
        items.append({
            "title": title,
            "link": f"https://www.pathofexile.com/forum/view-thread/{tid}",
            "pub_date": dt.strftime("%a, %d %b %Y %H:%M:%S +0000") if dt else "",
            "_pub_ts": dt.timestamp() if dt else 0,
            "author": user_m.group(1) if user_m else "",
            "summary": f"PoE 2 · {FORUMS.get(forum_id, 'Forum')}",
            "is_patch_note": _is_patch_note(title, forum_id),
            "forum_id": forum_id,
        })
    return items


def fetch_items(force: bool = False, window_days: int = DEFAULT_WINDOW_DAYS
                ) -> list[dict]:
    with _lock:
        now = time.time()
        if (not force) and _cache["items"] is not None and now - _cache["fetched_at"] < CACHE_TTL:
            # Apply window filter even on cache hit so a config change takes effect
            cutoff = now - (window_days * 86400) if window_days else 0
            if cutoff:
                return [it for it in _cache["items"]
                        if (it.get("_pub_ts") or 0) >= cutoff]
            return _cache["items"]
        merged: list[dict] = []
        for fid in FORUMS:
            try:
                merged.extend(fetch_forum(fid))
            except Exception:
                continue
        # Dedup by link (newest-first ordering so the first occurrence wins)
        seen: set[str] = set()
        dedup: list[dict] = []
        for it in sorted(merged, key=lambda x: -x.get("_pub_ts", 0)):
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            dedup.append(it)  # keep _pub_ts for window filtering on re-query
        _cache["items"] = dedup
        _cache["fetched_at"] = now
        # Apply window filter on the way out + strip the internal field
        cutoff = now - (window_days * 86400) if window_days else 0
        out = []
        for it in dedup:
            if cutoff and (it.get("_pub_ts") or 0) < cutoff:
                continue
            out.append({k: v for k, v in it.items() if not k.startswith("_")})
        return out
