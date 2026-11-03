# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round 7 recheck, the page: M1, M2, L3, L4 and L5.

M1: a scale that gives no reply to the page's own check or review is remembered, so no later tap reads it.
M2: one scale that may be frozen does not stop changes on the others. A review leaves it out, the result
says so, and it stays on the unfinished list as missing that change, with no fix button, until Bart clears it.

Fake scales on localhost, made up items. Each test counts LP messages to the scale that stalled.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "src" / "scaleconnect" / "web" / "static" / "app.js").read_text(encoding="utf-8")
STAFF = (ROOT / "docs" / "STAFF.md").read_text(encoding="utf-8")
DID_NOT_ANSWER = "Did not answer. Tell the Scale Manager."
THREE = {"A": True, "B": True, "C": True}
EDIT1 = [{"action": "edit", "target_plu": "1001", "price": "11.00"}]
EDIT2 = [{"action": "edit", "target_plu": "1002", "price": "7.00"}]


def ticking_clock():
    now = [datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)]

    def clock():
        now[0] += timedelta(seconds=1)
        return now[0]

    return clock


def three(make_web, flags=THREE):
    web = make_web(flags, clock=ticking_clock(), reply_timeout_seconds=0.3, connect_attempts=1)
    web.login()
    return web


def lp(web, scale):
    return web.env.fakes[scale].kinds().count("LP")


def check_with_b_silent(web, plu="1001"):
    web.env.fakes["B"].stall_read(times=None)
    rows = {r["scale"]: r for r in web.post("/api/check", {"plu": plu}).json()["scales"]}
    web.env.fakes["B"].clear_faults()
    return rows


def unfinished_on(web, scale):
    return [u for u in web.env.svc.list_unfinished() if u.scale == scale]


# ---------------------------------------------------------------- M1


def test_a_check_with_no_reply_is_remembered_and_the_next_tap_leaves_that_scale_out(make_web):
    web = three(make_web)
    first = check_with_b_silent(web)
    assert first["B"]["may_be_frozen"] is True and lp(web, "B") == 1
    assert web.env.svc.scales_may_be_frozen() == ["B"]
    assert web.client.get("/api/item/1001").json()["frozen_scales"] == ["B"]
    before = {s: lp(web, s) for s in THREE}
    second = {r["scale"]: r for r in web.post("/api/check", {"plu": "1001"}).json()["scales"]}
    assert second["B"]["status"] == "not_contacted" and DID_NOT_ANSWER in second["B"]["text"]
    assert lp(web, "B") == before["B"], "the second tap must send no LP to B"
    assert lp(web, "A") == before["A"] + 1 and lp(web, "C") == before["C"] + 1


def test_a_review_whose_read_gets_no_reply_is_remembered_and_the_next_review_leaves_it_out(make_web):
    web = three(make_web)
    web.env.fakes["B"].stall_read(times=None)
    review = web.preview(EDIT1, read=True).json()
    web.env.fakes["B"].clear_faults()
    assert any(w["may_be_frozen"] for w in review["warnings"])
    assert web.env.svc.scales_may_be_frozen() == ["B"]
    before = lp(web, "B")
    for read in (False, True):
        again = web.preview(EDIT1, read=read).json()
        assert again["ok"] is True and again["left_out"] == ["B"] and "B" not in again["scales"]
    assert lp(web, "B") == before


def test_a_send_of_a_review_made_before_the_check_with_no_reply_is_refused(make_web):
    web = three(make_web)
    early = web.preview(EDIT1, read=False).json()
    check_with_b_silent(web, "1002")
    before = lp(web, "B")
    r = web.send(early["preview_id"], warnings=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "scale_may_be_frozen"
    assert "Go back and review it again. B will be left out." in r.json()["error"]["message"]
    assert lp(web, "B") == before


def test_a_compare_that_answers_clears_a_freeze_from_a_check(make_web):
    web = three(make_web)
    check_with_b_silent(web)
    assert web.env.svc.compare_scales(["B"])[0].status == "compared"
    assert web.env.svc.scales_may_be_frozen() == []
    assert web.preview(EDIT1).json()["left_out"] == []


# ---------------------------------------------------------------- M2


def test_a_change_leaves_a_frozen_scale_out_goes_to_the_others_and_stays_unfinished_until_bart_settles(make_web):
    web = three(make_web)
    check_with_b_silent(web)
    before = lp(web, "B")
    p = web.preview(EDIT1, read=True).json()
    assert p["ok"] and p["scales"] == ["A", "C"] and p["left_out"] == ["B"]
    assert p["left_out_text"] == "B is left out. It did not answer last time. Tell the Scale Manager."
    assert web.send(p["preview_id"], warnings=False).status_code == 400, "leaving a scale out needs a tick"
    assert web.send(p["preview_id"], warnings=True).status_code == 200
    done = web.wait_finished(p["preview_id"])
    rows = {r["scale"]: r for r in done["rows"]}
    assert rows["A"]["status"] == "verified" and rows["C"]["status"] == "verified"
    assert rows["B"]["status"] == "left_out" and "left out" in rows["B"]["text"] and "Tell the Scale Manager" in rows["B"]["text"]
    assert done["left_out"] == ["B"] and done["left_out_text"] == p["left_out_text"]
    assert done["outcome"] == "partial" and done["heading"].startswith("Only some scales were changed")
    assert lp(web, "B") == before, "nothing may reach B"
    assert web.env.catalog.get_by_plu("1001").price == "11.00"

    # Through the existing missed change logic: B is missing this change, and may be frozen.
    listed = unfinished_on(web, "B")
    assert [(u.kind, u.plu, u.status, u.may_be_frozen) for u in listed] == [("missed_change", "1001", "not_sent", True)]
    card = [c for c in web.client.get("/api/unfinished").json()["items"] if c["scales"] == ["B"]][0]
    assert card["button"] is None and card["no_fix"] and DID_NOT_ANSWER in card["more"]
    row = {r["scale"]: r for r in web.client.get("/api/item/1001").json()["scales"]}["B"]
    assert row["flag"] == "frozen" and row["can_fix"] is False
    settle = web.post("/api/settle/preview", {"scale": "B", "take": "catalog", "plu": "1001"}).json()
    assert settle["ok"] is False and settle["errors"][0]["code"] == "scale_may_be_frozen"
    assert lp(web, "B") == before

    # Bart clears it: a compare of B answers. B still has the old price, so the miss stays, now with its button.
    web.env.svc.compare_scales(["B"])
    listed = unfinished_on(web, "B")
    assert [(u.kind, u.may_be_frozen) for u in listed] == [("missed_change", False)]
    card = [c for c in web.client.get("/api/unfinished").json()["items"] if c["scales"] == ["B"]][0]
    assert card["button"] == "Fix this on B"
    s = web.post("/api/settle/preview", {"scale": "B", "take": "catalog", "plu": "1001"}).json()
    assert s["ok"], s
    tokens = [d["ack_token"] for d in s["drift"] if d.get("ack_token")]
    assert web.send(s["preview_id"], warnings=True, drift=tokens).status_code == 200
    settled = web.wait_finished(s["preview_id"])
    assert [r["status"] for r in settled["rows"] if r["scale"] == "B"] == ["verified"]
    assert unfinished_on(web, "B") == []


def test_a_delete_and_an_add_that_leave_a_frozen_scale_out_stay_unfinished_on_it(make_web):
    web = three(make_web)
    check_with_b_silent(web)
    before = lp(web, "B")
    web.change_and_wait([{"action": "delete", "target_plu": "1002"}], warnings=True)
    web.change_and_wait([{"action": "add", "plu": "1888", "description": "Made Up Spread", "price": "5.00", "sold_by": "piece"}], warnings=True)
    kinds = sorted((u.kind, u.plu, u.may_be_frozen) for u in unfinished_on(web, "B"))
    assert kinds == [("delete", "1002", True), ("missed_change", "1888", True)]
    cards = [c for c in web.client.get("/api/unfinished").json()["items"] if "B" in c["scales"]]
    assert cards and all(c["button"] is None for c in cards)
    assert lp(web, "B") == before
    assert {u.scale for u in web.env.svc.list_unfinished()} == {"B"}, "A and C took both"


def test_a_scale_that_is_off_the_network_is_left_out_of_the_next_review(make_web):
    web = three(make_web)
    web.env.fakes["B"].stop_listening()
    web.change_and_wait(EDIT1, warnings=True)
    assert web.env.svc.scales_may_be_frozen() == ["B"]
    p = web.preview(EDIT2, read=True).json()
    assert p["ok"] and p["left_out"] == ["B"]
    web.env.fakes["B"].start_listening()


def test_when_every_scale_a_change_would_reach_may_be_frozen_the_review_is_refused(make_web):
    web = three(make_web, {"A": True, "B": True})
    check_with_b_silent(web)
    web.env.fakes["A"].stall_read(times=None)
    web.post("/api/check", {"plu": "1001"})
    web.env.fakes["A"].clear_faults()
    assert web.env.svc.scales_may_be_frozen() == ["A", "B"]
    before = (lp(web, "A"), lp(web, "B"))
    pv = web.preview(EDIT1, read=True).json()
    assert pv["ok"] is False and pv["errors"][0]["code"] == "scale_may_be_frozen" and pv["errors"][0]["message"].startswith(DID_NOT_ANSWER)
    assert (lp(web, "A"), lp(web, "B")) == before


def test_the_page_script_says_left_out_at_the_top_and_on_the_result():
    assert "pv.left_out_text" in SCRIPT and "d.left_out_text" in SCRIPT
    assert "wn.code === 'scale_left_out'" in SCRIPT
    review = SCRIPT.split("function renderReview")[1].split("function renderResult")[0]
    assert review.index("pv.left_out_text") < review.index("pv.differences.forEach"), "said before anything else"


# ---------------------------------------------------------------- L3


def test_the_words_no_longer_say_no_change_can_reach_any_scale():
    assert "no change can be sent to any scale" not in SCRIPT
    assert "will not send any change to any scale" not in STAFF
    section = STAFF.split("## Not finished work")[1].split("\n## ")[0]
    assert "is left out. It did not answer last time. Tell the Scale Manager." in section


def test_a_settle_on_another_scale_still_works_while_one_may_be_frozen(make_web):
    web = three(make_web)
    check_with_b_silent(web)
    before = lp(web, "B")
    s = web.post("/api/settle/preview", {"scale": "A", "take": "catalog", "plu": "1001"}).json()
    assert s["ok"] is False and s["errors"][0]["code"] == "no_drift", "A matches, so there is nothing to settle"
    assert lp(web, "B") == before


# ---------------------------------------------------------------- L4


def test_the_service_checks_for_a_freeze_under_the_lock_before_a_send(make_web):
    """A freeze another process logs after the page's own check is still caught. Here the page is passed by."""
    from scaleconnect.errors import ScaleMayBeFrozen

    web = three(make_web)
    p = web.preview(EDIT1, read=False).json()
    check_with_b_silent(web, "1002")
    before = {s: lp(web, s) for s in THREE}
    try:
        web.env.svc.apply_change(p["preview_id"], acknowledge_warnings=True, avoid_frozen=True)
    except ScaleMayBeFrozen as exc:
        assert exc.scales == ["B"]
    else:
        raise AssertionError("the apply must refuse")
    assert {s: lp(web, s) for s in THREE} == before, "nothing may go out to any scale"
    assert web.env.svc.list_changes(5) == [], "nothing was started"


def test_the_service_leaves_a_frozen_scale_out_of_a_read_under_the_lock(make_web):
    web = three(make_web)
    check_with_b_silent(web)
    before = lp(web, "B")
    reads = {r.scale: r for r in web.env.svc.read_scale_items(plus=["1001"], avoid_frozen=True)}
    assert reads["B"].status == "not_contacted" and reads["B"].may_be_frozen is True and reads["A"].status == "read"
    settle = web.env.svc.preview_settle_drift(scale="B", take="catalog", plu="1001", avoid_frozen=True)
    assert settle.ok is False and settle.errors[0].code == "scale_may_be_frozen"
    assert lp(web, "B") == before


# ---------------------------------------------------------------- L5


def test_a_log_that_cannot_be_read_refuses_rather_than_allows(make_web, monkeypatch):
    web = three(make_web)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(web.env.catalog, "frozen_scale_results", boom)
    assert web.env.svc.scales_may_be_frozen() == ["A", "B", "C"]
    before = {s: lp(web, s) for s in THREE}
    pv = web.preview(EDIT1, read=True).json()
    assert pv["ok"] is False and pv["errors"][0]["code"] == "scale_may_be_frozen"
    assert {s: lp(web, s) for s in THREE} == before


def test_the_staff_guide_has_no_double_hyphen_in_the_new_words():
    assert "--" not in re.sub(r"^\|[-| ]+\|$", "", STAFF, flags=re.M)
