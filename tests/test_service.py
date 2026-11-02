"""ScaleConnectService against fake scales on localhost.

The fixtures (env, env3, make_env) are in conftest.py and tests/service_env.py.
Each fake scale holds the seeded items, so the service can read them first, as
docs/DESIGN.md says. All items are made up. No real scale is contacted. After
every test the wire is checked: only known messages went out, each with a good
check character, and every record that reached a fake passed the validation
rules in docs/DESIGN.md.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta

import pytest

from scaleconnect import errors
from scaleconnect.config import load_config
from scaleconnect.models import Change, Item, ScaleStatus
from service_env import KNOWN_KINDS, SEED, TIMESTAMP

EDIT_PRICE = [{"action": "edit", "target_plu": "1001", "price": "10.10"}]


def price_on(fake, item_id=1):
    rec = fake.item(item_id)
    return rec[4] if rec else None


def gp_ids(fake):
    return [i for m in fake.messages_of("GP") for i in m.ids]


# ---------- add, edit, delete ----------

def test_add_sends_a_new_item_and_every_scale_confirms_it(env3):
    rec = env3.change([{"action": "add", "plu": "1234", "description": "New Test Item", "price": "12.50"}])
    assert rec.success and rec.outcome == "success" and rec.verified
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    for fake in env3.fakes.values():
        assert gp_ids(fake) == [4]  # the lowest unused id after 1 to 3
        sent = fake.item(4)
        assert sent[:5] == ("4", "1234", "1234", "New Test Item", "12.50")
        assert sent[5] == ""
        assert sent[6:12] == ("1", "1", "0", "0", "0", "0")
        assert sent[13:] == ("0",) * 7
        assert TIMESTAMP.match(sent[12])
    assert env3.catalog.get_by_plu("1234").id == 4


def test_add_takes_the_lowest_unused_id_and_callers_do_not_pick(env):
    p = env.svc.preview_changes([{"action": "add", "plu": "1234", "description": "X", "price": "1.00"}])
    assert p.ok and p.differences[0].item_id == 4
    p = env.svc.preview_changes(
        [{"action": "add", "plu": "1234", "description": "X", "price": "1.00", "id": 99}]
    )
    assert not p.ok


@pytest.mark.parametrize("sold_by,field7", [("weight", "1"), ("piece", "0")])
def test_add_sold_by_sets_field_7_and_nothing_else(env, sold_by, field7):
    env.change([{"action": "add", "plu": "1300", "description": "Choice", "price": "3.00", "sold_by": sold_by}])
    assert env.fakes["A"].item(4)[6:12] == (field7, "1", "0", "0", "0", "0")


def test_add_with_any_other_sold_by_is_refused(env):
    p = env.svc.preview_changes([{"action": "add", "plu": "1300", "description": "X", "price": "3.00", "sold_by": "tare"}])
    assert not p.ok and env.silent("A")


def test_edit_changes_only_the_price_and_the_timestamp(env):
    original = SEED[2]  # has odd values in fields 7 to 12 and 14 to 20, all must survive
    rec = env.change([{"action": "edit", "target_plu": "1003", "price": "6.00"}])
    assert rec.success
    sent = env.fakes["A"].item(3)
    expected = original.to_fields()
    assert sent[4] == "6.00"
    assert TIMESTAMP.match(sent[12]) and sent[12] != original.timestamp
    for i in range(20):
        if i not in (4, 12):
            assert sent[i] == expected[i], f"field {i + 1} changed"
    assert sent[9] == "0.139"
    assert sent[5] == " "
    assert gp_ids(env.fakes["A"]) == [3]  # only the edited item went out


def test_edit_by_id_changes_only_the_description(env):
    rec = env.change([{"action": "edit", "target_id": 2, "description": "Test Brie Wedge"}])
    assert rec.success
    sent = env.fakes["A"].item(2)
    expected = SEED[1].to_fields()
    assert sent[3] == "Test Brie Wedge"
    assert [sent[i] for i in range(20) if i not in (3, 12)] == [expected[i] for i in range(20) if i not in (3, 12)]
    assert env.catalog.get_by_id(2).description == "Test Brie Wedge"
    assert env.catalog.get_by_id(2).price == "20.00"


def test_edit_leaves_every_other_catalog_item_alone(env):
    before = {i.id: i for i in env.catalog.list_items()}
    env.change([{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    after = {i.id: i for i in env.catalog.list_items()}
    assert after[2] == before[2] and after[3] == before[3]
    assert after[1].price == "11.00"
    assert after[1].replace(price="10.00", timestamp=before[1].timestamp) == before[1]


def test_the_catalog_gets_the_time_that_was_sent(env):
    env.change(EDIT_PRICE)
    assert env.catalog.get_by_id(1).timestamp == env.fakes["A"].item(1)[12]


def test_delete_overwrites_the_id_with_a_placeholder_on_every_scale(env3):
    rec = env3.change([{"action": "delete", "target_plu": "1002"}], ack=True)
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    assert rec.success and rec.outcome == "success"
    assert env3.catalog.get_by_plu("1002") is None
    for fake in env3.fakes.values():
        assert fake.item(2)[1:5] == ("9002", "9002", "UNUSED", "0.01")


def test_delete_preview_needs_an_acknowledgement(env):
    p = env.preview([{"action": "delete", "target_plu": "1002"}])
    assert [(w.code, w.requires_ack) for w in p.warnings] == [("delete_placeholder", True)]
    assert p.differences[0].action == "delete" and p.differences[0].after is None
    with pytest.raises(errors.AcknowledgementRequired):
        env.svc.apply_change(p.preview_id)
    assert env.silent("A")


def test_a_mixed_preview_sends_the_edit_and_the_delete_in_one_session(env):
    rec = env.change([
        {"action": "edit", "target_plu": "1001", "price": "10.10"},
        {"action": "delete", "target_plu": "1002"},
    ], ack=True)
    items = {i.action: i.status for i in rec.scale_results[0].items}
    assert items == {"edit": "verified", "delete": "verified"}
    assert price_on(env.fakes["A"]) == "10.10"
    assert gp_ids(env.fakes["A"]) == [1, 2]


def test_capabilities_say_delete_and_reads_are_available(env):
    caps = env.svc.capabilities().to_dict()
    assert caps["add"] and caps["edit"] and caps["read"] and caps["verify"]
    assert caps["delete"] is True
    assert "delete" not in caps["notes"]


def test_a_code_alone_cannot_part_from_the_plu(env):
    p = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", "code": "1999"}])
    assert not p.ok and p.errors[0].code == "code_plu_mismatch"
    assert env.silent("A")


# ---------- statuses ----------

def test_the_statuses_are_exactly_these():
    assert {s.value for s in ScaleStatus} == {
        "verified", "accepted_not_verified", "rejected", "unknown", "unreachable",
        "skipped_not_write_enabled", "not_available", "wrong_scale", "mismatch",
        "drift_refused", "read_failed",
    }


def test_status_verified_when_the_read_back_matches(env):
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified"}
    assert rec.success and rec.verified
    assert "not verified" not in rec.display.lower()


def test_status_accepted_not_verified_when_it_cannot_be_read_back(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.3, read_back_window_seconds=0.5,
                   read_back_retry_seconds=0.1)
    fake = env.fakes["A"]
    fake.stall_read(times=None)
    fake.faults[-1].match = lambda m: m.kind == "LP" and len(fake.messages_of("LP")) >= 2  # only the read back
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "accepted_not_verified"}
    assert not rec.success and not rec.verified and rec.outcome == "partial"
    assert "not" in rec.display.lower()
    assert "confirmed" in rec.scale_results[0].message.lower() or "read it back" in rec.scale_results[0].message.lower()
    assert price_on(fake) == "10.10"


def test_status_read_failed_when_the_read_before_the_write_fails(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall_read(block=0, times=None)
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "read_failed"}
    assert env.fakes["A"].messages_of("GP") == []
    assert "RI" not in env.fakes["A"].kinds()


def test_status_rejected_when_the_scale_says_error(env):
    env.fakes["A"].error_on_ids(1)
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "rejected"}
    assert not rec.success and rec.outcome == "failed"
    assert env.catalog.get_by_id(1).price == "10.00"


def test_gpf0ok_alone_never_counts_as_success(env):
    env.fakes["A"].error_on_ids(1)
    rec = env.change(EDIT_PRICE)
    result = rec.scale_results[0]
    assert env.fakes["A"].kinds()[-3:] != []  # the session ran to the end
    assert "GPF0" in env.fakes["A"].kinds()
    assert any("GPF0Ok" in r for r in result.replies)  # the scale did say GPF0Ok
    assert result.status == "rejected" and not rec.success
    assert result.items[0].status == "rejected"


def test_status_unknown_on_a_stall_and_it_is_not_read_back_at_once(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1)
    kinds_before = None
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "unknown"}
    assert not rec.success
    assert rec.scale_results[0].attempts == 1
    assert env.catalog.get_by_id(1).price == "10.00"
    assert env.svc.get_item(plu="1001").scales["A"].status == "unknown"
    assert env.fakes["A"].kinds().count("LP") == 1  # the read before. None after: it may still be working


def test_status_unreachable_sends_nothing(make_env):
    env = make_env({"A": True}, connect_attempts=1)
    env.fakes["A"].stop_listening()
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "unreachable"}
    assert rec.scale_results[0].attempts == 0
    assert env.silent("A")
    assert not rec.success


def test_status_skipped_not_write_enabled(make_env):
    env = make_env({"A": False})
    p = env.preview(EDIT_PRICE)
    assert [w.code for w in p.warnings] == ["scale_not_write_enabled"]
    rec = env.svc.apply_change(p.preview_id)
    assert env.statuses(rec) == {"A": "skipped_not_write_enabled"}
    assert not rec.success
    assert env.silent("A")
    assert env.catalog.get_by_id(1).price == "10.00"


# ---------- more than one scale ----------

def test_results_come_one_per_scale_in_config_order(env3):
    rec = env3.change(EDIT_PRICE)
    assert [r.scale for r in rec.scale_results] == ["A", "B", "C"]
    assert all(r.attempts == 1 for r in rec.scale_results)


def test_every_scale_gets_the_same_time(env3):
    env3.change(EDIT_PRICE)
    sent = [f.item(1) for f in env3.fakes.values()]
    assert sent[0] == sent[1] == sent[2]


def test_one_scale_failing_does_not_stop_the_others(make_env):
    env = make_env({"A": True, "B": True, "C": True}, reply_timeout_seconds=0.4)
    env.fakes["B"].stall("GP", times=None)
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified", "B": "unknown", "C": "verified"}
    assert rec.outcome == "partial" and not rec.success and not rec.verified
    assert price_on(env.fakes["C"]) == "10.10"  # C was still sent to after B failed
    assert env.catalog.get_by_id(1).price == "10.10"  # at least one scale took it
    states = env.svc.get_item(plu="1001").scales
    assert {n: s.status for n, s in states.items()} == {"A": "verified", "B": "unknown", "C": "verified"}


def test_one_scale_rejecting_and_one_unreachable(make_env):
    env = make_env({"A": True, "B": True, "C": True}, connect_attempts=1)
    env.fakes["B"].error_on_ids(1)
    env.fakes["C"].stop_listening()
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified", "B": "rejected", "C": "unreachable"}
    assert rec.outcome == "partial" and not rec.success


def test_all_scales_failing_is_failed(make_env):
    env = make_env({"A": True, "B": True}, connect_attempts=1)
    for f in env.fakes.values():
        f.error_on_ids(1)
    rec = env.change(EDIT_PRICE)
    assert rec.outcome == "failed" and not rec.success
    assert env.catalog.get_by_id(1).price == "10.00"


def test_write_enabled_block_one_scale_off(make_env):
    env = make_env({"A": True, "B": False, "C": True})
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified", "B": "skipped_not_write_enabled", "C": "verified"}
    assert rec.outcome == "partial" and not rec.success
    assert env.fakes["B"].nothing_received
    assert env.fakes["B"].connection_count == 0 and env.fakes["B"].bytes_received == 0


def test_write_enabled_block_covers_adds_edits_and_deletes(make_env):
    env = make_env({"A": False})
    env.change([
        {"action": "add", "plu": "1234", "description": "New", "price": "1.00"},
        {"action": "edit", "target_plu": "1001", "price": "10.10"},
        {"action": "delete", "target_plu": "1002"},
    ], ack=True)
    assert env.silent("A")


def test_a_scale_with_neither_flag_is_not_contacted_even_for_a_read(make_env):
    env = make_env({"A": True, "B": False})
    env.svc.compare_scales()
    assert env.silent("B")
    assert not env.silent("A")


def test_default_for_a_scale_with_no_flag_is_not_write_enabled(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[scales]]\nname = "ONE"\naddress = "127.0.0.1"\n\n'
        '[[scales]]\nname = "TWO"\naddress = "127.0.0.2"\nwrite_enabled = "yes"\n\n'
        '[[scales]]\nname = "THREE"\naddress = "127.0.0.3"\nwrite_enabled = true\nserial = "S-3"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert {s.name: s.write_enabled for s in config.scales} == {"ONE": False, "TWO": False, "THREE": True}
    assert config.warnings


def test_only_named_scales_are_targets_and_unknown_scale_names_are_refused(env3):
    p = env3.svc.preview_changes(EDIT_PRICE, scales=["A", "NOPE"])
    assert not p.ok and p.errors[0].code == "unknown_scale"
    assert env3.silent("A", "B", "C")
    rec = env3.change(EDIT_PRICE, scales=["B"], ack=True)
    assert [r.scale for r in rec.scale_results] == ["B"]
    assert env3.silent("A", "C")
    assert set(rec.not_touched) == {"A", "C"}
    assert not rec.success


# ---------- timing ----------

def test_a_reply_after_3_25_seconds_succeeds_with_the_default_timeout(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=15.0)
    env.fakes["A"].delay_reply(3.25, "GP")
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified"}
    assert rec.success
    assert len(env.fakes["A"].messages_of("GP")) == 1  # no resend: it was patient


def test_the_slowest_scales_beat_wait_does_not_fail_a_change(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=15.0, fake_options={"beat_stall_seconds": 3.25})
    env.fakes["A"].trigger_beat()
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified"}


def test_a_reply_after_the_timeout_is_unknown_and_never_called_success(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.5)
    env.fakes["A"].delay_reply(1.5, "GP")
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "unknown"}
    assert not rec.success
    assert len(env.fakes["A"].messages_of("GP")) == 1
    # The fake did keep the item. We still must not claim it.
    assert price_on(env.fakes["A"]) == "10.10"


def test_the_real_gap_after_a_write_is_waited_out_for_the_read_back(make_env):
    env = make_env(
        {"A": True},
        fake_options={"post_write_refusal_seconds": 1.0},
        read_back_window_seconds=20.0,
        read_back_retry_seconds=0.1,
        connect_attempts=1,
    )
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified"}
    assert env.fakes["A"].refusal_windows == 1


def test_real_reply_waits_do_not_break_a_whole_change(make_env):
    env = make_env({"A": True}, fake_options={"timing_factor": 0.2})
    rec = env.change(EDIT_PRICE)
    assert rec.success


# ---------- validation: bad input never reaches a scale ----------

BAD_CHANGES = [
    ("price too high", {"action": "edit", "target_plu": "1001", "price": "1000.00"}, "invalid_price"),
    ("price zero", {"action": "edit", "target_plu": "1001", "price": "0.00"}, "invalid_price"),
    ("price negative", {"action": "edit", "target_plu": "1001", "price": "-1.00"}, "invalid_price"),
    ("price one decimal", {"action": "edit", "target_plu": "1001", "price": "9.9"}, "invalid_price"),
    ("price three decimals", {"action": "edit", "target_plu": "1001", "price": "9.999"}, "invalid_price"),
    ("price whole number", {"action": "edit", "target_plu": "1001", "price": "5"}, "invalid_price"),
    ("price text", {"action": "edit", "target_plu": "1001", "price": "abc"}, "invalid_price"),
    ("price leading zero", {"action": "edit", "target_plu": "1001", "price": "09.99"}, "invalid_price"),
    ("price blank", {"action": "edit", "target_plu": "1001", "price": ""}, "invalid_price"),
    ("add price too high", {"action": "add", "plu": "1234", "description": "X", "price": "1000.00"}, "invalid_price"),
    ("plu 3 digits", {"action": "add", "plu": "123", "description": "X", "price": "1.00"}, "invalid_plu"),
    ("plu 5 digits", {"action": "add", "plu": "12345", "description": "X", "price": "1.00"}, "invalid_plu"),
    ("plu letters", {"action": "add", "plu": "12a4", "description": "X", "price": "1.00"}, "invalid_plu"),
    ("plu with space", {"action": "add", "plu": "12 4", "description": "X", "price": "1.00"}, "invalid_plu"),
    ("edit plu 5 digits", {"action": "edit", "target_plu": "1001", "plu": "10011"}, "invalid_plu"),
    ("code differs from plu", {"action": "add", "plu": "1234", "code": "1235", "description": "X", "price": "1.00"},
     "code_plu_mismatch"),
    ("duplicate plu on add", {"action": "add", "plu": "1002", "description": "X", "price": "1.00"}, "duplicate_plu"),
    ("empty description", {"action": "add", "plu": "1234", "description": "", "price": "1.00"}, "invalid_description"),
    ("blank description", {"action": "add", "plu": "1234", "description": "   ", "price": "1.00"}, "invalid_description"),
    ("description with TAB", {"action": "add", "plu": "1234", "description": "A\tB", "price": "1.00"},
     "invalid_description"),
    ("description with LF", {"action": "add", "plu": "1234", "description": "A\nB", "price": "1.00"},
     "invalid_description"),
    ("description with CR", {"action": "add", "plu": "1234", "description": "A\rB", "price": "1.00"},
     "invalid_description"),
    ("description with accent", {"action": "add", "plu": "1234", "description": "Café", "price": "1.00"},
     "invalid_description"),
    ("description too long", {"action": "add", "plu": "1234", "description": "L" * 36, "price": "1.00"},
     "invalid_description"),
    ("description with trailing space", {"action": "add", "plu": "1234", "description": "Name ", "price": "1.00"},
     "invalid_description"),
    ("item not found", {"action": "edit", "target_plu": "9999", "price": "1.00"}, "item_not_found"),
    ("id not found", {"action": "edit", "target_id": 400, "price": "1.00"}, "item_not_found"),
]


@pytest.mark.parametrize("label,change,code", BAD_CHANGES, ids=[b[0] for b in BAD_CHANGES])
def test_bad_input_is_refused_before_any_byte_goes_out(env3, label, change, code):
    p = env3.svc.preview_changes([change], requested_by="test")
    assert p.ok is False
    assert p.preview_id is None
    assert code in [e.code for e in p.errors]
    assert env3.silent("A", "B", "C")
    assert [i.id for i in env3.catalog.list_items()] == [1, 2, 3]


def test_a_float_price_is_refused(env):
    p = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", "price": 9.99}])
    assert not p.ok and env.silent("A")


def test_a_description_of_exactly_35_characters_is_fine(env):
    env.change([{"action": "add", "plu": "1234", "description": "D" * 35, "price": "999.99"}])
    assert env.fakes["A"].item(4)[3] == "D" * 35
    assert env.fakes["A"].item(4)[4] == "999.99"


def test_price_of_one_cent_is_the_lowest_allowed(env):
    env.change([{"action": "add", "plu": "1234", "description": "Penny", "price": "0.01"}])
    assert env.fakes["A"].item(4)[4] == "0.01"


def test_an_edit_that_changes_nothing_warns_and_sends_the_same_record_again(env):
    p = env.preview([{"action": "edit", "target_plu": "1001", "price": "10.00"}])
    assert [w.code for w in p.warnings] == ["no_change"]
    rec = env.svc.apply_change(p.preview_id)
    assert rec.success
    assert price_on(env.fakes["A"]) == "10.00"


def test_one_bad_change_among_good_ones_blocks_the_whole_preview(env):
    p = env.svc.preview_changes([
        {"action": "edit", "target_plu": "1001", "price": "10.10"},
        {"action": "edit", "target_plu": "1002", "price": "1000.00"},
    ])
    assert not p.ok
    assert [e.index for e in p.errors] == [1]
    assert env.silent("A")


def test_the_same_item_twice_in_one_preview_is_refused(env):
    p = env.svc.preview_changes([
        {"action": "edit", "target_plu": "1001", "price": "10.10"},
        {"action": "edit", "target_id": 1, "price": "10.20"},
    ])
    assert not p.ok and "duplicate_target" in [e.code for e in p.errors]


def test_two_adds_with_the_same_plu_in_one_preview_are_refused(env):
    p = env.svc.preview_changes([
        {"action": "add", "plu": "1500", "description": "One", "price": "1.00"},
        {"action": "add", "plu": "1500", "description": "Two", "price": "1.00"},
    ])
    assert not p.ok and "duplicate_plu" in [e.code for e in p.errors]


@pytest.mark.parametrize("bad", [{"action": "add"}, {"action": "frobnicate"}, {"price": "1.00"}, "text", 5, {
    "action": "edit", "target_plu": "1001", "price": "1.00", "mystery": 1}, {3: "x"}])
def test_malformed_changes_are_errors_not_crashes(env, bad):
    p = env.svc.preview_changes([bad])
    assert not p.ok and p.errors
    assert env.silent("A")


def test_edit_needs_a_field_and_a_single_target(env):
    for bad in (
        {"action": "edit", "target_plu": "1001"},
        {"action": "edit", "price": "1.00"},
        {"action": "edit", "target_plu": "1001", "target_id": 1, "price": "1.00"},
    ):
        assert not env.svc.preview_changes([bad]).ok
    assert env.silent("A")


def test_an_edit_cannot_set_sold_by_or_raw_fields(env):
    for bad in (
        {"action": "edit", "target_plu": "1001", "sold_by": "piece"},
        {"action": "edit", "target_plu": "1001", "fields_7_12": ["0", "1", "0", "0", "0", "0"]},
    ):
        assert not env.svc.preview_changes([bad]).ok


# ---------- the id limit ----------

def test_id_500_is_usable_and_nothing_goes_past_it(make_env):
    seed = [Item(i, f"{1000 + i}", f"{1000 + i}", f"Seed {i}", "1.00", timestamp="2026-01-01 00:00:00")
            for i in range(1, 500)]
    env = make_env({"A": True}, seed=seed)
    rec = env.change([{"action": "add", "plu": "2000", "description": "Last Slot", "price": "1.00"}])
    assert rec.success
    assert gp_ids(env.fakes["A"]) == [500]
    assert env.fakes["A"].item(500)[3] == "Last Slot"


def test_when_all_500_ids_are_taken_an_add_is_refused_and_nothing_is_sent(make_env):
    seed = [Item(i, f"{1000 + i}", f"{1000 + i}", f"Seed {i}", "1.00", timestamp="2026-01-01 00:00:00")
            for i in range(1, 501)]
    env = make_env({"A": True}, seed=seed)
    p = env.svc.preview_changes([{"action": "add", "plu": "2000", "description": "No Room", "price": "1.00"}])
    assert not p.ok and "no_free_id" in [e.code for e in p.errors]
    assert env.silent("A")


def test_two_adds_in_one_preview_get_two_different_ids(env):
    rec = env.change([
        {"action": "add", "plu": "1500", "description": "One", "price": "1.00"},
        {"action": "add", "plu": "1501", "description": "Two", "price": "2.00"},
    ])
    assert rec.success
    assert gp_ids(env.fakes["A"]) == [4, 5]


def test_an_add_that_ended_unknown_does_not_hand_its_id_to_another_item(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1)
    first = env.change([{"action": "add", "plu": "1500", "description": "Maybe There", "price": "1.00"}])
    assert env.statuses(first) == {"A": "unknown"}
    second = env.svc.preview_changes([{"action": "add", "plu": "1501", "description": "Next", "price": "1.00"}])
    assert second.ok
    assert second.differences[0].item_id != first.changes[0].item_id


# ---------- preview and apply rules ----------

def test_a_large_price_change_needs_acknowledgement_and_nothing_is_sent_without_it(env):
    p = env.preview([{"action": "edit", "target_plu": "1001", "price": "20.00"}])
    assert [(w.code, w.requires_ack) for w in p.warnings] == [("price_change_large", True)]
    with pytest.raises(errors.AcknowledgementRequired):
        env.svc.apply_change(p.preview_id)
    assert env.silent("A")
    rec = env.svc.apply_change(p.preview_id, acknowledge_warnings=True)
    assert rec.success and rec.acknowledged_warnings
    assert price_on(env.fakes["A"]) == "20.00"


def test_exactly_fifty_percent_is_not_a_large_change(env):
    p = env.preview([{"action": "edit", "target_plu": "1001", "price": "15.00"}])
    assert p.warnings == ()
    p = env.preview([{"action": "edit", "target_plu": "1001", "price": "15.01"}])
    assert [w.code for w in p.warnings] == ["price_change_large"]


def test_a_preview_can_only_be_applied_once(env):
    p = env.preview(EDIT_PRICE)
    env.svc.apply_change(p.preview_id)
    sent = len(env.fakes["A"].messages)
    with pytest.raises(errors.PreviewAlreadyApplied):
        env.svc.apply_change(p.preview_id)
    assert len(env.fakes["A"].messages) == sent


def test_an_expired_preview_is_refused_and_nothing_is_sent(make_env):
    now = [datetime.now().astimezone()]
    env = make_env({"A": True}, clock=lambda: now[0], preview_ttl_seconds=60)
    p = env.preview(EDIT_PRICE)
    now[0] += timedelta(seconds=61)
    with pytest.raises(errors.PreviewExpired):
        env.svc.apply_change(p.preview_id)
    assert env.silent("A")


def test_a_preview_goes_stale_when_the_item_list_changes(env):
    p = env.preview(EDIT_PRICE)
    env.catalog.upsert_item(Item(9, "1009", "1009", "Sneaky", "1.00", timestamp="2026-01-01 00:00:00"))
    with pytest.raises(errors.PreviewStale):
        env.svc.apply_change(p.preview_id)
    assert env.silent("A")


def test_unknown_preview_id(env):
    with pytest.raises(errors.PreviewNotFound):
        env.svc.apply_change("nope")
    assert env.silent("A")


def test_a_plain_preview_never_contacts_a_scale(env3):
    env3.svc.list_scales()
    env3.svc.search_items("test")
    env3.svc.get_item(plu="1001")
    env3.svc.capabilities()
    env3.svc.preview_changes(EDIT_PRICE)
    env3.svc.list_changes()
    assert env3.silent("A", "B", "C")


def test_search_and_get_item(env):
    assert sorted(i.plu for i in env.svc.search_items("test")) == ["1001", "1002", "1003"]
    assert [i.plu for i in env.svc.search_items("brie")] == ["1002"]
    assert env.svc.search_items("zzz") == []
    assert env.svc.get_item(id=2).item.plu == "1002"
    with pytest.raises(errors.ItemNotFound):
        env.svc.get_item(plu="9999")
    with pytest.raises(errors.ValidationFailed):
        env.svc.get_item()
    with pytest.raises(errors.ValidationFailed):
        env.svc.get_item(plu="1001", id=1)


# ---------- backup, lock and the record ----------

def test_backup_runs_before_the_first_byte_to_each_write_enabled_scale(make_env):
    seen = []
    fakes = {}

    def hook(scale, items, backup_dir):
        seen.append((scale, len(fakes[scale].messages), [i.id for i in items]))
        return backup_dir

    env = make_env({"A": True, "B": False, "C": True}, backup_hook=hook)
    fakes.update(env.fakes)
    env.change(EDIT_PRICE)
    assert seen == [("A", 0, [1, 2, 3]), ("C", 0, [1, 2, 3])]  # B is not write enabled, so no backup
    env.change([{"action": "edit", "target_plu": "1001", "price": "10.20"}])
    assert len(seen) == 2  # only before the first write


def test_the_default_backup_saves_the_item_list_before_sending(env):
    env.change(EDIT_PRICE)
    folders = [p for p in env.settings.backup_dir.iterdir() if p.is_dir()]
    assert len(folders) == 1
    saved = json.loads((folders[0] / "items.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in saved] == [1, 2, 3]
    assert saved[0]["price"] == "10.00"  # the list as it was before the change


def test_a_failed_backup_stops_everything_and_nothing_is_sent(make_env):
    def hook(scale, items, backup_dir):
        raise OSError("disk full")

    env = make_env({"A": True, "B": True}, backup_hook=hook)
    p = env.preview(EDIT_PRICE)
    with pytest.raises(errors.BackupFailed):
        env.svc.apply_change(p.preview_id)
    assert env.silent("A", "B")
    assert env.catalog.get_by_id(1).price == "10.00"


def test_a_second_apply_while_one_is_running_is_refused(make_env):
    env = make_env({"A": True})
    env.fakes["A"].delay_reply(1.5, "GP")
    p1 = env.preview(EDIT_PRICE)
    p2 = env.preview([{"action": "edit", "target_plu": "1002", "price": "20.10"}])
    out = {}
    t = threading.Thread(target=lambda: out.update(rec=env.svc.apply_change(p1.preview_id)))
    t.start()
    try:
        import time as _t

        end = _t.monotonic() + 8
        while "GP" not in env.fakes["A"].kinds() and _t.monotonic() < end:
            _t.sleep(0.02)
        with pytest.raises(errors.ApplyBusy):
            env.svc.apply_change(p2.preview_id)
    finally:
        t.join(timeout=15)
    assert out["rec"].success
    assert gp_ids(env.fakes["A"]) == [1]


def test_the_change_record_can_be_read_back_and_matches(env3):
    rec = env3.change(EDIT_PRICE)
    back = env3.svc.get_change(rec.change_id)
    assert back.change_id == rec.change_id
    assert back.outcome == rec.outcome and back.success == rec.success and back.verified == rec.verified
    assert [(r.scale, r.status) for r in back.scale_results] == [(r.scale, r.status) for r in rec.scale_results]
    assert back.requested_by == "test"
    assert env3.svc.list_changes()[0].change_id == rec.change_id
    with pytest.raises(errors.ChangeNotFound):
        env3.svc.get_change("nope")


def test_the_change_for_a_preview_can_be_found_by_its_preview_id(env):
    p = env.preview(EDIT_PRICE)
    assert env.svc.get_change_for_preview(p.preview_id) is None
    rec = env.svc.apply_change(p.preview_id)
    assert env.svc.get_change_for_preview(p.preview_id).change_id == rec.change_id


def test_each_scales_reply_is_logged_even_if_a_later_scale_fails(make_env):
    env = make_env({"A": True, "B": True}, connect_attempts=1)
    env.fakes["B"].stop_listening()
    rec = env.change(EDIT_PRICE)
    events = [e["event"] for e in env.catalog.read_log(rec.change_id)]
    assert events == ["started", "scale_result", "scale_result", "finished"]


def test_list_scales_shows_the_last_result_and_never_the_address(make_env):
    env = make_env({"A": True, "B": False})
    env.change(EDIT_PRICE)
    info = {s.name: s for s in env.svc.list_scales()}
    assert info["A"].write_enabled and info["A"].last_status == "verified"
    assert not info["B"].write_enabled and info["B"].last_status == "skipped_not_write_enabled"
    assert "127.0.0.1" not in json.dumps([s.to_dict() for s in info.values()])


def _floats(x):
    if isinstance(x, float):
        return [x]
    if isinstance(x, dict):
        return [f for v in x.values() for f in _floats(v)]
    if isinstance(x, (list, tuple)):
        return [f for v in x for f in _floats(v)]
    return []


def test_results_are_json_safe_and_hold_no_floats(env):
    p = env.svc.preview_changes(EDIT_PRICE, read_scales=True)
    rec = env.svc.apply_change(p.preview_id)
    for obj in (p.to_dict(), rec.to_dict(), env.svc.capabilities().to_dict(), env.svc.get_item(plu="1001").to_dict(),
                [c.to_dict() for c in env.svc.compare_scales()]):
        json.dumps(obj)
        assert _floats(obj) == []


def test_a_decimal_price_is_accepted_and_sent_as_text(env):
    from decimal import Decimal

    env.change([Change(action="edit", target_plu="1001", price=Decimal("10.20"))])
    assert env.fakes["A"].item(1)[4] == "10.20"


def test_the_whole_flow_sends_only_known_messages(env3):
    env3.change([{"action": "add", "plu": "1500", "description": "One", "price": "1.00"}])
    env3.change([{"action": "edit", "target_plu": "1500", "price": "1.25"}])
    env3.change([{"action": "delete", "target_plu": "1500"}], ack=True)
    for fake in env3.fakes.values():
        assert set(fake.kinds()) <= KNOWN_KINDS
        assert {"CX", "LP", "RI", "bX01XX", "GP", "GPF0"} <= set(fake.kinds())
        assert fake.kinds().count("RI") == 3  # a delete is a write like any other
        assert fake.item(4)[3:5] == ("UNUSED", "0.01")
        assert not any(m.raw.startswith((b"LG", b"E")) for m in fake.messages)
        fake.assert_one_at_a_time()
