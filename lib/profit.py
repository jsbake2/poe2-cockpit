"""Profit-filter feature.

Manages a list of PoE2 unique items flagged as "worth picking up to sell".
Fetches the canonical unique list from GGG's trade2/data/items endpoint,
lets jbaker curate a profit shortlist, and injects a highlight block into
a copy of the user's NeverSink loot filter (leaving the original intact).

Live poe.ninja "top builds" scraping requires their internal API, which is
currently undocumented/gated. We ship a curated seed list of known-valuable
PoE2 uniques to get the feature live; jbaker can add/remove via the UI.
"""

from __future__ import annotations

import functools
import threading
import time
from pathlib import Path
from typing import Any

import httpx


ITEMS_URL = "https://www.pathofexile.com/api/trade2/data/items"
CACHE_TTL = 12 * 3600

_lock = threading.Lock()
_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


# Known-valuable PoE2 uniques to seed the profit list. Validated against GGG's
# PoE2 catalog (items not in catalog are filtered out at load time).
SEED_UNIQUE_NAMES: list[str] = [
    "Headhunter",
    "Astramentis",
    "Ventor's Gamble",
    "Atziri's Splendour",
    "Choir of the Storm",
    "Bramblejack",
    "Kalandra's Touch",
    "Ingenuity",
    "Widowhail",
    "Temporalis",
    "Melting Maelstrom",
    "Beacon of Azis",
    "Original Sin",
    "Darkness Enthroned",
    "Rathpith Globe",
    "Kaom's Heart",
    "Olroth's Resolve",
    # Added in 0.5 "Return of the Ancients" — PoE1-imported chase tier.
    "Mageblood",
    "Voices",
    "Loreweave",
]


def fetch_all_uniques(force: bool = False) -> list[dict]:
    """Return [{name, type, group}] for every PoE2 unique in GGG's catalog."""
    with _lock:
        now = time.time()
        if (not force) and _cache["data"] and now - _cache["fetched_at"] < CACHE_TTL:
            return _cache["data"]
        with httpx.Client(
            timeout=20,
            headers={"User-Agent": "poe2-companion/0.1", "Accept": "application/json"},
        ) as c:
            r = c.get(ITEMS_URL)
            r.raise_for_status()
            payload = r.json()
        out: list[dict] = []
        for g in payload.get("result", []):
            for e in g.get("entries", []):
                if isinstance(e, dict) and e.get("name") and e.get("type"):
                    out.append({
                        "name": e["name"],
                        "type": e["type"],
                        "group": g.get("id", ""),
                    })
        out.sort(key=lambda u: u["name"].lower())
        _cache["data"] = out
        _cache["fetched_at"] = now
        return out


# ---------- Filter injection ----------

BEGIN_MARKER = "# BEGIN poe2-companion profit-highlights"
END_MARKER   = "# END poe2-companion profit-highlights"

# Colors — R G B A
COLOR_BUILD = (100, 200, 255, 255)   # cyan — your build's own uniques
COLOR_PROFIT = (255, 215, 0, 255)    # gold — items worth selling
COLOR_RARE = (230, 230, 80, 255)     # amber — rare bases from your build
PURPLE_BG = (40, 0, 40, 220)
DARK_BG = (20, 15, 0, 220)


def _escape(name: str) -> str:
    # PoE filter strings use double-quotes; escape inner quotes by doubling
    return '"' + name.replace('"', '""') + '"'


def _resolve_names_to_bases(names: list[str], by_name: dict[str, str]
                            ) -> tuple[list[str], list[str], list[str]]:
    """Return (matched_name_with_base, unique_bases_in_order, unmatched_names)."""
    bases_seen: list[str] = []
    matched: list[str] = []
    unmatched: list[str] = []
    for n in names:
        t = by_name.get(n)
        if not t:
            unmatched.append(n)
            continue
        matched.append(f"{n} ({t})")
        if t not in bases_seen:
            bases_seen.append(t)
    return matched, bases_seen, unmatched


def _color_str(rgba: tuple[int, int, int, int]) -> str:
    return " ".join(str(v) for v in rgba)


def _render_show_block(bases: list[str], text_color, border_color, bg_color,
                       sound_id: int, sound_vol: int, effect: str, icon_color: str,
                       chunk_size: int = 25, rarity: str = "Unique",
                       icon_shape: str = "Star", font_size: int = 45) -> list[str]:
    """Emit Show blocks for `bases`, chunked so lines don't blow up."""
    out: list[str] = []
    for i in range(0, len(bases), chunk_size):
        chunk = bases[i:i+chunk_size]
        out.append("Show")
        out.append(f"\tRarity {rarity}")
        out.append("\tBaseType == " + " ".join(_escape(b) for b in chunk))
        out.append(f"\tSetFontSize {font_size}")
        out.append(f"\tSetTextColor {_color_str(text_color)}")
        out.append(f"\tSetBorderColor {_color_str(border_color)}")
        out.append(f"\tSetBackgroundColor {_color_str(bg_color)}")
        out.append(f"\tPlayAlertSound {sound_id} {sound_vol}")
        out.append(f"\tPlayEffect {effect}")
        out.append(f"\tMinimapIcon 0 {icon_color} {icon_shape}")
        out.append("")
    return out


def build_profit_block(profit_names: list[str],
                       build_names: list[str] | None = None,
                       rare_items: list[dict] | None = None) -> str:
    """Generate up to THREE highlight sections inside one marker-wrapped block:

    1. BUILD UNIQUES (cyan) — uniques the active build is wearing.
    2. PROFIT UNIQUES (gold) — curated uniques worth selling.
    3. RARE BASES (amber) — rare bases from the build walkthrough.

    `rare_items` is a list of dicts: {slot, base, must_mods: list[str]}.
    Must-mods are emitted as COMMENTS only (PoE2 filter syntax can't gate on
    numeric mod values, so we match by BaseType + Rarity Rare).
    """
    catalog = fetch_all_uniques()
    by_name: dict[str, str] = {u["name"]: u["type"] for u in catalog}

    profit_clean = [n.strip() for n in (profit_names or []) if n and n.strip()]
    build_clean = [n.strip() for n in (build_names or []) if n and n.strip()]
    rare_list = list(rare_items or [])

    # If a unique appears on BOTH lists, the BUILD section already handles it.
    # Remove it from the profit bases so the profit section doesn't shadow.
    build_set = set(build_clean)
    profit_minus_build = [n for n in profit_clean if n not in build_set]

    b_matched, b_bases, b_unmatched = _resolve_names_to_bases(build_clean, by_name)
    p_matched, p_bases, p_unmatched = _resolve_names_to_bases(profit_minus_build, by_name)

    # Deduplicate rare bases (multiple slots may share a base, e.g. two ring slots)
    rare_bases_seen: list[str] = []
    rare_info: list[dict] = []
    for r in rare_list:
        base = (r.get("base") or "").strip()
        if not base or base in rare_bases_seen:
            continue
        rare_bases_seen.append(base)
        rare_info.append(r)

    if not b_bases and not p_bases and not rare_bases_seen:
        return f"{BEGIN_MARKER}\n# (no uniques or rare bases configured)\n{END_MARKER}\n"

    lines: list[str] = [
        BEGIN_MARKER,
        "# Two-section unique highlight block injected by poe2-companion.",
        "# Do not edit between markers — rebuilds strip and replace.",
        "#",
        "# -------- SECTION 1: BUILD UNIQUES (cyan) --------",
    ]
    if b_matched:
        lines.append("# Uniques currently equipped on your active build:")
        for n in b_matched:
            lines.append(f"#   - {n}")
    else:
        lines.append("# (no active build uniques)")
    if b_unmatched:
        lines.append(f"# Unmatched (not in catalog): {', '.join(b_unmatched)}")
    lines.append("")
    if b_bases:
        lines += _render_show_block(
            b_bases,
            text_color=COLOR_BUILD, border_color=COLOR_BUILD, bg_color=PURPLE_BG,
            sound_id=6, sound_vol=250, effect="Cyan", icon_color="Cyan",
        )

    lines += [
        "# -------- SECTION 2: PROFIT UNIQUES (gold) --------",
    ]
    if p_matched:
        lines.append("# Curated sell-worthy uniques:")
        for n in p_matched:
            lines.append(f"#   - {n}")
    else:
        lines.append("# (empty profit list)")
    if p_unmatched:
        lines.append(f"# Unmatched (not in catalog): {', '.join(p_unmatched)}")
    lines.append("")
    if p_bases:
        lines += _render_show_block(
            p_bases,
            text_color=COLOR_PROFIT, border_color=COLOR_PROFIT, bg_color=PURPLE_BG,
            sound_id=1, sound_vol=300, effect="Yellow", icon_color="Yellow",
        )

    # Section 3: rare bases
    if rare_bases_seen:
        lines += [
            "# -------- SECTION 3: RARE BASES FROM YOUR BUILD (amber) --------",
            "# (PoE2 filter can't gate on numeric mod values — bases are matched;",
            "# must-mods listed here as guidance for manual inspection.)",
        ]
        for r in rare_info:
            slot = r.get("slot", "?")
            base = r.get("base", "?")
            must = r.get("must_mods") or []
            must_str = (" — must: " + "; ".join(must)[:180]) if must else ""
            lines.append(f"#   - {slot}: {base}{must_str}")
        lines.append("")
        lines += _render_show_block(
            rare_bases_seen,
            text_color=COLOR_RARE, border_color=COLOR_RARE, bg_color=DARK_BG,
            sound_id=2, sound_vol=220, effect="White", icon_color="White",
            rarity="Rare", icon_shape="Diamond", font_size=40,
            chunk_size=20,
        )

    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def inject_into_filter(profit_names: list[str], source_path: Path,
                       dest_path: Path,
                       build_names: list[str] | None = None,
                       rare_items: list[dict] | None = None) -> dict[str, Any]:
    """Read source filter, strip any existing profit block, prepend the new one
    (with BUILD, PROFIT, and RARE sections), write to dest. Original untouched.
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Source filter not found: {source_path}")
    source_text = source_path.read_text(encoding="utf-8")
    stripped = _strip_existing_block(source_text)
    block = build_profit_block(profit_names, build_names=build_names,
                               rare_items=rare_items)
    combined = block + "\n" + stripped
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(combined, encoding="utf-8")
    return {
        "source": str(source_path),
        "dest": str(dest_path),
        "bytes_written": len(combined),
        "profit_count": len(profit_names or []),
        "build_count": len(build_names or []),
        "rare_count": len(rare_items or []),
        "block_lines": block.count("\n"),
    }


def _strip_existing_block(text: str) -> str:
    if BEGIN_MARKER not in text or END_MARKER not in text:
        return text
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER, start) + len(END_MARKER)
    # Also consume a trailing newline if present
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:start] + text[end:]
