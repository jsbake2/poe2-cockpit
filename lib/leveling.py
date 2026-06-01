"""Scrapers for PoE2 leveling-build guides.

- maxroll.gg/poe2/build-guides: index-only (their planner isn't iframeable
  and no PoB export exposed). User clicks through to grab PoB manually.
- mobalytics.gg/poe-2/starter-builds: pobb.in links ARE in the rendered HTML
  (no JS required) so we can auto-import them.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any

import httpx


MAXROLL_INDEX = "https://maxroll.gg/poe2/build-guides"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (poe2-companion)"

# PoE2 base classes and their ascendancies (0.5 "Return of the Ancients").
# 0.5.0 introduced two new ascendancies: Martial Artist (Monk), Spirit Walker (Huntress).
CLASSES: dict[str, list[str]] = {
    "Witch":     ["Infernalist", "Blood Mage", "Lich"],
    "Sorceress": ["Stormweaver", "Chronomancer"],
    "Monk":      ["Invoker", "Acolyte of Chayula", "Martial Artist"],
    "Ranger":    ["Deadeye", "Pathfinder"],
    "Mercenary": ["Witchhunter", "Gemling Legionnaire", "Tactician"],
    "Huntress":  ["Amazon", "Ritualist", "Spirit Walker"],
    "Warrior":   ["Titan", "Warbringer", "Smith of Kitava"],
    "Druid":     ["Oracle", "Shaman"],
}

# Keywords that hint a guide is leveling-focused
LEVELING_KEYWORDS = ("leveling", "level ", "1-6", "1-8", "1-9", "campaign")


def _infer_class_ascendancy(title: str) -> tuple[str, str]:
    # Prefer the longest matching ascendancy name so "Abyssal Lich" wins over
    # "Lich", "Acolyte of Chayula" wins over any shorter conflict, etc.
    lower = title.lower()
    best_class = ""
    best_asc = ""
    best_len = 0
    for cls, ascs in CLASSES.items():
        for asc in ascs:
            asc_l = asc.lower()
            if asc_l in lower and len(asc_l) > best_len:
                best_class, best_asc, best_len = cls, asc, len(asc_l)
        if not best_asc and cls.lower() in lower:
            best_class = cls
    return best_class, best_asc


def _is_leveling(title: str, slug: str) -> bool:
    t = (title + " " + slug).lower()
    return any(kw in t for kw in LEVELING_KEYWORDS)


_LINK_RE = re.compile(
    r'href="(/poe2/build-guides/([a-z0-9-]+))"[^>]*>'
    r'(?:\s*<[^>]+>\s*)*([^<]{3,140})',
    re.I,
)

MOBALYTICS_INDEX = "https://mobalytics.gg/poe-2/starter-builds"
MOBALYTICS_BUILD_RE = re.compile(r'/poe-2/builds/([a-z0-9][a-z0-9-]{3,80})')
POBB_IN_RE = re.compile(r'pobb\.in/([A-Za-z0-9_-]{6,30})')
META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{20,300})',
    re.I,
)
META_TITLE_RE = re.compile(r'<title>([^<]{3,180})</title>', re.I)


def scrape_index() -> list[dict[str, Any]]:
    """Fetch + parse maxroll's build-guide index. Returns list of:
       {title, slug, url, class, ascendancy, is_leveling, discovered_at}
    """
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as c:
        r = c.get(MAXROLL_INDEX)
        r.raise_for_status()
        html = r.text

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    seen: dict[str, dict] = {}
    for path, slug, title in _LINK_RE.findall(html):
        title = re.sub(r"\s+", " ", title).strip()
        if not title or slug in seen:
            continue
        cls, asc = _infer_class_ascendancy(title)
        seen[slug] = {
            "title": title,
            "slug": slug,
            "url": "https://maxroll.gg" + path,
            "class": cls,
            "ascendancy": asc,
            "is_leveling": _is_leveling(title, slug),
            "discovered_at": now,
            "source": "maxroll",
        }
    return list(seen.values())


def scrape_leveling_only() -> list[dict[str, Any]]:
    """Return just the leveling guides."""
    return [g for g in scrape_index() if g["is_leveling"]]


def _derive_title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


_MOBALYTICS_CATEGORY_RE = re.compile(r'/poe-2/([a-z][a-z0-9-]+)-starter-builds')


def scrape_mobalytics(limit: int = 80) -> list[dict[str, Any]]:
    """Fetch mobalytics.gg/poe-2/starter-builds + each ascendancy starter-builds
    index page, collect individual build slugs, and extract the pobb.in PoB
    link from each build page. Returns records with title, slug, url, class,
    ascendancy, is_leveling=True, pob_code.

    Network-heavy (index + per-category + per-build), sequential. Acceptable
    for a manual user-initiated scan.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as c:
        r = c.get(MOBALYTICS_INDEX)
        r.raise_for_status()
        index_html = r.text
        slug_set: set[str] = set(MOBALYTICS_BUILD_RE.findall(index_html))
        # Follow category pages to get per-ascendancy starter builds
        categories = sorted(set(_MOBALYTICS_CATEGORY_RE.findall(index_html)))
        for cat in categories:
            try:
                cat_html = c.get(f"https://mobalytics.gg/poe-2/{cat}-starter-builds").text
                slug_set.update(MOBALYTICS_BUILD_RE.findall(cat_html))
            except Exception:
                continue
        slugs = sorted(slug_set)[:limit]
        out: list[dict[str, Any]] = []
        for slug in slugs:
            url = f"https://mobalytics.gg/poe-2/builds/{slug}"
            try:
                bh = c.get(url).text
            except Exception:
                continue
            m_pobb = POBB_IN_RE.search(bh)
            pob_code = f"https://pobb.in/{m_pobb.group(1)}" if m_pobb else ""
            desc = META_DESC_RE.search(bh)
            title_raw = desc.group(1) if desc else _derive_title_from_slug(slug)
            # Mobalytics descriptions are "Conquer the game with [0.5] BALROG - ... Fire Bear Smith of Kitava PoE 2 Warrior build!..."
            # The [0.X] version prefix is stripped by the regex below regardless of patch.
            title = title_raw.split("! Learn")[0].strip()
            title = re.sub(r"^Conquer the game with\s*(?:\[[^\]]+\]\s*)?", "", title).strip()
            title = title.rstrip(" -")
            # Decode HTML entities (&#x27; → ')
            title = html.unescape(title)[:140]
            cls, asc = _infer_class_ascendancy(title + " " + slug)
            out.append({
                "title": title or _derive_title_from_slug(slug),
                "slug": "mobalytics-" + slug,
                "url": url,
                "class": cls,
                "ascendancy": asc,
                "is_leveling": True,  # mobalytics labels these as starter builds
                "discovered_at": now,
                "source": "mobalytics",
                "pob_code": pob_code,
            })
        return out


def find_for_class(guides: list[dict], cls: str, asc: str = "") -> list[dict]:
    """Pick leveling guides relevant to a given class/ascendancy.

    Preference order:
      1. Same ascendancy (e.g., Infernalist).
      2. Same base class (e.g., Witch).
      3. Any leveling guide (fallback — empty list returned if the user
         doesn't want the fallback).
    """
    if asc:
        matches = [g for g in guides if g["is_leveling"] and g["ascendancy"].lower() == asc.lower()]
        if matches:
            return matches
    if cls:
        return [g for g in guides if g["is_leveling"] and g["class"].lower() == cls.lower()]
    return []
