# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round three: unfinished work, repeats, the frozen flag, table refusals and the words for them.

Fake scales on localhost only. Every item is made up. There is no JavaScript runner here, so the
page script is exercised by hand in a headless browser, and these tests cover everything the page
depends on at the server: the data it is handed, what it may send, and what the send refuses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scaleconnect.models import Drift, Item, ScaleResult, placeholder_record
from scaleconnect.web import words

STATIC = Path(__file__).resolve().parents[2] / "src" / "scaleconnect" / "web" / "static"
SCRIPT = (STATIC / "app.js").read_text(encoding="utf-8")
DOCS = Path(__file__).resolve().parents[2]
STAMP = "2026-01-01 00:00:00"
MOVE = [{"action": "edit", "target_plu": "1001", "plu": "1999"}]
DELETE = [{"action": "delete", "target_plu": "1002"}]


def row(item: Item):
    return tuple(item.to_fields())


def seed_of(count: int, price: str = "10.00") -> list[Item]:
    return [Item(i, f"{1000 + i}", f"{1000 + i}", f"Test {i}", price, timestamp=STAMP) for i in range(1, count + 1)]


def acks(preview: dict) -> bool:
    return any(w["requires_ack"] for w in preview["warnings"])


def run(web, changes, *, read=True, ack=None, drift=()):
    """Preview, send and wait, the way the page does. ack None means send what the preview asks for."""
    p = web.preview(changes, read=read).json()
    assert p["ok"], p["errors"]
    r = web.send(p["preview_id"], warnings=acks(p) if ack is None else ack, drift=drift)
    assert r.status_code == 200, r.text
    return p, web.wait_finished(p["preview_id"])


def partial_move(web3, price=None):
    """A PLU change that lands on A and C, while B says Error. Then B is healthy again, and still on the old PLU."""
    web3.login()
    web3.env.fakes["B"].error_on_ids(1)
    change = dict(MOVE[0], **({"price": price} if price else {}))
    _, done = run(web3, [change], ack=True)
    assert [r["status"] for r in done["rows"]] == ["verified", "rejected", "verified"]
    web3.env.fakes["B"].clear_faults()
    return done


def partial_delete(web3):
    web3.login()
    web3.env.fakes["B"].error_on_ids(2)
    _, done = run(web3, DELETE, ack=True)
    assert [r["status"] for r in done["rows"]] == ["verified", "rejected", "verified"]
    web3.env.fakes["B"].clear_faults()
    return done


# ---------------------------------------------------------------- L6: words never come from a message


def test_an_unfinished_change_is_known_by_the_field_and_never_by_the_words():
    said = {"kind": "different_product", "scale": "B", "message": "Repeat that edit to finish it.", "held": None,
            "unfinished_plu_change": False}
    assert words.is_unfinished_move(said) is False
    assert words.drift_words(said) is None
    flagged = dict(said, message="whatever the scale holds", unfinished_plu_change=True)
    assert words.is_unfinished_move(flagged) is True
    assert words.drift_words(flagged)["blocked"].startswith("This scale is still on the old PLU")
    # A drift object works the same way as a dict
    obj = Drift("B", "different_product", 1, "1001", {}, None, "Repeat that edit", None, False)
    assert words.is_unfinished_move(obj) is False
    assert words.is_unfinished_move(Drift("B", "different_product", 1, "1001", {}, None, "x", None, True)) is True
    # A flag that is not exactly true does not count
    assert words.is_unfinished_move({"unfinished_plu_change": "yes"}) is False


def test_a_scale_record_holding_those_words_does_not_flip_the_page(web3):
    web3.login()
    web3.env.fakes["B"].preload(row(Item(1, "1777", "1777", "Repeat that edit", "10.00", timestamp=STAMP)))
    p = web3.preview([{"action": "edit", "target_plu": "1001", "price": "10.50"}], read=True).json()
    drift = [d for d in p["drift"] if d["scale"] == "B"]
    assert len(drift) == 1 and drift[0]["different_product"] is True
    assert drift[0]["unfinished_move"] is False and drift[0]["plain"] is None
    web3.send(p["preview_id"], warnings=acks(p))
    done = web3.wait_finished(p["preview_id"])
    b = next(r for r in done["rows"] if r["scale"] == "B")
    assert b["status"] == "drift_refused"
    assert "different product" in b["text"] and "Call Bart" in b["text"] and "Repeat" not in b["text"]


def test_the_result_row_for_a_scale_record_with_those_words_says_call_bart():
    held = Item(1, "1777", "1777", "Repeat that edit", "10.00", timestamp=STAMP)
    drift = Drift("B", "different_product", 1, "1001", {}, held, "B holds Repeat that edit at id 1", None, False)
    row_ = words.scale_row(ScaleResult("B", "drift_refused", "", drift=(drift,)))
    assert "different product" in row_["text"] and "Repeat" not in row_["text"]


# ---------------------------------------------------------------- M3: never offer to settle a scale behind a PLU change


def test_an_item_page_offers_the_repeat_and_not_the_settle_for_a_scale_behind(web3):
    web3.login()
    partial_move(web3)
    item = web3.client.get("/api/item/1999").json()
    rows = {r["scale"]: r for r in item["scales"]}
    assert rows["B"]["flag"] == "unfinished" and rows["B"]["can_fix"] is False
    assert rows["B"]["repeat"] == {"action": "edit", "target_plu": "1999", "plu": "1999"}
    assert "old PLU" in rows["B"]["text"] and "1001" in rows["B"]["text"] and "Do not settle" in rows["B"]["text"]
    assert rows["A"]["flag"] != "unfinished" and rows["A"]["repeat"] is None
    # A read that found B on the old record still does not offer to settle it
    web3.post("/api/check", {"plu": "1999"})
    again = {r["scale"]: r for r in web3.client.get("/api/item/1999").json()["scales"]}
    assert again["B"]["flag"] == "unfinished" and again["B"]["can_fix"] is False


def test_the_unfinished_flag_goes_away_once_the_repeat_finishes(web3):
    web3.login()
    partial_move(web3)
    _, done = run(web3, [{"action": "edit", "target_plu": "1999", "plu": "1999"}])
    assert [r["status"] for r in done["rows"]] == ["verified"] * 3
    rows = {r["scale"]: r for r in web3.client.get("/api/item/1999").json()["scales"]}
    assert all(r["flag"] != "unfinished" and r["repeat"] is None for r in rows.values())
    assert web3.client.get("/api/unfinished").json()["items"] == []


@pytest.mark.parametrize("take,code", [("scale", "plu_change_not_finished"), ("catalog", "plu_edit_held")])
def test_settling_a_scale_behind_a_plu_change_is_refused_in_plain_words_with_a_repeat(web3, take, code):
    web3.login()
    partial_move(web3)
    sent_before = len(web3.env.writes("B"))
    r = web3.post("/api/settle/preview", {"scale": "B", "take": take, "plu": "1999"}).json()
    assert r["ok"] is False and r["errors"][0]["code"] == code
    message = r["errors"][0]["message"]
    assert "did not finish on this scale" in message and "Repeat the PLU change" in message
    assert "Change this item" not in message and not re.search(r"\bid \d", message)
    assert r["drift"][0]["unfinished_move"] is True and r["drift"][0]["can_ack"] is False
    assert r["repeat"] == {"change": {"action": "edit", "target_plu": "1999", "plu": "1999"}, "plu": "1999", "old": ["1001"]}
    assert len(web3.env.writes("B")) == sent_before, "a refused settle writes nothing"
    assert web3.client.get("/api/item/1999").json()["scales"][1]["flag"] == "unfinished"


def test_the_other_settle_refusals_keep_their_own_words(web):
    web.login()
    web.env.fakes["A"].preload(row(Item(1, "1777", "1777", "Test Cheddar", "10.00", timestamp=STAMP)))
    r = web.post("/api/settle/preview", {"scale": "A", "take": "catalog", "plu": "1001"}).json()
    assert r["errors"][0]["code"] == "plu_edit_held" and r["repeat"] is None
    assert "Settling cannot change a PLU" in r["errors"][0]["message"]


def test_plain_words_for_the_not_finished_refusals():
    for code in ("plu_change_not_finished", "plu_edit_held"):
        text = words.plain_error({"code": code, "message": "x", "index": 0, "field": "plu"}, True)["message"]
        assert "Repeat the PLU change" in text and "\u2014" not in text and "--" not in text
    plain = words.plain_error({"code": "plu_edit_held", "message": "x", "index": 0, "field": "plu"}, False)["message"]
    assert "Settling cannot change a PLU" in plain


# ---------------------------------------------------------------- M1: the frozen flag, never words


def test_a_flag_on_a_preview_error_and_a_settle_read_shows_the_reboot_line(make_web):
    web = make_web({"A": True}, fake_options=dict(freeze_lp_at_id=1), reply_timeout_seconds=0.3, connect_attempts=1)
    web.login()
    r = web.post("/api/settle/preview", {"scale": "A", "take": "scale", "plu": "1001"}).json()
    assert r["ok"] is False and r["errors"][0]["code"] == "settle_read_failed"
    assert r["errors"][0]["may_be_frozen"] is True
    assert "may_be_frozen" in SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    web.env.fakes["A"].reboot()


def test_an_error_without_the_flag_does_not_claim_a_freeze(web):
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "4444", "price": "1.00"}]).json()
    assert p["ok"] is False and p["errors"][0]["may_be_frozen"] is False


# ---------------------------------------------------------------- M4: a repeat sends only the target and the PLU


def test_a_repeat_from_an_old_result_never_puts_an_old_price_back(web3):
    web3.login()
    old = partial_move(web3, price="11.00")
    assert old["changes"][0]["fields"]["price"]["after"] == "11.00"
    # Later the price becomes 14.00. B is still on the old PLU, so it refuses that, and A and C take it.
    p = web3.preview([{"action": "edit", "target_plu": "1999", "price": "14.00"}], read=True).json()
    web3.send(p["preview_id"], warnings=True)
    web3.wait_finished(p["preview_id"])
    assert [web3.env.fakes[n].item(1)[4] for n in "AC"] == ["14.00", "14.00"]
    # The page builds the repeat from the old result. It carries the old PLU and the new PLU, nothing else.
    change = {"action": "edit", "target_plu": "1001", "plu": "1999"}
    pv = web3.preview([change], read=True).json()
    assert pv["ok"] and set(pv["differences"][0]["fields"]) <= {"plu", "code"}
    assert "price" not in pv["differences"][0]["fields"]
    assert any(w["code"] == "plu_change" and w["requires_ack"] for w in pv["warnings"])
    web3.send(pv["preview_id"], warnings=True)
    done = web3.wait_finished(pv["preview_id"])
    assert [r["status"] for r in done["rows"]] == ["verified"] * 3
    prices = {n: web3.env.fakes[n].item(1)[4] for n in "ABC"}
    assert "11.00" not in prices.values(), prices
    assert prices["A"] == prices["C"] == "14.00" and prices["B"] == "10.00"
    assert web3.env.catalog.get_by_plu("1999").price == "14.00"


def test_the_script_builds_a_repeat_with_no_price_or_name():
    result = SCRIPT.split("function pluResult")[1].split("function renderResult")[0]
    assert "again.price" not in result and "again.description" not in result and "f.price" not in result
    assert "{ action: 'edit', target_plu: pc.before, plu: pc.after }" in result
    assert "{ action: 'delete', target_plu: gone }" in result and "target_id" not in result
    # The sentence the review shows is true: only the PLU is sent, and a scale already on it is written again
    box = SCRIPT.split("function repeatBox")[1].split("function repeatTick")[0]
    assert "This sends only the PLU. Price and name are not changed." in box
    assert "are left as they are" not in SCRIPT
    assert "gets the same record again, with a new edit time" in box


# ---------------------------------------------------------------- M6: unfinished work on the home screen


def test_the_home_screen_lists_an_unfinished_plu_change_in_plain_words(web3):
    partial_move(web3)
    r = web3.client.get("/api/unfinished")
    assert r.status_code == 200, "reading needs no PIN, as for search"
    items = r.json()["items"]
    assert len(items) == 1
    card = items[0]
    assert card["kind"] == "plu_change" and card["plu"] == "1999" and card["scales"] == ["B"]
    assert card["old_plus"] == ["1001"]
    assert card["repeat"] == {"action": "edit", "target_plu": "1999", "plu": "1999"}
    assert "has not been confirmed on B" in card["text"]
    assert "assume PLU 1001 still works on B" in card["more"]
    assert card["button"] == "Repeat the PLU change to 1999"


def test_the_home_screen_lists_an_unfinished_delete_and_a_repeat_finishes_it(web3):
    web3.login()
    partial_delete(web3)
    # The page no longer finds the item, but the work is listed
    assert web3.client.get("/api/item/1002").status_code == 404
    card = web3.client.get("/api/unfinished").json()["items"][0]
    assert card["kind"] == "delete" and card["plu"] == "1002" and card["scales"] == ["B"]
    assert card["repeat"] == {"action": "delete", "target_plu": "1002"}, "a delete is repeated by the PLU it was about"
    assert "assume PLU 1002 still works on B" in card["more"] and card["button"] == "Repeat the delete of PLU 1002"
    # The page sends that repeat. It still needs a review, and the tick.
    p = web3.preview([card["repeat"]], read=True).json()
    assert p["ok"] and p["differences"][0]["action"] == "delete"
    assert (p["differences"][0]["before"]["plu"], p["differences"][0]["before"]["description"]) == ("1002", "Test Brie")
    assert any(w["code"] == "delete_placeholder" and w["requires_ack"] for w in p["warnings"])
    refused = web3.send(p["preview_id"], warnings=False)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "acknowledgement_required"
    writes_before = {n: len(web3.env.writes(n)) for n in "ABC"}
    sent = web3.send(p["preview_id"], warnings=True)
    assert sent.status_code == 200
    done = web3.wait_finished(p["preview_id"])
    assert [r["status"] for r in done["rows"]] == ["verified"] * 3
    assert web3.env.fakes["B"].item(2)[3] == "UNUSED"
    assert web3.client.get("/api/unfinished").json()["items"] == []
    # What the review says a repeat of a delete does: it sends the same delete again
    assert all(len(web3.env.writes(n)) >= writes_before[n] for n in "ABC")
    assert len(web3.env.writes("B")) > writes_before["B"]


def test_an_unfinished_delete_on_a_result_says_the_old_plu_may_still_work(web3):
    done = partial_delete(web3)
    assert done["changes"][0]["action"] == "delete" and done["changes"][0]["item_id"] == 2
    assert done["success"] is False
    b = next(r for r in done["rows"] if r["scale"] == "B")
    assert b["status"] == "rejected"
    script = SCRIPT.split("function pluResult")[1].split("function renderResult")[0]
    assert "has not confirmed it. Until it does, assume PLU" in script
    assert "' still works on that scale. Look at the scale.'" in script
    assert "Repeat the delete" in script


def test_a_repeat_of_a_delete_names_the_plu_and_the_page_never_sends_an_id(web3):
    web3.login()
    for bad in ({"action": "delete", "target_id": 2}, {"action": "delete", "target_id": "2"},
                {"action": "edit", "target_id": 1, "price": "1.00"},
                {"action": "delete", "target_plu": "1002", "price": "1.00"}):
        p = web3.preview([bad]).json()
        assert p["ok"] is False, bad
    ok = web3.preview([{"action": "delete", "target_plu": "1002"}]).json()
    assert ok["ok"] and ok["differences"][0]["before"]["plu"] == "1002"
    assert web3.env.silent("A", "B", "C")


def test_an_old_repeat_of_a_delete_cannot_hit_the_item_that_took_the_id(web3):
    """N1. A delete is partly done, then finished, then an add takes the id. The old result's repeat must refuse."""
    web3.login()
    old = partial_delete(web3)
    old_repeat = {"action": "delete", "target_plu": old["changes"][0]["before"]["plu"]}
    assert old_repeat == {"action": "delete", "target_plu": "1002"}
    card = web3.client.get("/api/unfinished").json()["items"][0]
    run(web3, [card["repeat"]])
    assert web3.client.get("/api/unfinished").json()["items"] == []
    _, added = run(web3, [{"action": "add", "plu": "1888", "description": "Brand New Product", "price": "5.00"}])
    assert [r["status"] for r in added["rows"]] == ["verified"] * 3
    assert web3.env.catalog.get_by_plu("1888").id == 2, "the new product took the id"
    before = {n: web3.env.fakes[n].item(2) for n in "ABC"}
    p = web3.preview([old_repeat], read=True).json()
    assert p["ok"] is False and p["errors"][0]["code"] == "item_not_found"
    assert web3.client.get("/api/item/1888").json()["item"]["description"] == "Brand New Product"
    assert {n: web3.env.fakes[n].item(2) for n in "ABC"} == before, "nothing was sent to any scale"
    # Even a stale tab that still holds the id based repeat cannot send it from this page
    assert web3.preview([{"action": "delete", "target_id": 2}]).json()["ok"] is False


def test_the_review_refuses_a_repeat_that_shows_another_item():
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "wrongItem" in review and "This is not the item this repeat was about." in review
    assert "(d.before || {}).plu" in review and "(d.after || {}).plu" in review
    assert "if (rep && !wrongItem) { holder.appendChild(repeatBox(rep)); }" in review
    assert "target_id" not in SCRIPT

def test_nothing_unfinished_says_nothing(web):
    assert web.client.get("/api/unfinished").json() == {"items": []}


def test_two_scales_behind_the_same_item_make_one_card(web3):
    web3.login()
    web3.env.fakes["B"].error_on_ids(2)
    web3.env.fakes["C"].error_on_ids(2)
    _, done = run(web3, DELETE, ack=True)
    assert [r["status"] for r in done["rows"]] == ["verified", "rejected", "rejected"]
    cards = web3.client.get("/api/unfinished").json()["items"]
    assert len(cards) == 1 and cards[0]["scales"] == ["B", "C"]
    assert "on B and C" in cards[0]["text"]


def test_a_repeat_of_a_plu_change_needs_the_tick_at_the_server(web3):
    web3.login()
    partial_move(web3)
    p = web3.preview([{"action": "edit", "target_plu": "1999", "plu": "1999"}], read=True).json()
    assert p["ok"]
    warn = [w for w in p["warnings"] if w["code"] == "plu_change"]
    assert warn and warn[0]["requires_ack"]
    r = web3.send(p["preview_id"], warnings=False)
    assert r.status_code == 400 and r.json()["error"]["code"] == "acknowledgement_required"
    assert web3.env.fakes["B"].item(1)[2] == "1001"


def test_the_script_lists_unfinished_work_and_asks_for_the_tick_on_every_repeat():
    assert "/api/unfinished" in SCRIPT and "unfinishedBoxes" in SCRIPT
    assert "function startRepeat" in SCRIPT and "repeat: meta" in SCRIPT
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "rep.kind === 'plu_change' && !repTicked && !ownLine" in review, "a repeat has a tick even if the library gave none"
    assert "tick('repeat', 'r:repeat'" in review
    item = SCRIPT.split("function renderItem")[1].split("function renderEdit")[0]
    assert "s.flag === 'unfinished' && s.repeat" in item
    assert item.index("s.flag === 'unfinished'") < item.index("else if (s.can_fix)")
    assert "Repeat the PLU change" in item


# ---------------------------------------------------------------- N2: the words say what sending really does


def lost_reply_move(make_web):
    """A PLU change whose reply was lost. The scale took it, nobody knows, and the catalog still says 1001."""
    web = make_web({"A": True}, reply_timeout_seconds=0.4)
    web.env.fakes["A"].stall("GP", times=1, apply=True)
    web.login()
    _, done = run(web, MOVE, ack=True)
    assert done["rows"][0]["status"] == "unknown"
    assert web.env.fakes["A"].item(1)[2] == "1999" and web.env.catalog.get_by_plu("1001") is not None
    return web


def test_after_a_lost_reply_the_card_names_the_plu_the_repeat_sends_and_the_one_that_stops(make_web):
    web = lost_reply_move(make_web)
    card = web.client.get("/api/unfinished").json()["items"][0]
    assert card["repeat"] == {"action": "edit", "target_plu": "1001", "plu": "1999"}
    assert card["target"] == "1999" and card["stopping"] == ["1001"]
    assert card["button"] == "Repeat the PLU change to 1999"
    assert card["text"] == "The PLU change from 1001 to 1999 may have landed on A, and nobody has confirmed it."
    assert "A may be on 1999 or on 1001" in card["more"] and "both may work" in card["more"]
    assert "1001 still works" not in card["more"] and "change to 1001" not in card["text"]
    row = web.client.get("/api/item/1001").json()["scales"][0]
    assert row["flag"] == "unfinished" and row["can_fix"] is False
    assert row["repeat"] == card["repeat"] and row["repeat_meta"] == {"plu": "1999", "old": ["1001"]}
    assert "may have landed" in row["text"] and "1999" in row["text"] and "still on 1001" in row["text"]


def test_after_a_lost_reply_the_repeat_review_and_the_send_agree(make_web):
    web = lost_reply_move(make_web)
    card = web.client.get("/api/unfinished").json()["items"][0]
    p = web.preview([card["repeat"]], read=True).json()
    assert p["ok"]
    d = p["differences"][0]
    assert d["fields"]["plu"] == {"before": "1001", "after": "1999"} and d["after"]["plu"] == "1999"
    assert any(w["code"] == "plu_change" and w["requires_ack"] for w in p["warnings"])
    assert web.send(p["preview_id"], warnings=False).status_code == 400
    assert web.send(p["preview_id"], warnings=True).status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert [r["status"] for r in done["rows"]] == ["verified"]
    assert web.env.fakes["A"].item(1)[1:3] == ("1999", "1999")
    assert web.client.get("/api/unfinished").json()["items"] == []


def test_a_settle_refused_after_a_lost_reply_names_the_plu_a_repeat_sends(make_web):
    web = lost_reply_move(make_web)
    r = web.post("/api/settle/preview", {"scale": "A", "take": "scale", "plu": "1001"}).json()
    assert r["ok"] is False and r["errors"][0]["code"] == "plu_change_not_finished"
    assert r["repeat"] == {"change": {"action": "edit", "target_plu": "1001", "plu": "1999"}, "plu": "1999", "old": ["1001"]}


def test_plu_roles_say_which_plu_every_scale_gets_and_which_stops():
    assert words.plu_roles("1999", ("1001",), {"action": "edit", "target_plu": "1999", "plu": "1999"}) == ("1999", ["1001"])
    assert words.plu_roles("1001", ("1999",), {"action": "edit", "target_plu": "1001", "plu": "1999"}) == ("1999", ["1001"])
    assert words.plu_roles("1001", ("1999",), {"action": "edit", "target_plu": "1001"}) == ("1001", ["1999"])


def test_the_script_takes_the_plu_that_stops_from_the_server_and_never_guesses_it():
    assert "meta.old" in SCRIPT and "s.repeat_meta" in SCRIPT and "old: c.stopping" in SCRIPT
    assert "plu: c.target" in SCRIPT and "plu: c.plu" not in SCRIPT


# ---------------------------------------------------------------- the line under The scales, and a scale that did not answer


def test_a_warning_that_names_a_scale_is_marked_so_the_review_does_not_say_nothing_is_different(make_web):
    web, add = fill_needed(make_web)
    web.login()
    p = web.preview(add, read=True).json()
    gap = next(w for w in p["warnings"] if w["code"] == "gap_filled")
    assert gap["scales_named"] == ["B"]
    other = [w for w in p["warnings"] if w["code"] != "gap_filled"]
    assert all("B" not in w["scales_named"] or "B" in w["message"] for w in other)
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "Read the note above." in review and "scales_named" in review


def test_a_scale_that_did_not_answer_blocks_the_send_and_says_so_on_its_line(make_web):
    web = make_web({"A": True}, fake_options=dict(freeze_lp_at_id=1), reply_timeout_seconds=0.3, connect_attempts=1)
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1001", "price": "10.50"}], read=True).json()
    sick = next(w for w in p["warnings"] if w["code"] == "scale_read_failed")
    assert sick["may_be_frozen"] is True and sick["scales_named"] == ["A"]
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "frozenSeen" in review and "Did not answer. Do not send. Tell the Scale Manager." in review
    assert "A scale did not answer, or could not connect, and sending would try it again." in review
    assert "blocked = true;" in review.split("if (frozenSeen) {")[1].split("}")[0]
    web.env.fakes["A"].reboot()

# ---------------------------------------------------------------- gaps, short reads, and a delete that may have landed


def fill_needed(make_web):
    """A holds an orphan at id 8. B ends at id 3, and the catalog lists nothing at 4 to 7, so an add at 8 must fill there on B."""
    web = make_web({"A": True, "B": True}, seed=seed_of(3))
    web.env.fakes["A"].preload(*[row(placeholder_record(i, STAMP)) for i in range(4, 8)],
                               row(Item(8, "1777", "1777", "Old Test", "1.00", timestamp=STAMP)))
    assert web.env.svc.mark_orphans_from_read("A").marked == (8,)
    add = [{"action": "add", "plu": "1888", "description": "New", "price": "2.00", "replace_orphan_id": 8}]
    return web, add


def test_a_gap_fill_is_a_read_this_warning_with_staff_words_and_needs_the_tick(make_web):
    web, add = fill_needed(make_web)
    web.login()
    p = web.preview(add, read=True).json()
    assert p["ok"]
    gap = [w for w in p["warnings"] if w["code"] == "gap_filled"]
    assert len(gap) == 1 and gap[0]["requires_ack"] is True
    assert gap[0]["plain"] == ("B has no item at id 4 to 7. To leave no gap, empty slots named UNUSED "
                               "will be put there in the same send.")
    assert "placeholder" not in gap[0]["plain"]
    refused = web.send(p["preview_id"], warnings=False)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "acknowledgement_required"
    assert web.env.writes("B") == []
    sent = web.send(p["preview_id"], warnings=True)
    assert sent.status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert [r["status"] for r in done["rows"]] == ["verified", "verified"]
    assert [i for i in web.env.fakes["B"].items if i >= 4] == [4, 5, 6, 7, 8]


def test_a_fill_needs_a_preview_with_the_read_on_and_the_row_says_so(make_web):
    web, add = fill_needed(make_web)
    web.login()
    p = web.preview(add, read=False).json()
    assert p["ok"] and not any(w["code"] == "gap_filled" for w in p["warnings"])
    web.send(p["preview_id"], warnings=acks(p))
    done = web.wait_finished(p["preview_id"])
    b = next(r for r in done["rows"] if r["scale"] == "B")
    assert b["status"] == "drift_refused"
    assert "no review showed that" in b["text"] and "Review it again" in b["text"]
    assert "Call Bart" not in b["text"]
    assert web.env.writes("B") == []


def test_the_script_blocks_a_send_when_the_read_check_did_not_run():
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "if (checkError) {" in review and "blocked = true;" in review.split("if (checkError) {")[1].split("}")[0]
    assert "wn.code === 'gap_filled'" in review and "Empty slots will be added so there is no gap" in review
    start = review.split("function start()")[1]
    assert "draft.settle" in start and "read: true" in start


def test_a_read_that_ends_early_is_a_blocked_drift_in_staff_words(make_web):
    web = make_web({"A": True}, seed=seed_of(12))
    web.env.fakes["A"].end_read_early(1)
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1012", "price": "11.00"}], read=True).json()
    assert p["ok"]
    d = p["drift"][0]
    assert d["kind"] == "read_ended_early" and d["can_ack"] is False and d["ack_token"] is None
    assert d["plain"]["plain"] == "A gave back fewer items than our list has."
    assert d["plain"]["blocked"] == "This scale differs from our list, and a person must look at it."
    assert "Tell the Scale Manager" in d["plain"]["more"] and "reload" in d["plain"]["more"]
    web.send(p["preview_id"], warnings=acks(p))
    done = web.wait_finished(p["preview_id"])
    r = done["rows"][0]
    assert r["status"] == "drift_refused" and "gave back fewer items" in r["text"] and "A person must look" in r["text"]
    assert "A person must look at the scale" in r["now"]
    assert web.env.writes("A") == []


def test_a_fill_over_listed_items_is_a_blocked_drift_in_staff_words(make_web):
    # B never took id 4, the catalog knows that, so its read ending at 3 is fine. An add at 5 must not overwrite 4.
    web = make_web({"A": True, "B": False}, seed=seed_of(3))
    web.login()
    run(web, [{"action": "add", "plu": "1004", "description": "Four", "price": "1.00"}], read=False)
    web.env.set_write_enabled("B", True)
    from webenv import Web
    web2 = Web(web.env)
    web2.login()
    p = web2.preview([{"action": "add", "plu": "1005", "description": "Five", "price": "1.00"}], read=True).json()
    assert p["ok"]
    bad = [d for d in p["drift"] if d["kind"] == "fill_blocked"]
    assert len(bad) == 1 and bad[0]["can_ack"] is False
    assert bad[0]["plain"]["plain"] == "B has gaps where our list has items."
    assert "Tell the Scale Manager" in bad[0]["plain"]["more"]
    web2.send(p["preview_id"], warnings=acks(p))
    done = web2.wait_finished(p["preview_id"])
    b = next(r for r in done["rows"] if r["scale"] == "B")
    assert b["status"] == "drift_refused" and "gaps where our list has items" in b["text"]


def test_a_delete_that_may_have_landed_can_be_ticked_and_says_what_it_means(make_web):
    web = make_web({"A": True}, seed=seed_of(6), reply_timeout_seconds=0.4)
    web.env.fakes["A"].stall("GP", times=1, apply=True)
    web.login()
    p = web.preview(DELETE, read=True).json()
    web.send(p["preview_id"], warnings=True)
    done = web.wait_finished(p["preview_id"])
    assert done["rows"][0]["status"] == "unknown"
    edit = [{"action": "edit", "target_plu": "1002", "price": "11.00"}]
    p2 = web.preview(edit, read=True).json()
    assert p2["ok"]
    d = p2["drift"][0]
    assert d["kind"] == "delete_may_have_landed" and d["can_ack"] is True and d["ack_token"]
    assert d["plain"]["plain"] == "A may have already deleted this item. An earlier delete has no confirmed result."
    assert d["plain"]["tick"] == "Yes. Bring the item back on A."
    assert "repeat the delete instead" in d["plain"]["note"]
    # Without the tick it is refused. With it, the item comes back.
    web.send(p2["preview_id"], warnings=acks(p2))
    refused = web.wait_finished(p2["preview_id"])
    assert refused["rows"][0]["status"] == "drift_refused"
    p3 = web.preview(edit, read=True).json()
    web.send(p3["preview_id"], warnings=acks(p3), drift=[p3["drift"][0]["ack_token"]])
    back = web.wait_finished(p3["preview_id"])
    assert back["rows"][0]["status"] == "verified" and web.env.fakes["A"].item(2)[4] == "11.00"


def test_a_repeat_started_from_the_review_address_redraws_the_review():
    # Found in the browser: from a refused settle the hash is already #/review, so setting it does nothing.
    start = SCRIPT.split("function startRepeat")[1].split("function unfinishedBoxes")[0]
    assert "location.hash === '#/review'" in start and "route();" in start
    assert "charAt(0).toUpperCase()" in SCRIPT.split("function repeatTick")[1].split("function diffBlock")[0]


def test_script_shows_the_tick_and_note_for_a_drift_a_person_may_acknowledge():
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert "plain && plain.tick" in review and "plain.note" in review


# ---------------------------------------------------------------- the words


@pytest.mark.parametrize("code", ["read_ended_early", "fill_blocked", "fill_not_previewed", "reload_differences"])
def test_table_and_reload_errors_have_plain_short_words(code):
    text = words.plain_error({"code": code, "message": "library words " * 5, "index": 0, "field": "scale"})["message"]
    assert "library words" not in text
    assert "\u2014" not in text and "\u2013" not in text and "--" not in text
    assert len(text) < 260 and text.endswith(".")
    assert "person" in text or "review" in text.lower() or "Review" in text or "Go back" in text


def test_every_table_row_word_names_the_scale_and_says_nothing_was_sent():
    for kind, entry in words.TABLE_DRIFT_WORDS.items():
        assert "Nothing was sent" in entry["row"], kind
        for value in entry.values():
            assert "\u2014" not in value and "--" not in value, kind
        assert "{scale}" in entry["plain"], kind


def test_gap_words_fall_back_when_the_message_is_not_the_usual_one():
    text = words.gap_text("something else entirely")
    assert text.startswith("A scale has gaps.") and "UNUSED" in text
    assert words.gap_text("B holds nothing at id 4 to 7, 9. To leave no gap").startswith("B has no item at id 4 to 7, 9.")


def test_the_unfinished_cards_say_the_old_plu_is_not_gone_until_confirmed():
    class U:
        def __init__(self, scale, kind, item_id, plu, old, repeat):
            self.scale, self.kind, self.item_id, self.plu, self.old_plus, self.repeat = scale, kind, item_id, plu, old, repeat

    cards = words.unfinished_cards([
        U("B", "plu_change", 1, "1999", ("1001", "evil <b>"), {"action": "edit", "target_plu": "1999", "plu": "1999"}),
        U("C", "plu_change", 1, "1999", ("1001",), {"action": "edit", "target_plu": "1999", "plu": "1999"}),
    ])
    assert len(cards) == 1 and cards[0]["scales"] == ["B", "C"]
    assert "assume PLU 1001 still works on B and C" in cards[0]["more"], "only a 4 digit PLU goes into a sentence"
    assert "evil" not in cards[0]["more"] and "evil" not in cards[0]["text"]


def test_plu_words_in_a_sentence_accept_only_four_digits():
    held = Item(1, "1001", "1001", "x", "1.00", timestamp=STAMP).to_dict()
    held["plu"] = "evil <b>"
    plain = words.drift_words({"kind": "different_product", "unfinished_plu_change": True, "scale": "B", "held": held})
    assert "evil" not in plain["plain"] and "old PLU." in plain["plain"].replace("is still on the old PLU. ", "old PLU. ")


# ---------------------------------------------------------------- docs


def test_install_doc_has_an_upgrade_section_that_restarts_both_together():
    text = (DOCS / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "## Upgrade" in text
    section = text.split("## Upgrade")[1].split("\n## ")[0]
    assert "web page and the MCP server" in section and "together" in section
    assert "old code" in section and "catalog" in section
    assert "stop" in section.lower() and "start" in section.lower()


def test_staff_doc_covers_the_new_screens_and_the_scale_differs_message():
    text = (DOCS / "docs" / "STAFF.md").read_text(encoding="utf-8")
    for needed in ("## Not finished work", "Repeat the PLU change", "Repeat the delete", "differs from the list",
                   "a person must look", "docs/CLI.md", "UNUSED", "reboot"):
        assert needed in text, needed
    assert "Repeat that edit" not in text


@pytest.mark.parametrize("name", ["README.md", "docs/STAFF.md", "docs/INSTALL.md"])
def test_docs_have_no_dashes_and_no_addresses(name):
    text = (DOCS / name).read_text(encoding="utf-8")
    assert "\u2014" not in text and "\u2013" not in text
    assert not re.search(r"(?<![\w-])--(?![\w-])", text.replace("---", ""))
    assert not re.search(r"\b192\.168\.\d+\.\d+\b", text)
