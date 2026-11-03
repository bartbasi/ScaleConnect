# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Wire format helpers for the fake scale.

This is a second, independent implementation of the framing and check
character. It must not import from src/scaleconnect, so that a bug in the
library cannot hide behind the same bug here.

Framed message: text, TAB, LF, check character, CR.
Check character: weights 2..8 repeating over every byte from the first
through the LF. Negate the total, mod 11, add 0x30. A value of 10 is ':'.
"""

from __future__ import annotations

TAB = b"\t"
LF = b"\n"
CR = b"\r"
WEIGHTS = (2, 3, 4, 5, 6, 7, 8)


def check_char(data: bytes) -> bytes:
    """Check character for data, which must already end with TAB LF."""
    total = sum(b * WEIGHTS[i % 7] for i, b in enumerate(data))
    return bytes([0x30 + ((-total) % 11)])


def frame(text: bytes) -> bytes:
    """Frame text: text, TAB, LF, check character, CR."""
    body = text + TAB + LF
    return body + check_char(body) + CR


def split_framed(buf: bytes) -> tuple[bytes, bytes] | None:
    """Take one framed message off the front of buf.

    Returns (message, rest), or None if buf holds no complete message yet.
    A message ends at TAB LF, one check character, then CR. Record
    separators inside a GP batch are TAB LF followed by the next id and a
    TAB, never by one character and CR, so the first match is the end.
    """
    start = 0
    while True:
        i = buf.find(TAB + LF, start)
        if i < 0 or i + 4 > len(buf):
            return None
        if buf[i + 3 : i + 4] == CR:
            end = i + 4
            return buf[:end], buf[end:]
        start = i + 2


def split_clock(buf: bytes) -> tuple[bytes, bytes] | None:
    """Clock messages end in LF with no check character."""
    i = buf.find(LF)
    if i < 0:
        return None
    return buf[: i + 1], buf[i + 1 :]


def message_ok_check(msg: bytes) -> bool:
    """True if the check character on a framed message is right."""
    body, chk = msg[:-2], msg[-2:-1]
    return check_char(body) == chk
