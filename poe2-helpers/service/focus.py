"""Active-window detection via xdotool getwindowfocus.

COSMIC doesn't update _NET_ACTIVE_WINDOW for XWayland clients, so
`xdotool getactivewindow` is useless here. But `xdotool getwindowfocus`
queries X11's XGetInputFocus directly and correctly reflects focus when
an XWayland client (e.g. PoE2 via Proton) is focused. When a native
Wayland window is focused, the command errors and we treat focus as
'(none)'.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

log = logging.getLogger(__name__)


async def active_window_title() -> str:
    """Title of the XWayland-focused window, or '' if none / not an X11 window."""
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["xdotool", "getwindowfocus", "getwindowname"],
            capture_output=True, timeout=1, text=True,
        )
        if r.returncode != 0:
            return ""
        return (r.stdout or "").strip()
    except Exception as ex:
        log.debug("xdotool failed: %s", ex)
        return ""


def matches_required(title: str, required: str | None) -> bool:
    """Case-insensitive substring match. Empty/None required -> always True."""
    if not required:
        return True
    return required.lower() in (title or "").lower()
