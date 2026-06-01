# poe2-helpers

Small Linux service that binds global hotkeys to keystroke sequences. Built for Path of Exile 2 on Linux (Proton/XWayland), but works with anything.

**Features**

- Global hotkeys via `/dev/input/event*` (evdev) — works across X11 and Wayland.
- Injects keystrokes via a kernel-level virtual keyboard (`/dev/uinput`) — indistinguishable from a real USB keyboard.
- Hotkeys declared in `hotkeys.yaml`; each one can:
  - Type a literal string (e.g. `/hideout`).
  - Tap a named key (`Enter`, `Esc`, `F5`, `q`, ...).
  - Tap a modifier combo (`Ctrl+Q`, `Shift+Alt+F1`).
  - Sleep for a fixed duration between steps (`sleep 200ms`).
  - Repeat on a timer (`repeatTimer: 500ms`) with press-again-to-stop toggle.
- Humanized timing: key-hold and inter-key gaps randomized; configured delays/sleeps/repeat intervals are one-sided jittered (never shorter than the stated value, up to +20%).
- **Focus gate**: optional `focus_required` per hotkey; if the named window isn't focused, the hotkey is blocked and a big red banner pops up in the status window.
- Simple Tk status window: shows the active window live, logs every fire/block, flashes on wrong-window.

## Install

```sh
./setup.sh
```

The setup script:

- Installs system deps (`python`, `tk`, `xdotool`) via pacman / apt / dnf.
- Creates `.venv/` and installs Python deps (`evdev`, `pyyaml`).
- Adds you to the `input` group.

**Log out and log back in** after the first run so the `input` group takes effect, then:

```sh
./run.sh
```

A dark status window pops up. Press your configured hotkeys — events show up in the log.

## Configure

All hotkeys live in `hotkeys.yaml`. Top-level `focus_required` (optional) applies to every hotkey; per-hotkey `focus_required` overrides it (use `false` to disable the gate for that one hotkey).

```yaml
focus_required: "Path of Exile"   # substring, case-insensitive

hideout:
  sequence: Ctrl+Alt+H
  keypress:
    - Enter          # open chat
    - /hideout       # type the command
    - Enter          # submit

buffs:
  sequence: Ctrl+Alt+B
  keypress:
    - Ctrl+q
    - sleep 200ms
    - Ctrl+r
    - sleep 250ms
    - Ctrl+t

spam_q:
  sequence: Ctrl+Alt+Q
  repeatTimer: 500ms   # runs every 500ms (±10%)
  toggleMode: true     # implied by repeatTimer; press again to stop
  keypress:
    - q
```

### Field reference

| field            | type       | notes |
|---               |---         |---|
| `sequence`       | string     | Trigger, e.g. `Ctrl+Alt+H`. Mods: `Ctrl`/`Alt`/`Shift`/`Meta` (`Super`, `Win` also accepted). |
| `keypress`       | list       | Ordered steps; each item is a key name, a combo, a literal string, or `sleep <duration>`. |
| `delayBefore`    | duration   | Pause before the first step. |
| `delayAfter`     | duration   | Pause after the last step. |
| `repeatTimer`    | duration   | If set, hotkey toggles a repeating loop with this interval between iterations. |
| `toggleMode`     | bool       | Press again to stop. Implicitly `true` when `repeatTimer` is set. |
| `focus_required` | string / `false` | Override the top-level focus gate. `false` disables it for this hotkey. |

Durations accept `100ms`, `1.5s`, or a bare number (interpreted as ms). Every non-zero timer (`delayBefore`, `delayAfter`, `repeatTimer`, `sleep` steps) is jittered **0 to +20%** — the configured value is the minimum, never shorter.

### Keypress step types

| form | example | behavior |
|---|---|---|
| key name | `Enter`, `Esc`, `F5`, `Tab`, `Space`, `q`, `7` | Single tap. |
| combo | `Ctrl+q`, `Shift+Alt+F1` | Holds modifiers, taps the key, releases. |
| literal | `/hideout`, `hello world` | Types each character (handles shift for caps/symbols). |
| sleep  | `sleep 200ms`, `sleep 1.5s` | Minimum pause; jittered up to +20%. |

## Focus gate — how it detects the active window

Uses `xdotool getwindowfocus getwindowname`, which reads X11's `XGetInputFocus`. This correctly reflects focus for any XWayland client (Proton games, most non-Wayland-native apps). Native Wayland windows return `""` — if `focus_required` is set, those are treated as wrong-window. Repeating loops re-check focus before every iteration and auto-cancel if focus is lost.

## Auto-start with systemd --user (optional)

```sh
cp poe2-helpers.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now poe2-helpers.service
journalctl --user -u poe2-helpers -f
```

## Tests

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/
```

## Troubleshooting

**Hotkeys don't fire.** You aren't in the `input` group yet. Run `groups | grep input`; if missing, `sudo gpasswd -a $USER input` and log out/in.

**Focus gate always blocks, even in-game.** Only X11/XWayland apps report focus. If your game is running natively (rare on Linux), remove or disable `focus_required`. Steam/Proton games are XWayland, so they work.

**Typing fires while I still hold the hotkey modifiers.** The service waits for you to release Ctrl/Alt/Shift/Meta before typing (up to 2s) so the target app doesn't see Ctrl+Alt+/, etc. If you hold the hotkey longer than 2s it'll fire anyway.

**`sg input -c ...` exits immediately.** If launching manually without logging out, use `sg input -c ./run.sh`. After re-login, `./run.sh` works directly.
