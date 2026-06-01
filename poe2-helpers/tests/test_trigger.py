from evdev import ecodes as e

from service.config import HotkeyCfg, Step, Trigger
from service.main import jitter, trigger_matches


def _cfg(mods, key, steps=()):
    return HotkeyCfg(
        name="t",
        trigger=Trigger(mods=frozenset(mods), key=key),
        steps=list(steps),
    )


def test_ctrl_alt_h_fires():
    cfg = _cfg({"ctrl", "alt"}, e.KEY_H)
    assert trigger_matches(cfg, {e.KEY_LEFTCTRL, e.KEY_LEFTALT, e.KEY_H}, e.KEY_H)


def test_right_ctrl_right_alt_also_fires():
    cfg = _cfg({"ctrl", "alt"}, e.KEY_H)
    assert trigger_matches(cfg, {e.KEY_RIGHTCTRL, e.KEY_RIGHTALT, e.KEY_H}, e.KEY_H)


def test_missing_alt_does_not_fire():
    cfg = _cfg({"ctrl", "alt"}, e.KEY_H)
    assert not trigger_matches(cfg, {e.KEY_LEFTCTRL, e.KEY_H}, e.KEY_H)


def test_extra_shift_blocks_fire():
    cfg = _cfg({"ctrl", "alt"}, e.KEY_H)
    held = {e.KEY_LEFTCTRL, e.KEY_LEFTALT, e.KEY_LEFTSHIFT, e.KEY_H}
    assert not trigger_matches(cfg, held, e.KEY_H)


def test_wrong_key_does_not_fire():
    cfg = _cfg({"ctrl", "alt"}, e.KEY_H)
    assert not trigger_matches(cfg, {e.KEY_LEFTCTRL, e.KEY_LEFTALT, e.KEY_J}, e.KEY_J)


def test_jitter_zero_passes_through():
    assert jitter(0) == 0


def test_jitter_is_one_sided_upward():
    samples = [jitter(1.0) for _ in range(200)]
    # Never below the input, never above +20%
    assert all(1.0 <= s <= 1.2 for s in samples)
    # And it actually varies across that range
    assert min(samples) < 1.05 and max(samples) > 1.15
