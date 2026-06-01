"""PoE2 passive tree loader and layout computation.

Reads the raw tree JSON published by Path of Building Community (src/TreeData/0_X/tree.json),
precomputes absolute (x, y) positions for every node from group/orbit/orbitIndex, and returns
a client-friendly shape with deduplicated connections.
"""

from __future__ import annotations

import functools
import json
import math
from pathlib import Path

TREE_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "tree"
DEFAULT_TREE = TREE_DATA_DIR / "tree_0_4.json"


@functools.lru_cache(maxsize=4)
def load_raw(tree_path: str = str(DEFAULT_TREE)) -> dict:
    with open(tree_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _classify(node: dict) -> str:
    if node.get("isKeystone"):
        return "keystone"
    if node.get("isMastery"):
        return "mastery"
    if node.get("isJewelSocket"):
        return "jewel"
    if node.get("isNotable"):
        return "notable"
    if node.get("classStartIndex") is not None:
        return "class_start"
    return "small"


def _node_pos(node: dict, groups: list, constants: dict) -> tuple[float, float]:
    gid = node.get("group")
    if gid is None or not isinstance(gid, int) or gid >= len(groups):
        return 0.0, 0.0
    g = groups[gid] or {}
    if not isinstance(g, dict):
        return 0.0, 0.0
    gx, gy = g.get("x", 0), g.get("y", 0)
    orbit = node.get("orbit", 0) or 0
    oi = node.get("orbitIndex", 0) or 0
    radii = constants.get("orbitRadii") or []
    angles_by_orbit = constants.get("orbitAnglesByOrbit") or []
    r = radii[orbit] if orbit < len(radii) else 0
    if orbit < len(angles_by_orbit) and oi < len(angles_by_orbit[orbit]):
        theta = angles_by_orbit[orbit][oi]
    else:
        per = constants.get("skillsPerOrbit", [])
        n = per[orbit] if orbit < len(per) else 1
        theta = (2 * math.pi * oi / n) if n else 0
    x = gx + r * math.sin(theta)
    y = gy - r * math.cos(theta)
    return x, y


def _arc_path(gx: float, gy: float, r: float,
              a1: float, a2: float) -> str:
    """Build an SVG path for an orbit arc from angle a1 to a2 (same group, same radius)."""
    # Points on the orbit circle using PoB's up-is-zero convention
    x1 = gx + r * math.sin(a1)
    y1 = gy - r * math.cos(a1)
    x2 = gx + r * math.sin(a2)
    y2 = gy - r * math.cos(a2)
    # Pick short-arc direction
    delta = (a2 - a1) % (2 * math.pi)
    sweep = 1 if delta <= math.pi else 0
    large = 0  # always short arc since we pick sweep accordingly
    return f"M{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 {large} {sweep} {x2:.1f},{y2:.1f}"


def _line_path(x1: float, y1: float, x2: float, y2: float) -> str:
    return f"M{x1:.1f},{y1:.1f} L{x2:.1f},{y2:.1f}"


@functools.lru_cache(maxsize=4)
def load_layout(tree_path: str = str(DEFAULT_TREE)) -> dict:
    """Return a client-optimized tree: nodes with (x, y) + kind + stats, edges as SVG paths."""
    raw = load_raw(tree_path)
    groups = raw.get("groups") or []
    constants = raw.get("constants") or {}
    radii = constants.get("orbitRadii") or []
    angles_by_orbit = constants.get("orbitAnglesByOrbit") or []

    # Include all passive nodes (including ascendancy — they are part of the
    # allocated path and need to render as connected).
    node_items = [(int(k), v) for k, v in raw["nodes"].items() if k.isdigit()]

    nodes_out: dict[int, dict] = {}
    node_meta: dict[int, dict] = {}  # raw node for edge computation
    for nid, n in node_items:
        x, y = _node_pos(n, groups, constants)
        nodes_out[nid] = {
            "id": nid,
            "x": x,
            "y": y,
            "name": n.get("name", ""),
            "stats": list(n.get("stats", []) or []),
            "kind": _classify(n),
        }
        node_meta[nid] = n

    # Build edges, dedup by sorted (a, b). Keep all declared connections —
    # in PoE2 the cross-tree "Attribute travel" edges and class-start edges
    # are real traversals, not ghosts. Only reject obvious sentinels (INT_MAX
    # orbit values that PoB uses to mark non-edges).
    seen: set[tuple[int, int]] = set()
    edges: list[dict] = []
    for nid, n in node_items:
        for c in (n.get("connections") or []):
            try:
                cid = int(c.get("id")) if isinstance(c, dict) else int(c)
                c_orbit = int(c.get("orbit", 0)) if isinstance(c, dict) else 0
            except (TypeError, ValueError):
                continue
            if c_orbit > 100 or c_orbit < 0:
                continue
            if cid not in nodes_out:
                continue
            a, b = (nid, cid) if nid < cid else (cid, nid)
            if (a, b) in seen:
                continue
            seen.add((a, b))
            na, nb = node_meta[a], node_meta[b]
            same_group = (na.get("group") == nb.get("group") and na.get("group") is not None)
            same_orbit = na.get("orbit") == nb.get("orbit")
            if same_group and same_orbit and (na.get("orbit") or 0) > 0:
                gid = na.get("group")
                g = groups[gid] if (gid is not None and 0 <= gid < len(groups)) else None
                if not isinstance(g, dict):
                    pa = nodes_out[a]; pb = nodes_out[b]
                    edges.append({"a": a, "b": b, "d": _line_path(pa["x"], pa["y"], pb["x"], pb["y"])})
                    continue
                gx, gy = g.get("x", 0), g.get("y", 0)
                orbit = na.get("orbit", 0)
                r = radii[orbit] if orbit < len(radii) else 0
                oi_a = na.get("orbitIndex", 0) or 0
                oi_b = nb.get("orbitIndex", 0) or 0
                try:
                    ang_a = angles_by_orbit[orbit][oi_a]
                    ang_b = angles_by_orbit[orbit][oi_b]
                except (IndexError, TypeError):
                    ang_a = ang_b = 0
                d = _arc_path(gx, gy, r, ang_a, ang_b)
            else:
                pa = nodes_out[a]; pb = nodes_out[b]
                d = _line_path(pa["x"], pa["y"], pb["x"], pb["y"])
            edges.append({"a": a, "b": b, "d": d})

    # Group backgrounds: for each group that has >1 non-ascendancy node, emit
    # a circle centered on group (x, y) with radius = max-used-orbit-radius.
    # Gives the tree visible cluster structure without needing PoB's DDS art.
    group_nodes: dict[int, list[tuple[int, int]]] = {}
    for nid, n in node_items:
        gid = n.get("group")
        if gid is None:
            continue
        group_nodes.setdefault(gid, []).append((nid, n.get("orbit", 0) or 0))

    group_bgs: list[dict] = []
    for gid, entries in group_nodes.items():
        if len(entries) < 2:
            continue
        if not (0 <= gid < len(groups)):
            continue
        g = groups[gid]
        if not isinstance(g, dict):
            continue
        max_orbit = max(o for _, o in entries)
        if max_orbit <= 0:
            continue
        r = radii[max_orbit] if max_orbit < len(radii) else 0
        if r <= 0:
            continue
        group_bgs.append({
            "x": g.get("x", 0),
            "y": g.get("y", 0),
            "r": r + 40,  # slight padding around outermost orbit
        })

    xs = [n["x"] for n in nodes_out.values()]
    ys = [n["y"] for n in nodes_out.values()]
    bounds = {
        "min_x": min(xs) if xs else 0,
        "max_x": max(xs) if xs else 0,
        "min_y": min(ys) if ys else 0,
        "max_y": max(ys) if ys else 0,
    }
    # Class-start nodes (one per class) for smart initial framing.
    class_starts = [nid for nid, n in node_items if n.get("classStartIndex") is not None]
    return {
        "version": tree_path.rsplit("/", 1)[-1],
        "bounds": bounds,
        "nodes": list(nodes_out.values()),
        "edges": edges,
        "group_bgs": group_bgs,
        "class_starts": class_starts,
    }
