"""driver-dev, round 4: the stage F review's library findings. Localhost and the fake scale only."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from scaleconnect import protocol
from scaleconnect.catalog import Catalog, IdInUseError, apply_lock
from scaleconnect.config import Config, ConfigError, ScaleConfig, Settings, listen_problem, load_config
from scaleconnect.errors import ApplyBusy, ValidationFailed
from scaleconnect.models import Item, ScaleStatus
from scaleconnect.service import ScaleConnectService

STAMP = "2026-01-01 00:00:00"


def items(count=3):
    return [Item(i, f"{1000 + i}", f"{1000 + i}", f"Test {i}", "10.00", timestamp=STAMP) for i in range(1, count + 1)]


def conf(fake, name="A", enabled=True, **kw):
    return ScaleConfig(name, "127.0.0.1", fake.port, enabled, serial=fake.serial, **kw)


def fast(tmp_path, **kw):
    base = dict(
        reply_timeout_seconds=0.5,
        connect_timeout_seconds=0.5,
        connect_attempts=2,
        connect_retry_wait_seconds=0.01,
        read_pace_seconds=0.0,
        read_back_window_seconds=1.0,
        read_back_retry_seconds=0.05,
        state_dir=tmp_path,
        scalelink_db_copy="not_available_here",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def make_service(tmp_path):
    made = []

    def _make(scales, count=3, settings=None, **kw):
        settings = settings or fast(tmp_path)
        catalog = Catalog(settings.catalog_path)
        if not catalog.is_seeded():
            catalog.import_items(items(count))
        service = ScaleConnectService(Config(scales=tuple(scales), settings=settings), catalog, **kw)
        made.append(service)
        return service

    yield _make
    for service in made:
        service.close()


def loaded(fake, count=3):
    fake.preload(*[tuple(i.to_fields()) for i in items(count)])
    return fake


def edit_with(service, tokens=(), ack=False, **fields):
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", **fields}], requested_by="test")
    assert preview.ok, preview.errors
    return service.apply_change(preview.preview_id, ack, acknowledge_drift=list(tokens))


def other_product(**kw):
    base = dict(plu="9999", code="9999", description="Renamed In ScaleLink", price="10.00", timestamp="2026-02-02 02:02:02")
    base.update(kw)
    return items()[0].replace(**base)


# ---------------------------------------------------------------- H1


@pytest.mark.parametrize("fields", [dict(plu="9999", code="9999"), dict(plu="9999", code="1001"), dict(plu="1001", code="9999")])
def test_H1_a_different_product_is_refused_and_no_acknowledgement_helps(make_service, make_fake, fields):
    fake = loaded(make_fake())
    held = other_product(**fields)
    fake.preload(tuple(held.to_fields()))
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "11.00"}], read_scales=True)
    assert preview.ok
    finding = preview.drift[0]
    assert finding.kind == "different_product" and finding.ack_token is None

    from scaleconnect.service import _drift_token

    forged = _drift_token("A", 1, held)
    record = service.apply_change(preview.preview_id, True, acknowledge_drift=[forged, "A:1:anything"])
    assert record.scale_results[0].status == "drift_refused" and not record.success
    assert record.scale_results[0].drift[0].kind == "different_product"
    assert not fake.messages_of("GP")
    assert fake.item(1) == tuple(held.to_fields())
    assert service.get_item(plu="1001").item.price == "10.00"


def test_H1_only_settling_resolves_a_different_product(make_service, make_fake):
    fake = loaded(make_fake())
    held = other_product()
    fake.preload(tuple(held.to_fields()))
    service = make_service([conf(fake)])
    assert service.preview_settle_drift(scale="A", take="catalog", plu="1001").errors[0].code == "plu_edit_held"
    preview = service.preview_settle_drift(scale="A", take="scale", plu="1001")
    assert preview.ok and preview.drift[0].kind == "different_product" and preview.drift[0].ack_token
    record = service.apply_change(preview.preview_id, acknowledge_drift=[preview.drift[0].ack_token])
    assert record.scale_results[0].status == "verified"
    assert service.get_item(plu="9999").item.id == 1
    assert not fake.messages_of("GP")


def test_H1_field_drift_on_the_same_product_can_still_be_acknowledged(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(tuple(items()[0].replace(price="11.00").to_fields()))
    service = make_service([conf(fake)])
    token = service.preview_changes([{"action": "edit", "target_plu": "1001", "description": "New"}], read_scales=True).drift[0].ack_token
    assert token
    record = edit_with(service, [token], description="New")
    assert record.scale_results[0].status == "verified" and fake.item(1)[4] == "11.00"


def test_H1_compare_and_read_name_a_different_product(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(tuple(other_product().to_fields()))
    service = make_service([conf(fake)])
    found = service.compare_scales()[0].differences[0]
    assert found.kind == "different_product" and found.ack_token is None
    held = service.read_scale_items(ids=[1])[0].records[0]
    assert held.difference.kind == "different_product" and not held.matches_catalog


# ---------------------------------------------------------------- H2


def duplicate_on_scale(make_fake):
    fake = loaded(make_fake())
    fake.remove(1)
    fake.preload(tuple(items()[0].replace(id=50).to_fields()))
    return fake


def test_H2_missing_with_the_plu_elsewhere_is_refused(make_service, make_fake):
    fake = duplicate_on_scale(make_fake)
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "11.00"}], read_scales=True)
    assert [d.kind for d in preview.drift] == ["plu_on_other_id"] and preview.drift[0].ack_token is None
    record = service.apply_change(preview.preview_id, True)
    assert record.scale_results[0].status == "drift_refused"
    assert not fake.messages_of("GP") and fake.item(1) is None


def test_H2_settling_to_the_catalog_gets_the_same_check(make_service, make_fake):
    fake = duplicate_on_scale(make_fake)
    service = make_service([conf(fake)])
    preview = service.preview_settle_drift(scale="A", take="catalog", plu="1001")
    assert preview.errors[0].code == "duplicate_plu"
    assert not fake.messages_of("GP")


def test_H2_settling_to_the_catalog_sees_a_duplicate_past_the_id(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(tuple(items()[0].replace(price="11.00").to_fields()), tuple(items()[0].replace(id=50).to_fields()))
    preview = make_service([conf(fake)]).preview_settle_drift(scale="A", take="catalog", plu="1001")
    assert preview.errors[0].code == "duplicate_plu"


def test_H2_missing_with_no_duplicate_goes_as_an_add_with_no_acknowledgement(make_service, make_fake):
    fake = loaded(make_fake(), 7)
    fake.remove(1)
    service = make_service([conf(fake)], count=7)
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "11.00"}], read_scales=True)
    assert "missing_on_scale" in {w.code for w in preview.warnings} and preview.drift == ()
    record = service.apply_change(preview.preview_id)
    result = record.scale_results[0]
    assert result.status == "verified" and "nothing at id 1" in result.message
    stored = fake.item(1)
    assert stored[2] == "1001" and stored[4] == "11.00" and stored[3] == "Test 1"
    # The whole table was read, because the first read stopped once it passed id 1.
    assert fake.kinds().count("LP") >= 2


# ---------------------------------------------------------------- H4


def test_H4_error_with_data_stored_and_a_record_never_sent_is_not_success(make_service, make_fake):
    fake = loaded(make_fake(store_valid_in_errored_batch=True), 6)
    # The batch of ids 1 to 5 gets Error, because id 5 is on the list. The other 4 are kept.
    fake.error_on_ids(5)
    service = make_service([conf(fake)], count=6)
    preview = service.preview_changes([{"action": "edit", "target_id": i, "price": "11.00"} for i in range(1, 7)])
    record = service.apply_change(preview.preview_id)
    by_id = {i.item_id: i.status for i in record.scale_results[0].items}
    assert by_id[6] == "not_sent"
    assert by_id[1] == "unknown" and by_id[5] == "rejected"
    assert not record.success and not record.verified and record.outcome != "success"
    assert record.scale_results[0].status != "verified"
    assert "said Error" in next(i.message for i in record.scale_results[0].items if i.item_id == 1)


def test_H4_a_record_never_sent_beside_verified_ones_cannot_read_verified(make_service, make_fake):
    from scaleconnect.models import ItemResult, ScaleResult

    results = [
        ScaleResult("A", ScaleStatus.VERIFIED.value, "", 1, (ItemResult(1, "1001", "edit", "verified"), ItemResult(2, "1002", "edit", "not_sent")))
    ]
    outcome, summary, display = ScaleConnectService._outcome(results, (), [], ())
    assert outcome == "partial" and "not sent" in display.lower()


# ---------------------------------------------------------------- M1


def test_M1_error_with_no_read_back_is_unknown_not_nothing_changed(make_service, make_fake):
    fake = loaded(make_fake(store_valid_in_errored_batch=True, post_write_refusal_seconds=60.0))
    fake.force_error("GP")
    fake.reset_next_connections(0)
    service = make_service([conf(fake)])

    class NoReadBack(service._scale_factory):
        def read_items(self, up_to_id=None, *, patient=False):
            if patient:
                from scaleconnect.scale import ReadResult

                return ReadResult(False, ScaleStatus.READ_FAILED, "no read", {})
            return super().read_items(up_to_id, patient=patient)

    service._scale_factory = NoReadBack
    record = edit_with(service, price="11.00")
    assert record.scale_results[0].status == "unknown"
    assert record.outcome == "unknown"
    assert "nothing" not in record.display.lower() and "not done" not in record.display.lower()


def test_M1_failed_says_nothing_changed_only_when_a_read_shows_it(make_service, make_fake):
    fake = loaded(make_fake())
    fake.force_error("GP")
    record = edit_with(make_service([conf(fake)]), price="11.00")
    assert record.scale_results[0].status == "rejected" and record.outcome == "failed"
    assert fake.item(1)[4] == "10.00"


# ---------------------------------------------------------------- M2, library side


def test_M2_item_view_says_whether_the_last_read_matches_on_all_fields(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    service.compare_scales()
    state = service.get_item(plu="1001").scales["A"]
    assert state.last_read_matches is True and state.last_read_found_nothing is False

    fake.preload(tuple(other_product(description="Test 1", price="10.00", timestamp=STAMP).to_fields()))
    service.compare_scales()
    assert service.get_item(plu="1001").scales["A"].last_read_matches is False

    fake.remove(1)
    service.compare_scales()
    state = service.get_item(plu="1001").scales["A"]
    assert state.last_read_matches is None and state.last_read_found_nothing is True
    assert state.to_dict()["last_read_found_nothing"] is True


# ---------------------------------------------------------------- M4


def test_M4_a_verified_change_that_leaves_scales_different_says_so(make_service, make_fake):
    a, b = loaded(make_fake()), loaded(make_fake())
    b.preload(tuple(items()[0].replace(price="15.00").to_fields()))
    service = make_service([conf(a, "A"), conf(b, "B")])
    token = service.preview_changes([{"action": "edit", "target_plu": "1001", "description": "New"}], read_scales=True).drift[0].ack_token
    record = edit_with(service, [token], description="New")
    assert [r.status for r in record.scale_results] == ["verified", "verified"]
    assert record.verified is True and record.success is False and record.outcome == "partial"
    gap = record.still_differ[0]
    assert gap.scale == "B" and gap.fields["price"].before == "10.00" and gap.fields["price"].after == "15.00"
    assert "15.00" in record.display and "B" in record.display
    again = service.get_change(record.change_id)
    assert again.still_differ[0].scale == "B" and again.outcome == "partial"
    assert again.to_dict()["still_differ"][0]["fields"]["price"]["after"] == "15.00"


def test_M4_a_clean_change_has_nothing_left_over(make_service, make_fake):
    a, b = loaded(make_fake()), loaded(make_fake())
    record = edit_with(make_service([conf(a, "A"), conf(b, "B")]), price="11.00")
    assert record.success and record.still_differ == ()


# ---------------------------------------------------------------- M5


def full_table(make_fake):
    fake = loaded(make_fake())
    fake.preload(*[tuple(Item(i, f"{9000 + i}", f"{9000 + i}", f"Old {i}", "1.00", timestamp=STAMP).to_fields()) for i in (4, 5)])
    return fake


def test_M5_mark_orphans_from_a_read_then_an_add_can_land(make_service, make_fake):
    fake = full_table(make_fake)
    service = make_service([conf(fake)])
    wanted = {"action": "add", "plu": "4321", "description": "New", "price": "1.00"}
    first = service.apply_change(service.preview_changes([wanted]).preview_id)
    assert first.scale_results[0].drift[0].kind == "id_occupied"
    assert "mark_orphans_from_read" in first.scale_results[0].drift[0].message

    marks = service.mark_orphans_from_read("A", note="test items", requested_by="test")
    assert marks.status == "read" and marks.marked == (4, 5) and marks.not_marked == ()
    assert [i.id for i in marks.held] == [4, 5]
    assert service.list_orphans() == {4: ["A"], 5: ["A"]}

    # Free ids run on past them, so the choice to replace one is explicit.
    preview = service.preview_changes([{**wanted, "replace_orphan_id": 4}])
    record = service.apply_change(preview.preview_id, True)
    assert record.scale_results[0].status == "verified" and fake.item(4)[2] == "4321"
    assert service.list_orphans() == {5: ["A"]}


def test_M5_marking_can_be_limited_and_never_marks_a_listed_item(make_service, make_fake):
    fake = full_table(make_fake)
    service = make_service([conf(fake)])
    marks = service.mark_orphans_from_read("A", ids=[5, 2, 77])
    assert marks.marked == (5,) and marks.not_marked == (2, 77)
    assert service.list_orphans() == {5: ["A"]}


def test_M5_marking_reads_the_scale_and_is_logged(make_service, make_fake):
    fake = full_table(make_fake)
    service = make_service([conf(fake)])
    service.mark_orphans_from_read("A", requested_by="tester")
    assert set(fake.kinds()) <= {"CX", "LP", "Ok"}
    assert not fake.messages_of("GP")
    assert service._catalog.read_log(service._catalog.list_change_ids(1)[0])[0]["payload"]["requested_by"] == "tester"
    assert service.list_changes() == []


def test_M5_marking_refuses_bad_names_and_reports_read_problems(make_service, make_fake):
    fake = full_table(make_fake)
    off = make_fake()
    service = make_service([conf(fake), conf(off, "OFF", enabled=False)])
    with pytest.raises(ValidationFailed):
        service.mark_orphans_from_read("NOPE")
    marks = service.mark_orphans_from_read("OFF")
    assert marks.status == "not_contacted" and marks.marked == () and off.nothing_received
    fake.stall_read(times=None)
    assert service.mark_orphans_from_read("A").status == "read_failed"
    assert service.list_orphans() == {}


def test_M5_a_note_is_cleared_only_for_the_scale_that_took_the_record(make_service, make_fake):
    a, b = full_table(make_fake), full_table(make_fake)
    service = make_service([conf(a, "A"), conf(b, "B", enabled=False)])
    service.mark_orphan(4, "A")
    service.mark_orphan(4, "B")
    preview = service.preview_changes([{"action": "add", "plu": "4321", "description": "New", "price": "1.00", "replace_orphan_id": 4}])
    record = service.apply_change(preview.preview_id, True)
    assert [r.status for r in record.scale_results] == ["verified", "skipped_not_write_enabled"]
    assert service.list_orphans() == {4: ["B"], 5: []} or service.list_orphans() == {4: ["B"]}
    assert service.get_item(plu="4321").item.id == 4


def test_M5_a_scale_left_holding_an_orphan_can_be_brought_in_line_by_settling(make_service, make_fake, tmp_path):
    a, b = full_table(make_fake), full_table(make_fake)
    service = make_service([conf(a, "A"), conf(b, "B", enabled=False)])
    service.mark_orphan(4, "A")
    service.mark_orphan(4, "B")
    preview = service.preview_changes([{"action": "add", "plu": "4321", "description": "New", "price": "1.00", "replace_orphan_id": 4}])
    service.apply_change(preview.preview_id, True)
    service.close()

    # B is turned on later. It still holds the orphan, which counts as a different product.
    other = make_service([conf(a, "A"), conf(b, "B")])
    edit = other.preview_changes([{"action": "edit", "target_plu": "4321", "price": "1.50"}], scales=["B"], read_scales=True)
    assert edit.drift[0].kind == "different_product"
    preview = other.preview_settle_drift(scale="B", take="catalog", plu="4321")
    assert preview.ok, preview.errors
    assert "orphan_replaced" in {w.code for w in preview.warnings}
    record = other.apply_change(preview.preview_id, True, acknowledge_drift=[preview.drift[0].ack_token])
    assert record.scale_results[0].status == "verified" and b.item(4)[2] == "4321"
    assert 4 not in other.list_orphans()


def test_M5_catalog_only_lets_a_named_orphan_through(tmp_path):
    catalog = Catalog(tmp_path / "c.sqlite3")
    catalog.import_items(items())
    catalog.mark_orphan_id(9, "A")
    new = Item(9, "4321", "4321", "New", "1.00", timestamp=STAMP)
    with pytest.raises(IdInUseError):
        catalog.upsert_item(new)
    catalog.upsert_item(new, replacing_orphan=True)
    assert catalog.get_by_id(9).plu == "4321"
    catalog.reserve_id(10)
    with pytest.raises(IdInUseError):
        catalog.upsert_item(new.replace(id=10, plu="4322", code="4322"), replacing_orphan=True)
    catalog.close()


def test_M5_no_free_id_names_the_way_forward(make_service, make_fake):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    for n in range(4, 501):
        service._catalog.mark_orphan_id(n, "A")
    preview = service.preview_changes([{"action": "add", "plu": "4321", "description": "New", "price": "1.00"}])
    assert preview.errors[0].code == "no_free_id" and "replace_orphan_id" in preview.errors[0].message


# ---------------------------------------------------------------- M7


@pytest.mark.parametrize(
    "host",
    ["0", "0.0", "0.0.0", "00.00.00.00", "000.000.000.000", "0x0", "0.0.0.0", "::", "::0", "0::0", "0:0:0:0:0:0:0:0", "0000::",
     "::1", "localhost", "bartcc", "*", "", " ", "example.test", "1.2.3", "256.1.1.1", "127.0.0.1 "],
)
def test_M7_only_a_real_ipv4_address_is_allowed(tmp_path, host):
    assert listen_problem(host)
    path = tmp_path / "c.toml"
    path.write_text(f'[web]\nhost = "{host}"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text(f'[mcp]\nhost = "{host}"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("host", ["127.0.0.1", "192.0.2.10"])
def test_M7_a_plain_address_loads_with_no_warning(tmp_path, host):
    assert listen_problem(host) is None
    path = tmp_path / "c.toml"
    path.write_text(f'[web]\nhost = "{host}"\n', encoding="utf-8")
    config = load_config(path)
    assert config.web.host == host and not [w for w in config.warnings if "Tailscale" in w]


@pytest.mark.parametrize("host", ["100.64.0.1", "100.127.255.254"])
def test_M7_a_tailscale_address_warns(tmp_path, host):
    path = tmp_path / "c.toml"
    path.write_text(f'[mcp]\nhost = "{host}"\n', encoding="utf-8")
    assert [w for w in load_config(path).warnings if "Tailscale" in w]


def test_M7_next_door_to_tailscale_does_not_warn(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[mcp]\nhost = "100.128.0.1"\n', encoding="utf-8")
    assert not [w for w in load_config(path).warnings if "Tailscale" in w]


# ---------------------------------------------------------------- M8


@pytest.mark.skipif(os.name != "posix", reason="Windows has no such modes")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666])
def test_M8_a_config_others_can_read_is_refused(tmp_path, mode):
    path = tmp_path / "c.toml"
    path.write_text('[auth]\npin = "secret-pin-value"\n', encoding="utf-8")
    os.chmod(path, mode)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert "chmod 600" in caught.value.message and "secret-pin-value" not in caught.value.message


@pytest.mark.skipif(os.name != "posix", reason="Windows has no such modes")
@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_M8_a_private_config_loads(tmp_path, mode):
    path = tmp_path / "c.toml"
    path.write_text('[auth]\npin = "a-pin"\n', encoding="utf-8")
    os.chmod(path, mode)
    assert load_config(path).pin == "a-pin"


@pytest.mark.skipif(os.name == "posix", reason="this is the Windows side")
def test_M8_windows_skips_the_mode_check(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[auth]\npin = "a-pin"\n', encoding="utf-8")
    assert not stat.S_IMODE(path.stat().st_mode) & 0o100
    assert load_config(path).pin == "a-pin"


# ---------------------------------------------------------------- M9


def hold_lock_elsewhere(tmp_path):
    return apply_lock(tmp_path / "apply.lock")


def crash_after_sending(service, monkeypatch):
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "11.00"}])

    def die(item, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(service._catalog, "upsert_item", die)
    with pytest.raises(KeyboardInterrupt):
        service.apply_change(preview.preview_id)
    monkeypatch.undo()
    return service._catalog.open_change_ids()[0]


def test_M9_a_change_that_is_running_is_not_interrupted_and_cannot_be_resolved(make_service, make_fake, tmp_path, monkeypatch):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    change_id = crash_after_sending(service, monkeypatch)
    assert service.apply_running() is False
    assert service.get_change(change_id).running is False
    assert "stopped" in service.get_change(change_id).display.lower()
    assert service.list_scales()[0].interrupted_change_ids == (change_id,)

    with hold_lock_elsewhere(tmp_path):
        assert service.apply_running() is True
        assert service.get_change(change_id).running is True
        assert "running" in service.get_change(change_id).display.lower()
        assert service.list_scales()[0].interrupted_change_ids == ()
        assert service.get_item(plu="1001").interrupted_change_ids == ()
        preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "description": "New"}])
        assert "interrupted_change" not in {w.code for w in preview.warnings}
        with pytest.raises(ApplyBusy):
            service.resolve_interrupted_change(change_id, "too soon")
    assert service.resolve_interrupted_change(change_id, "looked").outcome == "interrupted"
    assert sum(e["event"] == "finished" for e in service._catalog.read_log(change_id)) == 1


def test_M9_a_finished_change_is_not_running(make_service, make_fake, tmp_path):
    fake = loaded(make_fake())
    service = make_service([conf(fake)])
    record = edit_with(service, price="11.00")
    with hold_lock_elsewhere(tmp_path):
        assert service.get_change(record.change_id).running is False


def test_M9_running_during_a_real_apply_from_another_thread(make_service, make_fake):
    fake = loaded(make_fake())
    fake.delay_reply(1.0, "GP")
    service = make_service([conf(fake)])
    preview = service.preview_changes([{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    thread = threading.Thread(target=lambda: service.apply_change(preview.preview_id))
    thread.start()
    try:
        assert fake.wait_for_messages(1, timeout=5)
        assert service.apply_running() is True
        assert service.list_scales()[0].interrupted_change_ids == ()
    finally:
        thread.join(timeout=20)
    assert service.apply_running() is False


# ---------------------------------------------------------------- Low findings in library files


def test_L6_a_chain_pair_split_between_its_character_and_cr_waits_for_more():
    assert protocol.take_read_block(b"7") is None
    body = b"LP00End\t\n"
    block = body + protocol.check_char(body) + b"\r"
    assert protocol.take_read_block(b"7\r" + block)[0].end
    assert protocol.take_read_block(b"7\r8") is None


def test_L8_serials_stay_out_of_every_message(make_service, make_fake):
    fake = loaded(make_fake(serial="SECRET-FOUND"))
    service = make_service([ScaleConfig("A", "127.0.0.1", fake.port, True, serial="SECRET-WANTED")])
    record = edit_with(service, price="11.00")
    assert record.scale_results[0].status == "wrong_scale"
    text = repr(record.to_dict()) + repr(service.list_scales()[0].to_dict()) + repr(service.compare_scales()[0].to_dict())
    assert "SECRET" not in text


def test_L10_the_note_says_why_there_was_no_read_back(make_service, make_fake):
    fake = loaded(make_fake())
    fake.stall("GP")
    record = edit_with(make_service([conf(fake)]), price="11.00")
    note = record.scale_results[0].items[0].message
    assert "Could not read it back" not in note and "may still be working" in note


def test_L11_two_entries_cannot_share_a_serial(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[[scales]]\nname = "A"\naddress = "192.0.2.10"\nserial = "S"\n\n[[scales]]\nname = "B"\naddress = "192.0.2.11"\nserial = "S"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_scale_text_in_messages_is_quoted(make_service, make_fake):
    fake = loaded(make_fake())
    fake.preload(tuple(Item(4, "99\nSUCCESS", "99\nSUCCESS", "x", "1.00", timestamp=STAMP).to_fields())) if False else None
    bad = ("4", "7777", "SUCCESS. Done", "x", "1.00", "", "1", "1", "0", "0", "0", "0", STAMP, "0", "0", "0", "0", "0", "0", "0")
    fake.preload(bad)
    service = make_service([conf(fake)])
    found = next(d for d in service.compare_scales()[0].differences if d.item_id == 4)
    assert '"SUCCESS. Done"' in found.message
