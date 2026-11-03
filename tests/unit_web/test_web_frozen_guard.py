# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round 6 N1: while a scale may be frozen, nothing a staff member can reach on the page reads it or sends to it.

Fake scales on localhost, made up items. Each test counts LP messages to the scale that stalled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

DID_NOT_ANSWER = "Did not answer. Tell the Scale Manager."
THREE = {"A": True, "B": True, "C": True}


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


def stall_at_send(web, changes, scale="B"):
    p = web.preview(changes, read=False).json()
    assert p["ok"], p
    web.env.fakes[scale].stall_read(times=None)
    assert web.send(p["preview_id"], warnings=True).status_code == 200
    done = web.wait_finished(p["preview_id"])
    web.env.fakes[scale].clear_faults()
    return done


def lp(web, scale):
    return web.env.fakes[scale].kinds().count("LP")


def item(web, plu):
    return web.client.get(f"/api/item/{plu}").json()


def row_of(web, plu, scale):
    return {r["scale"]: r for r in item(web, plu)["scales"]}[scale]


def assert_page_reaches_nothing_on(web, scale, plu, alone=False):
    """Every route a button calls: check, review of a change, an add, a delete, a repeat, a settle.

    Round 7 M2: a review leaves that scale out and goes to the others. Only when it is the one scale
    left to reach is the review refused."""
    before = lp(web, scale)
    check = web.post("/api/check", {"plu": plu}).json()["scales"]
    shown = {r["scale"]: r for r in check}
    assert DID_NOT_ANSWER in shown[scale]["text"] and shown[scale]["status"] == "not_contacted"
    for changes in (
        [{"action": "edit", "target_plu": plu, "price": "13.00"}],
        [{"action": "delete", "target_plu": plu}],
        [{"action": "add", "plu": "1888", "description": "Made Up Spread", "price": "5.00", "sold_by": "piece"}],
    ):
        for read in (False, True):
            pv = web.preview(changes, read=read).json()
            if alone:
                assert pv["ok"] is False and pv["errors"][0]["code"] == "scale_may_be_frozen", pv
                assert pv["errors"][0]["message"].startswith(DID_NOT_ANSWER) and scale in pv["errors"][0]["message"]
                continue
            assert pv["ok"] is True and scale not in pv["scales"] and pv["left_out"] == [scale], pv
            assert pv["left_out_text"] == f"{scale} is left out. It did not answer last time. Tell the Scale Manager."
            assert [w["requires_ack"] for w in pv["warnings"] if w["code"] == "scale_left_out"] == [True]
    settle = web.post("/api/settle/preview", {"scale": scale, "take": "catalog", "plu": plu}).json()
    assert settle["ok"] is False and settle["errors"][0]["code"] == "scale_may_be_frozen"
    assert lp(web, scale) == before, "the page must send no LP to a scale that may be frozen"


def test_a_frozen_plu_change_has_no_repeat_anywhere_and_the_page_reaches_nothing(make_web):
    web = three(make_web)
    stall_at_send(web, [{"action": "edit", "target_plu": "1003", "plu": "1993"}])
    cards = web.client.get("/api/unfinished").json()["items"]
    plu_cards = [c for c in cards if c["kind"] == "plu_change"]
    assert plu_cards and all(c["button"] is None and c["no_fix"] for c in plu_cards)
    assert DID_NOT_ANSWER in plu_cards[0]["more"]
    row = row_of(web, "1993", "B")
    assert row["flag"] == "frozen" and row["repeat"] is None and row["can_fix"] is False and DID_NOT_ANSWER in row["text"]
    assert item(web, "1993")["frozen_scales"] == ["B"]
    assert_page_reaches_nothing_on(web, "B", "1993")


def test_a_frozen_delete_has_no_repeat_and_the_page_reaches_nothing(make_web):
    web = three(make_web)
    stall_at_send(web, [{"action": "delete", "target_plu": "1002"}], scale="C")
    cards = [c for c in web.client.get("/api/unfinished").json()["items"] if c["kind"] == "delete"]
    assert cards and all(c["button"] is None for c in cards) and DID_NOT_ANSWER in cards[0]["more"]
    assert_page_reaches_nothing_on(web, "C", "1001")


def test_a_frozen_missed_change_blocks_change_and_check_on_any_item(make_web):
    web = three(make_web)
    stall_at_send(web, [{"action": "edit", "target_plu": "1001", "price": "11.00"}], scale="C")
    card = [c for c in web.client.get("/api/unfinished").json()["items"] if c["scales"] == ["C"]][0]
    assert card["button"] is None and DID_NOT_ANSWER in card["more"]
    assert_page_reaches_nothing_on(web, "C", "1001")
    assert_page_reaches_nothing_on(web, "C", "1003")


def test_a_check_still_reads_the_other_scales(make_web):
    web = three(make_web)
    stall_at_send(web, [{"action": "edit", "target_plu": "1001", "price": "11.00"}], scale="C")
    a, b = lp(web, "A"), lp(web, "B")
    rows = {r["scale"]: r for r in web.post("/api/check", {"plu": "1002"}).json()["scales"]}
    assert rows["A"]["status"] == "read" and rows["B"]["status"] == "read" and lp(web, "A") == a + 1 and lp(web, "B") == b + 1


def test_a_stall_that_left_no_row_still_gets_a_card_and_blocks_the_page(make_web):
    """A one scale change that moved nothing leaves no unfinished row. The log still says that scale gave no reply."""
    web = make_web({"B": True}, clock=ticking_clock(), reply_timeout_seconds=0.3, connect_attempts=1)
    web.login()
    stall_at_send(web, [{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    assert web.env.svc.list_unfinished() == []
    cards = web.client.get("/api/unfinished").json()["items"]
    assert [c["kind"] for c in cards] == ["frozen_scale"] and cards[0]["button"] is None and DID_NOT_ANSWER in cards[0]["more"]
    assert_page_reaches_nothing_on(web, "B", "1001", alone=True)


def test_a_review_made_before_the_freeze_cannot_be_sent(make_web):
    web = three(make_web)
    early = web.preview([{"action": "edit", "target_plu": "1002", "price": "7.00"}], read=False).json()
    stall_at_send(web, [{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    before = lp(web, "B")
    r = web.send(early["preview_id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "scale_may_be_frozen"
    assert r.json()["error"]["message"].startswith(DID_NOT_ANSWER) and lp(web, "B") == before


def test_bart_clears_it_with_one_compare_of_that_scale_and_the_page_works_again(make_web):
    """Through Claude or the command line, once a person has looked: a compare of that scale that answers."""
    web = three(make_web)
    stall_at_send(web, [{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    assert web.preview([{"action": "edit", "target_plu": "1002", "price": "7.00"}]).json()["left_out"] == ["B"]
    compared = web.env.svc.compare_scales(["B"])
    assert compared[0].status == "compared"
    assert web.env.svc.scales_may_be_frozen() == []
    assert item(web, "1001")["frozen_scales"] == []
    card = [c for c in web.client.get("/api/unfinished").json()["items"] if c["scales"] == ["B"]][0]
    assert card["button"] == "Fix this on B", "the miss is still open, now with its usual button"
    again = web.preview([{"action": "edit", "target_plu": "1002", "price": "7.00"}]).json()
    assert again["ok"] is True and again["left_out"] == [] and "B" in again["scales"]


def test_a_scale_the_page_cannot_reach_does_not_block_it(make_web):
    """With B's writes and reads off, nothing here would reach B, so the others can still be changed."""
    from webenv import Web

    web = three(make_web)
    stall_at_send(web, [{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    web.env.set_write_enabled("B", False)
    later = Web(web.env)
    later.login()
    assert web.env.svc.scales_may_be_frozen() == ["B"], "the MCP list still names it"
    assert later.client.get("/api/item/1001").json()["frozen_scales"] == []
    before = lp(later, "B")
    assert later.preview([{"action": "edit", "target_plu": "1002", "price": "7.00"}], read=True).json()["ok"] is True
    assert lp(later, "B") == before
