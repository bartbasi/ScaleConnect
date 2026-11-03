"""What keeps a real miss on the list, what keeps a false one off it, and what the list says. Fake scales on localhost, made up items.

A verified result says a scale holds what was sent. For an edit that is the scale's own record with only
the asked for fields changed, so it can be verified and still differ from the catalog. These read the catalog's
answer, and check it asked no scale.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from scaleconnect.models import Item, placeholder_record
from service_env import SEED

STAMP = "2026-01-01 00:00:00"
JUNK = b"XX99\t\n0\r"  # not a read block. The scale answered, so this is no sign of a freeze
EDIT = [{"action": "edit", "target_plu": "1001", "price": "11.00"}]
TWO = {"A": True, "B": True}
THREE = {"A": True, "B": True, "C": True}


def ticking_clock():
    now = [datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)]

    def clock():
        now[0] += timedelta(seconds=1)
        return now[0]

    return clock


@pytest.fixture
def make(make_env):
    def _make(flags, **kw):
        return make_env(flags, clock=ticking_clock(), **kw)

    return _make


def contacts(env):
    return {name: (fake.connection_count, len(fake.messages)) for name, fake in env.fakes.items()}


def read_fails_at_apply(env, name, changes=EDIT, *, ack=False, tokens=()):
    p = env.preview(changes, read_scales=False)
    env.fakes[name].raw_reply(JUNK, kind="LP", times=None)
    rec = env.svc.apply_change(p.preview_id, acknowledge_warnings=ack, acknowledge_drift=list(tokens))
    env.fakes[name].clear_faults()
    return rec


def with_drift_acknowledged(env, changes, ack=False):
    p = env.preview(changes, read_scales=True)
    return env.svc.apply_change(p.preview_id, acknowledge_warnings=ack, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])


def rows(env):
    return [(u.scale, u.kind, u.plu, u.status) for u in env.svc.list_unfinished()]


# ---------------------------------------------------------------- a real miss stays listed


def test_a_later_edit_that_verified_but_left_the_price_behind_stays_listed(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    rec = with_drift_acknowledged(env, [{"action": "edit", "target_plu": "1001", "description": "New Name"}])
    assert env.statuses(rec) == {"A": "verified", "B": "verified"} and env.fakes["B"].item(1)[4] == "10.00"
    seen = contacts(env)
    found = env.svc.list_unfinished()
    assert contacts(env) == seen, "no scale was contacted"
    assert [(u.scale, u.kind, u.status) for u in found] == [("B", "missed_change", "verified")]
    assert found[0].differs_in == ("price",) and found[0].catalog_took_it is True and found[0].change_id == rec.change_id
    assert found[0].repeat == {"tool": "scale_preview_settle_drift", "scale": "B", "take": "catalog", "plu": "1001"}
    assert "may differ from the catalog" in found[0].message and "New Name" not in found[0].message


def test_it_stays_listed_after_a_compare_shows_the_difference(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    with_drift_acknowledged(env, [{"action": "edit", "target_plu": "1001", "description": "New Name"}])
    assert env.svc.compare_scales(["B"])[0].differences != ()
    assert [u.scale for u in env.svc.list_unfinished()] == ["B"]


def test_a_settle_that_took_a_scales_record_lists_the_scales_that_kept_the_new_price(make):
    env = make(THREE)
    read_fails_at_apply(env, "B")
    p = env.svc.preview_settle_drift(scale="B", take="scale", plu="1001")
    env.svc.apply_change(p.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])
    assert env.catalog.get_by_plu("1001").price == "10.00" and env.fakes["A"].item(1)[4] == "11.00"
    found = env.svc.list_unfinished()
    assert sorted(u.scale for u in found) == ["A", "C"] and all(u.differs_in == ("price",) for u in found)


def test_a_repeat_of_a_plu_change_that_left_the_price_behind_stays_listed(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    env.fakes["B"].error_on_ids(1)
    with_drift_acknowledged(env, [{"action": "edit", "target_plu": "1001", "plu": "1999"}], ack=True)
    env.fakes["B"].clear_faults()
    repeat = env.svc.list_unfinished()[0].repeat
    rec = with_drift_acknowledged(env, [repeat], ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"} and env.fakes["B"].item(1)[4] == "10.00"
    assert [(u.scale, u.kind, u.differs_in) for u in env.svc.list_unfinished()] == [("B", "missed_change", ("price",))]


def test_a_settle_to_the_catalog_ends_it(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    with_drift_acknowledged(env, [{"action": "edit", "target_plu": "1001", "description": "New Name"}])
    p = env.svc.preview_settle_drift(scale="B", take="catalog", plu="1001")
    env.svc.apply_change(p.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])
    assert env.svc.list_unfinished() == []


def test_a_later_read_that_matches_but_for_the_time_ends_it(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    with_drift_acknowledged(env, [{"action": "edit", "target_plu": "1001", "description": "New Name"}])
    assert env.svc.list_unfinished()
    fixed = env.catalog.get_by_plu("1001").replace(timestamp="2026-01-02 00:00:00")
    env.fakes["B"].preload(tuple(fixed.to_fields()))
    env.svc.compare_scales(["B"])
    assert env.svc.list_unfinished() == []


def test_a_read_from_before_that_record_does_not_end_it(make):
    env = make(TWO)
    env.svc.compare_scales(["B"])
    read_fails_at_apply(env, "B")
    with_drift_acknowledged(env, [{"action": "edit", "target_plu": "1001", "description": "New Name"}])
    assert [u.scale for u in env.svc.list_unfinished()] == ["B"]


# ---------------------------------------------------------------- a scale whose writes are off stays out


def test_a_scale_with_writes_off_after_a_miss_is_a_persons_choice(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    env.set_write_enabled("B", False)
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "12.00"}], read_scales=False)
    assert env.statuses(rec)["B"] == "skipped_not_write_enabled"
    assert env.svc.list_unfinished() == []


def test_a_scale_with_writes_back_on_is_listed_because_it_really_is_behind(make):
    env = make(TWO)
    env.change(EDIT)
    env.set_write_enabled("B", False)
    env.change([{"action": "edit", "target_plu": "1001", "price": "12.00"}], read_scales=False)
    assert env.svc.list_unfinished() == []
    env.set_write_enabled("B", True)
    assert [(u.scale, u.status, u.differs_in) for u in env.svc.list_unfinished()] == [("B", "skipped_not_write_enabled", ("price",))]


def test_a_verified_row_on_a_scale_whose_writes_are_now_off_stays_out(make):
    env = make(TWO)
    env.change(EDIT)
    env.set_write_enabled("B", False)
    env.change([{"action": "edit", "target_plu": "1001", "price": "12.00"}], read_scales=False)
    assert env.svc.list_unfinished() == []


# ---------------------------------------------------------------- no false cards


def test_a_table_where_every_scale_verified_makes_no_card_whatever_the_change(make):
    """Every path that writes a record to scale_state, on items with different fields."""
    env = make(THREE)

    def clean():
        assert env.svc.list_unfinished() == [], rows(env)

    assert env.statuses(env.change(EDIT)) == {"A": "verified", "B": "verified", "C": "verified"}
    clean()
    env.change([{"action": "edit", "target_plu": "1002", "description": "New Words", "price": "21.00"}])
    clean()
    env.change([{"action": "edit", "target_plu": "1003", "plu": "1993"}], ack=True)
    clean()
    env.change([{"action": "add", "plu": "1777", "description": "Added", "price": "7.00", "sold_by": "piece"}])
    clean()
    env.change([{"action": "edit", "target_plu": "1777", "price": "7.00"}])  # the same values, sent again
    clean()
    env.change([{"action": "delete", "target_plu": "1002"}], ack=True)
    clean()
    new = env.change([{"action": "add", "plu": "1888", "description": "On A Reused Id", "price": "8.00"}])
    assert new.changes[0].item_id == 2, "the id came free, and a new item took it"
    clean()
    # A settle each way, on a real difference, and then the same again on the other scales.
    env.fakes["A"].preload(tuple(env.catalog.get_by_plu("1001").replace(price="12.00").to_fields()))
    p = env.svc.preview_settle_drift(scale="A", take="catalog", plu="1001")
    env.svc.apply_change(p.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])
    clean()
    result = env.svc.reload_scale("B", accept_differences=True, requested_by="test")
    assert result.ok is True
    clean()
    assert all(c.differences == () for c in env.svc.compare_scales())
    clean()


def test_a_write_past_the_end_of_the_table_with_placeholders_makes_no_card(make):
    """A holds an orphan at id 8, and B ends at id 3, so an add at 8 puts placeholders on B."""
    env = make(TWO)
    env.fakes["A"].preload(*[tuple(placeholder_record(i, STAMP).to_fields()) for i in range(4, 8)], tuple(Item(8, "1777", "1777", "Old Test", "1.00", timestamp=STAMP).to_fields()))
    assert env.svc.mark_orphans_from_read("A").marked == (8,)
    add = env.preview([{"action": "add", "plu": "1888", "description": "New", "price": "2.00", "replace_orphan_id": 8}], read_scales=True)
    rec = env.svc.apply_change(add.preview_id, acknowledge_warnings=True)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"}, rec.summary
    assert rec.scale_results[1].fillers_landed == (4, 5, 6, 7)
    assert env.svc.list_unfinished() == []


def test_a_difference_only_in_the_time_makes_no_row(make):
    """A change to one scale gives the catalog a new time, and leaves the others on the old one."""
    env = make(TWO)
    env.change(EDIT)
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "11.00"}], scales=["A"], ack=True, read_scales=False)
    assert env.statuses(rec) == {"A": "verified"}
    assert env.catalog.get_by_plu("1001").timestamp != env.fakes["B"].item(1)[12]
    assert env.svc.list_unfinished() == []


def test_a_placeholder_as_the_record_makes_no_row(make):
    env = make(TWO)
    env.catalog.record_scale_state(1, "B", "verified", "old", "2026-11-02T09:00:00+00:00", belief="present", record=placeholder_record(1, STAMP))
    assert env.svc.list_unfinished() == []


def test_a_record_that_cannot_be_read_makes_no_row_and_stops_nothing(make):
    env = make(TWO)
    env.catalog.record_scale_state(1, "B", "verified", "old", "2026-11-02T09:00:00+00:00", belief="present", record=env.catalog.get_by_id(1))
    db = env.catalog._conn()
    db.execute("UPDATE scale_state SET record = '{not json' WHERE scale = 'B' AND item_id = 1")
    db.commit()
    assert env.svc.list_unfinished() == []
    db.execute("UPDATE scale_state SET record = '[\"1\", \"too few\"]' WHERE scale = 'B' AND item_id = 1")
    db.commit()
    assert env.svc.list_unfinished() == []


def test_a_scale_not_in_the_config_makes_no_row_by_its_record(make):
    env = make(TWO)
    env.catalog.record_scale_state(1, "GONE", "verified", "old", "2026-11-02T09:00:00+00:00", belief="present", record=env.catalog.get_by_id(1).replace(price="99.00"))
    assert env.svc.list_unfinished() == []


def test_a_scale_left_out_with_no_earlier_row_stays_out(make):
    """The record is what says a scale differs, so with no record there is nothing to go on."""
    env = make(TWO)
    env.change(EDIT, scales=["A"], ack=True)
    assert env.svc.list_unfinished() == []


def test_a_scale_left_out_with_an_earlier_record_that_now_differs_is_listed(make):
    env = make(TWO)
    env.change(EDIT)
    env.change([{"action": "edit", "target_plu": "1001", "price": "13.00"}], scales=["A"], ack=True)
    assert [(u.scale, u.differs_in) for u in env.svc.list_unfinished()] == [("B", ("price",))]


# ---------------------------------------------------------------- what the catalog took


def lost_reply_on_every_scale(env, landed="B", changes=EDIT):
    for name, fake in env.fakes.items():
        fake.drop_before_reply(kind="GP", times=None, apply=name == landed)
    rec = env.change(changes, read_scales=False)
    for fake in env.fakes.values():
        fake.clear_faults()
    return rec


def test_a_lost_reply_on_an_unmoved_catalog_is_marked_as_not_taken(make):
    env = make(TWO)
    lost_reply_on_every_scale(env)
    assert env.catalog.get_by_plu("1001").price == "10.00"
    found = {u.scale: u for u in env.svc.list_unfinished()}
    assert found["B"].catalog_took_it is False and found["B"].repeat == {}
    assert "The catalog did not take this change." in found["B"].message
    assert "B may hold the new value." in found["B"].message
    assert "A settle to the catalog puts the old value back." in found["B"].message
    assert "To finish the change, make a new preview of it." in found["B"].message


def test_a_miss_the_catalog_took_is_marked_as_taken(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    assert env.svc.list_unfinished()[0].catalog_took_it is True


def test_a_settle_with_a_lost_reply_is_not_marked_as_not_taken(make):
    """That record is the catalog's own, so another settle is the right way to finish it."""
    env = make(TWO)
    read_fails_at_apply(env, "B")
    p = env.svc.preview_settle_drift(scale="B", take="catalog", plu="1001")
    env.fakes["B"].drop_before_reply(kind="GP", times=None, apply=True)
    env.svc.apply_change(p.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])
    env.fakes["B"].clear_faults()
    found = env.svc.list_unfinished()
    assert [(u.scale, u.status, u.catalog_took_it) for u in found] == [("B", "unknown", True)]


def test_a_reload_that_could_not_be_read_back_is_not_marked_as_not_taken(make):
    env = make({"A": True})
    env.svc.compare_scales()
    env.fakes["A"].raw_reply(JUNK, kind="LP", times=None)
    env.svc.reload_scale("A", requested_by="test")
    env.fakes["A"].clear_faults()
    found = env.svc.list_unfinished()
    assert found and all(u.catalog_took_it is True and u.status == "accepted_not_verified" for u in found)


# ---------------------------------------------------------------- a scale that gave no reply


def test_the_freeze_flag_is_copied_from_the_logged_result(make):
    env = make(TWO, reply_timeout_seconds=0.3)
    p = env.preview(EDIT, read_scales=False)
    env.fakes["B"].stall_read(times=None)
    rec = env.svc.apply_change(p.preview_id)
    env.fakes["B"].clear_faults()
    assert [r.may_be_frozen for r in rec.scale_results if r.scale == "B"] == [True]
    found = env.svc.list_unfinished()
    assert [(u.scale, u.may_be_frozen) for u in found] == [("B", True)]
    assert "it may have frozen" in found[0].message and "Do not read it" in found[0].message
    assert "Settle" not in found[0].message, "a settle reads that scale"


def test_a_read_that_failed_with_an_answer_has_no_freeze_flag(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    assert [(u.scale, u.may_be_frozen) for u in env.svc.list_unfinished()] == [("B", False)]


def test_a_later_refusal_with_no_reply_adds_the_flag_to_an_earlier_miss(make):
    env = make(TWO, reply_timeout_seconds=0.3)
    first = read_fails_at_apply(env, "B")
    p = env.preview([{"action": "edit", "target_plu": "1001", "price": "13.00"}], read_scales=False)
    for fake in env.fakes.values():
        fake.stall_read(times=None)
    env.svc.apply_change(p.preview_id)
    for fake in env.fakes.values():
        fake.clear_faults()
    found = {u.scale: u for u in env.svc.list_unfinished()}
    assert found["B"].change_id == first.change_id and found["B"].may_be_frozen is True


def test_a_verified_result_on_that_item_ends_the_flag(make):
    env = make(TWO, reply_timeout_seconds=0.3)
    p = env.preview(EDIT, read_scales=False)
    env.fakes["B"].stall_read(times=None)
    env.svc.apply_change(p.preview_id)
    env.fakes["B"].clear_faults()
    q = env.svc.preview_settle_drift(scale="B", take="catalog", plu="1001")
    env.svc.apply_change(q.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in q.drift if d.ack_token])
    read_fails_at_apply(env, "B", [{"action": "edit", "target_plu": "1001", "price": "14.00"}])
    assert [(u.scale, u.may_be_frozen) for u in env.svc.list_unfinished()] == [("B", False)]


# ---------------------------------------------------------------- a settle that ended a miss


def settle_b(env):
    p = env.svc.preview_settle_drift(scale="B", take="catalog", plu="1001")
    assert p.ok, [e.to_dict() for e in p.errors]
    return env.svc.apply_change(p.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])


def test_the_service_says_a_settle_ended_a_miss_whose_catalog_had_moved(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    assert env.svc.settle_ended_a_miss(settle_b(env)) is True


def test_the_service_says_a_settle_on_plain_drift_ended_no_miss(make):
    env = make(TWO)
    env.fakes["B"].preload(tuple(env.catalog.get_by_plu("1001").replace(price="18.99").to_fields()))
    assert env.svc.settle_ended_a_miss(settle_b(env)) is False


def test_the_service_says_an_unmoved_catalog_ended_no_miss(make):
    env = make(TWO)
    lost_reply_on_every_scale(env)
    assert env.svc.settle_ended_a_miss(settle_b(env)) is False


def test_only_the_first_settle_counts_as_ending_the_miss(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    assert env.svc.settle_ended_a_miss(settle_b(env)) is True
    env.fakes["B"].preload(tuple(env.catalog.get_by_plu("1001").replace(price="18.99").to_fields()))
    assert env.svc.settle_ended_a_miss(settle_b(env)) is False


def test_a_change_that_was_not_a_settle_ended_no_miss(make):
    env = make(TWO)
    assert env.svc.settle_ended_a_miss(env.change(EDIT)) is False


# ---------------------------------------------------------------- one bad log row


def damage_a_log_row(env):
    env.catalog._conn().execute("INSERT INTO change_log (change_id, event, payload, at, scale) VALUES ('x', 'scale_result', '{not json', '2026-11-02T09:00:00+00:00', 'B')")


def test_one_bad_log_row_with_a_miss_open_says_the_list_could_not_be_built(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    damage_a_log_row(env)
    found = env.svc.list_unfinished()
    assert [u.kind for u in found] == ["list_failed"]
    assert "could not be built" in found[0].message and "Do not read it as all clear" in found[0].message
    assert "{not" not in found[0].message


def test_one_bad_log_row_keeps_what_needs_no_log(make):
    env = make(TWO)
    env.fakes["B"].error_on_ids(2)
    env.change([{"action": "delete", "target_plu": "1002"}], ack=True)
    env.fakes["B"].clear_faults()
    damage_a_log_row(env)
    assert [u.kind for u in env.svc.list_unfinished()] == ["delete", "list_failed"]


def test_one_bad_log_row_with_nothing_open_is_no_trouble(make):
    env = make(TWO)
    damage_a_log_row(env)
    assert env.svc.list_unfinished() == []


def test_one_bad_log_row_does_not_stop_the_list_of_scales(make):
    env = make(TWO)
    env.change(EDIT)
    env.catalog._conn().execute("INSERT INTO change_log (change_id, event, payload, at, scale) VALUES ('x', 'scale_result', '{not json', '2026-11-02T09:00:00+00:00', 'B')")
    assert {info.name for info in env.svc.list_scales()} == {"A", "B"}


# ---------------------------------------------------------------- checks a reviewer took out one at a time, which no test caught


def test_a_verified_result_ends_a_miss_even_if_a_later_refusal_rewrites_the_row(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    with_drift_acknowledged(env, [{"action": "edit", "target_plu": "1001", "price": "12.00"}])
    assert env.svc.list_unfinished() == []
    for fake in env.fakes.values():
        fake.error_on_ids(1)
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "13.00"}], read_scales=False)
    assert env.statuses(rec) == {"A": "rejected", "B": "rejected"}
    assert env.svc.list_unfinished() == [], "the row says rejected, and the old miss must not come back with it"


def test_a_new_add_on_a_reused_id_clears_misses_about_the_old_item(make):
    env = make(TWO)
    env.fakes["B"].drop_before_reply(kind="GP", times=None, apply=True)
    rec = env.change([{"action": "delete", "target_plu": "1002"}], ack=True, read_scales=False)
    env.fakes["B"].clear_faults()
    assert env.statuses(rec) == {"A": "verified", "B": "unknown"}
    env.svc.compare_scales(["B"])  # shows a placeholder at the id, so B holds nothing there and the id comes free
    assert env.svc.list_unfinished() == []
    new = env.change([{"action": "add", "plu": "1888", "description": "On A Reused Id", "price": "8.00"}], scales=["A"], ack=True)
    assert new.changes[0].item_id == 2 and env.statuses(new) == {"A": "verified"}
    assert env.svc.list_unfinished() == [], "B was left out of this add, and the old delete is about another item"


def test_an_add_with_a_lost_reply_moves_the_catalog_so_a_scale_that_missed_it_is_listed(make):
    env = make(TWO)
    p = env.preview([{"action": "add", "plu": "1777", "description": "Added", "price": "7.00"}], read_scales=False)
    env.fakes["A"].drop_before_reply(kind="GP", times=None, apply=False)
    env.fakes["B"].raw_reply(JUNK, kind="LP", times=None)
    rec = env.svc.apply_change(p.preview_id)
    for fake in env.fakes.values():
        fake.clear_faults()
    assert env.statuses(rec) == {"A": "unknown", "B": "read_failed"}
    assert env.catalog.get_by_plu("1777") is not None
    found = {u.scale: u for u in env.svc.list_unfinished()}
    assert found["B"].status == "read_failed" and found["B"].kind == "missed_change" and found["B"].catalog_took_it is True


def test_a_state_row_with_no_log_entry_is_still_listed(make):
    env = make(TWO)
    env.catalog.record_scale_state(1, "B", "unknown", "not-in-the-log", "2026-11-02T09:00:00+00:00")
    found = env.svc.list_unfinished()
    assert [(u.scale, u.kind, u.status, u.change_id) for u in found] == [("B", "missed_change", "unknown", "not-in-the-log")]


def test_a_refused_settle_that_takes_the_scale_lists_nothing_and_moves_nothing(make):
    """A settle names one scale, so it cannot have landed and also not been sent. It needs no line of its own in the walk."""
    env = make(TWO)
    env.fakes["B"].preload(tuple(env.catalog.get_by_plu("1001").replace(price="18.99").to_fields()))
    p = env.svc.preview_settle_drift(scale="B", take="scale", plu="1001")
    env.fakes["B"].preload(tuple(env.catalog.get_by_plu("1001").replace(price="19.99").to_fields()))
    rec = env.svc.apply_change(p.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])
    assert env.statuses(rec) == {"B": "drift_refused"} and env.catalog.get_by_plu("1001").price == "10.00"
    assert env.svc.list_unfinished() == []


def test_a_settle_that_takes_the_scale_ends_that_scales_miss(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    p = env.svc.preview_settle_drift(scale="B", take="scale", plu="1001")
    env.svc.apply_change(p.preview_id, acknowledge_warnings=True, acknowledge_drift=[d.ack_token for d in p.drift if d.ack_token])
    assert "B" not in {u.scale for u in env.svc.list_unfinished()}


def test_the_fields_on_the_row_reach_its_dict(make):
    env = make(TWO)
    read_fails_at_apply(env, "B")
    data = env.svc.list_unfinished()[0].to_dict()
    assert data["catalog_took_it"] is True and data["may_be_frozen"] is False and data["differs_in"] == [] and data["at"]
    assert dataclasses.asdict(env.svc.list_unfinished()[0])["status"] == "read_failed"
