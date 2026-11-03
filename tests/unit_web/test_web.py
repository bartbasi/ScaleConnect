# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Tests of the web page: PIN, lockout, cross site protection, sending, results, escaping."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import pytest

from scaleconnect import scale as scale_module
from scaleconnect.config import Config, ListenConfig
from scaleconnect.errors import ConfigError
from scaleconnect.models import Item, ScaleResult
from scaleconnect.web import create_app, words
from scaleconnect.web.__main__ import main as web_main
from scaleconnect.web.security import listen_host_problem

from webenv import PIN

EM, EN = chr(0x2014), chr(0x2013)
EDIT = [{"action": "edit", "target_plu": "1001", "price": "10.10"}]
STATIC = Path(__file__).resolve().parents[2] / "src" / "scaleconnect" / "web" / "static"


def price_on(fake, item_id=1):
    return fake.item(item_id)[4]


# ---------------------------------------------------------------- PIN, lockout, sessions

def test_browsing_needs_no_pin(web):
    assert web.client.get("/").status_code == 200
    assert web.client.get("/api/state").json()["signed_in"] is False
    assert web.client.get("/api/search", params={"q": "1001"}).json()["items"][0]["plu"] == "1001"
    assert web.client.get("/api/item/1001").json()["item"]["description"] == "Test Cheddar"
    assert web.client.get("/api/changes").status_code == 200


def test_changes_need_the_pin(web):
    r = web.preview(EDIT, csrf=None)
    assert r.status_code == 401 and r.json()["error"]["code"] == "pin_required"
    r = web.send("whatever")
    assert r.status_code == 401
    assert web.env.silent("A")  # nothing reached the scale


def test_pin_is_needed_even_with_a_csrf_token_from_someone_else(web, make_web):
    other = make_web({"A": True})
    other.login()
    r = web.post("/api/preview", {"changes": EDIT, "read": False}, csrf=other.csrf)
    assert r.status_code == 401


def test_wrong_pin_is_refused_and_right_pin_signs_in(web):
    r = web.login("0000")
    assert r.status_code == 401 and r.json()["error"]["code"] == "wrong_pin"
    assert "set-cookie" not in r.headers
    r = web.login()
    assert r.status_code == 200
    assert web.client.get("/api/state").json()["signed_in"] is True


def test_session_cookie_is_httponly_and_samesite_strict(web):
    r = web.login()
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert PIN not in r.text and PIN not in r.headers["set-cookie"]


def test_lockout_after_repeated_wrong_pins_then_it_ends(web):
    for _ in range(5):
        assert web.login("nope").status_code in (401, 429)
    r = web.login("nope")
    assert r.status_code == 429 and r.json()["error"]["code"] == "locked"
    # Even the right PIN is refused while locked
    assert web.login().status_code == 429
    assert web.client.get("/api/state").json()["signed_in"] is False
    web.clock.now += 61
    assert web.login().status_code == 200


def test_a_right_pin_resets_the_count(web):
    for _ in range(4):
        web.login("nope")
    assert web.login().status_code == 200
    for _ in range(4):
        assert web.login("nope").status_code == 401
    assert web.login().status_code == 200


def test_logout_ends_the_session(web):
    web.login()
    assert web.post("/api/logout", {}).status_code == 200
    assert web.preview(EDIT).status_code == 401


def test_pin_never_appears_in_logs_or_responses(web, caplog):
    caplog.set_level(logging.DEBUG)
    bad = web.login("secret-wrong-pin-77")
    good = web.login()
    web.preview(EDIT)
    assert "secret-wrong-pin-77" not in caplog.text and PIN not in caplog.text
    for r in (bad, good):
        assert PIN not in r.text and "secret-wrong-pin-77" not in r.text
    assert PIN not in repr(web.config)


def test_pin_compare_uses_a_constant_time_check():
    import inspect
    from scaleconnect.web import security

    source = inspect.getsource(security.Auth.pin_matches)
    assert "compare_digest" in source


# ---------------------------------------------------------------- cross site protection

def test_missing_or_wrong_csrf_token_is_refused(web):
    web.login()
    r = web.preview(EDIT, csrf=None)
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_csrf"
    r = web.preview(EDIT, csrf="not-the-token")
    assert r.status_code == 403
    assert web.env.silent("A")


def test_cross_site_origin_is_refused_everywhere(web):
    evil = {"Origin": "http://evil.example"}
    r = web.client.post("/api/login", json={"pin": PIN}, headers=evil)
    assert r.status_code == 403 and r.json()["error"]["code"] == "cross_site"
    web.login()
    r = web.post("/api/preview", {"changes": EDIT, "read": False}, headers=evil)
    assert r.status_code == 403 and r.json()["error"]["code"] == "cross_site"
    r = web.post("/api/send", {"preview_id": "x"}, headers=evil)
    assert r.status_code == 403
    r = web.post("/api/logout", {}, headers=evil)
    assert r.status_code == 403
    assert web.env.silent("A")


def test_cross_site_fetch_metadata_is_refused(web):
    web.login()
    r = web.post("/api/preview", {"changes": EDIT, "read": False}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_post_is_allowed(web):
    web.login()
    ok = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert web.post("/api/preview", {"changes": EDIT, "read": False}, headers=ok).status_code == 200


def test_state_changing_routes_do_not_answer_get(web):
    web.login()
    for path in ("/api/preview", "/api/send", "/api/login"):
        assert web.client.get(path).status_code == 405


# ---------------------------------------------------------------- start up rules

def test_app_refuses_to_build_without_a_pin(web):
    with pytest.raises(ConfigError):
        create_app(web.env.svc, Config(scales=web.env.config.scales, settings=web.env.settings, pin=None))
    with pytest.raises(ConfigError):
        create_app(web.env.svc, Config(scales=web.env.config.scales, settings=web.env.settings, pin=""))


def _config_file(tmp_path, pin_line, host="127.0.0.1"):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[auth]\n{pin_line}\n[web]\nhost = "{host}"\nport = 8099\n'
        '[[scales]]\nname = "S1"\naddress = "192.0.2.10"\n',
        encoding="utf-8",
    )
    return path


def test_main_refuses_to_start_with_no_pin_or_the_example_pin(tmp_path, capsys):
    assert web_main(["--config", str(_config_file(tmp_path, ""))]) == 8
    assert "PIN" in capsys.readouterr().err
    assert web_main(["--config", str(_config_file(tmp_path, 'pin = "change-me"'))]) == 8


def test_main_refuses_a_wildcard_host(tmp_path, capsys):
    for host in ("0.0.0.0", "::", "", "0", "00.00.00.00", "::0", "localhost", "100.100.100.100", "bartcc"):
        assert web_main(["--config", str(_config_file(tmp_path, 'pin = "abc123"', host))]) == 8
    assert "wildcard" in capsys.readouterr().err


def test_listen_host_check():
    assert listen_host_problem("0.0.0.0") and listen_host_problem("") and listen_host_problem("::")
    assert listen_host_problem("127.0.0.1") is None
    assert listen_host_problem("192.0.2.5") is None
    for bad in ("0", "0.0", "0x0", "00.00.00.00", "::0", "0::0", "0000::", "::1", "localhost", "bartcc",
                "100.64.0.1", "100.100.100.100", "100.127.255.255", "224.0.0.1", "169.254.1.1", "192.0.2.5/24", " "):
        assert listen_host_problem(bad), bad
    assert listen_host_problem("100.128.0.1") is None
    assert ListenConfig().host == "127.0.0.1"  # the library's default is localhost, never a wildcard


# ---------------------------------------------------------------- reading

def test_search_by_plu_and_description_uses_the_catalog_only(web):
    assert [i["plu"] for i in web.client.get("/api/search", params={"q": "brie"}).json()["items"]] == ["1002"]
    assert web.client.get("/api/search", params={"q": "zzzz"}).json()["items"] == []
    assert web.env.silent("A")  # no scale was asked


def test_item_shows_each_scale_and_flags_one_that_differs(make_web):
    web = make_web({"A": True, "B": True})
    # Make B hold a different price, then edit through the app so the catalog reads both.
    web.env.fakes["B"].preload(*[tuple(Item(1, "1001", "1001", "Test Cheddar", "11.11", field6=" ",
                                          timestamp="2026-01-01 00:00:00").to_fields())])
    web.login()
    p = web.preview(EDIT, read=True).json()
    assert p["ok"] and any(d["scale"] == "B" for d in p["drift"])
    web.env.catalog.record_scale_reads("A", [web.env.catalog.get_by_id(1)], "2026-01-02T00:00:00")
    web.env.catalog.record_scale_reads("B", [Item(1, "1001", "1001", "Test Cheddar", "11.11", field6=" ",
                                                 timestamp="2026-01-01 00:00:00")], "2026-01-02T00:00:00")
    d = web.client.get("/api/item/1001").json()
    rows = {r["scale"]: r for r in d["scales"]}
    assert rows["A"]["flag"] == "same"
    assert rows["B"]["flag"] == "different" and rows["B"]["differs"] == ["price"]
    assert set(rows) == {"A", "B"}


def test_unknown_item_is_a_plain_404(web):
    r = web.client.get("/api/item/9999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "item_not_found"


# ---------------------------------------------------------------- edit, add, delete, PLU hold

def test_a_change_reaches_the_fake_scale(web):
    web.login()
    p, done = web.change_and_wait([{"action": "edit", "target_plu": "1001", "price": "10.10"}], read=True)
    assert done["state"] == "finished"
    assert price_on(web.env.fakes["A"]) == "10.10"
    assert web.env.catalog.get_by_id(1).price == "10.10"
    assert [r["status"] for r in done["rows"]] == ["verified"]
    assert done["rows"][0]["text"] == "A: changed and checked"


def test_price_typed_loosely_is_tidied_but_the_library_still_checks(web):
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1001", "price": "$12.5"}]).json()
    assert p["ok"]
    assert p["differences"][0]["fields"]["price"]["after"] == "12.50"
    bad = web.preview([{"action": "edit", "target_plu": "1001", "price": "0"}]).json()
    assert not bad["ok"] and bad["errors"][0]["code"] == "invalid_price"
    bad = web.preview([{"action": "edit", "target_plu": "1001", "price": "1000.00"}]).json()
    assert not bad["ok"]
    bad = web.preview([{"action": "edit", "target_plu": "1001", "price": "abc"}]).json()
    assert not bad["ok"] and bad["errors"][0]["code"] == "invalid_price"
    bad = web.preview([{"action": "edit", "target_plu": "1001", "price": 9.99}]).json()
    assert not bad["ok"]
    assert web.env.silent("A")


@pytest.mark.parametrize("value,expected", [
    ("9.99", "9.99"), ("9.5", "9.50"), ("10", "10.00"), (" $3 ", "3.00"), (".5", "0.50"), ("5.", "5.00"),
])
def test_normalize_price_good(value, expected):
    assert words.normalize_price(value) == (expected, None)


@pytest.mark.parametrize("value", ["", "abc", "1.234", "-1", "1e2", "1,5", "$", ".", 5, None, "1234.00"])
def test_normalize_price_bad(value):
    price, why = words.normalize_price(value)
    assert price is None and why


def test_create_app_leaves_the_plu_screen_off_unless_asked_and_a_preview_sends_nothing(web):
    # The start up file asks for it (see test_web_delete_plu). The library checks every PLU change either way.
    web.login()
    state = web.client.get("/api/state").json()
    assert state["plu_edit_enabled"] is False
    p = web.preview([{"action": "edit", "target_plu": "1001", "plu": "1999"}]).json()
    assert p["ok"]
    assert web.env.silent("A")


def test_plu_edit_switch_is_easy_to_turn_on(make_web):
    web = make_web({"A": True}, web_kw={"plu_edit_enabled": True})
    assert web.client.get("/api/state").json()["plu_edit_enabled"] is True


def test_add_item_by_weight_and_by_piece(web):
    web.login()
    p, done = web.change_and_wait([
        {"action": "add", "plu": "2001", "description": "New Cheese", "price": "7.25", "sold_by": "weight"},
        {"action": "add", "plu": "2002", "description": "New Piece", "price": "3", "sold_by": "piece"},
    ], read=True)
    assert done["rows"][0]["status"] == "verified"
    assert web.env.catalog.get_by_plu("2001").price == "7.25"
    assert web.env.catalog.get_by_plu("2002").price == "3.00"
    assert web.env.catalog.get_by_plu("2001").fields_7_12[0] != web.env.catalog.get_by_plu("2002").fields_7_12[0]


def test_add_duplicate_plu_is_refused_with_plain_words(web):
    web.login()
    p = web.preview([{"action": "add", "plu": "1001", "description": "Copy", "price": "1.00"}]).json()
    assert not p["ok"] and p["errors"][0]["code"] == "duplicate_plu"


def test_page_cannot_send_fields_it_should_not(web):
    web.login()
    for bad in (
        {"action": "add", "plu": "2001", "description": "X", "price": "1.00", "replace_orphan_id": 5},
        {"action": "edit", "target_id": 1, "price": "1.00"},
        {"action": "edit", "target_plu": "1001", "price": "1.00", "fields_7_12": ["0"] * 6},
        {"action": "zap", "target_plu": "1001"},
    ):
        p = web.preview([bad]).json()
        assert not p["ok"], bad
    assert web.preview([]).json()["ok"] is False


def test_delete_is_not_offered_when_the_library_says_no(web, monkeypatch):
    monkeypatch.setattr(scale_module, "DELETE_AVAILABLE", False)
    state = web.client.get("/api/state").json()
    assert state["capabilities"]["delete"] is False
    assert state["delete_note"] == "Deleting is not available right now. Ask the Scale Manager."
    assert "yet" not in state["delete_note"]


def test_delete_is_offered_when_the_library_says_yes(web):
    assert web.client.get("/api/state").json()["capabilities"]["delete"] is True


def test_a_delete_preview_needs_an_acknowledgement_and_sends_nothing_yet(web):
    web.login()
    p = web.preview([{"action": "delete", "target_plu": "1002"}]).json()
    assert p["ok"]
    assert any(w["code"] == "delete_placeholder" and w["requires_ack"] for w in p["warnings"])
    r = web.send(p["preview_id"], warnings=False)
    assert r.status_code == 400 and r.json()["error"]["code"] == "acknowledgement_required"
    assert web.env.writes("A") == []


# ---------------------------------------------------------------- review: warnings and drift

def test_large_price_change_needs_an_acknowledgement(web):
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1001", "price": "20.00"}]).json()
    warn = [w for w in p["warnings"] if w["code"] == "price_change_large"]
    assert warn and warn[0]["requires_ack"]
    r = web.send(p["preview_id"], warnings=False)
    assert r.status_code == 400 and r.json()["error"]["code"] == "acknowledgement_required"
    assert web.env.writes("A") == []
    # The same preview can still be sent once it is acknowledged
    r = web.send(p["preview_id"], warnings=True)
    assert r.status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert done["rows"][0]["status"] == "verified"
    assert price_on(web.env.fakes["A"]) == "20.00"


def test_drift_is_shown_and_needs_its_own_acknowledgement(web):
    web.env.fakes["A"].preload(tuple(Item(1, "1001", "1001", "Test Cheddar", "9.00", field6=" ",
                                          timestamp="2026-01-01 00:00:00").to_fields()))
    web.login()
    p = web.preview(EDIT, read=True).json()
    assert p["scales_read"] == ["A"]
    assert len(p["drift"]) == 1 and p["drift"][0]["scale"] == "A" and p["drift"][0]["ack_token"]
    # Sent without the acknowledgement: refused, nothing written
    r = web.send(p["preview_id"])
    assert r.status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert done["rows"][0]["status"] == "drift_refused"
    assert done["severity"] == "bad" and not done["success"]
    assert "nothing was sent" in done["rows"][0]["text"].lower()
    assert web.env.writes("A") == []
    assert price_on(web.env.fakes["A"]) == "9.00"
    # A new preview with the acknowledgement goes through
    p2 = web.preview(EDIT, read=True).json()
    token = p2["drift"][0]["ack_token"]
    assert web.send(p2["preview_id"], drift=[token]).status_code == 200
    done = web.wait_finished(p2["preview_id"])
    assert done["rows"][0]["status"] == "verified"
    assert price_on(web.env.fakes["A"]) == "10.10"


def test_expired_or_unknown_preview_gives_a_plain_answer(web):
    web.login()
    r = web.send("no-such-preview")
    assert r.status_code == 404 and r.json()["error"]["code"] == "preview_not_found"
    assert web.client.get("/api/send/no-such-preview").status_code == 404


# ---------------------------------------------------------------- results

def test_per_scale_results_and_partial_failure_is_not_a_success(web3):
    web3.env.fakes["B"].error_on_ids(1)
    web3.env.fakes["C"].stop_listening()
    web3.login()
    p, done = web3.change_and_wait(EDIT)
    rows = {r["scale"]: r for r in done["rows"]}
    assert rows["A"]["text"] == "A: changed and checked" and rows["A"]["severity"] == "ok"
    assert rows["B"]["status"] == "rejected" and rows["B"]["severity"] == "bad"
    assert "refused" in rows["B"]["text"]
    assert rows["C"]["status"] == "unreachable" and "could not connect" in rows["C"]["text"]
    assert done["success"] is False
    assert done["outcome"] == "partial"
    assert done["severity"] != "ok"
    assert not done["heading"].lower().startswith("done")
    assert [r["scale"] for r in done["rows"]] == ["A", "B", "C"]
    assert price_on(web3.env.fakes["A"]) == "10.10"


def test_all_scales_failing_is_called_a_failure(web):
    web.env.fakes["A"].stop_listening()
    web.login()
    p, done = web.change_and_wait(EDIT)
    assert done["success"] is False and done["severity"] == "bad"
    assert done["rows"][0]["status"] == "unreachable"


def test_scale_with_writes_off_is_not_called_changed(make_web):
    web = make_web({"A": True, "B": False})
    web.login()
    p, done = web.change_and_wait(EDIT)
    rows = {r["scale"]: r for r in done["rows"]}
    assert rows["B"]["status"] == "skipped_not_write_enabled"
    assert "not changed" in rows["B"]["text"]
    assert done["success"] is False
    assert price_on(web.env.fakes["B"]) == "10.00"


def test_plain_words_for_every_status():
    for status in ("verified", "accepted_not_verified", "rejected", "unknown", "mismatch", "unreachable",
                   "read_failed", "wrong_scale", "drift_refused", "skipped_not_write_enabled", "not_available"):
        row = words.scale_row(ScaleResult("FRONT", status, "library words"))
        assert row["text"].startswith("FRONT: ")
        assert "â€”" not in row["text"] and "--" not in row["text"]
    row = words.scale_row(ScaleResult("FRONT", "verified", ""))
    assert row["text"] == "FRONT: changed and checked"
    row = words.scale_row(ScaleResult("FRONT", "accepted_not_verified", ""))
    assert row["text"] == "FRONT: accepted, but not checked. Look at the scale."
    assert row["severity"] == "warn"
    assert words.scale_row(ScaleResult("FRONT", "unknown", ""))["severity"] == "bad"
    for outcome, (heading, severity) in words.OUTCOME_WORDS.items():
        assert "â€”" not in heading and "--" not in heading
        if outcome != "success":
            assert severity != "ok", outcome


def test_result_can_be_looked_up_again_after_a_reconnect_or_restart(web):
    web.login()
    p, done = web.change_and_wait(EDIT)
    # A different app object on the same service and log, like a phone coming back later
    again = create_app(web.env.svc, web.config, allowed_hosts=["testserver"])
    from fastapi.testclient import TestClient

    other = TestClient(again)
    d = other.get(f"/api/send/{p['preview_id']}").json()
    assert d["state"] == "finished" and d["rows"][0]["status"] == "verified"
    assert d["changes"] and d["changes"][0]["plu"] == "1001"
    listed = other.get("/api/changes").json()["changes"]
    assert listed and listed[0]["preview_id"] == p["preview_id"]


def test_a_preview_that_was_never_sent_says_so(web):
    web.login()
    p = web.preview(EDIT).json()
    d = web.client.get(f"/api/send/{p['preview_id']}").json()
    assert d["state"] == "not_sent" and d["rows"] == []


# ---------------------------------------------------------------- live progress and double sends

def test_live_progress_per_scale_while_sending(web3):
    gate, entered = threading.Event(), threading.Event()
    real = web3.env.svc._apply_to_scale

    def slow(scale, *a, **k):
        if scale.name == "B":
            entered.set()
            assert gate.wait(20)
        return real(scale, *a, **k)

    web3.env.svc._apply_to_scale = slow
    web3.login()
    p = web3.preview(EDIT).json()
    r = web3.send(p["preview_id"])
    assert r.status_code == 200 and r.json()["started"] is True
    assert entered.wait(20)
    mid = web3.client.get(f"/api/send/{p['preview_id']}").json()
    assert mid["state"] == "running"
    rows = {x["scale"]: x for x in mid["rows"]}
    assert rows["A"]["status"] == "verified" and rows["A"]["state"] == "done"
    assert rows["B"]["state"] == "working" and rows["C"]["state"] == "waiting"
    assert mid["success"] is False
    gate.set()
    done = web3.wait_finished(p["preview_id"])
    assert done["state"] == "finished"
    assert {x["scale"]: x["status"] for x in done["rows"]} == {"A": "verified", "B": "verified", "C": "verified"}
    assert done["success"] is True


def test_double_send_is_prevented(make_web):
    fast = make_web({"A": True}, web_kw={"early_wait_seconds": 0.2})
    gate = threading.Event()
    calls = []
    real = fast.env.svc.apply_change

    def slow(*a, **k):
        calls.append(a)
        assert gate.wait(20)
        return real(*a, **k)

    fast.env.svc.apply_change = slow
    fast.login()
    p = fast.preview(EDIT).json()
    first = fast.send(p["preview_id"])
    second = fast.send(p["preview_id"])
    third = fast.send(p["preview_id"])
    assert first.json()["started"] is True
    assert second.json()["started"] is False and third.json()["started"] is False
    assert len(calls) == 1
    # A different change while one is running is refused, not queued
    p2 = fast.preview([{"action": "edit", "target_plu": "1002", "price": "21.00"}]).json()
    busy = fast.send(p2["preview_id"])
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
    assert fast.client.get("/api/state").json()["busy"] is True
    gate.set()
    done = fast.wait_finished(p["preview_id"])
    assert done["state"] == "finished"
    again = fast.send(p["preview_id"])
    assert again.status_code == 200 and again.json()["started"] is False
    assert len(calls) == 1
    assert len(fast.env.writes("A")) == 1  # one GP batch reached the scale, once

def test_sending_the_same_preview_after_it_finished_does_not_write_again(web):
    web.login()
    p, done = web.change_and_wait(EDIT)
    n = len(web.env.writes("A"))
    # New app on the same service, as after a restart: the library itself refuses a second use
    from fastapi.testclient import TestClient

    other = TestClient(create_app(web.env.svc, web.config, allowed_hosts=["testserver"]))
    r = other.post("/api/login", json={"pin": PIN})
    csrf = r.json()["csrf"]
    r = other.post("/api/send", json={"preview_id": p["preview_id"]}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409 and r.json()["error"]["code"] == "preview_already_applied"
    assert len(web.env.writes("A")) == n


# ---------------------------------------------------------------- escaping and external resources

ODD = "<img src=x onerror=alert(1)>&\"'`"


def test_odd_characters_are_data_and_never_html(make_web):
    seed = [Item(1, "1001", "1001", ODD[:35], "10.00", field6=" ", timestamp="2026-01-01 00:00:00")]
    web = make_web({"A": True}, seed=seed)
    r = web.client.get("/api/search", params={"q": "1001"})
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.json()["items"][0]["description"] == ODD[:35]
    item = web.client.get("/api/item/1001")
    assert item.headers["content-type"].startswith("application/json")
    # The page itself never carries item text
    assert ODD[:10] not in web.client.get("/").text
    # A search for markup does not echo the markup into any page
    r = web.client.get("/api/search", params={"q": "<script>alert(1)</script>"})
    assert r.headers["content-type"].startswith("application/json")


def test_script_never_builds_html_from_text():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute('on"):
        assert bad not in source, bad
    assert "textContent" in source


def test_security_headers_on_every_response(web):
    for path in ("/", "/static/app.js", "/static/style.css", "/api/state"):
        r = web.client.get(path)
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"


def test_pages_use_no_external_resources(web):
    texts = {
        "/": web.client.get("/").text,
        "/static/app.js": web.client.get("/static/app.js").text,
        "/static/style.css": web.client.get("/static/style.css").text,
    }
    for path, text in texts.items():
        assert not re.search(r"https?://", text, re.I), path
        assert not re.search(r"(?<![:\w])//[a-z0-9.-]+\.[a-z]{2,}", text, re.I), path
        assert "@import" not in text, path
        assert not re.search(r"url\(\s*['\"]?(?!data:)(?:https?:)?//", text, re.I), path
    html = texts["/"]
    refs = re.findall(r"""(?:src|href)\s*=\s*["']([^"']*)["']""", html, re.I)
    assert refs, "the page should link its own script and style"
    for ref in refs:
        assert ref.startswith(("/", "#")) and not ref.startswith("//"), ref
    assert "<script src=\"/static/app.js\">" in html
    assert "fonts.googleapis" not in html


def test_page_text_has_no_em_dashes_or_double_hyphens():
    for name in ("index.html", "app.js", "style.css"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert "â€”" not in text and "â€“" not in text, name
        assert "--" not in text, name
    for module in (words,):
        for value in list(words.SCALE_WORDS.values()) + list(words.OUTCOME_WORDS.values()):
            assert "â€”" not in value[0] and "--" not in value[0]


def test_no_scale_or_socket_code_in_the_web_package():
    root = STATIC.parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(socket|scaleconnect\.(scale|protocol))\b", text, re.M), path
        assert not re.search(r"^\s*from\s+scaleconnect\s+import\s+.*\b(scale|protocol)\b", text, re.M), path

