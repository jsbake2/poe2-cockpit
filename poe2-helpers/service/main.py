"""Configurable hotkey service driven by hotkeys.yaml, with tkinter status GUI."""
from __future__ import annotations

import asyncio
import logging
import queue
import random
import threading
import time
from pathlib import Path

from evdev import InputDevice, UInput, ecodes as e, list_devices

from .config import CHAR_MAP, Config, HotkeyCfg, KEY_ALIASES, MOD_CODES, Step, load
from .focus import active_window_title, matches_required

log = logging.getLogger(__name__)

VIRTUAL_DEVICE_NAME = "poe2-helpers virtual keyboard"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "hotkeys.yaml"

FOCUS_POLL_INTERVAL_S = 0.5

MOD_GROUPS = {
    "ctrl":  {e.KEY_LEFTCTRL, e.KEY_RIGHTCTRL},
    "alt":   {e.KEY_LEFTALT, e.KEY_RIGHTALT},
    "shift": {e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT},
    "meta":  {e.KEY_LEFTMETA, e.KEY_RIGHTMETA},
}
ALL_MODS: set[int] = set().union(*MOD_GROUPS.values())


def jitter(seconds: float, pct: float = 0.20) -> float:
    """Return a value between seconds and seconds*(1+pct). Zero passes through.

    One-sided on purpose: the configured number is the minimum, never shorter.
    In-game timings (e.g. buffs pipeline) break when a delay undershoots.
    """
    if seconds <= 0:
        return seconds
    return random.uniform(seconds, seconds * (1.0 + pct))


def trigger_matches(cfg: HotkeyCfg, held: set[int], code: int) -> bool:
    if code != cfg.trigger.key:
        return False
    for name, codes in MOD_GROUPS.items():
        if (name in cfg.trigger.mods) != bool(held & codes):
            return False
    return True


def _virtual_caps() -> dict:
    keys: set[int] = set()
    keys |= ALL_MODS
    keys |= set(KEY_ALIASES.values())
    keys |= {code for code, _ in CHAR_MAP.values()}
    return {e.EV_KEY: sorted(keys)}


class Typer:
    def __init__(self) -> None:
        self._ui = UInput(_virtual_caps(), name=VIRTUAL_DEVICE_NAME,
                          vendor=0x1209, product=0x2026, version=1, bustype=0x03)
        time.sleep(0.2)
        log.info("virtual device at %s", self._ui.device.path if self._ui.device else "?")

    @property
    def device_path(self) -> str | None:
        return self._ui.device.path if self._ui.device else None

    def close(self) -> None:
        try:
            self._ui.close()
        except Exception:
            pass

    def _emit(self, code: int, val: int) -> None:
        self._ui.write(e.EV_KEY, code, val)
        self._ui.syn()

    def tap(self, code: int) -> None:
        hold = random.uniform(0.035, 0.070)
        self._emit(code, 1)
        time.sleep(hold)
        self._emit(code, 0)

    def tap_shifted(self, code: int) -> None:
        self.tap_combo(frozenset({"shift"}), code)

    def tap_combo(self, mods: frozenset[str], code: int) -> None:
        mod_codes = [MOD_CODES[m] for m in mods]
        hold = random.uniform(0.035, 0.070)
        for mc in mod_codes:
            self._emit(mc, 1)
        self._emit(code, 1)
        time.sleep(hold)
        self._emit(code, 0)
        for mc in reversed(mod_codes):
            self._emit(mc, 0)

    def type_text(self, text: str) -> None:
        for i, ch in enumerate(text):
            code, needs_shift = CHAR_MAP[ch]
            if i > 0:
                time.sleep(random.uniform(0.06, 0.14))
            if needs_shift:
                self.tap_shifted(code)
            else:
                self.tap(code)


def _inter_step_gap(prev: Step, nxt: Step) -> float:
    if prev.kind == "sleep" or nxt.kind == "sleep":
        return 0.0  # an explicit sleep is the gap
    if prev.kind == "key" and prev.code == e.KEY_ENTER:
        return random.uniform(0.22, 0.32)
    if nxt.kind == "key" and nxt.code == e.KEY_ENTER:
        return random.uniform(0.18, 0.25)
    return random.uniform(0.06, 0.14)


async def wait_for_release(held: set[int], codes: set[int], timeout_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if not (held & codes):
            return
        await asyncio.sleep(0.01)


def _execute_sync(cfg: HotkeyCfg, typer: Typer) -> None:
    if cfg.delay_before_s > 0:
        time.sleep(jitter(cfg.delay_before_s))
    for i, step in enumerate(cfg.steps):
        if i > 0:
            gap = _inter_step_gap(cfg.steps[i - 1], step)
            if gap > 0:
                time.sleep(gap)
        if step.kind == "key":
            typer.tap(step.code)
        elif step.kind == "type":
            typer.type_text(step.text)
        elif step.kind == "combo":
            typer.tap_combo(step.mods, step.code)
        elif step.kind == "sleep":
            time.sleep(jitter(step.sleep_s))
    if cfg.delay_after_s > 0:
        time.sleep(jitter(cfg.delay_after_s))


async def execute_once(cfg: HotkeyCfg, typer: Typer) -> None:
    await asyncio.to_thread(_execute_sync, cfg, typer)


class HotkeyRunner:
    def __init__(self, cfg: HotkeyCfg, typer: Typer, held: set[int],
                 evq: "queue.Queue[dict] | None") -> None:
        self.cfg = cfg
        self.typer = typer
        self.held = held
        self.evq = evq
        self._loop_task: asyncio.Task | None = None

    def _emit(self, **kw) -> None:
        if self.evq is not None:
            try:
                self.evq.put_nowait(kw)
            except queue.Full:
                pass

    async def _focus_ok(self) -> tuple[bool, str]:
        if not self.cfg.focus_required:
            return True, ""
        title = await active_window_title()
        return matches_required(title, self.cfg.focus_required), title

    async def _repeat_loop(self) -> None:
        try:
            while True:
                ok, title = await self._focus_ok()
                if ok:
                    await execute_once(self.cfg, self.typer)
                else:
                    self._emit(kind="wrong_focus", name=self.cfg.name,
                               title=title, required=self.cfg.focus_required)
                    # stop the loop on wrong focus so we don't spam the banner
                    self._loop_task = None
                    self._emit(kind="loop_off", name=self.cfg.name)
                    log.info("[%s] loop auto-stopped: focus=%r", self.cfg.name, title)
                    return
                await asyncio.sleep(jitter(self.cfg.repeat_interval_s))
        except asyncio.CancelledError:
            pass

    async def fire(self) -> None:
        await wait_for_release(self.held, ALL_MODS | {self.cfg.trigger.key})
        await asyncio.sleep(0.04)

        if self.cfg.repeat_interval_s is not None:
            if self._loop_task and not self._loop_task.done():
                self._loop_task.cancel()
                self._loop_task = None
                log.info("[%s] loop OFF", self.cfg.name)
                self._emit(kind="loop_off", name=self.cfg.name)
            else:
                ok, title = await self._focus_ok()
                if not ok:
                    self._emit(kind="wrong_focus", name=self.cfg.name,
                               title=title, required=self.cfg.focus_required)
                    log.info("[%s] blocked: focus=%r", self.cfg.name, title)
                    return
                log.info("[%s] loop ON (%.0fms +0-20%%)", self.cfg.name,
                         self.cfg.repeat_interval_s * 1000)
                self._emit(kind="loop_on", name=self.cfg.name)
                self._loop_task = asyncio.create_task(self._repeat_loop())
        else:
            ok, title = await self._focus_ok()
            if not ok:
                self._emit(kind="wrong_focus", name=self.cfg.name,
                           title=title, required=self.cfg.focus_required)
                log.info("[%s] blocked: focus=%r", self.cfg.name, title)
                return
            log.info("[%s] fire", self.cfg.name)
            self._emit(kind="fire", name=self.cfg.name)
            await execute_once(self.cfg, self.typer)

    def cancel(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()


def open_keyboards(exclude_path: str | None) -> list[InputDevice]:
    devices: list[InputDevice] = []
    perm_errors = 0
    paths = list_devices()
    for path in paths:
        try:
            dev = InputDevice(path)
        except PermissionError:
            perm_errors += 1
            continue
        except OSError:
            continue
        if dev.path == exclude_path or dev.name == VIRTUAL_DEVICE_NAME:
            dev.close()
            continue
        caps = dev.capabilities().get(e.EV_KEY, [])
        if e.KEY_A in caps and e.KEY_SPACE in caps:
            devices.append(dev)
            log.info("listening on %s (%s)", path, dev.name)
        else:
            dev.close()
    if not devices:
        if perm_errors:
            log.error("no readable keyboards (PermissionError x%d). "
                      "Fix: `sudo gpasswd -a $USER input` and re-login.", perm_errors)
        else:
            log.error("no keyboards matched after scanning %d device(s)", len(paths))
    return devices


async def read_loop(dev: InputDevice, held: set[int], runners: list[HotkeyRunner]) -> None:
    try:
        async for event in dev.async_read_loop():
            if event.type != e.EV_KEY:
                continue
            code, value = event.code, event.value
            if value == 1:
                held.add(code)
                for r in runners:
                    if trigger_matches(r.cfg, held, code):
                        asyncio.create_task(r.fire())
                        break
            elif value == 0:
                held.discard(code)
    except (OSError, asyncio.CancelledError):
        pass


async def focus_poll_loop(evq: "queue.Queue[dict]") -> None:
    last: str | None = None
    try:
        while True:
            title = await active_window_title()
            if title != last:
                try:
                    evq.put_nowait({"kind": "focus", "title": title})
                except queue.Full:
                    pass
                last = title
            await asyncio.sleep(FOCUS_POLL_INTERVAL_S)
    except asyncio.CancelledError:
        pass


async def amain(evq: "queue.Queue[dict] | None") -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    def emit(**kw):
        if evq is not None:
            try: evq.put_nowait(kw)
            except queue.Full: pass

    if not CONFIG_PATH.exists():
        emit(kind="error", msg=f"config not found at {CONFIG_PATH}")
        log.error("config not found at %s", CONFIG_PATH)
        return
    try:
        cfg = load(CONFIG_PATH)
    except Exception as ex:
        emit(kind="error", msg=f"config load failed: {ex}")
        log.exception("config load failed")
        return
    if not cfg.hotkeys:
        emit(kind="error", msg="no hotkeys defined")
        log.error("no hotkeys defined")
        return
    for c in cfg.hotkeys:
        mods = "+".join(sorted(c.trigger.mods)) or "(no mods)"
        line = (f"config: {c.name} = {mods}+key({c.trigger.key}), steps={len(c.steps)}, "
                f"repeat={f'{c.repeat_interval_s*1000:.0f}ms' if c.repeat_interval_s else 'no'}, "
                f"focus_required={c.focus_required!r}")
        log.info(line)
        emit(kind="info", msg=line)

    typer = Typer()
    emit(kind="info", msg=f"virtual device: {typer.device_path}")
    held: set[int] = set()
    runners = [HotkeyRunner(c, typer, held, evq) for c in cfg.hotkeys]

    devices = open_keyboards(exclude_path=typer.device_path)
    if not devices:
        emit(kind="error", msg="no readable keyboards (check input-group membership)")
        typer.close()
        return

    tasks = [asyncio.create_task(read_loop(d, held, runners)) for d in devices]
    if evq is not None:
        tasks.append(asyncio.create_task(focus_poll_loop(evq)))
    try:
        await asyncio.gather(*tasks)
    finally:
        for r in runners:
            r.cancel()
        for t in tasks:
            t.cancel()
        for d in devices:
            try: d.close()
            except Exception: pass
        typer.close()


def _run_service(evq: "queue.Queue[dict]") -> None:
    try:
        asyncio.run(amain(evq))
    except Exception as ex:
        log.exception("service thread crashed")
        try:
            evq.put_nowait({"kind": "error", "msg": f"service crashed: {ex}"})
        except queue.Full:
            pass


def main() -> None:
    from .gui import StatusGUI
    evq: "queue.Queue[dict]" = queue.Queue(maxsize=256)
    t = threading.Thread(target=_run_service, args=(evq,), daemon=True)
    t.start()
    try:
        StatusGUI(evq).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
