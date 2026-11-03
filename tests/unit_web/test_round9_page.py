# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round 8 recheck fixes, the page: M1, L2 and L4.

M1: the documented way out works. A flagged scale with reads and writes turned off in the config, after a
restart, is not contacted, so it must not stop a send to the others. This test sends, not only reviews.
L2: a no reply the log cannot take still keeps that scale out of the next tap.
L4: a failed connect is told apart from a scale that connected and gave no reply. Both still count.

Fake scales on localhost, made up items. Each test counts LP messages to the scale in question.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from webenv import Web

THREE = {"A": True, "B": True, "C": True}
EDIT1 = [{"action": "edit", "target_plu": "1001", "price": "11.00"}]
EDIT2 = [{"action": "edit", "target_plu": "1002", "price": "7.00"}]


def ticking_clock():
    now = [datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)]

    def clock():
        now[0] += timedelta(seconds=1)
        return now[0]

    return clock


def three(make_web):
    web = make_web(THREE, clock=ticking_clock(), reply_timeout_seconds=0.3, connect_attempts=1)
    web.login()
    return web


def lp(web, scale):
    return web.env.fakes[scale].kinds().count("LP")


def check_with_b_silent(web, plu="1001"):
    web.env.fakes["B"].stall_read(times=None)
    rows = {r["scale"]: r for r in web.post("/api/check", {"plu": plu}).json()["scales"]}
    web.env.fakes["B"].clear_faults()
    return rows


# ---------------------------------------------------------------- M1


@pytest.mark.parametrize("how", ["check", "logged change"])
def test_turning_a_flagged_scale_off_in_the_config_lets_the_page_send_to_the_others(make_web, how):
    web = three(make_web)
    if how == "check":
        check_with_b_silent(web)
    else:
        p = web.preview(EDIT1).json()
        web.env.fakes["B"].stall_read(times=None)
        web.send(p["preview_id"], warnings=True)
        web.wait_finished(p["preview_id"])
        web.env.fakes["B"].clear_faults()
    assert web.env.svc.scales_may_be_frozen() == ["B"]

    # Bart turns B's reads and writes off in the config, then restarts web and MCP together.
    web.env.set_write_enabled("B", False)
    later = Web(web.env)
    later.login()
    assert [(s.name, s.write_enabled, s.read_enabled) for s in web.env.svc.list_scales()][1] == ("B", False, False)
    assert later.client.get("/api/item/1002").json()["frozen_scales"] == []
    rows = {r["scale"]: r for r in later.post("/api/check", {"plu": "1002"}).json()["scales"]}
    assert "Did not answer" not in rows["B"]["text"]

    before = {s: lp(web, s) for s in THREE}
    pv = later.preview(EDIT2, read=True).json()
    assert pv["ok"] is True and pv["left_out"] == []
    sent = later.send(pv["preview_id"], warnings=True)
    assert sent.status_code == 200, sent.text
    assert later.wait_finished(pv["preview_id"])["state"] == "finished"
    record = web.env.svc.get_change_for_preview(pv["preview_id"])
    assert {r.scale: r.status for r in record.scale_results} == {"A": "verified", "B": "skipped_not_write_enabled", "C": "verified"}
    assert lp(web, "B") == before["B"], "B is never contacted"
    assert lp(web, "A") > before["A"] and lp(web, "C") > before["C"]
    assert web.env.catalog.get_by_plu("1002").price == "7.00"


def test_a_flagged_scale_with_writes_on_still_stops_a_send_made_before_it_stopped(make_web):
    """The guard is narrowed only to scales apply would not contact. A scale with writes on still counts."""
    web = three(make_web)
    pv = web.preview(EDIT2).json()
    web.env.fakes["B"].stall_read(times=None)
    web.env.svc.compare_scales(["B"])
    web.env.fakes["B"].clear_faults()
    before = {s: lp(web, s) for s in THREE}
    r = web.send(pv["preview_id"], warnings=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "scale_may_be_frozen"
    assert {s: lp(web, s) for s in THREE} == before


# ---------------------------------------------------------------- L2


def test_a_no_reply_the_log_cannot_take_still_keeps_that_scale_out(make_web):
    web = three(make_web)
    real = web.env.catalog.note_read_no_reply

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    web.env.catalog.note_read_no_reply = locked
    check_with_b_silent(web)
    assert web.env.svc.scales_may_be_frozen() == ["B"]
    before = lp(web, "B")
    rows = {r["scale"]: r for r in web.post("/api/check", {"plu": "1001"}).json()["scales"]}
    assert rows["B"]["status"] == "not_contacted" and "Did not answer" in rows["B"]["text"]
    assert web.preview(EDIT1, read=True).json()["left_out"] == ["B"]
    assert lp(web, "B") == before
    # Once the log works, the note goes in, so another process sees it too.
    web.env.catalog.note_read_no_reply = real
    assert web.env.svc.scales_may_be_frozen() == ["B"]
    assert len(web.env.catalog.read_no_reply_events()) == 1


# ---------------------------------------------------------------- L4


def test_the_page_tells_a_failed_connect_from_a_scale_that_gave_no_reply(make_web):
    web = three(make_web)
    web.env.fakes["B"].stop_listening()
    web.post("/api/check", {"plu": "1001"})
    web.env.fakes["B"].start_listening()
    item = web.client.get("/api/item/1001").json()
    assert item["frozen_scales"] == ["B"] and item["frozen_how"] == {"B": "unreachable"}
    row = next(r for r in item["scales"] if r["scale"] == "B")
    assert row["flag"] == "frozen" and row["text"].startswith("Could not connect. Tell the Scale Manager.")
    card = next(c for c in web.client.get("/api/unfinished").json()["items"] if c["kind"] == "frozen_scale")
    assert "could not be reached" in card["text"] and "off the WiFi, or it may be frozen" in card["text"]
    before = lp(web, "B")
    rows = {r["scale"]: r for r in web.post("/api/check", {"plu": "1001"}).json()["scales"]}
    assert rows["B"]["text"].startswith("B: Could not connect. Tell the Scale Manager.") and lp(web, "B") == before


def test_the_page_keeps_did_not_answer_for_a_scale_that_connected_and_gave_no_reply(make_web):
    other = three(make_web)
    check_with_b_silent(other)
    item = other.client.get("/api/item/1001").json()
    assert item["frozen_how"] == {"B": "no_reply"}
    assert next(r for r in item["scales"] if r["scale"] == "B")["text"].startswith("Did not answer. Tell the Scale Manager.")
    card = next(c for c in other.client.get("/api/unfinished").json()["items"] if c["kind"] == "frozen_scale")
    assert card["text"].startswith("B did not answer earlier")


def test_the_item_page_script_reads_frozen_how():
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "src" / "scaleconnect" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "frozen_how" in script and "Could not connect" in script
