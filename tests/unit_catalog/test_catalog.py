"""Catalog tests. Every item here is made up. Nothing touches a scale."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from scaleconnect.catalog import (
    Catalog, DuplicatePluError, IdInUseError, InvalidIdError, InvalidItemError, NoFreeIdError,
    db_path_from_config, default_db_path,
)
from scaleconnect.models import Item


def mk(n: int, plu: str | None = None, **kw) -> Item:
    plu = plu or f"{9000 + n}"
    base = dict(id=n, code=plu, plu=plu, description=f"Test Item {n}", price="1.50",
                timestamp="2026-01-02 03:04:05")
    base.update(kw)
    return Item(**base)


@pytest.fixture
def cat(tmp_path):
    c = Catalog(tmp_path / "sub" / "catalog.sqlite3")
    yield c
    c.close()


def test_creates_file_and_parent_folder(tmp_path):
    p = tmp_path / "a" / "b" / "c.sqlite3"
    c = Catalog(p)
    c.close()
    assert p.exists()


def test_add_and_get_by_id_and_plu(cat):
    cat.upsert_item(mk(7, "9101"))
    assert cat.get_by_id(7).plu == "9101"
    assert cat.get_by_plu("9101").id == 7
    assert cat.get_by_id(8) is None
    assert cat.get_by_plu("0000") is None


def test_all_twenty_fields_kept_exactly(cat):
    it = Item(id=12, code="9201", plu="9201", description="Made Up  Cheese", price="12.50",
              field6=" ", fields_7_12=("0", "1", "0", "0.1399", "0", "0"),
              timestamp="2026-10-31 09:39:49", fields_14_20=("0", "0", "0", "0", "0", "0", "5"))
    cat.upsert_item(it)
    back = cat.get_by_id(12)
    assert back == it
    assert back.to_fields() == it.to_fields()
    assert back.field6 == " "  # one space is not trimmed to empty
    assert back.price == "12.50"  # text, trailing zero kept


def test_empty_field6_stays_empty(cat):
    cat.upsert_item(mk(1, field6=""))
    assert cat.get_by_id(1).field6 == ""


def test_fields_with_tab_or_newline_are_refused(cat):
    for bad in ("a\tb", "a\nb", "a\rb"):
        with pytest.raises(InvalidItemError):
            cat.upsert_item(mk(1, description=bad))
    assert cat.count() == 0


def test_id_range(cat):
    with pytest.raises(InvalidIdError):
        cat.upsert_item(mk(0, "9300"))
    with pytest.raises(InvalidIdError):
        cat.upsert_item(mk(501, "9301"))
    cat.upsert_item(mk(500, "9302"))
    assert cat.get_by_id(500) is not None


def test_duplicate_plu_refused_on_add_and_edit(cat):
    cat.upsert_item(mk(1, "9400"))
    with pytest.raises(DuplicatePluError):
        cat.upsert_item(mk(2, "9400"))
    cat.upsert_item(mk(2, "9401"))
    with pytest.raises(DuplicatePluError):
        cat.upsert_item(mk(2, "9400"))  # edit onto another item's PLU
    assert cat.get_by_id(2).plu == "9401"  # unchanged after the refusal


def test_edit_keeps_own_plu(cat):
    cat.upsert_item(mk(1, "9500"))
    cat.upsert_item(mk(1, "9500", price="2.00"))
    assert cat.get_by_id(1).price == "2.00"
    assert cat.count() == 1


def test_deleted_item_frees_its_plu(cat):
    cat.upsert_item(mk(1, "9600"))
    cat.remove_item(1)
    assert cat.get_by_plu("9600") is None
    assert cat.get_by_id(1) is None
    cat.upsert_item(mk(2, "9600"))
    assert cat.get_by_plu("9600").id == 2


def test_remove_marks_deleted_and_keeps_row(cat):
    cat.upsert_item(mk(1))
    cat.remove_item(1)
    assert cat.list_items() == []
    assert [i.id for i in cat.list_items(include_deleted=True)] == [1]
    assert cat.is_deleted(1)
    cat.remove_item(1)  # again: nothing happens
    cat.remove_item(5)  # missing: nothing happens


def test_search_by_plu_and_description(cat):
    cat.upsert_item(mk(1, "9701", description="Example Brie"))
    cat.upsert_item(mk(2, "9702", description="Brie Truffle"))
    cat.upsert_item(mk(3, "9710", description="Plain Crackers"))
    assert [i.id for i in cat.search("9701")] == [1]
    assert [i.id for i in cat.search("971")] == [3]  # start of a PLU
    assert [i.id for i in cat.search("brie")] == [2, 1]  # any case, sorted by name
    assert [i.id for i in cat.search("CRACK")] == [3]
    assert cat.search("zzz") == []
    assert len(cat.search("", limit=2)) == 2
    assert len(cat.search("brie", limit=1)) == 1


def test_search_exact_plu_ranks_first(cat):
    cat.upsert_item(mk(1, "9801", description="Aaa"))
    cat.upsert_item(mk(2, "9810", description="Item 9801 special"))
    assert [i.id for i in cat.search("9801")] == [1, 2]


def test_search_skips_deleted(cat):
    cat.upsert_item(mk(1, description="Gone Cheese"))
    cat.remove_item(1)
    assert cat.search("gone") == []


# ---- ids

def test_free_ids_lowest_first_and_gaps_reused(cat):
    for n in (1, 2, 4):
        cat.upsert_item(mk(n))
    assert cat.free_ids(3) == [3, 5, 6]
    assert cat.next_free_id() == 3


def test_free_ids_skip_orphans_and_reserved(cat):
    cat.upsert_item(mk(1))
    cat.mark_orphan_id(2, "FRONT", "seen on the scale")
    cat.reserve_id(3, "kept for tests")
    assert cat.free_ids(2) == [4, 5]
    assert cat.orphan_ids() == {2: ["FRONT"]}
    assert cat.reserved_ids() == {3: "kept for tests"}
    assert cat.clear_orphan_id(2, "FRONT")
    assert cat.free_ids(1) == [2]
    assert cat.unreserve_id(3)
    assert cat.free_ids(2) == [2, 3]


def test_orphan_on_two_scales(cat):
    cat.mark_orphan_id(9, "FRONT")
    cat.mark_orphan_id(9, "KITCHEN")
    assert cat.orphan_ids() == {9: ["FRONT", "KITCHEN"]}
    cat.clear_orphan_id(9, "FRONT")
    assert 9 not in cat.free_ids(20) and cat.orphan_ids() == {9: ["KITCHEN"]}


def test_add_on_orphan_or_reserved_id_is_refused(cat):
    cat.mark_orphan_id(5, "FRONT")
    cat.reserve_id(6)
    with pytest.raises(IdInUseError):
        cat.upsert_item(mk(5))
    with pytest.raises(IdInUseError):
        cat.upsert_item(mk(6))


def test_max_id_limits_free_ids(cat):
    assert cat.free_ids(10, max_id=3) == [1, 2, 3]
    assert cat.free_ids(5, max_id=9999)[-1] == 5


def test_no_free_id_error(cat):
    cat.import_items([mk(n, f"{n:04d}") for n in range(1, 501)])
    assert cat.free_ids(1) == []
    with pytest.raises(NoFreeIdError) as exc:
        cat.next_free_id()
    assert exc.value.code == "no_free_id"
    assert "no free ids" in exc.value.message.lower()


def test_orphans_can_use_up_the_last_ids(cat):
    cat.import_items([mk(n, f"{n:04d}") for n in range(1, 499)])
    cat.mark_orphan_id(499, "FRONT")
    assert cat.free_ids(5) == [500]
    cat.mark_orphan_id(500, "COUNTER")
    with pytest.raises(NoFreeIdError):
        cat.next_free_id()


def test_deleted_id_is_free_when_no_scale_holds_it(cat):
    cat.upsert_item(mk(1))
    cat.remove_item(1)
    assert cat.free_ids(1) == [1]
    cat.upsert_item(mk(1, "9999", description="New Thing"))
    assert cat.get_by_id(1).description == "New Thing"


def test_deleted_id_stays_blocked_while_a_scale_may_hold_it(cat):
    cat.upsert_item(mk(1))
    cat.record_scale_state(1, "FRONT", "accepted_not_verified", "c1", "t")
    cat.remove_item(1)
    assert cat.free_ids(1) == [2]
    cat.record_scale_state(1, "FRONT", "accepted_not_verified", "c2", "t", belief="absent")
    assert cat.free_ids(1) == [1]


def test_deleted_id_blocked_when_scale_state_unknown(cat):
    cat.upsert_item(mk(1))
    cat.record_scale_state(1, "COUNTER", "unknown", "c1", "t")
    cat.remove_item(1)
    assert cat.free_ids(1) == [2]


# ---- scale state

def test_scale_state_roundtrip(cat):
    cat.upsert_item(mk(1))
    assert cat.scale_states(1) == {}
    cat.record_scale_state(1, "FRONT", "accepted_not_verified", "c1", "2026-01-01T00:00:00Z",
                           record=mk(1))
    s = cat.scale_states(1)["FRONT"]
    assert (s["status"], s["change_id"], s["at"], s["belief"]) == (
        "accepted_not_verified", "c1", "2026-01-01T00:00:00Z", "present")
    assert s["record"] == mk(1).to_fields()
    cat.record_scale_state(1, "FRONT", "unknown", "c2", "t2")
    s = cat.scale_states(1)["FRONT"]
    assert s["status"] == "unknown" and s["belief"] == "unknown"
    assert s["record"] == mk(1).to_fields()  # last believed record kept


def test_scale_state_belief_kept_when_nothing_was_sent(cat):
    cat.record_scale_state(1, "FRONT", "verified", "c1", "t")
    cat.record_scale_state(1, "FRONT", "unreachable", "c2", "t")
    assert cat.scale_states(1)["FRONT"]["belief"] == "present"
    cat.record_scale_state(2, "FRONT", "skipped_not_write_enabled", "c2", "t")
    assert cat.scale_states(2)["FRONT"]["belief"] == "unknown"


def test_scale_state_bad_belief(cat):
    with pytest.raises(Exception):
        cat.record_scale_state(1, "FRONT", "verified", "c1", "t", belief="maybe")


def test_scale_state_per_scale(cat):
    cat.record_scale_state(1, "FRONT", "verified", "c1", "t")
    cat.record_scale_state(1, "COUNTER", "rejected", "c1", "t")
    assert set(cat.scale_states(1)) == {"FRONT", "COUNTER"}
    assert cat.believed_on_scale("FRONT").keys() == {1}


def test_believe_scale_holds_listed_items(cat):
    for n in (1, 2):
        cat.upsert_item(mk(n))
    cat.remove_item(2)
    assert cat.believe_scale_holds_listed_items("FRONT", "seed", "seed-1", "t") == 1
    assert cat.scale_states(1)["FRONT"]["belief"] == "unknown"  # L8: nobody has checked
    assert cat.scale_states(2) == {}


# ---- version

def test_version_changes_only_for_item_writes(cat):
    v0 = cat.version()
    cat.upsert_item(mk(1))
    v1 = cat.version()
    assert v1 != v0
    cat.record_scale_state(1, "FRONT", "verified", "c", "t")
    cat.mark_orphan_id(9, "FRONT")
    cat.reserve_id(10)
    cat.append_log("c", "started", {}, "t")
    cat.save_preview("p", {}, "a", "b")
    cat.consume_preview("p", "c")
    assert cat.version() == v1
    cat.upsert_item(mk(1, price="9.99"))
    v2 = cat.version()
    assert v2 not in (v0, v1)
    cat.remove_item(1)
    assert cat.version() not in (v0, v1, v2)


def test_failed_write_does_not_change_version(cat):
    cat.upsert_item(mk(1, "9100"))
    v = cat.version()
    with pytest.raises(DuplicatePluError):
        cat.upsert_item(mk(2, "9100"))
    assert cat.version() == v


# ---- log

def test_log_append_and_read(cat):
    cat.append_log("c1", "started", {"requested_by": "mcp:claude"}, "t1")
    cat.append_log("c1", "scale_result", {"scale": "FRONT", "status": "accepted_not_verified",
                                          "replies": ["5\\rOk"]}, "t2")
    cat.append_log("c2", "started", {}, "t3")
    log = cat.read_log("c1")
    assert [e["event"] for e in log] == ["started", "scale_result"]
    assert log[0] == {"change_id": "c1", "event": "started",
                      "payload": {"requested_by": "mcp:claude"}, "at": "t1"}
    assert log[1]["payload"]["replies"] == ["5\\rOk"]
    assert cat.read_log("nope") == []
    assert cat.list_change_ids() == ["c2", "c1"]
    assert cat.list_change_ids(limit=1) == ["c2"]


def test_log_cannot_be_changed_or_deleted(cat, tmp_path):
    cat.append_log("c1", "started", {}, "t")
    raw = sqlite3.connect(str(cat.path))
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("UPDATE change_log SET event = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("DELETE FROM change_log")
    raw.close()
    assert cat.read_log("c1")[0]["event"] == "started"


def test_item_history_is_append_only(cat):
    cat.upsert_item(mk(1))
    cat.upsert_item(mk(1, price="2.00"))
    cat.remove_item(1)
    raw = sqlite3.connect(str(cat.path))
    ops = [r[0] for r in raw.execute("SELECT op FROM item_history ORDER BY h_id")]
    assert ops == ["add", "update", "delete"]
    with pytest.raises(sqlite3.DatabaseError):
        raw.execute("DELETE FROM item_history")
    raw.close()


def test_last_scale_event(cat):
    assert cat.last_scale_event("FRONT") is None
    cat.append_log("c1", "scale_result", {"scale": "FRONT", "status": "rejected"}, "t1")
    cat.append_log("c1", "scale_result", {"scale": "COUNTER", "status": "verified"}, "t2")
    cat.append_log("c2", "started", {"scale": "FRONT"}, "t3")  # not a scale_result
    cat.append_log("c3", "scale_result", {"scale": "FRONT", "status": "accepted_not_verified"}, "t4")
    ev = cat.last_scale_event("FRONT")
    assert ev["status"] == "accepted_not_verified" and ev["change_id"] == "c3" and ev["at"] == "t4"
    assert cat.last_scale_event("COUNTER")["status"] == "verified"
    assert cat.last_scale_event("KITCHEN") is None


# ---- previews

def test_preview_save_load_consume(cat):
    assert cat.load_preview("p1") is None
    cat.save_preview("p1", {"a": [1, 2], "b": "x"}, "2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00")
    p = cat.load_preview("p1")
    assert p["payload"] == {"a": [1, 2], "b": "x"}
    assert p["consumed_by"] is None and p["expires_at"].endswith("00:15:00+00:00")
    assert cat.consume_preview("p1", "chg1") is True
    assert cat.consume_preview("p1", "chg2") is False
    assert cat.consume_preview("missing", "chg3") is False
    assert cat.load_preview("p1")["consumed_by"] == "chg1"


# ---- threads and processes

def test_threads_share_the_catalog(cat):
    errors = []

    def work(base):
        try:
            for k in range(8):
                n = base * 8 + k + 1
                cat.upsert_item(mk(n, f"{n:04d}"))
                cat.append_log(f"c{n}", "started", {}, "t")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert cat.count() == 40
    assert len(cat.list_change_ids(limit=500)) == 40


def test_two_catalog_objects_one_file(tmp_path):
    p = tmp_path / "shared.sqlite3"
    a, b = Catalog(p), Catalog(p)
    a.upsert_item(mk(1, "9001"))
    assert b.get_by_plu("9001").id == 1
    with pytest.raises(DuplicatePluError):
        b.upsert_item(mk(2, "9001"))
    assert a.version() == b.version()
    a.close()
    b.close()


def test_reopen_keeps_everything(tmp_path):
    p = tmp_path / "x.sqlite3"
    c = Catalog(p)
    c.upsert_item(mk(1, field6=" "))
    c.mark_orphan_id(3, "FRONT")
    c.append_log("c", "started", {"k": "v"}, "t")
    v = c.version()
    c.close()
    c2 = Catalog(p)
    assert c2.get_by_id(1).field6 == " "
    assert c2.orphan_ids() == {3: ["FRONT"]}
    assert c2.read_log("c")[0]["payload"] == {"k": "v"}
    assert c2.version() == v
    c2.close()


def test_closed_catalog_raises(tmp_path):
    c = Catalog(tmp_path / "z.sqlite3")
    c.close()
    with pytest.raises(Exception):
        c.list_items()


# ---- import and config

def test_import_items_all_or_nothing(cat):
    items = [mk(1, "9001"), mk(2, "9002"), mk(3, "9001")]
    with pytest.raises(DuplicatePluError):
        cat.import_items(items)
    assert cat.count() == 0
    assert cat.import_items([mk(1), mk(2)]) == 2
    with pytest.raises(Exception):
        cat.import_items([mk(3)])  # not empty any more


def test_default_path_is_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SCALECONNECT_CATALOG", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_db_path() == tmp_path / ".config" / "scaleconnect" / "catalog.sqlite3"
    # Stage D, C2: a variable must not quietly move the catalog away from the one path rule.
    monkeypatch.setenv("SCALECONNECT_CATALOG", str(tmp_path / "other.db"))
    assert default_db_path() == tmp_path / ".config" / "scaleconnect" / "catalog.sqlite3"


def test_path_from_config(tmp_path):
    p = str(tmp_path / "c.db")
    assert db_path_from_config({"catalog_path": p}) == Path(p)
    assert db_path_from_config({"catalog": {"path": p}}) == Path(p)

    class Cfg:
        catalog_path = p

    assert db_path_from_config(Cfg()) == Path(p)
    Catalog.from_config({"catalog_path": p}).close()
    assert Path(p).exists()

