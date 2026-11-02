"""ScaleClient (scale.py), the write path, against the fake scale on localhost.

Reads and identity are in test_scale_client_read.py. Everything here uses made
up items. No real scale is contacted.
"""

from __future__ import annotations

import time

import pytest

from scaleconnect import protocol
from scaleconnect.config import ScaleConfig, Settings
from scaleconnect.errors import NotWriteEnabled, ProtocolError
from scaleconnect.models import Item, ScaleStatus
from scaleconnect.scale import ScaleClient

KNOWN_KINDS = {"RI", "bX01XX", "GP", "GPF0", "CX", "LP", "Ok"}


def make_item(i: int, price: str = "9.99", desc: str | None = None, **kw) -> Item:
    plu = f"{1000 + i}"
    return Item(
        id=i,
        code=plu,
        plu=plu,
        description=desc if desc is not None else f"Test Item {i}",
        price=price,
        timestamp="2026-01-01 00:00:00",
        **kw,
    )


def settings(**kw) -> Settings:
    base = dict(
        reply_timeout_seconds=2.0,
        connect_timeout_seconds=2.0,
        connect_attempts=3,
        connect_retry_wait_seconds=0.0,
    )
    base.update(kw)
    return Settings(**base)


def client(fake, *, write_enabled=True, sleeps=None, serial="from_fake", **kw) -> ScaleClient:
    cfg = ScaleConfig(
        name="TESTSCALE", address=fake.host, port=fake.port, write_enabled=write_enabled,
        serial=fake.serial if serial == "from_fake" else serial,
    )
    sleep = sleeps.append if sleeps is not None else (lambda s: None)
    return ScaleClient(cfg, settings(**kw), sleep=sleep)


def only_known(fake):
    assert set(fake.kinds()) <= KNOWN_KINDS, fake.kinds()
    assert all(m.check_ok for m in fake.messages)
    assert "LG" not in fake.kinds()


# ---------- the normal send ----------

def test_one_item_is_accepted_not_verified(fake_scale):
    item = make_item(7)
    rep = client(fake_scale).send_items([item])
    assert rep.status == ScaleStatus.ACCEPTED_NOT_VERIFIED
    assert [o.status for o in rep.items] == ["accepted_not_verified"]
    assert rep.attempts == 1
    assert fake_scale.item(7) == tuple(item.to_fields())
    only_known(fake_scale)


def test_the_sequence_is_cx_then_ri_then_bx_on_a_new_connection_then_gp_then_gpf0(fake_scale):
    client(fake_scale).send_items([make_item(1)])
    assert fake_scale.kinds() == ["CX", "RI", "bX01XX", "GP", "GPF0"]
    cx, ri, bx, gp, fin = fake_scale.messages
    assert len({cx.conn_id, ri.conn_id, bx.conn_id}) == 3
    assert bx.conn_id == gp.conn_id == fin.conn_id
    fake_scale.assert_one_at_a_time()


def test_twelve_items_go_in_batches_of_five_in_one_session(fake_scale):
    items = [make_item(i) for i in range(1, 13)]
    rep = client(fake_scale).send_items(items)
    assert rep.status == ScaleStatus.ACCEPTED_NOT_VERIFIED
    gps = fake_scale.messages_of("GP")
    assert [len(m.records) for m in gps] == [5, 5, 2]
    assert len({m.conn_id for m in gps}) == 1
    assert fake_scale.kinds().count("RI") == 1
    assert fake_scale.accepted_ids() == list(range(1, 13))
    only_known(fake_scale)


def test_every_field_goes_out_exactly_as_the_item_holds_it(fake_scale):
    item = make_item(
        9, field6=" ", fields_7_12=("0", "1", "0", "0", "0", "0"),
        fields_14_20=("0", "0", "0", "0", "0", "0", "1"),
    )
    client(fake_scale).send_items([item])
    sent = fake_scale.item(9)
    assert len(sent) == 20
    assert sent == tuple(item.to_fields())
    assert sent[5] == " "
    assert sent[6] == "0"


def test_the_batch_size_setting_is_used(make_fake):
    fake = make_fake()
    client(fake, batch_size=2).send_items([make_item(i) for i in range(1, 6)])
    assert [len(m.records) for m in fake.messages_of("GP")] == [2, 2, 1]


def test_batches_above_5_are_not_allowed_in_the_config():
    from scaleconnect.errors import ConfigError

    with pytest.raises(ConfigError):
        Settings(batch_size=6)


# ---------- the scale says Error ----------

def test_error_on_a_record_is_rejected_even_though_gpf0_says_ok(fake_scale):
    fake_scale.error_on_ids(3)
    rep = client(fake_scale).send_items([make_item(3)])
    assert rep.status == ScaleStatus.REJECTED
    assert [o.status for o in rep.items] == ["rejected"]
    assert fake_scale.kinds()[-1] == "GPF0"  # the session was finished, and said GPF0Ok
    assert any("GPF0Ok" in r for r in rep.replies)
    assert fake_scale.items == {}
    assert rep.status not in (ScaleStatus.VERIFIED, ScaleStatus.ACCEPTED_NOT_VERIFIED)


def test_a_rejected_batch_stops_the_send_and_later_records_are_not_sent(fake_scale):
    fake_scale.error_on_ids(7)
    items = [make_item(i) for i in range(1, 13)]  # batches 1-5, 6-10, 11-12
    rep = client(fake_scale).send_items(items)
    by_id = {o.item_id: o.status for o in rep.items}
    assert all(by_id[i] == "accepted_not_verified" for i in range(1, 6))
    # M1: a five record batch got Error. Nobody knows if the scale kept the other four.
    assert all(by_id[i] == "unknown" for i in range(6, 11))
    assert all(by_id[i] == "not_sent" for i in (11, 12))
    assert rep.status != ScaleStatus.ACCEPTED_NOT_VERIFIED
    assert len(fake_scale.messages_of("GP")) == 2
    assert fake_scale.accepted_ids() == [1, 2, 3, 4, 5]


def test_a_single_record_batch_with_error_is_rejected(fake_scale):
    fake_scale.force_error("GP", times=1)
    rep = client(fake_scale).send_items([make_item(1)])
    assert rep.status == ScaleStatus.REJECTED
    assert len(fake_scale.messages_of("GP")) == 1
    assert fake_scale.items == {}


# ---------- timing ----------

def test_a_reply_after_3_25_seconds_still_succeeds_with_the_default_timeout(fake_scale):
    fake_scale.delay_reply(3.25, "GP")
    cfg = ScaleConfig("TESTSCALE", fake_scale.host, fake_scale.port, True, serial=fake_scale.serial)
    c = ScaleClient(cfg, Settings(), sleep=lambda s: None)
    t = time.monotonic()
    rep = c.send_items([make_item(1)])
    assert time.monotonic() - t >= 3.25
    assert rep.status == ScaleStatus.ACCEPTED_NOT_VERIFIED
    assert rep.attempts == 1
    assert len(fake_scale.messages_of("GP")) == 1


def test_the_slowest_scales_beat_wait_of_3_25_seconds_is_survived_with_defaults(make_fake):
    fake = make_fake(beat_stall_seconds=3.25)
    fake.trigger_beat()
    cfg = ScaleConfig("TESTSCALE", fake.host, fake.port, True, serial=fake.serial)
    rep = ScaleClient(cfg, Settings(), sleep=lambda s: None).send_items([make_item(1)])
    assert rep.status == ScaleStatus.ACCEPTED_NOT_VERIFIED


def test_a_beat_wait_longer_than_a_3_second_limit_would_fail_that_limit(make_fake):
    # ScaleLink's limit. This is why the reply wait floor exists.
    fake = make_fake(beat_stall_seconds=3.25)
    fake.trigger_beat()
    rep = client(fake, reply_timeout_seconds=3.0).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNKNOWN


def test_a_reply_slower_than_the_timeout_is_unknown_and_never_sent_again(fake_scale):
    fake_scale.delay_reply(1.5, "GP", times=1)
    sleeps = []
    rep = client(fake_scale, sleeps=sleeps, reply_timeout_seconds=0.5).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNKNOWN
    assert len(fake_scale.messages_of("GP")) == 1, "the record was sent again on its own"
    assert rep.attempts == 1
    assert fake_scale.kinds().count("bX01XX") == 1


def test_a_stall_gives_unknown_and_one_send(fake_scale):
    fake_scale.stall("GP", times=None)
    rep = client(fake_scale, reply_timeout_seconds=0.4).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNKNOWN
    assert rep.attempts == 1
    assert len(fake_scale.messages_of("GP")) == 1
    assert [o.status for o in rep.items] == ["unknown"]
    assert "unknown" in rep.message.lower()


def test_connection_dropped_before_the_reply_is_unknown(fake_scale):
    fake_scale.drop_before_reply("GP", times=None)
    rep = client(fake_scale).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNKNOWN
    assert fake_scale.item(1) is not None  # the scale did keep it, and we still say unknown
    assert len(fake_scale.messages_of("GP")) == 1


def test_connection_dropped_mid_send_is_unknown(fake_scale):
    fake_scale.drop_mid_send("GP", after_bytes=10, times=None)
    rep = client(fake_scale).send_items([make_item(i) for i in range(1, 4)])
    assert rep.status == ScaleStatus.UNKNOWN
    assert fake_scale.items == {}


def test_a_bad_check_character_on_the_reply_is_never_accepted(fake_scale):
    fake_scale.bad_reply_check("GP", times=None, where="trailer")
    rep = client(fake_scale).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNKNOWN


def test_a_wrong_echo_character_on_the_reply_is_never_accepted(fake_scale):
    fake_scale.bad_reply_check("GP", times=None, where="echo")
    rep = client(fake_scale).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNKNOWN


def test_one_batch_lost_marks_earlier_batches_unknown_too(fake_scale):
    # The reply to batch 2 never comes, so batch 1 was never closed out with GPF0Ok.
    fake_scale.stall("GP", times=None)
    fake_scale.faults[-1].match = lambda m: m.ids[0] == 6
    rep = client(fake_scale, reply_timeout_seconds=0.4).send_items([make_item(i) for i in range(1, 8)])
    assert {o.status for o in rep.items} == {"unknown"}
    assert rep.status == ScaleStatus.UNKNOWN


# ---------- connecting ----------

def test_refused_connection_means_unreachable_and_nothing_is_sent(make_fake):
    fake = make_fake()
    fake.stop_listening()
    rep = client(fake, connect_attempts=2).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNREACHABLE
    assert rep.attempts == 0
    assert fake.nothing_received
    assert {o.status for o in rep.items} <= {"unreachable", "not_sent"}


def test_connect_waits_grow_between_refused_attempts(make_fake):
    fake = make_fake()
    fake.stop_listening()
    sleeps = []
    client(fake, sleeps=sleeps, connect_attempts=3, connect_retry_wait_seconds=0.1).send_items([make_item(1)])
    assert sleeps == [0.1, 0.2]


def test_a_scale_that_refuses_for_a_moment_does_not_fail_the_send(make_fake):
    # This needs real waits. Windows retries a refused local connect by itself for about two
    # seconds, which hid that the other tests' helper skips waiting. Linux refuses at once.
    fake = make_fake()
    fake.refuse_for(1.0)
    cfg = ScaleConfig(name="TESTSCALE", address=fake.host, port=fake.port, write_enabled=True, serial=fake.serial)
    rep = ScaleClient(cfg, settings(connect_attempts=6, connect_retry_wait_seconds=0.5)).send_items([make_item(1)])
    assert rep.status == ScaleStatus.ACCEPTED_NOT_VERIFIED
    assert fake.item(1) is not None


def test_failing_the_opening_steps_is_unreachable_and_no_item_goes_out(fake_scale):
    fake_scale.bad_reply_check("bX01XX", times=None, where="trailer")
    rep = client(fake_scale).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNREACHABLE
    assert fake_scale.messages_of("GP") == []
    assert fake_scale.items == {}


def test_a_reset_during_the_opening_does_not_start_a_second_session(fake_scale):
    # M4: no churn. A failed opening is reported, not repeated. The drop comes after bX01XX
    # arrives, because a reset by connection count lands at a different step on Linux, where
    # it can arrive during the connect, which is then rightly retried.
    fake_scale.drop_before_reply("bX01XX", times=None)
    rep = client(fake_scale).send_items([make_item(1)])
    assert rep.status == ScaleStatus.UNREACHABLE
    assert fake_scale.messages_of("GP") == []
    assert fake_scale.kinds().count("bX01XX") == 1


# ---------- guards ----------

def test_not_write_enabled_sends_nothing_at_all(fake_scale):
    with pytest.raises(NotWriteEnabled):
        client(fake_scale, write_enabled=False).send_items([make_item(1)])
    assert fake_scale.nothing_received
    assert fake_scale.connection_count == 0


def test_write_enabled_defaults_to_off():
    assert ScaleConfig("X", "127.0.0.1").write_enabled is False


def test_a_text_true_does_not_enable_writes(fake_scale):
    cfg = ScaleConfig("X", fake_scale.host, fake_scale.port, "true", serial=fake_scale.serial)
    with pytest.raises(NotWriteEnabled):
        ScaleClient(cfg, settings(), sleep=lambda s: None).send_items([make_item(1)])
    assert fake_scale.nothing_received


@pytest.mark.parametrize("bad_id", [0, 501, 1000, -1])
def test_ids_outside_1_to_500_never_go_out(fake_scale, bad_id):
    with pytest.raises(ProtocolError):
        client(fake_scale).send_items([make_item(bad_id)])
    assert fake_scale.nothing_received


def test_id_500_goes_out(fake_scale):
    rep = client(fake_scale).send_items([make_item(500)])
    assert rep.status == ScaleStatus.ACCEPTED_NOT_VERIFIED
    assert fake_scale.accepted_ids() == [500]


@pytest.mark.parametrize(
    "desc",
    ["Tab\there", "Line\nbreak", "Return\rhere", "Café", "Smile ☺"],
)
def test_characters_that_break_the_message_never_go_out(fake_scale, desc):
    with pytest.raises(ProtocolError):
        client(fake_scale).send_items([make_item(1, desc=desc)])
    assert fake_scale.nothing_received


def test_one_bad_item_in_a_batch_stops_the_whole_send_before_any_byte(fake_scale):
    items = [make_item(1), make_item(2, desc="bad\tone"), make_item(3)]
    with pytest.raises(ProtocolError):
        client(fake_scale).send_items(items)
    assert fake_scale.nothing_received


def test_empty_send_is_refused_and_sends_nothing(fake_scale):
    with pytest.raises(ValueError):
        client(fake_scale).send_items([])
    assert fake_scale.nothing_received


# ---------- the driver only builds known messages ----------

def test_protocol_module_can_only_build_known_messages():
    assert protocol.build_ri() == b"RI\t\n4\r"
    assert protocol.build_open() == b"bX01XX\t\n1\r"
    assert protocol.build_finish() == b"GPF0\t\n8\r"
    public = {n for n in dir(protocol) if n.startswith("build_")}
    assert public == {
        "build_ri", "build_open", "build_finish", "build_item_batch",
        "build_identity", "build_read_start", "build_read_next",
    }
    assert protocol.build_identity() == b"CX\t\n0\r"
    assert protocol.build_read_start() == b"LP\t\n6\r"
    assert protocol.build_read_next() == b"Ok\t\n7\r"


def test_every_message_sent_in_a_busy_session_is_known_and_has_a_good_check(fake_scale):
    fake_scale.delay_reply(0.3, "GP")
    client(fake_scale).send_items([make_item(i, price=f"{i}.50") for i in range(1, 16)])
    only_known(fake_scale)
    assert fake_scale.violations == []
