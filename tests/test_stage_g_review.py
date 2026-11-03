"""The stage G review's findings, against fake scales on localhost. Made up items only.

Each test reads what the fake scale received, not just what the service said.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from scaleconnect import cli, errors
from scaleconnect.catalog import Catalog
from scaleconnect.config import load_config
from scaleconnect.models import Item, is_placeholder, placeholder_record
from service_env import SEED

STAMP = "2026-01-01 00:00:00"
MOVE = [{"action": "edit", "target_plu": "1001", "plu": "1999"}]
DELETE_1002 = [{"action": "delete", "target_plu": "1002"}]


def row(item: Item) -> tuple[str, ...]:
    return tuple(item.to_fields())


def placeholder_row(item_id: int) -> tuple[str, ...]:
    return row(placeholder_record(item_id, STAMP))


def seed_of(count: int, price: str = "10.00") -> list[Item]:
    return [Item(i, f"{1000 + i}", f"{1000 + i}", f"Test {i}", price, timestamp=STAMP) for i in range(1, count + 1)]


def placeholders_on(fake) -> list[int]:
    return sorted(i for i, r in fake.items.items() if is_placeholder(Item.from_fields(list(r))))


def kinds_of(rec, index: int = 0) -> list[str]:
    return [d.kind for d in rec.scale_results[index].drift]


# ---------------------------------------------------------------- H1: gap fill trusts the end of a read


@pytest.mark.parametrize("read_first", [True, False], ids=["preview read", "no preview read"])
@pytest.mark.parametrize("target", ["1012", "1006", "1003"], ids=["far past the end", "just past the end", "inside the read"])
def test_a_read_that_ends_early_refuses_the_scale_and_touches_nothing(make_env, read_first, target):
    env = make_env({"A": True}, seed=seed_of(12))
    fake = env.fakes["A"]
    before = fake.items
    fake.end_read_early(1)  # ids 1 to 5, then LP00End, though it holds 1 to 12
    p = env.preview([{"action": "edit", "target_plu": target, "price": "11.00"}], read_scales=read_first)
    if read_first:
        assert [d.kind for d in p.drift] == ["read_ended_early"] and p.drift[0].ack_token is None
        assert "gap_filled" not in {w.code for w in p.warnings} and "missing_on_scale" not in {w.code for w in p.warnings}
    rec = env.svc.apply_change(p.preview_id, True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not rec.success
    assert kinds_of(rec) == ["read_ended_early"]
    message = rec.scale_results[0].message
    assert "differs from the list" in message and "ended at id 5" in message and "Reload" in message and "person" in message
    assert not fake.messages_of("GP") and fake.items == before


def test_an_empty_read_of_a_full_scale_turns_nothing_into_placeholders(make_env):
    env = make_env({"A": True}, seed=seed_of(30))
    fake = env.fakes["A"]
    fake.end_read_early(0)
    rec = env.change([{"action": "edit", "target_plu": "1030", "price": "11.00"}])
    assert env.statuses(rec) == {"A": "drift_refused"} and placeholders_on(fake) == [] and not fake.messages_of("GP")


def test_a_delete_on_a_read_that_ends_early_is_refused(make_env):
    env = make_env({"A": True}, seed=seed_of(12))
    fake = env.fakes["A"]
    fake.end_read_early(1)
    rec = env.change([{"action": "delete", "target_plu": "1009"}], ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and placeholders_on(fake) == []


def test_a_scale_holding_nothing_is_refused_and_pointed_at_reload(make_env):
    env = make_env({"A": True}, seed=seed_of(7), preload=False)
    fake = env.fakes["A"]
    rec = env.change([{"action": "edit", "target_plu": "1007", "price": "12.00"}])
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    assert "Reload" in rec.scale_results[0].message


def test_a_fill_never_goes_over_an_id_the_catalog_lists(make_env):
    # B never took id 4, and the catalog knows that, so its read ending at 3 is fine. An add at 5 must not overwrite 4.
    env = make_env({"A": True, "B": False}, seed=seed_of(3))
    env.change([{"action": "add", "plu": "1004", "description": "Four", "price": "1.00"}])
    assert env.catalog.scale_states(4)["B"]["belief"] == "absent"
    env.set_write_enabled("B", True)
    rec = env.change([{"action": "add", "plu": "1005", "description": "Five", "price": "1.00"}], read_scales=True, ack=True)
    assert env.statuses(rec)["A"] == "verified" and env.statuses(rec)["B"] == "drift_refused"
    assert kinds_of(rec, 1) == ["fill_blocked"] and not env.fakes["B"].messages_of("GP")


def test_a_fill_never_goes_over_a_deleted_id_a_scale_may_still_hold(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    a, b = env.fakes["A"], env.fakes["B"]
    b.error_on_ids(4)
    env.change([{"action": "delete", "target_plu": "1004"}], ack=True)
    b.clear_faults()
    b.remove(4)  # B now reads as ending at 3, though the catalog notes it may hold id 4
    b.messages.clear()
    rec = env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}], read_scales=True)
    assert rec.changes[0].item_id == 5, "id 4 is still held"
    assert env.statuses(rec) == {"A": "verified", "B": "drift_refused"} and kinds_of(rec, 1) == ["read_ended_early"]
    assert not b.messages_of("GP") and placeholders_on(b) == [] and placeholders_on(a) == [4]


def test_a_short_read_does_not_free_a_deleted_id(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(12))
    b = env.fakes["B"]
    b.error_on_ids(9)
    env.change([{"action": "delete", "target_plu": "1009"}], ack=True)
    b.clear_faults()
    b.end_read_early(1)
    assert env.svc.compare_scales(["B"])[0].status == "compared"
    assert env.catalog.scale_states(9)["B"]["belief"] == "unknown", "the scale still holds it"
    assert 9 not in env.catalog.free_ids(500)
    assert b.item(9)[2] == "1009"


def test_a_placeholder_read_at_the_id_does_free_it_even_on_a_read_that_ends_early(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(12))
    b = env.fakes["B"]
    b.error_on_ids(2)
    env.change(DELETE_1002, ack=True)
    b.clear_faults()
    b.preload(placeholder_row(2))
    b.end_read_early(1)
    env.svc.compare_scales(["B"])
    assert env.catalog.scale_states(2)["B"]["belief"] == "absent"


def test_a_whole_read_showing_nothing_at_the_id_frees_it(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(6))
    b = env.fakes["B"]
    b.error_on_ids(2)
    env.change(DELETE_1002, ack=True)
    b.clear_faults()
    b.remove(2)
    env.svc.compare_scales(["B"])
    assert env.catalog.scale_states(2)["B"]["belief"] == "absent"


def test_a_settle_to_the_catalog_past_the_end_of_a_read_is_refused(make_env):
    env = make_env({"A": True}, seed=seed_of(12), preload=False)
    fake = env.fakes["A"]
    fake.preload(*[row(i) for i in seed_of(3)])
    p = env.svc.preview_settle_drift(scale="A", take="catalog", plu="1012")
    assert not p.ok and p.errors[0].code == "read_ended_early" and "read_ended_early" in [d.kind for d in p.drift]
    assert not fake.messages_of("GP")


def test_an_add_whose_reply_was_lost_does_not_block_its_own_retry(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    fake.stall("GP", times=1, apply=False)
    rec = env.change([{"action": "add", "plu": "1888", "description": "New", "price": "2.00"}])
    assert env.statuses(rec) == {"A": "unknown"} and fake.item(4) is None
    assert env.catalog.get_by_id(4) is not None, "it may be on the scale, so it keeps its id"
    rec = env.change([{"action": "edit", "target_plu": "1888", "price": "2.50"}])
    assert env.statuses(rec) == {"A": "verified"} and fake.item(4)[4] == "2.50"


# ---------------------------------------------------------------- H2: a deleted item's PLU is not free


def lands_on_a_only(env):
    env.fakes["B"].error_on_ids(2)
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "rejected"}
    env.fakes["B"].clear_faults()


def test_a_deleted_plu_stays_taken_until_every_scale_has_confirmed(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(6))
    lands_on_a_only(env)
    add = [{"action": "add", "plu": "1002", "description": "Brand New", "price": "5.00"}]
    p = env.svc.preview_changes(add)
    assert not p.ok and p.errors[0].code == "duplicate_plu" and "B may still sell it" in p.errors[0].message
    edit = env.svc.preview_changes([{"action": "edit", "target_plu": "1003", "plu": "1002"}])
    assert not edit.ok and edit.errors[0].code == "duplicate_plu"

    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec)["B"] == "verified"
    rec = env.change(add)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"}, "free once every scale confirmed"


def test_a_scale_that_is_down_keeps_the_plu_taken(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(6), reply_timeout_seconds=0.3, connect_timeout_seconds=0.3, connect_attempts=1)
    env.fakes["B"].stop_listening()
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "unreachable"}
    p = env.svc.preview_changes([{"action": "add", "plu": "1002", "description": "New", "price": "5.00"}])
    assert not p.ok and p.errors[0].code == "duplicate_plu"
    env.fakes["B"].start_listening()


def test_a_repeat_of_a_delete_by_plu_finds_the_deleted_item_and_no_new_product(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(6))
    lands_on_a_only(env)
    p = env.preview(DELETE_1002)
    assert p.differences[0].item_id == 2 and p.differences[0].before.description == "Test 2"


def test_a_plu_on_both_a_live_and_a_held_deleted_item_needs_the_id(make_env):
    # A catalog from before this rule can already hold this.
    env = make_env({"A": True, "B": True}, seed=seed_of(6))
    lands_on_a_only(env)
    env.catalog.upsert_item(Item(7, "1002", "1002", "New Product", "5.00", timestamp=STAMP))
    p = env.svc.preview_changes(DELETE_1002)
    assert not p.ok and p.errors[0].code == "duplicate_plu" and "target_id" in p.errors[0].message
    assert env.svc.preview_changes([{"action": "delete", "target_id": 7}]).ok


def test_a_delete_refused_for_a_plu_on_another_id_stays_held_after_a_later_read(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(6))
    b = env.fakes["B"]
    b.preload(placeholder_row(2), row(seed_of(2)[1].replace(id=7)))
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec)["B"] == "drift_refused"
    env.svc.compare_scales(["B"])
    assert env.catalog.scale_states(2)["B"]["belief"] == "unknown", "B still sells it, from id 7"
    assert 2 not in env.catalog.free_ids(500)
    assert env.svc.preview_changes(DELETE_1002).ok, "the delete can still be repeated"
    p = env.svc.preview_changes([{"action": "add", "plu": "1002", "description": "New", "price": "5.00"}])
    assert not p.ok


def test_settling_to_a_scale_cannot_take_a_plu_that_is_not_free(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(6))
    lands_on_a_only(env)
    env.fakes["B"].preload(row(seed_of(3)[2].replace(plu="1002", code="1002")))
    p = env.svc.preview_settle_drift(scale="B", take="scale", plu="1003")
    assert not p.ok and p.errors[0].code == "duplicate_plu"


# ---------------------------------------------------------------- H3: reload and an out of date catalog


def stale_price_env(make_env):
    seed = seed_of(4)
    env = make_env({"A": True}, seed=[seed[0].replace(price="12.34"), *seed[1:]])
    env.fakes["A"].preload(row(seed[0].replace(price="12.56")))
    return env


def test_reload_lists_a_stale_price_from_the_last_read_and_refuses(make_env):
    env = stale_price_env(make_env)
    fake = env.fakes["A"]
    env.svc.compare_scales(["A"])
    fake.messages.clear()
    connections = fake.connection_count
    with pytest.raises(errors.ReloadDifferences) as caught:
        env.svc.reload_scale("A", requested_by="test")
    assert fake.connection_count == connections and not fake.messages, "no scale contact"
    assert caught.value.code == "reload_differences" and caught.value.exit_code == 6
    assert "12.56" in caught.value.message and "12.34" in caught.value.message
    assert [d.item_id for d in caught.value.differences] == [1]
    assert caught.value.differences[0].fields["price"].to_dict() == {"before": "12.56", "after": "12.34"}
    assert caught.value.to_dict()["differences"][0]["read_at"]
    assert fake.item(1)[4] == "12.56"


def test_reload_says_plainly_when_nothing_is_known(env):
    with pytest.raises(errors.ReloadDifferences) as caught:
        env.svc.reload_scale("A", requested_by="test")
    assert "Nothing is known" in caught.value.message and caught.value.not_known == [1, 2, 3]
    assert env.silent("A")


def test_reload_with_a_clean_last_read_needs_no_second_flag(env):
    env.svc.compare_scales(["A"])
    result = env.svc.reload_scale("A", requested_by="test")
    assert result.ok and result.differences == () and result.not_known == ()
    assert "No price differs" in result.message


def test_reload_names_the_prices_it_changed(make_env):
    env = stale_price_env(make_env)
    env.svc.compare_scales(["A"])
    result = env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    assert result.ok and env.fakes["A"].item(1)[4] == "12.34"
    assert 'PLU "1001" from "12.56" to "12.34"' in result.message
    assert result.differences[0].fields["price"].before == "12.56"
    started = next(e for e in env.catalog.read_log(result.change_id) if e["event"] == "started")
    assert started["payload"]["differences"][0]["item_id"] == 1


def test_reload_says_when_it_could_not_name_a_price(env):
    result = env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    assert result.ok and result.not_known == (1, 2, 3) and "Nobody knew" in result.message


@pytest.mark.parametrize(
    "held, words",
    [
        (lambda: row(seed_of(1)[0].replace(description="Other Name")), "description"),
        (lambda: row(seed_of(1)[0].replace(plu="1777", code="1777")), "plu"),
    ],
)
def test_reload_lists_description_and_plu_differences_too(make_env, held, words):
    env = make_env({"A": True}, seed=seed_of(4))
    env.fakes["A"].preload(held())
    env.svc.compare_scales(["A"])
    with pytest.raises(errors.ReloadDifferences) as caught:
        env.svc.reload_scale("A", requested_by="test")
    assert words in caught.value.differences[0].fields


def test_reload_lists_an_item_it_would_remove_and_one_it_would_put_back(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(5))
    a, b = env.fakes["A"], env.fakes["B"]
    b.error_on_ids(2)
    env.change(DELETE_1002, ack=True)
    b.clear_faults()
    b.remove(3)
    env.svc.compare_scales(["B"])
    with pytest.raises(errors.ReloadDifferences) as caught:
        env.svc.reload_scale("B", requested_by="test")
    found = {d.item_id: d for d in caught.value.differences}
    assert "placeholder takes its place" in found[2].message and "nothing there" in found[3].message
    assert a.item(2) is not None


def write_config(tmp_path, fake):
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[settings]
state_dir = "{(tmp_path / 'state').as_posix()}"
reply_timeout_seconds = 1
connect_timeout_seconds = 1
connect_attempts = 1
connect_retry_wait_seconds = 0
read_pace_seconds = 0
read_back_window_seconds = 2
read_back_retry_seconds = 0.05
scalelink_db_copy = "not_available_here"

[[scales]]
name = "A"
address = "127.0.0.1"
port = {fake.port}
serial = "{fake.serial}"
write_enabled = true
""",
        encoding="utf-8",
    )
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.import_items(SEED)
    return str(path)


def test_cli_reload_lists_differences_and_exits_6_with_nothing_sent(tmp_path, make_fake, capsys):
    fake = make_fake()
    code = cli.main(["--config", write_config(tmp_path, fake), "reload", "--scale", "A", "--yes"])
    out = json.loads(capsys.readouterr().out)
    assert code == 6 and out["error"]["code"] == "reload_differences" and out["error"]["not_known"] == [1, 2, 3]
    assert fake.nothing_received


def test_cli_help_names_the_flag(capsys):
    with pytest.raises(SystemExit):
        cli.main(["reload", "--help"])
    assert "--accept-differences" in capsys.readouterr().out


# ---------------------------------------------------------------- M2: the same PLU at a later id


def test_a_plain_edit_is_refused_when_its_plu_is_at_a_later_id(make_env):
    env = make_env({"A": True}, seed=seed_of(12))
    fake = env.fakes["A"]
    fake.preload(row(seed_of(1)[0].replace(id=11, price="7.00")))
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "12.00"}])
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP")
    assert "plu_on_other_id" in kinds_of(rec)
    assert all(d.ack_token is None for d in rec.scale_results[0].drift if d.kind == "plu_on_other_id")
    assert fake.item(1)[4] == "10.00" and fake.item(11)[4] == "7.00"


def test_every_write_reads_the_whole_table_once_and_a_price_read_back_stops_at_the_id(make_env):
    env = make_env({"A": True}, seed=seed_of(30))
    fake = env.fakes["A"]
    env.change([{"action": "edit", "target_plu": "1030", "price": "10.50"}])  # the first write
    fake.messages.clear()
    env.change([{"action": "edit", "target_plu": "1002", "price": "10.50"}])
    kinds = fake.kinds()
    write_at = kinds.index("RI")
    assert kinds[:write_at].count("LP") == 1, "one read first"
    assert kinds[:write_at].count("Ok") == 6, "all 6 blocks, and the end"
    assert kinds[write_at:].count("LP") == 1 and kinds[write_at:].count("Ok") == 0, "the read back stops in the first block"


# ---------------------------------------------------------------- M3: settle on a lagging scale


def lagging_on_b(env):
    env.fakes["B"].error_on_ids(1)
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "rejected"}
    env.fakes["B"].clear_faults()


def test_settling_to_a_scale_still_on_the_old_plu_is_refused(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    lagging_on_b(env)
    p = env.svc.preview_settle_drift(scale="B", take="scale", plu="1999")
    assert not p.ok and p.errors[0].code == "plu_change_not_finished"
    assert "Repeat that edit" in p.errors[0].message
    assert p.drift[0].unfinished_plu_change is True and p.drift[0].ack_token is None
    assert env.catalog.get_by_id(1).plu == "1999" and env.catalog.old_plus_in_use() == {"1001": 1}


def test_settling_to_the_catalog_there_keeps_its_hold_and_says_what_to_do(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    lagging_on_b(env)
    p = env.svc.preview_settle_drift(scale="B", take="catalog", plu="1999")
    assert not p.ok and p.errors[0].code == "plu_edit_held" and "Repeat that edit" in p.errors[0].message


def test_a_read_shows_the_structured_field(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    lagging_on_b(env)
    finding = {c.scale: c for c in env.svc.compare_scales()}["B"].differences[0]
    assert finding.kind == "different_product" and finding.unfinished_plu_change is True
    held = {r.scale: r for r in env.svc.read_scale_items(ids=[1])}["B"].records[0]
    assert held.difference.unfinished_plu_change is True
    assert finding.to_dict()["unfinished_plu_change"] is True


def test_the_structured_field_ignores_a_record_that_only_reads_like_the_sentence(env):
    fake = env.fakes["A"]
    fake.preload(row(SEED[1].replace(plu="repeat that edit", code="repeat that edit")))
    finding = env.svc.compare_scales()[0].differences[0]
    assert finding.kind == "different_product" and finding.unfinished_plu_change is False


def test_a_move_note_counts_only_if_the_code_matches_too(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    lagging_on_b(env)
    b = env.fakes["B"]
    b.preload(row(seed_of(1)[0].replace(code="1777")))  # the old PLU, under another Code
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec)["B"] == "drift_refused" and kinds_of(rec, 1) == ["different_product"]
    assert rec.scale_results[1].drift[0].unfinished_plu_change is False
    assert b.item(1)[1:3] == ("1777", "1001")


# ---------------------------------------------------------------- M5: finishing a PLU change


def test_finishing_needs_an_acknowledgement_and_names_the_scales_with_or_without_a_read(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    lagging_on_b(env)
    p = env.preview(MOVE)
    assert [(w.code, w.requires_ack) for w in p.warnings] == [("plu_change", True)]
    assert "PLU 1001 on B" in p.warnings[0].message
    p = env.preview(MOVE, read_scales=True)
    lagging = [w for w in p.warnings if w.code == "plu_change_lagging"]
    assert len(lagging) == 1 and "B still holds" in lagging[0].message


def test_finishing_where_the_scale_has_moved_after_all_says_nothing_about_it_lagging(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    lagging_on_b(env)
    env.fakes["B"].preload(row(env.catalog.get_by_id(1)))
    p = env.preview(MOVE, read_scales=True)
    assert "plu_change_lagging" not in {w.code for w in p.warnings} and p.warnings[0].code == "plu_change"


# ---------------------------------------------------------------- B3: a PLU change with only lost replies


def lost_on_a_rejected_on_b(make_env, *, landed=True):
    env = make_env({"A": True, "B": True}, seed=seed_of(4), reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1, apply=landed)
    env.fakes["B"].error_on_ids(1)
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "unknown", "B": "rejected"} and rec.outcome == "unknown"
    env.fakes["B"].clear_faults()
    assert env.catalog.get_by_id(1).plu == "1001", "nothing is known to have landed, so the catalog stays"
    return env


@pytest.mark.parametrize(
    "change",
    [
        {"action": "add", "plu": "1999", "description": "Other", "price": "3.00"},
        {"action": "edit", "target_plu": "1002", "plu": "1999"},
        {"action": "add", "plu": "1001", "description": "Other", "price": "3.00"},
    ],
    ids=["add of the new plu", "edit onto the new plu", "add of the old plu"],
)
def test_both_plus_stay_taken_with_no_read_needed(make_env, change):
    env = lost_on_a_rejected_on_b(make_env)
    env.fakes["A"].messages.clear()
    p = env.svc.preview_changes([change])
    assert not p.ok and p.errors[0].code == "duplicate_plu"
    assert not env.fakes["A"].messages, "the catalog knows this with no scale contact"


def test_only_the_scale_with_the_lost_reply_is_noted(make_env):
    env = lost_on_a_rejected_on_b(make_env)
    assert set(env.catalog.plu_moves(1, "A")) == {"1999"} and env.catalog.plu_moves(1, "B") == {}


def test_a_read_showing_the_new_plu_nowhere_frees_it(make_env):
    env = lost_on_a_rejected_on_b(make_env, landed=False)
    add = [{"action": "add", "plu": "1999", "description": "Other", "price": "3.00"}]
    assert not env.svc.preview_changes(add).ok
    env.svc.compare_scales(["A"])
    assert env.catalog.old_plus_in_use() == {}
    assert env.svc.preview_changes(add).ok


def test_a_read_showing_the_new_plu_keeps_it_taken(make_env):
    env = lost_on_a_rejected_on_b(make_env, landed=True)
    finding = env.svc.compare_scales(["A"])[0].differences[0]
    assert finding.kind == "different_product" and finding.unfinished_plu_change is True
    p = env.svc.preview_changes([{"action": "add", "plu": "1999", "description": "Other", "price": "3.00"}])
    assert not p.ok and p.errors[0].code == "duplicate_plu"


def test_a_read_that_ended_early_settles_nothing(make_env):
    env = lost_on_a_rejected_on_b(make_env, landed=False)
    env.fakes["A"].end_read_early(0)
    env.svc.compare_scales(["A"])
    assert set(env.catalog.plu_moves(1, "A")) == {"1999"}


@pytest.mark.parametrize("landed", [True, False])
def test_a_repeat_settles_it_whichever_way_it_went(make_env, landed):
    env = lost_on_a_rejected_on_b(make_env, landed=landed)
    rec = env.change(MOVE, ack=True, read_scales=True)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"} and rec.scale_results[0].drift == ()
    assert env.catalog.get_by_id(1).plu == "1999" and env.catalog.old_plus_in_use() == {}
    for fake in env.fakes.values():
        assert fake.item(1)[1:3] == ("1999", "1999")
    assert env.svc.preview_changes([{"action": "add", "plu": "1001", "description": "Other", "price": "3.00"}]).ok


def test_it_can_be_put_back_by_asking_for_the_plu_the_catalog_has(make_env):
    env = lost_on_a_rejected_on_b(make_env, landed=True)
    p = env.preview([{"action": "edit", "target_plu": "1001", "plu": "1001"}])
    assert [w.code for w in p.warnings] == ["plu_change"] and "PLU 1999 on A" in p.warnings[0].message
    rec = env.svc.apply_change(p.preview_id, True)
    assert env.statuses(rec)["A"] == "verified" and env.fakes["A"].item(1)[2] == "1001"
    assert env.catalog.old_plus_in_use() == {}


def test_a_price_does_not_land_on_a_scale_that_may_hold_the_new_plu(make_env):
    env = lost_on_a_rejected_on_b(make_env, landed=True)
    fake = env.fakes["A"]
    before = fake.item(1)
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    assert env.statuses(rec)["A"] == "drift_refused" and fake.item(1) == before
    assert rec.scale_results[0].drift[0].unfinished_plu_change is True


def test_settling_to_that_scale_is_refused(make_env):
    env = lost_on_a_rejected_on_b(make_env, landed=True)
    p = env.svc.preview_settle_drift(scale="A", take="scale", plu="1001")
    assert not p.ok and p.errors[0].code == "plu_change_not_finished"


def test_unfinished_asks_for_the_new_plu_when_the_catalog_has_not_moved(make_env):
    env = lost_on_a_rejected_on_b(make_env)
    found = env.svc.list_unfinished()
    assert [(u.scale, u.kind, u.plu, u.old_plus) for u in found] == [("A", "plu_change", "1001", ("1999",))]
    assert found[0].repeat == {"action": "edit", "target_plu": "1001", "plu": "1999"}
    assert "may have taken" in found[0].message
    rec = env.change([found[0].repeat], ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"} and env.svc.list_unfinished() == []


def test_a_landed_change_still_notes_the_old_plu_and_not_the_new(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4), reply_timeout_seconds=0.4)
    env.fakes["B"].stall("GP", times=1, apply=False)
    rec = env.change(MOVE, ack=True)
    assert env.statuses(rec) == {"A": "verified", "B": "unknown"}
    assert set(env.catalog.plu_moves(1, "B")) == {"1001"} and env.catalog.plu_moves(1, "A") == {}
    assert env.svc.list_unfinished()[0].repeat == {"action": "edit", "target_plu": "1999", "plu": "1999"}


def test_a_catalog_file_from_schema_3_gets_the_new_column_and_keeps_its_notes(tmp_path):
    path = tmp_path / "old.sqlite3"
    with Catalog(path) as catalog:
        catalog.import_items(SEED)
    db = sqlite3.connect(path)
    db.execute("DROP TABLE plu_moves")
    db.execute("CREATE TABLE plu_moves (scale TEXT NOT NULL, item_id INTEGER NOT NULL, old_plu TEXT NOT NULL, "
               "record TEXT NOT NULL, noted_at TEXT NOT NULL, PRIMARY KEY (scale, item_id, old_plu))")
    db.execute("INSERT INTO plu_moves VALUES ('A', 1, '1777', ?, 't')", (json.dumps(SEED[0].to_fields()),))
    db.execute("PRAGMA user_version = 3")
    db.commit()
    db.close()
    with Catalog(path) as catalog:
        assert catalog.all_plu_moves() == [{"scale": "A", "item_id": 1, "old_plu": "1777", "trying": 0}]
    with Catalog(path) as catalog:  # and again, with the column there
        assert catalog._conn().execute("PRAGMA user_version").fetchone()[0] == 4


# ---------------------------------------------------------------- M6: what is unfinished


def test_unfinished_lists_each_delete_and_plu_change_per_scale_with_no_contact(make_env):
    env = make_env({"A": True, "B": True, "C": True}, seed=seed_of(6))
    env.fakes["B"].error_on_ids(1, 2)
    env.change([{"action": "edit", "target_plu": "1001", "plu": "1999"}, {"action": "delete", "target_plu": "1002"}], ack=True)
    env.fakes["B"].clear_faults()
    env.fakes["B"].messages.clear()
    connections = env.fakes["B"].connection_count
    found = env.svc.list_unfinished()
    assert env.fakes["B"].connection_count == connections
    assert {(u.scale, u.kind, u.item_id) for u in found} == {("B", "delete", 2), ("B", "plu_change", 1)}
    by_kind = {u.kind: u for u in found}
    assert by_kind["plu_change"].old_plus == ("1001",) and by_kind["plu_change"].plu == "1999"
    assert by_kind["delete"].plu == "1002"
    json.dumps([u.to_dict() for u in found])


def test_each_unfinished_entry_gives_a_change_that_finishes_it(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(6))
    env.fakes["B"].error_on_ids(1, 2)
    env.change([{"action": "edit", "target_plu": "1001", "plu": "1999"}, {"action": "delete", "target_plu": "1002"}], ack=True)
    env.fakes["B"].clear_faults()
    rec = env.change([u.repeat for u in env.svc.list_unfinished()], ack=True, scales=["B"])
    assert env.statuses(rec) == {"B": "verified"}
    assert env.svc.list_unfinished() == []


def test_nothing_unfinished_lists_nothing(env):
    env.change(DELETE_1002, ack=True)
    assert env.svc.list_unfinished() == []


# ---------------------------------------------------------------- the may_be_frozen marker


FROZEN = dict(fake_options=dict(freeze_lp_at_id=7), reply_timeout_seconds=0.3, connect_timeout_seconds=0.3, connect_attempts=1)


def test_the_marker_is_on_each_kind_of_result(make_env):
    env = make_env({"A": True}, seed=seed_of(12), **FROZEN)
    fake = env.fakes["A"]
    assert env.svc.compare_scales()[0].may_be_frozen is True
    fake.reboot()
    assert env.svc.read_scale_items()[0].may_be_frozen is True
    fake.reboot()
    assert env.svc.mark_orphans_from_read("A").may_be_frozen is True
    fake.reboot()
    p = env.svc.preview_changes([{"action": "edit", "target_plu": "1001", "price": "11.00"}], read_scales=True)
    assert [w.may_be_frozen for w in p.warnings if w.code == "scale_read_failed"] == [True]
    fake.reboot()
    settle = env.svc.preview_settle_drift(scale="A", take="scale", plu="1001")
    assert settle.errors[0].may_be_frozen is True
    fake.reboot()
    rec = env.svc.apply_change(p.preview_id)
    assert rec.scale_results[0].may_be_frozen is True and rec.scale_results[0].to_dict()["may_be_frozen"] is True
    assert env.svc.get_change(rec.change_id).scale_results[0].may_be_frozen is True, "it survives the log"
    fake.reboot()


def test_the_marker_is_on_a_scale_that_stopped_answering_the_read_back(make_env):
    env = make_env({"A": True}, reply_timeout_seconds=0.3, read_back_window_seconds=0.4, read_back_retry_seconds=0.1)
    fake = env.fakes["A"]
    fake.stall_read(block=0, times=None)
    fake.faults[-1].match = lambda m: m.kind == "LP" and fake.messages_of("GP")
    rec = env.change(DELETE_1002, ack=True)
    assert rec.scale_results[0].may_be_frozen is True and env.statuses(rec) == {"A": "accepted_not_verified"}


def test_the_marker_is_on_a_reload_whose_read_failed(make_env):
    env = make_env({"A": True}, seed=seed_of(12), preload=False, **FROZEN)
    result = env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    assert result.may_be_frozen is True and env.fakes["A"].frozen
    env.fakes["A"].reboot()


def test_the_marker_stays_off_when_a_scale_answers_badly_or_is_fine(make_env):
    env = make_env({"A": True})
    fake = env.fakes["A"]
    fake.raw_reply(b"XX99\t\n0\r", kind="LP", times=1)
    assert env.svc.compare_scales()[0].may_be_frozen is False
    assert env.svc.compare_scales()[0].may_be_frozen is False
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    assert rec.scale_results[0].may_be_frozen is False


# ---------------------------------------------------------------- the lows


def test_L1_a_reload_says_truly_that_it_tried_inside_the_window(make_env):
    env = make_env({"A": True}, seed=seed_of(4), preload=False, reply_timeout_seconds=0.2, read_back_window_seconds=0.5, read_back_retry_seconds=0.05)
    fake = env.fakes["A"]
    fake.stall_read(block=0, times=None)
    result = env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    assert fake.kinds().count("LP") > 1, "this is the case the old words were false for"
    assert "Not tried again" not in result.message and "stopped trying once the read back window ended" in result.message


def test_L3_a_write_over_a_placeholder_from_a_delete_with_no_result_needs_a_token(make_env):
    env = make_env({"A": True}, seed=seed_of(6), reply_timeout_seconds=0.4)
    fake = env.fakes["A"]
    fake.stall("GP", times=1, apply=True)
    assert env.statuses(env.change(DELETE_1002, ack=True)) == {"A": "unknown"}
    fake.messages.clear()
    edit = [{"action": "edit", "target_plu": "1002", "price": "11.00"}]
    rec = env.change(edit)
    finding = rec.scale_results[0].drift[0]
    assert env.statuses(rec) == {"A": "drift_refused"} and finding.kind == "delete_may_have_landed" and finding.ack_token
    assert not fake.messages_of("GP") and fake.item(2)[3] == "UNUSED"
    p = env.preview(edit, read_scales=True)
    assert [d.kind for d in p.drift] == ["delete_may_have_landed"]
    rec = env.svc.apply_change(p.preview_id, acknowledge_drift=[finding.ack_token])
    assert env.statuses(rec) == {"A": "verified"} and fake.item(2)[4] == "11.00"


def test_L3_repeating_the_delete_needs_no_token(make_env):
    env = make_env({"A": True}, seed=seed_of(6), reply_timeout_seconds=0.4)
    env.fakes["A"].stall("GP", times=1, apply=True)
    env.change(DELETE_1002, ack=True)
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "verified"} and rec.scale_results[0].drift == ()


def test_L5_a_plu_edit_onto_a_plu_held_there_under_another_name_is_a_different_product(make_env):
    env = make_env({"A": True}, seed=seed_of(4))
    fake = env.fakes["A"]
    fake.preload(row(Item(2, "2777", "2777", "Made Up Sausage", "30.00", timestamp=STAMP)))
    edit = [{"action": "edit", "target_plu": "1002", "plu": "2777", "price": "12.00"}]
    p = env.preview(edit, read_scales=True)
    assert [(d.kind, d.ack_token) for d in p.drift] == [("different_product", None)]
    rec = env.svc.apply_change(p.preview_id, True)
    assert env.statuses(rec) == {"A": "drift_refused"} and fake.item(2)[3:5] == ("Made Up Sausage", "30.00")
    assert env.catalog.get_by_id(2).plu == "1002"


def test_L8_finishing_on_just_the_lagging_scale_leaves_the_others_matching(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    lagging_on_b(env)
    stamp_before = env.catalog.get_by_id(1).timestamp
    clock_moved = env.svc._clock
    from datetime import timedelta

    env.svc._clock = lambda: clock_moved() + timedelta(minutes=5)
    rec = env.change(MOVE, ack=True, scales=["B"])
    assert env.statuses(rec) == {"B": "verified"}
    assert env.catalog.get_by_id(1).timestamp == stamp_before == env.fakes["B"].item(1)[12]
    assert {c.scale: c.differences for c in env.svc.compare_scales()}["A"] == ()
    rec = env.change([{"action": "edit", "target_plu": "1999", "price": "11.00"}])
    assert env.statuses(rec) == {"A": "verified", "B": "verified"}


# ---------------------------------------------------------------- M7: what the review found no test for


def test_a_delete_reads_the_whole_table_on_a_later_write_too(make_env):
    """The first write reads it all for rule 5 anyway, so this has to be a later one."""
    env = make_env({"A": True}, seed=seed_of(12))
    fake = env.fakes["A"]
    env.change([{"action": "edit", "target_plu": "1003", "price": "10.50"}])
    assert env.svc._read_marker("dump", "A") is not None
    fake.preload(row(seed_of(2)[1].replace(id=11)))  # past the first block
    fake.messages.clear()
    rec = env.change(DELETE_1002, ack=True)
    assert env.statuses(rec) == {"A": "drift_refused"} and not fake.messages_of("GP"), "refused before any write"
    assert "plu_on_other_id" in kinds_of(rec)


def test_reload_stops_if_the_write_flag_goes_off_after_it_began(make_env):
    env = make_env({"A": True})
    fake = env.fakes["A"]
    real = env.svc._current_scale
    turned_off = lambda name: __import__("dataclasses").replace(real(name), write_enabled=False)  # noqa: E731
    env.svc._current_scale = turned_off
    result = env.svc.reload_scale("A", accept_differences=True, requested_by="test")
    assert result.status == "skipped_not_write_enabled" and not result.ok
    assert fake.kinds() == ["CX"], "the identity check and nothing else"


def test_a_read_changes_no_belief_about_a_live_item(make_env):
    env = make_env({"A": True}, seed=seed_of(4))
    env.change([{"action": "edit", "target_plu": "1002", "price": "10.50"}])
    assert env.catalog.scale_states(2)["A"]["belief"] == "present"
    env.fakes["A"].remove(2)
    env.svc.compare_scales()
    assert env.catalog.scale_states(2)["A"]["belief"] == "present", "only a deleted item's id can go absent this way"


def test_the_old_plu_coming_back_waits_for_every_scale(make_env):
    env = make_env({"A": True, "B": True}, seed=seed_of(4))
    env.fakes["B"].error_on_ids(2)
    env.change(DELETE_1002, ack=True)
    env.fakes["B"].clear_faults()
    add = [{"action": "add", "plu": "1002", "description": "Back Again", "price": "2.00"}]
    assert not env.svc.preview_changes(add).ok
    env.change(DELETE_1002, ack=True)
    rec = env.change(add)
    assert env.statuses(rec) == {"A": "verified", "B": "verified"}
    for fake in env.fakes.values():
        assert fake.item(2)[3] == "Back Again"
