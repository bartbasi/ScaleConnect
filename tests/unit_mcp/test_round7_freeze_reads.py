"""Round 7 recheck, the library and the MCP server: M1, L1, L2 and L6. Fake scales on localhost, made up items."""

from __future__ import annotations

import pytest

import scaleconnect.mcp_server as server_module

from test_mcp_three_part_rule import EDIT, JUNK, Session, frozen_miss, ticking_clock

TWO = {"A": True, "B": True}


def lp(env, scale):
    return env.fakes[scale].kinds().count("LP")


# ---------------------------------------------------------------- M1: every read that gets no reply is kept


@pytest.mark.parametrize("path", ["compare", "read", "preview", "settle", "orphans", "mcp_preview"])
def test_a_read_with_no_reply_from_any_path_names_the_scale(make_env, path):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3)
    svc = env.svc
    env.fakes["B"].stall_read(times=None)
    if path == "compare":
        assert svc.compare_scales(["B"])[0].may_be_frozen
    elif path == "read":
        assert svc.read_scale_items(plus=["1001"], scales=["B"])[0].may_be_frozen
    elif path == "preview":
        assert any(w.may_be_frozen for w in svc.preview_changes(EDIT, read_scales=True).warnings)
    elif path == "settle":
        assert svc.preview_settle_drift(scale="B", take="catalog", plu="1001").errors[0].may_be_frozen
    elif path == "orphans":
        assert svc.mark_orphans_from_read("B").may_be_frozen
    else:
        Session(env).data("scale_preview_changes", changes=EDIT, read_scales=True)
    env.fakes["B"].clear_faults()
    assert svc.scales_may_be_frozen() == ["B"]
    listed = Session(env).data("scale_list_scales")
    assert listed["scales_may_be_frozen"] == ["B"] and "B: connected, then gave no reply, and no read of it has answered since" in listed["summary"]
    assert listed["frozen_how"] == {"B": "no_reply"}
    svc.compare_scales(["B"])
    assert svc.scales_may_be_frozen() == []


def test_a_freeze_from_a_read_marks_every_row_on_that_scale(make_env):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3)
    session = Session(env)
    env.fakes["B"].error_on_ids(1)
    session.apply(session.preview(EDIT, read_scales=False))  # B refuses: an answered miss, not frozen
    env.fakes["B"].clear_faults()
    assert [(u.scale, u.may_be_frozen) for u in env.svc.list_unfinished()] == [("B", False)]
    env.fakes["B"].stall_read(times=None)
    env.svc.compare_scales(["B"])
    env.fakes["B"].clear_faults()
    assert [(u.scale, u.may_be_frozen) for u in env.svc.list_unfinished()] == [("B", True)]


def test_a_failed_connect_on_a_read_is_kept_too(make_env):
    env = make_env(TWO, clock=ticking_clock(), connect_attempts=1, connect_timeout_seconds=0.3)
    env.fakes["B"].stop_listening()
    result = env.svc.compare_scales(["B"])[0]
    env.fakes["B"].start_listening()
    assert result.status == "unreachable" and result.may_be_frozen
    assert env.svc.scales_may_be_frozen() == ["B"]


# ---------------------------------------------------------------- M2 in the library: a scale left out


def test_a_left_out_scale_with_writes_off_is_skipped_not_missed(make_env):
    """Reads on and writes off: it would not have been written anyway, so leaving it out is no miss."""
    import dataclasses

    from scaleconnect.config import Config
    from scaleconnect.service import ScaleConnectService
    from service_env import scale_config

    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3)
    scales = (scale_config("A", env.fakes["A"], True), dataclasses.replace(scale_config("B", env.fakes["B"], False), read_enabled=True))
    svc = ScaleConnectService(Config(scales=scales, settings=env.settings), env.catalog, **env._svc_kw)
    env.fakes["B"].stall_read(times=None)
    svc.read_scale_items(plus=["1001"], scales=["B"])
    env.fakes["B"].clear_faults()
    assert svc.scales_may_be_frozen() == ["B"]
    before = lp(env, "B")
    preview = svc.preview_changes(EDIT, avoid_frozen=True, read_scales=True)
    assert preview.ok and preview.left_out == ("B",) and preview.scales == ("A",)
    record = svc.apply_change(preview.preview_id, acknowledge_warnings=True, avoid_frozen=True)
    assert {r.scale: r.status for r in record.scale_results} == {"A": "verified", "B": "skipped_not_write_enabled"}
    assert [u for u in svc.list_unfinished() if u.scale == "B"] == []
    assert lp(env, "B") == before


def test_a_left_out_scale_with_writes_on_is_logged_as_not_sent_and_listed(make_env):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3)
    env.fakes["B"].stall_read(times=None)
    env.svc.compare_scales(["B"])
    env.fakes["B"].clear_faults()
    preview = env.svc.preview_changes(EDIT, avoid_frozen=True)
    assert [w.code for w in preview.warnings if w.requires_ack] == ["scale_left_out"]
    record = env.svc.apply_change(preview.preview_id, acknowledge_warnings=True, avoid_frozen=True)
    logged = {r.scale: r for r in env.svc.get_change(record.change_id).scale_results}
    assert logged["B"].status == "not_available" and [i.status for i in logged["B"].items] == ["not_sent"]
    assert logged["B"].may_be_frozen is False, "it was not contacted, so this result says nothing new about a freeze"
    assert record.not_touched == () and record.outcome == "partial"
    assert [(u.kind, u.status, u.may_be_frozen) for u in env.svc.list_unfinished()] == [("missed_change", "not_sent", True)]
    listed = Session(env).data("scale_list_scales")
    assert listed["unfinished"][0]["may_be_frozen"] is True and listed["next_step"].startswith(server_module._FROZEN_NEXT_STEP)


# ---------------------------------------------------------------- L1: an empty table that answers clears it


def test_a_compare_of_an_emptied_scale_that_answers_clears_the_flag(make_env):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3)
    frozen_miss(env, Session(env))
    env.fakes["B"].clear_faults()
    env.fakes["B"].set_table([])
    compared = env.svc.compare_scales(["B"])[0]
    assert compared.status == "compared" and compared.items_read == 0
    assert env.svc.scales_may_be_frozen() == []
    assert all(not u.may_be_frozen for u in env.svc.list_unfinished())


def test_an_identity_check_alone_or_a_failed_read_does_not_clear_it(make_env):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3)
    frozen_miss(env, Session(env))
    env.fakes["B"].clear_faults()
    env.fakes["B"].stall_read(times=None)
    env.svc.compare_scales(["B"])
    env.fakes["B"].clear_faults()
    assert env.svc.scales_may_be_frozen() == ["B"]


# ---------------------------------------------------------------- L2: a reload read back that answered badly


def test_a_reload_whose_read_back_answered_badly_and_closed_is_not_frozen(make_env):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3, read_back_window_seconds=1.0)
    env.svc.compare_scales()
    env.fakes["B"].raw_reply(JUNK, kind="LP", times=None)
    result = env.svc.reload_scale("B", accept_differences=True, requested_by="test")
    env.fakes["B"].clear_faults()
    assert result.ok is False and result.may_be_frozen is False
    assert "may be frozen" not in result.message
    assert env.svc.get_change(result.change_id).scale_results[-1].may_be_frozen is False
    assert env.svc.scales_may_be_frozen() == []


def test_a_reload_whose_read_back_got_no_reply_is_still_frozen(make_env):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3, read_back_window_seconds=1.0)
    env.svc.compare_scales()
    env.fakes["B"].stall_read(times=None)
    result = env.svc.reload_scale("B", requested_by="test")
    env.fakes["B"].clear_faults()
    assert result.may_be_frozen is True and env.svc.scales_may_be_frozen() == ["B"]


# ---------------------------------------------------------------- L6: the freeze rule comes first, by name


def test_the_list_puts_a_silent_freeze_first_and_names_it(make_env):
    env = make_env(TWO, clock=ticking_clock(), reply_timeout_seconds=0.3)
    session = Session(env)
    env.fakes["A"].error_on_ids(2)
    session.apply(session.preview([{"action": "delete", "target_plu": "1002"}], read_scales=False), acknowledge_warnings=True)
    env.fakes["A"].clear_faults()
    env.fakes["B"].stall_read(times=None)
    env.svc.compare_scales(["B"])
    env.fakes["B"].clear_faults()
    data = session.data("scale_list_scales")
    assert data["scales_may_be_frozen"] == ["B"] and any(u["scale"] == "A" and not u["may_be_frozen"] for u in data["unfinished"])
    step = data["next_step"]
    assert step.startswith("B MAY BE FROZEN, with no row of its own. " + server_module._FROZEN_NEXT_STEP)
    assert "leave out B" in step
    assert step.index("MAY BE FROZEN") < step.index(server_module._UNFINISHED_NEXT_STEP)


def test_the_tool_texts_stay_within_their_limits():
    assert len(server_module.SERVER_INSTRUCTIONS) <= 4096
