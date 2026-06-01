#!/usr/bin/env python3
"""
PoE2 Filter Updater — Injects build-specific rules into your NeverSink filter.

Downloads filter rules from the PoE2 Companion app and injects them into the
Override Area of your loot filter. Existing injected rules are replaced on each run.

Usage:
    python update_filter.py [filter_path] [--server URL]

Examples:
    python update_filter.py
    python update_filter.py ~/Documents/my_filter.filter
    python update_filter.py --server http://192.168.1.50:8889
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_SERVER = "http://localhost:8889"

# Where PoE2 stores filters (Windows default — adjust for your OS)
DEFAULT_FILTER_PATHS = [
    Path.home() / "Documents" / "My Games" / "Path of Exile 2" / "NeverSink.filter",
    Path.home() / "Documents" / "My Games" / "Path of Exile" / "NeverSink.filter",
]

# Markers to identify our injected section
MARKER_START = "#>>> POE2-COMPANION FILTER RULES - DO NOT EDIT BETWEEN MARKERS <<<"
MARKER_END = "#>>> END POE2-COMPANION FILTER RULES <<<"


def find_filter() -> Path | None:
    """Find the filter file in default locations."""
    for p in DEFAULT_FILTER_PATHS:
        if p.exists():
            return p
        # Also check for any .filter file in the directory
        if p.parent.exists():
            filters = list(p.parent.glob("*.filter"))
            if filters:
                return filters[0]
    return None


def fetch_rules(server: str, cookie: str = "") -> dict:
    """Fetch filter rules from the companion app."""
    url = f"{server}/api/filter/export"
    headers = {"Accept": "application/json"}
    if cookie:
        headers["Cookie"] = f"poe2_token={cookie}"

    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except URLError as e:
        print(f"Error connecting to {server}: {e}")
        print("Make sure the PoE2 Companion app is running and you're logged in.")
        sys.exit(1)


def login(server: str, username: str, password: str) -> str:
    """Login and return the session cookie."""
    url = f"{server}/api/login"
    data = json.dumps({"username": username, "password": password}).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            # Extract cookie from response headers
            cookies = resp.headers.get_all("Set-Cookie") or []
            for c in cookies:
                if "poe2_token=" in c:
                    return c.split("poe2_token=")[1].split(";")[0]
    except URLError as e:
        print(f"Login failed: {e}")
        sys.exit(1)
    print("Login failed — bad credentials?")
    sys.exit(1)


def rules_to_filter_text(rules_data: dict) -> str:
    """Convert JSON rules to PoE2 filter syntax."""
    lines = [
        MARKER_START,
        f"# Generated: {rules_data.get('generated', 'unknown')}",
        f"# Builds: {', '.join(rules_data.get('builds', []))}",
        f"# Rules: {len(rules_data.get('rules', []))}",
        "",
    ]

    for rule in rules_data.get("rules", []):
        comment = rule.get("comment", "")
        action = rule.get("action", "Show")
        conditions = rule.get("conditions", {})
        style = rule.get("style", {})

        if comment:
            lines.append(f"# {comment}")
        lines.append(action)

        # Write conditions
        for key, val in conditions.items():
            if isinstance(val, list):
                # Quote each value: BaseType == "item1" "item2"
                quoted = " ".join(f'"{v}"' for v in val)
                lines.append(f"\t{key} == {quoted}")
            else:
                lines.append(f"\t{key} {val}")

        # Write style
        for key, val in style.items():
            lines.append(f"\t{key} {val}")

        lines.append("")

    lines.append(MARKER_END)
    lines.append("")
    return "\n".join(lines)


def inject_rules(filter_path: Path, rules_text: str) -> bool:
    """Inject rules into the filter file's Override Area."""
    content = filter_path.read_text(encoding="utf-8")

    # Remove existing injected section if present
    if MARKER_START in content:
        before = content[: content.index(MARKER_START)]
        after_marker = content[content.index(MARKER_END) + len(MARKER_END) :]
        content = before + after_marker.lstrip("\n")

    # Find the Override Area insertion point
    # NeverSink's filter has: # [[0100]] OVERRIDE AREA 1 - Override ALL rules here
    # We inject right after the Gold section header but before the first Show rule
    override_marker = "OVERRIDE AREA"
    if override_marker in content:
        idx = content.index(override_marker)
        # Find the next blank line after the override marker line
        next_newline = content.index("\n", idx)
        # Insert our rules after the override comment block
        # Find the next Show/Hide after the override area marker
        rest = content[next_newline:]
        # Skip comment lines to find first rule
        insert_pos = next_newline
        for i, line in enumerate(rest.split("\n")):
            if line.strip().startswith("Show") or line.strip().startswith("Hide"):
                insert_pos = next_newline + sum(len(l) + 1 for l in rest.split("\n")[:i])
                break

        content = content[:insert_pos] + "\n" + rules_text + "\n" + content[insert_pos:]
    else:
        # No override area found — prepend to file (after header comments)
        # Find first Show/Hide
        for keyword in ["Show", "Hide"]:
            if keyword in content:
                idx = content.index(keyword)
                content = content[:idx] + rules_text + "\n" + content[idx:]
                break
        else:
            content = rules_text + "\n" + content

    # Backup original
    backup = filter_path.with_suffix(".filter.bak")
    shutil.copy2(filter_path, backup)

    filter_path.write_text(content, encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Update your PoE2 loot filter with build-specific highlight rules."
    )
    parser.add_argument("filter_path", nargs="?", help="Path to your .filter file")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"Companion app URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--user", default="", help="Username for auto-login")
    parser.add_argument("--password", default="", help="Password for auto-login")
    args = parser.parse_args()

    # Find filter file
    if args.filter_path:
        filter_path = Path(args.filter_path)
    else:
        filter_path = find_filter()

    if not filter_path or not filter_path.exists():
        print("Could not find your .filter file.")
        print("Specify the path: python update_filter.py /path/to/your.filter")
        sys.exit(1)

    print(f"Filter: {filter_path}")
    print(f"Server: {args.server}")

    # Login if credentials provided
    cookie = ""
    if args.user and args.password:
        print(f"Logging in as {args.user}...")
        cookie = login(args.server, args.user, args.password)

    # Fetch rules
    print("Fetching filter rules from companion app...")
    rules_data = fetch_rules(args.server, cookie)

    rules = rules_data.get("rules", [])
    if not rules:
        print("No filter rules to inject. Add some builds with items in the companion app first.")
        sys.exit(0)

    builds = rules_data.get("builds", [])
    print(f"Got {len(rules)} rules from {len(builds)} builds:")
    for r in rules:
        print(f"  - {r.get('comment', '?')}")

    # Convert to filter text
    rules_text = rules_to_filter_text(rules_data)

    # Inject into filter
    print("Injecting rules into filter...")
    inject_rules(filter_path, rules_text)

    backup = filter_path.with_suffix(".filter.bak")
    print(f"Done! Backup saved to {backup}")
    print("Reload your filter in-game (Options → UI → reselect from dropdown).")


if __name__ == "__main__":
    main()
