"""An explicit edit may change a PLU, and Code follows it. Fake scales on localhost, made up items only.

Each test reads what the fake scale received, not just what the service said.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from scaleconnect.catalog import Catalog
from scaleconnect.errors import AcknowledgementRequired
from scaleconnect.models import Item, placeholder_record
from service_env import SEED

STAMP = "2026-01-01 00:00:00"
MOVE = [{"action": "edit", "target_plu": "1001", "plu": "1999"}]
OLD_RECORD = tuple(SEED[0].to_fields())


def row(item: Item) -> tuple[str, ...]:
    return tuple(item.to_fields())


def plus_on(fake) -> dict[int, str]:
    return {i: r[2] for i, r in fake.items.items()}


def sent_ids(fake) -> list[int]:
    return [int(r[0]) for m in fake.messages_of("GP") for r in m.records]


# ---------------------------------------------------------------- the edit


def test_a_plu_edit_replaces_the_record_at_the_same_id_and_code_follows(env):
    fake = env.fakes["A"]
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified"} and rec.success
    held = fake.item(1)
    assert held[1:3] == ("1999", "1999")
    assert held[3:12] == OLD_RECORD[3:12], "every other field stays, including field 6"
    assert held[13:] == OLD_RECORD[13:] and held[12] != OLD_RECORD[12]
    item = env.catalog.get_by_plu("1999")
    assert item.id == 1 and item.code == "1999" and env.catalog.get_by_plu("1001") is None
    assert env.catalog.scale_states(1)["A"]["belief"] == "present"
    assert not any(m.raw.startswith(b"E") for m in fake.messages)


def test_the_preview_shows_the_plu_and_code_changing(env):
    p = env.preview(MOVE)
    assert p.differences[0].fields["plu"].to_dict() == {"before": "1001", "after": "1999"}
    assert p.differences[0].fields["code"].to_dict() == {"before": "1001", "after": "1999"}


def test_a_plu_change_needs_acknowledging_and_says_the_old_plu_stops_working(env):
    fake = env.fakes["A"]
    p = env.preview(MOVE)
    assert [(w.code, w.requires_ack, w.index) for w in p.warnings] == [("plu_change", True, 0)]
    assert "PLU 1001 will stop working on the scales once this runs" in p.warnings[0].message
    assert "PLU 1999" in p.warnings[0].message
    with pytest.raises(AcknowledgementRequired):
        env.svc.apply_change(p.preview_id)
    assert not fake.messages_of("GP") and fake.item(1)[2] == "1001"
    rec = env.svc.apply_change(p.preview_id, True)
    assert env.statuses(rec) == {"A": "verified"} and fake.item(1)[2] == "1999"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"target_plu": "1001", "price": "11.00"}, id="a price edit"),
        pytest.param({"target_plu": "1001", "description": "New Name"}, id="a description edit"),
        pytest.param({"target_plu": "1001", "plu": "1001"}, id="naming the PLU it already has"),
    ],
)
def test_an_edit_that_keeps_the_plu_gets_no_plu_warning(env, change):
    assert "plu_change" not in {w.code for w in env.preview([{"action": "edit", **change}]).warnings}


def test_matching_an_odd_code_to_the_plu_gets_no_plu_warning(make_env):
    env = make_env({"A": True}, seed=[Item(1, "1002", "1001", "Odd One", "10.00", timestamp=STAMP)])
    assert "plu_change" not in {w.code for w in env.preview([{"action": "edit", "target_plu": "1001", "code": "1001"}]).warnings}


def test_a_plu_edit_can_also_change_the_price(env):
    rec = env.change([{"action": "edit", "target_plu": "1001", "plu": "1999", "price": "11.00"}], ack=True)
    assert env.statuses(rec) == {"A": "verified"}
    assert env.fakes["A"].item(1)[2:5] == ("1999", "Test Cheddar", "11.00")


def test_giving_the_matching_code_too_is_fine(env):
    rec = env.change([{"action": "edit", "target_id": 1, "plu": "1999", "code": "1999"}], ack=True)
    assert env.statuses(rec) == {"A": "verified"} and env.fakes["A"].item(1)[1:3] == ("1999", "1999")


def test_it_reads_the_whole_table_before_and_after(make_env):
    seed = [Item(i, f"{1000 + i}", f"{1000 + i}", f"Test {i}", "10.00", timestamp=STAMP) for i in range(1, 13)]
    env = make_env({"A": True}, seed=seed)
    env.change(MOVE, ack=True)  # the first write, which reads it all for rule 5 anyway
    fake = env.fakes["A"]
    fake.messages.clear()
    env.change([{"action": "edit", "target_plu": "1999", "plu": "1888"}], ack=True)
    kinds = fake.kinds()
    assert kinds[: kinds.index("RI")].count("Ok") == 3, "all three blocks, and the end, before the write"
    assert kinds[kinds.index("GPF0"):].count("Ok") == 3, "and again after it"


def test_an_item_whose_code_differs_gets_them_matched_by_giving_the_code(make_env):
    env = make_env({"A": True}, seed=[Item(1, "1002", "1001", "Odd One", "10.00", timestamp=STAMP)])
    refused = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    assert not refused.ok and refused.errors[0].code == "code_plu_mismatch"
    rec = env.change([{"action": "edit", "target_plu": "1001", "code": "1001"}])
    assert env.statuses(rec) == {"A": "verified"} and env.fakes["A"].item(1)[1:3] == ("1001", "1001")


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "change, code",
    [
        ({"plu": "1999", "code": "1998"}, "code_plu_mismatch"),
        ({"code": "1999"}, "code_plu_mismatch"),
        ({"plu": "1002"}, "duplicate_plu"),
        ({"plu": "9001"}, "reserved_plu"),
        ({"plu": "9500"}, "reserved_plu"),
        ({"plu": "199"}, "invalid_plu"),
        ({"plu": "19999"}, "invalid_plu"),
        ({"plu": "0999"}, "invalid_plu"),
    ],
)
def test_refused_at_preview_with_nothing_sent(env, change, code):
    p = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", **change}])
    assert not p.ok and code in {e.code for e in p.errors}
    assert env.silent("A")


def test_two_edits_cannot_land_on_one_plu(env):
    p = env.svc.preview_changes([
        {"action": "edit", "target_plu": "1001", "plu": "1999"},
        {"action": "edit", "target_plu": "1002", "plu": "1999"},
    ])
    assert not p.ok and "duplicate_plu" in {e.code for e in p.errors}


def test_a_new_plu_on_another_id_of_that_scale_is_refused_and_no_token_helps(env):
    fake = env.fakes["A"]
    fake.preload(row(Item(4, "1999", "1999", "Stray", "1.00", timestamp=STAMP)))
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    assert [d.kind for d in rec.scale_results[0].drift] == ["plu_on_other_id"]
    assert rec.scale_results[0].drift[0].ack_token is None
    assert env.catalog.get_by_plu("1001") is not None and env.catalog.get_by_plu("1999") is None


def test_a_different_product_at_that_id_is_refused_and_no_token_helps(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[0].replace(plu="1777", code="1777")))
    p = env.preview(MOVE)
    rec = env.svc.apply_change(p.preview_id, True, acknowledge_drift=[f"A:1:{'0' * 12}"])
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    assert rec.scale_results[0].drift[0].kind == "different_product"
    assert rec.scale_results[0].drift[0].ack_token is None


def test_the_old_plu_on_another_id_is_refused_first(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[0].replace(id=4)))
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    assert {d.kind for d in rec.scale_results[0].drift} == {"plu_on_other_id"}


def test_the_same_product_with_other_values_needs_its_token_and_keeps_them(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[0].replace(price="12.00")))
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    token = rec.scale_results[0].drift[0].ack_token
    p = env.preview(MOVE)
    rec = env.svc.apply_change(p.preview_id, True, acknowledge_drift=[token])
    assert env.statuses(rec) == {"A": "verified"}
    assert fake.item(1)[2] == "1999" and fake.item(1)[4] == "12.00", "the scale's own price stays"
    assert [d.kind for d in rec.still_differ] == ["still_differs"]


# ---------------------------------------------------------------- the read back


def test_the_old_plu_turning_up_on_another_id_is_a_mismatch(env):
    fake = env.fakes["A"]
    real = fake._handle

    def add_a_copy_on_finish(conn, raw):
        if raw.startswith(b"GPF0"):
            fake.preload(row(SEED[0].replace(id=4)))
        return real(conn, raw)

    fake._handle = add_a_copy_on_finish
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "mismatch"} and not rec.success
    assert "still on id 4" in rec.scale_results[0].items[0].message
    assert rec.scale_results[0].mismatches[0].plu == "1001"


def test_the_id_holding_something_else_after_the_write_is_a_mismatch(env):
    fake = env.fakes["A"]
    real = fake._handle

    def put_it_back_on_finish(conn, raw):
        result = real(conn, raw)
        if raw.startswith(b"GPF0"):
            fake.preload(OLD_RECORD)
        return result

    fake._handle = put_it_back_on_finish
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "mismatch"} and not rec.success
    assert env.catalog.scale_states(1)["A"]["belief"] == "unknown"


def test_a_scale_that_could_not_be_read_back_is_accepted_not_verified(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.3, read_back_window_seconds=0.4, read_back_retry_seconds=0.1)
    fake = env.fakes["A"]
    fake.stall_read(block=0, times=None)
    fake.faults[-1].match = lambda m: m.kind == "LP" and fake.messages_of("GP")
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "accepted_not_verified"} and rec.outcome == "partial"
    assert not rec.success


# ---------------------------------------------------------------- landing on one scale and not another


def rejected_on_b(env3):
    env3.fakes["B"].error_on_ids(1)
    rec = env3.change(MOVE, ack=True)
    assert env3.statuses(rec) == {"A": "verified", "B": "rejected", "C": "verified"}
    assert rec.outcome == "partial"
    env3.fakes["B"].clear_faults()
    return rec


@pytest.mark.parametrize(
    "retry",
    [
        pytest.param({"target_plu": "1001", "plu": "1999"}, id="the same edit again"),
        pytest.param({"target_plu": "1999", "plu": "1999"}, id="naming the new plu"),
        pytest.param({"target_id": 1, "plu": "1999"}, id="naming the id"),
    ],
)
def test_a_retry_works_with_one_scale_on_the_old_plu_and_others_on_the_new(env3, retry):
    rejected_on_b(env3)
    assert env3.catalog.get_by_id(1).plu == "1999", "at least one scale took it, so the catalog moved"
    assert env3.fakes["B"].item(1)[2] == "1001"

    p = env3.preview([{"action": "edit", **retry}], read_scales=True)
    assert p.drift == (), "neither end of the change is a different product"
    assert [w.code for w in p.warnings] == ["no_change"], "this was acknowledged the first time, and no_change covers it"
    rec = env3.svc.apply_change(p.preview_id)
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"} and rec.success
    for fake in env3.fakes.values():
        assert fake.item(1)[1:3] == ("1999", "1999")
        assert "1001" not in plus_on(fake).values()
    assert env3.catalog.old_plus_in_use() == {}


def test_a_retry_that_also_changes_a_field_warns_because_no_change_does_not_cover_it(env3):
    rejected_on_b(env3)
    p = env3.preview([{"action": "edit", "target_plu": "1999", "plu": "1999", "price": "11.00"}])
    assert [w.code for w in p.warnings] == ["plu_change"]
    assert "PLU 1001 will stop working" in p.warnings[0].message
    rec = env3.svc.apply_change(p.preview_id, True)
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    assert env3.fakes["B"].item(1)[2:5] == ("1999", "Test Cheddar", "11.00")


def test_a_retry_when_the_catalog_has_not_moved_warns_again(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1, apply=False)
    assert env.statuses(env.change(MOVE, ack=True)) == {"A": "unknown"}
    assert [w.code for w in env.preview(MOVE).warnings] == ["plu_change"]


def test_a_retry_to_just_the_scale_that_missed_it(env3):
    rejected_on_b(env3)
    a_before = len(env3.fakes["A"].messages_of("GP"))
    rec = env3.change(MOVE, ack=True, scales=["B"])
    assert env3.statuses(rec) == {"B": "verified"} and rec.not_touched == ("A", "C")
    assert env3.fakes["B"].item(1)[2] == "1999"
    assert len(env3.fakes["A"].messages_of("GP")) == a_before
    assert env3.catalog.old_plus_in_use() == {}


def test_a_retry_after_a_lost_reply_where_the_record_had_landed(make_env):
    env = make_env({"A": True, "B": True}, reply_timeout_seconds=0.4)
    env.fakes["B"].stall("GP", times=1, apply=True)
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "unknown"}
    assert env.fakes["B"].item(1)[2] == "1999", "it landed, and nobody knows that yet"
    rec = env.change(MOVE)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"} and rec.scale_results[1].drift == ()


def test_a_retry_after_a_lost_reply_where_the_record_had_not_landed(make_env):
    env = make_env({"A": True, "B": True}, reply_timeout_seconds=0.4)
    env.fakes["B"].stall("GP", times=1, apply=False)
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "unknown"}
    assert env.fakes["B"].item(1)[2] == "1001"
    rec = env.change(MOVE)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"}
    assert env.fakes["B"].item(1)[2] == "1999"


@pytest.mark.parametrize("landed", [True, False])
def test_a_retry_when_no_scale_is_known_to_have_taken_it(make_env, landed):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    fake.stall("GP", times=1, apply=landed)
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "unknown"} and rec.outcome == "unknown"
    assert env.catalog.get_by_plu("1001") is not None, "nothing is known to have landed, so the catalog stays"
    assert len(fake.messages_of("GP")) == 1, "never resent on its own"
    assert (fake.item(1)[2] == "1999") is landed

    # The catalog still says 1001. If the scale holds 1999, that is this edit having landed.
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified"} and rec.scale_results[0].drift == ()
    assert fake.item(1)[2] == "1999" and env.catalog.get_by_plu("1999").id == 1


def test_an_unconfirmed_scale_holding_the_new_plu_with_other_values_needs_its_token(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    fake.preload(row(SEED[0].replace(plu="1999", code="1999", price="12.00")))
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    finding = rec.scale_results[0].drift[0]
    assert finding.kind == "differs" and finding.ack_token and list(finding.fields) == ["price"]


def test_a_lagging_scale_whose_old_record_has_other_values_needs_its_token_and_keeps_them(env3):
    rejected_on_b(env3)
    fake = env3.fakes["B"]
    fake.preload(row(SEED[0].replace(price="12.00")))
    rec = env3.change(MOVE)
    assert env3.statuses(rec)["B"] == "drift_refused" and fake.item(1)[2] == "1001"
    finding = next(d for d in rec.scale_results[1].drift)
    assert finding.kind == "differs" and finding.ack_token and list(finding.fields) == ["price"]
    p = env3.preview(MOVE)
    rec = env3.svc.apply_change(p.preview_id, acknowledge_drift=[finding.ack_token])
    assert env3.statuses(rec)["B"] == "verified"
    assert fake.item(1)[1:3] == ("1999", "1999") and fake.item(1)[4] == "12.00"


def test_a_scale_with_writes_off_can_be_finished_once_they_are_on(make_env):
    env = make_env({"A": True, "B": False})
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "skipped_not_write_enabled"} and env.silent("B")
    env.set_write_enabled("B", True)
    rec = env.change(MOVE)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"}
    assert env.fakes["B"].item(1)[1:3] == ("1999", "1999")


def test_a_scale_the_change_left_out_counts_as_not_having_moved(env3):
    rec = env3.change(MOVE, ack=True, scales=["A"])
    assert rec.not_touched == ("B", "C") and env3.silent("B", "C")
    assert env3.catalog.plu_moves(1, "B")["1001"].to_fields() == SEED[0].to_fields()
    assert "1001" in env3.catalog.old_plus_in_use()
    rec = env3.change(MOVE)
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"} and rec.success


def test_a_second_change_before_the_first_finished_still_reaches_the_lagging_scale(env3):
    rejected_on_b(env3)
    env3.fakes["B"].error_on_ids(1)
    rec = env3.change([{"action": "edit", "target_plu": "1999", "plu": "1888"}], ack=True)
    assert env3.statuses(rec)["B"] == "rejected"
    env3.fakes["B"].clear_faults()
    assert set(env3.catalog.plu_moves(1, "B")) == {"1001", "1999"}
    rec = env3.change([{"action": "edit", "target_plu": "1888", "plu": "1888"}])
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    assert env3.fakes["B"].item(1)[2] == "1888"
    assert env3.catalog.old_plus_in_use() == {}


def test_changing_it_back_while_a_scale_lags_works(env3):
    rejected_on_b(env3)
    rec = env3.change([{"action": "edit", "target_plu": "1999", "plu": "1001"}], ack=True)
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    for fake in env3.fakes.values():
        assert fake.item(1)[1:3] == ("1001", "1001")
    assert env3.catalog.old_plus_in_use() == {}


def test_a_price_edit_does_not_land_on_a_scale_still_on_the_old_plu(env3):
    rejected_on_b(env3)
    rec = env3.change([{"action": "edit", "target_plu": "1999", "price": "11.00"}])
    statuses = env3.statuses(rec)
    assert statuses["B"] == "drift_refused" and statuses["A"] == statuses["C"] == "verified"
    finding = rec.scale_results[1].drift[0]
    assert finding.kind == "different_product" and finding.ack_token is None
    assert "Repeat that edit" in finding.message
    assert env3.fakes["B"].item(1) == OLD_RECORD


def test_a_comparison_says_how_to_finish_it(env3):
    rejected_on_b(env3)
    results = {c.scale: c for c in env3.svc.compare_scales()}
    assert results["A"].differences == ()
    finding = results["B"].differences[0]
    assert finding.kind == "different_product" and "Repeat that edit" in finding.message


def test_a_plu_the_scale_was_never_moved_from_is_still_a_different_product(env3):
    rejected_on_b(env3)
    fake = env3.fakes["B"]
    fake.preload(row(SEED[0].replace(plu="1777", code="1777")))
    rec = env3.change(MOVE)
    assert env3.statuses(rec)["B"] == "drift_refused"
    assert rec.scale_results[1].drift[0].kind == "different_product"
    assert fake.item(1)[2] == "1777"


def test_an_old_plu_cannot_be_retargeted_at_another_new_plu(env3):
    rejected_on_b(env3)
    p = env3.svc.preview_changes([{"action": "edit", "target_plu": "1001", "plu": "1888"}])
    assert not p.ok and p.errors[0].code == "item_not_found"


def test_a_finished_change_cannot_be_repeated_by_the_old_plu(env):
    env.change(MOVE, ack=True)
    p = env.svc.preview_changes(MOVE)
    assert not p.ok and p.errors[0].code == "item_not_found"


def test_an_old_plu_a_scale_may_hold_is_not_free_until_it_finishes(env3):
    rejected_on_b(env3)
    add = [{"action": "add", "plu": "1001", "description": "New", "price": "1.00"}]
    p = env3.svc.preview_changes(add)
    assert not p.ok and p.errors[0].code == "duplicate_plu"
    other = env3.svc.preview_changes([{"action": "edit", "target_plu": "1002", "plu": "1001"}])
    assert not other.ok and other.errors[0].code == "duplicate_plu"
    env3.change(MOVE)
    assert env3.svc.preview_changes(add).ok


def test_a_delete_can_reach_a_scale_still_on_the_old_plu(env3):
    rejected_on_b(env3)
    rec = env3.change([{"action": "delete", "target_plu": "1999"}], ack=True)
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    for fake in env3.fakes.values():
        assert fake.item(1)[1:5] == ("9001", "9001", "UNUSED", "0.01")
    assert env3.catalog.old_plus_in_use() == {}


def test_a_reload_finishes_it_too(env3):
    rejected_on_b(env3)
    result = env3.svc.reload_scale("B", requested_by="test")
    assert result.ok and env3.fakes["B"].item(1)[2] == "1999"
    assert env3.catalog.plu_moves(1, "B") == {}


def test_an_edit_over_a_placeholder_goes_there_with_the_new_plu(env):
    fake = env.fakes["A"]
    fake.preload(row(placeholder_record(1, STAMP)))
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified"} and fake.item(1)[1:3] == ("1999", "1999")


# ---------------------------------------------------------------- settling stays held


def test_settling_to_the_catalog_will_not_change_a_plu_on_a_scale(env3):
    rejected_on_b(env3)
    fake = env3.fakes["B"]
    p = env3.svc.preview_settle_drift(scale="B", take="catalog", plu="1999")
    assert not p.ok and p.errors[0].code == "plu_edit_held"
    assert fake.item(1) == OLD_RECORD, "still the old PLU, and only an edit that asks can change it"


def test_settling_to_the_catalog_still_refuses_with_no_plu_change_anywhere(env):
    env.fakes["A"].preload(row(SEED[0].replace(plu="1777", code="1777")))
    p = env.svc.preview_settle_drift(scale="A", take="catalog", plu="1001")
    assert not p.ok and p.errors[0].code == "plu_edit_held"


def test_settling_to_the_scale_can_still_take_a_scales_plu(env):
    env.fakes["A"].preload(row(SEED[0].replace(plu="1777", code="1777")))
    p = env.svc.preview_settle_drift(scale="A", take="scale", plu="1001")
    assert p.ok and p.drift[0].kind == "different_product"
    rec = env.svc.apply_change(p.preview_id, acknowledge_drift=[p.drift[0].ack_token])
    assert env.statuses(rec) == {"A": "verified"} and env.catalog.get_by_id(1).plu == "1777"
    assert not env.fakes["A"].messages_of("GP")


def test_results_stay_plain_json(env):
    rec = env.change(MOVE, ack=True)
    json.dumps(rec.to_dict())


# ---------------------------------------------------------------- a catalog from before this change


def test_a_catalog_file_from_the_version_before_gets_the_new_table(tmp_path):
    path = tmp_path / "old.sqlite3"
    with Catalog(path) as catalog:
        catalog.import_items(SEED)
    db = sqlite3.connect(path)
    db.execute("DROP TABLE plu_moves")
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()
    with Catalog(path) as catalog:
        assert [i.id for i in catalog.list_items()] == [1, 2, 3]
        assert catalog.old_plus_in_use() == {}
        catalog.note_plu_move("A", SEED[0])
        assert catalog.old_plus_in_use() == {"1001": 1}
