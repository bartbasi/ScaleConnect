"""Round 9 recheck fixes, the library and the MCP server: N1, N2 and N3.

N1: Claude's usual flow (search, preview with no scales, apply) leaves out a scale that may be frozen, with a
warning to acknowledge, and never reads it. So do a preview that names it, an apply that finds it flagged under
the lock, a compare with no scales, a read and a settle preview. Only a compare that names it, or a reload, reads it.
N2: a no reply the log cannot take is held in the state folder, so another process counts it at once.
N3: the preview's summary names each scale it leaves out.

Fake scales on localhost, made up items.
"""

from __future__ import annotations

import sqlite3

import scaleconnect.mcp_server as server_module
from scaleconnect.service import ScaleConnectService

from test_mcp_three_part_rule import EDIT, OLD_CUT, Session, ticking_clock

THREE = {"A": True, "B": True, "C": True}


def three(make_env, **kw):
    kw.setdefault("reply_timeout_seconds", 0.3)
    kw.setdefault("connect_attempts", 1)
    return make_env(THREE, clock=ticking_clock(), **kw)


def b_flagged(env):
    env.fakes["B"].stall_read(times=None)
    assert env.svc.compare_scales(["B"])[0].may_be_frozen
    env.fakes["B"].clear_faults()
    assert env.svc.scales_may_be_frozen() == ["B"]


def sent_to(env, scale):
    return len(env.fakes[scale].messages)


def test_claudes_usual_flow_leaves_a_flagged_scale_out_and_never_reads_it(make_env):
    env = three(make_env)
    b_flagged(env)
    session = Session(env)
    before = sent_to(env, "B")
    session.data("scale_search_items", query="1001")
    preview = session.data("scale_preview_changes", changes=EDIT)
    assert preview["ok"] and preview["scales"] == ["A", "C"] and preview["left_out"] == ["B"]
    assert [w["code"] for w in preview["warnings_requiring_ack"]] == ["scale_left_out"]
    assert "B is left out. It gave no reply earlier, and nothing is read or sent there." in preview["summary"].splitlines()
    assert "ask for a compare that names it" in preview["next_step"] and preview["scales_may_be_frozen"] == []
    refused = session.data("scale_apply_changes", preview_id=preview["preview_id"])
    assert refused["error"]["code"] == "acknowledgement_required"
    record = session.data("scale_apply_changes", preview_id=preview["preview_id"], acknowledge_warnings=True)
    assert record["scale_statuses"] == {"A": "verified", "B": "not_available", "C": "verified"}
    assert sent_to(env, "B") == before, "nothing reached B"
    assert env.svc.scales_may_be_frozen() == ["B"], "no read cleared the flag"


def test_a_preview_that_names_the_flagged_scale_leaves_it_out_too(make_env):
    env = three(make_env)
    b_flagged(env)
    before = sent_to(env, "B")
    preview = Session(env).data("scale_preview_changes", changes=EDIT, scales=["A", "B", "C"])
    assert preview["left_out"] == ["B"] and preview["scales"] == ["A", "C"]
    only_b = Session(env).data("scale_preview_changes", changes=EDIT, scales=["B"])
    assert only_b["ok"] is False and only_b["errors"][0]["code"] == "scale_may_be_frozen"
    assert only_b["scales_may_be_frozen"] == ["B"] and "B is left out." in only_b["summary"]
    assert "do not call this again" in only_b["next_step"] and "Fix every error" not in only_b["next_step"]
    assert sent_to(env, "B") == before and env.svc.scales_may_be_frozen() == ["B"]


def test_the_apply_leaves_out_a_scale_flagged_after_the_preview(make_env):
    env = three(make_env)
    session = Session(env)
    preview = session.data("scale_preview_changes", changes=EDIT, read_scales=False)
    assert preview["left_out"] == [] and preview["scales"] == ["A", "B", "C"]
    env.catalog.note_read_no_reply("B", "check", "2026-11-02T10:30:00+00:00", "unreachable")  # the page, a moment later
    before = sent_to(env, "B")
    record = session.data("scale_apply_changes", preview_id=preview["preview_id"])
    assert record["scale_statuses"] == {"A": "verified", "B": "not_available", "C": "verified"}
    assert record["scales_may_be_frozen"] == ["B"] and sent_to(env, "B") == before
    rows = [(u.kind, u.status, u.may_be_frozen) for u in env.svc.list_unfinished() if u.scale == "B"]
    assert rows == [("missed_change", "not_sent", True)]


def test_the_apply_refuses_when_no_scale_it_would_contact_is_left(make_env):
    env = make_env({"B": True}, clock=ticking_clock())
    session = Session(env)
    preview = session.data("scale_preview_changes", changes=EDIT, read_scales=False)
    env.catalog.note_read_no_reply("B", "check", "2026-11-02T10:30:00+00:00", "read_failed")
    before = sent_to(env, "B")
    refused = session.data("scale_apply_changes", preview_id=preview["preview_id"])
    assert refused["error"]["code"] == "scale_may_be_frozen" and sent_to(env, "B") == before
    assert refused["next_step"].startswith("Do not apply it again.")


def test_a_compare_with_no_scales_leaves_the_flagged_scale_out_and_naming_it_clears_it(make_env):
    env = three(make_env)
    b_flagged(env)
    session = Session(env)
    before = sent_to(env, "B")
    data = session.data("scale_compare_scales")
    by_scale = {c["scale"]: c for c in data["scales"]}
    assert by_scale["B"]["status"] == "not_contacted" and by_scale["B"]["may_be_frozen"] is True
    assert by_scale["A"]["status"] == "compared" and data["all_match"] is False
    assert "B: left out, so it was not read." in data["summary"] and data["next_step"].startswith(server_module._FROZEN_NEXT_STEP)
    assert sent_to(env, "B") == before and env.svc.scales_may_be_frozen() == ["B"]
    named = session.data("scale_compare_scales", scales=["B"])
    assert named["scales"][0]["status"] == "compared" and env.svc.scales_may_be_frozen() == []


def test_reads_and_a_settle_preview_never_read_a_flagged_scale(make_env):
    env = three(make_env)
    b_flagged(env)
    session = Session(env)
    before = sent_to(env, "B")
    for scales in (None, ["B"]):
        args = {"plus": ["1001"]} | ({"scales": scales} if scales else {})
        read = {r["scale"]: r for r in session.data("scale_read_items", **args)["scales"]}
        assert read["B"]["was_read"] is False and read["B"]["may_be_frozen"] is True
    settle = session.data("scale_preview_settle_drift", scale="B", take="catalog", plu="1001")
    assert settle["ok"] is False and settle["errors"][0]["code"] == "scale_may_be_frozen"
    orphan = session.data("scale_preview_mark_orphan", scale="B", id=200)
    assert orphan["error"]["code"] == "scale_not_read"
    assert sent_to(env, "B") == before and env.svc.scales_may_be_frozen() == ["B"]


def test_the_instructions_say_a_flagged_scale_is_left_out_until_a_compare_names_it():
    words = "Previews, applies and reads leave that scale out until the person has looked at it and asks for a compare that names it."
    assert words in server_module.SERVER_INSTRUCTIONS[:OLD_CUT]
    assert len(server_module.SERVER_INSTRUCTIONS) <= 4096
    assert "--" not in server_module._FROZEN_NEXT_STEP and "scale_compare_scales" not in server_module._FROZEN_NEXT_STEP


def test_a_held_note_reaches_every_process_at_once_and_then_the_log(make_env):
    env = three(make_env)
    real = env.catalog.note_read_no_reply

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    env.catalog.note_read_no_reply = locked
    b_flagged(env)
    other = ScaleConnectService(env.config, env.catalog, sleep=lambda s: None, clock=ticking_clock())
    assert other.scales_may_be_frozen() == ["B"], "another process counts it while the log refuses"
    assert other.frozen_how() == {"B": "no_reply"}
    env.catalog.note_read_no_reply = real
    assert other.scales_may_be_frozen() == ["B"] and len(env.catalog.read_no_reply_events()) == 1
    assert env.svc.scales_may_be_frozen() == ["B"]
    assert len(env.catalog.read_no_reply_events()) == 1, "written once"
    other.compare_scales(["B"])
    assert env.svc.scales_may_be_frozen() == [] and other.scales_may_be_frozen() == []


def test_a_read_that_answers_in_another_process_drops_the_held_note(make_env):
    env = three(make_env)
    real = env.catalog.note_read_no_reply
    env.catalog.note_read_no_reply = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    b_flagged(env)
    other = ScaleConnectService(env.config, env.catalog, sleep=lambda s: None, clock=ticking_clock())
    assert other.compare_scales(["B"])[0].status == "compared"
    env.catalog.note_read_no_reply = real
    assert other.scales_may_be_frozen() == [] and env.catalog.read_no_reply_events() == []
    assert env.svc.scales_may_be_frozen() == [], "the process that held it sees the answer too"


def test_if_the_state_folder_refuses_too_the_note_stays_in_this_process(make_env, monkeypatch):
    env = three(make_env)
    real = env.catalog.note_read_no_reply
    env.catalog.note_read_no_reply = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(env.svc, "_hold_in_file", lambda *a: False)
    b_flagged(env)
    assert set(env.svc._unnoted) == {"B"}
    env.catalog.note_read_no_reply = real
    assert env.svc.scales_may_be_frozen() == ["B"] and len(env.catalog.read_no_reply_events()) == 1
