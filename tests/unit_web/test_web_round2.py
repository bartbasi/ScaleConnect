# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round 2: Host check, settling drift, orphans, stopped changes, and the words for each result."""

from __future__ import annotations

import inspect
import threading
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scaleconnect.config import Config, ListenConfig
from scaleconnect.errors import ConfigError
from scaleconnect.models import Item, ScaleResult
from scaleconnect.web import create_app, words
from scaleconnect.web.security import (
    allowed_host_headers, config_mode_problem, same_origin,
)

from webenv import PIN, Web

EDIT = [{"action": "edit", "target_plu": "1001", "price": "10.10"}]


def item(id_, plu, desc, price, **kw):
    return Item(id_, plu, plu, desc, price, field6=kw.pop("field6", " "), timestamp="2026-01-01 00:00:00", **kw)


def hold(fake, *items):
    fake.preload(*[tuple(i.to_fields()) for i in items])


# ---------------------------------------------------------------- M6: Host check

def test_host_check_uses_the_configured_address_and_port_by_default(web):
    cfg = Config(scales=web.env.config.scales, settings=web.env.settings, pin=PIN, web=ListenConfig("127.0.0.1", 8080))
    client = TestClient(create_app(web.env.svc, cfg))
    for ok in ("127.0.0.1:8080", "localhost:8080", "LOCALHOST:8080"):
        assert client.get("/api/state", headers={"Host": ok}).status_code == 200, ok
    for bad in ("evil.example:8080", "127.0.0.1:9999", "127.0.0.1", "192.0.2.5:8080", "evil.example"):
        r = client.get("/api/state", headers={"Host": bad})
        assert r.status_code == 421 and r.json()["error"]["code"] == "wrong_host", bad


def test_dns_rebinding_gets_nothing(web):
    evil = {"Host": "evil.example:8080", "Origin": "http://evil.example:8080"}
    client = TestClient(create_app(web.env.svc, web.config, allowed_hosts=["127.0.0.1:8080"]))
    for path in ("/", "/static/app.js", "/api/search?q=1", "/api/item/1001", "/api/changes", "/api/state"):
        assert client.get(path, headers=evil).status_code == 421, path
    r = client.post("/api/login", json={"pin": PIN}, headers=evil)
    assert r.status_code == 421 and "set-cookie" not in r.headers
    r = client.post("/api/login", json={"pin": "wrong"}, headers=evil)
    assert r.status_code == 421  # no PIN guessing either


def test_allowed_host_values():
    assert allowed_host_headers("192.0.2.5", 8080) == {"192.0.2.5:8080"}
    assert allowed_host_headers("127.0.0.1", 8080) == {"127.0.0.1:8080", "localhost:8080"}
    assert allowed_host_headers("192.0.2.5", 80) == {"192.0.2.5:80", "192.0.2.5"}


def test_https_origin_is_not_accepted_for_this_plain_http_page():
    assert same_origin("http://a:1", "a:1")
    assert not same_origin("https://a:1", "a:1")
    assert not same_origin("http://a:1", "b:1")
    assert not same_origin("null", "a:1")


# ---------------------------------------------------------------- M8, L2: config mode, PIN, proxy headers

def test_config_mode_must_be_private():
    assert config_mode_problem("/x/config.toml", mode=0o100600) is None
    assert config_mode_problem("/x/config.toml", mode=0o100400) is None
    for mode in (0o100644, 0o100640, 0o100604, 0o100666):
        assert "chmod 600" in config_mode_problem("/x/config.toml", mode=mode)


def test_short_pin_is_refused(web):
    for pin in ("1", "abc", "   ", " 12 "):
        with pytest.raises(ConfigError):
            create_app(web.env.svc, Config(scales=web.env.config.scales, settings=web.env.settings, pin=pin))


def test_server_ignores_forwarded_for_headers():
    from scaleconnect.web import __main__ as entry

    assert "proxy_headers=False" in inspect.getsource(entry.main)


def test_state_does_not_hand_out_scale_messages(web):
    scales = web.client.get("/api/state").json()["scales"]
    assert scales and all("last_message" not in s for s in scales)
    assert set(scales[0]) == {"name", "write_enabled", "read_enabled", "last_status", "interrupted_change_ids"}


# ---------------------------------------------------------------- the words for each result

def test_the_five_results_cannot_be_mistaken_for_each_other():
    good = words.result_heading("success", ["verified"], False)
    unchecked = words.result_heading("partial", ["accepted_not_verified"], False)
    failed = words.result_heading("failed", ["unreachable"], False)
    differ = words.result_heading("success", ["verified"], True)
    assert good == ("Done. Every scale was changed and checked.", "ok")
    assert unchecked[1] == "warn" and "NOT checked" in unchecked[0]
    assert failed[1] == "bad" and failed[0].startswith("FAILED")
    assert differ[1] == "warn" and "do not all hold the same values" in differ[0]
    assert len({good[0], unchecked[0], failed[0], differ[0]}) == 4
    mixed = words.result_heading("partial", ["verified", "unreachable"], False)
    assert mixed[1] == "bad" and "FAILED" in mixed[0]


def test_failed_never_says_nothing_changed_when_a_scale_may_hold_it():
    kept = words.result_heading("failed", ["rejected"], False)[0]
    assert "Nothing was changed" not in kept and "Nothing was sent" not in kept
    assert "Look at the scales" in kept
    assert "Nothing was sent" in words.result_heading("failed", ["unreachable", "read_failed"], False)[0]


def test_different_product_row_says_call_bart(env):
    from scaleconnect.models import Drift

    d = Drift("A", "id_occupied", 1, None, {}, None, "A holds something here.", None)
    row = words.scale_row(ScaleResult("A", "drift_refused", "x", drift=(d,)))
    assert "different product" in row["text"] and "Call the Scale Manager" in row["text"]
    assert row["severity"] == "bad"


def test_unchecked_row_wording_stays():
    row = words.scale_row(ScaleResult("FRONT", "accepted_not_verified", ""))
    assert row["text"] == "FRONT: accepted, but not checked. Look at the scale."


# ---------------------------------------------------------------- M4: scales still differ after a success

def test_success_with_a_scale_still_different_is_not_called_clean(make_web):
    web = make_web({"A": True, "B": True})
    hold(web.env.fakes["B"], item(1, "1001", "Test Cheddar", "15.00"))
    web.login()
    p = web.preview([{"action": "edit", "target_plu": "1001", "description": "Cheddar Aged"}], read=True).json()
    assert [d["scale"] for d in p["drift"]] == ["B"]
    tokens = [d["ack_token"] for d in p["drift"]]
    assert p["drift"][0]["can_ack"] is True and p["drift"][0]["different_product"] is False
    assert web.send(p["preview_id"], drift=tokens).status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert done["outcome"] in ("success", "partial")  # the library now says partial
    assert done["success"] is False
    assert done["severity"] == "warn"
    assert "do not all hold the same values" in done["heading"]
    assert not done["heading"].startswith("Done. Every scale")
    assert any("B still holds a different price" in n and "$15.00" in n and "$10.00" in n for n in done["notes"])
    assert web.env.fakes["B"].item(1)[4] == "15.00"


# ---------------------------------------------------------------- M2: different product, and a read that found nothing

def test_item_page_says_different_product_not_matches(web):
    web.env.catalog.record_scale_reads("A", [item(1, "9999", "Test Cheddar", "10.00")], "2026-01-02T00:00:00")
    d = web.client.get("/api/item/1001").json()
    row = d["scales"][0]
    assert row["flag"] == "different_product" and "plu" in row["differs"] and row["can_fix"] is True  # Bart may settle it
    assert "DIFFERENT PRODUCT" in row["text"] and "Call the Scale Manager" in row["text"]
    assert row["last_read"]["plu"] == "9999"


def test_item_page_notices_a_difference_in_any_field(web):
    web.env.catalog.record_scale_reads(
        "A", [item(1, "1001", "Test Cheddar", "10.00", fields_7_12=("0", "1", "0", "0", "0", "0"))], "2026-01-02T00:00:00")
    row = web.client.get("/api/item/1001").json()["scales"][0]
    assert row["flag"] == "different" and "fields_7_12" in row["differs"] and row["can_fix"] is True
    assert "settings" in row["text"]


def test_item_page_says_when_the_read_found_nothing(web):
    web.env.catalog.record_scale_reads("A", [item(2, "1002", "Test Brie", "20.00")], "2026-01-02T00:00:00", covered_up_to=3)
    row = web.client.get("/api/item/1001").json()["scales"][0]
    assert row["flag"] == "missing" and "found nothing" in row["text"] and "Call the Scale Manager" in row["text"]
    assert row["last_read"] is None and row["last_read_at"]

def test_matching_record_still_says_matches(web):
    web.env.catalog.record_scale_reads("A", [web.env.catalog.get_by_id(1)], "2026-01-02T00:00:00")
    assert web.client.get("/api/item/1001").json()["scales"][0]["flag"] == "same"


# ---------------------------------------------------------------- the page will not force a different product

def test_a_different_product_cannot_be_forced_from_the_page(web):
    hold(web.env.fakes["A"], item(1, "9999", "Renamed In ScaleLink", "10.00"))
    web.login()
    p = web.preview(EDIT, read=True).json()
    drift = p["drift"][0]
    assert drift["different_product"] is True and drift["can_ack"] is False and drift["ack_token"] is None
    r = web.send(p["preview_id"])
    assert r.status_code == 200
    done = web.wait_finished(p["preview_id"])
    row = done["rows"][0]
    assert row["status"] == "drift_refused" and "Call the Scale Manager" in row["text"]
    assert done["success"] is False and done["severity"] == "bad"
    assert web.env.fakes["A"].item(1)[2] == "9999" and web.env.fakes["A"].item(1)[4] == "10.00"
    assert web.env.writes("A") == []


# ---------------------------------------------------------------- M5: settle drift and mark orphans from the page

def test_settle_needs_the_pin(web):
    r = web.post("/api/settle/preview", {"scale": "A", "take": "scale", "plu": "1001"}, csrf=None)
    assert r.status_code == 401
    assert web.env.silent("A")


def test_settle_by_putting_the_catalog_value_on_the_scale(web):
    hold(web.env.fakes["A"], item(1, "1001", "Test Cheddar", "9.00"))
    web.login()
    p = web.post("/api/settle/preview", {"scale": "A", "take": "catalog", "plu": "1001"}).json()
    assert p["ok"] and p["differences"][0]["action"] == "settle_push"
    token = p["drift"][0]["ack_token"]
    assert p["drift"][0]["can_ack"]
    ack = any(w["requires_ack"] for w in p["warnings"])
    assert web.send(p["preview_id"], drift=[token], warnings=ack).status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert done["rows"][0]["status"] == "verified"
    assert web.env.fakes["A"].item(1)[4] == "10.00"
    assert web.env.catalog.get_by_id(1).price == "10.00"


def test_settle_by_copying_the_scale_into_the_list_writes_nothing(web):
    hold(web.env.fakes["A"], item(1, "1001", "Test Cheddar", "9.00"))
    web.login()
    p = web.post("/api/settle/preview", {"scale": "A", "take": "scale", "plu": "1001"}).json()
    assert p["ok"] and p["differences"][0]["action"] == "settle_accept"
    ack = any(w["requires_ack"] for w in p["warnings"])
    assert web.send(p["preview_id"], drift=[p["drift"][0]["ack_token"]], warnings=ack).status_code == 200
    web.wait_finished(p["preview_id"])
    assert web.env.catalog.get_by_id(1).price == "9.00"
    assert web.env.writes("A") == []
    assert web.env.fakes["A"].item(1)[4] == "9.00"


def test_settle_says_plainly_when_there_is_nothing_to_settle_or_the_choice_is_bad(web):
    web.login()
    p = web.post("/api/settle/preview", {"scale": "A", "take": "catalog", "plu": "1001"}).json()
    assert not p["ok"] and p["errors"][0]["code"] == "no_drift"
    p = web.post("/api/settle/preview", {"scale": "A", "take": "sideways", "plu": "1001"}).json()
    assert not p["ok"]
    p = web.post("/api/settle/preview", {"scale": "NOPE", "take": "scale", "plu": "1001"}).json()
    assert not p["ok"] and p["errors"][0]["code"] == "unknown_scale"


def test_mark_orphan_needs_pin_and_a_real_scale(web):
    assert web.post("/api/orphan", {"scale": "A", "id": 485}, csrf=None).status_code == 401
    web.login()
    assert web.post("/api/orphan", {"scale": "NOPE", "id": 485}).status_code == 400
    assert web.post("/api/orphan", {"scale": "A", "id": 0}).status_code == 422
    assert web.post("/api/orphan", {"scale": "A", "id": 501}).status_code == 422
    r = web.post("/api/orphan", {"scale": "A", "id": 485, "note": "old test item\n"})
    assert r.status_code == 200 and r.json()["ok"]
    assert 485 in web.env.catalog.orphan_ids()


def test_an_add_can_land_on_a_marked_id(web):
    hold(web.env.fakes["A"], item(485, "8888", "Old Test Item", "1.00"))
    web.login()
    plain = web.preview([{"action": "add", "plu": "2001", "description": "New Cheese", "price": "7.25"}], read=True).json()
    assert plain["ok"]  # the lowest free id is used, and the old item is left alone
    web.post("/api/orphan", {"scale": "A", "id": 485, "note": "old test item"})
    p = web.preview([{"action": "add", "plu": "2001", "description": "New Cheese", "price": "7.25",
                      "replace_orphan_id": 485}], read=True).json()
    assert p["ok"], p["errors"]
    assert any(w["code"] == "orphan_replaced" and w["requires_ack"] for w in p["warnings"])
    assert web.send(p["preview_id"], warnings=True).status_code == 200
    done = web.wait_finished(p["preview_id"])
    assert done["rows"][0]["status"] == "verified", done["rows"]
    assert web.env.fakes["A"].item(485)[2] == "2001"


def test_orphan_id_from_the_page_must_be_a_whole_number(web):
    web.login()
    for bad in ("485", 485.5, True, 0, 501):
        p = web.preview([{"action": "add", "plu": "2001", "description": "X", "price": "1.00", "replace_orphan_id": bad}]).json()
        assert not p["ok"], bad


# ---------------------------------------------------------------- M9: stopped and running changes

def _dead_change(env, preview_id="deadpreview", minutes_old=60):
    """A change with a start and no finish, as after a crash."""
    change_id = uuid.uuid4().hex
    now = datetime.now().astimezone()
    started = (now - timedelta(minutes=minutes_old)).isoformat(timespec="seconds")
    env.catalog.save_preview(preview_id, {"x": 1}, started, (now + timedelta(hours=1)).isoformat(timespec="seconds"))
    assert env.catalog.consume_preview(preview_id, change_id)
    diff = {"index": 0, "action": "edit", "item_id": 1, "plu": "1001", "fields": {}, "before": None, "after": None}
    env.catalog.append_log(change_id, "started", {
        "preview_id": preview_id, "requested_by": "mcp:claude", "acknowledged_warnings": False,
        "started_at": started, "scales": ["A"], "not_touched": [], "changes": [diff],
    }, started)
    return change_id


def test_a_dead_change_does_not_say_working_forever(web):
    cid = _dead_change(web.env)
    d = web.client.get("/api/send/deadpreview").json()
    assert d["state"] == "stalled" and d["severity"] == "bad"
    assert "STOPPED" in d["heading"] and "Working" not in d["heading"]
    assert d["change_id"] == cid and d["rows"][0]["status"] == "no_result"
    recent = web.client.get("/api/changes").json()["changes"]
    assert recent[0]["heading"].startswith("STOPPED") and recent[0]["severity"] == "bad"


def test_a_change_another_program_is_sending_is_working_not_stopped(web):
    from scaleconnect.service import _apply_lock

    _dead_change(web.env, minutes_old=1)
    with _apply_lock(web.env.svc._lock_path):  # what the MCP server holds while it sends
        d = web.client.get("/api/send/deadpreview").json()
        assert d["state"] == "running" and "STOPPED" not in d["heading"]
    d = web.client.get("/api/send/deadpreview").json()  # the lock is free now, so it died
    assert d["state"] == "stalled"

def test_a_stopped_change_can_be_closed_by_bart_after_looking(web):
    cid = _dead_change(web.env)
    assert web.post("/api/resolve", {"change_id": cid, "note": "looked, FRONT is fine"}, csrf=None).status_code == 401
    web.login()
    assert web.post("/api/resolve", {"change_id": cid, "note": "x"}).status_code == 422
    assert web.post("/api/resolve", {"change_id": "nope", "note": "looked at it"}).status_code == 404
    r = web.post("/api/resolve", {"change_id": cid, "note": "looked, FRONT is fine"})
    assert r.status_code == 200 and r.json()["outcome"] == "interrupted"
    d = web.client.get("/api/send/deadpreview").json()
    assert d["state"] == "finished" and "never finished" in d["heading"]


def test_a_running_change_is_not_called_interrupted_or_stopped(make_web):
    web = make_web({"A": True}, web_kw={"early_wait_seconds": 0.2, "stalled_after_seconds": 0.0})
    gate, entered = threading.Event(), threading.Event()
    real = web.env.svc._apply_to_scale

    def slow(scale, *a, **k):
        entered.set()
        assert gate.wait(20)
        return real(scale, *a, **k)

    web.env.svc._apply_to_scale = slow
    web.login()
    p = web.preview(EDIT).json()
    assert web.send(p["preview_id"]).status_code == 200
    assert entered.wait(20)
    d = web.client.get(f"/api/send/{p['preview_id']}").json()
    assert d["state"] == "running" and "STOPPED" not in d["heading"]
    page = web.client.get("/api/item/1001").json()
    assert page["sending_now"] is True
    gate.set()
    assert web.wait_finished(p["preview_id"])["state"] == "finished"
    assert web.client.get("/api/item/1001").json()["sending_now"] is False


def test_a_send_is_not_called_finished_while_the_job_still_counts_as_busy(make_web):
    """The library logs the finish before the job ends. A client told finished must not then see sending_now."""
    web = make_web({"A": True})
    gate, returned = threading.Event(), threading.Event()
    real = web.env.svc.apply_change

    def slow_to_return(*a, **k):
        record = real(*a, **k)
        returned.set()
        assert gate.wait(20)
        return record

    web.env.svc.apply_change = slow_to_return
    web.login()
    p = web.preview(EDIT).json()
    assert web.send(p["preview_id"]).status_code == 200
    assert returned.wait(20)
    assert web.env.catalog.load_preview(p["preview_id"])["consumed_by"]
    d = web.client.get(f"/api/send/{p['preview_id']}").json()
    assert d["state"] == "running" and d["success"] is False and d["outcome"] == "in_progress"
    assert web.client.get("/api/item/1001").json()["sending_now"] is True
    gate.set()
    d = web.wait_finished(p["preview_id"])
    assert d["state"] == "finished" and d["success"] is True
    assert web.client.get("/api/item/1001").json()["sending_now"] is False


def test_settle_and_orphan_screens_are_reachable_from_the_page():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src/scaleconnect/web/static/app.js").read_text(encoding="utf-8")
    for needle in ("renderSettle", "renderOrphans", "resolveForm", "/api/settle/preview", "/api/orphan", "/api/resolve",
                   "Call the Scale Manager", "acked = {}"):
        assert needle in source, needle


def test_check_now_reads_the_scale_so_the_item_page_can_flag_a_difference(web):
    hold(web.env.fakes["A"], item(1, "1001", "Test Cheddar", "9.00"))
    assert web.post("/api/check", {"plu": "1001"}, csrf=None).status_code == 401
    assert web.client.get("/api/item/1001").json()["scales"][0]["flag"] == "none"
    web.login()
    r = web.post("/api/check", {"plu": "1001"})
    assert r.status_code == 200 and r.json()["scales"][0]["status"] == "read"
    row = web.client.get("/api/item/1001").json()["scales"][0]
    assert row["flag"] == "different" and row["differs"] == ["price"] and row["can_fix"] is True
    assert web.env.writes("A") == []

def test_bart_can_settle_a_different_product_from_the_settle_screen_only(web):
    hold(web.env.fakes["A"], item(1, "9999", "Renamed In ScaleLink", "10.00"))
    web.login()
    p = web.post("/api/settle/preview", {"scale": "A", "take": "scale", "plu": "1001"}).json()
    assert p["ok"], p["errors"]
    d = p["drift"][0]
    assert d["different_product"] is True and d["can_ack"] is True and d["ack_token"]
    ack = any(w["requires_ack"] for w in p["warnings"])
    assert web.send(p["preview_id"], drift=[d["ack_token"]], warnings=ack).status_code == 200
    web.wait_finished(p["preview_id"])
    assert web.env.catalog.get_by_id(1).plu == "9999"
    assert web.env.writes("A") == []