"""GGG trade2 stat-catalog client and mod-text matcher.

Fetches https://www.pathofexile.com/api/trade2/data/stats (big JSON catalog
of every trade-searchable stat with their text template e.g. "# to maximum Life"),
caches it in-memory, and offers `match_mod()` to map a PoB item-mod line like
"+120 to maximum Life" to a trade stat ID such as "explicit.stat_3299347043".
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import httpx


STATS_URL = "https://www.pathofexile.com/api/trade2/data/stats"
CACHE_TTL = 24 * 3600  # re-fetch daily

_lock = threading.Lock()
_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0, "index": None}


def _normalize(text: str) -> str:
    """Collapse whitespace, strip leading + / -, and replace all numbers with #."""
    t = text.strip()
    # Remove leading +/- sign
    t = re.sub(r"^[+\-]", "", t)
    # Replace integers/decimals with "#", including negatives like -25
    t = re.sub(r"-?\d+(?:\.\d+)?", "#", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t)
    return t


def _kind_to_trade_type(kind: str) -> str:
    return {
        "explicit": "explicit",
        "implicit": "implicit",
        "enchant": "enchant",
        "crafted": "crafted",
        "fractured": "fractured",
        "rune": "rune",
    }.get(kind, "explicit")


def _build_index(stats_result: list[dict]) -> dict[tuple[str, str], dict]:
    """(type, normalized_text) -> stat record."""
    idx: dict[tuple[str, str], dict] = {}
    for group in stats_result:
        group_type = (group.get("id") or "").lower()  # "explicit","implicit","fractured",...
        for entry in (group.get("entries") or []):
            raw = entry.get("text", "")
            norm = _normalize(raw)
            key = (group_type, norm)
            if key not in idx:
                idx[key] = entry
    return idx


def fetch_stats(force: bool = False) -> dict[str, Any]:
    """Fetch + cache GGG's trade2 stat catalog. Returns the parsed JSON."""
    with _lock:
        now = time.time()
        if (not force) and _cache["data"] and now - _cache["fetched_at"] < CACHE_TTL:
            return _cache["data"]
        headers = {
            "User-Agent": "poe2-companion/0.1 (self-hosted, jbaker@local)",
            "Accept": "application/json",
        }
        with httpx.Client(timeout=20.0, headers=headers) as client:
            r = client.get(STATS_URL)
            r.raise_for_status()
            data = r.json()
        _cache["data"] = data
        _cache["fetched_at"] = now
        _cache["index"] = _build_index(data.get("result") or [])
        return data


def _get_index() -> dict[tuple[str, str], dict]:
    if _cache["index"] is None:
        fetch_stats()
    return _cache["index"] or {}


_VAL_RE = re.compile(r"-?\d+(?:\.\d+)?")


def match_mod(text: str, kind: str = "explicit") -> dict | None:
    """Try to match a PoB mod line to a GGG stat entry.

    Falls back through related stat-types because PoE2 often has the same
    stat available as explicit, fractured, desecrated, rune, etc.
    Returns None if no match.
    """
    idx = _get_index()
    norm = _normalize(text)
    types_to_try: list[str] = []
    base = _kind_to_trade_type(kind)
    types_to_try.append(base)
    # Fallback cascade
    for alt in ("explicit", "implicit", "enchant", "rune", "crafted", "fractured"):
        if alt not in types_to_try:
            types_to_try.append(alt)

    for t in types_to_try:
        entry = idx.get((t, norm))
        if entry:
            return dict(entry, _matched_type=t)
    return None


def extract_values(text: str) -> list[float]:
    """Pull all numeric values from a mod line (useful for `min` defaults)."""
    out: list[float] = []
    for m in _VAL_RE.findall(text):
        try:
            out.append(float(m))
        except ValueError:
            pass
    return out
