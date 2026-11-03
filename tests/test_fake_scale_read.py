# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""The fake scale's read (LP), identity (CX), timing and refusal behavior.

Written from docs/PROTOCOL.md sections 2, 3, 5, 6 and 10. Made up items only.
"""

from __future__ import annotations

import socket
import time

import pytest

from fake_scale import FakeScale, frame, make_record
from fake_scale.rawclient import RawClient, gp_batch
from fake_scale.server import read_block_bytes
from fake_scale.wire import check_char

LP = frame(b"LP")
OK = frame(b"Ok")
CX = frame(b"CX")
BX = frame(b"bX01XX")
GPF0 = frame(b"GPF0")


def rec(i, **kw):
    return make_record(i, f"{1000 + i}", f"Item {i}", "1.00", **kw)


def walk_chain(reply: bytes) -> bytes:
    """Check the chain at the end of a read block by the rule in PROTOCOL.md:
    the character before each CR (after the block's TAB LF) is the check of
    all bytes before that character. Returns the block body."""
    end = reply.rfind(b"\t\n")
    body = reply[: end + 2]
    rest = reply[end + 2 :]
    assert rest.endswith(b"\r") and len(rest) % 2 == 0, rest
    at = len(body)
    for i in range(0, len(rest), 2):
        assert rest[i + 1 : i + 2] == b"\r"
        assert rest[i : i + 1] == check_char(reply[:at]), f"pair {i // 2} is wrong"
        at += 2
    return body


def read_reply(c: RawClient, timeout=2.0) -> bytes:
    """One whole LP block or LP00End. A block ends with its chain; we know
    where by the count, so we read until the pairs are complete."""
    c.sock.settimeout(timeout)
    out = b""
    while True:
        chunk = c.sock.recv(65536)
        if not chunk:
            return out
        out += chunk
        if out.startswith(b"LP00End") and out.endswith(b"\r"):
            return out
        i = out.rfind(b"\t\n")
        if i >= 0 and out.endswith(b"\r") and (len(out) - i - 2) % 2 == 0 and len(out) - i - 2 >= 4:
            # at least two pairs after the last TAB LF; a new chunk may add more
            time.sleep(0.02)
            c.sock.settimeout(0.05)
            try:
                more = c.sock.recv(65536)
                out += more
            except socket.timeout:
                pass
            return out


def load(fake, n):
    fake.preload(*[rec(i) for i in range(1, n + 1)])


# ---------- CX ----------

def test_cx_is_answered_by_default_with_the_serial(fake_scale):
    with RawClient(fake_scale.address) as c:
        c.send(CX)
        r = c.recv_reply()
    assert r.startswith(b"CX00\tFAKE-000001\t40.00 lb\t0.010 lb\tEnglish\t\tlb\tW-LABEL\t2\t\n")
    assert r.endswith(check_char(r[:-2]) + b"\r")


def test_cx_serial_can_be_set():
    with FakeScale(serial="ZZ-9").start() as f:
        with RawClient(f.address) as c:
            c.send(CX)
            assert b"\tZZ-9\t" in c.recv_reply()


def test_cx_keeps_the_connection_open_for_what_comes_next(fake_scale):
    with RawClient(fake_scale.address) as c:
        c.send(CX)
        c.recv_reply()
        c.send(BX)
        assert c.recv_reply() == b"bXXXOk\t\n7\r"


# ---------- LP ----------

def test_a_read_of_twelve_items_comes_in_blocks_of_5_5_2_then_end(fake_scale):
    load(fake_scale, 12)
    got = []
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        while True:
            r = read_reply(c)
            got.append(r)
            if r.startswith(b"LP00End"):
                break
            c.send(OK)
    assert [b[:4] for b in got] == [b"LP05", b"LP05", b"LP02", b"LP00"]
    assert got[-1] == b"LP00End\t\n4\r"
    ids = []
    for b in got[:-1]:
        body = walk_chain(b)
        count = int(body[2:4])
        recs = [r for r in body[4:].split(b"\t\n") if r]
        assert len(recs) == count
        assert all(len(r.split(b"\t")) == 20 for r in recs)
        ids += [int(r.split(b"\t")[0]) for r in recs]
    assert ids == list(range(1, 13))


def test_a_block_ends_with_two_check_pairs(fake_scale):
    load(fake_scale, 3)
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        r = read_reply(c)
    body = walk_chain(r)
    assert len(r) - len(body) == 4


def test_the_chain_follows_the_rule_in_the_notes_for_a_known_block():
    # Built by hand from the rule: c1 = check(body), c2 = check(body + c1 + CR)
    recs = [rec(1)]
    body = b"LP01" + "\t".join(recs[0]).encode() + b"\t\n"
    c1 = check_char(body)
    c2 = check_char(body + c1 + b"\r")
    assert read_block_bytes(recs) == body + c1 + b"\r" + c2 + b"\r"
    c3 = check_char(body + c1 + b"\r" + c2 + b"\r")
    c4 = check_char(body + c1 + b"\r" + c2 + b"\r" + c3 + b"\r")
    assert read_block_bytes(recs, resends=1) == body + c1 + b"\r" + c2 + b"\r" + c3 + b"\r" + c4 + b"\r"


def test_an_empty_table_reads_as_lp00end(fake_scale):
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        assert c.recv_reply() == b"LP00End\t\n4\r"


def test_a_read_needs_no_ri_or_bx(fake_scale):
    load(fake_scale, 1)
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        assert read_reply(c).startswith(b"LP01")
    assert fake_scale.kinds() == ["LP"]


def test_anything_but_ok_makes_the_scale_send_the_same_block_with_a_longer_chain(fake_scale):
    load(fake_scale, 7)
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        first = read_reply(c)
        c.send(CX)
        again = read_reply(c)
        assert again != first
        assert walk_chain(again) == walk_chain(first)  # same block
        assert len(again) - len(walk_chain(again)) == 8  # 4 pairs now
        c.send(OK)
        second = read_reply(c)
    assert second.startswith(b"LP02")


def test_closing_mid_read_makes_the_scale_resend_the_block_then_end(fake_scale):
    load(fake_scale, 7)
    c = RawClient(fake_scale.address)
    c.send(LP)
    first = read_reply(c)
    c.sock.shutdown(socket.SHUT_WR)  # the PC closes its side, it can still be heard
    c.sock.settimeout(2)
    tail = b""
    while True:
        chunk = c.sock.recv(65536)
        if not chunk:
            break
        tail += chunk
        if tail.endswith(b"LP00End\t\n4\r"):
            break
    c.close()
    assert tail.endswith(b"LP00End\t\n4\r")
    resent = tail[: -len(b"LP00End\t\n4\r")]
    assert walk_chain(resent) == walk_chain(first)
    assert any(e.startswith("read_closed_mid") for e in fake_scale.events)


def test_closing_after_a_resend_gets_only_lp00end(fake_scale):
    # Seen in a capture: CX during the read gives the block again. When the PC
    # then closes, the scale sends LP00End and no second copy of the block.
    load(fake_scale, 7)
    c = RawClient(fake_scale.address)
    c.send(LP)
    read_reply(c)
    c.send(CX)
    read_reply(c)
    c.sock.shutdown(socket.SHUT_WR)
    c.sock.settimeout(2)
    tail = b""
    while not tail.endswith(b"LP00End\t\n4\r"):
        chunk = c.sock.recv(65536)
        if not chunk:
            break
        tail += chunk
    c.close()
    assert tail == b"LP00End\t\n4\r"


def test_a_new_read_always_starts_at_id_1(fake_scale):
    load(fake_scale, 12)
    for _ in range(2):
        with RawClient(fake_scale.address) as c:
            c.send(LP)
            r = read_reply(c)
            assert r.startswith(b"LP051\t")


def test_id_0_never_appears_in_a_read(fake_scale):
    fake_scale.preload(rec(0), rec(1))
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        r = read_reply(c)
    assert r.startswith(b"LP011\t")


def test_a_read_shows_what_a_write_stored(fake_scale):
    with RawClient(fake_scale.address) as c:
        c.send(BX)
        c.recv_reply()
        c.send(gp_batch([rec(3), rec(1)]))
        c.recv_reply()
        c.send(GPF0)
        c.recv_reply()
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        r = read_reply(c)
    body = walk_chain(r)
    assert body.startswith(b"LP021\t")
    assert b"\t\n3\t" in body


def test_ASSUMPTION_gaps_in_ids_are_skipped_and_the_count_is_the_real_count(fake_scale):
    # Nobody has seen this on a real scale. See the note at the top of server.py.
    fake_scale.preload(rec(1), rec(3), rec(9))
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        r = read_reply(c)
        assert r.startswith(b"LP03")
        c.send(OK)
        assert c.recv_reply() == b"LP00End\t\n4\r"


def test_lp_with_extra_text_is_a_violation_unless_allowed():
    f = FakeScale().start()
    try:
        with RawClient(f.address) as c:
            c.send(frame(b"LP485"))
            assert c.recv_reply() is None
        assert [v.code for v in f.unexpected_violations()] == ["lp_params"]
    finally:
        f.stop()
    with FakeScale(allow_lp_params=True) as g:
        with RawClient(g.address) as c:
            c.send(frame(b"LP485"))
            assert c.recv_reply() == b"LP00End\t\n4\r"


def test_ok_outside_a_read_is_a_violation():
    f = FakeScale().start()
    try:
        with RawClient(f.address) as c:
            c.send(OK)
            assert c.recv_reply() is None
        assert [v.code for v in f.unexpected_violations()] == ["sequence"]
    finally:
        f.stop()


@pytest.mark.parametrize("text", [b"EP", b"EP01500", b"E", b"EX", b"LG", b"LG01"])
def test_e_messages_and_lg_are_forbidden(text):
    f = FakeScale().start()
    try:
        with RawClient(f.address) as c:
            c.send(frame(text))
            assert c.recv_reply() is None
        assert [v.code for v in f.unexpected_violations()] == ["forbidden_message"]
        with pytest.raises(AssertionError, match="outside the known protocol"):
            f.assert_clean()
    finally:
        f.stop()


@pytest.mark.parametrize("text", [b"LH", b"LT", b"LC", b"DP", b"SP"])
def test_other_messages_are_unknown(text):
    f = FakeScale().start()
    try:
        with RawClient(f.address) as c:
            c.send(frame(text))
            assert c.recv_reply() is None
        assert [v.code for v in f.unexpected_violations()] == ["unknown_message"]
    finally:
        f.stop()


def test_a_dropped_read_fault(fake_scale):
    load(fake_scale, 7)
    fake_scale.drop_read(block=1)
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        assert read_reply(c).startswith(b"LP05")
        c.send(OK)
        assert c.recv_reply() is None


def test_a_stalled_read_fault(fake_scale):
    load(fake_scale, 7)
    fake_scale.stall_read(block=0)
    with RawClient(fake_scale.address) as c:
        c.send(LP)
        with pytest.raises(socket.timeout):
            c.recv_reply(timeout=0.4)


# ---------- the gap after a write ----------

def write_session(fake, ids=(1,), ok=True):
    with RawClient(fake.address) as c:
        c.send(BX)
        c.recv_reply()
        c.send(gp_batch([rec(i) for i in ids]))
        c.recv_reply()
        c.send(GPF0)
        c.recv_reply()


def test_default_gap_after_an_accepted_write_is_the_real_10_to_11_seconds():
    f = FakeScale()
    assert 10.0 <= f.post_write_refusal_seconds <= 11.0


def test_connections_are_refused_after_gpf0_when_something_was_accepted():
    with FakeScale(post_write_refusal_seconds=1.0) as f:
        write_session(f)
        t = time.monotonic()
        with pytest.raises(OSError):
            socket.create_connection(f.address, timeout=0.4).close()
        time.sleep(max(0.0, 1.2 - (time.monotonic() - t)))
        socket.create_connection(f.address, timeout=1).close()
        assert f.refusal_windows == 1


def test_no_gap_when_the_item_got_error():
    with FakeScale(post_write_refusal_seconds=1.0) as f:
        f.error_on_ids(1)
        write_session(f)
        socket.create_connection(f.address, timeout=1).close()
        assert f.refusal_windows == 0


def test_no_gap_after_a_read_only_session():
    with FakeScale(post_write_refusal_seconds=1.0) as f:
        with RawClient(f.address) as c:
            c.send(LP)
            c.recv_reply()
        socket.create_connection(f.address, timeout=1).close()
        assert f.refusal_windows == 0


def test_silent_gap_accepts_the_connection_and_says_nothing():
    with FakeScale(post_write_refusal_seconds=0.8, post_write_mode="silent") as f:
        write_session(f)
        with RawClient(f.address) as c:
            c.send(CX)
            with pytest.raises(socket.timeout):
                c.recv_reply(timeout=0.3)
        time.sleep(0.8)
        with RawClient(f.address) as c:
            c.send(CX)
            assert c.recv_reply().startswith(b"CX00")


# ---------- reply waits and the beat ----------

def test_real_timing_factor_scales_the_waits():
    with FakeScale(timing_factor=0.2) as f:
        with RawClient(f.address) as c:
            t = time.monotonic()
            c.send(BX)
            c.recv_reply()
            assert time.monotonic() - t >= 0.2  # 1.04 s at full size
            t = time.monotonic()
            c.send(gp_batch([rec(i) for i in range(1, 6)]))
            c.recv_reply()
            assert 0.12 <= time.monotonic() - t < 0.5  # 0.7 s at full size


def test_the_beat_stalls_one_batch_each_period():
    with FakeScale(beat_stall_seconds=0.6, beat_period_seconds=0.5) as f:
        waits = []
        with RawClient(f.address) as c:
            c.send(BX)
            c.recv_reply()
            for i in range(1, 5):
                t = time.monotonic()
                c.send(gp_batch([rec(i)]))
                c.recv_reply(timeout=3)
                waits.append(time.monotonic() - t)
                time.sleep(0.45)
        assert waits[0] < 0.3  # the beat clock starts with the first batch
        assert any(w >= 0.55 for w in waits[1:])
        assert sum(w >= 0.55 for w in waits) <= 3


def test_a_beat_can_be_triggered_on_demand_with_counters_wait():
    with FakeScale(beat_stall_seconds=0.7) as f:
        f.trigger_beat()
        with RawClient(f.address) as c:
            c.send(BX)
            c.recv_reply()
            t = time.monotonic()
            c.send(gp_batch([rec(1)]))
            c.recv_reply(timeout=3)
            assert time.monotonic() - t >= 0.65
            t = time.monotonic()
            c.send(gp_batch([rec(2)]))
            c.recv_reply(timeout=3)
            assert time.monotonic() - t < 0.3


def test_counters_beat_is_3_25_seconds():
    from fake_scale.server import BEAT_STALL_COUNTER, BEAT_STALL_OTHER

    assert BEAT_STALL_COUNTER == 3.25
    assert 1.75 <= BEAT_STALL_OTHER <= 1.85


# ---------- sessions ----------

def test_queue_sessions_holds_a_second_connection_until_the_first_closes():
    with FakeScale(queue_sessions=True) as f:
        a = RawClient(f.address)
        a.send(CX)
        a.recv_reply()
        b = RawClient(f.address)
        b.send(CX)
        with pytest.raises(socket.timeout):
            b.recv_reply(timeout=0.4)
        a.close()
        assert b.recv_reply(timeout=2).startswith(b"CX00")
        b.close()


def test_max_concurrent_counts_open_connections():
    with FakeScale() as f:
        a = RawClient(f.address)
        a.send(CX)
        a.recv_reply()
        b = RawClient(f.address)
        b.send(CX)
        b.recv_reply()
        assert f.max_concurrent == 2
        with pytest.raises(AssertionError):
            f.assert_one_at_a_time()
        a.close()
        b.close()
