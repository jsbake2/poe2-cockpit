from pathlib import Path

import pytest
from evdev import ecodes as e

from service.config import (
    load,
    parse_duration,
    parse_trigger,
    resolve_step,
)


def test_parse_duration_ms():
    assert parse_duration("100ms") == pytest.approx(0.1)
    assert parse_duration("0ms") == 0.0


def test_parse_duration_seconds():
    assert parse_duration("1.5s") == pytest.approx(1.5)


def test_parse_duration_plain_number_is_ms():
    assert parse_duration(250) == pytest.approx(0.25)


def test_parse_duration_missing_defaults():
    assert parse_duration(None) == 0.0
    assert parse_duration("") == 0.0


def test_parse_duration_bad_value():
    with pytest.raises(ValueError):
        parse_duration("abc")


def test_parse_trigger_basic():
    t = parse_trigger("Ctrl+Alt+H")
    assert t.mods == frozenset({"ctrl", "alt"})
    assert t.key == e.KEY_H


def test_parse_trigger_aliases():
    t = parse_trigger("Control+Super+Enter")
    assert t.mods == frozenset({"ctrl", "meta"})
    assert t.key == e.KEY_ENTER


def test_parse_trigger_no_mods():
    t = parse_trigger("F5")
    assert t.mods == frozenset()
    assert t.key == e.KEY_F5


def test_parse_trigger_unknown_mod():
    with pytest.raises(ValueError):
        parse_trigger("Foo+H")


def test_resolve_step_known_key():
    s = resolve_step("Enter")
    assert s.kind == "key" and s.code == e.KEY_ENTER


def test_resolve_step_single_char_is_key():
    s = resolve_step("q")
    assert s.kind == "key" and s.code == e.KEY_Q


def test_resolve_step_string_is_type():
    s = resolve_step("/hideout")
    assert s.kind == "type" and s.text == "/hideout"


def test_resolve_step_untypeable_char():
    with pytest.raises(ValueError):
        resolve_step("héllo")  # 'é' not in CHAR_MAP


def test_resolve_step_sleep_ms():
    s = resolve_step("sleep 200ms")
    assert s.kind == "sleep" and s.sleep_s == pytest.approx(0.2)


def test_resolve_step_sleep_seconds():
    s = resolve_step("sleep 1.5s")
    assert s.kind == "sleep" and s.sleep_s == pytest.approx(1.5)


def test_resolve_step_sleep_zero_rejected():
    with pytest.raises(ValueError):
        resolve_step("sleep 0ms")


def test_resolve_step_combo_ctrl_q():
    s = resolve_step("Ctrl+q")
    assert s.kind == "combo"
    assert s.mods == frozenset({"ctrl"})
    assert s.code == e.KEY_Q


def test_resolve_step_combo_multi_mod():
    s = resolve_step("Shift+Alt+F1")
    assert s.kind == "combo"
    assert s.mods == frozenset({"shift", "alt"})
    assert s.code == e.KEY_F1


def test_resolve_step_combo_bad_last_token():
    with pytest.raises(ValueError):
        resolve_step("Ctrl+foobar")


def test_resolve_step_plus_in_type_string():
    # "a+b" has '+' but 'a' is a known single-char key, which wins first
    # so let's verify with something that isn't a modifier at all:
    s = resolve_step("1+1")
    assert s.kind == "type" and s.text == "1+1"


def test_load_example(tmp_path: Path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "hideout:\n"
        "  sequence: Ctrl+Alt+H\n"
        "  delayBefore: 0ms\n"
        "  delayAfter: 100ms\n"
        "  keypress:\n"
        "    - Enter\n"
        "    - /hideout\n"
        "    - Enter\n"
        "spammer:\n"
        "  sequence: Ctrl+Alt+X\n"
        "  repeatTimer: 200ms\n"
        "  toggleMode: true\n"
        "  keypress:\n"
        "    - q\n"
    )
    cfg = load(p)
    assert [c.name for c in cfg.hotkeys] == ["hideout", "spammer"]

    h = cfg.hotkeys[0]
    assert h.trigger.key == e.KEY_H
    assert h.trigger.mods == frozenset({"ctrl", "alt"})
    assert h.delay_before_s == 0.0
    assert h.delay_after_s == pytest.approx(0.1)
    assert h.repeat_interval_s is None
    assert [s.kind for s in h.steps] == ["key", "type", "key"]
    assert h.steps[1].text == "/hideout"

    sp = cfg.hotkeys[1]
    assert sp.repeat_interval_s == pytest.approx(0.2)
    assert sp.toggle_mode is True
    assert sp.steps[0].kind == "key" and sp.steps[0].code == e.KEY_Q


def test_load_tolerates_sequenct_typo(tmp_path: Path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "hideout:\n"
        "  sequenct: Ctrl+Alt+H\n"
        "  keypress: [Enter]\n"
    )
    cfg = load(p)
    assert cfg.hotkeys[0].trigger.key == e.KEY_H


def test_load_repeat_implies_toggle(tmp_path: Path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "x:\n"
        "  sequence: Ctrl+Alt+X\n"
        "  repeatTimer: 500ms\n"
        "  keypress: [q]\n"
    )
    cfg = load(p)
    assert cfg.hotkeys[0].toggle_mode is True


def test_load_missing_sequence(tmp_path: Path):
    p = tmp_path / "h.yaml"
    p.write_text("bad:\n  keypress: [q]\n")
    with pytest.raises(ValueError, match="missing 'sequence'"):
        load(p)


def test_load_focus_required_top_level(tmp_path: Path):
    p = tmp_path / "h.yaml"
    p.write_text(
        'focus_required: "Path of Exile"\n'
        "hideout:\n"
        "  sequence: Ctrl+Alt+H\n"
        "  keypress: [Enter]\n"
    )
    cfg = load(p)
    assert cfg.focus_required == "Path of Exile"
    assert cfg.hotkeys[0].focus_required == "Path of Exile"


def test_load_focus_required_per_hotkey_override(tmp_path: Path):
    p = tmp_path / "h.yaml"
    p.write_text(
        'focus_required: "Path of Exile"\n'
        "hideout:\n"
        "  sequence: Ctrl+Alt+H\n"
        "  keypress: [Enter]\n"
        "global_thing:\n"
        "  sequence: Ctrl+Alt+G\n"
        "  focus_required: false\n"
        "  keypress: [q]\n"
    )
    cfg = load(p)
    assert cfg.hotkeys[0].focus_required == "Path of Exile"
    assert cfg.hotkeys[1].focus_required is None
