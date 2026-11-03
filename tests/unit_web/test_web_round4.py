# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round four: a changed config file, a delete with no confirmed result, and the timing words.

Fake scales on localhost only. Every item is made up. The page script has no runner here, so these
tests cover what the server hands the page, and a few strings the script must hold.
"""

from __future__ import annotations

import re
from pathlib import Path

from scaleconnect.models import ScaleResult, Drift
from scaleconnect.web import app as web_app
from scaleconnect.web import words

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "src" / "scaleconnect" / "web" / "static" / "app.js").read_text(encoding="utf-8")
STAFF = (ROOT / "docs" / "STAFF.md").read_text(encoding="utf-8")
LIBRARY_WORDS = ("The config file has changed since this service started, so nothing was sent. "
                 "Restart the web page and the MCP server together, then try again.")


def changed(web, monkeypatch):
    monkeypatch.setattr(web.env.svc, "config_changed", lambda: LIBRARY_WORDS)


# ---------------------------------------------------------------- config_changed and target_changed


def test_the_notice_words_say_nothing_was_sent_tell_bart_and_restart_both():
    text = words.CONFIG_NOTICE
    assert "Nothing was sent." in text and "Tell Bart." in text
    assert "web page and the MCP server need restarting together" in text
    assert "\u2014" not in text and "--" not in text
    assert words.plain_error({"code": "config_changed", "message": LIBRARY_WORDS})["message"] == text
    t = words.plain_error({"code": "target_changed", "message": "Id 2 now holds PLU 1888, not PLU 1002.", "index": 0})
    assert "nothing was changed" in t["message"] and "1888" not in t["message"] and "id " not in t["message"].lower().replace("did", "")


def test_both_codes_have_an_http_status():
    assert web_app.STATUS_FOR_CODE["config_changed"] == 409 and web_app.STATUS_FOR_CODE["target_changed"] == 409


def test_state_has_no_notice_when_the_config_is_as_loaded(web):
    assert web.client.get("/api/state").json()["config_notice"] is None


def test_state_shows_the_notice_in_staff_words_when_the_config_changed(web, monkeypatch):
    changed(web, monkeypatch)
    notice = web.client.get("/api/state").json()["config_notice"]
    assert notice == words.CONFIG_NOTICE and "MCP server" in notice and "config file" not in notice


def test_a_state_check_that_breaks_does_not_stop_the_page(web, monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(web.env.svc, "config_changed", boom)
    r = web.client.get("/api/state")
    assert r.status_code == 200 and r.json()["config_notice"] is None


def test_a_preview_refused_for_a_changed_config_has_staff_words(web, monkeypatch):
    web.login()
    changed(web, monkeypatch)
    p = web.preview([{"action": "edit", "target_plu": "1001", "price": "10.50"}]).json()
    assert p["ok"] is False and p["errors"][0]["code"] == "config_changed"
    assert p["errors"][0]["message"] == words.CONFIG_NOTICE
    assert web.env.silent("A")


def test_a_send_after_the_config_changed_is_not_sent_and_says_so_plainly(web, monkeypatch):
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1001", "price": "10.50"}], read=False).json()
    assert p["ok"]
    changed(web, monkeypatch)
    r = web.send(p["preview_id"], warnings=False)
    assert r.status_code == 409
    error = r.json()["error"]
    assert error["code"] == "config_changed" and error["sent"] is False
    assert error["message"] == words.CONFIG_NOTICE
    assert web.env.silent("A"), "nothing reached the scale"
    assert web.env.catalog.get_by_plu("1001").price == "10.00"


def test_the_script_shows_the_notice_and_keeps_the_send_off_while_it_shows():
    assert "S.configNotice = d.config_notice || null" in SCRIPT
    assert "if (S.configNotice) { app.appendChild(box('bad', 'Do not send', S.configNotice)); }" in SCRIPT
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "!S.configNotice" in review.split("function ready()")[1].split("}")[0]
    assert "(blocked || S.configNotice) ? 'Cannot send'" in review
    assert "if (e.code === 'config_changed') { S.configNotice = e.message; }" in review


# ---------------------------------------------------------------- N9: a delete with no confirmed result


def lost_delete(make_web):
    web = make_web({"A": True}, reply_timeout_seconds=0.4)
    web.env.fakes["A"].stall("GP", times=1, apply=True)
    web.login()
    p = web.preview([{"action": "delete", "target_plu": "1002"}], read=True).json()
    web.send(p["preview_id"], warnings=True)
    assert web.wait_finished(p["preview_id"])["rows"][0]["status"] == "unknown"
    return web


def test_a_settle_to_the_list_after_a_lost_delete_says_what_to_do_instead(make_web):
    web = lost_delete(make_web)
    r = web.post("/api/settle/preview", {"scale": "A", "take": "catalog", "plu": "1002"}).json()
    assert r["ok"] and r["drift"][0]["kind"] == "missing"
    assert "do not settle" in r["drift"][0]["plain_note"] and "Delete this item" in r["drift"][0]["plain_note"]
    # Settle it anyway. The library refuses, because the scale may already have deleted it, and it needs its own token.
    web.send(r["preview_id"], warnings=False, drift=[d["ack_token"] for d in r["drift"]])
    done = web.wait_finished(r["preview_id"])
    row = done["rows"][0]
    assert row["status"] == "drift_refused"
    assert "an earlier delete of this item has no confirmed result" in row["text"]
    assert "open the item and tap Delete this item" in row["text"] and "tell Bart" in row["text"]
    assert "Call Bart" not in row["text"] and "something different from our list" not in row["text"]
    assert "Do not settle this item to our list" in row["now"]
    # The item is still listed, the scale still holds the empty slot, and nothing was brought back
    assert web.env.catalog.get_by_plu("1002") is not None
    assert web.env.fakes["A"].item(2)[3] == "UNUSED"


def test_repeating_the_delete_from_the_item_page_works_after_a_lost_reply(make_web):
    web = lost_delete(make_web)
    p = web.preview([{"action": "delete", "target_plu": "1002"}], read=True).json()
    assert p["ok"] and any(w["requires_ack"] for w in p["warnings"])
    web.send(p["preview_id"], warnings=True)
    done = web.wait_finished(p["preview_id"])
    assert done["rows"][0]["status"] == "verified" and web.env.catalog.get_by_plu("1002") is None


def test_the_row_words_for_that_refusal_have_no_dashes():
    drift = Drift("A", "delete_may_have_landed", 2, "1002", {}, None, "library words", "A:2:x", False)
    row = words.scale_row(ScaleResult("A", "drift_refused", "", drift=(drift,)))
    for text in (row["text"], row["now"]):
        assert "\u2014" not in text and "--" not in text


def test_the_script_shows_the_settle_note_under_the_drift():
    assert "dr.plain_note" in SCRIPT.split("function renderReview")[1].split("function renderResult")[0]


# ---------------------------------------------------------------- timing words


def test_the_page_says_how_long_the_check_and_the_send_take():
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "Checking the scales takes about a minute and a half." in review
    assert "Do not refresh the page, and do not tap anything twice." in review
    result = SCRIPT.split("function renderResult")[1]
    assert "takes about two and a half minutes" in result
    assert "Do not refresh the page and do not send again." in result


def test_the_staff_guide_gives_the_timing_and_says_not_to_refresh_or_send_again():
    assert "about 95 seconds" in STAFF and "about 2.5 minutes" in STAFF
    section = STAFF.split("## How long it takes")[1].split("\n## ")[0]
    assert "Do not refresh the page" in section and "Do not send the same change again" in section
    assert chr(0x2014) not in STAFF and "--" not in re.sub(r"^\|[-| ]+\|$", "", STAFF, flags=re.M)


def test_the_staff_guide_covers_a_page_that_says_the_settings_changed():
    assert "The settings have changed" in STAFF and "restarting together" in STAFF
    assert "has no confirmed result" in STAFF and "Delete this item" in STAFF
