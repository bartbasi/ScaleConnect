"""What a read says when the scale sends something odd, the one repeat, and the read problem log.

Fake scales on localhost, made up items. No test here really waits for the repeat: the wait goes
through the injected sleep. See PROTOCOL.md section 3, the event of November 2.
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import anyio
import pytest
from fake_scale.server import read_block_bytes
from mcp import Client

from scaleconnect import read_log
from scaleconnect.config import ScaleConfig, Settings, load_config
from scaleconnect.errors import ConfigError
from scaleconnect.mcp_server import build_server
from scaleconnect.models import Item, ScaleStatus
from scaleconnect.scale import FROZEN_NOTE, ReadResult, ScaleClient

WAIT = 7.5
PRODUCT_WORD = "SECRETCHEESE"
SAME_AT_6_TO_10 = "the scale sent blocks out of order: last id seen 10, then a block from id 6 to id 10, exactly the same as the block before"
NOT_A_BLOCK = "The reply is not a read block."
JUNK = b"XX99\t\n0\r"


def item(i: int) -> Item:
    return Item(i, f"{1000 + i}", f"{1000 + i}", f"{PRODUCT_WORD} {i}", "1.00", timestamp="2026-01-01 00:00:00")


def row(i: int) -> tuple[str, ...]:
    return tuple(item(i).to_fields())


def load(fake, n: int = 20):
    fake.preload(*[row(i) for i in range(1, n + 1)])
    return fake


class Harness:
    def __init__(self, fake, tmp_path, *, on_sleep=None, **kw):
        self.fake = fake
        self.log = tmp_path / "state" / "read-problems.log"
        self.slept: list[float] = []
        base = dict(
            reply_timeout_seconds=0.3, connect_timeout_seconds=0.3, connect_attempts=1, connect_retry_wait_seconds=0.0,
            read_pace_seconds=0.0, read_back_window_seconds=30.0, read_back_retry_seconds=0.01,
            read_repeat_enabled=True, read_repeat_wait_seconds=WAIT, read_problem_log=self.log,
        )
        base.update(kw)
        self.settings = Settings(**base)
        config = ScaleConfig("T", fake.host, fake.port, True, serial=fake.serial)

        def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            if on_sleep:
                on_sleep(seconds)

        self.client = ScaleClient(config, self.settings, sleep=sleep)

    @property
    def lines(self) -> list[str]:
        return self.log.read_text(encoding="ascii").splitlines() if self.log.exists() else []

    @property
    def waits(self) -> int:
        return self.slept.count(WAIT)


@pytest.fixture
def harness(tmp_path):
    return lambda fake, **kw: Harness(fake, tmp_path, **kw)


# ---------------------------------------------------------------- a failed identity check


def _wrong_serial_client(fake, h):
    return ScaleClient(ScaleConfig("T", fake.host, fake.port, True, serial="WRONG"), h.settings, sleep=lambda _s: None)


def test_a_failed_identity_goes_in_the_log(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake)
    check = _wrong_serial_client(fake, h).identify()
    assert not check.ok
    text = h.log.read_text(encoding="utf-8")
    assert text.count("\n") == 1
    assert "read=identity" in text and "repeat=no" in text and PRODUCT_WORD not in text


def test_a_read_back_asking_identity_while_the_scale_settles_adds_no_lines(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake)
    check = _wrong_serial_client(fake, h)._identify_once(retry=False)
    assert not check.ok and not h.log.exists()


def test_a_good_identity_adds_no_line(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake)
    assert h.client.identify().ok and not h.log.exists()


# ---------------------------------------------------------------- the message says what came


def test_a_block_sent_twice_gives_the_ids_and_says_it_was_the_same(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7, times=None)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.READ_FAILED and r.first_problem == SAME_AT_6_TO_10
    assert f"T: the read failed ({SAME_AT_6_TO_10})." in r.message


def test_a_block_from_further_back_gives_the_ids_and_says_it_differs(make_fake, harness):
    fake = load(make_fake())
    fault = fake.raw_reply(read_block_bytes([row(i) for i in range(1, 6)]), kind="Ok")
    fault.match = lambda m: fake._block_of(m) == 2
    h = harness(fake)
    r = h.client.read_items()
    assert r.ok and r.first_problem == (
        "the scale sent blocks out of order: last id seen 10, then a block from id 1 to id 5, different from the block before"
    )


def test_a_first_block_out_of_order_says_there_was_none_before(make_fake, harness):
    fake = load(make_fake())
    fake.raw_reply(read_block_bytes([row(3), row(1)]), kind="LP")
    r = harness(fake).client.read_items()
    assert not r.ok and not r.repeated and r.first_problem is None and not r.may_be_frozen
    assert (
        "the scale sent blocks out of order: last id seen 0, then a block from id 3 to id 1, with no block before it, "
        "with ids inside it out of order or repeated"
    ) in r.message
    assert fake.kinds().count("LP") == 1


def test_a_huge_id_is_not_spelled_out(make_fake, harness):
    fake = load(make_fake())
    huge = "9" * 400
    fake.raw_reply(read_block_bytes([(huge, *row(2)[1:]), row(1)]), kind="LP", times=None)
    r = harness(fake).client.read_items()
    assert not r.ok and huge not in r.message and "from id over 99999 to id 1" in r.message


def test_an_id_too_long_to_be_a_number_is_a_bad_reply_and_not_a_crash(make_fake, harness):
    fake = load(make_fake())
    fake.raw_reply(read_block_bytes([("9" * 5000, *row(2)[1:])]), kind="LP", times=None)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and not r.may_be_frozen and "not in the expected form" in r.message
    assert "9999" not in r.message and len(h.lines) == 1 and fake.kinds().count("LP") == 1


# ---------------------------------------------------------------- the plain path


def test_the_repeat_succeeds_and_returns_the_whole_table(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake)
    r = h.client.read_items()
    assert r.ok and r.complete and r.repeated and r.first_problem == SAME_AT_6_TO_10
    assert r.items == {i: item(i) for i in range(1, 21)}
    assert r.message == "T: read 20 records."
    assert fake.kinds().count("LP") == 2 and fake.connection_count == 3, "the identity, then two reads"
    assert h.waits == 1
    fake.assert_one_at_a_time()


def test_a_read_with_no_problem_does_not_repeat_or_log(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake)
    r = h.client.read_items()
    assert r.ok and not r.repeated and r.first_problem is None
    assert fake.kinds().count("LP") == 1 and h.waits == 0 and not h.log.exists()


def blank_block(n: int = 5) -> bytes:
    """The shape of FRONT's damaged table on November 2: blanks, a line feed, then odd bytes."""
    junk = bytes([0x80, 0x25, 0xD4, 0x80]).decode("latin-1")
    return read_block_bytes([(" " * 254 + "\n" + junk, *(junk,) * 19)] * n)


def bad_check_block() -> bytes:
    good = read_block_bytes([row(i) for i in range(6, 11)])
    return good[:-2] + (b"0" if good[-2:-1] != b"0" else b"1") + b"\r"


NOT_REPEATED = {
    "not_in_expected_form": (blank_block(), "not in the expected form"),
    "not_a_block": (JUNK, NOT_A_BLOCK),
    "an_LG_shaped_reply": (b"LG01made up\t\n0\r", NOT_A_BLOCK),
    "bad_check_character": (bad_check_block(), "check character"),
    "id_above_the_limit": (read_block_bytes([(str(501), *row(2)[1:])]), "id above the limit"),
    "long_block": (b"LP05" + b"x" * 200_001, "too long"),
}


@pytest.mark.parametrize("where", ["LP", "Ok"])
@pytest.mark.parametrize("name", sorted(NOT_REPEATED))
def test_these_answers_are_a_plain_failure_with_one_lp_on_the_wire(make_fake, harness, name, where):
    raw, words = NOT_REPEATED[name]
    fake = load(make_fake())
    fake.raw_reply(raw, kind=where, times=None)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.READ_FAILED and words in r.message
    assert not r.repeated and r.first_problem is None and not r.may_be_frozen and FROZEN_NOTE not in r.message
    assert fake.kinds().count("LP") == 1 and h.waits == 0
    only, = h.lines
    assert "repeat=no" in only and "silent=no" in only


def test_the_blank_records_before_a_real_freeze_do_not_send_a_second_lp(make_fake, harness):
    """FRONT, November 2: blank records, and the next LP froze it. So there must be no next LP."""
    fake = load(make_fake())
    fake.raw_reply(blank_block(), kind="LP", times=1)
    fake.stall_read(times=None).match = lambda m: m.kind == "LP" and len(fake.messages_of("LP")) >= 2
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and not r.may_be_frozen and not r.repeated
    assert fake.kinds() == ["CX", "LP"] and h.waits == 0


def test_blank_records_after_good_blocks_do_not_send_a_second_lp(make_fake, harness):
    fake = load(make_fake())
    fault = fake.raw_reply(blank_block(), kind="Ok", times=None)
    fault.match = lambda m: fake._block_of(m) == 3
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and not r.repeated and fake.kinds().count("LP") == 1 and h.waits == 0


def test_a_block_with_an_id_above_the_limit_after_good_blocks_is_not_repeated_though_out_of_order(make_fake, harness):
    fake = load(make_fake())
    fault = fake.raw_reply(read_block_bytes([(str(501), *row(2)[1:]), row(3)]), kind="Ok", times=None)
    fault.match = lambda m: fake._block_of(m) == 2
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and not r.repeated and "from id 501 to id 3" in r.message
    assert fake.kinds().count("LP") == 1 and h.waits == 0


def test_a_block_starting_at_id_zero_after_good_blocks_is_not_repeated(make_fake, harness):
    fake = load(make_fake())
    fault = fake.raw_reply(read_block_bytes([("0", *row(2)[1:]), row(1)]), kind="Ok", times=None)
    fault.match = lambda m: fake._block_of(m) == 2
    r = harness(fake).client.read_items()
    assert not r.ok and not r.repeated and fake.kinds().count("LP") == 1


def test_ids_rising_past_the_limit_are_not_repeated(make_fake, harness):
    fake = load(make_fake())
    fault = fake.raw_reply(read_block_bytes([(str(499), *row(2)[1:]), (str(501), *row(3)[1:])]), kind="Ok", times=None)
    fault.match = lambda m: fake._block_of(m) == 1
    r = harness(fake).client.read_items()
    assert not r.ok and not r.repeated and "id above the limit" in r.message and fake.kinds().count("LP") == 1


def test_order_inside_a_later_block_can_repeat_since_a_good_block_came_first(make_fake, harness):
    fake = load(make_fake())
    fault = fake.raw_reply(read_block_bytes([row(12), row(11)]), kind="Ok")
    fault.match = lambda m: fake._block_of(m) == 2
    r = harness(fake).client.read_items()
    assert r.ok and r.repeated and "with ids inside it out of order or repeated" in r.first_problem
    assert fake.kinds().count("LP") == 2


def test_the_repeat_fails_too_and_both_reasons_are_given(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.raw_reply(JUNK, kind="Ok", times=None).match = lambda m: len(fake.messages_of("LP")) == 2
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.READ_FAILED and r.repeated
    assert not r.may_be_frozen and FROZEN_NOTE not in r.message
    assert r.message == (
        f"T: the read failed ({SAME_AT_6_TO_10}). It was read once more, and that failed too. T: the read failed ({NOT_A_BLOCK})."
    )
    assert r.items == {} and fake.kinds().count("LP") == 2 and h.waits == 1, "never a third read"


def test_a_silent_repeat_takes_may_be_frozen_from_the_second_read(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.stall_read(times=None).match = lambda m: m.kind == "LP" and len(fake.messages_of("LP")) == 2
    r = harness(fake).client.read_items()
    assert not r.ok and r.may_be_frozen and r.repeated
    assert SAME_AT_6_TO_10 in r.message and "timed out" in r.message and r.message.endswith(FROZEN_NOTE)
    assert fake.kinds().count("LP") == 2


def test_a_repeat_that_cannot_connect_is_not_pressed_and_names_both(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake, on_sleep=lambda s: fake.stop_listening() if s == WAIT else None)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.UNREACHABLE and r.may_be_frozen and r.repeated
    assert SAME_AT_6_TO_10 in r.message and "could not connect to read" in r.message
    assert h.waits == 1 and fake.connection_count == 2


# ---------------------------------------------------------------- silence is never repeated


def test_a_silent_stall_is_not_repeated_and_stays_may_be_frozen(make_fake, harness):
    fake = load(make_fake())
    fake.stall_read(block=1, times=None)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and not r.repeated and r.first_problem is None
    assert FROZEN_NOTE in r.message
    assert fake.kinds().count("LP") == 1 and fake.connection_count == 2 and h.waits == 0


def test_a_scale_that_closes_on_a_silent_stop_is_not_contacted_again(make_fake, harness):
    fake = load(make_fake())
    fake.drop_read(block=1, times=None)
    h = harness(fake, connect_attempts=5)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and not r.repeated
    assert fake.connection_count == 2 and h.slept == [0.0], "the identity and one read, and only the one read pace wait"


def test_a_frozen_scale_is_not_contacted_again(make_fake, harness):
    fake = load(make_fake(freeze_lp_at_id=7))
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and fake.freezes == 1
    assert fake.connection_count == 2 and fake.kinds().count("LP") == 1 and h.waits == 0


def test_a_failed_connect_is_not_repeated_beyond_its_own_retries(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake, connect_attempts=3, connect_retry_wait_seconds=0.01)
    h.client._identity_confirmed = True
    fake.stop_listening()
    r = h.client.read_items()
    assert r.status == ScaleStatus.UNREACHABLE and r.may_be_frozen and not r.repeated
    assert h.slept == [0.01, 0.02] and h.waits == 0


# ---------------------------------------------------------------- the patient path


def patient(h, up_to_id=None) -> ReadResult:
    h.client._identity_confirmed = True
    return h.client.read_items(up_to_id, patient=True)


def test_the_patient_read_repeats_and_succeeds(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake)
    r = patient(h)
    assert r.ok and r.complete and r.repeated and r.first_problem == SAME_AT_6_TO_10 and len(r.items) == 20
    assert fake.kinds().count("LP") == 2 and h.waits == 1
    assert [re.search(r"repeat=(\w+)", line).group(1) for line in h.lines] == ["will_try", "worked"]
    assert "read=patient" in h.lines[0]


def test_the_patient_read_fails_twice_and_names_both_and_stops(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7, times=None)
    h = harness(fake)
    r = patient(h)
    assert not r.ok and r.repeated and not r.may_be_frozen
    assert r.message.count(SAME_AT_6_TO_10) == 2 and "It was read once more, and that failed too." in r.message
    assert fake.kinds().count("LP") == 2 and h.waits == 1


def test_junk_at_the_start_of_a_patient_read_is_a_plain_failure_with_one_lp(make_fake, harness):
    fake = load(make_fake())
    fake.raw_reply(JUNK, kind="LP", times=None)
    h = harness(fake)
    r = patient(h)
    assert not r.ok and not r.repeated and r.first_problem is None and r.message.count(NOT_A_BLOCK) == 1 and not r.may_be_frozen
    assert fake.kinds().count("LP") == 1 and h.waits == 0
    assert "repeat=no" in h.lines[0] and "read=patient" in h.lines[0]


@pytest.mark.parametrize("name", sorted(NOT_REPEATED))
def test_these_answers_are_a_plain_failure_on_a_read_back_too(make_fake, harness, name):
    raw, words = NOT_REPEATED[name]
    fake = load(make_fake())
    fake.raw_reply(raw, kind="Ok", times=None)
    h = harness(fake)
    r = patient(h)
    assert not r.ok and not r.repeated and words in r.message and not r.may_be_frozen
    assert fake.kinds().count("LP") == 1 and h.waits == 0


def test_a_first_block_out_of_order_on_a_read_back_is_a_plain_failure(make_fake, harness):
    fake = load(make_fake())
    fake.raw_reply(read_block_bytes([row(3), row(1)]), kind="LP", times=None)
    h = harness(fake)
    r = patient(h)
    assert not r.ok and not r.repeated and fake.kinds().count("LP") == 1 and h.waits == 0


def test_a_patient_stall_after_blocks_is_not_repeated(make_fake, harness):
    fake = load(make_fake())
    fake.stall_read(block=1, times=None)
    h = harness(fake)
    r = patient(h)
    assert not r.ok and r.may_be_frozen and not r.repeated and FROZEN_NOTE in r.message
    assert fake.kinds().count("LP") == 1 and fake.connection_count == 1 and h.waits == 0


def test_a_patient_read_that_closes_after_blocks_is_not_contacted_again(make_fake, harness):
    fake = load(make_fake())
    fake.drop_read(block=1, times=None)
    h = harness(fake)
    r = patient(h)
    assert not r.ok and r.may_be_frozen and fake.connection_count == 1 and h.waits == 0


def test_a_patient_repeat_that_goes_silent_stops_at_once_inside_the_window(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.stall_read(times=None).match = lambda m: m.kind == "LP" and len(fake.messages_of("LP")) >= 2
    h = harness(fake)
    r = patient(h)
    assert not r.ok and r.may_be_frozen and r.repeated and SAME_AT_6_TO_10 in r.message and FROZEN_NOTE in r.message
    assert fake.kinds().count("LP") == 2, "not one more LP, though the window has time left"


def test_a_patient_repeat_gets_no_time_of_its_own(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake, read_back_window_seconds=WAIT - 1)
    r = patient(h)
    assert not r.ok and not r.repeated and r.first_problem is None and not r.may_be_frozen
    assert SAME_AT_6_TO_10 in r.message and "failed too" not in r.message
    assert fake.kinds().count("LP") == 1 and h.waits == 0
    assert "repeat=no_time" in h.lines[0]


def fail_connects_after_the_wait(h, error, *, times=None):
    """Connects after the repeat's wait fail this way, and work again after `times` of them.
    The real thing depends on the platform: on Windows a short connect limit times out first."""
    real = h.client._open_socket
    tried = []

    def open_socket():
        if h.waits and (times is None or len(tried) < times):
            tried.append(error)
            raise error("made up")
        return real()

    h.client._open_socket = open_socket
    return tried


def test_a_patient_repeat_refused_until_the_window_ends_names_both(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake, read_back_window_seconds=WAIT + 0.2)
    tried = fail_connects_after_the_wait(h, ConnectionRefusedError)
    r = patient(h)
    assert not r.ok and r.repeated and r.may_be_frozen
    assert SAME_AT_6_TO_10 in r.message and "could not read back in time" in r.message and FROZEN_NOTE in r.message
    assert fake.connection_count == 1 and len(tried) > 1


def test_a_patient_repeat_refused_then_let_in_sends_one_more_lp(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake)
    tried = fail_connects_after_the_wait(h, ConnectionRefusedError, times=2)
    r = patient(h)
    assert r.ok and r.repeated and len(tried) == 2 and fake.kinds().count("LP") == 2


@pytest.mark.parametrize("error", [TimeoutError, ConnectionResetError])
def test_a_patient_repeat_with_a_connect_that_gets_no_answer_stops_at_once(make_fake, harness, error):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake)
    tried = fail_connects_after_the_wait(h, error)
    r = patient(h)
    assert not r.ok and r.repeated and r.may_be_frozen and SAME_AT_6_TO_10 in r.message and FROZEN_NOTE in r.message
    assert len(tried) == 1 and fake.kinds().count("LP") == 1


def test_a_patient_repeat_with_lp_dropped_is_not_tried_again(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.drop_read(block=0, times=None).match = lambda m: m.kind == "LP" and len(fake.messages_of("LP")) >= 2
    h = harness(fake)
    r = patient(h)
    assert not r.ok and r.repeated and r.may_be_frozen and fake.kinds().count("LP") == 2


def test_a_read_back_with_a_dropped_lp_during_the_busy_gap_gets_no_repeat_on_top(make_fake, harness):
    fake = load(make_fake())
    fake.drop_read(block=0, times=1)
    fake.resend_block_at_id(7)
    h = harness(fake)
    r = patient(h)
    assert not r.ok and not r.repeated and not r.may_be_frozen and SAME_AT_6_TO_10 in r.message
    assert fake.kinds().count("LP") == 2 and h.waits == 0


def test_the_busy_gap_is_still_waited_out_before_a_patient_repeat(make_fake, harness):
    fake = load(make_fake(post_write_refusal_seconds=0.0))
    fake.refuse_for(0.15)
    fake.resend_block_at_id(7)
    h = harness(fake, connect_timeout_seconds=0.1, on_sleep=lambda s: time.sleep(0.01))
    r = patient(h)
    assert r.ok and r.repeated and h.waits == 1 and fake.kinds().count("LP") == 2
    assert fake.connection_count == 2, "refused connects never reached the fake"


# ---------------------------------------------------------------- a repeat takes one try at LP


def test_a_repeat_with_lp_dropped_sends_no_third_lp_with_default_connect_attempts(make_fake, harness):
    """The review's run A4 sent 6 LP in one read call."""
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.drop_read(block=0, times=None).match = lambda m: m.kind == "LP" and len(fake.messages_of("LP")) >= 2
    h = harness(fake, connect_attempts=5, connect_retry_wait_seconds=0.01)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.READ_FAILED and r.may_be_frozen and r.repeated
    assert SAME_AT_6_TO_10 in r.message and r.message.endswith(FROZEN_NOTE)
    assert fake.kinds().count("LP") == 2
    assert 0.01 not in h.slept, "no retry wait"


def test_a_repeat_on_a_connection_reset_at_once_is_not_tried_again(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake, connect_attempts=5, connect_retry_wait_seconds=0.01, on_sleep=lambda s: fake.reset_next_connections(1) if s == WAIT else None)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and r.repeated
    assert fake.connection_count == 3, "the identity, the read, and one try at the repeat"
    assert 0.01 not in h.slept


def test_a_repeat_with_a_silent_lp_sends_no_third_lp(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.stall_read(times=None).match = lambda m: m.kind == "LP" and len(fake.messages_of("LP")) >= 2
    h = harness(fake, connect_attempts=5, connect_retry_wait_seconds=0.01)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and fake.kinds().count("LP") == 2


def test_a_repeat_refused_then_let_in_sends_one_more_lp(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake, connect_attempts=5, connect_retry_wait_seconds=0.01)
    tried = fail_connects_after_the_wait(h, ConnectionRefusedError, times=2)
    r = h.client.read_items()
    assert r.ok and r.repeated and len(tried) == 2 and fake.kinds().count("LP") == 2
    assert h.slept.count(0.01) == 1 and h.slept.count(0.02) == 1


def test_a_repeat_refused_every_time_stops_after_its_tries(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake, connect_attempts=3, connect_retry_wait_seconds=0.01)
    tried = fail_connects_after_the_wait(h, ConnectionRefusedError)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.UNREACHABLE and r.may_be_frozen and r.repeated
    assert len(tried) == 3 and fake.kinds().count("LP") == 1


def test_a_repeat_with_a_connect_that_gets_no_answer_is_not_tried_again(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake, connect_attempts=5, connect_retry_wait_seconds=0.01)
    tried = fail_connects_after_the_wait(h, TimeoutError)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.UNREACHABLE and r.may_be_frozen and r.repeated
    assert len(tried) == 1 and fake.kinds().count("LP") == 1


def test_a_first_read_that_needed_a_second_lp_gets_no_repeat_on_top(make_fake, harness):
    fake = load(make_fake())
    fake.drop_read(block=0, times=1)
    fake.resend_block_at_id(7)
    h = harness(fake, connect_attempts=5, connect_retry_wait_seconds=0.01)
    r = h.client.read_items()
    assert not r.ok and not r.repeated and not r.may_be_frozen and SAME_AT_6_TO_10 in r.message
    assert fake.kinds().count("LP") == 2 and h.waits == 0


def test_the_first_read_still_retries_a_dropped_lp_as_before(make_fake, harness):
    fake = load(make_fake())
    fake.drop_read(block=0, times=2)
    h = harness(fake, connect_attempts=5, connect_retry_wait_seconds=0.01)
    r = h.client.read_items()
    assert r.ok and not r.repeated and fake.kinds().count("LP") == 3


# ---------------------------------------------------------------- a scale that does not close


NOT_CLOSED_WORDS = "The scale did not close the connection after we hung up."


def test_blocks_out_of_order_and_no_close_means_no_repeat_and_may_be_frozen(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.slow_to_close()
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.READ_FAILED and r.may_be_frozen
    assert not r.repeated and r.first_problem is None
    assert r.message == f"T: the read failed ({SAME_AT_6_TO_10}). {NOT_CLOSED_WORDS} {FROZEN_NOTE}"
    assert fake.kinds().count("LP") == 1 and h.waits == 0
    only, = (fields_of(line) for line in h.lines)
    assert only["silent"] == "yes" and only["repeat"] == "no" and NOT_CLOSED_WORDS in only["what"] and FROZEN_NOTE not in only["what"]


def test_a_scale_that_says_nothing_and_does_not_close_is_the_same(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.slow_to_close(talks=False)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and not r.repeated and fake.kinds().count("LP") == 1


def test_a_close_past_the_wait_counts_as_no_close(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.slow_to_close(1.0)
    h = harness(fake, reply_timeout_seconds=0.2)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and not r.repeated and fake.kinds().count("LP") == 1


def test_bytes_and_then_a_close_inside_the_wait_is_a_close(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.slow_to_close(0.1)
    h = harness(fake, reply_timeout_seconds=1.0)
    r = h.client.read_items()
    assert r.ok and r.repeated and fake.kinds().count("LP") == 2


def test_a_read_back_with_no_close_stops_with_may_be_frozen(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.slow_to_close()
    h = harness(fake)
    r = patient(h)
    assert not r.ok and r.may_be_frozen and not r.repeated
    assert r.message == f"T: the read failed ({SAME_AT_6_TO_10}). {NOT_CLOSED_WORDS} {FROZEN_NOTE}"
    assert fake.kinds().count("LP") == 1 and h.waits == 0 and "repeat=no" in h.lines[0]


def test_a_second_read_with_no_close_keeps_may_be_frozen(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7, times=None)
    h = harness(fake, on_sleep=lambda s: fake.slow_to_close() if s == WAIT else None)  # only the second hang up gets no close
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and r.repeated and NOT_CLOSED_WORDS in r.message and r.message.endswith(FROZEN_NOTE)
    assert fake.kinds().count("LP") == 2


def test_other_bad_answers_and_no_close_say_may_be_frozen_and_send_nothing_more(make_fake, harness):
    """Round 6 N6: blank records came right before a real freeze, so any bad answer and no close raises the flag."""
    fake = load(make_fake())
    fake.raw_reply(JUNK, kind="Ok", times=None)
    fake.slow_to_close()
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen and not r.repeated and NOT_CLOSED_WORDS in r.message and r.message.endswith(FROZEN_NOTE)
    assert fake.kinds().count("LP") == 1 and h.waits == 0


def test_other_bad_answers_and_a_close_stay_plain_failures(make_fake, harness):
    fake = load(make_fake())
    fake.raw_reply(JUNK, kind="Ok", times=None)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and not r.may_be_frozen and NOT_CLOSED_WORDS not in r.message and fake.kinds().count("LP") == 1


def test_a_first_block_out_of_order_and_no_close_says_may_be_frozen(make_fake, harness):
    fake = load(make_fake())
    fake.raw_reply(read_block_bytes([row(3), row(1)]), kind="LP", times=None)
    fake.slow_to_close()
    r = harness(fake).client.read_items()
    assert not r.ok and r.may_be_frozen and not r.repeated and NOT_CLOSED_WORDS in r.message
    assert fake.kinds().count("LP") == 1


@pytest.mark.parametrize("how", ["plain", "patient"])
def test_a_read_back_with_a_bad_answer_and_no_close_says_may_be_frozen(make_fake, harness, how):
    fake = load(make_fake())
    fake.raw_reply(JUNK, kind="LP", times=None)
    fake.slow_to_close(times=None)
    h = harness(fake, read_repeat_enabled=False)
    r = patient(h) if how == "patient" else h.client.read_items()
    assert not r.ok and r.may_be_frozen and NOT_CLOSED_WORDS in r.message and fake.kinds().count("LP") == 1


def test_after_a_bad_answer_a_close_up_to_eight_seconds_counts(make_fake, harness):
    """Round 6 N5: a close at 2.5 seconds after a bad answer is a close, not a freeze. The reply timeout still caps it."""
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.slow_to_close(2.5)
    h = harness(fake, reply_timeout_seconds=9.0, read_repeat_enabled=False)
    started = time.monotonic()
    r = h.client.read_items()
    assert not r.ok and not r.may_be_frozen and NOT_CLOSED_WORDS not in r.message
    assert 2.0 < time.monotonic() - started < 8.0 and fake.kinds().count("LP") == 1


def test_nothing_else_cares_what_end_session_answers(make_fake, harness):
    """The identity, a good read, an early stop and a write go on as ever when no scale closes."""
    fake = load(make_fake())
    h = harness(fake)
    real = h.client._end_session
    h.client._end_session = lambda sock, **kw: real(sock, **kw) and False
    assert h.client.identify().ok
    assert h.client.read_items().ok
    early = h.client.read_items(10)
    assert early.ok and not early.complete and not early.may_be_frozen
    report = h.client.send_items([item(1)])
    assert report.status == ScaleStatus.ACCEPTED_NOT_VERIFIED
    assert patient(h).ok
    assert not h.log.exists()


class TestEndSession:
    """What counts as closing, on a real pair of sockets."""

    @pytest.fixture
    def pair(self):
        ours, theirs = socket.socketpair()
        yield ours, theirs
        for s in (ours, theirs):
            s.close()

    def client(self, wait=0.3):
        settings = Settings(reply_timeout_seconds=wait, connect_timeout_seconds=0.1, read_problem_log=None)
        return ScaleClient(ScaleConfig("T", "127.0.0.1", 1, False, serial="X", read_enabled=True), settings)

    def test_a_close_counts(self, pair):
        ours, theirs = pair
        theirs.close()
        assert self.client()._end_session(ours) is True

    def test_bytes_and_a_close_count(self, pair):
        ours, theirs = pair
        theirs.sendall(b"LP00End\t\n4\r")
        theirs.close()
        assert self.client()._end_session(ours) is True

    def test_bytes_and_no_close_do_not_count(self, pair):
        ours, theirs = pair
        theirs.sendall(b"LP00End\t\n4\r")
        assert self.client()._end_session(ours) is False

    def test_silence_does_not_count_and_the_wait_is_kept(self, pair):
        ours, _ = pair
        started = time.monotonic()
        assert self.client(0.2)._end_session(ours) is False
        assert 0.15 < time.monotonic() - started < 1.5

    def test_a_scale_that_never_stops_sending_cannot_hold_the_wait_open(self, pair):
        ours, theirs = pair
        stop = threading.Event()

        def talk():
            while not stop.is_set():
                try:
                    theirs.sendall(b"x" * 100)
                except OSError:
                    return
                time.sleep(0.005)

        thread = threading.Thread(target=talk, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            assert self.client(0.2)._end_session(ours) is False
            assert time.monotonic() - started < 1.5
        finally:
            stop.set()
            thread.join(timeout=2)

    def test_the_wait_is_never_more_than_two_seconds(self, pair):
        ours, _ = pair
        started = time.monotonic()
        assert self.client(60.0)._end_session(ours) is False
        assert 1.8 < time.monotonic() - started < 3.0

    def test_after_a_bad_answer_the_wait_goes_past_two_seconds_up_to_the_reply_timeout(self, pair):
        ours, _ = pair
        started = time.monotonic()
        assert self.client(3.0)._end_session(ours, after_bad_answer=True) is False
        assert 2.8 < time.monotonic() - started < 4.5

    def test_a_reset_counts(self, pair):
        ours, theirs = pair
        theirs.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        theirs.close()
        assert self.client()._end_session(ours) is True

    def test_a_connection_this_pc_gave_up_on_does_not_count(self):
        class Gone:
            def shutdown(self, _how):
                raise ConnectionAbortedError("made up")

            def close(self):
                pass

        assert self.client()._end_session(Gone()) is False


# ---------------------------------------------------------------- the setting


def test_the_setting_is_off_by_default():
    assert Settings().read_repeat_enabled is False


@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_a_setting_that_is_not_true_or_false_is_refused(value):
    with pytest.raises(ConfigError, match="read_repeat_enabled"):
        Settings(read_repeat_enabled=value)


def test_the_setting_loads_from_a_config_file_and_only_as_true_or_false(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[settings]\nread_repeat_enabled = true\n", encoding="utf-8")
    assert load_config(path).settings.read_repeat_enabled is True
    path.write_text("", encoding="utf-8")
    assert load_config(path).settings.read_repeat_enabled is False
    for bad in ('"true"', "1"):
        path.write_text(f"[settings]\nread_repeat_enabled = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="read_repeat_enabled"):
            load_config(path)


def test_the_example_config_leaves_it_off_and_says_why():
    # Read it as text and TOML. load_config refuses a file others can read, as a checked out copy is on Linux.
    text = (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8")
    assert re.search(r"^read_repeat_enabled = false$", text, re.M)
    assert tomllib.loads(text)["settings"]["read_repeat_enabled"] is False


@pytest.mark.parametrize("how", ["plain", "patient"])
def test_with_the_setting_off_no_repeat_exists_but_the_ids_and_the_log_stay(make_fake, harness, how):
    fake = load(make_fake())
    fake.resend_block_at_id(7, times=None)
    h = harness(fake, read_repeat_enabled=False)
    r = patient(h) if how == "patient" else h.client.read_items()
    assert not r.ok and r.status == ScaleStatus.READ_FAILED and not r.may_be_frozen
    assert not r.repeated and r.first_problem is None
    assert r.message == f"T: the read failed ({SAME_AT_6_TO_10})."
    assert fake.kinds().count("LP") == 1 and h.waits == 0
    only, = (fields_of(line) for line in h.lines)
    assert only["repeat"] == "no" and only["silent"] == "no" and SAME_AT_6_TO_10 in only["what"]
    assert only["read"] == how


@pytest.mark.parametrize("how", ["plain", "patient"])
def test_with_the_setting_off_a_scale_that_does_not_close_still_says_may_be_frozen(make_fake, harness, how):
    """Turning the repeat off must not hide the one early sign of a freeze."""
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.slow_to_close()
    h = harness(fake, read_repeat_enabled=False)
    r = patient(h) if how == "patient" else h.client.read_items()
    assert not r.ok and r.may_be_frozen and not r.repeated and NOT_CLOSED_WORDS in r.message and r.message.endswith(FROZEN_NOTE)
    assert fake.kinds().count("LP") == 1 and h.waits == 0


# ---------------------------------------------------------------- the log


def fields_of(line: str) -> dict[str, str]:
    head, what = line.split(" what=", 1)
    parts = head.split(" ")
    return {"when": parts[0], "what": what, **dict(p.split("=", 1) for p in parts[1:])}


def test_a_problem_writes_a_line_with_every_field_and_no_product_text(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    h = harness(fake)
    assert h.client.read_items().ok
    first, second = (fields_of(line) for line in h.lines)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", first["when"])
    assert first["scale"] == "T" and first["read"] == "plain"
    assert first["last_id"] == "10" and first["blocks"] == "3"
    assert float(first["seconds"]) >= 0 and first["silent"] == "no" and first["repeat"] == "will_try"
    assert first["what"] == f"the read failed ({SAME_AT_6_TO_10})."
    assert second["last_id"] == "20" and second["blocks"] == "4" and second["repeat"] == "worked"
    assert second["what"] == "the repeat read worked"
    assert PRODUCT_WORD not in h.log.read_text(encoding="ascii")
    assert "1007" not in h.log.read_text(encoding="ascii"), "not a PLU either"


def test_a_failed_repeat_logs_each_reason_on_its_own_line(make_fake, harness):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    fake.raw_reply(JUNK, kind="Ok", times=None).match = lambda m: len(fake.messages_of("LP")) == 2
    h = harness(fake)
    assert not h.client.read_items().ok
    first, second = (fields_of(line) for line in h.lines)
    assert SAME_AT_6_TO_10 in first["what"] and first["repeat"] == "will_try"
    assert NOT_A_BLOCK in second["what"] and SAME_AT_6_TO_10 not in second["what"] and second["repeat"] == "failed"


def test_a_silent_stall_logs_how_far_it_got(make_fake, harness):
    fake = load(make_fake())
    fake.stall_read(block=2, times=None)
    h = harness(fake)
    assert not h.client.read_items().ok
    only, = (fields_of(line) for line in h.lines)
    assert only["last_id"] == "10" and only["blocks"] == "2" and only["silent"] == "yes" and only["repeat"] == "no"
    assert FROZEN_NOTE not in only["what"] and "timed out" in only["what"]


def test_a_failed_connect_and_a_failed_identity_during_a_read_are_logged(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake)
    fake.stop_listening()
    assert not h.client.read_items().ok
    assert fields_of(h.lines[0])["silent"] == "yes" and "could not connect" in h.lines[0]
    h.client._identity_confirmed = True
    assert not h.client.read_items().ok
    assert len(h.lines) == 2 and "could not connect to read" in h.lines[1]


def test_a_failed_identity_inside_a_read_makes_one_line(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake)
    r = _wrong_serial_client(fake, h).read_items()
    assert not r.ok and r.status == ScaleStatus.WRONG_SCALE
    only, = (fields_of(line) for line in h.lines)
    assert only["read"] == "identity" and only["repeat"] == "no" and fake.kinds() == ["CX"]


def test_a_failed_identity_inside_a_read_with_no_reply_makes_one_line(make_fake, harness):
    fake = load(make_fake())
    fake.stall("CX", times=None)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and r.may_be_frozen
    only, = (fields_of(line) for line in h.lines)
    assert only["read"] == "identity" and only["silent"] == "yes"


def test_a_long_id_cannot_make_a_long_line_in_the_log(make_fake, harness):
    fake = load(make_fake())
    huge = "9" * 4000
    fake.raw_reply(read_block_bytes([(huge, *row(2)[1:])]), kind="LP", times=None)
    h = harness(fake)
    r = h.client.read_items()
    assert not r.ok and fake.kinds().count("LP") == 1
    only, = h.lines
    assert fields_of(only)["last_id"] == "99999" and len(only) < 600 and "9" * 6 not in only


def test_last_id_in_a_line_is_capped_as_a_number():
    line = read_log.format_line(scale="T", read="plain", what="x", last_id=10**5000, blocks=1, seconds=0.0, silent=False, repeat="no")
    assert "last_id=99999 " in line
    line = read_log.format_line(scale="T", read="plain", what="x", last_id=170, blocks=1, seconds=0.0, silent=False, repeat="no")
    assert "last_id=170 " in line


def test_a_read_back_out_of_time_is_logged_once_not_per_try(make_fake, harness):
    fake = load(make_fake())
    h = harness(fake, read_back_window_seconds=0.5, connect_timeout_seconds=0.1, on_sleep=lambda s: time.sleep(0.01))
    fake.stop_listening()
    r = patient(h)
    assert not r.ok and len(h.slept) >= 1 and len(h.lines) == 1 and "could not read back in time" in h.lines[0]


@pytest.mark.parametrize("how", ["parent_is_a_file", "open_raises", "write_raises"])
def test_a_log_that_cannot_be_written_does_not_break_the_read(make_fake, harness, tmp_path, monkeypatch, how):
    fake = load(make_fake())
    fake.resend_block_at_id(7)
    kw = {}
    if how == "parent_is_a_file":
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="ascii")
        kw["read_problem_log"] = blocker / "read-problems.log"
    h = harness(fake, **kw)
    if how == "open_raises":
        monkeypatch.setattr(read_log.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("no")))
    if how == "write_raises":
        monkeypatch.setattr(read_log, "_append", lambda *a: (_ for _ in ()).throw(RuntimeError("no")))
    r = h.client.read_items()
    assert r.ok and r.repeated


def test_a_line_with_odd_text_stays_on_one_line():
    line = read_log.format_line(scale="A B\n", read="plain", what="one\ntwo\tthree\xe9" + "x" * 900, last_id=1, blocks=1, seconds=0.0, silent=False, repeat="no")
    assert line.count("\n") == 1 and line.endswith("\n") and line.isascii() and len(line) < 600
    assert "one?two?three?" in line and "scale=A_B?" in line


def test_the_log_drops_the_older_half_past_the_limit(tmp_path):
    path = tmp_path / "read-problems.log"
    filler = "".join(f"{i:07d}\n" for i in range(read_log.MAX_BYTES // 8 + 10)).encode()
    path.write_bytes(filler)
    read_log.record(path, scale="T", read="plain", what="the newest line", last_id=0, blocks=0, seconds=0.0, silent=False, repeat="no")
    kept = path.read_bytes()
    assert kept.endswith(b"what=the newest line\n")
    assert read_log.MAX_BYTES * 0.4 < len(kept) <= read_log.MAX_BYTES * 0.5
    assert kept.split(b"\n")[0].isdigit() and len(kept.split(b"\n")[0]) == 7, "it starts on a whole line"
    assert b"0000000\n" not in kept
    assert [p.name for p in tmp_path.iterdir()] == ["read-problems.log"], "no temp file left behind"


def test_appends_from_many_writers_stay_whole_lines(tmp_path):
    path = tmp_path / "read-problems.log"

    def write(n):
        for i in range(50):
            read_log.record(path, scale=f"S{n}", read="plain", what=f"line {i}", last_id=i, blocks=i, seconds=0.0, silent=False, repeat="no")

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text(encoding="ascii").splitlines()
    assert len(lines) == 200 and all(re.search(r"what=line \d+$", line) for line in lines)


WRITER = """
import sys
from scaleconnect import read_log
for i in range(50):
    read_log.record(None, scale=sys.argv[1], read="plain", what=f"line {i}", last_id=i, blocks=i, seconds=0.0, silent=False, repeat="no")
"""


def test_appends_from_separate_processes_all_land(tmp_path):
    """On Windows an append is a seek and then a write, so only Linux, where the app lives, can promise every line."""
    path = tmp_path / "read-problems.log"
    env = {**os.environ, "PYTHONPATH": str(Path(read_log.__file__).parents[1]), read_log.PATH_VARIABLE: str(path)}
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, f"S{n}"], env=env) for n in range(4)]
    assert all(writer.wait(timeout=60) == 0 for writer in writers)
    lines = path.read_text(encoding="ascii", errors="replace").splitlines()
    assert lines
    if os.name == "posix":
        assert len(lines) == 200 and all(re.search(r"what=line \d+$", line) for line in lines)


@pytest.mark.skipif(os.name != "posix", reason="Windows has no such modes")
def test_the_log_is_private_even_if_it_was_not(tmp_path):
    path = tmp_path / "new" / "read-problems.log"
    read_log.record(path, scale="T", read="plain", what="x", last_id=0, blocks=0, seconds=0.0, silent=False, repeat="no")
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    read_log.record(path, scale="T", read="plain", what="x", last_id=0, blocks=0, seconds=0.0, silent=False, repeat="no")
    assert path.stat().st_mode & 0o777 == 0o600


def test_the_default_path_is_the_state_folder_of_the_deploy_scripts(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert read_log.default_path() == tmp_path / ".local" / "state" / "scaleconnect" / "read-problems.log"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "x"))
    assert read_log.default_path() == tmp_path / "x" / "scaleconnect" / "read-problems.log"


def test_the_setting_beats_the_variable_and_the_variable_beats_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv(read_log.PATH_VARIABLE, str(tmp_path / "from-variable.log"))
    assert read_log.path_for(tmp_path / "set.log") == tmp_path / "set.log"
    assert read_log.path_for(None) == tmp_path / "from-variable.log"
    monkeypatch.delenv(read_log.PATH_VARIABLE)
    assert read_log.path_for(None) == read_log.default_path()


def test_tests_never_reach_the_real_log():
    assert read_log.path_for(None) != read_log.default_path()
    assert "scaleconnect-tests-" in str(read_log.path_for(None))


# ---------------------------------------------------------------- settings


def test_the_default_wait_is_five_seconds_and_the_log_path_is_unset():
    assert Settings().read_repeat_wait_seconds == 5.0 and Settings().read_problem_log is None


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_a_bad_wait_is_refused(value):
    with pytest.raises(ConfigError):
        Settings(read_repeat_wait_seconds=value)


def test_the_settings_load_from_a_config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[settings]\nread_repeat_wait_seconds = 2\nread_problem_log = "~/x.log"\n', encoding="utf-8")
    settings = load_config(path).settings
    assert settings.read_repeat_wait_seconds == 2.0 and settings.read_problem_log == Path.home() / "x.log"
    path.write_text('[settings]\nread_repeat_wait_seconds = "soon"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="read_repeat_wait_seconds"):
        load_config(path)


# ---------------------------------------------------------------- writes are never repeated


@pytest.mark.parametrize("fault", ["junk", "stall", "drop"])
def test_a_write_is_never_sent_twice(make_fake, harness, fault):
    fake = load(make_fake())
    if fault == "junk":
        fake.raw_reply(JUNK, kind="GP", times=None)
    elif fault == "stall":
        fake.stall("GP", times=None)
    else:
        fake.drop_before_reply("GP", times=None)
    h = harness(fake)
    report = h.client.send_items([item(1)])
    assert report.status == ScaleStatus.UNKNOWN and report.attempts == 1
    assert len(fake.messages_of("GP")) == 1 and h.waits == 0 and not h.log.exists()


def test_with_the_setting_off_a_read_before_a_write_fails_and_nothing_is_sent(make_env):
    env = make_env({"A": True})
    fake = env.fakes["A"]
    fake.resend_block_at_id(1)
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    assert env.statuses(rec) == {"A": "read_failed"}
    assert not fake.messages_of("GP") and fake.kinds().count("LP") == 1


def test_a_repeated_read_before_a_write_still_sends_the_write_once(make_env):
    env = make_env({"A": True}, read_repeat_enabled=True)
    fake = env.fakes["A"]
    fake.resend_block_at_id(1)
    rec = env.change([{"action": "edit", "target_plu": "1001", "price": "11.00"}])
    assert env.statuses(rec) == {"A": "verified"}
    assert len(fake.messages_of("GP")) == 1 and fake.item(1)[4] == "11.00"


# ---------------------------------------------------------------- through the service and MCP


def compare_message(env) -> str:
    comparison = env.svc.compare_scales()[0]
    assert comparison.status == "read_failed" and comparison.may_be_frozen is False
    return comparison.message


def expected_ids(env) -> str:
    first = sorted(env.fakes["A"].items)[:5]
    return f"last id seen {first[-1]}, then a block from id {first[0]} to id {first[-1]}, exactly the same as the block before"


def call(env, name, **arguments):
    async def go():
        async with Client(build_server(env.svc)) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(go).structured_content


def test_the_ids_reach_a_compare_a_preview_an_apply_and_the_mcp_data(make_env):
    env = make_env({"A": True})
    fake = env.fakes["A"]
    fake.resend_block_at_id(1, times=None)
    ids = expected_ids(env)
    assert ids in compare_message(env)

    change = [{"action": "edit", "target_plu": "1001", "price": "11.00"}]
    preview = env.svc.preview_changes(change, read_scales=True)
    warning = next(w for w in preview.warnings if w.code == "scale_read_failed")
    assert ids in warning.message and warning.may_be_frozen is False

    compared = call(env, "scale_compare_scales")
    assert ids in compared["scales"][0]["message"]
    assert compared["scales_may_be_frozen"] == [] and "FROZEN" not in compared["summary"]

    previewed = call(env, "scale_preview_changes", changes=change, read_scales=True)
    assert any(ids in w["message"] for w in previewed["warnings"])
    assert "FROZEN" not in previewed["summary"]

    rec = env.change(change)
    result = rec.scale_results[0]
    assert result.status == "read_failed" and ids in result.message and "Nothing was written to this scale." in result.message
    assert not fake.messages_of("GP")


def test_no_product_text_reaches_any_message_or_summary(make_env):
    env = make_env({"A": True})
    fake = env.fakes["A"]
    description = fake.item(1)[3]
    fake.resend_block_at_id(1, times=None)
    compared = call(env, "scale_compare_scales")
    previewed = call(env, "scale_preview_changes", changes=[{"action": "edit", "target_plu": "1002", "price": "11.00"}], read_scales=True)
    assert description not in compared["summary"] and description not in compared["scales"][0]["message"]
    assert description not in previewed["summary"] and description not in json.dumps(previewed["warnings"])
