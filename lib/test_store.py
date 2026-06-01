"""Smoke tests for lib.store — round-trips state through save/load,
validates slot-diff and variant creation.
Run: .venv/bin/python -m lib.test_store
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from . import store
from .test_pob import SYNTHETIC_XML, encode_pob
from . import pob as pob_mod


def main() -> None:
    code = encode_pob(SYNTHETIC_XML)
    parsed = pob_mod.import_build(code)

    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(store, "DATA_DIR", Path(td)), \
             mock.patch.object(store, "STATE_PATH", Path(td) / "state.json"):
            s = store.State()
            build = store.new_build_from_pob_import(
                parsed, label="Frost Darts — Endgame", phase="endgame",
                pob_code=code, imported_by="jbaker",
            )
            s.builds[build.id] = build
            s.active_builds["jbaker"] = build.id

            v = store.new_variant(
                build_id=build.id, slot="Body Armour", label="budget",
                created_by="jbaker",
                mod_rankings={"abc": "must", "def": "nice"},
            )
            s.variants[v.id] = v
            s.notes.append(store.new_note("jbaker", "hello"))
            s.filter.path = "/tmp/test.filter"

            store.save(s)
            loaded = store.load()

        assert loaded.schema_version == store.SCHEMA_VERSION
        assert build.id in loaded.builds
        lb = loaded.builds[build.id]
        assert lb.label == "Frost Darts — Endgame"
        assert "Body Armour" in lb.items
        assert lb.items["Body Armour"].rarity == "Rare"
        assert lb.items["Body Armour"].content_hash == build.items["Body Armour"].content_hash
        assert lb.tree.class_name == "Ranger"
        assert 10000 in lb.tree.nodes

        assert v.id in loaded.variants
        assert loaded.variants[v.id].mod_rankings["abc"] == "must"
        assert loaded.active_builds["jbaker"] == build.id
        assert loaded.notes[0].author == "jbaker"
        assert loaded.filter.path == "/tmp/test.filter"

        # Slot-diff: unchanged → unchanged
        diff = store.diff_items(build, build.items)
        assert all(s == "unchanged" for s in diff.values()), diff

        # Modify a mod on chest → should report modified
        chest_new = store.StoredItem(
            slot="Body Armour", name=build.items["Body Armour"].name,
            base=build.items["Body Armour"].base, rarity="Rare",
            item_level=84,
            mods=[{"text": "+999 to maximum Life", "kind": "explicit"}],
            content_hash="",
        )
        # rebuild content_hash
        from hashlib import sha1
        payload = f"Body Armour|{chest_new.name}|{chest_new.base}|Rare|"
        payload += "||".join(
            sha1(f'{m["kind"]}|{m["text"]}'.encode()).hexdigest()[:12]
            for m in chest_new.mods
        )
        chest_new.content_hash = sha1(payload.encode()).hexdigest()[:16]

        diff2 = store.diff_items(build, {"Body Armour": chest_new, "Ring 1": build.items["Ring 1"]})
        assert diff2["Body Armour"] == "modified", diff2
        assert diff2["Ring 1"] == "unchanged", diff2

    print("lib.store smoke tests passed.")


if __name__ == "__main__":
    main()
