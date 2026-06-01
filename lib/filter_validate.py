"""Lightweight PoE2 loot-filter syntax validator.

Not authoritative — PoE itself is the source of truth for acceptance, and the
filter grammar isn't fully public. But we catch the common mistakes that break
a filter silently: invalid Show/Hide blocks, bad color values, out-of-range
numbers, and unbalanced quotes.

Also tracks whether a rule block has `Continue` (which affects first-match
semantics — see lint_hierarchy below).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# Keywords we know about (non-exhaustive; unknowns are warned, not errored).
CONDITION_KEYWORDS = {
    "BaseType", "Class", "Rarity", "ItemLevel", "DropLevel", "Quality",
    "Sockets", "LinkedSockets", "Width", "Height", "AreaLevel", "MapTier",
    "StackSize", "GemLevel", "GemQualityType", "Identified", "Corrupted",
    "Mirrored", "ElderItem", "ShaperItem", "Influenced", "FracturedItem",
    "SynthesisedItem", "EnchantmentPassiveNum", "EnchantmentPassiveNode",
    "HasExplicitMod", "HasImplicitMod", "HasEnchantment", "HasInfluence",
    "AlternateQuality", "Scourged", "HasSearingExarchImplicit",
    "HasEaterOfWorldsImplicit", "ArchnemesisMod", "UberBlightedMap",
    "BlightedMap", "ElderMap", "ShapedMap", "Replica", "Transfigured",
    "Sanctum", "HasTrialRelic", "HasSocketable",
}
ACTION_KEYWORDS = {
    "SetFontSize", "SetTextColor", "SetBorderColor", "SetBackgroundColor",
    "PlayAlertSound", "PlayAlertSoundPositional", "CustomAlertSound",
    "CustomAlertSoundOptional", "DisableDropSound", "EnableDropSound",
    "DisableDropSoundIfAlertSound", "EnableDropSoundIfAlertSound",
    "PlayEffect", "MinimapIcon", "Continue",
}
BLOCK_KEYWORDS = {"Show", "Hide", "Minimal"}


@dataclass
class Issue:
    line_no: int
    severity: str  # "error" | "warning"
    msg: str
    line_text: str = ""


@dataclass
class ValidationReport:
    filename: str = ""
    total_lines: int = 0
    blocks: int = 0
    show_blocks: int = 0
    hide_blocks: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{self.filename}: {self.total_lines} lines, {self.blocks} blocks "
            f"({self.show_blocks} Show / {self.hide_blocks} Hide), "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings."
        )


_INT_RE = re.compile(r"^-?\d+$")


def _parse_ints(tokens: list[str]) -> list[int] | None:
    vals = []
    for t in tokens:
        if not _INT_RE.match(t):
            return None
        vals.append(int(t))
    return vals


def _split_tokens(content: str) -> list[str]:
    """Tokenize a line, respecting quoted strings. An unquoted `#` starts an
    inline comment and is stripped from the token stream."""
    tokens: list[str] = []
    cur = ""
    in_q = False
    i = 0
    while i < len(content):
        ch = content[i]
        if ch == '"':
            in_q = not in_q
            cur += ch
        elif ch == "#" and not in_q:
            # rest of line is a comment
            break
        elif ch.isspace() and not in_q:
            if cur:
                tokens.append(cur); cur = ""
        else:
            cur += ch
        i += 1
    if cur:
        tokens.append(cur)
    return tokens


def validate_filter_text(text: str, filename: str = "") -> ValidationReport:
    r = ValidationReport(filename=filename)
    lines = text.splitlines()
    r.total_lines = len(lines)

    in_block = False
    current_block_start = 0
    for i, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Check for unbalanced quotes on this line
        if stripped.count('"') % 2:
            r.issues.append(Issue(i, "error", "Unbalanced double-quote", stripped))
            continue
        tokens = _split_tokens(stripped)
        if not tokens:
            continue
        head = tokens[0]
        args = tokens[1:]

        if head in BLOCK_KEYWORDS:
            in_block = True
            current_block_start = i
            r.blocks += 1
            if head == "Show":
                r.show_blocks += 1
            elif head == "Hide":
                r.hide_blocks += 1
            if args:
                r.issues.append(Issue(i, "warning", f"{head} has trailing tokens: {args}", stripped))
            continue

        if not in_block:
            r.issues.append(Issue(i, "error", f"Statement outside Show/Hide: {head}", stripped))
            continue

        # Validate action args for the ones we care about
        if head == "SetFontSize":
            ints = _parse_ints(args)
            if not ints or len(ints) != 1 or not (18 <= ints[0] <= 45):
                r.issues.append(Issue(i, "error", "SetFontSize needs 1 integer 18-45", stripped))
        elif head in ("SetTextColor", "SetBorderColor", "SetBackgroundColor"):
            ints = _parse_ints(args)
            if not ints or len(ints) not in (3, 4) or any(not (0 <= v <= 255) for v in ints):
                r.issues.append(Issue(i, "error",
                    f"{head} needs 3-4 ints 0-255 (R G B [A])", stripped))
        elif head == "PlayAlertSound":
            # Format: PlayAlertSound <id 1-16> [volume 0-300]
            if not (1 <= len(args) <= 2):
                r.issues.append(Issue(i, "error", "PlayAlertSound needs 1-2 args", stripped))
            else:
                ints = _parse_ints(args)
                if not ints:
                    r.issues.append(Issue(i, "error", "PlayAlertSound args must be integers", stripped))
                elif not (1 <= ints[0] <= 16):
                    r.issues.append(Issue(i, "error",
                        f"PlayAlertSound id must be 1-16 (got {ints[0]})", stripped))
                elif len(ints) == 2 and not (0 <= ints[1] <= 300):
                    r.issues.append(Issue(i, "error",
                        f"PlayAlertSound volume must be 0-300 (got {ints[1]})", stripped))
        elif head == "PlayEffect":
            valid_colors = {"Red","Green","Blue","Brown","White","Yellow","Cyan","Grey","Orange","Pink","Purple","Temp"}
            if not args or args[0].rstrip() not in valid_colors:
                r.issues.append(Issue(i, "error",
                    f"PlayEffect needs a color from {sorted(valid_colors)}", stripped))
        elif head == "MinimapIcon":
            # size color shape ; size 0=large, 1=medium, 2=small
            if len(args) != 3:
                r.issues.append(Issue(i, "error", "MinimapIcon needs 3 args: size color shape", stripped))
            else:
                try:
                    sz = int(args[0])
                    if sz not in (0, 1, 2):
                        r.issues.append(Issue(i, "error", "MinimapIcon size must be 0/1/2", stripped))
                except ValueError:
                    r.issues.append(Issue(i, "error", "MinimapIcon size must be integer", stripped))
        elif head in CONDITION_KEYWORDS or head in ACTION_KEYWORDS:
            pass  # known keyword; no deeper validation in this pass
        else:
            r.issues.append(Issue(i, "warning", f"Unknown keyword: {head}", stripped))
    return r


def validate_filter_file(path: Path) -> ValidationReport:
    text = path.read_text(encoding="utf-8")
    return validate_filter_text(text, filename=str(path))


# ---------- hierarchy / first-match analysis ----------

@dataclass
class HierarchyReport:
    profit_block_position: int = -1   # line of our BEGIN marker in the file (-1 if not found)
    rules_before_profit: int = 0       # count of Show/Hide rules ABOVE our block
    profit_block_has_continue: bool = False  # if True, items continue past our rule
    explanation: str = ""


PROFIT_BEGIN = "# BEGIN poe2-companion profit-highlights"
PROFIT_END = "# END poe2-companion profit-highlights"


def analyze_hierarchy(text: str) -> HierarchyReport:
    lines = text.splitlines()
    begin_i = end_i = -1
    for i, ln in enumerate(lines):
        if ln.strip() == PROFIT_BEGIN and begin_i == -1:
            begin_i = i
        elif ln.strip() == PROFIT_END:
            end_i = i
    rep = HierarchyReport(profit_block_position=begin_i + 1 if begin_i >= 0 else -1)
    if begin_i < 0:
        rep.explanation = "No profit block found in file."
        return rep

    # Count Show/Hide blocks before our profit block
    for ln in lines[:begin_i]:
        h = ln.strip().split(" ", 1)[0] if ln.strip() else ""
        if h in ("Show", "Hide"):
            rep.rules_before_profit += 1

    # Check if our block has a Continue (which means items also evaluate below)
    block_slice = "\n".join(lines[begin_i:(end_i + 1) if end_i >= 0 else len(lines)])
    rep.profit_block_has_continue = "\nContinue" in block_slice or "\n\tContinue" in block_slice

    # Generate human-readable explanation
    if rep.rules_before_profit == 0:
        rep.explanation = (
            "Profit block is FIRST in the file. PoE filters use first-match "
            "semantics, so any unique matching our BaseType list gets our "
            "highlight, overriding anything below."
        )
    else:
        rep.explanation = (
            f"{rep.rules_before_profit} Show/Hide rules appear BEFORE the profit "
            "block. If any of those match the same unique, they win instead. "
            "Move the profit block above them to ensure it takes precedence."
        )
    if rep.profit_block_has_continue:
        rep.explanation += (
            " (Profit block has `Continue` — items also evaluate subsequent "
            "rules; that's unusual for a highlight-only filter.)"
        )
    return rep
