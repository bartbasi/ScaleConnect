# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""The page's delete flow, PLU change flow, new refusals and the frozen scale notice.

Fake scales on localhost only. Every item is made up. The page script is plain JavaScript and
there is no JavaScript runner here, so what the script shows is checked in two ways: the data
the server hands it (which these tests run end to end), and a few strings the script must hold.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scaleconnect.models import ScaleResult
from scaleconnect.web import words
from scaleconnect.web import __main__ as web_main_module

EM = chr(0x2014)
STATIC = Path(__file__).resolve().parents[2] / "src" / "scaleconnect" / "web" / "static"
SCRIPT = (STATIC / "app.js").read_text(encoding="utf-8")

MOVE = [{"action": "edit", "target_plu": "1001", "plu": "1999"}]
DELETE = [{"action": "delete", "target_plu": "1002"}]


# ---------------------------------------------------------------- switches


def test_the_web_start_up_turns_plu_editing_on_and_passes_it_to_the_page():
    assert web_main_module.PLU_EDIT_ENABLED is True
    source = Path(web_main_module.__file__).read_text(encoding="utf-8")
    assert "plu_edit_enabled=PLU_EDIT_ENABLED" in source
    assert "plu_edit_held" not in source and "lifts the hold" not in source


def test_state_has_no_stale_delete_note_when_delete_works(web):
    state = web.client.get("/api/state").json()
    assert state["capabilities"]["delete"] is True
    assert state["delete_note"] == ""


def test_the_edit_form_has_no_code_field_and_sends_the_plu_only_when_it_changed():
    assert "id: 'code'" not in SCRIPT and "'Code'" not in SCRIPT.split("function renderEdit")[1].split("function renderAdd")[0]
    edit = SCRIPT.split("function renderEdit")[1].split("function renderAdd")[0]
    assert "change.plu = pluInput.value" in edit
    assert "S.pluEdit" in edit


def test_no_stale_text_about_delete_or_plu_changes_being_unavailable():
    stale = ("Use ScaleLink for a PLU change", "cannot be changed here yet", "Deleting items is not available yet",
             "not available yet", "on hold")
    texts = [SCRIPT, (STATIC.parent / "app.py").read_text(encoding="utf-8"), (STATIC.parent / "words.py").read_text(encoding="utf-8")]
    for text in texts:
        for bad in stale:
            assert bad not in text, bad


# ---------------------------------------------------------------- delete


def test_a_delete_preview_names_the_item_and_asks_for_an_acknowledgement(web):
    web.login()
    p = web.preview(DELETE).json()
    assert p["ok"]
    diff = p["differences"][0]
    assert diff["action"] == "delete"
    assert (diff["before"]["plu"], diff["before"]["description"], diff["before"]["price"]) == ("1002", "Test Brie", "20.00")
    assert diff["after"] is None
    warn = [w for w in p["warnings"] if w["code"] == "delete_placeholder"]
    assert warn and warn[0]["requires_ack"] and warn[0]["index"] == diff["index"]
    assert web.env.silent("A"), "a preview sends nothing"


def test_a_delete_cannot_be_sent_without_the_acknowledgement(web):
    web.login()
    p = web.preview(DELETE).json()
    for body in ({"preview_id": p["preview_id"]}, {"preview_id": p["preview_id"], "acknowledge_warnings": False}):
        r = web.post("/api/send", body)
        assert r.status_code == 400 and r.json()["error"]["code"] == "acknowledgement_required"
    # A text or a number does not count as an acknowledgement either
    for loose in ("true", 1, "yes"):
        r = web.post("/api/send", {"preview_id": p["preview_id"], "acknowledge_warnings": loose})
        assert r.status_code in (400, 422)
    assert web.env.writes("A") == []
    assert web.env.catalog.get_by_plu("1002") is not None


def test_an_acknowledged_delete_shows_a_result_for_each_scale_and_the_old_plu_is_gone(web3):
    web3.login()
    p = web3.preview(DELETE, read=True).json()
    r = web3.send(p["preview_id"], warnings=True)
    assert r.status_code == 200
    done = web3.wait_finished(p["preview_id"])
    assert done["state"] == "finished" and done["outcome"] == "success"
    assert [(row["scale"], row["status"]) for row in done["rows"]] == [("A", "verified"), ("B", "verified"), ("C", "verified")]
    assert all(row["text"].endswith("changed and checked") for row in done["rows"])
    assert done["changes"][0]["action"] == "delete" and done["changes"][0]["before"]["plu"] == "1002"
    for fake in web3.env.fakes.values():
        assert fake.item(2)[1:5] == ("9002", "9002", "UNUSED", "0.01")
    # The old PLU no longer finds anything, and no page offers the placeholder
    assert web3.client.get("/api/search", params={"q": "1002"}).json()["items"] == []
    assert web3.client.get("/api/item/1002").status_code == 404
    assert web3.client.get("/api/search", params={"q": "UNUSED"}).json()["items"] == []


def test_a_reserved_number_cannot_be_deleted_or_edited_and_the_words_are_plain(web):
    web.login()
    for change in ({"action": "delete", "target_plu": "9002"}, {"action": "edit", "target_plu": "9002", "price": "1.00"}):
        p = web.preview([change]).json()
        assert not p["ok"] and p["errors"][0]["code"] == "reserved_plu"
        message = p["errors"][0]["message"]
        assert "set aside" in message and "not a real item" in message
        assert "held back" not in message and "placeholder" not in message
    assert web.env.silent("A")


def test_deleting_an_item_that_is_not_there_is_a_plain_404_style_refusal(web):
    web.login()
    p = web.preview([{"action": "delete", "target_plu": "4444"}]).json()
    assert not p["ok"] and p["errors"][0]["code"] == "item_not_found"


def test_the_page_sends_a_delete_with_only_a_target(web):
    web.login()
    p = web.preview([{"action": "delete", "target_plu": "1002", "price": "1.00"}]).json()
    assert not p["ok"] and p["errors"][0]["code"] == "invalid_field"
    assert web.env.silent("A")


def test_the_script_shows_the_item_the_warning_and_needs_a_tick_for_a_delete():
    block = SCRIPT.split("function diffBlock")[1].split("function checkbox")[0]
    delete = block.split("d.action === 'delete'")[1].split("} else {")[0]
    assert "gone.plu" in delete and "gone.description" in delete and "gone.price" in delete
    assert "will stop working on the scales once this runs" in SCRIPT
    assert "The slot will show UNUSED." in delete
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "stopKind(d)" in review and "ackText(d)" in review
    assert "needed.every" in review and "sendBtn.disabled = !ready()" in review
    assert "Do not delete. Go back." in SCRIPT
    # Treated the same, by code, so a library warning for a PLU change gets the same tick
    assert "delete_placeholder" in SCRIPT and "plu_change" in SCRIPT


# ---------------------------------------------------------------- PLU change


def test_a_plu_change_preview_has_the_old_and_new_plu_and_code_follows(web):
    web.login()
    p = web.preview(MOVE).json()
    assert p["ok"]
    fields = p["differences"][0]["fields"]
    assert fields["plu"] == {"before": "1001", "after": "1999"}
    assert fields["code"] == {"before": "1001", "after": "1999"}
    assert web.env.silent("A")


def test_a_plu_change_runs_end_to_end_and_the_old_plu_stops_working(web):
    web.login()
    p = web.preview(MOVE, read=True).json()
    # If the library asks for an acknowledgement, the page sends one. If not, none is needed.
    needs = any(w["requires_ack"] for w in p["warnings"])
    r = web.send(p["preview_id"], warnings=needs)
    assert r.status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert done["outcome"] == "success" and [row["status"] for row in done["rows"]] == ["verified"]
    assert done["changes"][0]["fields"]["plu"] == {"before": "1001", "after": "1999"}
    held = web.env.fakes["A"].item(1)
    assert held[1:3] == ("1999", "1999")
    assert web.client.get("/api/item/1001").status_code == 404
    assert web.client.get("/api/item/1999").json()["item"]["code"] == "1999"
    assert web.client.get("/api/search", params={"q": "1001"}).json()["items"] == []


def test_the_script_shows_old_plu_new_plu_and_that_the_old_one_stops_working():
    block = SCRIPT.split("function pluChange")[1].split("function checkbox")[0]
    assert "The old PLU ' + pc.before + ' will stop working on the scales once this runs" in block
    assert "PLU ' + pc.after + ' takes its place" in block
    assert "fieldLine(k, d.fields[k].before, d.fields[k].after)" in block
    # Code follows the PLU, so it is not shown as a second change
    assert "k === 'code' && pc" in block
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "'p:' + d.index" in review, "the page asks for its own tick when the library did not"


def test_plu_change_refusals_are_in_plain_words(web):
    web.login()
    cases = [
        ({"plu": "1002"}, "duplicate_plu", "is already used by another item"),
        ({"plu": "9001"}, "reserved_plu", "set aside for empty slots"),
        ({"plu": "199"}, "invalid_plu", None),
    ]
    for extra, code, phrase in cases:
        p = web.preview([{"action": "edit", "target_plu": "1001", **extra}]).json()
        assert not p["ok"] and code in {e["code"] for e in p["errors"]}, extra
        message = next(e["message"] for e in p["errors"] if e["code"] == code)
        if phrase:
            assert phrase in message, message
        assert " id " not in f" {message} ".lower().replace("pid", ""), message
    dup = web.preview([{"action": "edit", "target_plu": "1001", "plu": "1002"}]).json()
    assert "PLU 1002" in dup["errors"][0]["message"]
    assert web.env.silent("A")


def test_the_page_cannot_send_a_code(web):
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1001", "plu": "1999", "code": "1998"}]).json()
    assert not p["ok"] and p["errors"][0]["code"] == "invalid_field" and p["errors"][0]["field"] == "code"
    assert web.env.silent("A")


def test_a_code_that_does_not_match_gets_a_plain_refusal():
    err = {"code": "code_plu_mismatch", "message": "Code 1998 must equal the PLU 1999.", "index": 0, "field": "code"}
    plain = words.plain_error(err)
    assert plain["message"] == "The PLU and the code must be the same number. Do not send this. Tell the Scale Manager."
    assert plain["code"] == "code_plu_mismatch" and plain["index"] == 0 and plain["field"] == "code"


def test_a_plu_the_scale_holds_under_another_item_is_in_plain_words():
    err = {"code": "plu_on_other_id", "message": "A already holds PLU 1999 at id 4.", "index": 0, "field": None}
    assert words.plain_error(err)["message"] == "A scale already has that PLU on a different item. Call the Scale Manager."
    still = {"code": "duplicate_plu", "index": 0, "field": "plu",
             "message": "PLU 1001 may still be on id 1 of a scale, because a PLU change there has not finished."}
    text = words.plain_error(still)["message"]
    assert "PLU 1001 may still be in use on a scale" in text and "did not finish" in text and " id " not in text


def test_other_errors_keep_the_librarys_words():
    err = {"code": "invalid_price", "message": "Price must be more than 0.", "index": 0, "field": "price"}
    assert words.plain_error(err) == err


def test_a_scale_that_is_marked_lagging_says_repeat_that_edit(web3):
    web3.login()
    web3.env.fakes["B"].error_on_ids(1)
    p = web3.preview(MOVE).json()
    web3.send(p["preview_id"], warnings=any(w["requires_ack"] for w in p["warnings"]))
    first = web3.wait_finished(p["preview_id"])
    assert first["outcome"] == "partial"
    assert {row["scale"]: row["status"] for row in first["rows"]} == {"A": "verified", "B": "rejected", "C": "verified"}
    # The data the script needs to show "Repeat the PLU change"
    assert first["changes"][0]["fields"]["plu"] == {"before": "1001", "after": "1999"}
    assert first["success"] is False
    web3.env.fakes["B"].clear_faults()

    # A later price edit meets the lagging scale. The preview says so in plain words and blocks it.
    later = web3.preview([{"action": "edit", "target_plu": "1999", "price": "11.00"}], read=True).json()
    assert later["ok"]
    drift = [d for d in later["drift"] if d["scale"] == "B"]
    assert len(drift) == 1
    assert drift[0]["unfinished_move"] is True and drift[0]["can_ack"] is False and drift[0]["ack_token"] is None
    plain = drift[0]["plain"]
    assert "earlier PLU change did not finish" in plain["plain"]
    assert "Repeat the PLU change" in plain["more"]
    assert "Call the Scale Manager" not in plain["more"]

    # Sending it anyway is refused for B, and the result row says to repeat the earlier edit
    web3.send(later["preview_id"], warnings=any(w["requires_ack"] for w in later["warnings"]))
    second = web3.wait_finished(later["preview_id"])
    rows = {row["scale"]: row for row in second["rows"]}
    assert rows["B"]["status"] == "drift_refused"
    assert "Repeat the PLU change" in rows["B"]["text"] and "Call the Scale Manager" not in rows["B"]["text"]
    assert web3.env.fakes["B"].item(1)[2] == "1001"

    # Repeating the same edit finishes it
    again = web3.preview(MOVE, read=True).json()
    assert again["ok"] and not again["drift"]
    web3.send(again["preview_id"], warnings=any(w["requires_ack"] for w in again["warnings"]))
    last = web3.wait_finished(again["preview_id"])
    # Every scale took the PLU change. B still holds the old price from before, and the page says so.
    assert [row["status"] for row in last["rows"]] == ["verified"] * 3
    assert web3.env.fakes["B"].item(1)[1:3] == ("1999", "1999")
    assert any("B still differs" in note for note in last["notes"])


def test_the_script_offers_repeat_that_edit_when_a_plu_change_did_not_finish_everywhere():
    result = SCRIPT.split("function pluResult")[1].split("function renderResult")[0]
    assert "Repeat the PLU change" in result
    assert "r.status === 'verified'" in result
    assert "kind: 'plu_change', plu: pc.after, old: [pc.before]" in result
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "draft.repeat" in review and "repeatBox(rep)" in review and "'r:repeat'" in review
    assert "Finish the PLU change to" in SCRIPT
    assert "target_plu: pc.before, plu: pc.after" in result
    assert "may not have finished on every scale" in result


def test_a_different_product_at_the_id_still_says_call_the_scale_manager(web3):
    web3.login()
    p = web3.preview(MOVE).json()
    web3.env.fakes["B"].preload(tuple(web3.env.catalog.get_by_plu("1001").replace(plu="1777", code="1777").to_fields()))
    web3.send(p["preview_id"], warnings=any(w["requires_ack"] for w in p["warnings"]))
    done = web3.wait_finished(p["preview_id"])
    row = next(r for r in done["rows"] if r["scale"] == "B")
    assert row["status"] == "drift_refused" and "different product" in row["text"] and "Call the Scale Manager" in row["text"]


# ---------------------------------------------------------------- a scale that may be frozen


FROZEN = dict(fake_options=dict(freeze_lp_at_id=1), reply_timeout_seconds=0.3, connect_attempts=1)


def test_a_read_with_no_reply_says_the_scale_may_need_a_reboot_in_the_review(make_web):
    web = make_web({"A": True}, **FROZEN)
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1001", "price": "10.50"}], read=True).json()
    assert p["ok"]
    sick = [w for w in p["warnings"] if w["code"] == "scale_read_failed"]
    assert sick and sick[0]["may_be_frozen"] is True
    assert p["scales_read"] == []
    assert "REBOOT = 'The scale did not answer. It may need a reboot. Tell the Scale Manager.'" in SCRIPT
    web.env.fakes["A"].reboot()


def test_a_read_with_no_reply_says_the_scale_may_need_a_reboot_in_the_result(make_web):
    web = make_web({"A": True}, **FROZEN)
    web.login()
    p, done = web.change_and_wait([{"action": "edit", "target_plu": "1001", "price": "10.50"}])
    row = done["rows"][0]
    assert row["status"] == "read_failed" and row["may_be_frozen"] is True
    assert "Nothing was sent" in row["now"]
    assert web.env.writes("A") == []
    web.env.fakes["A"].reboot()


def test_a_check_now_with_no_reply_says_the_scale_may_need_a_reboot(make_web):
    web = make_web({"A": True}, **FROZEN)
    web.login()
    r = web.post("/api/check", {"plu": "1001"}).json()
    scale = r["scales"][0]
    assert scale["status"] != "read" and scale["may_be_frozen"] is True
    assert "message" not in scale, "no raw scale messages reach the page"
    assert scale["text"].startswith("A: ")
    web.env.fakes["A"].reboot()


def test_a_good_read_and_other_failures_do_not_claim_a_reboot(web):
    web.login()
    good = web.post("/api/check", {"plu": "1001"}).json()["scales"][0]
    assert good["status"] == "read" and good["may_be_frozen"] is False
    other = words.read_row("A", "wrong_scale", False)
    assert other["may_be_frozen"] is False and "not the scale it should be" in other["text"]
    assert words.read_row("A", "read_failed", True)["may_be_frozen"] is True
    # Words in a message mean nothing. Only the library's flag does.
    said = ScaleResult("A", "read_failed", "The scale may have frozen, and may need a reboot.")
    assert words.scale_row(said)["may_be_frozen"] is False
    assert words.scale_row(ScaleResult("A", "read_failed", "x", may_be_frozen=True))["may_be_frozen"] is True
    assert not hasattr(words, "may_need_reboot")


# ---------------------------------------------------------------- words


def test_the_not_available_status_no_longer_says_yet():
    text, _ = words.status_words("not_available")
    assert "yet" not in text and "not available" in text


@pytest.mark.parametrize("code,field", [
    ("reserved_plu", "plu"), ("reserved_plu", "target_plu"), ("code_plu_mismatch", "code"),
    ("duplicate_plu", "plu"), ("plu_on_other_id", None), ("plu_edit_held", "plu"), ("delete_not_available", "action"),
])
def test_new_error_words_have_no_dashes_and_are_short(code, field):
    out = words.plain_error({"code": code, "message": "PLU 1234 may still be on id 4, not finished.", "index": 0, "field": field})
    text = out["message"]
    assert EM not in text and "--" not in text and "–" not in text
    assert len(text) < 200 and text.endswith(".")


def test_new_script_text_has_no_dashes():
    for name in ("app.js", "style.css"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert EM not in text and "–" not in text and "--" not in text, name
    assert not re.search(r"innerHTML|insertAdjacentHTML", SCRIPT)
