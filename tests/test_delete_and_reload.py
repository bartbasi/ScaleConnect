# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Delete by placeholder, gap filling, reload and frozen scales, against fake scales on localhost.

Made up items only. Each test reads what the fake scale received, not just what the service said.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scaleconnect import cli, errors, protocol
from scaleconnect.catalog import Catalog
from scaleconnect.config import ScaleConfig, Settings, load_config
from scaleconnect.errors import ProtocolError
from scaleconnect.models import Item, is_placeholder, is_reserved_plu, placeholder_record
from scaleconnect.scale import FROZEN_NOTE, ScaleClient
from service_env import SEED

STAMP = "2026-01-01 00:00:00"
SRC = Path(__file__).resolve().parents[1] / "src" / "scaleconnect"
DELETE_1002 = [{"action": "delete", "target_plu": "1002"}]


def row(item: Item) -> tuple[str, ...]:
    return tuple(item.to_fields())


def placeholder_row(item_id: int) -> tuple[str, ...]:
    return row(placeholder_record(item_id, STAMP))


def is_placeholder_on(fake, item_id: int) -> bool:
    return fake.item(item_id) is not None and is_placeholder(Item.from_fields(list(fake.item(item_id))))


def seed_of(count: int) -> list[Item]:
    return [Item(i, f"{1000 + i}", f"{1000 + i}", f"Test {i}", "10.00", timestamp=STAMP) for i in range(1, count + 1)]


def sent_ids_of(fake) -> list[int]:
    return [int(r[0]) for m in fake.messages_of("GP") for r in m.records]


def never_sent_an_e(fake) -> None:
    assert not any(m.raw.startswith(b"E") or m.raw.startswith(b"LG") for m in fake.messages)
    assert "FORBIDDEN" not in fake.kinds()


# ---------------------------------------------------------------- the placeholder itself


def test_the_placeholder_record_is_exactly_what_was_tested_on_the_scale():
    record = placeholder_record(486, "2026-11-02 10:00:00")
    assert record.to_fields() == [
        "486", "9486", "9486", "UNUSED", "0.01", "", "1", "1", "0", "0", "0", "0",
        "2026-11-02 10:00:00", "0", "0", "0", "0", "0", "0", "0",
    ]
    assert is_placeholder(record)


@pytest.mark.parametrize(
    "changes",
    [
        dict(plu="9487"),  # another id's placeholder PLU
        dict(description="Real Item"),
        dict(price="1.00"),
        dict(field6=" "),
        dict(fields_7_12=("0", "1", "0", "0", "0", "0")),
        dict(fields_14_20=("1", "0", "0", "0", "0", "0", "0")),
    ],
)
def test_only_an_exact_placeholder_counts_as_one(changes):
    assert not is_placeholder(placeholder_record(486, STAMP).replace(**changes))


@pytest.mark.parametrize("plu, reserved", [("9001", True), ("9500", True), ("9000", False), ("9501", False), ("1001", False), ("7262", False), ("", False), (None, False)])
def test_the_reserved_range_is_9001_to_9500(plu, reserved):
    assert is_reserved_plu(plu) is reserved


# ---------------------------------------------------------------- delete


def test_delete_verified_goes_through_the_steps_in_order(env):
    rec = env.change(DELETE_1002, ack=True)
    fake = env.fakes["A"]
    assert env.statuses(rec) == {"A": "verified"} and rec.success
    assert rec.scale_results[0].items[0].action == "delete"
    kinds = fake.kinds()
    assert kinds[0] == "CX" and kinds.index("LP") < kinds.index("RI")
    assert kinds[-1] in ("Ok", "LP") and kinds.count("GP") == 1
    assert kinds.index("GPF0") < len(kinds) - 1, "a read back follows the write"
    assert is_placeholder_on(fake, 2)
    assert fake.item(1) == row(SEED[0]) and fake.item(3) == row(SEED[2]), "other records stay as they were"
    assert env.catalog.get_by_plu("1002") is None
    assert env.catalog.scale_states(2)["A"]["belief"] == "absent"
    never_sent_an_e(fake)


def test_delete_reads_the_whole_table_after_the_write(make_env):
    seed = seed_of(12)
    env = make_env({"A": True}, seed=seed)
    rec = env.change([{"action": "delete", "target_plu": "1002"}], ack=True)
    assert rec.success
    fake = env.fakes["A"]
    after_write = fake.kinds()[fake.kinds().index("GPF0") + 1:]
    assert after_write.count("LP") == 1 and after_write.count("Ok") == 3, "all three blocks and the end of the table"


def test_delete_refused_on_a_different_product_and_no_token_helps(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[1].replace(plu="1777", code="1777", description="Another Product")))
    p = env.preview(DELETE_1002)
    forged = f"A:2:{'0' * 12}"
    rec = env.svc.apply_change(p.preview_id, True, acknowledge_drift=[forged])
    assert env.statuses(rec) == {"A": "drift_refused"} and not rec.success
    assert rec.scale_results[0].drift[0].kind == "different_product"
    assert rec.scale_results[0].drift[0].ack_token is None
    assert not fake.messages_of("GP") and fake.item(2)[2] == "1777"
    assert env.catalog.get_by_plu("1002") is not None


def test_delete_of_the_same_product_with_other_values_needs_the_drift_token(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[1].replace(price="21.00")))
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    token = rec.scale_results[0].drift[0].ack_token
    assert token
    p = env.preview(DELETE_1002)
    rec = env.svc.apply_change(p.preview_id, True, acknowledge_drift=[token])
    assert env.statuses(rec) == {"A": "verified"} and is_placeholder_on(fake, 2)


def test_delete_with_a_lost_reply_is_unknown_and_never_resent(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    fake.stall("GP", times=1, apply=True)
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "unknown"} and rec.outcome == "unknown" and not rec.success
    assert len(fake.messages_of("GP")) == 1, "never resent on its own"
    assert env.catalog.get_by_plu("1002") is not None, "the item stays listed, as nobody knows"
    assert 2 not in env.catalog.free_ids(500), "the id is not free"

    # The next attempt starts from the top, and the read shows it landed. A placeholder is no drift.
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "verified"} and rec.scale_results[0].drift == ()
    assert env.catalog.get_by_plu("1002") is None


def test_delete_with_a_lost_reply_that_did_not_land_can_be_tried_again(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    fake.stall("GP", times=1, apply=False)
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "unknown"} and fake.item(2) == row(SEED[1])
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "verified"} and is_placeholder_on(fake, 2)


def test_delete_when_the_read_back_fails_is_accepted_not_verified_and_the_id_stays_blocked(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.3, read_back_window_seconds=0.5, read_back_retry_seconds=0.1)
    fake = env.fakes["A"]
    # The first LP is the read first. The ones after it are the read back.
    fake.stall_read(block=0, times=None)
    fake.faults[-1].match = lambda m: m.kind == "LP" and fake.messages_of("GP")
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "accepted_not_verified"} and rec.outcome == "partial"
    assert FROZEN_NOTE in rec.scale_results[0].message
    assert env.catalog.scale_states(2)["A"]["belief"] == "unknown"
    assert 2 not in env.catalog.free_ids(500)

    # A later read that shows the placeholder is the confirmation.
    fake.clear_faults()
    assert env.svc.compare_scales()[0].status == "compared"
    assert env.catalog.scale_states(2)["A"]["belief"] == "absent"
    assert 2 in env.catalog.free_ids(500)


def test_delete_is_refused_if_the_old_plu_is_on_another_id_of_that_scale(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[1].replace(id=9)))
    fake.preload(*[placeholder_row(i) for i in range(4, 9)])
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    assert {d.kind for d in rec.scale_results[0].drift} == {"plu_on_other_id"}


def test_delete_is_a_mismatch_if_the_old_plu_turns_up_on_another_id_after_the_write(env):
    fake = env.fakes["A"]
    real = fake._handle

    def add_a_copy_on_finish(conn, raw):
        if raw.startswith(b"GPF0"):
            fake.preload(*[placeholder_row(i) for i in range(4, 9)], row(SEED[1].replace(id=9)))
        return real(conn, raw)

    fake._handle = add_a_copy_on_finish
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "mismatch"} and not rec.success
    assert "still on id 9" in rec.scale_results[0].items[0].message
    assert env.catalog.scale_states(2)["A"]["belief"] == "unknown"


def test_delete_of_an_id_this_scale_holds_nothing_at_still_leaves_a_placeholder(env):
    fake = env.fakes["A"]
    fake.remove(2)
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "verified"} and is_placeholder_on(fake, 2)


def test_delete_is_refused_when_its_placeholder_plu_is_taken_on_that_scale(env):
    fake = env.fakes["A"]
    fake.preload(row(Item(4, "9002", "9002", "Old Test", "1.00", timestamp=STAMP)))
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")


def test_a_delete_takes_the_backup_and_the_dump_first(make_env):
    env = make_env({"A": True})
    env.change(DELETE_1002, ack=True)
    names = [p.name for p in env.settings.backup_dir.iterdir()]
    assert any(n.endswith("-scale-dump.json") for n in names) and "first-write-items-A.marker" in names
    dump = next(p for p in env.settings.backup_dir.iterdir() if p.name.endswith("-scale-dump.json"))
    assert [r["plu"] for r in json.loads(dump.read_text(encoding="utf-8"))] == ["1001", "1002", "1003"]


def test_a_delete_on_a_scale_not_write_enabled_sends_nothing_and_holds_the_id(make_env):
    env = make_env({"A": True, "B": False})
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "skipped_not_write_enabled"}
    assert rec.outcome == "partial" and env.silent("B")
    assert env.catalog.scale_states(2)["A"]["belief"] == "absent"
    assert env.catalog.scale_states(2)["B"]["belief"] == "unknown"
    assert 2 not in env.catalog.free_ids(500)


def test_a_preview_that_reads_reports_drift_for_a_delete_and_sends_nothing(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[1].replace(plu="1777", code="1777")))
    p = env.preview(DELETE_1002, read_scales=True)
    assert [d.kind for d in p.drift] == ["different_product"] and not fake.messages_of("GP")


# ---------------------------------------------------------------- reserved PLUs


@pytest.mark.parametrize("plu", ["9001", "9250", "9500"])
def test_an_add_with_a_reserved_plu_is_refused(env, plu):
    p = env.svc.preview_changes([{"action": "add", "plu": plu, "description": "Nope", "price": "1.00"}])
    assert not p.ok and p.errors[0].code == "reserved_plu"
    assert env.silent("A")


@pytest.mark.parametrize("plu", ["9000", "9501"])
def test_the_plus_either_side_of_the_range_are_still_allowed(env, plu):
    assert env.svc.preview_changes([{"action": "add", "plu": plu, "description": "Fine", "price": "1.00"}]).ok


@pytest.mark.parametrize("change", [{"plu": "9001"}, {"code": "9001"}, {"plu": "9001", "code": "9001"}])
def test_an_edit_to_a_reserved_plu_is_refused(env, change):
    p = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", **change}])
    assert not p.ok and p.errors[0].code == "reserved_plu"


@pytest.mark.parametrize("action", [{"action": "delete"}, {"action": "edit", "price": "1.00"}])
def test_a_placeholder_cannot_be_edited_or_deleted(env, action):
    env.change(DELETE_1002, ack=True)
    p = env.svc.preview_changes([{**action, "target_plu": "9002"}])
    assert not p.ok and p.errors[0].code == "reserved_plu"


def test_an_edit_or_delete_of_an_id_that_only_holds_a_placeholder_finds_no_item(env):
    env.change(DELETE_1002, ack=True)
    for change in ({"action": "edit", "target_id": 2, "price": "1.00"}, {"action": "delete", "target_id": 2}):
        p = env.svc.preview_changes([change])
        assert not p.ok and p.errors[0].code == "item_not_found"


# ---------------------------------------------------------------- a placeholder is an empty slot


def test_a_placeholder_is_not_drift_or_an_orphan_in_a_comparison(env):
    fake = env.fakes["A"]
    fake.preload(placeholder_row(4), placeholder_row(5))
    result = env.svc.compare_scales()[0]
    assert result.status == "compared" and result.differences == ()
    assert "2 of them are placeholders" in result.message
    marks = env.svc.mark_orphans_from_read("A")
    assert marks.marked == () and env.svc.list_orphans() == {}
    assert env.svc.mark_orphans_from_read("A", ids=[4]).not_marked == (4,)


def test_a_placeholder_where_the_catalog_lists_an_item_shows_as_missing(env):
    env.fakes["A"].preload(placeholder_row(2))
    result = env.svc.compare_scales()[0]
    assert [(d.kind, d.item_id) for d in result.differences] == [("missing", 2)]


def test_a_placeholder_is_left_out_of_a_read(env):
    env.fakes["A"].preload(placeholder_row(2), placeholder_row(4))
    result = env.svc.read_scale_items()[0]
    assert [r.item.id for r in result.records] == [1, 3]
    assert result.items_read == 4
    assert env.svc.read_scale_items(ids=[2])[0].records == ()
    assert env.svc.get_item(plu="1002").scales["A"].last_read_found_nothing is True


def test_a_deleted_item_and_its_placeholder_are_not_in_search_or_the_list(env):
    env.change(DELETE_1002, ack=True)
    env.svc.compare_scales()
    assert env.svc.search_items("1002") == [] and env.svc.search_items("9002") == []
    assert env.svc.search_items("UNUSED") == []
    assert [i.id for i in env.svc.search_items("")] == [1, 3]
    assert [i.id for i in env.catalog.list_items()] == [1, 3]
    with pytest.raises(errors.ItemNotFound):
        env.svc.get_item(plu="9002")


def test_an_edit_over_a_placeholder_goes_as_a_new_item_there(env):
    fake = env.fakes["A"]
    fake.preload(placeholder_row(2))
    p = env.preview([{"action": "edit", "target_plu": "1002", "price": "21.00"}], read_scales=True)
    assert p.drift == () and "missing_on_scale" in {w.code for w in p.warnings}
    rec = env.svc.apply_change(p.preview_id)
    assert env.statuses(rec) == {"A": "verified"}
    assert fake.item(2)[1:5] == ("1002", "1002", "Test Brie", "21.00")


def test_settling_cannot_take_a_placeholder_into_the_catalog(make_env):
    env = make_env({"A": True})
    env.fakes["A"].preload(placeholder_row(2))
    p = env.svc.preview_settle_drift(scale="A", take="scale", plu="1002")
    assert not p.ok and p.errors[0].code == "settle_not_possible"
    assert env.catalog.get_by_plu("9002") is None


def test_settling_can_write_the_catalog_record_over_a_placeholder(make_env):
    env = make_env({"A": True})
    fake = env.fakes["A"]
    fake.preload(placeholder_row(2))
    p = env.svc.preview_settle_drift(scale="A", take="catalog", plu="1002")
    assert p.ok and p.drift[0].kind == "missing"
    rec = env.svc.apply_change(p.preview_id, acknowledge_drift=[p.drift[0].ack_token])
    assert env.statuses(rec) == {"A": "verified"} and fake.item(2) == row(SEED[1])


# ---------------------------------------------------------------- an orphan: no service path, and a one off


def test_the_service_has_no_way_to_put_a_placeholder_on_an_orphan(env):
    orphan = row(Item(4, "1777", "1777", "Old Test", "1.00", timestamp=STAMP))
    fake = env.fakes["A"]
    fake.preload(orphan)
    assert env.svc.mark_orphans_from_read("A").marked == (4,)
    for change in ({"action": "delete", "target_id": 4}, {"action": "delete", "target_plu": "1777"}):
        p = env.svc.preview_changes([change])
        assert not p.ok and p.errors[0].code == "item_not_found"
    assert not env.svc.preview_settle_drift(scale="A", take="catalog", id=4).ok
    assert env.svc.reload_scale("A", accept_differences=True, requested_by="test").left_above == (4,)
    assert fake.item(4) == orphan


def test_a_one_off_placeholder_on_an_orphan_leaves_compare_clean_once_the_note_is_cleared(env):
    fake = env.fakes["A"]
    fake.preload(row(Item(4, "1777", "1777", "Old Test", "1.00", timestamp=STAMP)))
    assert env.svc.mark_orphans_from_read("A").marked == (4,)
    assert 4 not in env.catalog.free_ids(500)
    version = env.catalog.version()

    client = ScaleClient(env.config.scales[0], env.settings, sleep=lambda s: None)
    assert client.send_items([placeholder_record(4, STAMP)]).items[0].status == "accepted_not_verified"
    assert is_placeholder(client.read_items(None, patient=True).items[4])

    assert env.svc.compare_scales()[0].differences == ()
    assert env.catalog.version() == version and env.catalog.scale_states(4) == {}
    assert 4 not in env.catalog.free_ids(500), "the note still holds the id"
    assert env.catalog.clear_orphan_id(4, "A") and env.svc.list_orphans() == {}
    assert env.catalog.free_ids(1) == [4]
    rec = env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    assert env.statuses(rec) == {"A": "verified"} and fake.item(4)[2] == "1888"


# ---------------------------------------------------------------- id reuse


def test_a_deleted_id_is_reused_once_the_one_scale_confirmed_it(env):
    fake = env.fakes["A"]
    env.change(DELETE_1002, ack=True)
    assert env.catalog.free_ids(1) == [2]
    p = env.preview([{"action": "add", "plu": "1888", "description": "New Item", "price": "2.00"}], read_scales=True)
    assert p.differences[0].item_id == 2 and p.drift == ()
    rec = env.svc.apply_change(p.preview_id)
    assert env.statuses(rec) == {"A": "verified"} and rec.success
    assert fake.item(2)[1:5] == ("1888", "1888", "New Item", "2.00")
    assert env.catalog.get_by_id(2).plu == "1888"
    assert env.catalog.scale_states(2)["A"]["belief"] == "present"


def test_the_old_plu_can_come_back_after_its_delete(env):
    env.change(DELETE_1002, ack=True)
    rec = env.change([{"action": "add", "plu": "1002", "description": "Back Again", "price": "2.00"}])
    assert env.statuses(rec) == {"A": "verified"} and env.fakes["A"].item(2)[3] == "Back Again"


def test_an_id_waits_for_every_scale_including_one_the_change_left_out(env3):
    rec = env3.change(DELETE_1002, ack=True, scales=["A", "B"])
    assert env3.statuses(rec) == {"A": "verified", "B": "verified"} and rec.not_touched == ("C",)
    assert env3.catalog.scale_states(2)["C"]["belief"] == "unknown"
    assert 2 not in env3.catalog.free_ids(500)
    p = env3.svc.preview_changes([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    assert p.differences[0].item_id == 4, "the new item does not get the id C may still hold"


def test_a_scale_that_missed_the_delete_can_be_finished_later_and_that_frees_the_id(env3):
    env3.change(DELETE_1002, ack=True, scales=["A", "B"])
    assert env3.fakes["C"].item(2) == row(SEED[1])
    rec = env3.change(DELETE_1002, ack=True, scales=["C"])
    assert env3.statuses(rec) == {"C": "verified"} and is_placeholder_on(env3.fakes["C"], 2)
    assert env3.catalog.scale_states(2)["C"]["belief"] == "absent"
    assert 2 in env3.catalog.free_ids(500)


def test_a_finished_delete_cannot_be_targeted_again(env):
    env.change(DELETE_1002, ack=True)
    p = env.svc.preview_changes(DELETE_1002)
    assert not p.ok and p.errors[0].code == "item_not_found"


def test_an_add_over_a_placeholder_the_catalog_never_knew_is_no_drift(env):
    fake = env.fakes["A"]
    fake.preload(placeholder_row(4))
    p = env.preview([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}], read_scales=True)
    assert p.differences[0].item_id == 4 and p.drift == ()
    rec = env.svc.apply_change(p.preview_id)
    assert env.statuses(rec) == {"A": "verified"} and fake.item(4)[2] == "1888"


# ---------------------------------------------------------------- never leave a gap


def fill_needed_on_b(make_env, *, b_extra=()):
    """A holds an orphan at id 8. B ends at id 3, and the catalog lists nothing at 4 to 7, so an add at 8 must fill there on B."""
    env = make_env({"A": True, "B": True}, seed=seed_of(3))
    env.fakes["A"].preload(*[placeholder_row(i) for i in range(4, 8)], row(Item(8, "1777", "1777", "Old Test", "1.00", timestamp=STAMP)))
    env.fakes["B"].preload(*b_extra)
    assert env.svc.mark_orphans_from_read("A").marked == (8,)
    return env, [{"action": "add", "plu": "1888", "description": "New", "price": "2.00", "replace_orphan_id": 8}]


def test_a_write_past_the_end_fills_the_ids_between_in_the_same_send(make_env):
    env, add = fill_needed_on_b(make_env)
    fake = env.fakes["B"]
    rec = env.change(add, ack=True, read_scales=True)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"}
    assert sent_ids_of(fake) == [4, 5, 6, 7, 8], "in id order, so a send that stops early leaves no gap"
    assert len(fake.messages_of("RI")) == 1, "one session"
    for i in (4, 5, 6, 7):
        assert is_placeholder_on(fake, i)
    assert fake.item(8)[2] == "1888"
    message = rec.scale_results[1].message
    assert "ids 4 to 7 got placeholders" in message and "confirmed" in message
    assert rec.scale_results[1].fillers_landed == (4, 5, 6, 7)


def test_a_preview_that_reads_names_the_scale_and_ids_it_would_fill(make_env):
    env, add = fill_needed_on_b(make_env)
    p = env.preview(add, read_scales=True)
    filled = [w for w in p.warnings if w.code == "gap_filled"]
    assert len(filled) == 1 and filled[0].requires_ack, "a real gap fill needs a yes"
    assert "B holds nothing at id 4 to 7" in filled[0].message
    assert not env.fakes["A"].messages_of("GP") and not env.fakes["B"].messages_of("GP")
    with pytest.raises(errors.AcknowledgementRequired):
        env.svc.apply_change(p.preview_id)
    assert not env.fakes["B"].messages_of("GP")


def test_a_preview_with_no_read_says_nothing_about_a_gap_and_apply_will_not_fill(make_env):
    env, add = fill_needed_on_b(make_env)
    p = env.preview(add)
    assert "gap_filled" not in {w.code for w in p.warnings}
    rec = env.svc.apply_change(p.preview_id, True)
    assert env.statuses(rec) == {"A": "verified", "B": "drift_refused"}
    assert [d.kind for d in rec.scale_results[1].drift] == ["fill_not_previewed"]
    assert rec.scale_results[1].drift[0].ack_token is None
    assert "no preview showed that" in rec.scale_results[1].message
    assert not env.fakes["B"].messages_of("GP")


def test_a_fill_larger_than_the_preview_showed_is_refused_too(make_env):
    env, add = fill_needed_on_b(make_env)
    p = env.preview(add, read_scales=True)
    env.fakes["B"].remove(3)  # the catalog expects 3 there, so this read now ends early
    rec = env.svc.apply_change(p.preview_id, True)
    assert env.statuses(rec)["B"] == "drift_refused" and not env.fakes["B"].messages_of("GP")


def test_a_preview_with_nothing_to_fill_says_nothing_about_a_gap(env):
    p = env.preview([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}], read_scales=True)
    assert "gap_filled" not in {w.code for w in p.warnings}


def test_a_preview_shows_a_filler_whose_plu_is_taken_as_drift(make_env):
    env, add = fill_needed_on_b(make_env, b_extra=[row(Item(3, "9005", "9005", "Old Test", "1.00", timestamp=STAMP))])
    p = env.preview(add, read_scales=True)
    assert [(d.scale, d.kind, d.item_id) for d in p.drift] == [("B", "plu_on_other_id", 5)]


def test_a_write_right_after_the_last_id_needs_no_filling(env):
    fake = env.fakes["A"]
    rec = env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    assert [r[0] for m in fake.messages_of("GP") for r in m.records] == ["4"]
    assert "placeholders" not in rec.scale_results[0].message


def test_a_filler_placeholder_that_does_not_land_stops_it_being_called_verified(make_env):
    env, add = fill_needed_on_b(make_env)
    fake = env.fakes["B"]
    real = fake._handle

    def lose_a_filler(conn, raw):
        result = real(conn, raw)
        if raw.startswith(b"GPF0"):
            fake.remove(6)
        return result

    fake._handle = lose_a_filler
    rec = env.change(add, ack=True, read_scales=True)
    result = rec.scale_results[1]
    assert result.status == "mismatch" and not rec.success and not rec.verified
    assert [d.item_id for d in result.mismatches] == [6]


def test_a_filler_refused_for_a_taken_plu_stops_the_whole_scale(make_env):
    env, add = fill_needed_on_b(make_env, b_extra=[row(Item(3, "9005", "9005", "Old Test", "1.00", timestamp=STAMP))])
    rec = env.change(add, ack=True, read_scales=True)
    assert env.statuses(rec)["B"] == "drift_refused" and not env.fakes["B"].messages_of("GP")


def test_fillers_that_landed_before_an_error_are_not_called_nothing_landed(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(3))
    env.fakes["A"].preload(*[placeholder_row(i) for i in range(4, 12)], row(Item(12, "1777", "1777", "Old Test", "1.00", timestamp=STAMP)))
    assert env.svc.mark_orphans_from_read("A").marked == (12,)
    fake = env.fakes["B"]
    fake.error_on_ids(12)  # the first batch of placeholders is kept. The batch with the record gets Error
    add = [{"action": "add", "plu": "1888", "description": "New", "price": "2.00", "replace_orphan_id": 12}]
    rec = env.change(add, ack=True, read_scales=True, scales=["B"])
    result = rec.scale_results[0]
    assert sorted(fake.items)[:8] == [1, 2, 3, 4, 5, 6, 7, 8] and result.fillers_landed == (4, 5, 6, 7, 8)
    assert result.status != "verified" and rec.outcome == "partial", "not failed, because placeholders landed"
    assert "B took placeholders at id 4 to 8" in rec.display

def test_a_gap_already_on_the_table_is_reported_and_left_alone(make_env):
    env = make_env({"A": True}, seed=seed_of(6))
    fake = env.fakes["A"]
    fake.remove(3)
    rec = env.change([{"action": "edit", "target_plu": "1005", "price": "12.00"}], read_scales=False)
    assert env.statuses(rec) == {"A": "verified"}
    assert [r[0] for m in fake.messages_of("GP") for r in m.records] == ["5"]
    assert "nothing at id 3" in rec.scale_results[0].message and "reload" in rec.scale_results[0].message


# ---------------------------------------------------------------- reload


def reload_of(env, name="A"):
    return env.svc.reload_scale(name, accept_differences=True, requested_by="test")


def test_reload_on_an_empty_scale_sends_everything_with_no_read_first(make_env):
    env = make_env({"A": True}, preload=False)
    fake = env.fakes["A"]
    result = reload_of(env)
    assert result.ok and result.status == "verified" and result.top_id == 3
    assert result.accepted == 3 and result.placeholders == ()
    kinds = fake.kinds()
    first_write = kinds.index("RI")
    assert kinds[:first_write] == ["CX"], "identity only, and no read before the send"
    assert kinds.index("LP") > kinds.index("GPF0")
    assert {i: fake.item(i) for i in (1, 2, 3)} == {i.id: row(i) for i in SEED}, "exactly what the catalog holds"
    assert fake.messages_of("LP") and len(fake.messages_of("LP")) == 1, "one read to verify"
    assert fake.messages_of("RI") and len(fake.messages_of("RI")) == 1
    never_sent_an_e(fake)


def test_reload_on_a_scale_whose_read_would_freeze_still_sends(make_env):
    env = make_env({"A": True}, seed=seed_of(6), preload=False, fake_options=dict(freeze_on_gap=True))
    fake = env.fakes["A"]
    fake.preload(row(seed_of(6)[0].replace(id=4, plu="1777", code="1777")))  # one item, and an empty start
    result = reload_of(env)
    assert result.status == "verified" and result.ok, result.message
    assert fake.freezes == 0, "the send cured the gap before anything read it"
    assert fake.item(4) == row(seed_of(6)[3])


def test_reload_uses_placeholders_for_deleted_ids_and_fills_every_gap(make_env):
    env = make_env({"A": True}, seed=seed_of(6))
    env.change([{"action": "delete", "target_id": 3}, {"action": "delete", "target_id": 6}], ack=True)
    fake = env.fakes["A"]
    fake.set_table([])
    fake.messages.clear()
    result = reload_of(env)
    assert result.ok and result.placeholders == (3, 6) and result.top_id == 6
    assert fake.accepted_ids() == [1, 2, 3, 4, 5, 6]
    assert is_placeholder_on(fake, 3) and is_placeholder_on(fake, 6)
    assert env.catalog.scale_states(3)["A"]["belief"] == "absent"
    assert env.catalog.scale_states(1)["A"]["belief"] == "present"
    assert "Id 3, 6 got placeholders" in result.message


def test_reload_frees_an_id_that_was_waiting_on_this_scale(env3):
    env3.change(DELETE_1002, ack=True, scales=["A", "B"])
    assert 2 not in env3.catalog.free_ids(500)
    assert reload_of(env3, "C").ok
    assert is_placeholder_on(env3.fakes["C"], 2) and 2 in env3.catalog.free_ids(500)


def test_reload_repairs_a_scale_whose_ids_differ_from_the_catalog(env):
    fake = env.fakes["A"]
    fake.set_table([row(SEED[2].replace(id=1)), row(SEED[0].replace(id=2)), row(SEED[1].replace(id=3))])
    result = reload_of(env)
    assert result.ok and {i: fake.item(i) for i in (1, 2, 3)} == {i.id: row(i) for i in SEED}
    assert env.svc.compare_scales()[0].differences == ()


def test_reload_says_what_it_left_above_the_highest_id(env):
    fake = env.fakes["A"]
    fake.preload(*[placeholder_row(i) for i in range(4, 8)], row(Item(8, "1777", "1777", "Old Item", "1.00", timestamp=STAMP)))
    result = reload_of(env)
    assert result.ok and result.left_above == (8,) and "ids above 3" in result.message
    assert fake.item(8)[2] == "1777", "a reload overwrites ids 1 to N and nothing else"


def test_reload_flags_a_plu_left_on_two_ids(env):
    fake = env.fakes["A"]
    fake.preload(*[placeholder_row(i) for i in range(4, 8)], row(SEED[0].replace(id=8)))
    result = reload_of(env)
    assert result.status == "mismatch" and not result.ok and result.duplicate_plus == ("1001",)
    record = env.svc.get_change(result.change_id)
    assert not record.verified and not record.success, "a PLU on two ids is not verified, in the log either"


def test_reload_does_not_change_the_catalog(env):
    before = env.catalog.version(), [i.to_fields() for i in env.catalog.list_items()]
    assert reload_of(env).ok
    assert before == (env.catalog.version(), [i.to_fields() for i in env.catalog.list_items()])


def test_reload_is_refused_on_a_scale_not_write_enabled(make_env):
    env = make_env({"A": False})
    with pytest.raises(errors.ValidationFailed):
        reload_of(env)
    assert env.silent("A")


def test_reload_is_refused_for_an_unknown_scale(env):
    with pytest.raises(errors.ValidationFailed):
        reload_of(env, "NOPE")


def test_reload_on_the_wrong_scale_sends_nothing_but_the_identity_check(make_env):
    env = make_env({"A": True})
    fake = env.fakes["A"]
    fake.serial = "SOME-OTHER"
    result = reload_of(env)
    assert result.status == "wrong_scale" and not result.ok and fake.kinds() == ["CX"]


def test_reload_takes_the_item_list_backup_first(make_env):
    calls = []
    env = make_env({"A": True}, backup_hook=lambda scale, items, d: calls.append((scale, len(items))))
    assert reload_of(env).ok
    assert calls == [("A", 3)]
    started = next(e for e in env.catalog.read_log(env.svc.list_scales()[0].last_change_id) if e["event"] == "started")
    assert started["payload"]["scale_dump"] == "not_taken_no_read_before_a_reload"


def test_reload_refuses_a_failed_backup_and_sends_nothing(make_env):
    def bad(scale, items, d):
        raise OSError("no disk")

    env = make_env({"A": True}, backup_hook=bad)
    with pytest.raises(errors.BackupFailed):
        reload_of(env)
    assert env.silent("A")


def test_reload_refuses_a_catalog_record_that_breaks_the_rules(make_env):
    env = make_env({"A": True}, seed=[Item(1, "1001", "1001", "Bad Price", "5", timestamp=STAMP)])
    with pytest.raises(errors.ValidationFailed) as caught:
        reload_of(env)
    assert "id 1" in caught.value.message and env.silent("A")


def test_reload_refuses_while_a_change_is_interrupted(env):
    env.catalog.append_log("dead", "started", {"changes": [{"item_id": 1}]}, "2026-01-01T00:00:00+00:00")
    with pytest.raises(errors.InterruptedChange):
        reload_of(env)
    assert env.silent("A")


def test_reload_stops_at_an_error_reads_nothing_and_says_so(make_env):
    env = make_env({"A": True}, seed=seed_of(8), preload=False)
    fake = env.fakes["A"]
    fake.error_on_ids(7)
    result = reload_of(env)
    assert result.status == "unknown" and not result.ok
    assert result.accepted == 5 and result.not_accepted == (6, 7, 8)
    assert "LP" not in fake.kinds(), "no read unless every record was accepted"
    assert "accepted 5 of 8" in result.message


def test_reload_with_a_lost_reply_is_unknown_is_not_resent_and_reads_nothing(make_env):
    env = make_env({"A": True}, seed=seed_of(8), preload=False, reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    fake.stall("GP", times=1)
    result = reload_of(env)
    assert result.status == "unknown" and not result.ok and len(fake.messages_of("GP")) == 1
    assert "LP" not in fake.kinds()


def test_reload_when_the_read_freezes_says_reboot_and_does_not_keep_trying(make_env):
    env = make_env(
        {"A": True}, seed=seed_of(12), preload=False, reply_timeout_seconds=0.3,
        read_back_window_seconds=0.2, read_back_retry_seconds=0.05, fake_options=dict(freeze_lp_at_id=7),
    )
    fake = env.fakes["A"]
    result = reload_of(env)
    assert result.status == "accepted_not_verified" and not result.ok
    assert "may be frozen" in result.message and "reboot" in result.message and "stopped trying" in result.message and result.may_be_frozen
    assert fake.frozen and len(fake.messages_of("LP")) == 1, "once blocks have come, a stall ends it"
    assert env.catalog.scale_states(1)["A"]["belief"] == "unknown"


def test_reload_is_logged_and_can_be_looked_up(env):
    result = reload_of(env)
    record = env.svc.get_change(result.change_id)
    assert record.outcome == "success" and record.verified and record.display == result.message
    assert [c.action for c in record.changes] == ["reload"] * 3
    assert record.scale_results[0].items[0].status == "verified"


def test_a_scale_left_out_keeps_a_reload_from_being_called_a_success_in_the_log(env3):
    result = reload_of(env3, "A")
    record = env3.svc.get_change(result.change_id)
    assert record.outcome == "partial" and record.not_touched == ("B", "C")
    assert env3.silent("B", "C")


def test_reload_is_only_on_the_command_line():
    for path in [SRC / "mcp_server.py", *(SRC / "web").glob("*.py")]:
        assert "reload_scale" not in path.read_text(encoding="utf-8"), path.name


# ---------------------------------------------------------------- reload on the command line


def write_config(tmp_path, fake, enabled="true"):
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[settings]
state_dir = "{(tmp_path / 'state').as_posix()}"
reply_timeout_seconds = 1
connect_timeout_seconds = 1
connect_attempts = 1
connect_retry_wait_seconds = 0
read_pace_seconds = 0
read_back_window_seconds = 2
read_back_retry_seconds = 0.05
scalelink_db_copy = "not_available_here"

[[scales]]
name = "A"
address = "127.0.0.1"
port = {fake.port}
serial = "{fake.serial}"
write_enabled = {enabled}
""",
        encoding="utf-8",
    )
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.import_items(SEED)
    return str(path)


def run_cli(capsys, *args):
    code = cli.main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_cli_reload_refuses_without_yes(tmp_path, make_fake, capsys):
    fake = make_fake()
    code, out = run_cli(capsys, "--config", write_config(tmp_path, fake), "reload", "--scale", "A")
    assert code == 2 and out["error"]["code"] == "usage" and "overwrites ids 1 to N" in out["error"]["message"]
    assert fake.nothing_received


def test_cli_reload_refuses_a_scale_that_is_not_write_enabled(tmp_path, make_fake, capsys):
    fake = make_fake()
    code, out = run_cli(capsys, "--config", write_config(tmp_path, fake, "false"), "reload", "--scale", "A", "--yes")
    assert code == 3 and out["error"]["code"] == "validation_failed"
    assert fake.nothing_received


def test_cli_reload_runs_and_reports(tmp_path, make_fake, capsys):
    fake = make_fake()
    code, out = run_cli(capsys, "--config", write_config(tmp_path, fake), "reload", "--scale", "A", "--yes", "--accept-differences", "--who", "manager")
    assert code == 0 and out["ok"] and out["status"] == "verified" and out["top_id"] == 3
    assert "Ids 1 to 3 now match the catalog" in out["message"]
    assert fake.accepted_ids() == [1, 2, 3]
    assert fake.kinds()[0] == "CX" and fake.kinds()[1] == "RI"


def test_cli_reload_that_is_not_verified_exits_7(tmp_path, make_fake, capsys):
    fake = make_fake(freeze_lp_at_id=1)
    code, out = run_cli(capsys, "--config", write_config(tmp_path, fake), "reload", "--scale", "A", "--yes", "--accept-differences")
    assert code == 7 and out["status"] == "accepted_not_verified" and "reboot" in out["message"]
    fake.reboot()


def test_cli_help_says_what_reload_overwrites(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["reload", "--help"])
    assert caught.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "overwrite ids 1 to N" in text and "No read comes first" in text


# ---------------------------------------------------------------- frozen scales


def client_for(fake, **kw):
    base = dict(reply_timeout_seconds=0.3, connect_timeout_seconds=0.3, connect_attempts=2, connect_retry_wait_seconds=0.0,
                read_pace_seconds=0.0, read_back_window_seconds=1.0, read_back_retry_seconds=0.1)
    base.update(kw)
    config = ScaleConfig("A", fake.host, fake.port, True, serial=fake.serial)
    return ScaleClient(config, Settings(**base), sleep=lambda s: None)


def test_the_fake_freezes_at_the_block_holding_that_id_and_drops_off(make_fake):
    fake = make_fake(freeze_lp_at_id=7)
    fake.preload(*[row(i) for i in seed_of(12)])
    result = client_for(fake).read_items()
    assert not result.ok and result.status == "read_failed" and FROZEN_NOTE in result.message
    assert fake.frozen and fake.freezes == 1
    assert fake.kinds().count("LP") == 1 and fake.kinds().count("Ok") == 1, "the second block hung"
    again = client_for(fake).read_items()
    assert not again.ok and again.status == "unreachable" and FROZEN_NOTE in again.message
    assert fake.kinds().count("LP") == 1, "nothing gets through until a reboot"
    fake.freeze_lp_at_id = None
    fake.reboot()
    assert client_for(fake).read_items().ok


def test_the_fake_freezes_on_a_gap_and_on_an_empty_start(make_fake):
    gap = make_fake(freeze_on_gap=True)
    gap.preload(*[row(i) for i in seed_of(12) if i.id != 8])
    assert not client_for(gap).read_items().ok and gap.frozen
    start = make_fake(freeze_on_gap=True)
    start.preload(row(Item(65, "1065", "1065", "Lone Item", "1.00", timestamp=STAMP)))
    assert not client_for(start).read_items().ok and start.frozen


def test_a_table_with_no_gap_and_empty_slots_after_it_reads_fine(make_fake):
    fake = make_fake(freeze_on_gap=True)
    fake.preload(*[row(i) for i in seed_of(12)])
    result = client_for(fake).read_items()
    assert result.ok and result.complete and not fake.frozen


def test_a_read_up_to_an_id_before_the_gap_does_not_freeze(make_fake):
    fake = make_fake(freeze_on_gap=True)
    fake.preload(*[row(i) for i in seed_of(12) if i.id != 8])
    assert client_for(fake).read_items(up_to_id=3).ok and not fake.frozen


def test_a_read_gets_no_reply_says_it_may_have_frozen(make_fake):
    fake = make_fake()
    fake.preload(*[row(i) for i in seed_of(3)])
    fake.stall_read(times=None)
    result = client_for(fake).read_items()
    assert not result.ok and FROZEN_NOTE in result.message


def test_a_bad_reply_does_not_say_it_may_have_frozen(make_fake):
    fake = make_fake()
    fake.preload(*[row(i) for i in seed_of(3)])
    fake.raw_reply(b"XX99\t\n0\r", kind="LP", times=None)
    result = client_for(fake).read_items()
    assert not result.ok and FROZEN_NOTE not in result.message


def test_an_identity_with_no_reply_says_it_may_have_frozen(make_fake):
    fake = make_fake()
    fake.stall("CX", times=None)
    identity = client_for(fake).identify()
    assert not identity.ok and FROZEN_NOTE in identity.message


def test_a_read_back_with_no_reply_says_it_may_have_frozen_and_stays_in_its_window(make_fake):
    fake = make_fake()
    fake.preload(*[row(i) for i in seed_of(3)])
    fake.stall_read(times=None)
    client = client_for(fake, reply_timeout_seconds=0.2, read_back_window_seconds=0.5, read_back_retry_seconds=0.05)
    client._identity_confirmed = True
    result = client.read_items(patient=True)
    assert not result.ok and FROZEN_NOTE in result.message
    # Each try waits 0.2 s for a reply, and the window is 0.5 s, so it can only have a few tries.
    assert fake.kinds().count("LP") <= 4


def test_a_read_that_stalls_once_blocks_have_come_is_not_tried_again_even_in_the_window(make_fake):
    fake = make_fake(freeze_lp_at_id=7)
    fake.preload(*[row(i) for i in seed_of(12)])
    client = client_for(fake)
    client._identity_confirmed = True
    assert not client.read_items(patient=True).ok
    assert fake.kinds().count("LP") == 1


def test_a_stall_on_an_ordinary_read_sends_one_lp_only(make_fake):
    fake = make_fake()
    fake.preload(*[row(i) for i in seed_of(3)])
    fake.stall_read(times=None)
    assert not client_for(fake, connect_attempts=5).read_items().ok
    assert fake.kinds().count("LP") == 1


def test_the_first_write_to_a_frozen_table_reads_once_and_writes_nothing(make_env):
    env = make_env({"A": True}, seed=seed_of(12), fake_options=dict(freeze_lp_at_id=7), reply_timeout_seconds=0.3)
    fake = env.fakes["A"]
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "12.00"}])
    assert env.statuses(rec) == {"A": "read_failed"}
    assert FROZEN_NOTE in rec.scale_results[0].message and "Nothing was written" in rec.scale_results[0].message
    assert not fake.messages_of("GP") and fake.kinds().count("LP") == 1
    assert env.catalog.get_by_plu("1001").price == "10.00"
    fake.reboot()


# ---------------------------------------------------------------- no message starting with E


def test_no_message_starting_with_e_ever_goes_out_in_any_of_these_flows(make_env):
    env = make_env({"A": True}, seed=seed_of(8))
    env.change([{"action": "delete", "target_plu": "1002"}], ack=True)
    env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    env.change([{"action": "edit", "target_plu": "1003", "price": "11.00"}])
    env.svc.compare_scales()
    env.svc.read_scale_items()
    env.svc.mark_orphans_from_read("A")
    reload_of(env)
    fake = env.fakes["A"]
    never_sent_an_e(fake)
    assert set(fake.kinds()) <= {"RI", "bX01XX", "GP", "GPF0", "LP", "Ok", "CX"}
    assert all(m.check_ok for m in fake.messages)


@pytest.mark.parametrize("text", ["EP", "EP01", "EP01486", "E", "EX", "EP\t486"])
def test_the_library_cannot_frame_an_e_message(text):
    with pytest.raises(ProtocolError):
        protocol._frame(text)


def test_no_builder_makes_an_e_message():
    for name in dir(protocol):
        if name.startswith("build_") and name != "build_item_batch":
            assert not getattr(protocol, name)().startswith(b"E"), name
