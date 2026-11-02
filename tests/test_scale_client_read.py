"""ScaleClient identity check and reads (scale.py) against the fake scale.

Covers DESIGN.md "Each scale is the truth about itself", steps 1, 2 and 6, and
PROTOCOL.md section 3. Made up items only.
"""

from __future__ import annotations

import time

import pytest

from scaleconnect.config import ScaleConfig, Settings
from scaleconnect.errors import NotReadEnabled, NotWriteEnabled
from scaleconnect.models import Item, ScaleStatus
from scaleconnect.scale import ScaleClient


def item(i, price="1.00"):
    return Item(i, f"{1000 + i}", f"{1000 + i}", f"Item {i}", price, timestamp="2026-01-01 00:00:00")


def load(fake, n):
    fake.preload(*[tuple(item(i).to_fields()) for i in range(1, n + 1)])


def make(fake, *, write=False, read=True, serial="from_fake", **kw):
    base = dict(reply_timeout_seconds=2.0, connect_timeout_seconds=2.0, connect_attempts=1,
                read_pace_seconds=0.0, read_back_retry_seconds=0.1, read_back_window_seconds=5.0)
    base.update(kw)
    cfg = ScaleConfig("T", fake.host, fake.port, write_enabled=write, read_enabled=read,
                      serial=fake.serial if serial == "from_fake" else serial)
    return ScaleClient(cfg, Settings(**base), sleep=lambda s: None)


# ---------- identity ----------

def test_identity_matches(fake_scale):
    r = make(fake_scale).identify()
    assert r.ok and r.serial == fake_scale.serial
    assert fake_scale.kinds() == ["CX"]


def test_identity_mismatch_is_wrong_scale_and_nothing_else_is_sent(fake_scale):
    r = make(fake_scale, serial="OTHER-1").identify()
    assert not r.ok and r.status == ScaleStatus.WRONG_SCALE
    assert fake_scale.kinds() == ["CX"]
    # Stage F L8: a message can reach a page with no login, so it holds no serial.
    assert r.serial == fake_scale.serial
    assert fake_scale.serial not in r.message and "OTHER-1" not in r.message


def test_no_serial_in_the_config_means_nothing_is_sent(fake_scale):
    r = make(fake_scale, serial=None).identify()
    assert r.status == ScaleStatus.WRONG_SCALE
    assert fake_scale.nothing_received


def test_identity_unreachable(make_fake):
    fake = make_fake()
    fake.stop_listening()
    r = make(fake).identify()
    assert r.status == ScaleStatus.UNREACHABLE and fake.nothing_received


def test_identity_that_gets_no_answer_is_read_failed(fake_scale):
    fake_scale.stall(kind="CX")
    r = make(fake_scale, reply_timeout_seconds=0.4).identify()
    assert r.status == ScaleStatus.READ_FAILED


def test_a_reply_that_is_not_an_identity_is_not_trusted(fake_scale):
    fake_scale.raw_reply(frame_cx_junk(), kind="CX")
    r = make(fake_scale).identify()
    assert not r.ok


def frame_cx_junk():
    from fake_scale import frame

    return frame(b"CX00")


# ---------- guards ----------

def test_a_scale_with_neither_flag_is_never_contacted(fake_scale):
    c = make(fake_scale, write=False, read=False)
    for call in (c.identify, c.read_items):
        with pytest.raises(NotReadEnabled):
            call()
    assert fake_scale.nothing_received


def test_read_enabled_does_not_allow_a_write(fake_scale):
    with pytest.raises(NotWriteEnabled):
        make(fake_scale, write=False, read=True).send_items([item(1)])
    assert fake_scale.nothing_received


def test_a_text_true_does_not_enable_reads(fake_scale):
    cfg = ScaleConfig("T", fake_scale.host, fake_scale.port, False, read_enabled="true", serial=fake_scale.serial)
    with pytest.raises(NotReadEnabled):
        ScaleClient(cfg, Settings(), sleep=lambda s: None).read_items()
    assert fake_scale.nothing_received


def test_a_read_sends_only_cx_lp_and_ok_and_never_lg(fake_scale):
    load(fake_scale, 12)
    make(fake_scale).read_items()
    assert set(fake_scale.kinds()) <= {"CX", "LP", "Ok"}
    assert not any(m.raw.startswith(b"LG") for m in fake_scale.messages)
    assert all(m.raw == m.raw for m in fake_scale.messages)  # exact bytes are checked by the fake
    fake_scale.assert_clean()


# ---------- reading ----------

def test_a_full_read_returns_every_item_exactly(fake_scale):
    load(fake_scale, 12)
    r = make(fake_scale).read_items()
    assert r.ok and r.complete
    assert sorted(r.items) == list(range(1, 13))
    assert r.items[7] == item(7)


def test_a_read_needs_no_ri_or_bx_and_closes_politely(fake_scale):
    load(fake_scale, 3)
    make(fake_scale).read_items()
    assert "RI" not in fake_scale.kinds() and "bX01XX" not in fake_scale.kinds()


def test_reading_stops_early_once_the_id_is_reached(fake_scale):
    load(fake_scale, 30)
    r = make(fake_scale).read_items(up_to_id=3)
    assert r.ok and not r.complete
    assert 3 in r.items
    assert fake_scale.kinds().count("Ok") == 0  # id 3 is in the first block


def test_reading_to_a_later_id_reads_only_as_many_blocks_as_needed(fake_scale):
    load(fake_scale, 30)
    r = make(fake_scale).read_items(up_to_id=12)
    assert 12 in r.items
    assert fake_scale.kinds().count("Ok") == 2  # blocks 2 and 3


def test_id_0_is_not_in_a_read(fake_scale):
    fake_scale.preload(tuple(item(0).to_fields()), tuple(item(1).to_fields()))
    r = make(fake_scale).read_items()
    assert sorted(r.items) == [1]


def test_an_empty_table_reads_as_complete_and_empty(fake_scale):
    r = make(fake_scale).read_items()
    assert r.ok and r.complete and r.items == {}


def test_ASSUMPTION_gaps_are_read_as_the_records_that_exist(fake_scale):
    fake_scale.preload(*[tuple(item(i).to_fields()) for i in (1, 3, 9, 40)])
    r = make(fake_scale).read_items()
    assert sorted(r.items) == [1, 3, 9, 40]


def test_a_stalled_read_is_read_failed_and_stops(fake_scale):
    load(fake_scale, 12)
    fake_scale.stall_read(block=1, times=None)
    r = make(fake_scale, reply_timeout_seconds=0.4).read_items()
    assert not r.ok and r.status == ScaleStatus.READ_FAILED
    assert fake_scale.kinds().count("Ok") == 1  # one Ok, never a second try


def test_a_dropped_read_is_read_failed(fake_scale):
    load(fake_scale, 12)
    fake_scale.drop_read(block=1)
    r = make(fake_scale).read_items()
    assert r.status == ScaleStatus.READ_FAILED


def test_a_read_that_never_answers_is_read_failed(fake_scale):
    fake_scale.stall_read(block=0, times=None)
    r = make(fake_scale, reply_timeout_seconds=0.4).read_items()
    assert r.status == ScaleStatus.READ_FAILED


def test_read_messages_keep_the_configured_pace(fake_scale):
    load(fake_scale, 20)
    slept = []
    cfg = ScaleConfig("T", fake_scale.host, fake_scale.port, False, read_enabled=True, serial=fake_scale.serial)
    ScaleClient(cfg, Settings(read_pace_seconds=0.2), sleep=slept.append).read_items()
    assert slept == [0.2] * 4  # before each Ok: blocks 2, 3, 4 and then the end


def test_the_default_read_pace_is_two_tenths_of_a_second():
    assert Settings().read_pace_seconds == 0.2


def test_read_blocks_never_reach_the_reply_list(fake_scale):
    load(fake_scale, 5)
    c = make(fake_scale, write=True, read=True)
    c.read_items()
    rep = c.send_items([item(2, "2.50")])
    assert not any("LP0" in r or "LP00End" in r for r in rep.replies)


def test_a_lg_reply_by_surprise_is_not_kept(fake_scale):
    load(fake_scale, 2)
    fake_scale.raw_reply(b"LG01SECRETVALUES\t\n3\r", kind="LP")
    c = make(fake_scale, write=True, read=True)
    c.read_items()
    rep = c.send_items([item(2, "2.50")])
    assert not any("SECRET" in r for r in rep.replies)


# ---------- the read back after a write ----------

def test_a_patient_read_waits_out_the_gap_after_a_write(make_fake):
    fake = make_fake(post_write_refusal_seconds=1.0)
    c = make(fake, write=True, read=True)
    rep = c.send_items([item(1, "3.00")])
    assert rep.status == ScaleStatus.ACCEPTED_NOT_VERIFIED
    t = time.monotonic()
    r = make(fake, write=True, read=True).read_items(up_to_id=1, patient=True)
    assert r.ok and r.items[1].price == "3.00"
    assert fake.refusal_windows == 1
    assert time.monotonic() - t >= 0.3  # it did have to wait


def test_a_patient_read_gives_up_after_the_window(make_fake):
    fake = make_fake(post_write_refusal_seconds=30.0)
    c = make(fake, write=True, read_back_window_seconds=0.6, connect_timeout_seconds=0.2)
    c.send_items([item(1)])
    r = c.read_items(patient=True)
    assert r.status == ScaleStatus.READ_FAILED


def test_the_silent_gap_is_a_read_failure_not_a_hang(make_fake):
    fake = make_fake(post_write_refusal_seconds=2.0, post_write_mode="silent")
    c = make(fake, write=True, reply_timeout_seconds=0.4, read_back_window_seconds=0.5)
    c.send_items([item(1)])
    t = time.monotonic()
    r = c.read_items(patient=True)
    assert time.monotonic() - t < 4
    assert r.status in (ScaleStatus.READ_FAILED, ScaleStatus.UNREACHABLE)
