"""driver-dev, round 3. Localhost and the fake scale only. Made up data only."""

from __future__ import annotations

import json
import threading
import time
import tomllib
from pathlib import Path

import pytest

from scaleconnect import cli
from scaleconnect.catalog import Catalog, apply_lock
from scaleconnect.config import Config, ScaleConfig, Settings, connect_retry_span_seconds, load_config
from scaleconnect.errors import ApplyBusy, ValidationFailed
from scaleconnect.mcp_server import configured_caller
from scaleconnect.models import Item, ScaleStatus
from scaleconnect.scale import ScaleClient
from scaleconnect.seed import seed_catalog
from scaleconnect.service import ScaleConnectService

ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-01-01 00:00:00"


def seed_items():
    return [
        Item(1, "1001", "1001", "Test Alpha", "10.00", field6=" ", timestamp=STAMP),
        Item(2, "1002", "1002", "Test Beta", "5.50", timestamp=STAMP),
        Item(3, "1004", "1004", "Test Gamma", "2.00", timestamp=STAMP),
    ]


def conf(fake, name="A", enabled=True, **kw):
    return ScaleConfig(name, "127.0.0.1", fake.port, enabled, serial=fake.serial, **kw)


def fast(tmp_path, **kw):
    base = dict(
        reply_timeout_seconds=0.5,
        connect_timeout_seconds=0.5,
        connect_attempts=2,
        connect_retry_wait_seconds=0.01,
        read_pace_seconds=0.0,
        read_back_window_seconds=3.0,
        read_back_retry_seconds=0.05,
        state_dir=tmp_path,
        scalelink_db_copy="not_available_here",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def make_service(tmp_path):
    made = []

    def _make(scales, settings=None, **kw):
        settings = settings or fast(tmp_path)
        catalog = Catalog(settings.catalog_path)
        if not catalog.is_seeded():
            catalog.import_items(seed_items())
        service = ScaleConnectService(Config(scales=tuple(scales), settings=settings), catalog, **kw)
        made.append(service)
        return service

    yield _make
    for service in made:
        service.close()


def loaded(fake):
    fake.preload(*[tuple(i.to_fields()) for i in seed_items()])
    return fake


def edit(service, price="10.50", plu="1001", **kw):
    preview = service.preview_changes([{"action": "edit", "target_plu": plu, "price": price}], requested_by="test", **kw)
    assert preview.ok, preview.errors
    return service.apply_change(preview.preview_id)


# ---------------------------------------------------------------- the busy gap, in real time


def test_default_settings_cover_the_gap_in_real_time_if_refusals_come_at_once():
    settings = Settings()
    assert connect_retry_span_seconds(settings) >= 60
    assert settings.read_back_window_seconds >= 30


def test_a_config_that_gives_up_too_soon_says_so(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[settings]\nconnect_attempts = 4\nconnect_retry_wait_seconds = 0.5\nread_back_window_seconds = 5\n", encoding="utf-8")
    warnings = " ".join(load_config(path).warnings)
    assert "Connect retries cover under 15 seconds" in warnings
    assert "read back window is under 15 seconds" in warnings


def _written(make_fake, mode, gap, tmp_path, **kw):
    fake = make_fake(post_write_refusal_seconds=gap, post_write_mode=mode)
    settings = Settings(state_dir=tmp_path, read_pace_seconds=0.0, **kw)
    report = ScaleClient(conf(fake), settings).send_items(seed_items()[:1])
    assert report.status is ScaleStatus.ACCEPTED_NOT_VERIFIED
    return fake, settings


@pytest.mark.parametrize("mode", ["refuse", "reset", "silent"])
def test_default_read_back_outlasts_an_11_second_gap_with_real_waits(make_fake, tmp_path, mode):
    fake, settings = _written(make_fake, mode, 11.5, tmp_path)
    started = time.monotonic()
    result = ScaleClient(conf(fake), settings).read_items(1, patient=True)
    assert result.ok and result.items[1].price == "10.00"
    assert time.monotonic() - started >= 11.0


@pytest.mark.parametrize("mode", ["refuse", "reset"])
def test_default_connect_retries_outlast_an_11_second_gap_with_real_waits(make_fake, tmp_path, mode):
    fake, settings = _written(make_fake, mode, 11.5, tmp_path)
    assert ScaleClient(conf(fake), settings).identify().ok


def test_patient_read_on_a_new_client_checks_identity_through_the_gap(make_fake, tmp_path):
    fake = make_fake(post_write_refusal_seconds=0.5, serial="OTHER")
    cfg = ScaleConfig("A", "127.0.0.1", fake.port, True, serial="OTHER")
    assert ScaleClient(cfg, fast(tmp_path)).send_items(seed_items()[:1]).status is ScaleStatus.ACCEPTED_NOT_VERIFIED
    wrong = ScaleConfig("A", "127.0.0.1", fake.port, True, serial="EXPECTED")
    result = ScaleClient(wrong, fast(tmp_path)).read_items(1, patient=True)
    assert result.status is ScaleStatus.WRONG_SCALE
    assert "LP" not in fake.kinds()


# ---------------------------------------------------------------- 1. the connection leak


def test_short_threads_do_not_keep_connections(tmp_path):
    catalog = Catalog(tmp_path / "c.sqlite3")
    for _ in range(50):
        thread = threading.Thread(target=catalog.version)
        thread.start()
        thread.join()
    assert len(catalog._conns) <= 2
    assert catalog.version()
    catalog.close()


def test_threads_in_use_keep_working(tmp_path):
    catalog = Catalog(tmp_path / "c.sqlite3")
    catalog.import_items(seed_items())
    start, stop, seen = threading.Event(), threading.Event(), []

    def worker():
        seen.append(catalog.get_by_id(1).plu)
        start.set()
        stop.wait(5)
        seen.append(catalog.get_by_id(2).plu)

    alive = threading.Thread(target=worker)
    alive.start()
    start.wait(5)
    for _ in range(5):
        thread = threading.Thread(target=catalog.version)
        thread.start()
        thread.join()
    stop.set()
    alive.join()
    assert seen == ["1001", "1002"]
    catalog.close()


# ---------------------------------------------------------------- 2. naming scales


@pytest.mark.parametrize("call", ["compare_scales", "read_scale_items"])
def test_unknown_and_empty_scale_names_are_refused_with_no_contact(make_service, make_fake, call):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    run = (lambda s: service.compare_scales(s)) if call == "compare_scales" else (lambda s: service.read_scale_items(scales=s))
    for names, code in ((["A", "NOPE"], "unknown_scale"), ([], "no_scales")):
        with pytest.raises(ValidationFailed) as caught:
            run(names)
        assert caught.value.errors[0].code == code
    assert fake.nothing_received


# ---------------------------------------------------------------- 3. the read call


def test_read_scale_items_says_what_each_scale_holds(make_service, make_fake):
    good, odd, off = loaded(make_fake()), loaded(make_fake()), loaded(make_fake())
    odd.preload(tuple(seed_items()[0].replace(price="11.00").to_fields()))
    odd.preload(tuple(Item(9, "1002", "1002", "Stray", "1.00", timestamp=STAMP).to_fields()))
    odd.remove(3)
    service = make_service([conf(good, "GOOD"), conf(odd, "ODD"), conf(off, "OFF", enabled=False)])

    first, second, third = service.read_scale_items(plus=["1001", "1002"], ids=[3])
    assert first.status == "read" and first.complete
    assert [r.item.id for r in first.records] == [1, 2, 3] and all(r.matches_catalog for r in first.records)
    assert [r.item.id for r in second.records] == [1, 2, 9]
    assert second.records[0].difference.fields["price"].after == "11.00"
    assert second.records[0].catalog.price == "10.00"
    assert second.records[2].difference.kind == "not_in_catalog" and second.records[2].catalog is None
    assert third.status == "not_contacted" and off.nothing_received
    assert set(good.kinds()) <= {"CX", "LP", "Ok"}
    json.dumps(second.to_dict())


def test_read_by_id_alone_stops_early_and_everything_reads_it_all(make_service, make_fake):
    fake = make_fake()
    fake.preload(*[tuple(Item(i, f"{2000 + i}", f"{2000 + i}", f"T {i}", "1.00", timestamp=STAMP).to_fields()) for i in range(1, 21)])
    service = make_service([conf(fake)])
    part = service.read_scale_items(ids=[2])[0]
    assert not part.complete and [r.item.id for r in part.records] == [2]
    assert fake.kinds().count("Ok") == 0
    whole = service.read_scale_items()[0]
    assert whole.complete and len(whole.records) == 20


def test_read_refuses_bad_targets(make_service, make_fake):
    service = make_service([conf(make_fake())])
    with pytest.raises(ValidationFailed):
        service.read_scale_items(plus=[1001])
    with pytest.raises(ValidationFailed):
        service.read_scale_items(ids=["1"])


def test_read_waits_its_turn(make_service, make_fake, tmp_path):
    service = make_service([conf(loaded(make_fake()))])
    with apply_lock(tmp_path / "apply.lock"):
        with pytest.raises(ApplyBusy):
            service.read_scale_items()


# ---------------------------------------------------------------- 4. last read


def test_last_read_follows_a_scale_that_lost_the_id(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    service.compare_scales()
    assert service.get_item(plu="1002").scales["A"].last_read.price == "5.50"
    fake.remove(2)
    service.compare_scales()
    state = service.get_item(plu="1002").scales["A"]
    assert state.last_read is None and state.last_read_at is not None
    assert state.to_dict()["last_read"] is None


def test_an_early_stop_says_nothing_about_ids_past_it(make_service, make_fake):
    fake = make_fake()
    fake.preload(*[tuple(i.to_fields()) for i in seed_items()])
    fake.preload(*[tuple(Item(i, f"{2000 + i}", f"{2000 + i}", f"T {i}", "1.00", timestamp=STAMP).to_fields()) for i in range(4, 13)])
    service = make_service([conf(fake)])
    service.read_scale_items()
    fake.remove(12)
    service.read_scale_items(ids=[1])  # a block reaches id 5
    catalog = service._catalog
    assert catalog.last_reads(12)["A"]["record"].plu == "2012"
    assert catalog.last_reads(5)["A"]["record"].plu == "2005"


def test_a_catalog_id_never_seen_on_a_scale_records_the_read(make_service, make_fake):
    fake = make_fake()
    fake.preload(tuple(seed_items()[0].to_fields()))
    service = make_service([conf(fake)])
    service.compare_scales()
    state = service.get_item(plu="1004").scales["A"]
    assert state.last_read is None and state.last_read_at


# ---------------------------------------------------------------- 5. the message agrees


def test_message_agrees_with_a_final_rejected(make_service, make_fake):
    fake = loaded(make_fake())
    fake.error_on_ids(1)
    service = make_service([conf(fake)])
    preview = service.preview_changes(
        [{"action": "edit", "target_plu": "1001", "price": "10.50"}, {"action": "edit", "target_plu": "1002", "price": "5.60"}]
    )
    result = service.apply_change(preview.preview_id).scale_results[0]
    assert result.status == "rejected"
    assert "unknown" not in result.message.lower() and "Error" in result.message


@pytest.mark.parametrize("fault,status,word", [("clean", "verified", "confirmed"), ("stall", "unknown", "unknown")])
def test_message_agrees_with_other_final_statuses(make_service, make_fake, fault, status, word):
    fake = loaded(make_fake())
    if fault == "stall":
        fake.stall("GP")
    result = edit(make_service([conf(fake)])).scale_results[0]
    assert result.status == status and word in result.message


# ---------------------------------------------------------------- 6. mcp_caller


def test_mcp_caller_comes_through_config(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[auth]\nmcp_caller = "  invoice-desk "\n', encoding="utf-8")
    config = load_config(path)
    assert config.mcp_caller == "invoice-desk" and configured_caller(config) == "invoice-desk"
    path.write_text('[auth]\nmcp_caller = "bad\\nname"\n', encoding="utf-8")
    with pytest.raises(Exception) as caught:
        load_config(path)
    assert caught.value.code == "config_error" and "mcp_caller" in caught.value.message
    path.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert load_config(path).mcp_caller is None
    assert "mcp_caller" in (ROOT / "config.example.toml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 7. package data


def test_pyproject_ships_the_web_page():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["setuptools"]["package-data"]["scaleconnect.web"] == ["static/*"]
    assert (ROOT / "src" / "scaleconnect" / "web" / "__init__.py").exists()
    assert (ROOT / "src" / "scaleconnect" / "web" / "static" / "index.html").exists()


# ---------------------------------------------------------------- 9. settling drift


def drifted(make_fake, **fields):
    fake = loaded(make_fake())
    fake.preload(tuple(seed_items()[0].replace(timestamp="2026-02-02 02:02:02", **fields).to_fields()))
    return fake


def test_take_the_scale_updates_only_the_catalog_and_clears_the_drift(make_service, make_fake):
    fake = drifted(make_fake, price="11.00")
    service = make_service([conf(fake)])
    preview = service.preview_settle_drift(scale="A", take="scale", plu="1001", requested_by="test")
    assert preview.ok, preview.errors
    assert preview.scales_read == ("A",)
    assert preview.differences[0].action == "settle_accept"
    assert preview.differences[0].fields["price"].before == "10.00" and preview.differences[0].fields["price"].after == "11.00"
    token = preview.drift[0].ack_token

    # Without naming it, nothing changes.
    record = service.apply_change(preview.preview_id)
    assert record.scale_results[0].status == "drift_refused"
    assert service.get_item(plu="1001").item.price == "10.00"

    preview = service.preview_settle_drift(scale="A", take="scale", plu="1001", requested_by="test")
    record = service.apply_change(preview.preview_id, acknowledge_drift=[token])
    result = record.scale_results[0]
    assert result.status == "verified" and record.success and record.verified
    assert not fake.messages_of("GP") and "RI" not in fake.kinds()
    item = service.get_item(plu="1001").item
    assert item.to_fields() == list(fake.item(1))
    assert service.compare_scales()[0].differences == ()
    logged = service.get_change(record.change_id)
    assert logged.changes[0].action == "settle_accept" and logged.outcome == "success"
    assert edit(service, "11.50").scale_results[0].status == "verified"


def test_take_the_catalog_writes_through_the_seven_steps_and_clears_the_drift(make_service, make_fake):
    fake = drifted(make_fake, description="Scale Name")
    service = make_service([conf(fake)])
    preview = service.preview_settle_drift(scale="A", take="catalog", id=1, requested_by="test")
    assert preview.ok, preview.errors
    assert preview.differences[0].fields["description"].before == "Scale Name"
    assert preview.differences[0].fields["description"].after == "Test Alpha"
    version = service._catalog.version()
    record = service.apply_change(preview.preview_id, acknowledge_drift=[preview.drift[0].ack_token])
    assert record.scale_results[0].status == "verified" and record.success
    kinds = fake.kinds()
    assert kinds[0] == "CX" and "RI" in kinds and kinds.count("GP") == 1 and "GPF0" in kinds
    assert list(fake.item(1)) == service.get_item(plu="1001").item.to_fields()
    assert service._catalog.version() == version
    assert service.compare_scales()[0].differences == ()
    assert service.get_item(plu="1001").scales["A"].belief == "present"


def test_settling_one_scale_leaves_the_others_and_needs_acknowledging(make_service, make_fake):
    a, b = drifted(make_fake, price="11.00"), loaded(make_fake())
    service = make_service([conf(a, "A"), conf(b, "B")])
    preview = service.preview_settle_drift(scale="A", take="scale", plu="1001")
    assert {"scale_not_targeted", "other_scales_may_differ"} <= {w.code for w in preview.warnings}
    token = preview.drift[0].ack_token
    with pytest.raises(Exception) as caught:
        service.apply_change(preview.preview_id, acknowledge_drift=[token])
    assert caught.value.code == "acknowledgement_required"
    record = service.apply_change(preview.preview_id, True, acknowledge_drift=[token])
    assert record.outcome == "partial" and record.not_touched == ("B",) and not record.success
    assert b.nothing_received


def test_settle_refuses_if_the_scale_changed_after_the_preview(make_service, make_fake):
    fake = drifted(make_fake, price="11.00")
    service = make_service([conf(fake)])
    preview = service.preview_settle_drift(scale="A", take="scale", plu="1001")
    token = preview.drift[0].ack_token
    fake.preload(tuple(seed_items()[0].replace(price="12.00").to_fields()))
    record = service.apply_change(preview.preview_id, acknowledge_drift=[token])
    assert record.scale_results[0].status == "drift_refused"
    assert service.get_item(plu="1001").item.price == "10.00"


@pytest.mark.parametrize(
    "kw,code",
    [
        (dict(scale="A", take="sideways", plu="1001"), "invalid_change"),
        (dict(scale="A", take="scale"), "invalid_change"),
        (dict(scale="A", take="scale", plu="1001", id=1), "invalid_change"),
        (dict(scale="NOPE", take="scale", plu="1001"), "unknown_scale"),
        (dict(scale="A", take="scale", plu="9999"), "item_not_found"),
    ],
)
def test_settle_bad_input_contacts_nothing(make_service, make_fake, kw, code):
    fake = loaded(make_fake())
    preview = make_service([conf(fake)]).preview_settle_drift(**kw)
    assert not preview.ok and preview.errors[0].code == code
    assert fake.nothing_received


def test_settle_with_no_drift_says_so(make_service, make_fake):
    preview = make_service([conf(loaded(make_fake()))]).preview_settle_drift(scale="A", take="catalog", plu="1001")
    assert preview.errors[0].code == "no_drift"


def test_a_value_the_scale_holds_that_breaks_the_rules_cannot_enter_the_catalog(make_service, make_fake):
    fake = drifted(make_fake, price="1000.00")
    preview = make_service([conf(fake)]).preview_settle_drift(scale="A", take="scale", plu="1001")
    assert preview.errors[0].code == "invalid_price" and preview.drift


def test_a_plu_held_by_another_catalog_item_cannot_enter_the_catalog(make_service, make_fake):
    fake = drifted(make_fake, plu="1002", code="1002")
    preview = make_service([conf(fake)]).preview_settle_drift(scale="A", take="scale", id=1)
    assert preview.errors[0].code == "duplicate_plu"


def test_pushing_cannot_change_a_plu_on_a_scale_while_that_is_on_hold(make_service, make_fake):
    fake = drifted(make_fake, plu="1999", code="1999")
    service = make_service([conf(fake)])
    assert service.preview_settle_drift(scale="A", take="catalog", id=1).errors[0].code == "plu_edit_held"
    assert not fake.messages_of("GP")


def test_an_empty_id_can_be_pushed_but_not_taken(make_service, make_fake):
    fake = loaded(make_fake())
    fake.remove(2)
    service = make_service([conf(fake)])
    assert service.preview_settle_drift(scale="A", take="scale", plu="1002").errors[0].code == "settle_not_possible"
    preview = service.preview_settle_drift(scale="A", take="catalog", plu="1002")
    record = service.apply_change(preview.preview_id, acknowledge_drift=[preview.drift[0].ack_token])
    assert record.scale_results[0].status == "verified" and fake.item(2)[2] == "1002"


def test_write_flag_rules_for_settling(make_service, make_fake):
    fake = drifted(make_fake, price="11.00")
    reader = conf(fake, enabled=False, read_enabled=True)
    service = make_service([reader])
    assert service.preview_settle_drift(scale="A", take="catalog", plu="1001").errors[0].code == "scale_not_write_enabled"
    assert fake.nothing_received
    preview = service.preview_settle_drift(scale="A", take="scale", plu="1001")
    record = service.apply_change(preview.preview_id, acknowledge_drift=[preview.drift[0].ack_token])
    assert record.scale_results[0].status == "verified"
    assert service.get_item(plu="1001").item.price == "11.00"
    assert not fake.messages_of("GP")


def test_large_price_gap_needs_acknowledging_when_settling(make_service, make_fake):
    fake = drifted(make_fake, price="99.00")
    service = make_service([conf(fake)])
    preview = service.preview_settle_drift(scale="A", take="scale", plu="1001")
    assert any(w.code == "price_change_large" and w.requires_ack for w in preview.warnings)


def test_cli_read_and_settle(tmp_path, make_fake, capsys):
    fake = drifted(make_fake, price="11.00")
    path = tmp_path / "config.toml"
    path.write_text(
        f'[settings]\nstate_dir = "{tmp_path.as_posix()}"\nread_pace_seconds = 0\nconnect_attempts = 1\n'
        f'scalelink_db_copy = "not_available_here"\n\n[[scales]]\nname = "A"\naddress = "127.0.0.1"\n'
        f'port = {fake.port}\nserial = "{fake.serial}"\nwrite_enabled = true\n',
        encoding="utf-8",
    )
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.import_items(seed_items())

    def run(*args):
        code = cli.main(["--config", str(path), *args])
        return code, json.loads(capsys.readouterr().out)

    code, out = run("read", "--plu", "1001")
    assert code == 0 and out[0]["records"][0]["matches_catalog"] is False
    code, out = run("compare", "--scale", "NOPE")
    assert code == 3
    code, out = run("settle", "--plu", "1001", "--scale", "A", "--take", "scale")
    assert code == 0 and out["ok"]
    code, out = run("apply", out["preview_id"], "--acknowledge-drift", out["drift"][0]["ack_token"])
    assert code == 0 and out["verified"]
    code, out = run("settle", "--plu", "1001", "--scale", "A", "--take", "scale")
    assert code == 3 and out["errors"][0]["code"] == "no_drift"


# ---------------------------------------------------------------- L8, L13 and the removed settings


def test_believed_on_is_not_present_until_a_read_shows_it(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"items": [i.to_dict() for i in seed_items()]}), encoding="utf-8")
    db = tmp_path / "c.sqlite3"
    seed_catalog(db, seed, believed_on=("A",))
    with Catalog(db) as catalog:
        state = catalog.scale_states(1)["A"]
    assert state["belief"] == "unknown" and state["status"] == "seed"


def test_overwrite_refuses_during_a_change_and_keeps_the_old_catalog(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"items": [i.to_dict() for i in seed_items()]}), encoding="utf-8")
    db = tmp_path / "c.sqlite3"
    seed_catalog(db, seed)
    with apply_lock(tmp_path / "apply.lock"):
        with pytest.raises(ApplyBusy):
            seed_catalog(db, seed, overwrite=True)
    assert db.exists() and not list(tmp_path.glob("*.old-*"))
    assert seed_catalog(db, seed, overwrite=True)["previous_catalog"]


def test_failed_overwrite_puts_the_side_files_back(tmp_path, monkeypatch):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"items": [i.to_dict() for i in seed_items()]}), encoding="utf-8")
    db = tmp_path / "c.sqlite3"
    seed_catalog(db, seed)
    side = db.with_name(db.name + "-wal")
    side.write_bytes(b"old side file")

    def boom(self, items):
        raise RuntimeError("no")

    monkeypatch.setattr(Catalog, "import_items", boom)
    with pytest.raises(RuntimeError):
        seed_catalog(db, seed, overwrite=True)
    assert db.exists() and side.read_bytes() == b"old side file"
    assert not list(tmp_path.glob("*.old-*"))


@pytest.mark.parametrize("plu", ["0123", "0000", "0999"])
def test_a_leading_zero_plu_is_refused(make_service, make_fake, plu):
    fake = loaded(make_fake())
    preview = make_service([conf(fake)]).preview_changes([{"action": "add", "plu": plu, "description": "X", "price": "1.00"}])
    assert not preview.ok and "invalid_plu" in {e.code for e in preview.errors}
    assert fake.nothing_received


def test_removed_settings_are_gone_and_an_old_config_still_loads(tmp_path):
    with pytest.raises(TypeError):
        Settings(unknown_retries=1)
    path = tmp_path / "c.toml"
    path.write_text("[settings]\nunknown_retries = 1\nunknown_retry_wait_seconds = 5\n", encoding="utf-8")
    assert sum("no longer used" in w for w in load_config(path).warnings) == 2
