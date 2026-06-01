"""Path of Building (PoE2) import parser.

Accepts either a raw PoB export code (URL-safe base64 of zlib-compressed XML)
or a pobb.in / pob.cool / poe2db.tw share URL. Returns a structured dict
describing the build: items per slot, passive tree allocation, jewels, flasks,
skills.

Not a full PoB interpreter — we extract only the fields the companion tool needs.
"""

from __future__ import annotations

import base64
import hashlib
import re
import zlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx


POBBIN_HOST_RE = re.compile(r"^(?:www\.)?(pobb\.in|pob\.cool|poe2db\.tw|poe\.ninja)$", re.I)


class PoBImportError(ValueError):
    pass


# ---------- input normalization ----------

_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean_url(text: str) -> str:
    """Strip whitespace and control characters that sometimes ride along
    with pasted URLs (null bytes, newlines, etc. break httpx validation)."""
    return _CTRL_CHARS.sub("", text).strip()


def looks_like_url(text: str) -> bool:
    t = _clean_url(text)
    return t.startswith("http://") or t.startswith("https://")


def fetch_share_url(url: str, client: httpx.Client | None = None) -> str:
    """Resolve a pobb.in / pob.cool / poe2db.tw share URL to the raw PoB export code."""
    url = _clean_url(url)
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise PoBImportError(f"Malformed URL: {e}") from e

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not POBBIN_HOST_RE.match(host):
        raise PoBImportError(
            f"Unsupported share host: {host!r}. "
            "Paste a PoB export code, or a pobb.in / pob.cool / poe2db.tw URL."
        )

    owned_client = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=10.0)
    try:
        if "pobb.in" in host:
            raw_url = f"https://pobb.in{parsed.path.rstrip('/')}/raw"
        elif "pob.cool" in host:
            raw_url = f"https://pob.cool{parsed.path.rstrip('/')}/raw"
        elif "poe2db.tw" in host:
            # poe2db.tw hosts PoB pastes at /pob/<id> with a /raw sibling.
            path = parsed.path.rstrip("/")
            if not path.startswith("/pob/"):
                raise PoBImportError(
                    f"Not a poe2db.tw PoB share URL: {url}. "
                    "Expected a https://poe2db.tw/pob/<id> link."
                )
            raw_url = f"https://poe2db.tw{path}/raw"
        else:
            raise PoBImportError(
                f"Cannot auto-fetch from {host} — paste the raw PoB code instead."
            )
        try:
            r = client.get(raw_url)
        except httpx.InvalidURL as e:
            raise PoBImportError(f"Invalid URL after cleanup: {raw_url!r} ({e})") from e
        except httpx.HTTPError as e:
            raise PoBImportError(f"HTTP error fetching {raw_url}: {e}") from e
        if r.status_code != 200:
            raise PoBImportError(f"{raw_url} returned HTTP {r.status_code}")
        return r.text.strip()
    finally:
        if owned_client:
            client.close()


# ---------- decode ----------

_B64_CHAR = re.compile(r"[A-Za-z0-9+/=\-_]")


def decode_pob_code(code: str) -> ET.Element:
    """Decode a PoB export string into an XML root element."""
    # Strip any whitespace / non-base64 chars the user's copy-paste pipeline
    # may have introduced (newlines from chat line-wrapping, spaces, etc.).
    s = "".join(_B64_CHAR.findall(code))
    # URL-safe base64 per PoB convention
    s = s.replace("-", "+").replace("_", "/")
    # Pad to multiple of 4
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s)
    except Exception as e:
        raise PoBImportError(f"Not valid base64: {e}") from e
    try:
        xml_bytes = zlib.decompress(raw)
    except zlib.error as e:
        raise PoBImportError(f"Not valid zlib payload: {e}") from e
    try:
        return ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise PoBImportError(f"Not valid XML: {e}") from e


def resolve_pob_code(text: str, *, client: httpx.Client | None = None) -> str:
    """Return the raw PoB export code. If `text` is a share URL (pobb.in /
    pob.cool), fetch and return the server's raw code. Otherwise assume the
    input IS already the raw code and return it."""
    if looks_like_url(text):
        return fetch_share_url(text, client=client)
    return text.strip()


def load(text: str, *, client: httpx.Client | None = None) -> ET.Element:
    """Accept either a PoB code or a share URL; return XML root."""
    code = resolve_pob_code(text, client=client)
    return decode_pob_code(code)


# ---------- parse ----------

_TAG_PREFIX_RE = re.compile(r"^(?:\{[^}]+\})+")


@dataclass
class Mod:
    text: str
    kind: str = "explicit"  # implicit, enchant, crafted, explicit, rune, fractured
    tags: list[str] = field(default_factory=list)  # PoE2 {tag} markers, e.g. ["enchant","rune"]

    def hash(self) -> str:
        return hashlib.sha1(f"{self.kind}|{self.text}".encode()).hexdigest()[:12]

    @classmethod
    def parse_line(cls, ln: str, default_kind: str = "explicit") -> "Mod":
        """Strip leading {tag}{tag}... markers and infer kind from them."""
        tags: list[str] = []
        m = _TAG_PREFIX_RE.match(ln)
        if m:
            tags = [t[1:-1] for t in re.findall(r"\{[^}]+\}", m.group(0))]
            ln = ln[m.end():]
        kind = default_kind
        # Rune: prefix is how PoB lists socketed soul cores / runes in PoE2.
        if ln.startswith("Rune: "):
            kind = "rune"
            ln = ln[len("Rune: "):]
        elif "rune" in tags:
            kind = "rune"
        elif "crafted" in tags:
            kind = "crafted"
        elif "enchant" in tags:
            kind = "enchant"
        return cls(text=ln, kind=kind, tags=tags)


@dataclass
class Item:
    slot: str
    name: str = ""
    base: str = ""
    rarity: str = ""
    item_level: int | None = None
    mods: list[Mod] = field(default_factory=list)
    raw: str = ""

    def content_hash(self) -> str:
        """Stable hash of the item's identity + mod list, for diff-aware re-import."""
        payload = f"{self.slot}|{self.name}|{self.base}|{self.rarity}|"
        payload += "||".join(m.hash() for m in self.mods)
        return hashlib.sha1(payload.encode()).hexdigest()[:16]


@dataclass
class Skill:
    label: str
    gems: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class TreeSpec:
    class_name: str = ""
    ascendancy: str = ""
    nodes: list[int] = field(default_factory=list)
    mastery_effects: list[int] = field(default_factory=list)
    jewel_sockets: dict[int, str] = field(default_factory=dict)
    url: str = ""


@dataclass
class Build:
    level: int = 1
    class_name: str = ""
    ascendancy: str = ""
    main_skill: str = ""
    items: list[Item] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    tree: TreeSpec = field(default_factory=TreeSpec)
    stats: dict[str, float] = field(default_factory=dict)
    notes: str = ""

    def items_by_slot(self) -> dict[str, Item]:
        return {it.slot: it for it in self.items}


ITEM_RARITIES = {"Normal", "Magic", "Rare", "Unique"}


_META_PREFIXES = (
    "Item Level:", "Quality:", "Sockets:", "Level:", "LevelReq:",
    "Requires ", "Requirements:", "Note:", "Crafted:",
    "Corrupted", "Shaper Item", "Elder Item", "Influence:",
    "Warband:", "Selected Variant:", "Has Variants:",
    "Unique ID:", "Price:", "Radius:", "Evasion:", "Armour:",
    "Energy Shield:", "Block:", "League:", "Talisman Tier:",
)
_MOD_TAG_SUFFIXES = {
    " (implicit)": "implicit",
    " (enchant)": "enchant",
    " (crafted)": "crafted",
    " (rune)": "rune",
    " (fractured)": "fractured",
}


def _parse_item_text(slot: str, raw: str) -> Item:
    """Parse the free-text body of a <Item> element.

    PoB stores items in the game's 'Copy Item' format. Two variants seen:

    1. Classic PoE1 format with '--------' dividers between blocks.
    2. PoE2 / pobb.in format: no dividers, flat key:value stream with
       `Implicits: N` indicating that the next N mod lines are implicits.
    """
    item = Item(slot=slot, raw=raw)
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return item

    i = 0
    if lines[i].startswith("Rarity:"):
        item.rarity = lines[i].split(":", 1)[1].strip().title()
        i += 1

    # Header (name/base) ends at the first non-header line. Header lines are
    # lines with no ':' that aren't dividers. For Rare/Unique we expect up to
    # 2 header lines (name, base). For Magic/Normal, 1 (base only).
    header: list[str] = []
    max_header = 2 if item.rarity in {"Rare", "Unique"} else 1
    while i < len(lines) and len(header) < max_header:
        ln = lines[i]
        if ln.startswith("--------") or ":" in ln or ln.endswith(")"):
            break
        header.append(ln)
        i += 1
    if header:
        if item.rarity in {"Rare", "Unique"} and len(header) >= 2:
            item.name, item.base = header[0], header[1]
        elif len(header) == 1:
            if item.rarity in {"Rare", "Unique"}:
                item.name = header[0]
            else:
                item.base = header[0]

    # Walk remaining lines. Any line can be metadata, divider, or mod.
    implicits_remaining = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if ln.startswith("--------"):
            continue
        if ln.startswith("Implicits:"):
            try:
                implicits_remaining = int(ln.split(":", 1)[1].strip())
            except ValueError:
                implicits_remaining = 0
            continue
        if any(ln.startswith(p) for p in _META_PREFIXES):
            if ln.startswith("Item Level:"):
                try:
                    item.item_level = int(ln.split(":", 1)[1].strip())
                except ValueError:
                    pass
            continue
        # Explicit (suffix) mod-kind tag wins over implicits-counter
        matched_tag = False
        for suffix, kind in _MOD_TAG_SUFFIXES.items():
            if ln.endswith(suffix):
                item.mods.append(Mod.parse_line(ln[: -len(suffix)], default_kind=kind))
                matched_tag = True
                break
        if matched_tag:
            continue
        # PoE2/pobb.in: "Implicits: N" means next N non-meta lines are implicits
        if implicits_remaining > 0:
            item.mods.append(Mod.parse_line(ln, default_kind="implicit"))
            implicits_remaining -= 1
            continue
        item.mods.append(Mod.parse_line(ln, default_kind="explicit"))
    return item


def _active_child(parent: ET.Element, child_tag: str, id_attr: str,
                  override_id: str | None = None) -> ET.Element | None:
    """Return the child whose `id` matches parent's activeX attribute, or
    `override_id` when provided (used to force a specific variant).

    PoB sometimes omits `id` attributes on `<Spec>` (passive tree) elements
    and uses 1-based positional indexing via `activeSpec`. When none of the
    candidates have an `id`, we interpret target_id as that 1-based index."""
    candidates = parent.findall(child_tag)
    if not candidates:
        return None
    target_id = override_id or parent.get(f"active{child_tag}") or parent.get(f"active{child_tag}Id")
    if target_id is not None:
        for c in candidates:
            if c.get("id") == target_id:
                return c
        # Fallback: positional. activeSpec="5" -> candidates[4].
        if all(c.get("id") is None for c in candidates):
            try:
                idx = int(target_id) - 1
                if 0 <= idx < len(candidates):
                    return candidates[idx]
            except (TypeError, ValueError):
                pass
    return candidates[0]


@dataclass
class VariantSpec:
    """One selectable build variant — a paired (ItemSet, SkillSet, TreeSpec)
    triple from a PoB export. Most multi-variant PoBs author these in lockstep
    (e.g. ItemSet id=2 + SkillSet id=2 + Spec id=2 = "MoM variant"); we pair
    by id first, then by index."""
    id: str  # synthetic — equals the ItemSet id, since that's the anchor PoB users see
    label: str
    item_set_id: str
    skill_set_id: str
    tree_spec_id: str


def _title_for(el: ET.Element, fallback_prefix: str, idx: int) -> str:
    for attr in ("title", "name", "label"):
        v = (el.get(attr) or "").strip()
        if v:
            return v
    return f"{fallback_prefix} {idx + 1}"


def _normalize_title(s: str | None) -> str:
    return (s or "").strip().lower()


def list_variants(root: ET.Element) -> list[VariantSpec]:
    """Enumerate variant triples (ItemSet × SkillSet × TreeSpec) present in
    the PoB XML. Pairs siblings by **title** first (PoB users typically give
    matching variants the same title across sets), falling back to positional
    index for unnamed entries. Always returns at least one variant.

    Tree `<Spec>` elements may have no `id` attribute (PoB-PoE2 indexes them
    positionally via `activeSpec="N"`); we synthesise a 1-based string id in
    that case so the downstream encoder can rewrite the active attr."""
    items_el = root.find("Items")
    skills_el = root.find("Skills")
    tree_el = root.find("Tree")

    item_sets = items_el.findall("ItemSet") if items_el is not None else []
    skill_sets = skills_el.findall("SkillSet") if skills_el is not None else []
    tree_specs = tree_el.findall("Spec") if tree_el is not None else []

    if not item_sets and not skill_sets and not tree_specs:
        return [VariantSpec(id="0", label="Default", item_set_id="",
                            skill_set_id="", tree_spec_id="")]

    # Title indexes for cross-set matching. PoB conventions: when users author
    # multiple variants they apply the same title to ItemSet/SkillSet/Spec
    # ("End Game (Cheap)" appears in all three blocks). Pairing by id is
    # unreliable because PoB allocates ids in author-creation order, which
    # diverges across blocks.
    sk_by_title: dict[str, ET.Element] = {}
    sk_used: set[int] = set()
    for s in skill_sets:
        t = _normalize_title(s.get("title"))
        if t and t not in sk_by_title:
            sk_by_title[t] = s
    tr_by_title: dict[str, ET.Element] = {}
    tr_used: set[int] = set()
    for s in tree_specs:
        t = _normalize_title(s.get("title"))
        if t and t not in tr_by_title:
            tr_by_title[t] = s

    def _spec_id(el: ET.Element | None, lst: list[ET.Element]) -> str:
        """Return the element's id, or its 1-based positional index for
        id-less Specs (which PoB references via activeSpec="N")."""
        if el is None:
            return ""
        eid = el.get("id")
        if eid:
            return eid
        try:
            return str(lst.index(el) + 1)
        except ValueError:
            return ""

    # Anchor on whichever list is longest; usually ItemSets.
    anchor = item_sets if len(item_sets) >= max(len(skill_sets), len(tree_specs)) \
             else (skill_sets if len(skill_sets) >= len(tree_specs) else tree_specs)
    anchor_kind = ("Items" if anchor is item_sets else
                   "Skills" if anchor is skill_sets else "Tree")

    variants: list[VariantSpec] = []
    for i, a in enumerate(anchor):
        title = _normalize_title(a.get("title"))
        # ItemSet anchor: match SkillSet/Spec by title; fall back to position.
        if anchor is item_sets:
            it = a
            sk = sk_by_title.get(title)
            tr = tr_by_title.get(title)
        elif anchor is skill_sets:
            sk = a
            it = next((x for x in item_sets if _normalize_title(x.get("title")) == title), None)
            tr = tr_by_title.get(title)
        else:
            tr = a
            it = next((x for x in item_sets if _normalize_title(x.get("title")) == title), None)
            sk = sk_by_title.get(title)

        # Positional fallback for any unmatched slot — but only with an
        # un-claimed sibling, so two anchor rows can't both grab the same
        # other-block entry.
        if sk is None and i < len(skill_sets) and i not in sk_used:
            sk = skill_sets[i]; sk_used.add(i)
        elif sk is not None:
            try: sk_used.add(skill_sets.index(sk))
            except ValueError: pass
        if tr is None and i < len(tree_specs) and i not in tr_used:
            tr = tree_specs[i]; tr_used.add(i)
        elif tr is not None:
            try: tr_used.add(tree_specs.index(tr))
            except ValueError: pass

        display = title or _normalize_title(
            (sk.get("title") if sk is not None else "") or
            (tr.get("title") if tr is not None else "")
        )
        label = a.get("title") or (sk.get("title") if sk is not None else "") \
                or (tr.get("title") if tr is not None else "") \
                or f"Variant {i + 1}"

        variants.append(VariantSpec(
            id=(it.get("id") if it is not None else str(i + 1)) or str(i + 1),
            label=label,
            item_set_id=(it.get("id") if it is not None else ""),
            skill_set_id=(sk.get("id") if sk is not None else ""),
            tree_spec_id=_spec_id(tr, tree_specs),
        ))
    return variants


def parse(root: ET.Element, *, item_set_id: str | None = None,
          skill_set_id: str | None = None,
          tree_spec_id: str | None = None) -> Build:
    """Parse PoB XML into a Build. When variant IDs are supplied they override
    the document's `active*` attrs — used for multi-variant imports."""
    b = Build()

    build_el = root.find("Build")
    if build_el is not None:
        try:
            b.level = int(build_el.get("level", "1"))
        except ValueError:
            pass
        b.class_name = build_el.get("className", "") or build_el.get("class", "")
        b.ascendancy = build_el.get("ascendClassName", "") or build_el.get("ascendancy", "")
        b.main_skill = build_el.get("mainSocketGroup", "") or ""
        for stat_el in build_el.findall("PlayerStat"):
            name = stat_el.get("stat", "")
            try:
                b.stats[name] = float(stat_el.get("value", "0"))
            except ValueError:
                pass

    # Items
    items_el = root.find("Items")
    if items_el is not None:
        item_set = _active_child(items_el, "ItemSet", "id",
                                 override_id=item_set_id) or items_el
        # Index raw items by id from the top-level <Item> elements
        by_id: dict[str, str] = {}
        for it in items_el.findall("Item"):
            iid = it.get("id", "")
            if iid:
                by_id[iid] = (it.text or "")
        # Slots live in the active item set (fallback: items_el itself)
        slots_container = item_set if item_set.find("Slot") is not None else items_el
        for slot_el in slots_container.findall("Slot"):
            slot_name = slot_el.get("name", "").strip()
            item_id = slot_el.get("itemId", "")
            raw = by_id.get(item_id, "")
            if not slot_name or not raw.strip():
                continue
            b.items.append(_parse_item_text(slot_name, raw))

    # Skills
    skills_el = root.find("Skills")
    if skills_el is not None:
        skillset = _active_child(skills_el, "SkillSet", "id",
                                 override_id=skill_set_id) or skills_el
        for sg in skillset.findall("Skill"):
            label = sg.get("label", "") or sg.get("mainActiveSkill", "") or ""
            enabled = sg.get("enabled", "true").lower() != "false"
            gems = [
                g.get("nameSpec", "") or g.get("gemId", "")
                for g in sg.findall("Gem")
                if (g.get("nameSpec") or g.get("gemId"))
            ]
            b.skills.append(Skill(label=label, gems=gems, enabled=enabled))

    # Tree
    tree_el = root.find("Tree")
    if tree_el is not None:
        spec = _active_child(tree_el, "Spec", "id", override_id=tree_spec_id)
        if spec is not None:
            b.tree.class_name = spec.get("className", "") or b.class_name
            b.tree.ascendancy = spec.get("ascendClassName", "") or b.ascendancy
            nodes_str = spec.get("nodes", "")
            if nodes_str:
                b.tree.nodes = [int(x) for x in nodes_str.split(",") if x.strip().isdigit()]
            url_el = spec.find("URL")
            if url_el is not None and url_el.text:
                b.tree.url = url_el.text.strip()
            sockets_el = spec.find("Sockets")
            if sockets_el is not None:
                for s in sockets_el.findall("Socket"):
                    try:
                        nid = int(s.get("nodeId", "0"))
                    except ValueError:
                        continue
                    b.tree.jewel_sockets[nid] = s.get("itemId", "")

    notes_el = root.find("Notes")
    if notes_el is not None and notes_el.text:
        b.notes = notes_el.text.strip()

    return b


def import_build(text: str, *, client: httpx.Client | None = None,
                 variant: VariantSpec | None = None) -> Build:
    """Top-level entry: paste code or share URL -> Build. If `variant` is
    given, parse uses that variant's ItemSet/SkillSet/Spec ids."""
    root = load(text, client=client)
    if variant is None:
        return parse(root)
    return parse(
        root,
        item_set_id=variant.item_set_id or None,
        skill_set_id=variant.skill_set_id or None,
        tree_spec_id=variant.tree_spec_id or None,
    )


def import_build_with_code(text: str, *, client: httpx.Client | None = None,
                           variant: VariantSpec | None = None
                           ) -> tuple[Build, str]:
    """Like `import_build`, but also returns the raw export code (useful for
    passing to downstream tools that expect the original PoB string)."""
    code = resolve_pob_code(text, client=client)
    root = decode_pob_code(code)
    if variant is None:
        return parse(root), code
    return parse(
        root,
        item_set_id=variant.item_set_id or None,
        skill_set_id=variant.skill_set_id or None,
        tree_spec_id=variant.tree_spec_id or None,
    ), code


def list_variants_from_code(text: str, *, client: httpx.Client | None = None
                            ) -> tuple[list[VariantSpec], str]:
    """Resolve the input (raw code or share URL) and return (variants, raw_code).
    Used by the import wizard to preview before persisting."""
    code = resolve_pob_code(text, client=client)
    return list_variants(decode_pob_code(code)), code


def encode_pob_xml(root: ET.Element) -> str:
    """Re-encode an XML tree as a PoB export code (zlib + urlsafe-base64).
    Inverse of decode_pob_code."""
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    raw = zlib.compress(xml_bytes)
    b64 = base64.b64encode(raw).decode("ascii")
    return b64.replace("+", "-").replace("/", "_")


def encode_with_active_variant(code: str, variant: VariantSpec) -> str:
    """Return a new PoB export code with the document's `active*` attributes
    rewritten to point at this variant. Used by the import wizard so each
    variant can be pasted into poe.ninja's PoB viewer to render correctly."""
    root = decode_pob_code(code)
    items_el = root.find("Items")
    skills_el = root.find("Skills")
    tree_el = root.find("Tree")
    if items_el is not None and variant.item_set_id:
        items_el.set("activeItemSet", variant.item_set_id)
    if skills_el is not None and variant.skill_set_id:
        skills_el.set("activeSkillSet", variant.skill_set_id)
    if tree_el is not None and variant.tree_spec_id:
        tree_el.set("activeSpec", variant.tree_spec_id)
    return encode_pob_xml(root)
