"""The stage G recheck's findings, against fake scales on localhost. Made up items only."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from scaleconnect import catalog as catalog_module
from scaleconnect import cli, errors
from scaleconnect.catalog import Catalog, CatalogError
from scaleconnect.config import Config, load_config
from scaleconnect.models import Change, Item, placeholder_record
from scaleconnect.service import RELOAD_READ_MAX_AGE, ScaleConnectService
from service_env import SEED, scale_config

STAMP = "2026-01-01 00:00:00"
DELETE_1002 = [{"action": "delete", "target_plu": "1002"}]
PRICE_2 = [{"action": "edit", "target_id": 2, "price": "10.50"}]


def row(item: Item) -> tuple[str, ...]:
    return tuple(item.to_fields())


def placeholder_row(item_id: int) -> tuple[str, ...]:
    return row(placeholder_record(item_id, STAMP))


def seed_of(count: int) -> list[Item]:
    return [Item(i, f"{1000 + i}", f"{1000 + i}", f"Test {i}", "10.00", timestamp=STAMP) for i in range(1, count + 1)]


def later(env, **delta) -> None:
    real = env.svc._clock
    env.svc._clock = lambda: real() + timedelta(**delta)


def service_for(env, *names):
    scales = tuple(scale_config(n, env.fakes[n], True) for n in names)
    return ScaleConnectService(Config(scales=scales, settings=env.settings), env.catalog, **env._svc_kw)


# ---------------------------------------------------------------- H3: how old, and which fields


def test_the_limit_is_fifteen_minutes():
    assert RELOAD_READ_MAX_AGE == timedelta(minutes=15)


def test_a_clean_read_past_the_limit_counts_as_nothing_known(env):
    env.svc.compare_scales(["A"])
    fake = env.fakes["A"]
    fake.messages.clear()
    later(env, minutes=16)
    with pytest.raises(errors.ReloadDifferences) as caught:
        env.svc.reload_scale("A", requested_by="test")
    assert caught.value.not_known == [1, 2, 3] and not fake.messages
    assert "more than 15 minutes old" in caught.value.message and "Its last read was at" in caught.value.message
    assert caught.value.to_dict()["last_read_at"] == caught.value.last_read_at and caught.value.last_read_at


def test_a_clean_read_inside_the_limit_needs_no_second_flag_and_the_report_gives_its_time(env):
    env.svc.compare_scales(["A"])
    later(env, minutes=14)
    result = env.svc.reload_scale("A", requested_by="test")
    assert result.ok and result.not_known == () and result.last_read_at
    assert f"before this was at {result.last_read_at}" in result.message
    assert result.to_dict()["last_read_at"] == result.last_read_at


def test_a_price_changed_in_scalelink_after_an_old_read_no_longer_slips_through(make_env):
    env = make_env({"A": True}, seed=seed_of(4))
    env.svc.compare_scales(["A"])
    fake = env.fakes["A"]
    fake.preload(row(seed_of(1)[0].replace(price="12.56")))  # ScaleLink sent this. The last read cannot know
    later(env, hours=3)
    with pytest.raises(errors.ReloadDifferences):
        env.svc.reload_scale("A", requested_by="test")
    assert fake.item(1)[4] == "12.56"


def test_an_old_read_is_still_listed_and_named_when_accepted(make_env):
    env = make_env({"A": True}, seed=seed_of(4))
    fake = env.fakes["A"]
    fake.preload(row(seed_of(1)[0].replace(price="12.56")))
    env.svc.compare_scales(["A"])
    later(env, hours=3)
    result = env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    assert result.ok and [d.item_id for d in result.differences] == [1] and result.not_known == (1, 2, 3, 4)
    assert 'PLU "1001" from "12.56" to "10.00"' in result.message and "Nobody knew" in result.message


def test_only_the_ids_a_fresh_read_reached_count_as_known(make_env):
    env = make_env({"A": True}, seed=seed_of(12))
    env.svc.compare_scales(["A"])
    later(env, hours=1)
    env.svc.read_scale_items(ids=[1])  # the first block only, so ids 1 to 5
    with pytest.raises(errors.ReloadDifferences) as caught:
        env.svc.reload_scale("A", requested_by="test")
    assert caught.value.not_known == list(range(6, 13))


@pytest.mark.parametrize(
    "changes, field, words",
    [
        (dict(fields_7_12=("0", "1", "0", "0", "0", "0")), "fields_7_12", "sold by"),
        (dict(fields_7_12=("1", "1", "0", "0.139", "0", "0")), "fields_7_12", "tax"),
        (dict(fields_14_20=("1", "0", "0", "0", "0", "0", "0")), "fields_14_20", "other settings"),
        (dict(field6=" "), "field6", "field 6"),
        (dict(code="1777"), "code", "code"),
    ],
)
def test_every_field_that_matters_refuses_and_is_named(make_env, changes, field, words):
    env = make_env({"A": True}, seed=seed_of(4))
    env.fakes["A"].preload(row(seed_of(1)[0].replace(**changes)))
    env.svc.compare_scales(["A"])
    with pytest.raises(errors.ReloadDifferences) as caught:
        env.svc.reload_scale("A", requested_by="test")
    assert list(caught.value.differences[0].fields) == [field]
    assert words in caught.value.differences[0].message


def test_a_difference_in_the_time_alone_is_not_a_difference(make_env):
    env = make_env({"A": True}, seed=seed_of(4))
    env.fakes["A"].preload(row(seed_of(1)[0].replace(timestamp="2026-02-02 02:02:02")))
    env.svc.compare_scales(["A"])
    assert env.svc.reload_scale("A", requested_by="test").differences == ()


def test_the_report_says_other_fields_changed_too(make_env):
    env = make_env({"A": True}, seed=seed_of(4))
    env.fakes["A"].preload(row(seed_of(1)[0].replace(fields_7_12=("0", "1", "0", "0", "0", "0"))))
    env.svc.compare_scales(["A"])
    result = env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    assert "Other fields changed at id 1" in result.message and env.fakes["A"].item(1)[6] == "1"


# ---------------------------------------------------------------- N3: a healthy scale stuck


def orphan_on_a(make_env, **kw):
    env = make_env({"A": True, "B": True}, seed=seed_of(3), **kw)
    env.fakes["A"].preload(*[placeholder_row(i) for i in range(4, 8)], row(Item(8, "1777", "1777", "Old Test", "1.00", timestamp=STAMP)))
    assert env.svc.mark_orphans_from_read("A").marked == (8,)
    return env, [{"action": "add", "plu": "1888", "description": "New", "price": "2.00", "replace_orphan_id": 8}]


def test_an_add_over_an_orphan_while_a_scale_is_off_does_not_block_it(make_env):
    env, add = orphan_on_a(make_env)
    env.set_write_enabled("B", False)
    rec = env.change(add, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "skipped_not_write_enabled"}
    assert env.catalog.scale_states(8)["B"]["belief"] == "absent"
    env.set_write_enabled("B", True)
    rec = env.change(PRICE_2)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"} and env.fakes["B"].item(2)[4] == "10.50"


def test_an_add_over_an_orphan_while_a_scale_is_down_does_not_block_it(make_env):
    env, add = orphan_on_a(make_env, reply_timeout_seconds=0.3, connect_timeout_seconds=0.3, connect_attempts=1)
    env.fakes["B"].stop_listening()
    assert env.statuses(env.change(add, ack=True)) == {"A": "verified", "B": "unreachable"}
    env.fakes["B"].start_listening()
    assert env.statuses(env.change(PRICE_2)) == {"A": "verified", "B": "verified"}


def test_a_row_written_before_this_fix_does_not_block_either(make_env):
    env, add = orphan_on_a(make_env, reply_timeout_seconds=0.3, connect_timeout_seconds=0.3, connect_attempts=1)
    env.fakes["B"].stop_listening()
    rec = env.change(add, ack=True)
    env.fakes["B"].start_listening()
    env.catalog.record_scale_state(8, "B", "unreachable", rec.change_id, "t", belief="unknown")  # what the old code recorded
    assert env.statuses(env.change(PRICE_2)) == {"A": "verified", "B": "verified"}


def test_a_scale_joining_the_config_after_an_add_it_never_saw_is_not_blocked(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(3))
    only_a = service_for(env, "A")
    p = only_a.preview_changes([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    only_a.apply_change(p.preview_id)
    assert env.fakes["A"].item(4)[2] == "1888" and env.fakes["B"].item(4) is None
    assert env.catalog.scale_states(4).get("B") is None
    assert env.statuses(env.change(PRICE_2)) == {"A": "verified", "B": "verified"}
    rec = env.change([{"action": "edit", "target_plu": "1888", "price": "2.50"}])
    assert env.statuses(rec) == {"A": "verified", "B": "verified"} and env.fakes["B"].item(4)[2:5] == ("1888", "New", "2.50")


def test_a_scale_an_add_left_out_is_not_blocked(env3):
    add = [{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}]
    env3.change(add, ack=True, scales=["A"])
    assert env3.catalog.scale_states(4)["B"]["belief"] == "absent"
    rec = env3.change(PRICE_2, ack=True, scales=["B", "C"])
    assert env3.statuses(rec) == {"B": "verified", "C": "verified"}


def test_a_read_that_really_ended_early_still_refuses_on_a_seeded_id(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(12))
    only_a = service_for(env, "A")
    p = only_a.preview_changes([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    only_a.apply_change(p.preview_id)  # id 13 never went to B. That must not excuse ids 6 to 12
    env.fakes["B"].end_read_early(1)
    rec = env.change(PRICE_2)
    assert env.statuses(rec)["B"] == "drift_refused" and "ended at id 5" in rec.scale_results[1].message
    assert not env.fakes["B"].messages_of("GP")


def test_a_read_ending_below_an_add_the_scale_took_still_refuses(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(5))
    env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    assert env.catalog.scale_states(6)["B"]["belief"] == "present"
    env.fakes["B"].messages.clear()
    env.fakes["B"].end_read_early(1)
    rec = env.change(PRICE_2)
    assert env.statuses(rec)["B"] == "drift_refused" and not env.fakes["B"].messages_of("GP")


def test_a_read_ending_below_an_add_the_scale_said_ok_to_still_refuses(make_env):
    env = make_env({"A": True}, seed=seed_of(5), reply_timeout_seconds=0.3, read_back_window_seconds=0.3, read_back_retry_seconds=0.1)
    fake = env.fakes["A"]
    fake.stall_read(block=0, times=None)
    fake.faults[-1].match = lambda m: m.kind == "LP" and fake.messages_of("GP")
    rec = env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    assert env.statuses(rec) == {"A": "accepted_not_verified"}
    fake.clear_faults()
    fake.remove(6)
    assert env.statuses(env.change(PRICE_2)) == {"A": "drift_refused"}


def test_a_noted_orphan_still_counts_when_the_add_over_it_did_not_land(make_env):
    env, add = orphan_on_a(make_env, reply_timeout_seconds=0.3, connect_timeout_seconds=0.3, connect_attempts=1)
    a = env.fakes["A"]
    a.stop_listening()
    p = env.preview(add, read_scales=True)
    assert env.statuses(env.svc.apply_change(p.preview_id, True))["A"] == "unreachable"
    a.start_listening()
    a.end_read_early(1)  # ids 1 to 5, though it holds the orphan at 8
    rec = env.change(PRICE_2)
    assert env.statuses(rec)["A"] == "drift_refused" and "ended at id 5" in rec.scale_results[0].message
    assert a.item(8)[2] == "1777"


def test_a_later_add_after_a_missed_one_says_settling_can_help(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(3))
    env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}], ack=True, scales=["A"])
    rec = env.change([{"action": "add", "plu": "1889", "description": "Next", "price": "2.00"}], read_scales=True)
    assert env.statuses(rec)["B"] == "drift_refused" and "Settle those items" in rec.scale_results[1].message
    p = env.svc.preview_settle_drift(scale="B", take="catalog", plu="1888")
    assert p.ok
    assert env.statuses(env.svc.apply_change(p.preview_id, True, acknowledge_drift=[p.drift[0].ack_token])) == {"B": "verified"}


# ---------------------------------------------------------------- N4: a config changed under a running service


def write_config(path, tmp_path, fakes, *, extra="", flags=None):
    flags = flags or {}
    body = [
        f'[settings]\nstate_dir = "{(tmp_path / "state").as_posix()}"\nreply_timeout_seconds = 1\nconnect_timeout_seconds = 1\n'
        f'connect_attempts = 1\nconnect_retry_wait_seconds = 0\nread_pace_seconds = 0\nread_back_window_seconds = 2\n'
        f'read_back_retry_seconds = 0.05\nscalelink_db_copy = "not_available_here"\n{extra}'
    ]
    for name, fake in fakes.items():
        body.append(f'[[scales]]\nname = "{name}"\naddress = "127.0.0.1"\nport = {fake.port}\nserial = "{fake.serial}"\nwrite_enabled = {flags.get(name, "true")}\n')
    path.write_text("\n".join(body), encoding="utf-8")


@pytest.fixture
def on_a_file(tmp_path, make_fake):
    path = tmp_path / "config.toml"
    a, b = make_fake(serial="S-A"), make_fake(serial="S-B")
    write_config(path, tmp_path, {"A": a})
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.import_items(SEED)
    a.preload(*[row(i) for i in SEED])
    b.preload(*[row(i) for i in SEED])
    service = ScaleConnectService.open(path)
    yield service, path, tmp_path, a, b
    service.close()


def test_a_scale_added_to_the_file_refuses_writes_until_a_restart(on_a_file):
    service, path, tmp_path, a, b = on_a_file
    assert service.config_changed() is None
    p = service.preview_changes(PRICE_2)
    assert p.ok
    write_config(path, tmp_path, {"A": a, "B": b})
    assert "Restart the web page and the MCP server together" in service.config_changed()
    with pytest.raises(errors.ConfigChanged) as caught:
        service.apply_change(p.preview_id)
    assert caught.value.code == "config_changed" and caught.value.exit_code == 8 and "nothing was sent" in caught.value.message
    assert not a.messages_of("GP") and b.nothing_received
    refused = service.preview_changes(PRICE_2)
    assert not refused.ok and refused.errors[0].code == "config_changed"
    assert service.preview_settle_drift(scale="A", take="catalog", id=1).errors[0].code == "config_changed"
    with pytest.raises(errors.ConfigChanged):
        service.reload_scale("A", accept_differences=True, requested_by="test")
    assert service.apply_running() is False and not a.messages_of("GP")

    fresh = ScaleConnectService.open(path)
    try:
        rec = fresh.apply_change(fresh.preview_changes(PRICE_2).preview_id)
        assert {r.scale: r.status for r in rec.scale_results} == {"A": "verified", "B": "verified"}
    finally:
        fresh.close()


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda text: text.replace("reply_timeout_seconds = 1", "reply_timeout_seconds = 2"), id="a setting"),
        pytest.param(lambda text: text.replace('serial = "S-A"', 'serial = "S-OTHER"'), id="a serial"),
        pytest.param(lambda text: text + '\n[auth]\npin = "a-new-pin-value"\n', id="the PIN"),
    ],
)
def test_other_changes_to_the_file_refuse_too(on_a_file, edit):
    service, path, *_ = on_a_file
    path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
    assert service.config_changed() is not None
    assert "Restart" in service.preview_changes(PRICE_2).errors[0].message


def test_a_flag_turned_on_refuses(tmp_path, make_fake):
    path = tmp_path / "config.toml"
    a = make_fake(serial="S-A")
    write_config(path, tmp_path, {"A": a}, flags={"A": "false"})
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.import_items(SEED)
    service = ScaleConnectService.open(path)
    try:
        write_config(path, tmp_path, {"A": a}, flags={"A": "true"})
        assert service.config_changed() is not None
    finally:
        service.close()


def test_turning_a_flag_off_still_works_at_once_with_no_restart(on_a_file):
    service, path, tmp_path, a, _ = on_a_file
    p = service.preview_changes(PRICE_2)
    write_config(path, tmp_path, {"A": a}, flags={"A": "false"})
    assert service.config_changed() is None
    with pytest.raises(errors.PreviewStale):  # the write flags changed since the preview, as before
        service.apply_change(p.preview_id)
    fresh = service.preview_changes(PRICE_2)
    rec = service.apply_change(fresh.preview_id)
    assert rec.scale_results[0].status == "skipped_not_write_enabled" and not a.messages_of("GP")


def test_a_comment_in_the_file_changes_nothing(on_a_file):
    service, path, *_ = on_a_file
    path.write_text(path.read_text(encoding="utf-8") + "\n# a note\n", encoding="utf-8")
    assert service.config_changed() is None


def test_the_command_line_loads_the_file_fresh_each_time_so_it_is_never_refused(on_a_file, capsys):
    service, path, tmp_path, a, b = on_a_file
    write_config(path, tmp_path, {"A": a, "B": b})
    assert service.config_changed() is not None
    assert cli.main(["--config", str(path), "preview", "--json", json.dumps(PRICE_2)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# ---------------------------------------------------------------- N1: a delete that names what it expects


def test_a_delete_naming_the_plu_it_expects_will_not_hit_what_now_holds_the_id(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    env.fakes["B"].error_on_ids(2)
    env.change(DELETE_1002, ack=True)
    env.fakes["B"].clear_faults()
    old_button = [{"action": "delete", "target_id": 2, "expected_plu": "1002"}]
    assert env.svc.preview_changes(old_button).ok, "still that delete, so it may go"
    env.change(old_button, ack=True)
    env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    assert env.catalog.get_by_id(2).plu == "1888", "the next add took that id"
    for fake in env.fakes.values():
        fake.messages.clear()
    p = env.svc.preview_changes(old_button)
    assert not p.ok and p.errors[0].code == "target_changed" and p.errors[0].field == "expected_plu"
    assert "now holds PLU 1888, not PLU 1002" in p.errors[0].message
    assert all(not fake.messages for fake in env.fakes.values())


def test_a_delete_with_the_right_plu_goes(env):
    rec = env.change([{"action": "delete", "target_id": 2, "expected_plu": "1002"}], ack=True)
    assert env.statuses(rec) == {"A": "verified"}


def test_a_finished_delete_naming_its_plu_finds_no_item(env):
    env.change(DELETE_1002, ack=True)
    p = env.svc.preview_changes([{"action": "delete", "target_id": 2, "expected_plu": "1002"}])
    assert not p.ok and p.errors[0].code == "item_not_found"


@pytest.mark.parametrize(
    "change",
    [
        {"action": "add", "plu": "1888", "description": "New", "price": "2.00", "expected_plu": "1888"},
        {"action": "edit", "target_plu": "1001", "price": "2.00", "expected_plu": "1001"},
    ],
)
def test_only_a_delete_takes_expected_plu(env, change):
    p = env.svc.preview_changes([change])
    assert not p.ok and p.errors[0].code == "invalid_change" and p.errors[0].field == "expected_plu"


def test_expected_plu_must_be_text_and_round_trips():
    with pytest.raises(errors.ValidationFailed):
        Change.from_dict({"action": "delete", "target_id": 2, "expected_plu": 1002})
    change = Change.from_dict({"action": "delete", "target_id": 2, "expected_plu": "1002"})
    assert change.to_dict() == {"action": "delete", "target_id": 2, "expected_plu": "1002"}


# ---------------------------------------------------------------- N7, N8, N12


def old_schema_file(path) -> None:
    with Catalog(path) as catalog:
        catalog.import_items(SEED)
    db = sqlite3.connect(path)
    db.execute("DROP TABLE plu_moves")
    db.execute("CREATE TABLE plu_moves (scale TEXT NOT NULL, item_id INTEGER NOT NULL, old_plu TEXT NOT NULL, "
               "record TEXT NOT NULL, noted_at TEXT NOT NULL, PRIMARY KEY (scale, item_id, old_plu))")
    db.execute("PRAGMA user_version = 3")
    db.commit()
    db.close()


def test_N7_a_busy_file_stops_the_upgrade_out_loud_and_the_next_open_finishes_it(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    old_schema_file(path)
    monkeypatch.setattr(catalog_module, "_BUSY_SECONDS", 0.05)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises((CatalogError, sqlite3.OperationalError)):
            Catalog(path)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 3, "it must not claim a version it did not reach"
    check.close()
    with Catalog(path) as catalog:
        assert catalog.all_plu_moves() == []
        assert catalog._conn().execute("PRAGMA user_version").fetchone()[0] == 4


def test_N8_a_scale_no_longer_in_the_config_does_not_hold_a_plu_for_ever(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    env.fakes["B"].error_on_ids(2)
    env.change(DELETE_1002, ack=True)
    add = [{"action": "add", "plu": "1002", "description": "Back", "price": "2.00"}]
    assert not env.svc.preview_changes(add).ok, "B is in the config, so it still counts"
    only_a = service_for(env, "A")
    assert only_a.list_unfinished() == []
    assert only_a.preview_changes(add).ok


def test_N12_a_clean_read_after_a_lost_reply_says_present(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1, apply=False)
    assert env.statuses(env.change(PRICE_2)) == {"A": "unknown"}
    assert env.catalog.scale_states(2)["A"]["belief"] == "unknown"
    env.svc.compare_scales()
    assert env.catalog.scale_states(2)["A"]["belief"] == "present"


def test_N12_a_read_showing_something_else_leaves_it_unknown(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1, apply=True)
    assert env.statuses(env.change(PRICE_2)) == {"A": "unknown"}
    env.svc.compare_scales()
    assert env.catalog.scale_states(2)["A"]["belief"] == "unknown"
