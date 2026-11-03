# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round 6 recheck: N2, N3, N4, N10 and N12. Fake scales on localhost, made up items. No Square call."""

from __future__ import annotations

import scaleconnect.mcp_server as server_module
from scaleconnect.models import Item

from test_mcp_three_part_rule import EDIT, JUNK, Session, frozen_miss, ticking_clock

STAMP = "2026-01-01 00:00:00"


def rows(env):
    return [(u.scale, u.kind, u.plu, u.may_be_frozen) for u in env.svc.list_unfinished()]


def junk_at_apply(env, session, changes):
    preview = session.preview(changes, read_scales=False)
    env.fakes["B"].raw_reply(JUNK, kind="LP", times=None)
    data = session.apply(preview)
    env.fakes["B"].clear_faults()
    return data


# ---------------------------------------------------------------- N2


def test_an_old_freeze_does_not_come_back_on_a_new_answered_miss(make_env):
    env = make_env({"A": True, "B": True}, clock=ticking_clock(), reply_timeout_seconds=0.3)
    session = Session(env)
    frozen_miss(env, session)
    env.fakes["B"].clear_faults()
    env.fakes["B"].preload(tuple(env.catalog.get_by_plu("1001").to_fields()))
    env.svc.compare_scales(["B"])
    assert rows(env) == [], "a matching read ends the row"
    data = junk_at_apply(env, session, [{"action": "edit", "target_plu": "1001", "price": "12.00"}])
    assert data["scales_may_be_frozen"] == []
    assert rows(env) == [("B", "missed_change", "1001", False)], "the new miss is open, not frozen"
    listed = session.data("scale_list_scales")
    assert listed["scales_may_be_frozen"] == [] and "MAY BE FROZEN" not in listed["summary"]


def test_a_read_that_answers_after_the_freeze_drops_the_flag_and_keeps_the_miss(make_env):
    env = make_env({"A": True, "B": True}, clock=ticking_clock(), reply_timeout_seconds=0.3)
    session = Session(env)
    frozen_miss(env, session)
    env.fakes["B"].clear_faults()
    assert rows(env) == [("B", "missed_change", "1001", True)]
    env.svc.compare_scales(["B"])  # B still holds the old price, so the miss stands
    assert rows(env) == [("B", "missed_change", "1001", False)]
    assert env.svc.scales_may_be_frozen() == []


def test_a_freeze_with_no_read_since_still_spreads_to_every_row_on_that_scale(make_env):
    env = make_env({"A": True, "B": True}, clock=ticking_clock(), reply_timeout_seconds=0.3)
    session = Session(env)
    frozen_miss(env, session)
    env.fakes["B"].clear_faults()
    # Round 9 N1: the next change leaves B out, with a warning to acknowledge, and B misses it too.
    seen = len(env.fakes["B"].messages)
    preview = session.preview([{"action": "edit", "target_plu": "1002", "price": "22.00"}], read_scales=False)
    assert preview["left_out"] == ["B"]
    session.apply(preview, acknowledge_warnings=True)
    assert len(env.fakes["B"].messages) == seen
    assert {r[3] for r in rows(env)} == {True} and len(rows(env)) == 2


# ---------------------------------------------------------------- N3


def test_a_reload_with_no_read_back_logs_the_flag_and_the_rows_carry_it(make_env):
    env = make_env({"A": True, "B": True}, clock=ticking_clock(), reply_timeout_seconds=0.3, read_back_window_seconds=1.0)
    env.svc.compare_scales()
    env.fakes["B"].stall_read(times=None)
    result = env.svc.reload_scale("B", requested_by="test")
    env.fakes["B"].clear_faults()
    assert result.may_be_frozen is True
    logged = env.svc.get_change(result.change_id).scale_results[-1]
    assert logged.may_be_frozen is True
    listed = rows(env)
    assert listed and all(r[0] == "B" and r[3] is True for r in listed)
    data = Session(env).data("scale_list_scales")
    assert data["scales_may_be_frozen"] == ["B"] and data["next_step"].startswith(server_module._FROZEN_NEXT_STEP)


# ---------------------------------------------------------------- N4


def test_a_one_scale_stall_that_moved_nothing_is_still_named(make_env):
    env = make_env({"B": True}, clock=ticking_clock(), reply_timeout_seconds=0.3)
    session = Session(env)
    frozen_miss(env, session)
    env.fakes["B"].clear_faults()
    data = session.data("scale_list_scales")
    assert data["unfinished"] == [] and data["scales_may_be_frozen"] == ["B"]
    assert "B: connected, then gave no reply, and no read of it has answered since, so it MAY BE FROZEN" in data["summary"]
    assert server_module._FROZEN_NEXT_STEP in data["next_step"]
    env.svc.compare_scales(["B"])
    data = session.data("scale_list_scales")
    assert data["scales_may_be_frozen"] == [] and "next_step" not in data


def test_the_list_tool_says_a_stall_in_any_read_is_seen():
    """Round 7 M1: a stall in a preview, a compare or the page's own check is now kept too."""
    description = server_module.build_server.__code__.co_consts  # the text is built inside build_server
    text = " ".join(c for c in description if isinstance(c, str))
    assert "It cannot see a stall in a preview or a compare" not in text
    assert "and the web page's own checks and reviews. If the log cannot be read, it names every scale." in text


# ---------------------------------------------------------------- N10


def test_a_settle_that_changes_sold_by_or_tax_names_square(make_env):
    env = make_env({"A": True})
    session = Session(env)
    held = Item(2, "1002", "1002", "Test Brie", "20.00", field6="", timestamp=STAMP, fields_7_12=("1", "1", "0", "0", "0", "0"))
    env.fakes["A"].preload(tuple(held.to_fields()))
    preview = session.data("scale_preview_settle_drift", scale="A", take="catalog", plu="1002")
    assert preview["ok"], preview["summary"]
    assert server_module._SETTLE_CHANGED_SALE_NEXT_STEP in preview["next_step"]
    settled = session.apply(preview, acknowledge_warnings=True, acknowledge_drift=preview["drift_tokens_to_acknowledge"])
    assert settled["still_to_do"] == ["square", "labels"] and server_module._SETTLE_CHANGED_SALE_NEXT_STEP in settled["next_step"]


# ---------------------------------------------------------------- N12


def test_an_old_partial_result_reads_as_when_it_ran(make_env):
    env = make_env({"A": True, "B": True}, clock=ticking_clock())
    session = Session(env)
    applied = session.apply_with_read_failing("B", EDIT)
    assert applied["outcome"] == "partial" and applied["still_to_do"] == []
    assert "Square and the labels are not done either" in applied["next_step"]
    old = session.data("scale_get_change", change_id=applied["change_id"])
    assert old["next_step"].startswith("When this ran: ") and old["still_to_do_as_of"] == "when this ran"
    assert old["summary"].splitlines()[-1].startswith("Next: When this ran: ")
