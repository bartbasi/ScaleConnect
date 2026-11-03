"""Round 8 recheck fixes, the library and the MCP server: L1, L2, L3, L4 and L5.

L1: scale_get_change on a change that left a scale out carries the freeze rule, names that scale, and never
says to make a preview that would read it.
L2: a no reply the log cannot take still counts, and goes into the log once it works.
L3: the place in the log decides what clears a no reply, not the clock. A damaged time, a time with no offset
or a time in the future cannot lock a scale out, and a read saved with a future time cannot end a later no reply.
L4: a failed connect is told apart from a scale that connected and then gave no reply.
L5: a flagged scale that Claude leaves out with scales is left out the way the page does it, and stays listed.

Fake scales on localhost, made up items.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

import scaleconnect.mcp_server as server_module
from scaleconnect.service import ScaleConnectService

from test_mcp_three_part_rule import EDIT, Session, ticking_clock

THREE = {"A": True, "B": True, "C": True}


def lp(env, scale):
    return env.fakes[scale].kinds().count("LP")


def three(make_env, **kw):
    kw.setdefault("reply_timeout_seconds", 0.3)
    kw.setdefault("connect_attempts", 1)
    return make_env(THREE, clock=ticking_clock(), **kw)


def b_silent_on_a_compare(env):
    env.fakes["B"].stall_read(times=None)
    assert env.svc.compare_scales(["B"])[0].may_be_frozen
    env.fakes["B"].clear_faults()


def add_event(env, scale, at, status=None):
    payload = {"scale": scale, "read": "check", "may_be_frozen": True, "at": at}
    if status:
        payload["status"] = status
    env.catalog.append_log(uuid.uuid4().hex, "read_no_reply", payload, at)


def page_change(env, changes):
    """What the page does: leave out any scale that may be frozen, and check again under the lock."""
    preview = env.svc.preview_changes(changes, requested_by="web", avoid_frozen=True)
    assert preview.ok, preview.errors
    return env.svc.apply_change(preview.preview_id, acknowledge_warnings=True, requested_by="web", avoid_frozen=True)


# ---------------------------------------------------------------- L1


@pytest.mark.parametrize("changes", [EDIT, [{"action": "delete", "target_plu": "1003"}]], ids=["edit", "delete"])
def test_get_change_on_a_change_that_left_a_scale_out_says_it_may_be_frozen(make_env, changes):
    env = three(make_env)
    b_silent_on_a_compare(env)
    record = page_change(env, changes)
    assert {r.scale: r.status for r in record.scale_results}["B"] == "not_available"
    data = Session(env).data("scale_get_change", change_id=record.change_id)
    assert data["scales_may_be_frozen"] == ["B"] and data["still_to_do"] == []
    assert server_module._FROZEN_NEXT_STEP in data["next_step"] and "B MAY BE FROZEN." in data["next_step"]
    assert "use scales to name that scale" not in data["next_step"]
    assert "B: MAY BE FROZEN." in data["summary"]


def test_the_new_fixed_next_steps_are_safe():
    for text in (server_module._PLU_NOT_FINISHED_FROZEN_NEXT_STEP, server_module._DELETE_NOT_FINISHED_FROZEN_NEXT_STEP):
        assert "Do not make a preview for that scale now" in text and "--" not in text and chr(0x2014) not in text


# ---------------------------------------------------------------- L2


def test_a_no_reply_the_log_cannot_take_is_kept_and_written_later(make_env):
    env = three(make_env)
    real = env.catalog.note_read_no_reply

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    env.catalog.note_read_no_reply = locked
    b_silent_on_a_compare(env)
    assert env.svc.scales_may_be_frozen() == ["B"] and env.catalog.read_no_reply_events() == []
    assert env.svc.preview_changes(EDIT, avoid_frozen=True).left_out == ("B",)
    env.catalog.note_read_no_reply = real
    assert env.svc.scales_may_be_frozen() == ["B"] and len(env.catalog.read_no_reply_events()) == 1
    other = ScaleConnectService(env.config, env.catalog, sleep=lambda s: None, clock=ticking_clock())
    assert other.scales_may_be_frozen() == ["B"], "another process sees it once it is in the log"
    env.svc.compare_scales(["B"])
    assert env.svc.scales_may_be_frozen() == [] and other.scales_may_be_frozen() == []


def test_a_held_note_is_dropped_when_that_scale_answers_first(make_env):
    env = three(make_env)
    real = env.catalog.note_read_no_reply
    env.catalog.note_read_no_reply = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    b_silent_on_a_compare(env)
    assert env.svc.compare_scales(["B"])[0].status == "compared"
    env.catalog.note_read_no_reply = real
    assert env.svc.scales_may_be_frozen() == [] and env.catalog.read_no_reply_events() == []


# ---------------------------------------------------------------- L3


@pytest.mark.parametrize("at", ["not a time", "2027-03-01T08:00:00+00:00", "2026-11-02T09:00:00"], ids=["damaged", "future", "no_offset"])
@pytest.mark.parametrize("clear", ["compare", "reload"])
def test_an_event_with_a_time_that_cannot_be_compared_is_cleared_by_a_read_that_answers(make_env, at, clear):
    env = three(make_env)
    add_event(env, "B", at)
    assert env.svc.scales_may_be_frozen() == ["B"]
    if clear == "compare":
        assert env.svc.compare_scales(["B"])[0].status == "compared"
    else:
        assert env.svc.reload_scale("B", accept_differences=True, requested_by="test").ok
    assert env.svc.scales_may_be_frozen() == []


def test_a_read_saved_with_a_future_time_does_not_end_a_later_no_reply(make_env):
    env = three(make_env)
    env.svc.compare_scales(["B"])
    with env.catalog._tx() as db:
        db.execute("UPDATE scale_read SET at = '2027-03-01T08:00:00+00:00' WHERE scale = 'B'")
        db.execute("UPDATE meta SET value = '2027-03-01T08:00:00+00:00' WHERE key = 'read_answered:B'")
    b_silent_on_a_compare(env)
    assert env.svc.scales_may_be_frozen() == ["B"]


def test_with_no_place_in_the_log_kept_the_clock_decides_and_a_damaged_time_waits_for_a_read(make_env):
    """Rows from before the place in the log was kept."""
    env = three(make_env)
    env.svc.compare_scales(["B"])
    with env.catalog._tx() as db:
        db.execute("DELETE FROM meta WHERE key = 'read_answered_log:B'")
    add_event(env, "B", "2026-11-02T09:00:00+00:00")
    assert env.svc.scales_may_be_frozen() == [], "older than the last read by a time that can be trusted"
    add_event(env, "B", "not a time")
    assert env.svc.scales_may_be_frozen() == ["B"]
    env.svc.compare_scales(["B"])
    assert env.svc.scales_may_be_frozen() == []


def test_a_no_reply_logged_after_a_read_in_the_same_second_still_counts(make_env):
    from datetime import datetime, timezone

    fixed = datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)
    env = make_env(THREE, clock=lambda: fixed, reply_timeout_seconds=0.3, connect_attempts=1)
    env.svc.compare_scales(["B"])
    b_silent_on_a_compare(env)
    assert env.svc.scales_may_be_frozen() == ["B"]
    env.svc.compare_scales(["B"])
    assert env.svc.scales_may_be_frozen() == [], "a read after it in the log ends it, even in the same second"


# ---------------------------------------------------------------- L4


def test_the_list_tells_a_failed_connect_from_a_scale_that_gave_no_reply(make_env):
    env = three(make_env)
    env.fakes["B"].stop_listening()
    assert env.svc.read_scale_items(plus=["1001"], scales=["B"])[0].status == "unreachable"
    env.fakes["B"].start_listening()
    data = Session(env).data("scale_list_scales")
    assert data["scales_may_be_frozen"] == ["B"] and data["frozen_how"] == {"B": "unreachable"}
    assert "B: could not connect, and no read of it has answered since, so it MAY BE FROZEN." in data["summary"]
    assert data["next_step"].startswith("B MAY BE FROZEN.")


def test_the_list_says_connected_then_no_reply_for_a_stall(make_env):
    other = three(make_env)
    b_silent_on_a_compare(other)
    data = Session(other).data("scale_list_scales")
    assert data["frozen_how"] == {"B": "no_reply"}
    assert "B: connected, then gave no reply, and no read of it has answered since, so it MAY BE FROZEN." in data["summary"]


def test_an_old_event_with_no_status_keeps_the_old_words(make_env):
    env = three(make_env)
    add_event(env, "B", "2026-11-02T10:00:00+00:00")
    data = Session(env).data("scale_list_scales")
    assert data["frozen_how"] == {"B": "unknown"}
    assert "B: gave no reply, and no read of it has answered since, so it MAY BE FROZEN." in data["summary"]


def test_a_logged_change_that_could_not_connect_says_so_on_its_row(make_env):
    env = three(make_env)
    preview = env.svc.preview_changes(EDIT, read_scales=False)
    env.fakes["B"].stop_listening()
    env.svc.apply_change(preview.preview_id, acknowledge_warnings=True)
    env.fakes["B"].start_listening()
    rows = [u for u in env.svc.list_unfinished() if u.scale == "B"]
    assert rows and rows[0].may_be_frozen and "could not be reached" in rows[0].message
    assert env.svc.frozen_how() == {"B": "unreachable"}


# ---------------------------------------------------------------- L5


@pytest.mark.parametrize(
    "changes",
    [[{"action": "add", "plu": "1777", "description": "Made Up Spread", "price": "5.00", "sold_by": "piece"}], EDIT],
    ids=["add", "edit"],
)
def test_a_flagged_scale_left_out_with_scales_stays_on_the_unfinished_list(make_env, changes):
    env = three(make_env)
    env.svc.compare_scales()
    b_silent_on_a_compare(env)
    session = Session(env)
    before = lp(env, "B")
    preview = session.data("scale_preview_changes", changes=changes, scales=["A", "C"], read_scales=True)
    assert preview["left_out"] == ["B"] and "scale_left_out" in [w["code"] for w in preview["warnings"]]
    record = session.data("scale_apply_changes", preview_id=preview["preview_id"], acknowledge_warnings=True)
    assert record["scale_statuses"] == {"A": "verified", "B": "not_available", "C": "verified"}
    assert record["scales_may_be_frozen"] == ["B"] and lp(env, "B") == before
    rows = [u for u in env.svc.list_unfinished() if u.scale == "B"]
    assert [(u.kind, u.status, u.may_be_frozen) for u in rows] == [("missed_change", "not_sent", True)]
    env.svc.compare_scales(["B"])
    rows = [u for u in env.svc.list_unfinished() if u.scale == "B"]
    assert [(u.kind, u.status, u.may_be_frozen) for u in rows] == [("missed_change", "not_sent", False)]


def test_scales_that_leave_out_a_scale_with_writes_off_add_nothing(make_env):
    env = make_env({"A": True, "B": False, "C": True}, clock=ticking_clock())
    add_event(env, "B", "2026-11-02T10:00:00+00:00", "read_failed")
    assert env.svc.preview_changes(EDIT, scales=["A", "C"]).left_out == ()


def test_the_command_line_frozen_lists_flagged_scales_and_contacts_none(tmp_path, make_fake, capsys):
    """For Bart away from the store, over SSH to bartcc."""
    from scaleconnect.catalog import Catalog
    from scaleconnect.config import load_config

    from test_delete_and_reload import run_cli, write_config

    fake = make_fake()
    path = write_config(tmp_path, fake)
    code, out = run_cli(capsys, "--config", path, "frozen")
    assert code == 0 and out == {"scales_may_be_frozen": [], "how": {}}
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.note_read_no_reply("A", "check", "2026-11-02T10:00:00+00:00", "unreachable")
    code, out = run_cli(capsys, "--config", path, "frozen")
    assert code == 0 and out == {"scales_may_be_frozen": ["A"], "how": {"A": "unreachable"}}
    assert fake.nothing_received


def test_scales_that_leave_out_a_scale_that_is_not_flagged_add_nothing(make_env):
    env = three(make_env)
    assert env.svc.preview_changes(EDIT, scales=["A", "C"]).left_out == ()
