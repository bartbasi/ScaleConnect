# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""The page: what it says about Square and labels, and how a missed change reads. Fake scales, made up items."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from scaleconnect.models import Item, UnfinishedWork
from scaleconnect.web import words

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "src" / "scaleconnect" / "web" / "static" / "app.js").read_text(encoding="utf-8")
STAFF = (ROOT / "docs" / "STAFF.md").read_text(encoding="utf-8")
EDIT = [{"action": "edit", "target_plu": "1001", "price": "11.00"}]
NOTE = "The scales are done. This page does not change Square or the shelf labels. Tell the manager so they can be updated."
JUNK = b"XX99\t\n0\r"  # not a read block. The scale answered, so this is no sign of a freeze


def ticking_clock():
    now = [datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)]

    def clock():
        now[0] += timedelta(seconds=1)
        return now[0]

    return clock


def miss_on_b(web):
    """A price edit that reached A, while B's read failed."""
    web.login()
    p = web.preview(EDIT, read=False).json()
    web.env.fakes["B"].raw_reply(JUNK, kind="LP", times=None)
    assert web.send(p["preview_id"]).status_code == 200
    done = web.wait_finished(p["preview_id"])
    web.env.fakes["B"].clear_faults()
    return done


def test_the_words_for_the_note():
    assert words.AFTER_SUCCESS_NOTE == NOTE
    assert "—" not in NOTE and "--" not in NOTE
    assert words.after_success_note(["edit"], True) == NOTE
    assert words.after_success_note(["add", "delete"], True) == NOTE
    assert words.after_success_note(["edit"], False) is None
    assert words.after_success_note(["settle_push"], True) is None
    assert words.after_success_note(["settle_accept"], True) is None


def test_a_successful_change_says_the_page_did_not_change_square(make_web):
    web = make_web({"A": True, "B": True})
    web.login()
    _, done = web.change_and_wait(EDIT)
    assert done["success"] is True and done["after_note"] == NOTE


def test_a_partial_change_does_not_say_the_scales_are_done_but_does_say_square(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    done = miss_on_b(web)
    assert done["success"] is False and done["after_note"] == words.AFTER_PARTIAL_NOTE
    assert "The scales are done" not in done["after_note"] and "Square" in done["after_note"]


def test_a_change_where_the_catalog_did_not_move_gets_no_note(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    web.login()
    for fake in web.env.fakes.values():
        fake.raw_reply(JUNK, kind="LP", times=None)
    p = web.preview(EDIT, read=False).json()
    web.send(p["preview_id"])
    done = web.wait_finished(p["preview_id"])
    assert done["outcome"] == "failed" and done["after_note"] is None


def settle_on_a(web, take="catalog", **held):
    web.login()
    fields = dict(description="Test Cheddar", price="10.00")
    fields.update(held)
    web.env.fakes["A"].preload(tuple(Item(1, "1001", "1001", fields["description"], fields["price"], field6=" ", timestamp="2026-01-01 00:00:00").to_fields()))
    p = web.post("/api/settle/preview", {"scale": "A", "take": take, "plu": "1001"}).json()
    assert p["ok"], p["errors"]
    tokens = [d["ack_token"] for d in p["drift"] if d.get("ack_token")]
    assert web.send(p["preview_id"], warnings=True, drift=tokens).status_code == 200
    return web.wait_finished(p["preview_id"])


def test_a_settle_that_changes_no_price_gets_no_note(make_web):
    web = make_web({"A": True}, clock=ticking_clock())
    done = settle_on_a(web, description="Other Words")
    assert done["success"] is True and done["after_note"] is None


def test_a_settle_that_changes_a_price_gets_the_note(make_web):
    web = make_web({"A": True}, clock=ticking_clock())
    done = settle_on_a(web, price="12.00")
    assert done["success"] is True and done["after_note"] == words.AFTER_SETTLE_SALE_NOTE


def test_a_settle_that_takes_the_scale_gets_the_note(make_web):
    web = make_web({"A": True}, clock=ticking_clock())
    done = settle_on_a(web, take="scale", price="12.00")
    assert done["after_note"] == words.AFTER_TAKE_SCALE_NOTE


def test_a_settle_that_finishes_a_miss_gets_the_note_even_as_a_partial_result(make_web):
    """The real case: a partial change, finished by the fix button, must not lose Square."""
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b(web)
    p = web.post("/api/settle/preview", {"scale": "B", "take": "catalog", "plu": "1001"}).json()
    tokens = [d["ack_token"] for d in p["drift"] if d.get("ack_token")]
    web.send(p["preview_id"], warnings=True, drift=tokens)
    done = web.wait_finished(p["preview_id"])
    assert done["outcome"] == "partial" and done["after_note"] and "Square" in done["after_note"]


def test_the_words_of_every_note_keep_the_writing_rule():
    for note in (words.AFTER_PARTIAL_NOTE, words.AFTER_SETTLE_SALE_NOTE, words.AFTER_SETTLE_MISS_NOTE, words.AFTER_TAKE_SCALE_NOTE,
                 words.DID_NOT_ANSWER, words.CHECK_THEN_CALL, words.LIST_FAILED_TEXT, words.LIST_FAILED_MORE):
        assert "\u2014" not in note and "\u2013" not in note and "--" not in note


def test_the_page_puts_the_note_on_the_result():
    paint = SCRIPT.split("function paint(d)")[1].split("function poll()")[0]
    assert "if (d.after_note) { holder.appendChild(box('warn', 'Next', d.after_note)); }" in paint


def test_the_staff_guide_says_it():
    assert "does not change Square or the shelf labels" in STAFF
    assert "Tell the manager" in STAFF


def test_a_missed_change_reads_right_on_the_home_screen(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b(web)
    cards = web.client.get("/api/unfinished").json()["items"]
    assert len(cards) == 1
    card = cards[0]
    assert (card["kind"], card["plu"], card["scales"]) == ("missed_change", "1001", ["B"])
    assert card["text"] == "The newest change to PLU 1001 has not been confirmed on B."
    assert card["button"] == "Fix this on B"
    assert "Test Cheddar" not in card["text"] and "Test Cheddar" not in card["more"]


def test_two_scales_get_a_card_each_because_a_settle_is_one_scale(make_web):
    web = make_web({"A": True, "B": True, "C": True}, clock=ticking_clock(), connect_attempts=1)
    web.login()
    p = web.preview(EDIT, read=False).json()
    for name in "BC":
        web.env.fakes[name].drop_read(block=0, times=None)
    web.send(p["preview_id"])
    web.wait_finished(p["preview_id"])
    cards = web.client.get("/api/unfinished").json()["items"]
    assert [(c["kind"], c["scales"]) for c in cards] == [("missed_change", ["B"]), ("missed_change", ["C"])]


def test_the_home_screen_sends_a_missed_change_to_the_settle_page_not_to_a_repeat():
    boxes = SCRIPT.split("function unfinishedBoxes")[1].split("/* home: search */")[0]
    missed, rest = boxes.split("c.kind === 'missed_change'")
    assert "#/settle/" in rest.split("startRepeat")[0], "the settle page asks which side is right"
    assert "startRepeat" not in rest.split("return h('div', null,")[1]


def test_the_item_page_offers_the_fix_and_does_not_say_matches(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b(web)
    rows = {r["scale"]: r for r in web.client.get("/api/item/1001").json()["scales"]}
    assert rows["B"]["can_fix"] is True
    assert rows["B"]["flag"] != "same"
    assert rows["A"]["flag"] == "same" or rows["A"]["flag"] == "none"
    assert rows["A"]["can_fix"] is False


def test_a_settle_from_the_card_clears_it(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b(web)
    card = web.client.get("/api/unfinished").json()["items"][0]
    p = web.post("/api/settle/preview", {"scale": card["scales"][0], "take": "catalog", "plu": card["plu"]}).json()
    assert p["ok"], p["errors"]
    tokens = [d["ack_token"] for d in p["drift"] if d.get("ack_token")]
    assert web.send(p["preview_id"], warnings=True, drift=tokens).status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert [r["status"] for r in done["rows"] if r["scale"] == "B"] == ["verified"]
    assert web.client.get("/api/unfinished").json()["items"] == []


def test_a_settle_that_ends_a_miss_on_a_description_only_gets_the_waiting_note():
    class Item_:
        item_id, action, status = 1, "settle_push", "verified"

    class Result:
        items = (Item_,)

    class Change:
        item_id, action, fields = 1, "settle_push", {"description": object()}

    class Record:
        changes, scale_results = (Change,), (Result,)

    assert words.after_note(Record, False, ended_a_miss=True) == words.AFTER_SETTLE_MISS_NOTE
    assert words.after_note(Record, False, ended_a_miss=False) is None


# ---------------------------------------------------------------- the cards, the item page and the settle screen


def lost_reply_on_every_scale(web, landed="B"):
    web.login()
    p = web.preview(EDIT, read=False).json()
    for name, fake in web.env.fakes.items():
        fake.drop_before_reply(kind="GP", times=None, apply=name == landed)
    web.send(p["preview_id"])
    web.wait_finished(p["preview_id"])
    for fake in web.env.fakes.values():
        fake.clear_faults()


def frozen_miss_on_b(web):
    web.login()
    p = web.preview(EDIT, read=False).json()
    web.env.fakes["B"].stall_read(times=None)
    web.send(p["preview_id"])
    return web.wait_finished(p["preview_id"])


def item_row(web, scale, plu="1001"):
    return {r["scale"]: r for r in web.client.get(f"/api/item/{plu}").json()["scales"]}[scale]


def test_a_change_our_list_did_not_take_gets_its_own_card_with_no_fix_button(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    lost_reply_on_every_scale(web)
    cards = {c["scales"][0]: c for c in web.client.get("/api/unfinished").json()["items"]}
    card = cards["B"]
    assert card["button"] is None and card["no_fix"] is True and len(cards) == 2
    assert "Check this scale, then call Bart." in card["more"] and "our list did not take it" in card["text"]
    assert "would put the old value back" in card["more"]
    row = item_row(web, "B")
    assert row["can_fix"] is False and row["missed"]["kind"] == "not_taken" and "Check this scale, then call Bart." in row["text"]


def test_a_scale_that_did_not_answer_gets_no_fix_button(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock(), reply_timeout_seconds=0.3, connect_attempts=1)
    frozen_miss_on_b(web)
    card = web.client.get("/api/unfinished").json()["items"][0]
    row = item_row(web, "B")
    web.env.fakes["B"].clear_faults()
    assert card["button"] is None and "Did not answer. Tell Bart." in card["more"]
    assert row["can_fix"] is False and row["missed"]["kind"] == "frozen" and "Did not answer. Tell Bart." in row["text"]


def test_a_scale_that_may_differ_says_may_differ_when_nobody_has_read_it_since(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    web.login()
    web.env.svc.compare_scales(["B"])  # an old read, from before the change
    miss_on_b(web)
    row = item_row(web, "B")
    assert row["flag"] == "unsure" and "may differ from our list" in row["text"] and "holds something different" not in row["text"]
    assert row["missed"]["read_since"] is False and row["can_fix"] is True
    assert "could not read the scale first" in row["missed"]["status_text"]


def test_a_scale_read_since_that_shows_a_difference_says_it_differed(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b(web)
    web.env.svc.compare_scales(["B"])
    row = item_row(web, "B")
    assert row["missed"]["read_since"] is True and row["flag"] == "different"
    assert "When B was last read, it differed from our list in:" in row["missed"]["text"]


def test_the_item_page_does_not_say_matches_from_a_read_older_than_the_miss(make_web):
    """A reload that could not be read back moves nothing in the catalog, so the read before it still matches. It says nothing about what happened since."""
    web = make_web({"A": True}, clock=ticking_clock(), read_back_window_seconds=1.0)
    web.env.svc.compare_scales(["A"])
    assert item_row(web, "A")["flag"] == "same", "the read matches, and the page says so"
    web.env.fakes["A"].raw_reply(JUNK, kind="LP", times=None)
    web.env.svc.reload_scale("A", requested_by="test")
    web.env.fakes["A"].clear_faults()
    row = item_row(web, "A")
    assert row["last_read"] is not None and row["differs"] == [], "the read itself does match"
    # Round 7 L2: a bad answer on a scale that then closed is a plain failure, not a freeze. The row still does not say it matches.
    assert row["flag"] == "unsure" and "Matches" not in row["text"]
    assert "Did not answer. Tell Bart." not in row["text"]
    # Round 6 N3: a reload whose read back got no reply carries the freeze flag, so the row says that, and offers nothing.
    web.env.fakes["A"].stall_read(times=None)
    web.env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    web.env.fakes["A"].clear_faults()
    row = item_row(web, "A")
    assert row["flag"] == "frozen" and "Matches" not in row["text"]
    assert "Did not answer. Tell Bart." in row["text"] and row["can_fix"] is False


def test_the_item_page_does_not_say_different_from_a_read_older_than_the_miss(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    web.login()
    web.env.svc.compare_scales(["B"])
    miss_on_b(web)
    row = item_row(web, "B")
    assert row["differs"] != [] and row["flag"] == "unsure" and "holds something different" not in row["text"]


def test_a_real_drift_with_no_miss_keeps_the_old_words_and_the_fix(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    web.env.fakes["B"].preload(tuple(Item(1, "1001", "1001", "Test Cheddar", "18.99", field6=" ", timestamp="2026-01-01 00:00:00").to_fields()))
    web.env.svc.compare_scales(["B"])
    row = item_row(web, "B")
    assert row["flag"] == "different" and row["text"].startswith("This scale holds something different") and row["missed"] is None and row["can_fix"] is True


def test_the_settle_screen_for_a_miss_has_one_button_and_never_says_holds_something_different():
    missed = SCRIPT.split("function renderMissedSettle")[1].split("/* settle a difference on one scale")[0]
    assert "'Our list is right. Put it on ' + scale + '.'" in missed
    assert "If you are not sure, call Bart." in missed
    assert "The scale is right" not in missed and "holds something different" not in missed
    assert "go('scale')" not in missed
    assert "miss.status_text" in missed and "miss.kind !== 'fixable'" in missed


def test_the_settle_screen_keeps_the_choice_for_a_real_difference():
    settle = SCRIPT.split("function renderSettle(plu, scale, my)")[1].split("/* mark a scale number")[0]
    assert "mine.missed" in settle and settle.index("mine.missed") < settle.index("The scale is right. Copy it into our list.")
    assert "holds something different from our list. Pick which one is right." in settle


def test_the_item_page_says_a_scale_did_not_answer_before_anyone_taps():
    """Round 6 N1: the server leaves that scale out of a check and refuses reviews. The page says so first."""
    item = SCRIPT.split("function renderItem(plu, my, checked)")[1].split("if (d.sending_now)")[0]
    assert "d.frozen_scales" in item and "'Did not answer. Tell Bart.'" in item
    assert "A check reads the other scales only." in item and "s.flag === 'frozen'" in item


def test_a_card_with_no_button_shows_no_link():
    boxes = SCRIPT.split("function unfinishedBoxes")[1].split("/* home: search */")[0]
    assert "c.button ? link(" in boxes and "if (!c.button) { return h('div', null, box('bad', 'Not finished', c.text, c.more)); }" in boxes


def fake_row(scale, plu, **kw):
    return UnfinishedWork(scale, "missed_change", int(plu) - 1000, plu, (), {"tool": "scale_preview_settle_drift", "scale": scale, "take": "catalog", "plu": plu}, "x", "read_failed", "c", **kw)


def test_many_misses_on_one_scale_roll_up_into_one_card_with_every_row_in_the_data():
    work = [fake_row("B", str(1000 + i)) for i in range(1, 61)] + [fake_row("C", "1100")]
    cards = words.unfinished_cards(work)
    assert len(cards) == 2
    rolled = cards[0]
    assert rolled["rolled_up"] is True and rolled["button"] is None and rolled["count"] == 60 and len(rolled["rows"]) == 60
    assert rolled["rows"][0]["plu"] == "1001"
    assert rolled["text"] == "60 items have not been confirmed on B." and "One check of B clears any that match" in rolled["more"]
    assert cards[1]["button"] == "Fix this on C"


def test_five_misses_stay_five_cards():
    assert len(words.unfinished_cards([fake_row("B", str(1000 + i)) for i in range(1, 6)])) == 5


def test_a_roll_up_keeps_a_scale_that_did_not_answer_and_what_our_list_did_not_take_apart():
    frozen = words.unfinished_cards([fake_row("B", str(1000 + i), may_be_frozen=True) for i in range(1, 8)])
    assert len(frozen) == 1 and "Did not answer. Tell Bart." in frozen[0]["more"]
    taken_or_not = words.unfinished_cards(
        [fake_row("B", str(1000 + i), catalog_took_it=False) for i in range(1, 8)] + [fake_row("B", str(1100 + i)) for i in range(1, 8)]
    )
    assert len(taken_or_not) == 2 and "our list did not take them" in taken_or_not[0]["text"] and "would put the old values back" in taken_or_not[0]["more"]


def test_a_card_says_when_the_list_could_not_be_built():
    card = words.unfinished_cards([UnfinishedWork("", "list_failed", 0, "", (), {}, "x")])[0]
    assert card["button"] is None and card["text"] == words.LIST_FAILED_TEXT and "all clear" in card["more"]


def test_one_bad_log_row_does_not_stop_the_item_page_or_the_home_cards(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b(web)
    env = web.env
    env.catalog._conn().execute("INSERT INTO change_log (change_id, event, payload, at, scale) VALUES ('x', 'scale_result', '{not json', '2026-11-02T09:00:00+00:00', 'B')")
    assert web.client.get("/api/item/1001").status_code == 200
    items = web.client.get("/api/unfinished").json()["items"]
    assert [c["kind"] for c in items] == ["list_failed"]


# ---------------------------------------------------------------- the staff guide agrees with the page


def test_the_staff_guide_says_what_the_page_does_for_a_missed_change():
    section = STAFF.split("## Not finished work")[1].split("\n## ")[0]
    assert "Did not answer. Tell Bart." in section
    assert "Check this scale, then call Bart" in section
    assert "may differ" in section and "If you are not sure, call Bart" in section
    assert "The page does not offer it there" not in section
    assert "The result page and the item page show it too" not in section


# ---------------------------------------------------------------- round 6 L9, the B8 corner: another product at that id


def miss_on_b_with_another_product(web):
    """B missed the edit, and its last read shows another product at that id. A settle is refused there,
    and an edit from the page meets the same product, so no button may point at either."""
    miss_on_b(web)
    web.env.fakes["B"].preload(tuple(Item(1, "1777", "1777", "Test Other", "10.00", timestamp="2026-01-01 00:00:00").to_fields()))
    web.env.svc.compare_scales(["B"])
    rows = [u for u in web.env.svc.list_unfinished() if u.scale == "B"]
    assert [(u.kind, u.plu) for u in rows] == [("missed_change", "1001")], "the miss is still real"


def test_a_missed_change_on_a_scale_holding_another_product_has_no_button_and_says_why(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b_with_another_product(web)
    [card] = web.client.get("/api/unfinished").json()["items"]
    assert card["button"] is None and card["no_fix"] is True
    assert card["text"] == "The newest change to PLU 1001 has not reached B."
    assert "When B was last read, it had a different product at this number." in card["more"]
    assert "Tell Bart." in card["more"] and "Change this item" not in card["more"] and "Test Other" not in card["more"]


def test_the_item_page_row_says_another_product_and_offers_no_fix(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b_with_another_product(web)
    rows = {r["scale"]: r for r in web.client.get("/api/item/1001").json()["scales"]}
    b = rows["B"]
    assert b["flag"] == "different_product" and b["can_fix"] is False
    assert b["missed"]["kind"] == "other_product" and "This page cannot fix that." in b["text"]
    assert "Change this item" not in b["text"]


def test_the_settle_refusal_no_longer_points_to_change_this_item(make_web):
    web = make_web({"A": True, "B": True}, clock=ticking_clock())
    miss_on_b_with_another_product(web)
    r = web.post("/api/settle/preview", {"scale": "B", "take": "catalog", "plu": "1001"}).json()
    assert r["ok"] is False and r["errors"][0]["code"] == "plu_edit_held"
    message = r["errors"][0]["message"]
    assert "Change this item" not in message and "different product" in message and "Tell Bart." in message


def test_the_other_product_words_keep_the_writing_rule():
    u = UnfinishedWork("B", "missed_change", 1, "1001", (), {"tool": "scale_preview_settle_drift"}, "x", "read_failed", "c1")
    [card] = words.unfinished_cards([u], {}, {("B", 1)})
    said = words.missed_settle_words(u, True, ["plu", "code"])
    plain = words.plain_error({"code": "plu_edit_held", "message": "x", "index": 0, "field": "plu"}, False)["message"]
    for text in (card["text"], card["more"], said["stop_text"], plain):
        assert "\u2014" not in text and "--" not in text
    assert said["kind"] == "other_product" and card["button"] is None
    assert words.unfinished_cards([u])[0]["button"] == "Fix this on B", "without another product the fix stays"
