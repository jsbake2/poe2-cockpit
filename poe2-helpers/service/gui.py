"""Tkinter status window: shows active-window, event log, and a BIG banner when a hotkey fires in the wrong window."""
from __future__ import annotations

import queue
import time
import tkinter as tk
from tkinter import font as tkfont

BG = "#14171b"
BG_PANEL = "#0f1114"
FG = "#d0d4d8"
DIM = "#7a7f86"
WARN = "#ff5a5a"
OK = "#6ac46a"

BANNER_SECONDS = 4.0
MAX_LOG_LINES = 300


class StatusGUI:
    def __init__(self, evq: "queue.Queue[dict]") -> None:
        self.q = evq
        self.root = tk.Tk()
        self.root.title("poe2-helpers")
        self.root.geometry("560x420")
        self.root.configure(bg=BG)
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass

        big = tkfont.Font(family="sans", size=15, weight="bold")
        med = tkfont.Font(family="sans", size=11)
        mono = tkfont.Font(family="monospace", size=10)

        hdr = tk.Frame(self.root, bg=BG)
        hdr.pack(fill="x", padx=12, pady=(12, 6))
        tk.Label(hdr, text="Active window", font=med, bg=BG, fg=DIM).pack(anchor="w")
        self.focus_label = tk.Label(
            hdr, text="(unknown)", font=mono, bg=BG, fg=FG,
            anchor="w", justify="left", wraplength=520,
        )
        self.focus_label.pack(anchor="w", fill="x")

        self.banner = tk.Label(
            self.root, text="", font=big, bg=BG, fg=WARN,
            pady=12, anchor="center",
        )
        self.banner.pack(fill="x", padx=12)

        tk.Label(self.root, text="Events", font=med, bg=BG, fg=DIM,
                 anchor="w").pack(anchor="w", padx=12, pady=(8, 2))
        self.log = tk.Text(
            self.root, bg=BG_PANEL, fg=FG, font=mono,
            height=14, relief="flat", borderwidth=0,
            padx=10, pady=8, state="disabled", wrap="word",
            insertbackground=FG,
        )
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("dim", foreground=DIM)
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._banner_clear_at = 0.0
        self.root.after(100, self._poll)

    def _append(self, line: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        if tag:
            self.log.insert("end", line + "\n", tag)
        else:
            self.log.insert("end", line + "\n")
        self.log.see("end")
        total = int(self.log.index("end-1c").split(".")[0])
        if total > MAX_LOG_LINES:
            self.log.delete("1.0", f"{total - MAX_LOG_LINES}.0")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self._banner_clear_at and time.time() > self._banner_clear_at:
            self.banner.config(text="")
            self._banner_clear_at = 0.0
        self.root.after(100, self._poll)

    def _handle(self, evt: dict) -> None:
        kind = evt.get("kind", "")
        ts = time.strftime("%H:%M:%S")
        if kind == "focus":
            self.focus_label.config(text=evt.get("title") or "(no XWayland window focused)")
        elif kind == "fire":
            self._append(f"[{ts}] {evt.get('name','?')} -> fired", tag="ok")
        elif kind == "wrong_focus":
            title = evt.get("title") or "(none)"
            required = evt.get("required") or ""
            self._append(f"[{ts}] {evt.get('name','?')} BLOCKED - focus={title!r}", tag="warn")
            self.banner.config(text=f"WRONG WINDOW  (need: {required})")
            self._banner_clear_at = time.time() + BANNER_SECONDS
        elif kind == "loop_on":
            self._append(f"[{ts}] {evt.get('name','?')} loop ON", tag="ok")
        elif kind == "loop_off":
            self._append(f"[{ts}] {evt.get('name','?')} loop OFF", tag="dim")
        elif kind == "info":
            self._append(f"[{ts}] {evt.get('msg','')}", tag="dim")
        elif kind == "error":
            self._append(f"[{ts}] ERROR: {evt.get('msg','')}", tag="warn")

    def run(self) -> None:
        self.root.mainloop()
