"""Round 9 recheck N4: a failed connect is not called a scale that did not answer, and the grammar of a list.
Also N2 on the page: a no reply the MCP process could not log still keeps the page off that scale.
Fake scales on localhost, made up items."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scaleconnect.models import ScaleResult, ItemResult
from scaleconnect.service import ScaleConnectService
from scaleconnect.web import words

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "src" / "scaleconnect" / "web" / "static" / "app.js").read_text(encoding="utf-8")
STAFF = (ROOT / "docs" / "STAFF.md").read_text(encoding="utf-8")
EDIT1 = [{"action": "edit", "target_plu": "1001", "price": "11.00"}]


def test_left_out_words_follow_how_each_scale_stopped():
    assert words.left_out_text(["B"]) == "B is left out. It did not answer last time. Tell Bart."
    assert words.left_out_text(["B"], {"B": "unreachable"}) == "B is left out. We could not connect to it last time. Tell Bart."
    assert words.left_out_text(["B", "C"]) == "B and C are left out. They did not answer last time. Tell Bart."
    assert words.left_out_text(["B", "C"], {"C": "unreachable"}) == (
        "B and C are left out. B did not answer last time. We could not connect to C last time. Tell Bart."
    )
    assert words.left_out_review_text("unreachable") == "Left out. We could not connect to it last time. Nothing is read or sent to it. Tell Bart."


def test_refusals_and_rows_say_could_not_connect_for_a_failed_connect():
    refusal = words.frozen_refusal(["B"], {"B": "unreachable"})["message"]
    assert refusal.startswith(words.COULD_NOT_CONNECT) and "did not answer" not in refusal
    assert words.frozen_refusal(["B"])["message"].startswith(words.DID_NOT_ANSWER)
    sent = words.frozen_send_refusal(["B", "C"], {"B": "unreachable", "C": "unreachable"})
    assert "did not answer" not in sent and sent.endswith("B and C will be left out.")
    assert words.read_row("B", "unreachable", True)["reboot"] == words.REBOOT_NO_CONNECT
    assert words.read_row("B", "read_failed", True)["reboot"] == words.REBOOT_LINE
    assert words.scale_row(ScaleResult("B", "unreachable", "x", may_be_frozen=True))["reboot"] == words.REBOOT_NO_CONNECT
    left = ScaleResult("B", "not_available", "x", items=(ItemResult(1, "1001", "edit", "not_sent"),))
    assert words.scale_row(left, "unreachable")["text"] == "B: left out. We could not connect to it last time, so nothing was sent to it. Tell Bart."
    assert words.REBOOT_NO_CONNECT in SCRIPT, "the page script tells the two apart"


def test_the_page_script_uses_the_servers_words_for_a_failed_connect():
    assert "s.reboot || REBOOT" in SCRIPT and "r.reboot || REBOOT" in SCRIPT and "wn.reboot || REBOOT" in SCRIPT
    assert "pv.left_out_rows" in SCRIPT
    assert "It did not answer last time. Nothing is read or sent to it." not in SCRIPT


def test_staff_guide_covers_a_failed_connect_and_says_not_to_retry():
    assert "If the line below says it did not answer, or could not connect, do not try again. Tell Bart." in STAFF
    assert "| Left out. We could not connect to it last time, so nothing was sent to it. |" in STAFF


def test_a_review_after_a_failed_connect_says_could_not_connect(make_web):
    web = make_web({"A": True, "B": True, "C": True}, reply_timeout_seconds=0.3, connect_attempts=1, connect_timeout_seconds=0.3)
    web.login()
    web.env.fakes["B"].stop_listening()
    rows = {r["scale"]: r for r in web.post("/api/check", {"plu": "1001"}).json()["scales"]}
    web.env.fakes["B"].start_listening()
    assert rows["B"]["reboot"] == words.REBOOT_NO_CONNECT
    p = web.preview(EDIT1, read=True).json()
    assert p["left_out_text"] == "B is left out. We could not connect to it last time. Tell Bart."
    assert p["left_out_rows"] == {"B": words.left_out_review_text("unreachable")}
    assert web.send(p["preview_id"], warnings=True).status_code == 200
    done = web.wait_finished(p["preview_id"])
    b = next(r for r in done["rows"] if r["scale"] == "B")
    assert "could not connect" in b["text"] and "did not answer" not in b["text"]
    assert done["left_out_text"] == p["left_out_text"]


def test_a_note_the_mcp_process_could_not_log_keeps_the_page_off_that_scale(make_web):
    web = make_web({"A": True, "B": True, "C": True}, reply_timeout_seconds=0.3, connect_attempts=1)
    web.login()
    env = web.env
    mcp = ScaleConnectService(env.config, env.catalog, sleep=lambda s: None)
    real = env.catalog.note_read_no_reply
    env.catalog.note_read_no_reply = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    env.fakes["B"].stall_read(times=None)
    assert mcp.compare_scales(["B"])[0].may_be_frozen
    env.fakes["B"].clear_faults()
    env.catalog.note_read_no_reply = real
    before = len(env.fakes["B"].messages)
    rows = {r["scale"]: r for r in web.post("/api/check", {"plu": "1001"}).json()["scales"]}
    assert rows["B"]["status"] == "not_contacted" and len(env.fakes["B"].messages) == before
    assert len(env.catalog.read_no_reply_events()) == 1, "the page wrote the held note to the log"
