"""Tests of the fake scale itself, using a bare socket client.

These pin down what the fake does, so that library tests built on it can
be trusted. The reply bytes below are protocol constants seen in captures
and written in docs/DESIGN.md. No product data is used.
"""

from __future__ import annotations

import socket
import time

import pytest

from fake_scale import FakeScale, frame, make_record
from fake_scale.rawclient import RawClient, gp_batch
from fake_scale.wire import check_char, message_ok_check, split_framed

RI = frame(b"RI")
BX = frame(b"bX01XX")
GPF0 = frame(b"GPF0")


def rec(i, plu="1111", desc="Test Cheese", price="9.99", **kw):
    return make_record(i, plu, desc, price, **kw)


def session(fake, records_batches, *, finish=True):
    """Do a normal bX01XX, GP..., GPF0 session. Returns the replies."""
    replies = []
    with RawClient(fake.address) as c:
        c.send(BX)
        replies.append(c.recv_reply())
        for batch in records_batches:
            c.send(gp_batch(batch))
            replies.append(c.recv_reply())
        if finish:
            c.send(GPF0)
            replies.append(c.recv_reply())
    return replies


# ---------- wire format ----------

def test_check_character_known_values():
    assert RI == b"RI\t\n4\r"
    assert BX == b"bX01XX\t\n1\r"
    assert GPF0 == b"GPF0\t\n8\r"
    assert frame(b"bXXXOk") == b"bXXXOk\t\n7\r"
    assert frame(b"GPF0Ok") == b"GPF0Ok\t\n5\r"


def test_check_value_ten_is_colon():
    # Find some text whose check value is 10, and make sure it shows as ':'
    for n in range(200):
        body = f"X{n}".encode() + b"\t\n"
        if check_char(body) == b":":
            assert message_ok_check(body + b":\r")
            return
    pytest.fail("no sample text gave a check value of 10")


def test_splitter_does_not_end_a_batch_at_a_record_separator():
    batch = gp_batch([rec(1), rec(2), rec(3)])
    got = split_framed(batch + b"extra")
    assert got is not None and got[0] == batch and got[1] == b"extra"
    assert split_framed(batch[:-1]) is None


# ---------- normal protocol ----------

def test_ri_gets_no_reply_and_the_scale_closes(fake_scale):
    with RawClient(fake_scale.address) as c:
        c.send(RI)
        assert c.recv_reply() is None
    assert fake_scale.kinds() == ["RI"]


def test_bx01xx_gets_bxxxok(fake_scale):
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        assert c.recv_reply() == b"bXXXOk\t\n7\r"


def test_gp_batch_reply_is_echo_cr_ok_framed(fake_scale):
    batch = gp_batch([rec(1), rec(2)])
    echo = batch[-2:-1]
    replies = session(fake_scale, [[rec(1), rec(2)]])
    gp = replies[1]
    assert gp[:1] == echo
    assert gp[1:2] == b"\r"
    assert gp[2:4] == b"Ok"
    # The trailer check covers the echo, the CR and the word.
    body = gp[:-2]
    assert gp.endswith(check_char(body) + b"\r")
    assert body == echo + b"\r" + b"Ok\t\n"


def test_gpf0_gets_gpf0ok(fake_scale):
    replies = session(fake_scale, [])
    assert replies[-1] == b"GPF0Ok\t\n5\r"


def test_gpf0ok_comes_back_even_when_items_failed(fake_scale):
    replies = session(fake_scale, [[rec(900)]])
    assert replies[1][2:7] == b"Error"
    assert replies[2] == b"GPF0Ok\t\n5\r"
    assert fake_scale.items == {}


def test_ids_above_500_get_error_and_500_and_0_get_ok(fake_scale):
    r = session(fake_scale, [[rec(501)], [rec(500, "2222")], [rec(0, "3333")]])
    assert r[1][2:7] == b"Error"
    assert r[2][2:4] == b"Ok"
    assert r[3][2:4] == b"Ok"
    # Id 0 got Ok, but the real scale never shows it in a read, so it is not an item.
    assert sorted(fake_scale.items) == [500]
    assert sorted(fake_scale.hidden_items) == [0]


def test_error_reply_has_a_correct_trailer(fake_scale):
    r = session(fake_scale, [[rec(501)]])[1]
    assert r.endswith(check_char(r[:-2]) + b"\r")


def test_wrong_check_character_closes_with_no_reply(fake_scale):
    fake_scale.allow("bad_check")
    good = gp_batch([rec(1)])
    bad = good[:-2] + bytes([good[-2] ^ 1]) + b"\r"
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        assert c.recv_reply() == b"bXXXOk\t\n7\r"
        c.send(bad)
        assert c.recv_reply() is None
    assert fake_scale.items == {}


def test_items_are_kept_and_replaced_by_id(fake_scale):
    session(fake_scale, [[rec(5, "1005", "First", "1.00"), rec(6, "1006", "Other", "2.00")]])
    session(fake_scale, [[rec(5, "1099", "Changed", "3.50")]])
    assert fake_scale.item(5)[1:5] == ("1099", "1099", "Changed", "3.50")
    assert fake_scale.item(6)[3] == "Other"
    assert fake_scale.accepted_ids() == [5, 6]


def test_it_never_validates_values(fake_scale):
    weird = [
        rec(1, "1000", "Too dear", "1000.00"),
        rec(2, "1000", "Duplicate plu", "1.00"),
        rec(3, "12345", "L" * 100, "0.00"),
        rec(4, "7", "Caf\xe9", "5"),
    ]
    r = session(fake_scale, [weird])
    assert r[1][2:4] == b"Ok"
    assert fake_scale.item(1)[4] == "1000.00"
    assert fake_scale.item(4)[3] == "Caf\xe9"
    assert len(fake_scale.items) == 4


def test_five_in_a_batch_and_two_batches_in_one_connection(fake_scale):
    batch1 = [rec(i, f"{1000 + i}") for i in range(1, 6)]
    batch2 = [rec(i, f"{1000 + i}") for i in range(6, 11)]
    r = session(fake_scale, [batch1, batch2])
    assert [x[2:4] for x in r[1:3]] == [b"Ok", b"Ok"]
    assert fake_scale.accepted_ids() == list(range(1, 11))


def test_sends_are_all_recorded(fake_scale):
    session(fake_scale, [[rec(1)]])
    assert fake_scale.kinds() == ["bX01XX", "GP", "GPF0"]
    assert fake_scale.messages_of("GP")[0].ids == [1]


# ---------- violations: anything outside the known protocol fails the test ----------

def test_unknown_message_is_a_violation():
    fake = FakeScale().start()
    try:
        with RawClient(fake.address) as c:
            c.send(frame(b"CALIBRATE"))
            assert c.recv_reply() is None
        time.sleep(0.1)
        assert [v.code for v in fake.unexpected_violations()] == ["unknown_message"]
        with pytest.raises(AssertionError, match="outside the known protocol"):
            fake.assert_clean()
    finally:
        fake.stop()


@pytest.mark.parametrize("msg", [b"SetHORA,10:00:00\n", b"SetFECHA,01/01/2026\n"])
def test_clock_messages_are_violations_by_default(msg):
    fake = FakeScale().start()
    try:
        with RawClient(fake.address) as c:
            c.send(msg)
            time.sleep(0.3)
        assert [v.code for v in fake.unexpected_violations()] == ["unknown_message"]
    finally:
        fake.stop()


def test_gp_before_bx_is_a_violation():
    fake = FakeScale().start()
    try:
        with RawClient(fake.address) as c:
            c.send(gp_batch([rec(1)]))
            assert c.recv_reply() is None
        assert [v.code for v in fake.unexpected_violations()] == ["sequence"]
        assert fake.items == {}
    finally:
        fake.stop()


def test_wrong_count_is_a_violation():
    fake = FakeScale().start()
    try:
        with RawClient(fake.address) as c:
            c.send(BX)
            c.recv_reply()
            # says 02 but holds one record
            body = "\t".join(rec(1)).encode()
            c.send(frame(b"GP02" + body))
            assert c.recv_reply() is None
        assert [v.code for v in fake.unexpected_violations()] == ["malformed_gp"]
    finally:
        fake.stop()


def test_short_record_is_a_violation():
    fake = FakeScale().start()
    try:
        with RawClient(fake.address) as c:
            c.send(BX)
            c.recv_reply()
            c.send(frame(b"GP01" + "\t".join(rec(1)[:19]).encode()))
            assert c.recv_reply() is None
        assert [v.code for v in fake.unexpected_violations()] == ["malformed_gp"]
    finally:
        fake.stop()


def test_a_wrong_check_character_is_a_violation_unless_allowed():
    fake = FakeScale().start()
    try:
        with RawClient(fake.address) as c:
            c.send(b"bX01XX\t\n2\r")
            assert c.recv_reply() is None
        assert [v.code for v in fake.unexpected_violations()] == ["bad_check"]
    finally:
        fake.stop()


def test_sending_before_the_reply_is_a_violation():
    fake = FakeScale().start()
    try:
        fake.delay_reply(0.5, "bX01XX")
        with RawClient(fake.address) as c:
            c.send(BX)
            time.sleep(0.1)
            c.send(gp_batch([rec(1)]))
            c.recv_reply(timeout=1.5)
        assert "pipelined" in [v.code for v in fake.unexpected_violations()]
    finally:
        fake.stop()


def test_clock_works_when_allowed():
    fake = FakeScale(allow_clock=True, serial="X-1").start()
    try:
        with RawClient(fake.address) as c:
            c.send(frame(b"CX"))
            r = c.recv_reply()
            assert r.startswith(b"CX00\tX-1\t40.00 lb")
            c.send(b"SetHORA,10:00:00\n")
            assert c.recv_line() == b"OK\n"
        fake.assert_clean()
    finally:
        fake.stop()


# ---------- faults ----------

def test_slow_reply(fake_scale):
    fake_scale.delay_reply(0.6, "GP")
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        c.recv_reply()
        t = time.monotonic()
        c.send(gp_batch([rec(1)]))
        r = c.recv_reply(timeout=3)
        assert time.monotonic() - t >= 0.55
        assert r[2:4] == b"Ok"
    assert fake_scale.item(1) is not None


def test_stall_never_replies_but_keeps_the_items_by_default(fake_scale):
    fake_scale.stall("GP")
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        c.recv_reply()
        c.send(gp_batch([rec(1)]))
        with pytest.raises(socket.timeout):
            c.recv_reply(timeout=0.5)
    assert fake_scale.item(1) is not None


def test_stall_can_lose_the_items(fake_scale):
    fake_scale.stall("GP", apply=False)
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        c.recv_reply()
        c.send(gp_batch([rec(1)]))
        with pytest.raises(socket.timeout):
            c.recv_reply(timeout=0.4)
    assert fake_scale.items == {}


def test_drop_before_reply(fake_scale):
    fake_scale.drop_before_reply("GP")
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        c.recv_reply()
        c.send(gp_batch([rec(1)]))
        assert c.recv_reply() is None
    assert fake_scale.item(1) is not None


def test_drop_mid_send_keeps_nothing(fake_scale):
    fake_scale.drop_mid_send("GP", after_bytes=15)
    data = gp_batch([rec(1), rec(2), rec(3)])
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        c.recv_reply()
        c.send(data[:30])
        assert c.recv_reply() is None
    assert fake_scale.items == {}


def test_error_on_a_chosen_record(fake_scale):
    fake_scale.error_on_ids(2)
    r = session(fake_scale, [[rec(1)], [rec(2, "2222")], [rec(3, "3333")]])
    assert [x[2:4] for x in r[1:4]] == [b"Ok", b"Er", b"Ok"]
    assert fake_scale.accepted_ids() == [1, 3]


def test_force_error_once(fake_scale):
    fake_scale.force_error("GP", times=1)
    r = session(fake_scale, [[rec(1)], [rec(1)]])
    assert r[1][2:7] == b"Error"
    assert r[2][2:4] == b"Ok"
    assert fake_scale.accepted_ids() == [1]


def test_bad_check_on_the_reply_trailer(fake_scale):
    fake_scale.bad_reply_check("GP", where="trailer")
    r = session(fake_scale, [[rec(1)]])[1]
    assert r[2:4] == b"Ok"
    assert r[-2:-1] != check_char(r[:-2])


def test_bad_check_on_the_reply_echo(fake_scale):
    fake_scale.bad_reply_check("GP", where="echo")
    batch = gp_batch([rec(1)])
    r = session(fake_scale, [[rec(1)]])[1]
    assert r[:1] != batch[-2:-1]
    # the trailer is consistent with the changed echo, so only the echo is wrong
    assert r[-2:-1] == check_char(r[:-2])


def test_refuse_for_a_while_then_accept_again(fake_scale):
    fake_scale.refuse_for(0.8)
    t = time.monotonic()
    with pytest.raises(OSError):
        socket.create_connection(fake_scale.address, timeout=0.3).close()
    time.sleep(max(0, 1.0 - (time.monotonic() - t)))
    c = socket.create_connection(fake_scale.address, timeout=1)
    c.close()


def test_reset_next_connection(fake_scale):
    fake_scale.reset_next_connections(1)
    # On Linux the reset can arrive during the connect itself, so that is inside the try.
    try:
        with RawClient(fake_scale.address) as c:
            c.send(BX)
            got = c.recv_reply()
    except OSError:
        got = None
    assert got is None
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        assert c.recv_reply() == b"bXXXOk\t\n7\r"


def test_nothing_received_before_any_traffic(fake_scale):
    assert fake_scale.nothing_received
    session(fake_scale, [])
    assert not fake_scale.nothing_received


def test_listens_on_localhost_only(fake_scale):
    assert fake_scale.host == "127.0.0.1"
    assert fake_scale._listener.getsockname()[0] == "127.0.0.1"
