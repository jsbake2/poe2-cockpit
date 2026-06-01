"""YAML hotkey config parsing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from evdev import ecodes as e

KEY_ALIASES: dict[str, int] = {
    "enter": e.KEY_ENTER, "return": e.KEY_ENTER,
    "esc": e.KEY_ESC, "escape": e.KEY_ESC,
    "tab": e.KEY_TAB, "space": e.KEY_SPACE,
    "backspace": e.KEY_BACKSPACE, "delete": e.KEY_DELETE, "del": e.KEY_DELETE,
    "up": e.KEY_UP, "down": e.KEY_DOWN, "left": e.KEY_LEFT, "right": e.KEY_RIGHT,
    "home": e.KEY_HOME, "end": e.KEY_END,
    "pageup": e.KEY_PAGEUP, "pagedown": e.KEY_PAGEDOWN,
    "insert": e.KEY_INSERT,
}
for _i in range(1, 13):
    KEY_ALIASES[f"f{_i}"] = getattr(e, f"KEY_F{_i}")
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEY_ALIASES[_c] = getattr(e, f"KEY_{_c.upper()}")
for _c in "0123456789":
    KEY_ALIASES[_c] = getattr(e, f"KEY_{_c}")

CHAR_MAP: dict[str, tuple[int, bool]] = {}
for _c in "abcdefghijklmnopqrstuvwxyz":
    CHAR_MAP[_c] = (getattr(e, f"KEY_{_c.upper()}"), False)
    CHAR_MAP[_c.upper()] = (getattr(e, f"KEY_{_c.upper()}"), True)
for _c in "0123456789":
    CHAR_MAP[_c] = (getattr(e, f"KEY_{_c}"), False)
CHAR_MAP.update({
    " ":  (e.KEY_SPACE, False),
    "/":  (e.KEY_SLASH, False),   "?": (e.KEY_SLASH, True),
    "-":  (e.KEY_MINUS, False),   "_": (e.KEY_MINUS, True),
    ".":  (e.KEY_DOT, False),     ">": (e.KEY_DOT, True),
    ",":  (e.KEY_COMMA, False),   "<": (e.KEY_COMMA, True),
    ";":  (e.KEY_SEMICOLON, False), ":": (e.KEY_SEMICOLON, True),
    "'":  (e.KEY_APOSTROPHE, False), '"': (e.KEY_APOSTROPHE, True),
    "[":  (e.KEY_LEFTBRACE, False), "{": (e.KEY_LEFTBRACE, True),
    "]":  (e.KEY_RIGHTBRACE, False), "}": (e.KEY_RIGHTBRACE, True),
    "\\": (e.KEY_BACKSLASH, False), "|": (e.KEY_BACKSLASH, True),
    "`":  (e.KEY_GRAVE, False),   "~": (e.KEY_GRAVE, True),
    "=":  (e.KEY_EQUAL, False),   "+": (e.KEY_EQUAL, True),
    "!":  (e.KEY_1, True), "@": (e.KEY_2, True), "#": (e.KEY_3, True),
    "$":  (e.KEY_4, True), "%": (e.KEY_5, True), "^": (e.KEY_6, True),
    "&":  (e.KEY_7, True), "*": (e.KEY_8, True), "(": (e.KEY_9, True),
    ")":  (e.KEY_0, True),
})

MOD_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "meta": "meta", "super": "meta", "win": "meta",
}

MOD_CODES = {
    "ctrl":  e.KEY_LEFTCTRL,
    "alt":   e.KEY_LEFTALT,
    "shift": e.KEY_LEFTSHIFT,
    "meta":  e.KEY_LEFTMETA,
}


@dataclass(frozen=True)
class Trigger:
    mods: frozenset[str]
    key: int


@dataclass
class Step:
    kind: str            # "key" | "type" | "combo" | "sleep"
    code: int | None = None
    text: str | None = None
    mods: frozenset[str] | None = None
    sleep_s: float | None = None


@dataclass
class HotkeyCfg:
    name: str
    trigger: Trigger
    steps: list[Step]
    delay_before_s: float = 0.0
    delay_after_s: float = 0.0
    repeat_interval_s: float | None = None
    toggle_mode: bool = False
    focus_required: str | None = None


@dataclass
class Config:
    hotkeys: list[HotkeyCfg]
    focus_required: str | None = None


_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s)?\s*$", re.IGNORECASE)


def parse_duration(v, default: float = 0.0) -> float:
    """Parse '100ms' / '1.5s' / plain number (ms) -> seconds."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v) / 1000.0
    m = _DUR_RE.match(str(v))
    if not m:
        raise ValueError(f"bad duration: {v!r}")
    n = float(m.group(1))
    unit = (m.group(2) or "ms").lower()
    return n / 1000.0 if unit == "ms" else n


def parse_trigger(s: str) -> Trigger:
    parts = [p.strip() for p in s.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty trigger: {s!r}")
    mods: set[str] = set()
    for p in parts[:-1]:
        canon = MOD_ALIASES.get(p.lower())
        if not canon:
            raise ValueError(f"unknown modifier {p!r} in {s!r}")
        mods.add(canon)
    last = parts[-1].lower()
    if last not in KEY_ALIASES:
        raise ValueError(f"unknown key {last!r} in {s!r}")
    return Trigger(mods=frozenset(mods), key=KEY_ALIASES[last])


_SLEEP_RE = re.compile(r"^\s*sleep\s+(.+?)\s*$", re.IGNORECASE)


def resolve_step(item: str) -> Step:
    """Resolve a keypress list item to a Step.

    - 'sleep 200ms' / 'sleep 1.5s' -> sleep step
    - 'Ctrl+q' / 'Shift+Alt+F1'    -> combo step
    - 'Enter' / 'q' (known name)   -> single-key step
    - anything else                -> type the literal string
    """
    m = _SLEEP_RE.match(item)
    if m:
        dur = parse_duration(m.group(1))
        if dur <= 0:
            raise ValueError(f"sleep duration must be positive: {item!r}")
        return Step(kind="sleep", sleep_s=dur)

    if item.lower() in KEY_ALIASES:
        return Step(kind="key", code=KEY_ALIASES[item.lower()])

    if "+" in item:
        parts = [p.strip() for p in item.split("+") if p.strip()]
        if len(parts) >= 2:
            mods = [MOD_ALIASES.get(p.lower()) for p in parts[:-1]]
            if all(mods):
                last = parts[-1].lower()
                if last not in KEY_ALIASES:
                    raise ValueError(f"combo {item!r}: {last!r} is not a known key")
                return Step(kind="combo", mods=frozenset(mods), code=KEY_ALIASES[last])

    for c in item:
        if c not in CHAR_MAP:
            raise ValueError(f"keypress {item!r}: character {c!r} not typeable")
    return Step(kind="type", text=item)


_TOP_LEVEL_KEYS = {"focus_required"}


def load(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")

    top_focus = raw.get("focus_required")
    if top_focus is not None and not isinstance(top_focus, str):
        raise ValueError("focus_required must be a string")

    out: list[HotkeyCfg] = []
    for name, cfg in raw.items():
        if name in _TOP_LEVEL_KEYS:
            continue
        if not isinstance(cfg, dict):
            raise ValueError(f"{name}: config must be a mapping")
        seq = cfg.get("sequence") or cfg.get("sequenct")
        if not seq:
            raise ValueError(f"{name}: missing 'sequence'")
        steps = [resolve_step(str(k)) for k in cfg.get("keypress", [])]
        repeat_raw = cfg.get("repeatTimer")
        repeat_s = parse_duration(repeat_raw) if repeat_raw else None
        toggle = bool(cfg.get("toggleMode", False))
        if repeat_s is not None:
            toggle = True

        # per-hotkey focus_required: string overrides top-level; False disables
        fr_raw = cfg.get("focus_required", ...)
        if fr_raw is ...:
            focus_required = top_focus
        elif fr_raw is False or fr_raw is None:
            focus_required = None
        elif isinstance(fr_raw, str):
            focus_required = fr_raw
        else:
            raise ValueError(f"{name}: focus_required must be a string or false")

        out.append(HotkeyCfg(
            name=str(name),
            trigger=parse_trigger(str(seq)),
            steps=steps,
            delay_before_s=parse_duration(cfg.get("delayBefore")),
            delay_after_s=parse_duration(cfg.get("delayAfter")),
            repeat_interval_s=repeat_s,
            toggle_mode=toggle,
            focus_required=focus_required,
        ))
    return Config(hotkeys=out, focus_required=top_focus)
