"""FastAPI routes for the revamped cockpit (prefix /v2).

Reuses the auth middleware and get_user() helper from app.py. Import is
jbaker-only; everything else is visible to both users.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

import json
import time
from urllib.parse import quote

from . import filter_validate, leveling, news, pob, profit, scanner, store, trade_stats, tree

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
COCKPIT_HTML = STATIC / "cockpit.html"

router = APIRouter(prefix="/v2")


def _require_user(request: Request) -> str | None:
    # Resolved lazily to avoid a circular import at module load.
    from app import get_user  # type: ignore
    return get_user(request)


# Cockpit HTML is served at "/" by app.py now; /v2 redirects to /.

# --- Tree -----------------------------------------------------------------

@router.get("/api/tree")
async def get_tree():
    return tree.load_layout()


# --- Builds ---------------------------------------------------------------

def _sanitize_for_json(obj):
    # PoB emits inf/nan for some derived stats (e.g. ChaosMaximumHitTaken on
    # full chaos immunity). The strict JSON encoder rejects these and 500s the
    # whole response, so we replace them with None before serializing.
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _augment_items_with_hashes(items: dict) -> None:
    for slot, it in items.items():
        it["mods"] = [
            {**m, "hash": pob.Mod(text=m["text"], kind=m["kind"]).hash()}
            for m in it["mods"]
        ]


def _build_to_dict(b: store.Build) -> dict:
    d = asdict(b)
    # Include mod hashes alongside the text for client-side keying of rankings.
    _augment_items_with_hashes(d["items"])
    # Same for every variant so the UI can switch variants client-side.
    for v in d.get("build_variants") or []:
        _augment_items_with_hashes(v["items"])
    return _sanitize_for_json(d)


@router.get("/api/builds")
async def list_builds():
    s = store.load()
    return {
        "builds": [
            {
                "id": b.id, "label": b.label, "phase": b.phase,
                "character_class": b.character_class,
                "ascendancy": b.ascendancy,
                "main_skill": b.main_skill, "level": 0,
                "imported_at": b.imported_at,
                "imported_by": b.imported_by,
                "build_variants": [
                    {"id": v.id, "label": v.label} for v in b.build_variants
                ],
                "active_build_variant_id": b.active_build_variant_id,
            }
            for b in s.builds.values()
        ]
    }


@router.get("/api/builds/{build_id}")
async def get_build(build_id: str):
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _build_to_dict(b)


@router.post("/api/builds/preview")
async def preview_pob(request: Request):
    """Parse a PoB paste/URL without persisting; return variants for the
    multi-variant import wizard. jbaker-only (matches /api/builds POST)."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    text = (body.get("pob") or "").strip()
    if not text:
        return JSONResponse({"error": "missing 'pob'"}, status_code=400)
    try:
        variants, raw_code = pob.list_variants_from_code(text)
        # Also parse the default variant for the build-level metadata
        # (class, ascendancy) used to suggest a label.
        default_build = pob.import_build(raw_code)
    except pob.PoBImportError as e:
        return JSONResponse({"error": f"PoB parse failed: {e}"}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"error": f"Preview failed ({type(e).__name__}): {e}"}, status_code=400
        )
    return {
        "ok": True,
        "pob_code": raw_code,
        "class_name": default_build.class_name,
        "ascendancy": default_build.ascendancy,
        "level": default_build.level,
        "variants": [
            {
                "id": v.id,
                "label": v.label,
                "item_set_id": v.item_set_id,
                "skill_set_id": v.skill_set_id,
                "tree_spec_id": v.tree_spec_id,
                # Variant-specific PoB code: same XML, but with active* attrs
                # rewritten to this variant. Pasting this into poe.ninja's PoB
                # viewer renders THIS variant, so the user can grab a distinct
                # poe.ninja URL per variant.
                "pob_code": pob.encode_with_active_variant(raw_code, v),
            }
            for v in variants
        ],
    }


def _short_variant_id(idx: int) -> str:
    return f"v{idx + 1}"


@router.post("/api/builds")
async def import_build(request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "import is jbaker-only"}, status_code=403)
    body = await request.json()
    text = (body.get("pob") or "").strip()
    if not text:
        return JSONResponse({"error": "missing 'pob' (paste code or share URL)"}, status_code=400)
    label = (body.get("label") or "").strip()
    phase = (body.get("phase") or "endgame").strip().lower()
    if phase not in ("leveling", "endgame"):
        return JSONResponse({"error": "phase must be 'leveling' or 'endgame'"}, status_code=400)

    # Legacy single-variant body: just a tree_viewer_url.
    # New multi-variant body: a `variants` array where each entry references a
    # pob variant id (from /preview) and supplies its own tree_viewer_url+label.
    legacy_tree_viewer_url = (body.get("tree_viewer_url") or "").strip()
    variants_in = body.get("variants")

    try:
        pob_variants, raw_code = pob.list_variants_from_code(text)
    except pob.PoBImportError as e:
        return JSONResponse({"error": f"PoB parse failed: {e}"}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"error": f"Import failed ({type(e).__name__}): {e}"}, status_code=400
        )
    pob_variants_by_id = {v.id: v for v in pob_variants}

    # Normalize selection list. If client didn't send `variants`, fall back to
    # the PoB's currently-active variant (legacy behavior).
    if isinstance(variants_in, list) and variants_in:
        selected: list[dict] = []
        for entry in variants_in:
            pob_vid = (entry.get("variant_id") or "").strip()
            spec = pob_variants_by_id.get(pob_vid)
            if spec is None:
                return JSONResponse(
                    {"error": f"unknown variant_id {pob_vid!r}"}, status_code=400
                )
            selected.append({
                "spec": spec,
                "label": (entry.get("label") or spec.label).strip(),
                "tree_viewer_url": (entry.get("tree_viewer_url") or "").strip(),
            })
    else:
        spec = pob_variants[0]
        selected = [{
            "spec": spec,
            "label": spec.label,
            "tree_viewer_url": legacy_tree_viewer_url,
        }]

    # Parse each selected variant to a pob.Build, then convert to BuildVariant.
    try:
        per_variant_builds = [
            (sel, pob.import_build(raw_code, variant=sel["spec"]))
            for sel in selected
        ]
    except pob.PoBImportError as e:
        return JSONResponse({"error": f"PoB parse failed: {e}"}, status_code=400)

    # Derive top-level metadata from the first selected variant.
    first_build = per_variant_builds[0][1]
    if not label:
        label = f"{first_build.class_name} — {first_build.ascendancy or phase}".strip(" —")

    build_variants: list[store.BuildVariant] = []
    for idx, (sel, pb) in enumerate(per_variant_builds):
        build_variants.append(store.new_variant_from_pob_import(
            pb,
            variant_id=_short_variant_id(idx),
            label=sel["label"],
            tree_viewer_url=sel["tree_viewer_url"],
            pob_variant_spec=sel["spec"],
        ))

    s = store.load()
    stored = store.new_build_from_pob_import(
        first_build, label=label, phase=phase, pob_code=raw_code, imported_by=user,
    )
    # Replace the synthesized single-variant entry with the user's selection
    # and re-mirror flat fields to whichever variant they marked active.
    stored.build_variants = build_variants
    requested_active = (body.get("active_variant_id") or build_variants[0].id).strip()
    stored.active_build_variant_id = (
        requested_active if any(v.id == requested_active for v in build_variants)
        else build_variants[0].id
    )
    store.apply_active_variant(stored)
    s.builds[stored.id] = stored
    s.active_builds.setdefault(user, stored.id)
    for other in ("jbaker", "matt"):
        s.active_builds.setdefault(other, stored.id)
    store.save(s)
    return {
        "ok": True,
        "build_id": stored.id,
        "label": stored.label,
        "variants": [{"id": v.id, "label": v.label} for v in stored.build_variants],
        "active_build_variant_id": stored.active_build_variant_id,
    }


@router.put("/api/builds/{build_id}/active-variant")
async def set_active_build_variant(build_id: str, request: Request):
    """Switch the active BuildVariant; flat fields get re-mirrored from it."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    variant_id = (body.get("variant_id") or "").strip()
    if not variant_id:
        return JSONResponse({"error": "missing 'variant_id'"}, status_code=400)
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not any(v.id == variant_id for v in b.build_variants):
        return JSONResponse({"error": f"unknown variant {variant_id!r}"}, status_code=400)
    b.active_build_variant_id = variant_id
    store.apply_active_variant(b)
    store.save(s)
    return {
        "ok": True,
        "build_id": build_id,
        "active_build_variant_id": variant_id,
    }


@router.put("/api/builds/{build_id}/variants/{variant_id}")
async def update_build_variant(build_id: str, variant_id: str, request: Request):
    """Edit a build variant's user-facing fields (label, poe.ninja URL).
    If the edited variant is active, the build's flat tree_viewer_url is updated
    too."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    target = next((v for v in b.build_variants if v.id == variant_id), None)
    if target is None:
        return JSONResponse({"error": "variant not found"}, status_code=404)
    if "label" in body:
        target.label = (body.get("label") or "").strip() or target.label
    if "tree_viewer_url" in body:
        target.tree_viewer_url = (body.get("tree_viewer_url") or "").strip()
    if target.id == b.active_build_variant_id:
        store.apply_active_variant(b)
    store.save(s)
    return {"ok": True, "variant": asdict(target)}


@router.put("/api/builds/{build_id}/tree-viewer")
async def set_tree_viewer_url(build_id: str, request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    url = (body.get("url") or "").strip()
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Persist on the active variant (the flat field is just a mirror).
    active = next((v for v in b.build_variants
                   if v.id == b.active_build_variant_id), None)
    if active is not None:
        active.tree_viewer_url = url
    b.tree_viewer_url = url
    store.save(s)
    return {"ok": True, "url": url}


@router.put("/api/builds/{build_id}")
async def edit_build(build_id: str, request: Request):
    """Re-parse the build from a new PoB code and/or update its viewer URL.
    Preserves id, label, phase, and any attached variants."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    pob_text = (body.get("pob") or "").strip()
    new_url = body.get("tree_viewer_url")
    new_label = (body.get("label") or "").strip()
    new_phase = (body.get("phase") or "").strip().lower()

    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Optional re-parse
    if pob_text:
        try:
            parsed, raw_code = pob.import_build_with_code(pob_text)
        except pob.PoBImportError as e:
            return JSONResponse({"error": f"PoB parse failed: {e}"}, status_code=400)
        # Build a fresh stored view then copy fields onto existing build (preserves id)
        fresh = store.new_build_from_pob_import(
            parsed, label=b.label, phase=b.phase, pob_code=raw_code, imported_by=user,
        )
        b.pob_code = raw_code
        b.imported_at = fresh.imported_at
        b.imported_by = user
        b.notes = fresh.notes
        b.stats = fresh.stats
        b.character_class = fresh.character_class
        b.ascendancy = fresh.ascendancy
        b.main_skill = fresh.main_skill
        b.items = fresh.items
        b.tree = fresh.tree
        b.skills = fresh.skills

    if new_url is not None:
        b.tree_viewer_url = new_url.strip()
    if new_label:
        b.label = new_label
    if new_phase in ("leveling", "endgame"):
        b.phase = new_phase

    store.save(s)
    return {"ok": True, "build_id": b.id}


@router.delete("/api/builds/{build_id}")
async def delete_build(build_id: str, request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "delete is jbaker-only"}, status_code=403)
    s = store.load()
    if build_id not in s.builds:
        return JSONResponse({"error": "not found"}, status_code=404)
    del s.builds[build_id]
    # Clear active pointers that referenced it.
    for u, bid in list(s.active_builds.items()):
        if bid == build_id:
            s.active_builds[u] = next(iter(s.builds), "")
    s.acquired.pop(build_id, None)
    # Drop variants for that build.
    s.variants = {vid: v for vid, v in s.variants.items() if v.build_id != build_id}
    store.save(s)
    return {"ok": True}


# --- Per-build links ------------------------------------------------------

@router.post("/api/builds/{build_id}/links")
async def add_build_link(build_id: str, request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    url = (body.get("url") or "").strip()
    title = (body.get("title") or "").strip()
    if not url:
        return JSONResponse({"error": "missing 'url'"}, status_code=400)
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse({"error": "url must start with http(s)://"}, status_code=400)
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    import secrets, time
    link = {
        "id": secrets.token_urlsafe(6),
        "url": url,
        "title": title,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not isinstance(getattr(b, "links", None), list):
        b.links = []
    b.links.append(link)
    store.save(s)
    return {"ok": True, "link": link}


@router.delete("/api/builds/{build_id}/links/{link_id}")
async def remove_build_link(build_id: str, link_id: str, request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    before = len(b.links or [])
    b.links = [l for l in (b.links or []) if l.get("id") != link_id]
    if len(b.links) == before:
        return JSONResponse({"error": "link not found"}, status_code=404)
    store.save(s)
    return {"ok": True}


# --- Trade URL builder ----------------------------------------------------

@router.post("/api/builds/{build_id}/slots/{slot}/trade-url")
async def build_trade_url(build_id: str, slot: str, request: Request):
    """Given slot's mods + a rankings dict (mod_hash -> must/nice/ignore),
    return a trade.pathofexile.com URL with proper stat filters.
    """
    body = await request.json()
    league = (body.get("league") or "Standard").strip() or "Standard"
    rankings: dict[str, str] = dict(body.get("rankings") or {})
    # rank_values[mod_hash] = {"min": float|None, "max": float|None}
    rank_values: dict[str, dict] = dict(body.get("rank_values") or {})
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "unknown build"}, status_code=404)
    item = b.items.get(slot)
    if not item:
        return JSONResponse({"error": "unknown slot"}, status_code=404)

    # PoE2 supports offline trading, so default to "any" (online + offline).
    query: dict = {"status": {"option": "any"}}
    if item.rarity == "Unique" and item.name:
        query["name"] = item.name
    elif item.base:
        query["type"] = item.base

    filters: list[dict] = []
    unmatched: list[str] = []
    for m in item.mods:
        mod_hash = pob.Mod(text=m["text"], kind=m["kind"]).hash()
        rank = rankings.get(mod_hash, "nice")
        if rank == "ignore":
            continue
        match = trade_stats.match_mod(m["text"], m["kind"])
        if not match:
            unmatched.append(m["text"])
            continue
        f: dict = {"id": match["id"], "disabled": rank != "must"}
        if rank == "must":
            # Prefer explicit user-entered min/max; else seed from mod text's first number.
            user_vals = rank_values.get(mod_hash) or {}
            val_block: dict = {}
            if user_vals.get("min") not in (None, ""):
                try: val_block["min"] = float(user_vals["min"])
                except (TypeError, ValueError): pass
            if user_vals.get("max") not in (None, ""):
                try: val_block["max"] = float(user_vals["max"])
                except (TypeError, ValueError): pass
            if "min" not in val_block:
                vals = trade_stats.extract_values(m["text"])
                if vals:
                    val_block["min"] = vals[0]
            if val_block:
                f["value"] = val_block
        filters.append(f)

    if filters:
        query["stats"] = [{"type": "and", "filters": filters}]

    payload = {"query": query, "sort": {"price": "asc"}}
    url = (f"https://www.pathofexile.com/trade2/search/poe2/{quote(league)}"
           f"?q={quote(json.dumps(payload, separators=(',', ':')))}")
    return {"url": url, "unmatched_mods": unmatched, "matched_count": len(filters)}


# --- Variants (shared per-slot trade URL library) ------------------------

@router.get("/api/builds/{build_id}/variants")
async def list_variants(build_id: str):
    s = store.load()
    if build_id not in s.builds:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"variants": [asdict(v) for v in s.variants.values() if v.build_id == build_id]}


@router.post("/api/builds/{build_id}/variants")
async def create_variant(build_id: str, request: Request):
    user = _require_user(request)
    body = await request.json()
    slot = (body.get("slot") or "").strip()
    label = (body.get("label") or "").strip()
    trade_url = (body.get("trade_url") or "").strip()
    if not slot or not label:
        return JSONResponse({"error": "slot and label required"}, status_code=400)
    s = store.load()
    if build_id not in s.builds:
        return JSONResponse({"error": "unknown build"}, status_code=404)
    v = store.new_variant(
        build_id=build_id, slot=slot, label=label,
        trade_url=trade_url, notes=(body.get("notes") or "").strip(),
        created_by=user or "",
    )
    s.variants[v.id] = v
    store.save(s)
    return {"ok": True, "variant": asdict(v)}


@router.put("/api/variants/{variant_id}")
async def update_variant(variant_id: str, request: Request):
    body = await request.json()
    s = store.load()
    v = s.variants.get(variant_id)
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    for field in ("label", "trade_url", "notes"):
        if field in body:
            setattr(v, field, (body.get(field) or "").strip())
    store.save(s)
    return {"ok": True, "variant": asdict(v)}


@router.delete("/api/variants/{variant_id}")
async def delete_variant(variant_id: str):
    s = store.load()
    if variant_id not in s.variants:
        return JSONResponse({"error": "not found"}, status_code=404)
    del s.variants[variant_id]
    store.save(s)
    return {"ok": True}


# --- Link scanner --------------------------------------------------------

def _default_keywords_for(b: store.Build) -> list[str]:
    """Guess sensible search keywords from a build's class/asc/main skill."""
    kws: list[str] = []
    if b.ascendancy: kws.append(b.ascendancy)
    elif b.character_class: kws.append(b.character_class)
    if b.main_skill and not b.main_skill.isdigit():
        kws.append(b.main_skill)
    # Pull skill labels that look like skill names (not group numbers)
    for sk in b.skills[:3]:
        if sk.label and not sk.label.isdigit() and len(sk.label) < 40:
            if sk.label not in kws:
                kws.append(sk.label)
    return kws[:6]


@router.get("/api/scanner/config")
async def get_scanner_config(request: Request):
    build_id = (request.query_params.get("build_id") or "").strip()
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "unknown build_id"}, status_code=404)
    return {
        "build_id": b.id,
        "label": b.label,
        "subreddits": b.scanner_subreddits or list(scanner.DEFAULT_SUBREDDITS),
        "keywords": b.scanner_keywords or _default_keywords_for(b),
        "defaults_used": not (b.scanner_subreddits or b.scanner_keywords),
    }


@router.put("/api/scanner/config")
async def set_scanner_config(request: Request):
    body = await request.json()
    build_id = (body.get("build_id") or "").strip()
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "unknown build_id"}, status_code=404)
    subs = body.get("subreddits")
    kws = body.get("keywords")
    if isinstance(subs, list):
        b.scanner_subreddits = [str(x).strip() for x in subs if str(x).strip()]
    if isinstance(kws, list):
        b.scanner_keywords = [str(x).strip() for x in kws if str(x).strip()]
    store.save(s)
    return {"ok": True}


@router.post("/api/scanner/scan")
async def run_scanner(request: Request):
    body = await request.json()
    build_id = (body.get("build_id") or "").strip()
    s = store.load()
    b = s.builds.get(build_id)
    if not b:
        return JSONResponse({"error": "unknown build_id"}, status_code=404)
    subs = b.scanner_subreddits or list(scanner.DEFAULT_SUBREDDITS)
    kws = b.scanner_keywords or _default_keywords_for(b)
    # Time filter
    t = (body.get("time_range") or "month").strip().lower()
    sort = (body.get("sort") or "new").strip().lower()
    def _ts(x):
        try: return float(x) if x else None
        except (TypeError, ValueError): return None
    from_ts = _ts(body.get("from_ts"))
    to_ts = _ts(body.get("to_ts"))
    # Sources: default to reddit+youtube+maxroll+mobalytics
    src = body.get("sources")
    if not isinstance(src, list) or not src:
        src = ["reddit", "youtube", "maxroll", "mobalytics"]
    # Mode: "or" (any keyword matches) | "and" (post must match ALL keywords)
    mode = (body.get("mode") or "or").strip().lower()
    if mode not in ("and", "or"):
        mode = "or"
    try:
        result = scanner.scan(subs, kws, t=t, from_ts=from_ts,
                              to_ts=to_ts, sort=sort, sources=src, mode=mode)
    except Exception as e:
        return JSONResponse({"error": f"Scan failed: {e}"}, status_code=502)
    # Also emit a YouTube search URL per keyword so the user can jump there
    result["youtube_searches"] = [
        {"keyword": k, "url": scanner.youtube_search_url(f"PoE 2 {k}")}
        for k in kws
    ]
    result["used_subreddits"] = subs
    result["used_keywords"] = kws
    return result


# --- News / patch notes --------------------------------------------------

@router.get("/api/news")
async def get_news(request: Request):
    force = (request.query_params.get("force") or "").lower() in ("1", "true", "yes")
    try:
        items = news.fetch_items(force=force)
    except Exception as e:
        return JSONResponse({"error": f"News fetch failed: {e}"}, status_code=502)
    return {"items": items, "count": len(items)}


# --- Profit filter (jbaker-only) -----------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Per-user cooldown for profit-filter download (user -> last_download_ts)
_profit_download_rate: dict[str, float] = {}


def _profit_paths(state: store.State) -> tuple[Path, Path]:
    """Resolve source and dest filter paths from state config."""
    src = state.profit_filter_source or "FilterBlade.filter"
    src_path = Path(src) if Path(src).is_absolute() else PROJECT_ROOT / src
    dest = state.profit_filter_dest
    if not dest:
        stem = src_path.stem
        dest_path = src_path.with_name(f"{stem}_profit{src_path.suffix}")
    else:
        dest_path = Path(dest) if Path(dest).is_absolute() else PROJECT_ROOT / dest
    return src_path, dest_path


@router.get("/api/profit/uniques")
async def list_all_uniques():
    """Full PoE2 unique catalog from GGG (cached)."""
    try:
        return {"uniques": profit.fetch_all_uniques()}
    except Exception as e:
        return JSONResponse({"error": f"Fetch failed: {e}"}, status_code=502)


@router.get("/api/profit/list")
async def get_profit_list(request: Request):
    s = store.load()
    src, dest = _profit_paths(s)
    # Seed on first use
    current = list(s.profit_uniques)
    if not current:
        try:
            catalog_names = {u["name"] for u in profit.fetch_all_uniques()}
            seed = [n for n in profit.SEED_UNIQUE_NAMES if n in catalog_names]
            current = seed
        except Exception:
            current = list(profit.SEED_UNIQUE_NAMES)
    return {
        "uniques": current,
        "source_path": str(src),
        "source_exists": src.exists(),
        "dest_path": str(dest),
        "dest_exists": dest.exists(),
        "last_built": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(dest.stat().st_mtime))
            if dest.exists() else ""
        ),
    }


@router.put("/api/profit/list")
async def set_profit_list(request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    uniques = body.get("uniques")
    if not isinstance(uniques, list):
        return JSONResponse({"error": "uniques must be a list of strings"}, status_code=400)
    s = store.load()
    s.profit_uniques = [str(u).strip() for u in uniques if str(u).strip()]
    if "source_path" in body:
        s.profit_filter_source = str(body["source_path"] or "")
    if "dest_path" in body:
        s.profit_filter_dest = str(body["dest_path"] or "")
    store.save(s)
    return {"ok": True, "count": len(s.profit_uniques)}


@router.get("/api/profit/rare-walkthrough")
async def get_rare_walkthrough(request: Request):
    """Return the active build's rare items + which are currently approved,
    with each rare's must-mods pre-computed from the user's rankings."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    s = store.load()
    bid = s.active_builds.get(user, "")
    b = s.builds.get(bid)
    if not b:
        return JSONResponse({"error": "no active build"}, status_code=400)
    # Load user's per-item-mod rankings from their per-user prefs (v1 store)
    from app import load_user  # type: ignore
    prefs = (load_user(user) or {}).get("state", {})
    rankings = (prefs.get("rankings") or {})

    rare_items = []
    for slot, it in b.items.items():
        if (it.rarity or "").lower() != "rare":
            continue
        mods_out = []
        slot_ranks = rankings.get(slot, {})
        for m in it.mods:
            mh = pob.Mod(text=m["text"], kind=m["kind"]).hash()
            mods_out.append({
                "text": m["text"],
                "kind": m["kind"],
                "hash": mh,
                "rank": slot_ranks.get(mh, "nice"),
            })
        must_mods = [m["text"] for m in mods_out if m["rank"] == "must"]
        rare_items.append({
            "slot": slot,
            "name": it.name,
            "base": it.base,
            "item_level": it.item_level,
            "mods": mods_out,
            "must_mods": must_mods,
        })
    enabled = s.profit_rare_slots.get(bid, [])
    return {
        "build_id": bid,
        "label": b.label,
        "rares": rare_items,
        "enabled_slots": enabled,
    }


@router.put("/api/profit/rare-slots")
async def set_rare_slots(request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    bid = (body.get("build_id") or "").strip()
    slots = body.get("slots")
    if not isinstance(slots, list):
        return JSONResponse({"error": "slots must be a list"}, status_code=400)
    s = store.load()
    if bid not in s.builds:
        return JSONResponse({"error": "unknown build"}, status_code=400)
    s.profit_rare_slots[bid] = [str(x).strip() for x in slots if str(x).strip()]
    store.save(s)
    return {"ok": True, "count": len(s.profit_rare_slots[bid])}


@router.get("/api/profit/filter-download")
async def download_profit_filter(request: Request):
    """Stream the generated profit filter to the caller. Rate-limited per user
    to discourage accidental rapid-fire polling from either side."""
    user = _require_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    s = store.load()
    _src, dest = _profit_paths(s)
    if not dest.exists():
        return JSONResponse(
            {"error": "Profit filter hasn't been built yet. Ask jbaker to hit Build on the Profit tab."},
            status_code=404,
        )

    # Rate limit: one download per user per 10 seconds
    now = time.time()
    _rate = _profit_download_rate.setdefault(user, 0.0)
    if now - _rate < 10:
        wait = int(10 - (now - _rate))
        return JSONResponse(
            {"error": f"Rate-limited — wait {wait}s and retry."},
            status_code=429,
        )
    _profit_download_rate[user] = now

    from fastapi.responses import FileResponse
    return FileResponse(
        dest, filename=dest.name,
        media_type="application/octet-stream",
    )


@router.get("/api/profit/filter-info")
async def profit_filter_info(request: Request):
    """Metadata about the current built filter (for the Files modal UI).
    Also reports base-filter status so the UI can prompt for an upload when
    the user hasn't dropped a fresh FilterBlade export yet."""
    s = store.load()
    src, dest = _profit_paths(s)
    base = {"exists": src.exists(), "path": str(src), "name": src.name}
    if src.exists():
        bst = src.stat()
        base["size"] = bst.st_size
        base["modified"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(bst.st_mtime))
    if not dest.exists():
        return {"exists": False, "path": str(dest), "base": base}
    st = dest.stat()
    return {
        "exists": True,
        "path": str(dest),
        "name": dest.name,
        "size": st.st_size,
        "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        "base": base,
    }


@router.post("/api/profit/base-filter-upload")
async def upload_base_filter(request: Request):
    """jbaker uploads a fresh FilterBlade-generated .filter to serve as the
    base. Stored at filters/base.filter; the profit block is prepended on each
    rebuild so the user owns the bulk of the filter and the tool just owns its
    inserted section."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    form = await request.form()
    uploaded = form.get("file")
    if not uploaded or not hasattr(uploaded, "filename"):
        return JSONResponse({"error": "No file"}, status_code=400)
    fname = (uploaded.filename or "").lower()
    if not fname.endswith(".filter"):
        return JSONResponse({"error": "Must be a .filter file"}, status_code=400)
    content = await uploaded.read()
    if not content:
        return JSONResponse({"error": "Empty file"}, status_code=400)
    text = content.decode("utf-8", errors="replace")
    if "Show" not in text and "Hide" not in text:
        return JSONResponse({"error": "Doesn't look like a loot filter (no Show/Hide rules)"},
                            status_code=400)
    base_path = PROJECT_ROOT / "filters" / "base.filter"
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(text, encoding="utf-8")
    # Point the profit pipeline at the new base; keep the existing dest name
    # so the sync timer's hardcoded path stays valid.
    s = store.load()
    s.profit_filter_source = "filters/base.filter"
    if not s.profit_filter_dest:
        s.profit_filter_dest = "FilterBlade_profit.filter"
    store.save(s)
    st = base_path.stat()
    return {
        "ok": True,
        "path": str(base_path),
        "name": base_path.name,
        "size": st.st_size,
        "uploaded_name": uploaded.filename,
    }


@router.post("/api/profit/extract-from-builds")
async def extract_uniques_from_builds(request: Request):
    """Parse a list of pasted build URLs/codes and return their unique-item names.
    Accepts pobb.in / pob.cool / poe2db.tw URLs and raw PoB export codes. Flags
    URLs we can't auto-resolve (e.g. poe.ninja, since their API isn't public).
    """
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    inputs = body.get("inputs") or ""
    if isinstance(inputs, str):
        lines = [ln.strip() for ln in inputs.splitlines() if ln.strip()]
    elif isinstance(inputs, list):
        lines = [str(x).strip() for x in inputs if str(x).strip()]
    else:
        return JSONResponse({"error": "inputs must be string or list"}, status_code=400)

    results: list[dict] = []
    unique_counts: dict[str, int] = {}  # name -> number of builds using it
    catalog_names = {u["name"] for u in profit.fetch_all_uniques()}

    for idx, line in enumerate(lines):
        entry: dict = {"index": idx, "input": line[:80] + ("…" if len(line) > 80 else ""), "ok": False}
        # Detect poe.ninja URLs early — they need special handling
        low = line.lower()
        if low.startswith(("http://", "https://")) and "poe.ninja" in low:
            if "/character/" in low:
                entry["error"] = (
                    "poe.ninja ladder character pages aren't scrapable (their data "
                    "loads via JS we can't run server-side). Open the character on "
                    "poe.ninja, click their 'Export to Path of Building' button to "
                    "get a pobb.in link, then paste that link here."
                )
            elif "/poe2/pob/" in low:
                entry["error"] = (
                    "poe.ninja /poe2/pob URLs aren't auto-importable either. On "
                    "that page, click their Export/Copy button to get the raw "
                    "PoB code or a pobb.in link, then paste that here."
                )
            else:
                entry["error"] = (
                    "poe.ninja URLs can't be auto-imported. Paste a pobb.in, "
                    "pob.cool, or poe2db.tw URL, or a raw PoB code instead."
                )
            results.append(entry)
            continue
        try:
            parsed, raw_code = pob.import_build_with_code(line)
        except pob.PoBImportError as e:
            entry["error"] = f"parse failed: {e}"; results.append(entry); continue
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"; results.append(entry); continue

        names = []
        for it in parsed.items:
            if (it.rarity or "").lower() == "unique" and it.name and it.name in catalog_names:
                names.append(it.name)
        entry["ok"] = True
        entry["class"] = parsed.class_name
        entry["ascendancy"] = parsed.ascendancy
        entry["unique_names"] = sorted(set(names))
        for n in set(names):
            unique_counts[n] = unique_counts.get(n, 0) + 1
        results.append(entry)

    # Sorted by popularity (most-used first)
    aggregated = [{"name": n, "count": c}
                  for n, c in sorted(unique_counts.items(), key=lambda x: (-x[1], x[0]))]

    return {
        "ok": True,
        "parsed_count": sum(1 for r in results if r["ok"]),
        "skipped_count": sum(1 for r in results if not r["ok"]),
        "per_input": results,
        "aggregated_uniques": aggregated,
    }


@router.post("/api/profit/build-filter")
async def build_profit_filter(request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    s = store.load()
    src, dest = _profit_paths(s)
    names = s.profit_uniques or list(profit.SEED_UNIQUE_NAMES)

    # Pull the CURRENT BUILD's equipped uniques (jbaker's active build)
    build_names: list[str] = []
    rare_items: list[dict] = []
    bid = s.active_builds.get(user, "")
    if bid and bid in s.builds:
        b = s.builds[bid]
        enabled_slots = set(s.profit_rare_slots.get(bid, []))
        for slot, it in b.items.items():
            rarity = (it.rarity or "").lower()
            if rarity == "unique" and it.name:
                build_names.append(it.name)
            elif rarity == "rare" and slot in enabled_slots and it.base:
                # Pull must-mods from user prefs
                from app import load_user  # type: ignore
                prefs = (load_user(user) or {}).get("state", {})
                ranks = (prefs.get("rankings") or {}).get(slot, {})
                must_mods = [
                    m["text"] for m in it.mods
                    if ranks.get(pob.Mod(text=m["text"], kind=m["kind"]).hash()) == "must"
                ]
                rare_items.append({
                    "slot": slot, "base": it.base, "must_mods": must_mods,
                })

    try:
        info = profit.inject_into_filter(names, src, dest,
                                         build_names=build_names,
                                         rare_items=rare_items)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Build failed ({type(e).__name__}): {e}"}, status_code=500)

    # Validate what we just wrote
    report = filter_validate.validate_filter_file(dest)
    text = dest.read_text(encoding="utf-8")
    hierarchy = filter_validate.analyze_hierarchy(text)

    return {
        "ok": True,
        **info,
        "validation": {
            "total_lines": report.total_lines,
            "blocks": report.blocks,
            "show_blocks": report.show_blocks,
            "hide_blocks": report.hide_blocks,
            "errors": len(report.errors),
            "warnings_unknown_keyword": sum(1 for w in report.warnings if "Unknown keyword" in w.msg),
            "warnings_other": sum(1 for w in report.warnings if "Unknown keyword" not in w.msg),
            "sample_errors": [
                {"line": e.line_no, "msg": e.msg, "text": e.line_text[:120]}
                for e in report.errors[:8]
            ],
        },
        "hierarchy": {
            "profit_block_line": hierarchy.profit_block_position,
            "rules_before": hierarchy.rules_before_profit,
            "has_continue": hierarchy.profit_block_has_continue,
            "explanation": hierarchy.explanation,
        },
    }


@router.post("/api/profit/validate")
async def validate_profit_filter(request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    s = store.load()
    _src, dest = _profit_paths(s)
    if not dest.exists():
        return JSONResponse({"error": "Profit filter hasn't been built yet."}, status_code=400)
    report = filter_validate.validate_filter_file(dest)
    text = dest.read_text(encoding="utf-8")
    hierarchy = filter_validate.analyze_hierarchy(text)
    return {
        "ok": report.ok(),
        "summary": report.summary(),
        "errors": [{"line": e.line_no, "msg": e.msg, "text": e.line_text[:160]} for e in report.errors[:20]],
        "warnings_unknown_keyword": sum(1 for w in report.warnings if "Unknown keyword" in w.msg),
        "warnings_other": sum(1 for w in report.warnings if "Unknown keyword" not in w.msg),
        "hierarchy": {
            "profit_block_line": hierarchy.profit_block_position,
            "rules_before": hierarchy.rules_before_profit,
            "has_continue": hierarchy.profit_block_has_continue,
            "explanation": hierarchy.explanation,
        },
    }


# --- Leveling build library (scraped from maxroll) -----------------------

@router.get("/api/leveling-builds")
async def list_leveling_builds(request: Request):
    s = store.load()
    builds = list(s.leveling_builds.values())
    # Optionally filter by class/ascendancy for per-build suggestions
    cls = (request.query_params.get("class") or "").strip()
    asc = (request.query_params.get("ascendancy") or "").strip()
    if cls or asc:
        builds = leveling.find_for_class(builds, cls, asc)
    return {"builds": builds, "last_scan": s.leveling_last_scan}


@router.post("/api/leveling-builds/scan")
async def scan_leveling_builds(request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)

    scraped: list[dict] = []
    source_stats: dict[str, int] = {}
    errors: list[str] = []

    for source_name, fn in [
        ("maxroll", leveling.scrape_index),
        ("mobalytics", leveling.scrape_mobalytics),
    ]:
        try:
            got = fn()
            scraped.extend(got)
            source_stats[source_name] = len(got)
        except Exception as e:
            errors.append(f"{source_name}: {e}")
            source_stats[source_name] = 0

    s = store.load()
    # Preserve manual entries (source=manual) — don't touch them here.
    # Upsert scraped entries by slug.
    for g in scraped:
        slug = g["slug"]
        prev = s.leveling_builds.get(slug)
        if prev and prev.get("source") == "manual":
            continue  # never overwrite a manual entry
        if prev:
            g = {**g, "discovered_at": prev.get("discovered_at", g["discovered_at"])}
        s.leveling_builds[slug] = g
    s.leveling_last_scan = scraped[0]["discovered_at"] if scraped else s.leveling_last_scan
    store.save(s)
    return {
        "ok": True,
        "sources": source_stats,
        "scraped_total": len(scraped),
        "leveling_total": sum(1 for g in scraped if g.get("is_leveling")),
        "stored_total": len(s.leveling_builds),
        "errors": errors,
    }


@router.post("/api/leveling-builds")
async def add_leveling_build(request: Request):
    """Manually add a leveling build: accepts pobb.in / pob.cool / poe2db.tw URL
    or raw PoB code. Extracts class/ascendancy from the PoB. Title + optional
    external URL (maxroll guide link, etc.) provided by the user."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    body = await request.json()
    pob_text = (body.get("pob") or "").strip()
    title = (body.get("title") or "").strip()
    guide_url = (body.get("url") or "").strip()
    if not pob_text and not guide_url:
        return JSONResponse(
            {"error": "Provide either a PoB (code/URL) or a guide URL."},
            status_code=400)

    cls = ""
    asc = ""
    if pob_text:
        try:
            parsed, _raw = pob.import_build_with_code(pob_text)
            cls = parsed.class_name
            asc = parsed.ascendancy
        except pob.PoBImportError as e:
            return JSONResponse({"error": f"PoB parse failed: {e}"}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": f"Parse failed ({type(e).__name__}): {e}"}, status_code=400)

    s = store.load()
    # Generate a unique slug — prefer the URL's last path segment if it looks
    # reasonable, else derive from title, else a random one.
    import re, secrets
    def _slugify(t: str) -> str:
        t = re.sub(r"[^a-zA-Z0-9-]+", "-", t.lower()).strip("-")
        return t[:60] or ""
    slug = ""
    if guide_url:
        m = re.search(r"/([a-z0-9][a-z0-9-]{2,80})/?$", guide_url, re.I)
        if m: slug = _slugify(m.group(1))
    if not slug and title:
        slug = _slugify(title)
    if not slug:
        slug = "manual-" + secrets.token_urlsafe(6)
    # Ensure uniqueness
    base_slug = slug
    i = 2
    while slug in s.leveling_builds:
        slug = f"{base_slug}-{i}"; i += 1

    s.leveling_builds[slug] = {
        "title": title or f"{cls}{' / '+asc if asc else ''} — leveling".strip(" —"),
        "slug": slug,
        "url": guide_url or "",
        "class": cls,
        "ascendancy": asc,
        "is_leveling": True,
        "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "manual",
        "pob_code": pob_text,   # keep the raw/URL for one-click re-import later
    }
    store.save(s)
    return {"ok": True, "slug": slug, "entry": s.leveling_builds[slug]}


@router.post("/api/leveling-builds/{slug}/import")
async def import_leveling_library_entry(slug: str, request: Request):
    """Import a library entry's pob_code as a full Build, set active for jbaker.
    Skips entries without a pob_code (library entries scraped from maxroll,
    which don't expose PoB directly)."""
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    s = store.load()
    entry = s.leveling_builds.get(slug)
    if not entry:
        return JSONResponse({"error": "unknown slug"}, status_code=404)
    pob_text = (entry.get("pob_code") or "").strip()
    if not pob_text:
        return JSONResponse({
            "error": "This entry has no PoB code — click its Open ↗ to grab it manually.",
            "url": entry.get("url"),
        }, status_code=400)
    try:
        parsed, raw_code = pob.import_build_with_code(pob_text)
    except pob.PoBImportError as e:
        return JSONResponse({"error": f"PoB parse failed: {e}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)

    stored = store.new_build_from_pob_import(
        parsed,
        label=entry.get("title") or f"{parsed.class_name}/{parsed.ascendancy} — leveling",
        phase="leveling",
        pob_code=raw_code,
        imported_by=user,
    )
    # Point the tree viewer iframe at the guide URL (so the user lands directly
    # on the maxroll/mobalytics guide, not the generic poe.ninja paste page).
    stored.tree_viewer_url = entry.get("url", "") or ""
    s.builds[stored.id] = stored
    s.active_builds[user] = stored.id
    store.save(s)
    return {"ok": True, "build_id": stored.id, "label": stored.label}


@router.delete("/api/leveling-builds/{slug}")
async def remove_leveling_build(slug: str, request: Request):
    user = _require_user(request)
    if user != "jbaker":
        return JSONResponse({"error": "jbaker-only"}, status_code=403)
    s = store.load()
    if slug not in s.leveling_builds:
        return JSONResponse({"error": "not found"}, status_code=404)
    del s.leveling_builds[slug]
    store.save(s)
    return {"ok": True}


# --- Active build toggle --------------------------------------------------

@router.get("/api/active")
async def get_active(request: Request):
    user = _require_user(request)
    s = store.load()
    return {"user": user, "build_id": s.active_builds.get(user, "")}


@router.put("/api/active")
async def set_active(request: Request):
    user = _require_user(request)
    body = await request.json()
    bid = (body.get("build_id") or "").strip()
    s = store.load()
    if bid and bid not in s.builds:
        return JSONResponse({"error": "unknown build_id"}, status_code=400)
    s.active_builds[user] = bid
    store.save(s)
    return {"ok": True, "build_id": bid}
