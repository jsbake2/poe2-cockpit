"""Data store for the PoE2 Companion revamp.

Single JSON file at data/state.json. Atomic writes via tmp+rename. No locking —
2-user tool, low concurrency, last writer wins is acceptable.

Schema version tracked in the file; load() migrates forward.
"""

from __future__ import annotations

import json
import os
import secrets as _sys_secrets
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "state.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return _sys_secrets.token_urlsafe(9)  # ~12 chars, URL-safe


# ---------- schema ----------

@dataclass
class StoredItem:
    slot: str
    name: str = ""
    base: str = ""
    rarity: str = ""
    item_level: int | None = None
    mods: list[dict[str, str]] = field(default_factory=list)  # [{text, kind}]
    content_hash: str = ""


@dataclass
class StoredTree:
    class_name: str = ""
    ascendancy: str = ""
    nodes: list[int] = field(default_factory=list)
    mastery_effects: list[int] = field(default_factory=list)
    jewel_sockets: dict[str, str] = field(default_factory=dict)  # nodeId(str) -> itemId
    url: str = ""


@dataclass
class StoredSkill:
    label: str
    gems: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class BuildVariant:
    """One PoB variant attached to a Build. Carries its own items/tree/skills/
    main_skill + poe.ninja link. The Build's flat `items`/`tree`/`skills`/
    `main_skill` fields mirror whichever variant is currently active, so every
    existing endpoint keeps working without per-variant awareness."""
    id: str                 # short, build-local id (e.g. "v1")
    label: str
    item_set_id: str = ""   # PoB ItemSet id this variant was sourced from
    skill_set_id: str = ""  # PoB SkillSet id
    tree_spec_id: str = ""  # PoB Spec id
    main_skill: str = ""
    items: dict[str, StoredItem] = field(default_factory=dict)
    tree: StoredTree = field(default_factory=StoredTree)
    skills: list[StoredSkill] = field(default_factory=list)
    tree_viewer_url: str = ""
    notes: str = ""


@dataclass
class Build:
    id: str
    label: str
    phase: str  # "leveling" | "endgame"
    character_class: str = ""
    ascendancy: str = ""
    main_skill: str = ""
    pob_code: str = ""
    imported_at: str = field(default_factory=_now)
    imported_by: str = ""
    notes: str = ""
    stats: dict[str, float] = field(default_factory=dict)
    items: dict[str, StoredItem] = field(default_factory=dict)  # slot -> item
    tree: StoredTree = field(default_factory=StoredTree)
    skills: list[StoredSkill] = field(default_factory=list)
    # Per-build URL for the tree viewer (poe.ninja / pob.cool). Manually pasted
    # by jbaker after importing, since PoB's code format can't be automatically
    # converted to poe.ninja's short-id format without their (unpublished) API.
    tree_viewer_url: str = ""
    # Per-build link-scanner config: subreddits + keywords. If blank, defaults
    # derived from class/ascendancy/main_skill are used at scan time.
    scanner_subreddits: list[str] = field(default_factory=list)
    scanner_keywords: list[str] = field(default_factory=list)
    # Free-form links attached to the build (guides, variants, wiki, etc.).
    # Each entry: {id, url, title, added_at}.
    links: list[dict] = field(default_factory=list)
    # PoB build variants (MoM/CI/budget/etc.). When non-empty, the flat fields
    # above mirror build_variants[active]. Single-variant builds may leave this
    # empty — load() will synthesize one on read for uniform UI handling.
    build_variants: list[BuildVariant] = field(default_factory=list)
    active_build_variant_id: str = ""


@dataclass
class Variant:
    """Per-slot (or per-tree) labeled filter variant, shared across users.

    `slot` is one of the item slots ("Body Armour", "Ring 1", ...),
    "Passive Tree", or a jewel/flask identifier. `mod_rankings` keys are
    mod-hashes (lib.pob.Mod.hash()); values are "must" | "nice" | "ignore".
    For tree variants, `nodes` is used instead.
    """
    id: str
    build_id: str
    slot: str
    label: str
    notes: str = ""
    mod_rankings: dict[str, str] = field(default_factory=dict)
    trade_url: str = ""  # optional override; empty means "compute from rankings"
    nodes: list[int] = field(default_factory=list)  # used for tree variants
    created_by: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class Note:
    id: str
    author: str
    text: str
    created_at: str = field(default_factory=_now)


@dataclass
class ScriptLink:
    id: str
    title: str
    body: str = ""          # text snippet (e.g., AHK macro)
    url: str = ""           # external link (e.g., YouTube, crafting calc)
    tags: list[str] = field(default_factory=list)
    created_by: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class Scanner:
    id: str
    label: str
    terms: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # e.g. "reddit:pathofexile2", "youtube"
    created_by: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class FilterState:
    path: str = ""                # absolute path to the .filter file on server
    last_sync_at: str = ""
    last_file_hash: str = ""
    profit_block_hash: str = ""   # hash of our injected block when last seen
    has_profit_block: bool = False


@dataclass
class State:
    schema_version: int = SCHEMA_VERSION
    builds: dict[str, Build] = field(default_factory=dict)
    variants: dict[str, Variant] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    scripts: list[ScriptLink] = field(default_factory=list)
    scanners: list[Scanner] = field(default_factory=list)
    filter: FilterState = field(default_factory=FilterState)
    active_builds: dict[str, str] = field(default_factory=dict)  # user -> build_id
    # acquired[build_id][user][slot] = bool
    acquired: dict[str, dict[str, dict[str, bool]]] = field(default_factory=dict)
    # Maxroll-scraped leveling build guides (keyed by slug for dedup across scans)
    leveling_builds: dict[str, dict] = field(default_factory=dict)
    leveling_last_scan: str = ""
    # Profit-filter feature: list of unique-item names user wants highlighted
    profit_uniques: list[str] = field(default_factory=list)
    # Path settings for the loot-filter injection
    profit_filter_source: str = "FilterBlade.filter"  # relative to project root or absolute
    profit_filter_dest: str = ""  # "" = auto: <source basename>_profit.filter
    # Rare-gear walkthrough: per-build list of slot names the user approved for
    # inclusion in the rare-highlights section. Filter gen looks up the build's
    # item at that slot and emits a BaseType rule for it.
    profit_rare_slots: dict[str, list[str]] = field(default_factory=dict)


# ---------- (de)serialization ----------

def _dict_to_dc(dc_type, data: dict) -> Any:
    """Shallow dataclass hydration, skipping unknown keys."""
    field_names = {f.name for f in dc_type.__dataclass_fields__.values()}
    return dc_type(**{k: v for k, v in data.items() if k in field_names})


def _variant_from_dict(d: dict) -> BuildVariant:
    items = {slot: _dict_to_dc(StoredItem, iv)
             for slot, iv in (d.get("items") or {}).items()}
    tree = _dict_to_dc(StoredTree, d.get("tree") or {})
    skills = [_dict_to_dc(StoredSkill, sk) for sk in (d.get("skills") or [])]
    base = {k: v for k, v in d.items() if k not in {"items", "tree", "skills"}}
    v = _dict_to_dc(BuildVariant, base)
    v.items = items
    v.tree = tree
    v.skills = skills
    return v


def _state_from_dict(d: dict) -> State:
    s = State(schema_version=int(d.get("schema_version", SCHEMA_VERSION)))
    for bid, bd in (d.get("builds") or {}).items():
        items = {
            slot: _dict_to_dc(StoredItem, iv)
            for slot, iv in (bd.get("items") or {}).items()
        }
        tree = _dict_to_dc(StoredTree, bd.get("tree") or {})
        skills = [_dict_to_dc(StoredSkill, sk) for sk in (bd.get("skills") or [])]
        build_variants = [_variant_from_dict(vd) for vd in (bd.get("build_variants") or [])]
        base = {k: v for k, v in bd.items()
                if k not in {"items", "tree", "skills", "build_variants"}}
        b = _dict_to_dc(Build, {**base, "id": bid})
        b.items = items
        b.tree = tree
        b.skills = skills
        b.build_variants = build_variants
        # Migrate legacy single-variant builds: synthesize one variant mirroring
        # the flat fields so UI/import code can assume the list is non-empty.
        if not b.build_variants:
            b.build_variants = [BuildVariant(
                id="v1",
                label=b.label or "Default",
                main_skill=b.main_skill,
                items=dict(b.items),
                tree=b.tree,
                skills=list(b.skills),
                tree_viewer_url=b.tree_viewer_url,
            )]
            b.active_build_variant_id = "v1"
        elif not b.active_build_variant_id:
            b.active_build_variant_id = b.build_variants[0].id
        s.builds[bid] = b
    for vid, vd in (d.get("variants") or {}).items():
        s.variants[vid] = _dict_to_dc(Variant, {**vd, "id": vid})
    s.notes = [_dict_to_dc(Note, n) for n in (d.get("notes") or [])]
    s.scripts = [_dict_to_dc(ScriptLink, n) for n in (d.get("scripts") or [])]
    s.scanners = [_dict_to_dc(Scanner, n) for n in (d.get("scanners") or [])]
    s.filter = _dict_to_dc(FilterState, d.get("filter") or {})
    s.active_builds = dict(d.get("active_builds") or {})
    s.acquired = {k: {u: dict(v2 or {}) for u, v2 in (v or {}).items()}
                  for k, v in (d.get("acquired") or {}).items()}
    s.leveling_builds = dict(d.get("leveling_builds") or {})
    s.leveling_last_scan = d.get("leveling_last_scan") or ""
    s.profit_uniques = list(d.get("profit_uniques") or [])
    s.profit_filter_source = d.get("profit_filter_source") or "FilterBlade.filter"
    s.profit_filter_dest = d.get("profit_filter_dest") or ""
    s.profit_rare_slots = {k: list(v or []) for k, v in (d.get("profit_rare_slots") or {}).items()}
    return s


def _variant_to_dict(v: BuildVariant) -> dict:
    return {
        **asdict(v),
        "items": {slot: asdict(it) for slot, it in v.items.items()},
        "tree": asdict(v.tree),
        "skills": [asdict(sk) for sk in v.skills],
    }


def _state_to_dict(s: State) -> dict:
    return {
        "schema_version": s.schema_version,
        "builds": {
            bid: {
                **asdict(b),
                "items": {slot: asdict(it) for slot, it in b.items.items()},
                "tree": asdict(b.tree),
                "skills": [asdict(sk) for sk in b.skills],
                "build_variants": [_variant_to_dict(v) for v in b.build_variants],
            }
            for bid, b in s.builds.items()
        },
        "variants": {vid: asdict(v) for vid, v in s.variants.items()},
        "notes": [asdict(n) for n in s.notes],
        "scripts": [asdict(n) for n in s.scripts],
        "scanners": [asdict(n) for n in s.scanners],
        "filter": asdict(s.filter),
        "active_builds": dict(s.active_builds),
        "acquired": s.acquired,
        "leveling_builds": dict(s.leveling_builds),
        "leveling_last_scan": s.leveling_last_scan,
        "profit_uniques": list(s.profit_uniques),
        "profit_filter_source": s.profit_filter_source,
        "profit_filter_dest": s.profit_filter_dest,
        "profit_rare_slots": {k: list(v) for k, v in s.profit_rare_slots.items()},
    }


# ---------- persistence ----------

def load() -> State:
    if not STATE_PATH.exists():
        return State()
    with STATE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return _state_from_dict(data)


def save(state: State) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(_state_to_dict(state), f, indent=2, sort_keys=False)
    os.replace(tmp, STATE_PATH)


# ---------- domain helpers ----------

def _items_from_pob(build_obj) -> dict[str, StoredItem]:
    items: dict[str, StoredItem] = {}
    for it in build_obj.items:
        items[it.slot] = StoredItem(
            slot=it.slot,
            name=it.name,
            base=it.base,
            rarity=it.rarity,
            item_level=it.item_level,
            mods=[{"text": m.text, "kind": m.kind, "tags": list(m.tags)} for m in it.mods],
            content_hash=it.content_hash(),
        )
    return items


def _tree_from_pob(build_obj) -> StoredTree:
    return StoredTree(
        class_name=build_obj.tree.class_name,
        ascendancy=build_obj.tree.ascendancy,
        nodes=list(build_obj.tree.nodes),
        mastery_effects=list(build_obj.tree.mastery_effects),
        jewel_sockets={str(k): v for k, v in build_obj.tree.jewel_sockets.items()},
        url=build_obj.tree.url,
    )


def _skills_from_pob(build_obj) -> list[StoredSkill]:
    return [StoredSkill(label=sk.label, gems=list(sk.gems), enabled=sk.enabled)
            for sk in build_obj.skills]


def new_build_from_pob_import(build_obj, *, label: str, phase: str,
                              pob_code: str, imported_by: str) -> Build:
    """Convert a lib.pob.Build into our storage Build dataclass.

    Synthesizes a single default BuildVariant so the Build always has at
    least one entry in build_variants — multi-variant imports should use
    new_variant_from_pob_import and apply_active_variant afterwards."""
    items = _items_from_pob(build_obj)
    tree = _tree_from_pob(build_obj)
    skills = _skills_from_pob(build_obj)
    default_variant = BuildVariant(
        id="v1",
        label=label or "Default",
        main_skill=build_obj.main_skill,
        items=dict(items),
        tree=tree,
        skills=list(skills),
    )
    return Build(
        id=_new_id(),
        label=label,
        phase=phase,
        character_class=build_obj.class_name,
        ascendancy=build_obj.ascendancy,
        main_skill=build_obj.main_skill,
        pob_code=pob_code,
        imported_by=imported_by,
        notes=build_obj.notes,
        stats=dict(build_obj.stats),
        items=items,
        tree=tree,
        skills=skills,
        build_variants=[default_variant],
        active_build_variant_id="v1",
    )


def new_variant_from_pob_import(build_obj, *, variant_id: str, label: str,
                                tree_viewer_url: str = "",
                                pob_variant_spec=None) -> BuildVariant:
    """Build a BuildVariant from a parsed pob.Build. `pob_variant_spec` is the
    pob.VariantSpec used to drive the parse — its ids are recorded so re-import
    can reproduce the selection."""
    return BuildVariant(
        id=variant_id,
        label=label,
        item_set_id=(pob_variant_spec.item_set_id if pob_variant_spec else ""),
        skill_set_id=(pob_variant_spec.skill_set_id if pob_variant_spec else ""),
        tree_spec_id=(pob_variant_spec.tree_spec_id if pob_variant_spec else ""),
        main_skill=build_obj.main_skill,
        items=_items_from_pob(build_obj),
        tree=_tree_from_pob(build_obj),
        skills=_skills_from_pob(build_obj),
        tree_viewer_url=tree_viewer_url,
    )


def apply_active_variant(b: Build) -> None:
    """Copy the currently active BuildVariant's data into the Build's flat
    fields. Called after switching variants or after multi-variant import so
    downstream code reading b.items / b.tree / b.skills sees the right data."""
    if not b.build_variants:
        return
    target = next((v for v in b.build_variants
                   if v.id == b.active_build_variant_id), b.build_variants[0])
    b.active_build_variant_id = target.id
    b.main_skill = target.main_skill
    b.items = dict(target.items)
    b.tree = target.tree
    b.skills = list(target.skills)
    b.tree_viewer_url = target.tree_viewer_url


def diff_items(old: Build | None, new_items: dict[str, StoredItem]) -> dict[str, str]:
    """Per-slot: 'unchanged' | 'added' | 'modified' | 'removed'.

    On re-import, use this to know which slots keep their variant mod-rankings
    and which need re-ranking.
    """
    result: dict[str, str] = {}
    old_items = (old.items if old else {})
    all_slots = set(old_items) | set(new_items)
    for slot in all_slots:
        o, n = old_items.get(slot), new_items.get(slot)
        if o is None:
            result[slot] = "added"
        elif n is None:
            result[slot] = "removed"
        elif o.content_hash == n.content_hash:
            result[slot] = "unchanged"
        else:
            result[slot] = "modified"
    return result


def new_variant(*, build_id: str, slot: str, label: str, created_by: str,
                notes: str = "", mod_rankings: dict[str, str] | None = None,
                trade_url: str = "", nodes: list[int] | None = None) -> Variant:
    return Variant(
        id=_new_id(),
        build_id=build_id,
        slot=slot,
        label=label,
        notes=notes,
        mod_rankings=dict(mod_rankings or {}),
        trade_url=trade_url,
        nodes=list(nodes or []),
        created_by=created_by,
    )


def new_note(author: str, text: str) -> Note:
    return Note(id=_new_id(), author=author, text=text)


def new_script(*, title: str, body: str = "", url: str = "",
               tags: list[str] | None = None, created_by: str = "") -> ScriptLink:
    return ScriptLink(id=_new_id(), title=title, body=body, url=url,
                      tags=list(tags or []), created_by=created_by)


def new_scanner(*, label: str, terms: list[str], sources: list[str],
                created_by: str = "") -> Scanner:
    return Scanner(id=_new_id(), label=label, terms=list(terms),
                   sources=list(sources), created_by=created_by)
