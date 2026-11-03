# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Seed loader tests. The seed data here is made up."""
from __future__ import annotations

import json

import pytest

from scaleconnect.catalog import Catalog
from scaleconnect.models import Item
from scaleconnect.seed import CatalogExistsError, SeedFileError, main, read_seed, seed_catalog


def raw(n: int, plu: str | None = None, field6: str = " ") -> list[str]:
    plu = plu or f"{9000 + n}"
    return [str(n), plu, plu, f"Made Up {n}", "3.25", field6, "1", "1", "0", "0", "0", "0",
            "2026-01-02 03:04:05", "0", "0", "0", "0", "0", "0", "0"]


def write_seed(path, entries):
    path.write_text(json.dumps({"items": entries}), encoding="utf-8")
    return path


def named(r):
    return {"id": r[0], "code": r[1], "plu": r[2], "description": r[3], "price": r[4], "field6": r[5],
            "fields_7_12": r[6:12], "timestamp": r[12], "fields_14_20": r[13:20]}


def test_seed_with_raw_and_named_and_lists(tmp_path):
    seed = write_seed(tmp_path / "seed.json", [
        {**named(raw(1)), "raw": raw(1)},      # both, agreeing
        {"raw": raw(2, field6="")},            # raw only
        raw(3),                                 # plain list
        named(raw(4)),                          # named only, id as text
    ])
    db = tmp_path / "cat.sqlite3"
    info = seed_catalog(db, seed)
    assert info["items"] == 4 and info["free_ids"] == 496 and info["previous_catalog"] is None
    with Catalog(db) as c:
        assert [i.to_fields() for i in c.list_items()] == [raw(1), raw(2, field6=""), raw(3), raw(4)]
        assert c.get_by_id(1).field6 == " " and c.get_by_id(2).field6 == ""
        assert c.free_ids(1) == [5]
        assert c.read_log(c.list_change_ids()[0])[0]["event"] == "seeded"


def test_refuses_to_overwrite_existing_catalog(tmp_path):
    seed = write_seed(tmp_path / "seed.json", [raw(1)])
    db = tmp_path / "cat.sqlite3"
    seed_catalog(db, seed)
    with Catalog(db) as c:
        c.upsert_item(Item.from_fields(raw(2)))
    before = db.read_bytes()
    with pytest.raises(CatalogExistsError):
        seed_catalog(db, seed)
    assert db.read_bytes() == before
    with Catalog(db) as c:
        assert c.count() == 2  # the later item is still there


def test_overwrite_renames_the_old_catalog(tmp_path):
    seed = write_seed(tmp_path / "seed.json", [raw(1)])
    db = tmp_path / "cat.sqlite3"
    seed_catalog(db, seed)
    with Catalog(db) as c:
        c.upsert_item(Item.from_fields(raw(2)))
    info = seed_catalog(db, seed, overwrite=True)
    assert info["previous_catalog"]
    with Catalog(db) as c:
        assert c.count() == 1
    from pathlib import Path
    with Catalog(Path(info["previous_catalog"])) as old:
        assert old.count() == 2


def test_bad_seed_leaves_an_existing_catalog_alone(tmp_path):
    good = write_seed(tmp_path / "good.json", [raw(1)])
    db = tmp_path / "cat.sqlite3"
    seed_catalog(db, good)
    dup = write_seed(tmp_path / "dup.json", [raw(1, "9100"), raw(2, "9100")])
    with pytest.raises(Exception):
        seed_catalog(db, dup, overwrite=True)
    with Catalog(db) as c:
        assert c.count() == 1 and c.get_by_id(1).plu == "9001"
    assert list(tmp_path.glob("cat.sqlite3.old-*")) == []


def test_bad_seed_creates_nothing(tmp_path):
    db = tmp_path / "new.sqlite3"
    dup = write_seed(tmp_path / "dup.json", [raw(1, "9100"), raw(2, "9100")])
    with pytest.raises(Exception):
        seed_catalog(db, dup)
    assert not db.exists()


@pytest.mark.parametrize("entries", [
    [],
    [raw(1)[:19]],
    [{"id": "1"}],
    ["text"],
    [raw(501)],
    [raw(0)],
    [{**named(raw(1)), "raw": raw(2)}],
])
def test_bad_seed_files_are_rejected(tmp_path, entries):
    seed = write_seed(tmp_path / "s.json", entries)
    with pytest.raises(Exception):
        read_seed(seed) if entries == [] else seed_catalog(tmp_path / "c.sqlite3", seed)
    assert not (tmp_path / "c.sqlite3").exists()


def test_missing_and_broken_files(tmp_path):
    with pytest.raises(SeedFileError):
        read_seed(tmp_path / "nope.json")
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SeedFileError):
        read_seed(p)


def test_believed_on_marks_the_scale(tmp_path):
    seed = write_seed(tmp_path / "s.json", [raw(1), raw(2)])
    db = tmp_path / "c.sqlite3"
    info = seed_catalog(db, seed, believed_on=("FRONT",))
    assert info["believed_on"] == ["FRONT"]
    with Catalog(db) as c:
        st = c.scale_states(1)["FRONT"]
        # L8: nobody has checked that scale, so it is not "present".
        assert st["belief"] == "unknown" and st["status"] == "seed" and st["record"] == raw(1)
        assert c.scale_states(1).keys() == {"FRONT"}  # no belief about other scales


def test_without_believed_on_no_scale_state(tmp_path):
    db = tmp_path / "c.sqlite3"
    seed_catalog(db, write_seed(tmp_path / "s.json", [raw(1)]))
    with Catalog(db) as c:
        assert c.scale_states(1) == {}


def test_command_line(tmp_path, capsys):
    seed = write_seed(tmp_path / "s.json", [raw(1), raw(2)])
    db = tmp_path / "c.sqlite3"
    assert main([str(seed), "--db", str(db)]) == 0
    assert "Seeded 2 items" in capsys.readouterr().out
    assert main([str(seed), "--db", str(db)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main([str(seed), "--db", str(db), "--overwrite", "--believed-on", "FRONT"]) == 0
