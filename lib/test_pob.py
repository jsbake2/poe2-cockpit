"""Smoke tests for lib.pob — round-trips a synthetic PoB XML through
encode (zlib + urlsafe-base64) -> decode -> parse.
Run: .venv/bin/python -m lib.test_pob
"""

from __future__ import annotations

import base64
import zlib

from . import pob


SYNTHETIC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PathOfBuilding>
  <Build level="85" className="Ranger" ascendClassName="Deadeye" mainSocketGroup="1">
    <PlayerStat stat="Life" value="4120"/>
    <PlayerStat stat="EnergyShield" value="512"/>
    <PlayerStat stat="CombinedDPS" value="1250000"/>
  </Build>
  <Items activeItemSet="1">
    <Item id="1">
Rarity: RARE
Frost Eye
Vaal Regalia
--------
Item Level: 84
--------
+120 to maximum Life
+38% to Fire Resistance
+41% to Cold Resistance
+37% to Lightning Resistance
+25% increased Energy Shield (crafted)
6% increased Movement Speed (enchant)
    </Item>
    <Item id="2">
Rarity: UNIQUE
Ventor's Gamble
Gold Ring
--------
Item Level: 76
--------
+20 to all Attributes (implicit)
--------
(-25-50)% to all Elemental Resistances
(0-40)% increased Rarity of Items found
    </Item>
    <ItemSet id="1">
      <Slot name="Body Armour" itemId="1"/>
      <Slot name="Ring 1" itemId="2"/>
    </ItemSet>
  </Items>
  <Skills activeSkillSet="1">
    <SkillSet id="1">
      <Skill label="Main — Frost Darts" enabled="true" mainActiveSkill="1">
        <Gem nameSpec="Frost Darts" level="20" quality="20"/>
        <Gem nameSpec="Added Cold Damage" level="20" quality="20"/>
      </Skill>
      <Skill label="Movement" enabled="true">
        <Gem nameSpec="Blink" level="1" quality="0"/>
      </Skill>
    </SkillSet>
  </Skills>
  <Tree activeSpec="1">
    <Spec id="1" className="Ranger" ascendClassName="Deadeye" nodes="10000,10001,10002,52980">
      <URL>https://www.pathofexile.com/passive-skill-tree/...</URL>
      <Sockets>
        <Socket nodeId="52980" itemId="3"/>
      </Sockets>
    </Spec>
  </Tree>
  <Notes>Leveling notes go here.</Notes>
</PathOfBuilding>
"""


def encode_pob(xml: str) -> str:
    raw = zlib.compress(xml.encode("utf-8"))
    b64 = base64.b64encode(raw).decode("ascii")
    # Convert to urlsafe (PoB's convention)
    return b64.replace("+", "-").replace("/", "_")


def main() -> None:
    code = encode_pob(SYNTHETIC_XML)
    build = pob.import_build(code)

    assert build.level == 85, build.level
    assert build.class_name == "Ranger"
    assert build.ascendancy == "Deadeye"
    assert build.stats["Life"] == 4120
    assert build.stats["CombinedDPS"] == 1_250_000

    by_slot = build.items_by_slot()
    assert "Body Armour" in by_slot
    assert "Ring 1" in by_slot

    chest = by_slot["Body Armour"]
    assert chest.rarity == "Rare"
    assert chest.name == "Frost Eye"
    assert chest.base == "Vaal Regalia"
    assert chest.item_level == 84
    mod_texts = [(m.kind, m.text) for m in chest.mods]
    assert ("explicit", "+120 to maximum Life") in mod_texts
    assert ("crafted", "+25% increased Energy Shield") in mod_texts
    assert ("enchant", "6% increased Movement Speed") in mod_texts

    ring = by_slot["Ring 1"]
    assert ring.rarity == "Unique"
    assert ring.name == "Ventor's Gamble"
    assert ring.base == "Gold Ring"
    ring_mods = [(m.kind, m.text) for m in ring.mods]
    assert ("implicit", "+20 to all Attributes") in ring_mods
    assert any("Elemental Resistances" in m.text for m in ring.mods)

    # Content-hash stability
    h1 = chest.content_hash()
    h2 = pob.import_build(code).items_by_slot()["Body Armour"].content_hash()
    assert h1 == h2, "hash not stable across re-parse"

    # Mutation should change hash
    chest.mods[0].text = "+121 to maximum Life"
    h3 = chest.content_hash()
    assert h3 != h1, "hash did not change on mod edit"

    # Skills
    assert len(build.skills) == 2
    main_skill = build.skills[0]
    assert main_skill.label == "Main — Frost Darts"
    assert "Frost Darts" in main_skill.gems

    # Tree
    assert build.tree.class_name == "Ranger"
    assert 10000 in build.tree.nodes
    assert 52980 in build.tree.nodes
    assert build.tree.jewel_sockets.get(52980) == "3"

    assert build.notes.startswith("Leveling notes")

    print("lib.pob smoke tests passed.")


if __name__ == "__main__":
    main()
