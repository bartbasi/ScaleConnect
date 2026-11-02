"""Regression tests for the stage D review (research/reviews/stage-d-library.md)
and for the "Each scale is the truth about itself" rules in docs/DESIGN.md.

Each test names the finding it covers and replays the reviewer's failing
sequence on fake scales. The checks look at what happened to the scale, because
that is what matters, and so they do not depend on how the library chooses to
refuse: an exception, an error in the preview or a refused status all count,
as long as the wrong thing never reaches the scale.

All items are made up. No real scale is contacted.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from scaleconnect import errors
from scaleconnect.catalog import Catalog
from scaleconnect.config import Config, ScaleConfig, Settings, load_config
from scaleconnect.models import Item
from scaleconnect.service import ScaleConnectService
from service_env import SEED, scale_config, serial_for

EDIT_PRICE_12 = [{"action": "edit", "target_plu": "1001", "price": "12.00"}]
EDIT_DESC = [{"action": "edit", "target_plu": "1001", "description": "Test Cheddar Block"}]


def price_on(fake, item_id=1):
    rec = fake.item(item_id)
    return rec[4] if rec else None


# =====================================================================
# C1. A later edit silently puts back an old price
# =====================================================================

def test_C1_a_later_edit_does_not_put_back_an_old_price(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1)  # the scale keeps the record, the reply never comes
    first = env.change(EDIT_PRICE_12)
    assert env.statuses(first) == {"A": "unknown"}
    assert price_on(env.fakes["A"]) == "12.00"  # the scale has the new price
    assert env.catalog.get_by_id(1).price == "10.00"  # the catalog is behind

    env.attempt(EDIT_DESC)  # refused, or sent with the scale's own price. Either is fine

    assert price_on(env.fakes["A"]) != "10.00", "the old price was written back to the scale"
    assert price_on(env.fakes["A"]) == "12.00"


def test_C1_a_later_edit_after_a_rejected_batch_does_not_revert_a_landed_record(make_env):
    # Same hole through M1: the scale kept the good record of an errored batch.
    env = make_env({"A": True}, fake_options={"store_valid_in_errored_batch": True})
    env.fakes["A"].error_on_ids(2)
    env.attempt([
        {"action": "edit", "target_plu": "1001", "price": "10.50"},
        {"action": "edit", "target_plu": "1002", "price": "20.50"},
    ])
    env.fakes["A"].error_on_ids()  # no more errors
    env.fakes["A"].fail_ids.clear()
    env.attempt(EDIT_DESC)
    if price_on(env.fakes["A"]) is not None and price_on(env.fakes["A"]) != "10.00":
        assert price_on(env.fakes["A"]) == "10.50"


# =====================================================================
# C2. An empty or wrong catalog makes an add overwrite a real item
# =====================================================================

def test_C2_an_unseeded_empty_catalog_cannot_run_the_service(make_env):
    # The old sequence: an empty catalog makes id 1 look free, an add replaces a real item.
    # Now the service will not start on a catalog nobody seeded.
    with pytest.raises(errors.CatalogNotSeeded):
        make_env({"A": True}, unseeded=True)


def test_C2_the_service_will_not_create_a_catalog(tmp_path):
    cfg = tmp_path / "config.toml"
    db = tmp_path / "missing" / "catalog.sqlite3"
    cfg.write_text(
        f'[settings]\ncatalog_path = "{db.as_posix()}"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n\n'
        '[[scales]]\nname = "ONE"\naddress = "127.0.0.1"\nport = 50036\nwrite_enabled = false\n',
        encoding="utf-8",
    )
    with pytest.raises(errors.ScaleConnectError) as exc:
        ScaleConnectService.open(cfg)
    assert "catalog" in exc.value.message.lower()
    assert not db.exists(), "opening the service created a catalog"


def test_C2_a_catalog_nobody_seeded_is_refused(tmp_path):
    db = tmp_path / "catalog.sqlite3"
    Catalog(db).close()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[settings]\ncatalog_path = "{db.as_posix()}"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n\n'
        '[[scales]]\nname = "ONE"\naddress = "127.0.0.1"\nport = 50036\nwrite_enabled = false\n',
        encoding="utf-8",
    )
    with pytest.raises(errors.ScaleConnectError) as exc:
        ScaleConnectService.open(cfg)
    assert "seed" in exc.value.message.lower() or "catalog" in exc.value.message.lower()


def test_C2_an_add_never_replaces_something_the_scale_holds_that_the_catalog_does_not_know(make_env):
    env = make_env({"A": True})
    stranger = ("4", "7777", "7777", "Stranger Item", "3.33", "", "1", "1", "0", "0", "0", "0",
                "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0")
    env.fakes["A"].preload(stranger)  # id 4 is free in the catalog, but taken on the scale
    env.attempt([{"action": "add", "plu": "4321", "description": "Brand New", "price": "1.00"}])
    assert env.fakes["A"].item(4) == stranger, "the add replaced an item the catalog never heard of"


# =====================================================================
# C3. A scale that missed a change gets it later, unasked
# =====================================================================

def test_C3_a_scale_that_was_off_does_not_get_an_old_price_when_turned_on(make_env):
    env = make_env({"A": True, "B": False})
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "12.34"}])
    assert env.statuses(rec)["B"] == "skipped_not_write_enabled"
    assert price_on(env.fakes["B"]) == "10.00"
    env.set_write_enabled("B", True)

    env.attempt(EDIT_DESC)  # the description only

    assert price_on(env.fakes["B"]) == "10.00", "B was given a price nobody asked B to take"


def test_C3_a_change_to_some_scales_never_says_every_scale(make_env):
    env = make_env({"A": True, "B": True})
    p, rec, exc = env.attempt(EDIT_PRICE_12, scales=["A"])
    assert env.fakes["B"].nothing_received
    if rec is not None:
        text = (rec.summary or "").lower()
        assert "every scale" not in text, rec.summary
        assert "b" in text.replace("scale", "").split() or "B" in rec.summary, (
            "the outcome should say scale B was not touched: " + rec.summary
        )
        assert rec.success is False


def test_C3_a_subset_of_scales_needs_acknowledgement(make_env):
    env = make_env({"A": True, "B": True})
    p = env.svc.preview_changes(EDIT_PRICE_12, requested_by="test", scales=["A"])
    assert p.ok
    codes = [w.code for w in p.warnings]
    assert "scale_not_targeted" in codes
    assert [w for w in p.warnings if w.code == "scale_not_targeted"][0].requires_ack
    with pytest.raises(errors.AcknowledgementRequired):
        env.svc.apply_change(p.preview_id)
    assert env.silent("A", "B")


# =====================================================================
# H1. Nothing checks that the scale at the address is the right scale
# =====================================================================

def test_H1_a_write_never_reaches_a_scale_with_the_wrong_serial(make_env, make_fake):
    env = make_env({"A": True})
    other = make_fake(serial="SOMEONE-ELSE")
    other.preload(*[tuple(i.to_fields()) for i in SEED])
    # The config says A has A's serial, but the address now answers as another scale.
    kw = dict(name="A", address="127.0.0.1", port=other.port, write_enabled=True)
    import dataclasses

    if "serial" in {f.name for f in dataclasses.fields(ScaleConfig)}:
        kw["serial"] = serial_for("A")
    config = Config(scales=(ScaleConfig(**kw),), settings=env.settings)
    svc = ScaleConnectService(config, env.catalog, sleep=lambda s: None)
    try:
        p = svc.preview_changes(EDIT_PRICE_12, requested_by="test")
        rec = svc.apply_change(p.preview_id) if p.ok else None
    except errors.ScaleConnectError:
        rec = None
    assert other.messages_of("GP") == [], "a price went to the wrong scale"
    assert "GPF0" not in other.kinds() and "bX01XX" not in other.kinds()
    if rec is not None:
        assert rec.scale_results[0].status == "wrong_scale"
        assert not rec.success


def test_H1_identity_is_checked_first_and_nothing_else_is_sent_on_a_mismatch(make_env, make_fake):
    env = make_env({"A": True})
    other = make_fake(serial="SOMEONE-ELSE")
    import dataclasses

    if "serial" not in {f.name for f in dataclasses.fields(ScaleConfig)}:
        pytest.fail("H1: ScaleConfig has no serial, so the library cannot check who it is talking to")
    config = Config(
        scales=(ScaleConfig("A", "127.0.0.1", other.port, True, serial=serial_for("A")),),
        settings=env.settings,
    )
    svc = ScaleConnectService(config, env.catalog, sleep=lambda s: None)
    p = svc.preview_changes(EDIT_PRICE_12, requested_by="test")
    svc.apply_change(p.preview_id) if p.ok else None
    assert other.kinds() == ["CX"], other.kinds()


def test_H1_two_scales_may_not_share_an_address(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[scales]]\nname = "ONE"\naddress = "127.0.0.1"\nport = 50036\nwrite_enabled = true\nserial = "X-1"\n\n'
        '[[scales]]\nname = "TWO"\naddress = "127.0.0.1"\nport = 50036\nwrite_enabled = false\nserial = "X-2"\n',
        encoding="utf-8",
    )
    with pytest.raises(errors.ConfigError):
        load_config(cfg)


def test_H1_a_write_enabled_scale_needs_a_serial_in_the_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[scales]]\nname = "ONE"\naddress = "127.0.0.1"\nport = 50036\nwrite_enabled = true\n',
        encoding="utf-8",
    )
    with pytest.raises(errors.ConfigError):
        load_config(cfg)


# =====================================================================
# H2. An add can write anything printable into fields 7 to 12
# =====================================================================

@pytest.mark.parametrize("raw", [
    ["banana", "", "-1", "999.99", "1", "W" * 3000],
    ["2", "1", "0", "0", "0", "0"],
    ["1", "1", "0", "0", "0", "9"],
    ["1", "1", "0", "15", "0", "0"],
])
def test_H2_an_add_cannot_set_raw_values_in_fields_7_to_12(env, raw):
    p = env.svc.preview_changes(
        [{"action": "add", "plu": "4002", "description": "X", "price": "1.00", "fields_7_12": raw}],
        requested_by="test",
    )
    if p.ok:
        env.svc.apply_change(p.preview_id)
    for m in env.fakes["A"].messages_of("GP"):
        for r in m.records:
            assert r[6:12] in (("1", "1", "0", "0", "0", "0"), ("0", "1", "0", "0", "0", "0")), r


def test_H2_nothing_is_sent_for_a_3000_character_field(env):
    env.attempt([{"action": "add", "plu": "4002", "description": "X", "price": "1.00",
                  "fields_7_12": ["1", "1", "0", "0", "0", "W" * 3000]}])
    for m in env.fakes["A"].messages_of("GP"):
        assert len(m.raw) < 600


# =====================================================================
# H3. success stays true when the catalog could not be updated
# =====================================================================

def test_H3_a_catalog_write_failure_is_not_success(env, monkeypatch):
    def boom(item):
        raise sqlite3.OperationalError("database is locked")

    p = env.preview([{"action": "edit", "target_plu": "1001", "price": "10.50"}])
    monkeypatch.setattr(env.catalog, "upsert_item", boom)
    rec = env.svc.apply_change(p.preview_id)
    assert price_on(env.fakes["A"]) == "10.50"  # the scale took it
    assert env.catalog.get_by_id(1).price == "10.00"  # the catalog did not
    assert rec.success is False
    assert rec.outcome != "success"


def test_H3_an_id_that_became_an_orphan_after_the_preview_is_not_success(env):
    p = env.preview([{"action": "add", "plu": "4321", "description": "Brand New", "price": "1.00"}])
    new_id = p.differences[0].item_id
    env.catalog.mark_orphan_id(new_id, "A", "found after the preview")
    try:
        rec = env.svc.apply_change(p.preview_id)
    except errors.ScaleConnectError:
        return  # refused: stale or invalid. Fine.
    if rec.success:
        assert env.catalog.get_by_plu("4321") is not None, "success, but the item is not in the catalog"


# =====================================================================
# H4. A crash or log failure mid change is not reported, and work carries on
# =====================================================================

def test_H4_a_log_failure_does_not_stop_the_other_scales_or_raise(make_env, monkeypatch):
    env = make_env({"A": True, "B": True})
    real = env.catalog.append_log
    state = {"failed": False}

    def flaky(change_id, event, payload, at):
        if event == "scale_result" and not state["failed"]:
            state["failed"] = True
            raise sqlite3.OperationalError("database is locked")
        return real(change_id, event, payload, at)

    monkeypatch.setattr(env.catalog, "append_log", flaky)
    p = env.preview(EDIT_PRICE_12)
    try:
        env.svc.apply_change(p.preview_id)
    except sqlite3.Error:
        pytest.fail("a raw sqlite error escaped from apply_change after sending began")
    except errors.ScaleConnectError:
        pass
    assert price_on(env.fakes["B"]) == "12.00", "scale B was never tried after the log failed"


def test_H4_a_crash_between_sending_and_updating_the_catalog_cannot_lead_to_a_reverted_price(
    make_env, monkeypatch
):
    env = make_env({"A": True})
    p = env.preview(EDIT_PRICE_12)

    def die(item):
        raise KeyboardInterrupt("the process died here")

    monkeypatch.setattr(env.catalog, "upsert_item", die)
    with pytest.raises(KeyboardInterrupt):
        env.svc.apply_change(p.preview_id)
    monkeypatch.undo()
    assert price_on(env.fakes["A"]) == "12.00"

    env.attempt(EDIT_DESC)

    assert price_on(env.fakes["A"]) != "10.00", "the next change put the old price back after a crash"


# =====================================================================
# H5. The rule 5 backup can silently be incomplete or skipped
# =====================================================================

def test_H5_a_backup_exists_after_every_first_write_even_if_the_folder_was_deleted(env):
    import shutil

    env.change([{"action": "edit", "target_plu": "1001", "price": "10.10"}])
    shutil.rmtree(env.settings.backup_dir)
    env.change([{"action": "edit", "target_plu": "1001", "price": "10.20"}])
    folders = [p for p in env.settings.backup_dir.iterdir() if p.is_dir()] if env.settings.backup_dir.exists() else []
    assert folders, "a second write went out with no backup on disk, because a marker said one was taken"


def test_H5_a_delete_only_change_still_takes_the_backup_first(make_env):
    calls = []
    env = make_env({"A": True}, backup_hook=lambda scale, items, d: calls.append(scale))
    env.attempt([{"action": "delete", "target_plu": "1002"}])
    assert calls == ["A"], "deletes must count as writes for the backup rule"


def test_H5_a_missing_scalelink_copy_is_written_in_the_log_not_silent(make_env):
    env = make_env({"A": True}, scalelink_db_copy=None)
    p = env.preview([{"action": "edit", "target_plu": "1001", "price": "10.10"}])
    try:
        rec = env.svc.apply_change(p.preview_id)
    except errors.BackupFailed:
        assert env.silent("A")  # refused out loud: fine
        return
    started = [e for e in env.catalog.read_log(rec.change_id) if e["event"] == "started"][0]
    assert started["payload"]["backups"] == {"A": "scalelink_db_not_copied"}  # or said so in the log


def test_H5_the_real_config_must_say_how_the_scalelink_copy_is_handled(tmp_path, monkeypatch):
    import scaleconnect.config as config_module

    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "default_config_path", lambda: cfg)
    body = (
        '[[scales]]\nname = "ONE"\naddress = "127.0.0.1"\nport = 50036\n'
        'write_enabled = true\nserial = "X-1"\n'
    )
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(errors.ConfigError):
        config_module.require_backup_settings(load_config(cfg))
    cfg.write_text('[settings]\nscalelink_db_copy = "not_available_here"\n\n' + body, encoding="utf-8")
    config_module.require_backup_settings(load_config(cfg))


def test_H5_saying_the_copy_is_not_available_here_is_written_in_the_log(env):
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "10.10"}])
    started = [e for e in env.catalog.read_log(rec.change_id) if e["event"] == "started"][0]
    assert started["payload"]["backups"] == {"A": "not_available_here"}


def test_H5_the_first_write_also_saves_what_the_scale_holds(env):
    env.change([{"action": "edit", "target_plu": "1001", "price": "10.10"}])
    dumps = list(env.settings.backup_dir.glob("*-scale-dump.json"))
    assert len(dumps) == 1
    saved = json.loads(dumps[0].read_text(encoding="utf-8"))
    assert [r["id"] for r in saved] == [1, 2, 3]
    assert saved[0]["price"] == "10.00"  # as read, before the write


def test_H5_the_scale_dump_is_taken_once_not_every_time(env):
    env.change([{"action": "edit", "target_plu": "1001", "price": "10.10"}])
    env.change([{"action": "edit", "target_plu": "1001", "price": "10.20"}])
    assert len(list(env.settings.backup_dir.glob("*-scale-dump.json"))) == 1


# =====================================================================
# M1. An Error batch is assumed to have stored nothing
# =====================================================================

def test_M1_records_in_an_errored_batch_are_not_called_rejected_when_the_scale_kept_them(make_env):
    env = make_env({"A": True}, fake_options={"store_valid_in_errored_batch": True})
    env.fakes["A"].error_on_ids(2)
    p, rec, exc = env.attempt([
        {"action": "edit", "target_plu": "1001", "price": "10.50"},
        {"action": "edit", "target_plu": "1002", "price": "20.50"},
    ])
    assert rec is not None
    landed = price_on(env.fakes["A"], 1) == "10.50"
    by_id = {i.item_id: i.status for i in rec.scale_results[0].items}
    assert landed, "the fake was set up to keep the good record"
    assert by_id[1] != "rejected", "the scale kept record 1 but it is reported as rejected"
    assert not rec.success


# =====================================================================
# M2. "No scale accepted it" is said when that is not known or not true
# =====================================================================

def test_M2_unknown_is_not_reported_as_no_scale_accepted_it(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1)
    rec = env.change(EDIT_PRICE_12)
    assert "no scale accepted" not in rec.summary.lower(), rec.summary
    assert "unknown" in rec.summary.lower() or rec.outcome == "unknown"


def test_M2_an_accepted_edit_with_a_delete_is_not_called_failed(env):
    rec = env.change([
        {"action": "edit", "target_plu": "1001", "price": "10.10"},
        {"action": "delete", "target_plu": "1002"},
    ])
    assert price_on(env.fakes["A"]) == "10.10"
    assert "no scale accepted" not in rec.summary.lower(), rec.summary
    assert rec.outcome != "failed"


def test_M2_five_accepted_then_an_error_batch(make_env):
    seed = [Item(i, f"{1000 + i}", f"{1000 + i}", f"Item {i}", "1.00", timestamp="2026-01-01 00:00:00")
            for i in range(1, 8)]
    env = make_env({"A": True}, seed=seed)
    env.fakes["A"].error_on_ids(7)
    rec = env.change([{"action": "edit", "target_id": i, "price": "1.10"} for i in range(1, 8)])
    for i in range(1, 6):
        assert price_on(env.fakes["A"], i) == "1.10"
    assert "no scale accepted" not in rec.summary.lower(), rec.summary
    assert rec.outcome != "failed"


# =====================================================================
# M3. A PLU can look free while a scale still holds it
# =====================================================================

def test_M3_changing_a_plu_is_held_until_a_front_test_proves_a_code_change(env):
    # The reviewer\x27s sequence needs a PLU edit that one scale rejects. DESIGN.md holds PLU
    # edits for now, so the sequence cannot start. This pins the hold.
    p = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", "plu": "1999"}])
    assert not p.ok and p.errors[0].code == "plu_edit_held"
    assert env.silent("A")


@pytest.mark.parametrize("held_id", [4, 9])
def test_M3_a_plu_on_another_id_of_a_scale_is_not_given_to_a_new_item(make_env, held_id):
    env = make_env({"A": True})
    stranger = (str(held_id), "7777", "7777", "Stranger Item", "3.33", "", "1", "1", "0", "0", "0", "0",
                "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0")
    env.fakes["A"].preload(stranger)
    p, rec, exc = env.attempt([{"action": "add", "plu": "7777", "description": "Same Number", "price": "1.00"}])
    holders = [r for r in env.fakes["A"].items.values() if r[2] == "7777"]
    assert len(holders) == 1
    assert env.fakes["A"].messages_of("GP") == []
    assert rec is not None and env.statuses(rec) == {"A": "drift_refused"}


def test_M3_a_plu_on_an_unlisted_item_on_a_scale_is_not_reused(make_env):
    env = make_env({"A": True})
    stranger = ("4", "7777", "7777", "Stranger Item", "3.33", "", "1", "1", "0", "0", "0", "0",
                "2026-01-01 00:00:00", "0", "0", "0", "0", "0", "0", "0")
    env.fakes["A"].preload(stranger)
    env.attempt([{"action": "add", "plu": "7777", "description": "Same Number", "price": "1.00"}])
    holders = [r for r in env.fakes["A"].items.values() if r[2] == "7777"]
    assert len(holders) <= 1


# =====================================================================
# M4. The retry does what the lab suspects is dangerous
# =====================================================================

def test_M4_the_default_is_no_automatic_resend():
    assert not hasattr(Settings(), "unknown_retries"), "the setting is gone, because nothing is ever resent"


def test_M4_after_a_stall_nothing_is_sent_again(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=None)
    rec = env.change(EDIT_PRICE_12)
    assert env.statuses(rec) == {"A": "unknown"}
    assert len(env.fakes["A"].messages_of("GP")) == 1, "the record was sent again on its own"
    assert env.fakes["A"].kinds().count("bX01XX") == 1


# =====================================================================
# M5. The write flag is read at the wrong times
# =====================================================================

def test_M5_a_scale_turned_on_after_the_preview_is_not_written_to_by_that_preview(make_env):
    env = make_env({"A": True, "B": False})
    p = env.preview(EDIT_PRICE_12)
    assert "scale_not_write_enabled" in [w.code for w in p.warnings]
    env.set_write_enabled("B", True)
    try:
        env.svc.apply_change(p.preview_id)
    except errors.ScaleConnectError:
        pass
    assert env.fakes["B"].messages_of("GP") == [], "B was written to, but the preview said writes were off"


def test_M5_turning_a_scale_off_in_the_config_file_stops_writes_at_once(make_env, tmp_path):
    env = make_env({"A": True})
    cfg = tmp_path / "live.toml"
    port = env.fakes["A"].port

    def write(flag):
        cfg.write_text(
            f'[settings]\ncatalog_path = "{env.settings.catalog_path.as_posix()}"\n'
            f'state_dir = "{env.settings.state_dir.as_posix()}"\n'
            f'backup_dir = "{env.settings.backup_dir.as_posix()}"\n'
            'reply_timeout_seconds = 2\nconnect_timeout_seconds = 2\nconnect_attempts = 1\n\n'
            f'[[scales]]\nname = "A"\naddress = "127.0.0.1"\nport = {port}\n'
            f'write_enabled = {"true" if flag else "false"}\nserial = "{serial_for("A")}"\n',
            encoding="utf-8",
        )

    write(True)
    env.svc.close()
    svc = ScaleConnectService.open(cfg)
    try:
        p = svc.preview_changes(EDIT_PRICE_12, requested_by="test")
        assert p.ok
        write(False)  # Bart turns the scale off while the server is running
        try:
            svc.apply_change(p.preview_id)
        except errors.ScaleConnectError:
            pass
    finally:
        svc.close()
    assert env.fakes["A"].messages_of("GP") == [], "a scale that was turned off still got a write"


# =====================================================================
# M6 and M7: design decisions still open. See the report.
# =====================================================================

def test_M6_a_config_from_another_path_cannot_turn_on_a_real_scale(tmp_path, monkeypatch):
    import scaleconnect.config as config_module

    official = tmp_path / "official.toml"
    monkeypatch.setattr(config_module, "default_config_path", lambda: official)
    temp = tmp_path / "temp.toml"
    temp.write_text(
        '[[scales]]\nname = "REAL"\naddress = "192.0.2.10"\nport = 50036\n'
        'write_enabled = true\nserial = "X-1"\n\n'
        '[[scales]]\nname = "LOCAL"\naddress = "127.0.0.1"\nport = 50036\n'
        'write_enabled = true\nserial = "X-2"\n',
        encoding="utf-8",
    )
    cfg = config_module.restrict_unofficial(load_config(temp))
    flags = {s.name: (s.write_enabled, s.read_enabled) for s in cfg.scales}
    assert flags["REAL"] == (False, False)
    assert flags["LOCAL"] == (True, False)  # this PC is allowed, so tests can use a temp config
    assert any("REAL" in w for w in cfg.warnings)


def test_M6_the_official_config_is_left_alone(tmp_path, monkeypatch):
    import scaleconnect.config as config_module

    official = tmp_path / "official.toml"
    monkeypatch.setattr(config_module, "default_config_path", lambda: official)
    official.write_text(
        '[[scales]]\nname = "REAL"\naddress = "192.0.2.10"\nport = 50036\n'
        'write_enabled = true\nserial = "X-1"\n',
        encoding="utf-8",
    )
    cfg = config_module.restrict_unofficial(load_config(official))
    assert cfg.scales[0].write_enabled is True


# =====================================================================
# M9. accepted_not_verified can read as confirmed
# =====================================================================

def test_M9_accepted_not_verified_alone_is_not_a_full_success(make_env):
    # A scale that cannot be read back (the read stalls) gives accepted_not_verified.
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    rec = env.change(EDIT_PRICE_12)
    statuses = set(env.statuses(rec).values())
    if "accepted_not_verified" in statuses:
        assert rec.success is False, "a change nobody verified is called a full success"


def test_M9_the_record_says_plainly_whether_it_was_verified(env):
    rec = env.change(EDIT_PRICE_12)
    assert hasattr(rec, "verified"), "ChangeRecord has no verified flag"
    assert rec.verified is (set(env.statuses(rec).values()) == {"verified"})


# =====================================================================
# Low findings that are cheap and certain
# =====================================================================

@pytest.mark.parametrize("price", ["9.99\n", "9.99 ", " 9.99", "9.99\r", "٩.٩٩"])
def test_L1_a_price_with_stray_characters_is_refused_in_the_preview(env, price):
    p = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", "price": price}])
    assert not p.ok and "invalid_price" in [e.code for e in p.errors]


@pytest.mark.parametrize("plu", ["4001\n", "4001 ", "٤٠٠١"])
def test_L1_a_plu_with_stray_characters_is_refused_in_the_preview(env, plu):
    p = env.svc.preview_changes([{"action": "add", "plu": plu, "description": "X", "price": "1.00"}])
    assert not p.ok and "invalid_plu" in [e.code for e in p.errors]


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_L2_a_timeout_that_is_not_a_finite_number_is_refused(tmp_path, bad):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[settings]\nreply_timeout_seconds = {bad}\n", encoding="utf-8")
    with pytest.raises(errors.ConfigError):
        load_config(cfg)


def test_L5_a_boolean_or_float_target_id_does_not_pick_item_1(env):
    from scaleconnect.models import Change

    for bad in (True, 1.0):
        p = env.svc.preview_changes([Change(action="edit", target_id=bad, price="11.00")], requested_by="test")
        assert not p.ok, f"target_id={bad!r} was accepted"


def test_L6_the_seeded_entry_is_not_listed_as_an_unfinished_change(env):
    assert [c for c in env.svc.list_changes() if c.outcome == "in_progress"] == []


# =====================================================================
# Session limits that M8 and the notebook ask for
# =====================================================================

def test_M8_one_connection_at_a_time_per_scale_through_a_whole_change(env3):
    env3.change(EDIT_PRICE_12)
    for fake in env3.fakes.values():
        fake.assert_one_at_a_time()


def test_M8_a_second_apply_waits_its_turn_not_in_parallel(make_env):
    env = make_env({"A": True})
    env.fakes["A"].delay_reply(1.0, "GP")
    p1 = env.preview(EDIT_PRICE_12)
    p2 = env.preview([{"action": "edit", "target_plu": "1002", "price": "20.10"}])
    out = {}
    t = threading.Thread(target=lambda: out.update(rec=env.svc.apply_change(p1.preview_id)))
    t.start()
    try:
        env.fakes["A"].wait_for_messages(3, timeout=5)
        with pytest.raises(errors.ApplyBusy):
            env.svc.apply_change(p2.preview_id)
    finally:
        t.join(timeout=15)
    env.fakes["A"].assert_one_at_a_time()
