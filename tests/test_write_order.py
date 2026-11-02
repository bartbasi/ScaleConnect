"""The seven step write order in docs/DESIGN.md, "Each scale is the truth about itself".

1 identity, 2 read first, 3 compare (drift refusal), 4 build from the scale's own
values, 5 write, 6 read back (verified or mismatch), 7 never resend after sending
began. Plus compare_scales and the interrupted change guard.

Made up items, fake scales on localhost only.
"""

from __future__ import annotations

import pytest

from scaleconnect import errors
from scaleconnect.models import Item
from service_env import SEED, serial_for

EDIT_PRICE = [{"action": "edit", "target_plu": "1001", "price": "12.00"}]
EDIT_DESC = [{"action": "edit", "target_plu": "1001", "description": "Test Cheddar Block"}]


def with_changes(record: tuple, **fields) -> tuple:
    """A copy of a 20 field record with named fields changed."""
    names = {"price": 4, "description": 3, "field6": 5, "f7": 6, "f10": 9, "timestamp": 12}
    out = list(record)
    for k, v in fields.items():
        out[names[k]] = v
    return tuple(out)


def scale_holds(env, name, item_id, **fields):
    """Make a fake hold a record that differs from the catalog."""
    fake = env.fakes[name]
    fake.preload(with_changes(fake.item(item_id), **fields))


def kinds_after(fake, n):
    return fake.kinds()[n:]


# =====================================================================
# Step 1. Identity
# =====================================================================

def test_step1_cx_is_the_first_message_to_every_scale(env3):
    env3.change(EDIT_PRICE)
    for fake in env3.fakes.values():
        assert fake.kinds()[0] == "CX"


def test_step1_a_wrong_serial_sends_nothing_else_and_reports_wrong_scale(make_env):
    env = make_env({"A": True, "B": True})
    # B's address now answers as some other scale
    env.fakes["B"].serial = "SOMEONE-ELSE"
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "verified", "B": "wrong_scale"}
    assert env.fakes["B"].kinds() == ["CX"]
    assert "SOMEONE-ELSE" not in rec.scale_results[1].message  # stage F L8
    assert not rec.success and rec.outcome == "partial"
    assert env.fakes["B"].item(1)[4] == "10.00"


def test_step1_the_identity_check_comes_before_the_backup_dump_of_a_wrong_scale(make_env):
    env = make_env({"A": True})
    env.fakes["A"].serial = "SOMEONE-ELSE"
    env.change(EDIT_PRICE)
    assert list(env.settings.backup_dir.glob("*-scale-dump.json")) == []


def test_step1_an_unreachable_scale_is_unreachable_and_nothing_goes_to_the_others_less(make_env):
    env = make_env({"A": True, "B": True}, connect_attempts=1)
    env.fakes["A"].stop_listening()
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "unreachable", "B": "verified"}


# =====================================================================
# Step 2. Read first
# =====================================================================

def test_step2_the_read_comes_before_the_write(env):
    env.change(EDIT_PRICE)
    kinds = env.fakes["A"].kinds()
    assert kinds.index("CX") < kinds.index("LP") < kinds.index("RI") < kinds.index("bX01XX") < kinds.index("GP")


def test_step2_a_failed_read_writes_nothing(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].drop_read(block=0, times=None)
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "read_failed"}
    assert env.fakes["A"].messages_of("GP") == []
    assert env.fakes["A"].item(1)[4] == "10.00"
    assert "Nothing was written" in rec.scale_results[0].message


def test_step2_a_stalled_read_stops_and_is_not_retried_in_a_loop(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall_read(block=0, times=None)
    env.change(EDIT_PRICE)
    assert env.fakes["A"].kinds().count("LP") == 1


def test_step2_the_read_covers_the_table_and_the_read_back_stops_at_the_id(make_env):
    seed = [Item(i, f"{1000 + i}", f"{1000 + i}", f"Item {i}", "1.00", timestamp="2026-01-01 00:00:00")
            for i in range(1, 31)]
    env = make_env({"A": True}, seed=seed)
    fake = env.fakes["A"]
    env.change([{"action": "edit", "target_id": 1, "price": "1.10"}])
    whole_table = fake.kinds().count("Ok")  # the read first. The read back stays in the first block
    assert whole_table == 6
    env.change([{"action": "edit", "target_id": 1, "price": "1.20"}])  # id 1 is in the first block
    assert fake.kinds().count("Ok") == 2 * whole_table, "its PLU could be under any later id"
    env.change([{"action": "edit", "target_id": 12, "price": "1.30"}])  # id 12 is in the third block
    assert fake.kinds().count("Ok") == 3 * whole_table + 2  # the table before the write, two to read back


def test_step2_an_add_reads_the_whole_table_to_look_for_its_plu(make_env):
    seed = [Item(i, f"{1000 + i}", f"{1000 + i}", f"Item {i}", "1.00", timestamp="2026-01-01 00:00:00")
            for i in range(1, 31)]
    env = make_env({"A": True}, seed=seed)
    fake = env.fakes["A"]
    env.change([{"action": "edit", "target_id": 1, "price": "1.10"}])
    before = fake.kinds().count("Ok")
    env.change([{"action": "add", "plu": "4321", "description": "New", "price": "1.00"}])
    assert fake.kinds().count("Ok") - before >= 6  # all 30 records are 6 blocks


def test_step2_reads_keep_the_configured_pace(make_env):
    slept = []
    env = make_env({"A": True}, read_pace_seconds=0.3)
    env.svc._sleep = slept.append
    env.svc.compare_scales()
    assert slept and set(slept) == {0.3}


def test_step2_lg_and_other_messages_are_never_sent_in_a_whole_change(env):
    env.svc.preview_changes(EDIT_PRICE, read_scales=True)
    env.svc.compare_scales()
    env.change(EDIT_PRICE)
    assert not any(m.raw.startswith((b"LG", b"E", b"L" + b"H", b"LT", b"SP", b"DP")) for m in env.fakes["A"].messages)
    assert set(env.fakes["A"].kinds()) <= {"CX", "LP", "Ok", "RI", "bX01XX", "GP", "GPF0"}


# =====================================================================
# Step 3. Compare, and refuse drift
# =====================================================================

def test_step3_a_scale_that_differs_is_refused_and_nothing_is_written(env):
    scale_holds(env, "A", 1, price="11.00")
    rec = env.change(EDIT_DESC)
    assert env.statuses(rec) == {"A": "drift_refused"}
    assert not rec.success
    assert env.fakes["A"].messages_of("GP") == []
    assert "RI" not in env.fakes["A"].kinds()
    assert env.fakes["A"].item(1)[4] == "11.00"
    drift = rec.scale_results[0].drift
    assert len(drift) == 1 and drift[0].kind == "differs"
    assert drift[0].fields["price"].before == "10.00" and drift[0].fields["price"].after == "11.00"
    assert "price" in rec.scale_results[0].items[0].message


def test_step3_the_preview_names_the_drift_when_asked_to_read(env):
    scale_holds(env, "A", 1, price="11.00")
    p = env.svc.preview_changes(EDIT_DESC, read_scales=True)
    assert p.ok and p.scales_read == ("A",)
    assert [(d.scale, d.kind, d.item_id) for d in p.drift] == [("A", "differs", 1)]
    assert p.drift[0].ack_token
    assert "11.00" in p.drift[0].message and "10.00" in p.drift[0].message


def test_step3_a_plain_preview_does_not_read_so_it_cannot_know(env):
    scale_holds(env, "A", 1, price="11.00")
    p = env.svc.preview_changes(EDIT_DESC)
    assert p.drift == () and env.silent("A")


def test_step3_a_missing_item_goes_as_an_add_there(env):
    # The lead's call on stage F H2. It needs no acknowledgement, and gets an add's PLU check.
    env.fakes["A"].remove(1)
    rec = env.change(EDIT_DESC)
    assert env.statuses(rec) == {"A": "verified"}
    assert rec.scale_results[0].drift == ()
    assert env.fakes["A"].item(1)[2] == "1001"


def test_step3_acknowledging_one_scales_drift_needs_its_own_token(env3):
    scale_holds(env3, "A", 1, price="11.00")
    scale_holds(env3, "B", 1, price="13.00")
    p = env3.svc.preview_changes(EDIT_DESC, read_scales=True)
    tokens = {d.scale: d.ack_token for d in p.drift}
    assert set(tokens) == {"A", "B"} and tokens["A"] != tokens["B"]
    rec = env3.svc.apply_change(p.preview_id, acknowledge_drift=[tokens["A"]])
    assert env3.statuses(rec) == {"A": "verified", "B": "drift_refused", "C": "verified"}


def test_step3_an_acknowledgement_names_exactly_what_was_seen(env):
    scale_holds(env, "A", 1, price="11.00")
    p = env.svc.preview_changes(EDIT_DESC, read_scales=True)
    token = p.drift[0].ack_token
    scale_holds(env, "A", 1, price="12.00")  # the scale changed again after the look
    rec = env.svc.apply_change(p.preview_id, acknowledge_drift=[token])
    assert env.statuses(rec) == {"A": "drift_refused"}
    assert env.fakes["A"].messages_of("GP") == []


def test_step3_warnings_are_not_drift_acknowledgements(env):
    scale_holds(env, "A", 1, price="11.00")
    p = env.svc.preview_changes(EDIT_DESC, read_scales=True)
    rec = env.svc.apply_change(p.preview_id, acknowledge_warnings=True)
    assert env.statuses(rec) == {"A": "drift_refused"}


def test_step3_other_records_are_not_sent_when_one_is_refused(env):
    scale_holds(env, "A", 2, price="21.00")
    rec = env.change([
        {"action": "edit", "target_plu": "1001", "price": "10.10"},
        {"action": "edit", "target_plu": "1002", "price": "20.10"},
    ])
    by_id = {i.item_id: i.status for i in rec.scale_results[0].items}
    assert by_id == {1: "not_sent", 2: "drift_refused"}
    assert env.fakes["A"].messages_of("GP") == []


def test_step3_an_add_is_refused_when_the_id_holds_something_on_that_scale(env):
    env.fakes["A"].preload(("4", "7777", "7777", "Stranger Item", "3.33", "", "1", "1", "0", "0", "0", "0",
                            "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0"))
    p = env.svc.preview_changes([{"action": "add", "plu": "4321", "description": "New", "price": "1.00"}],
                                read_scales=True)
    assert p.ok and [d.kind for d in p.drift] == ["id_occupied"]
    assert p.drift[0].ack_token is None  # nothing can allow it
    rec = env.svc.apply_change(p.preview_id, acknowledge_warnings=True,
                               acknowledge_drift=["A:4:anything", "anything"])
    assert env.statuses(rec) == {"A": "drift_refused"}
    assert env.fakes["A"].item(4)[3] == "Stranger Item"


def test_step3_a_recorded_orphan_can_be_replaced_when_asked_by_name(env):
    stranger = ("4", "7777", "7777", "Stranger Item", "3.33", "", "1", "1", "0", "0", "0", "0",
                "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0")
    env.fakes["A"].preload(stranger)
    env.svc.mark_orphan(4, "A", "left over test item")
    p = env.svc.preview_changes(
        [{"action": "add", "plu": "4321", "description": "New", "price": "1.00", "replace_orphan_id": 4}],
        read_scales=True,
    )
    assert p.ok and "orphan_replaced" in [w.code for w in p.warnings]
    rec = env.svc.apply_change(p.preview_id, acknowledge_warnings=True)
    assert rec.success
    assert env.fakes["A"].item(4)[3] == "New"


def test_step3_an_orphan_is_not_replaced_by_a_plain_add(env):
    env.fakes["A"].preload(("4", "7777", "7777", "Stranger Item", "3.33", "", "1", "1", "0", "0", "0", "0",
                            "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0"))
    env.svc.mark_orphan(4, "A", "left over test item")
    p = env.svc.preview_changes([{"action": "add", "plu": "4321", "description": "New", "price": "1.00"}])
    assert p.ok and p.differences[0].item_id != 4  # the orphan id is skipped


# =====================================================================
# Step 4. Build from what the scale holds
# =====================================================================

def test_step4_the_record_sent_is_the_scales_own_with_only_the_asked_field_changed(env):
    scale_holds(env, "A", 1, price="11.00", field6="", f10="0.1399")
    p = env.svc.preview_changes(EDIT_DESC, read_scales=True)
    rec = env.svc.apply_change(p.preview_id, acknowledge_drift=[d.ack_token for d in p.drift])
    sent = env.fakes["A"].messages_of("GP")[0].records[0]
    assert sent[3] == "Test Cheddar Block"  # what was asked
    assert sent[4] == "11.00"  # the scale's price, not the catalog's 10.00
    assert sent[5] == ""  # the scale's field 6, not the catalog's space
    assert sent[9] == "0.1399"  # the scale's field 10
    assert sent[12] != "2026-01-01 00:00:00"  # a new time
    assert env.statuses(rec) == {"A": "verified"}


def test_step4_one_scales_unrelated_difference_does_not_reach_another(env3):
    scale_holds(env3, "A", 1, field6="")  # only A differs, in a field nobody asked about
    p = env3.svc.preview_changes(EDIT_PRICE, read_scales=True)
    assert [d.scale for d in p.drift] == ["A"]
    rec = env3.svc.apply_change(p.preview_id, acknowledge_drift=[p.drift[0].ack_token])
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    sent = {n: f.messages_of("GP")[0].records[0] for n, f in env3.fakes.items()}
    assert sent["A"][5] == ""  # A keeps its own
    assert sent["B"][5] == " " and sent["C"][5] == " "  # the others keep theirs
    assert all(s[4] == "12.00" for s in sent.values())


def test_step4_two_scales_with_different_unrelated_values_each_keep_their_own(env3):
    scale_holds(env3, "A", 1, f10="0.5")
    scale_holds(env3, "B", 1, f10="0.7")
    p = env3.svc.preview_changes(EDIT_PRICE, read_scales=True)
    rec = env3.svc.apply_change(p.preview_id, acknowledge_drift=[d.ack_token for d in p.drift])
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    assert env3.fakes["A"].item(1)[9] == "0.5"
    assert env3.fakes["B"].item(1)[9] == "0.7"
    assert env3.fakes["C"].item(1)[9] == "0"


def test_step4_a_value_the_scale_holds_that_cannot_be_sent_back_stops_the_scale(env):
    scale_holds(env, "A", 1, f10="bad\x7fvalue")  # not printable ASCII
    p = env.svc.preview_changes(EDIT_PRICE, read_scales=True)
    rec = env.svc.apply_change(p.preview_id, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])
    assert env.statuses(rec)["A"] in ("drift_refused", "read_failed")
    assert env.fakes["A"].messages_of("GP") == []


# =====================================================================
# Step 6. Read back
# =====================================================================

def test_step6_a_matching_read_back_is_verified_and_a_full_success(env3):
    rec = env3.change(EDIT_PRICE)
    assert env3.statuses(rec) == {n: "verified" for n in "ABC"}
    assert rec.success and rec.verified and rec.outcome == "success"
    for fake in env3.fakes.values():
        assert fake.kinds().count("LP") == 2  # the read first and the read back
    assert env3.svc.get_item(plu="1001").scales["A"].last_read.price == "12.00"


def test_step6_an_ok_the_scale_did_not_keep_is_a_mismatch(env):
    env.fakes["A"].delay_reply(0.0, "GP", times=1, apply=False)  # says Ok, keeps nothing
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "mismatch"}
    result = rec.scale_results[0]
    assert result.items[0].status == "mismatch"
    assert len(result.mismatches) == 1 and result.mismatches[0].kind == "mismatch"
    assert result.mismatches[0].held.price == "10.00"  # what the scale holds
    assert not rec.success and not rec.verified
    assert env.catalog.get_by_id(1).price == "10.00"
    assert env.fakes["A"].item(1)[4] == "10.00"


def test_step6_a_mismatch_is_never_called_accepted_not_verified(env):
    env.fakes["A"].delay_reply(0.0, "GP", times=1, apply=False)
    rec = env.change(EDIT_PRICE)
    assert "accepted_not_verified" not in {i.status for r in rec.scale_results for i in r.items}


def test_step6_a_mismatch_where_the_scale_kept_something_else(env):
    # The scale keeps a record with a different price from the one sent.
    fake = env.fakes["A"]
    orig_handle = fake._handle

    def tampering(conn, raw):
        ok = orig_handle(conn, raw)
        if raw.startswith(b"GP"):
            fake.preload(with_changes(fake.item(1), price="99.99"))
        return ok

    fake._handle = tampering
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "mismatch"}
    assert rec.scale_results[0].mismatches[0].held.price == "99.99"
    assert "price" in rec.scale_results[0].mismatches[0].fields


def test_step6_the_read_back_waits_out_the_gap_after_the_write(make_env):
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


def test_step6_an_error_batch_is_confirmed_by_the_read_back(make_env):
    env = make_env({"A": True}, fake_options={"store_valid_in_errored_batch": True})
    env.fakes["A"].error_on_ids(2)
    rec = env.change([
        {"action": "edit", "target_plu": "1001", "price": "10.50"},
        {"action": "edit", "target_plu": "1002", "price": "20.50"},
    ])
    by_id = {i.item_id: i.status for i in rec.scale_results[0].items}
    assert env.fakes["A"].item(1)[4] == "10.50"  # it landed
    # Stage F H4: Error with data stored must not read as success, even with the read back.
    assert by_id[1] == "unknown", "the record is there, but the scale said Error"
    assert by_id[2] in ("rejected", "unknown")
    assert env.fakes["A"].item(2)[4] == "20.00"
    assert not rec.success


# =====================================================================
# Step 7. Never resend after sending began
# =====================================================================

@pytest.mark.parametrize("fault", ["stall", "drop_before_reply", "delay"])
def test_step7_a_lost_reply_is_never_resent_and_the_next_attempt_starts_from_the_read(make_env, fault):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    if fault == "stall":
        fake.stall("GP", times=None)
    elif fault == "drop_before_reply":
        fake.drop_before_reply("GP", times=None)
    else:
        fake.delay_reply(1.5, "GP", times=1)
    rec = env.change(EDIT_PRICE)
    assert env.statuses(rec) == {"A": "unknown"}
    assert len(fake.messages_of("GP")) == 1
    assert fake.kinds().count("bX01XX") == 1 and fake.kinds().count("RI") == 1
    assert fake.item(1)[4] == "12.00"  # it did land. The service cannot tell yet.

    fake.clear_faults()
    cx_before = fake.kinds().count("CX")
    p = env.svc.preview_changes(EDIT_DESC, read_scales=True)  # the next attempt: identity and read first
    assert fake.kinds().count("CX") == cx_before + 1
    assert [d.kind for d in p.drift] == ["differs"] and p.drift[0].fields["price"].after == "12.00"
    rec2 = env.svc.apply_change(p.preview_id, acknowledge_drift=[p.drift[0].ack_token])
    assert env.statuses(rec2) == {"A": "verified"}
    assert fake.item(1)[4] == "12.00"  # the price that landed is kept
    assert fake.item(1)[3] == "Test Cheddar Block"


def test_step7_the_old_price_is_not_written_back_after_a_lost_reply(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1)
    env.change(EDIT_PRICE)
    env.fakes["A"].clear_faults()
    rec = env.change(EDIT_DESC)  # not acknowledged
    assert env.statuses(rec) == {"A": "drift_refused"}
    assert env.fakes["A"].item(1)[4] == "12.00"


# =====================================================================
# Interrupted changes
# =====================================================================

def test_a_change_that_never_finished_blocks_its_items_until_resolved(make_env, monkeypatch):
    env = make_env({"A": True})
    p = env.preview(EDIT_PRICE)

    def die(item):
        raise KeyboardInterrupt("the process died here")

    monkeypatch.setattr(env.catalog, "upsert_item", die)
    with pytest.raises(KeyboardInterrupt):
        env.svc.apply_change(p.preview_id)
    monkeypatch.undo()

    open_ids = env.svc.list_scales()[0].interrupted_change_ids
    assert len(open_ids) == 1
    assert env.svc.get_item(plu="1001").interrupted_change_ids == open_ids
    assert env.svc.list_changes()[0].outcome == "in_progress"

    p2 = env.svc.preview_changes(EDIT_DESC)
    assert "interrupted_change" in [w.code for w in p2.warnings]
    sent_before = len(env.fakes["A"].messages)
    with pytest.raises(errors.InterruptedChange):
        env.svc.apply_change(p2.preview_id)
    assert len(env.fakes["A"].messages) == sent_before

    env.svc.resolve_interrupted_change(open_ids[0], "looked at the scale", requested_by="test")
    assert env.svc.list_scales()[0].interrupted_change_ids == ()
    p3 = env.svc.preview_changes(EDIT_DESC, read_scales=True)
    assert [d.kind for d in p3.drift] == ["differs"]  # what landed shows as drift


def test_resolving_a_change_that_is_not_interrupted_is_refused(env):
    rec = env.change(EDIT_PRICE)
    with pytest.raises(errors.ChangeNotFound):
        env.svc.resolve_interrupted_change(rec.change_id, "no")


# =====================================================================
# compare_scales
# =====================================================================

def test_compare_lists_the_differences_by_scale_and_writes_nothing(env3):
    scale_holds(env3, "B", 2, price="21.00")
    env3.fakes["C"].remove(3)
    env3.fakes["A"].preload(("9", "7777", "7777", "Stranger", "3.33", "", "1", "1", "0", "0", "0", "0",
                             "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0"))
    results = {c.scale: c for c in env3.svc.compare_scales()}
    assert [(d.kind, d.item_id) for d in results["A"].differences] == [("not_in_catalog", 9)]
    assert [(d.kind, d.item_id) for d in results["B"].differences] == [("differs", 2)]
    assert [(d.kind, d.item_id) for d in results["C"].differences] == [("missing", 3)]
    assert all(c.status == "compared" for c in results.values())
    for fake in env3.fakes.values():
        assert fake.messages_of("GP") == [] and "RI" not in fake.kinds()


def test_compare_with_no_differences(env):
    (result,) = env.svc.compare_scales()
    assert result.status == "compared" and result.differences == () and result.items_read == 3


def test_compare_names_a_wrong_scale_and_an_unreachable_one(make_env):
    env = make_env({"A": True, "B": True}, connect_attempts=1)
    env.fakes["A"].serial = "SOMEONE-ELSE"
    env.fakes["B"].stop_listening()
    results = {c.scale: c.status for c in env.svc.compare_scales()}
    assert results == {"A": "wrong_scale", "B": "unreachable"}


def test_compare_limited_to_named_scales(env3):
    env3.svc.compare_scales(["B"])
    assert env3.silent("A", "C") and not env3.silent("B")
