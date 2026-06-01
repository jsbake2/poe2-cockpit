"""Rare-base chase discovery.

Scrapes maxroll.gg's PoE2 build guides for their structured gear payloads
(per-slot required/optional mod ids + base metadata paths + display names),
aggregates frequency across guides, and surfaces "chased" rare bases — slot
+ base + most-demanded mods — with optional trade2 price confirmation.

Why maxroll: poe.ninja's PoE2 builds API is closed (404 from anonymous GETs
as of 2026-06); their site is Astro-rendered with internal-only data fetches.
Maxroll guides embed the full gear-tier JSON inline in their HTML, so a
plain HTTP scrape is sufficient.

Coverage caveat: ~25% of guides delegate to a planner page (URL of the form
/poe2/planner/<id>) which lazy-loads its data over XHR. Those guides are
skipped and reported in the snapshot's `skipped_guides` list.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "chase_cache"
MOD_MAP_PATH = ROOT / "data" / "chase_mod_map.json"
BASES_CATALOG_PATH = ROOT / "data" / "poe2_bases_catalog.json"

MAXROLL_BASE = "https://maxroll.gg"
GUIDES_INDEX_URL = f"{MAXROLL_BASE}/poe2/build-guides"
GUIDE_URL_TEMPLATE = MAXROLL_BASE + "/poe2/build-guides/{slug}"

INDEX_TTL = 24 * 3600
GUIDE_TTL = 7 * 24 * 3600

UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) "
      "Gecko/20100101 Firefox/140.0")
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


# ---------- types ----------

@dataclass
class GuideRef:
    slug: str
    title: str
    url: str


@dataclass
class ParsedItem:
    base_metadata: str  # e.g. "Metadata/Items/Armours/BodyArmours/BodyDex9"
    display_base: str   # e.g. "Sacrificial Garb"
    rarity: str         # "rare" | "unique" | "magic" | "normal"
    item_level: int = 0


@dataclass
class GuideData:
    slug: str
    title: str
    url: str
    fetched_at: float
    # slot name -> {required: [mod_id], optional: [mod_id]}
    priority: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # slot name -> ParsedItem (the example item the guide actually equips)
    items_by_slot: dict[str, ParsedItem] = field(default_factory=dict)


@dataclass
class ChaseEntry:
    """One aggregated chase target — a slot + base combo seen across guides."""
    key: str                   # stable id: f"{slot}|{base_metadata}"
    slot: str
    base_metadata: str
    base: str                  # friendly display name (e.g. "Sacrificial Garb")
    count: int                 # how many guides recommend this (slot, base)
    share: float               # count / total_guides_with_data
    sources: list[str] = field(default_factory=list)  # guide slugs
    # mod_id -> weight (number of guides where this mod is in the priority for this slot)
    required_weights: dict[str, int] = field(default_factory=dict)
    optional_weights: dict[str, int] = field(default_factory=dict)
    # trade2 lookup
    trade_url: str = ""
    median_price: float | None = None   # in chaos / divine — keyed by trade response
    median_price_currency: str = ""
    floor_price: float | None = None
    listing_count: int = 0
    # mod ids whose symbolic→trade2 mapping is missing (UI hint)
    unmapped_mods: list[str] = field(default_factory=list)


@dataclass
class ChaseSnapshot:
    league: str
    generated_at: float
    entries: list[ChaseEntry] = field(default_factory=list)
    guides_scanned: int = 0
    guides_with_data: int = 0
    skipped_guides: list[dict] = field(default_factory=list)  # [{slug, reason}]
    # symbolic mod ids encountered across the snapshot with no trade2 mapping;
    # surfaced so the user knows what to add to chase_mod_map.json next.
    unmapped_mods: list[str] = field(default_factory=list)


# ---------- HTTP + cache ----------

def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _cache_read(name: str, ttl: float) -> Any | None:
    p = _cache_path(name)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl:
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _cache_write(name: str, data: Any) -> None:
    p = _cache_path(name)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
    tmp.replace(p)


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
    })


# ---------- guide index ----------

_GUIDE_HREF_RE = re.compile(
    r'href="(/poe2/build-guides/([a-z0-9-]+))"', re.I
)


def fetch_guide_index(force: bool = False, client: httpx.Client | None = None
                      ) -> list[GuideRef]:
    cached = (not force) and _cache_read("guides_index.json", INDEX_TTL)
    if cached:
        return [GuideRef(**r) for r in cached]
    owned = client is None
    client = client or _client()
    try:
        r = client.get(GUIDES_INDEX_URL)
        r.raise_for_status()
        html = r.text
    finally:
        if owned:
            client.close()
    refs: dict[str, GuideRef] = {}
    for m in _GUIDE_HREF_RE.finditer(html):
        path, slug = m.group(1), m.group(2)
        if slug in {"build-guides", "community-builds"}:
            continue
        # Title fallback: humanize the slug. The index doesn't reliably surface
        # a clean title near each href, and per-guide fetches already pull the
        # canonical <title>.
        title = slug.replace("-", " ").title()
        refs[slug] = GuideRef(slug=slug, title=title, url=MAXROLL_BASE + path)
    out = sorted(refs.values(), key=lambda r: r.slug)
    _cache_write("guides_index.json", [asdict(r) for r in out])
    return out


# ---------- guide HTML parsing ----------

_TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.I)


def _balanced_object(s: str, start_brace_idx: int) -> str | None:
    """Return the JSON object starting at `s[start_brace_idx]` (which must be
    a '{'), walking balanced braces while respecting string escaping.
    Returns the raw substring including both braces, or None on imbalance."""
    if start_brace_idx >= len(s) or s[start_brace_idx] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    i = start_brace_idx
    while i < len(s):
        c = s[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start_brace_idx:i + 1]
        i += 1
    return None


def _iter_anchored_objects(html: str, anchor: str):
    """Yield every balanced JSON object that immediately follows an occurrence
    of `anchor` (e.g. `"items":`). Skips unparseable ones."""
    i = 0
    while True:
        j = html.find(anchor, i)
        if j < 0:
            return
        brace = html.find("{", j)
        if brace < 0:
            return
        blob = _balanced_object(html, brace)
        if blob:
            try:
                yield json.loads(blob)
            except json.JSONDecodeError:
                pass
            i = brace + len(blob)
        else:
            i = brace + 1


def _looks_like_registry(obj: dict) -> bool:
    """True if this `items` blob is the global item registry (keys are numeric
    ids, values are dicts with `base` + `rarity`). False if it's a variant's
    slot->id map (values are integers like `416`)."""
    if not isinstance(obj, dict):
        return False
    sample = next(iter(obj.values()), None)
    return isinstance(sample, dict) and "base" in sample and "rarity" in sample


def _looks_like_slot_map(obj: dict) -> bool:
    """True if this `items` blob is a slot->itemId map (Weapon: 416, Helm: 417)."""
    if not isinstance(obj, dict) or not obj:
        return False
    # Keys should be slot names (strings starting uppercase), values numeric.
    sample_key = next(iter(obj.keys()), "")
    sample_val = next(iter(obj.values()), None)
    return (isinstance(sample_key, str) and sample_key[:1].isupper()
            and isinstance(sample_val, (int, str)))


def _looks_like_priority(obj: dict) -> bool:
    """True if values look like {required: [...], optional: [...]} dicts."""
    if not isinstance(obj, dict) or not obj:
        return False
    sample = next(iter(obj.values()), None)
    return (isinstance(sample, dict)
            and ("required" in sample or "optional" in sample))


def parse_guide_html(slug: str, url: str, html: str) -> GuideData | None:
    """Lift the guide's structured payload. Returns None if the guide doesn't
    embed gear data (e.g. delegates to /poe2/planner/<id>).

    Structure (current maxroll, ~2026 schema):
      data.profiles[].equipment.variants[] = {
          items: { <slot_name>: <registry_id_int>, ... },
          priority: { <slot_name>: { required: [{id}], optional: [{id}] }, ... },
      }
      data.items = { <registry_id_str>: { base, ilvl, rarity, name?, ... }, ... }

    We tolerate the embedded layout by scanning ALL `"items":` / `"priority":`
    JSON blobs, classifying each, then pairing slot-maps to priority blobs in
    order (they're emitted as siblings, one pair per variant)."""
    title_m = _TITLE_RE.search(html)
    title = (title_m.group(1).strip() if title_m else slug).split("|")[0].strip()
    data = GuideData(slug=slug, title=title, url=url, fetched_at=time.time())

    registry: dict[str, dict] = {}
    slot_maps: list[dict[str, int]] = []
    for obj in _iter_anchored_objects(html, '"items":'):
        if _looks_like_registry(obj):
            # Normalise keys to str for lookup parity.
            registry.update({str(k): v for k, v in obj.items()})
        elif _looks_like_slot_map(obj):
            slot_maps.append({k: int(v) if isinstance(v, str) and v.isdigit() else v
                              for k, v in obj.items()})

    priorities: list[dict] = []
    for obj in _iter_anchored_objects(html, '"priority":'):
        if _looks_like_priority(obj):
            priorities.append(obj)

    if not registry or (not slot_maps and not priorities):
        return None

    # Merge priorities — multiple variants in the same guide pile their slot
    # priorities together; if a slot appears twice, union the required lists.
    merged_priority: dict[str, dict[str, list[str]]] = {}
    for pri in priorities:
        for slot, d in pri.items():
            if not isinstance(d, dict):
                continue
            bucket = merged_priority.setdefault(slot, {"required": [], "optional": []})
            for arr in ("required", "optional"):
                for entry in (d.get(arr) or []):
                    if isinstance(entry, dict) and entry.get("id"):
                        if entry["id"] not in bucket[arr]:
                            bucket[arr].append(entry["id"])
    data.priority = merged_priority

    # Combine all slot-maps so we capture every variant's gear pick.
    for slot_map in slot_maps:
        for slot, item_id in slot_map.items():
            if slot in data.items_by_slot:
                continue
            entry = registry.get(str(item_id))
            if not isinstance(entry, dict):
                continue
            base_meta = entry.get("base", "")
            if not isinstance(base_meta, str) or not base_meta.startswith("Metadata/"):
                continue
            rarity = (entry.get("rarity") or "").lower()
            if rarity not in {"rare", "unique", "magic", "normal"}:
                continue
            # Catalog is authoritative — maxroll's `name` field often carries
            # the profile label ("Campaign", "Endgame", "New Item") rather
            # than the base type. Catalog hit always wins.
            catalog = _load_bases_catalog()
            display = catalog.get(base_meta) or entry.get("name") or _friendly_from_metadata(base_meta)
            # Reject obvious placeholder labels — they indicate an empty
            # / unfilled slot the guide author hasn't picked yet.
            if display in {"New Item", "Campaign", "Endgame", "Default",
                           "Profile", ""}:
                continue
            try:
                ilvl = int(entry.get("ilvl", 0))
            except (TypeError, ValueError):
                ilvl = 0
            data.items_by_slot[slot] = ParsedItem(
                base_metadata=base_meta,
                display_base=display,
                rarity=rarity,
                item_level=ilvl,
            )
    return data


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])(?<![A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)")
_bases_catalog: dict[str, str] = {}


def _load_bases_catalog() -> dict[str, str]:
    global _bases_catalog
    if _bases_catalog:
        return _bases_catalog
    if BASES_CATALOG_PATH.exists():
        try:
            with BASES_CATALOG_PATH.open("r", encoding="utf-8") as f:
                _bases_catalog = json.load(f)
        except Exception:
            log.exception("poe2_bases_catalog.json failed to load")
            _bases_catalog = {}
    return _bases_catalog


def _friendly_from_metadata(meta: str) -> str:
    """Map a Metadata path to the in-game display name via the poe2db-derived
    catalog. Falls back to a humanised tail if the catalog has no entry."""
    catalog = _load_bases_catalog()
    hit = catalog.get(meta)
    if hit:
        return hit
    tail = meta.rstrip("/").rsplit("/", 1)[-1]
    # Strip PoE2 dev-codename prefix "Four" (visible in many metadata names).
    if tail.startswith("Four") and len(tail) > 4 and tail[4].isupper():
        tail = tail[4:]
    return _CAMEL_RE.sub(" ", tail).strip()


# ---------- guide fetch (with cache) ----------

def fetch_guide(slug: str, *, force: bool = False,
                client: httpx.Client | None = None) -> GuideData | None:
    cache_name = f"guide_{slug}.json"
    cached = (not force) and _cache_read(cache_name, GUIDE_TTL)
    if cached:
        # Re-hydrate cached dict into GuideData (ParsedItem in items_by_slot)
        gd = GuideData(
            slug=cached.get("slug", slug),
            title=cached.get("title", slug),
            url=cached.get("url", GUIDE_URL_TEMPLATE.format(slug=slug)),
            fetched_at=cached.get("fetched_at", 0.0),
            priority=cached.get("priority") or {},
        )
        for s, it in (cached.get("items_by_slot") or {}).items():
            gd.items_by_slot[s] = ParsedItem(**it)
        return gd

    owned = client is None
    client = client or _client()
    try:
        url = GUIDE_URL_TEMPLATE.format(slug=slug)
        r = client.get(url)
        if r.status_code != 200:
            log.warning("guide fetch failed: slug=%s status=%s", slug, r.status_code)
            return None
        gd = parse_guide_html(slug, url, r.text)
        if gd is None:
            return None
        _cache_write(cache_name, asdict(gd))
        return gd
    finally:
        if owned:
            client.close()


# ---------- aggregation ----------

def _entry_key(slot: str, base_metadata: str) -> str:
    return f"{slot}|{base_metadata}"


def aggregate(guides: list[GuideData]) -> list[ChaseEntry]:
    """Roll guides up into (slot, base) frequency rows + mod weights."""
    by_key: dict[str, ChaseEntry] = {}
    guides_with_data = sum(1 for g in guides if g.items_by_slot)
    if guides_with_data == 0:
        return []

    for g in guides:
        for slot, item in g.items_by_slot.items():
            if item.rarity != "rare":
                continue
            key = _entry_key(slot, item.base_metadata)
            e = by_key.get(key)
            if e is None:
                e = ChaseEntry(
                    key=key, slot=slot,
                    base_metadata=item.base_metadata,
                    base=item.display_base,
                    count=0, share=0.0,
                )
                by_key[key] = e
            e.count += 1
            if g.slug not in e.sources:
                e.sources.append(g.slug)
            # Accumulate the priority for THIS slot in THIS guide. Most
            # guides give one priority per slot regardless of which rare base
            # they slot in — that's intentional: the build wants those mods
            # on whatever base sits in that slot.
            pri = g.priority.get(slot) or {}
            for mid in pri.get("required", []):
                e.required_weights[mid] = e.required_weights.get(mid, 0) + 1
            for mid in pri.get("optional", []):
                e.optional_weights[mid] = e.optional_weights.get(mid, 0) + 1

    out = list(by_key.values())
    for e in out:
        e.share = round(e.count / guides_with_data, 3)
    out.sort(key=lambda r: (-r.count, -sum(r.required_weights.values()), r.slot, r.base))
    return out


# ---------- mod-id mapping (symbolic → trade2) ----------

_mod_map_cache: dict[str, dict] = {}


def load_mod_map() -> dict[str, dict]:
    """Symbolic mod id -> {trade2_id, text}. Curated.

    Maxroll uses GGG's internal stat names (e.g. "base_maximum_life"). Trade2
    uses hashed ids (e.g. "explicit.stat_3299347043"). The catalog at
    /api/trade2/data/stats has no symbolic key, so we ship a hand-curated map.
    Missing entries are reported back to the UI via ChaseEntry.unmapped_mods."""
    global _mod_map_cache
    if _mod_map_cache:
        return _mod_map_cache
    if MOD_MAP_PATH.exists():
        try:
            with MOD_MAP_PATH.open("r", encoding="utf-8") as f:
                _mod_map_cache = json.load(f)
                return _mod_map_cache
        except Exception:
            log.exception("chase_mod_map.json failed to load")
    _mod_map_cache = {}
    return _mod_map_cache


# ---------- trade URL + optional price fetch ----------

TRADE_SEARCH_URL_TEMPLATE = (
    "https://www.pathofexile.com/trade2/search/poe2/{league}"
)
TRADE_API_SEARCH = "https://www.pathofexile.com/api/trade2/search/poe2/{league}"
TRADE_API_FETCH = "https://www.pathofexile.com/api/trade2/fetch/{ids}"


def build_trade_query(entry: ChaseEntry, top_n_required: int = 3) -> dict:
    """Build the JSON body for a trade2 search. Filters: rarity rare + the
    entry's base type + the top-N required mods (mapped to trade2 ids)."""
    mod_map = load_mod_map()
    stat_filters: list[dict] = []
    unmapped: list[str] = []
    top_required = sorted(entry.required_weights.items(),
                          key=lambda x: -x[1])[:top_n_required]
    for symbolic, _weight in top_required:
        mapping = mod_map.get(symbolic)
        if not mapping or not mapping.get("trade2_id"):
            unmapped.append(symbolic)
            continue
        stat_filters.append({
            "id": mapping["trade2_id"],
            "value": {},  # no min/max — just presence
            "disabled": False,
        })
    entry.unmapped_mods = unmapped
    return {
        "query": {
            "status": {"option": "any"},
            "type": entry.base,
            "filters": {
                "type_filters": {
                    "filters": {
                        "rarity": {"option": "rare"},
                    },
                },
            },
            "stats": [{"type": "and", "filters": stat_filters}] if stat_filters else [],
        },
        "sort": {"price": "asc"},
    }


def build_trade_url(entry: ChaseEntry, league: str) -> str:
    """Build a browser-shareable trade2 URL. Same query shape as POST, encoded
    into the path. Always works — no auth required to construct."""
    q = build_trade_query(entry)
    return (TRADE_SEARCH_URL_TEMPLATE.format(league=quote(league, safe="")) +
            "?q=" + quote(json.dumps(q["query"], separators=(",", ":"))))


class TradeAuth:
    """Encapsulates the auth needed for server-side trade2 POSTs.

    POE_SESSID env var (the user's pathofexile.com POESESSID cookie) is the
    minimum. cf-clearance / cf_bm cookies may also be required depending on
    Cloudflare state; we accept them via POE_CF_CLEARANCE and POE_CF_BM."""

    def __init__(self) -> None:
        self.poesessid = os.environ.get("POE_SESSID", "").strip()
        self.cf_clearance = os.environ.get("POE_CF_CLEARANCE", "").strip()
        self.cf_bm = os.environ.get("POE_CF_BM", "").strip()

    @property
    def ready(self) -> bool:
        return bool(self.poesessid)

    def cookies(self) -> dict[str, str]:
        c: dict[str, str] = {}
        if self.poesessid:
            c["POESESSID"] = self.poesessid
        if self.cf_clearance:
            c["cf_clearance"] = self.cf_clearance
        if self.cf_bm:
            c["__cf_bm"] = self.cf_bm
        return c


def _polite_sleep(state: dict, budget_s: float = 2.0) -> None:
    """Sleep enough to hold ~5 requests / 10 seconds (GGG's documented limit)."""
    now = time.time()
    last = state.get("last", 0.0)
    delta = now - last
    if delta < budget_s:
        time.sleep(budget_s - delta)
    state["last"] = time.time()


def confirm_prices(entries: list[ChaseEntry], league: str,
                   max_entries: int = 30,
                   auth: TradeAuth | None = None) -> None:
    """Mutate entries in-place with floor_price / median_price / listing_count.

    Requires POE_SESSID. Without it, only the trade_url is set (browser uses
    its own session). Skips silently on per-entry failure — chase data still
    ships, just without server-side prices."""
    auth = auth or TradeAuth()
    rate_state: dict = {}
    for entry in entries[:max_entries]:
        entry.trade_url = build_trade_url(entry, league)
        if not auth.ready:
            continue
        try:
            _polite_sleep(rate_state)
            query = build_trade_query(entry)
            with httpx.Client(timeout=20.0, cookies=auth.cookies(),
                              headers={
                                  "User-Agent": UA,
                                  "Accept": "application/json",
                                  "Content-Type": "application/json",
                                  "Referer": "https://www.pathofexile.com/trade2/",
                              }) as c:
                r = c.post(TRADE_API_SEARCH.format(league=quote(league, safe="")),
                           json=query)
                if r.status_code != 200:
                    log.warning("trade search %s -> %s", entry.key, r.status_code)
                    continue
                d = r.json()
                ids = (d.get("result") or [])[:20]
                entry.listing_count = int(d.get("total") or len(ids))
                if not ids:
                    continue
                _polite_sleep(rate_state)
                fetch_url = TRADE_API_FETCH.format(ids=",".join(ids[:10]))
                rf = c.get(fetch_url, params={"query": d.get("id", "")})
                if rf.status_code != 200:
                    log.warning("trade fetch %s -> %s", entry.key, rf.status_code)
                    continue
                listings = rf.json().get("result") or []
                prices: list[float] = []
                currency = ""
                for lst in listings:
                    p = (lst.get("listing") or {}).get("price") or {}
                    amount = p.get("amount")
                    cur = p.get("currency")
                    if isinstance(amount, (int, float)) and cur:
                        prices.append(float(amount))
                        currency = currency or str(cur)
                if prices:
                    prices.sort()
                    # floor = lowest, median = 50th percentile (skip top to dodge
                    # price-fixers); fall back to plain median if list is small.
                    entry.floor_price = prices[0]
                    body = prices[1:] if len(prices) > 4 else prices
                    entry.median_price = body[len(body) // 2]
                    entry.median_price_currency = currency
        except Exception as exc:
            log.warning("trade confirm failed for %s: %s", entry.key, exc)


# ---------- orchestration ----------

_refresh_lock = threading.Lock()


def refresh_snapshot(league: str, *, max_guides: int = 60,
                     do_prices: bool = True,
                     force_refresh: bool = False) -> ChaseSnapshot:
    """Run the whole pipeline: scrape -> aggregate -> price-confirm -> persist."""
    snapshot = ChaseSnapshot(league=league, generated_at=time.time())
    with _refresh_lock:
        index = fetch_guide_index(force=force_refresh)
        snapshot.guides_scanned = len(index)
        guides: list[GuideData] = []
        with _client() as client:
            for ref in index[:max_guides]:
                try:
                    gd = fetch_guide(ref.slug, client=client, force=force_refresh)
                except Exception as exc:
                    log.warning("guide %s raised %s", ref.slug, exc)
                    snapshot.skipped_guides.append({
                        "slug": ref.slug, "reason": f"exception: {exc}",
                    })
                    continue
                if gd is None or not gd.items_by_slot:
                    snapshot.skipped_guides.append({
                        "slug": ref.slug,
                        "reason": "no gear data (planner-only or unknown shape)",
                    })
                    continue
                guides.append(gd)
        snapshot.guides_with_data = len(guides)
        snapshot.entries = aggregate(guides)
        if do_prices:
            confirm_prices(snapshot.entries, league)
        # Collect unique unmapped mods across all entries for UI surfacing.
        seen: set[str] = set()
        for e in snapshot.entries:
            for m in e.unmapped_mods:
                if m not in seen:
                    snapshot.unmapped_mods.append(m)
                    seen.add(m)
        # Always (re)build trade URLs even if no auth (browser-side use).
        for e in snapshot.entries:
            if not e.trade_url:
                e.trade_url = build_trade_url(e, league)
        save_snapshot(snapshot)
    return snapshot


# ---------- snapshot persistence ----------

def _snapshot_path(league: str) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", league).strip("_") or "unknown"
    return _cache_path(f"snapshot_{slug}.json")


def save_snapshot(snapshot: ChaseSnapshot) -> None:
    path = _snapshot_path(snapshot.league)
    payload = {
        "league": snapshot.league,
        "generated_at": snapshot.generated_at,
        "guides_scanned": snapshot.guides_scanned,
        "guides_with_data": snapshot.guides_with_data,
        "skipped_guides": list(snapshot.skipped_guides),
        "unmapped_mods": list(snapshot.unmapped_mods),
        "entries": [asdict(e) for e in snapshot.entries],
    }
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
    tmp.replace(path)


def load_snapshot(league: str) -> ChaseSnapshot | None:
    path = _snapshot_path(league)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    snap = ChaseSnapshot(
        league=d.get("league", league),
        generated_at=float(d.get("generated_at", 0.0)),
        guides_scanned=int(d.get("guides_scanned", 0)),
        guides_with_data=int(d.get("guides_with_data", 0)),
        skipped_guides=list(d.get("skipped_guides") or []),
        unmapped_mods=list(d.get("unmapped_mods") or []),
    )
    for e in d.get("entries") or []:
        snap.entries.append(ChaseEntry(
            key=e.get("key", ""), slot=e.get("slot", ""),
            base_metadata=e.get("base_metadata", ""),
            base=e.get("base", ""),
            count=int(e.get("count", 0)), share=float(e.get("share", 0.0)),
            sources=list(e.get("sources") or []),
            required_weights=dict(e.get("required_weights") or {}),
            optional_weights=dict(e.get("optional_weights") or {}),
            trade_url=e.get("trade_url", ""),
            median_price=e.get("median_price"),
            median_price_currency=e.get("median_price_currency", ""),
            floor_price=e.get("floor_price"),
            listing_count=int(e.get("listing_count", 0)),
            unmapped_mods=list(e.get("unmapped_mods") or []),
        ))
    return snap
