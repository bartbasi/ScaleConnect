"""Scale message building and reply parsing. No sockets. Standard library only.

Only the seven named messages can be built: RI, bX01XX, GP batches, GPF0, LP, Ok (in a
read) and CX. The framing function refuses anything else, so this library has no way
to send arbitrary text, an E message or LG to a scale. See docs/PROTOCOL.md sections 2,
3, 5 and 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from .errors import ProtocolError
from .models import FIELD_COUNT, MAX_ITEM_ID, Item

PORT = 50036
MAX_BATCH = 99  # the count in a GP batch is two digits
MAX_REPLY_BYTES = 4096
MAX_READ_BLOCK_BYTES = 200_000
_WEIGHTS = (2, 3, 4, 5, 6, 7, 8)
_TERMINATOR = b"\t\n"
_CR = 0x0D
_RECORD_SEPARATOR = "\t\n"
_CHECK_ALPHABET = frozenset(b"0123456789:")
_FIXED_MESSAGES = frozenset({"RI", "bX01XX", "GPF0", "LP", "Ok", "CX"})


def check_char(data: bytes) -> bytes:
    """Weights 2 to 8 repeat from the first byte. Negate the total, mod 11, add 0x30."""
    total = sum(byte * _WEIGHTS[i % 7] for i, byte in enumerate(data))
    return bytes([0x30 + ((-total) % 11)])


def _frame(text: str) -> bytes:
    is_batch = text[:2] == "GP" and len(text) > 4 and text[2:4].isdigit() and text[2:4] != "00"
    if text not in _FIXED_MESSAGES and not is_batch:
        raise ProtocolError("That message is not one this library may send.")
    body = text.encode("ascii") + _TERMINATOR
    return body + check_char(body) + b"\r"


def build_ri() -> bytes:
    """The first step of a send. The scale closes the connection in reply."""
    return _frame("RI")


def build_open() -> bytes:
    """Opens a send session. The scale replies bXXXOk."""
    return _frame("bX01XX")


def build_finish() -> bytes:
    """Ends a send session. GPF0Ok says nothing about whether items were accepted."""
    return _frame("GPF0")


def build_identity() -> bytes:
    """Asks the scale who it is. The reply starts CX00, then the serial."""
    return _frame("CX")


def build_read_start() -> bytes:
    """Starts a read at id 1. There is no way to start in the middle."""
    return _frame("LP")


def build_read_next() -> bytes:
    """Asks for the next block of a read."""
    return _frame("Ok")


def _record_text(item: Item) -> str:
    fields = item.to_fields()
    if len(fields) != FIELD_COUNT:
        raise ProtocolError(f"A record needs {FIELD_COUNT} fields, this one has {len(fields)}.")
    if not 1 <= item.id <= MAX_ITEM_ID:
        raise ProtocolError(f"Item id {item.id} is outside 1 to {MAX_ITEM_ID}.")
    for value in fields:
        # This also blocks TAB, CR and LF, which would change the message's structure.
        if not all(0x20 <= ord(ch) <= 0x7E for ch in value):
            raise ProtocolError(f"Item id {item.id} has a character that cannot be sent safely.")
    return "\t".join(fields)


def build_item_batch(items: Sequence[Item]) -> bytes:
    """A GP batch: GP, two digit count, then the records. The last TAB LF is the framing."""
    if not 1 <= len(items) <= MAX_BATCH:
        raise ProtocolError(f"A batch holds 1 to {MAX_BATCH} records, not {len(items)}.")
    records = _RECORD_SEPARATOR.join(_record_text(item) for item in items)
    return _frame(f"GP{len(items):02d}{records}")


# ---------------------------------------------------------------- the chain rules


def chain_ok(data: bytes) -> bool:
    """PROTOCOL.md section 2. Walk the CR positions. The character before each CR must be
    the check of all bytes before that character. A leading echo pair has nothing before
    it, so it is skipped."""
    if not data.endswith(b"\r"):
        return False
    for position, byte in enumerate(data):
        if byte != _CR:
            continue
        if position == 0:
            return False
        if position == 1:
            continue
        if check_char(data[: position - 1]) != data[position - 1 : position]:
            return False
    return True


def _leading_pairs(data: bytes) -> list[int]:
    """Start offsets of the x CR pairs at the front of data."""
    starts = []
    offset = 0
    while len(data) >= offset + 2 and data[offset] in _CHECK_ALPHABET and data[offset + 1] == _CR:
        starts.append(offset)
        offset += 2
    return starts


def _end_of_chain(data: bytes, start: int) -> int:
    """Where the run of whole x CR pairs from start ends."""
    end = start
    while len(data) >= end + 2 and data[end] in _CHECK_ALPHABET and data[end + 1] == _CR:
        end += 2
    return end


def _split_frame(frame: bytes) -> tuple[bytes | None, bytes, bool]:
    """Returns (echo character or None, text, whether the chain checks out).

    Any pairs in front are skipped as leftovers of an earlier reply. The last one may
    be an echo, which the reply's own check characters cover.
    """
    starts = _leading_pairs(frame)
    after = starts[-1] + 2 if starts else 0
    echo = frame[starts[-1] : starts[-1] + 1] if starts else None
    terminator = frame.find(_TERMINATOR, after)
    text = frame[after:terminator] if terminator >= 0 else b""
    valid = terminator >= 0 and (chain_ok(frame[after:]) or (bool(starts) and chain_ok(frame[starts[-1] :])))
    return echo, text, valid


# ---------------------------------------------------------------- write replies


class BatchResult(StrEnum):
    OK = "ok"
    ERROR = "error"
    BAD = "bad"  # cannot be trusted, so the result is unknown


def take_reply(buffer: bytes) -> tuple[bytes, bytes] | None:
    """Split one reply off the front of the buffer. None means keep reading.

    A reply ends at TAB LF, then a chain of one or more check and CR pairs.
    """
    at = buffer.find(_TERMINATOR)
    if at < 0:
        return None
    start = at + len(_TERMINATOR)
    if len(buffer) < start + 2:
        return None
    end = _end_of_chain(buffer, start)
    if end == start:
        end = start + 2  # not a pair. The caller's check will fail it.
    return buffer[:end], buffer[end:]


def reply_check_ok(reply: bytes) -> bool:
    """True if the reply is complete and its check characters are right."""
    return _split_frame(reply)[2]


def is_open_ok(reply: bytes) -> bool:
    _, text, valid = _split_frame(reply)
    return valid and text == b"bXXXOk"


def is_open_error(reply: bytes) -> bool:
    _, text, valid = _split_frame(reply)
    return valid and text == b"Error"


def is_finish_ok(reply: bytes) -> bool:
    _, text, valid = _split_frame(reply)
    return valid and text == b"GPF0Ok"


@dataclass(frozen=True)
class BatchReply:
    result: BatchResult
    detail: str


def parse_batch_reply(reply: bytes, sent: bytes) -> BatchReply:
    """A GP reply is the check character of what the scale received, CR, then Ok or Error.

    Its own check character covers all of that. Anything else is BAD, which the
    caller reports as unknown. Only an exact, checked Ok counts as Ok.
    """
    echo, text, valid = _split_frame(reply)
    if not valid:
        return BatchReply(BatchResult.BAD, "The check character on the reply is wrong.")
    if echo is None:
        if text == b"Error":
            return BatchReply(BatchResult.ERROR, "The scale said Error.")
        return BatchReply(BatchResult.BAD, "The reply is not in the expected form.")
    if echo != sent[-2:-1]:
        return BatchReply(BatchResult.BAD, "The scale's check of what it received does not match what was sent.")
    if text == b"Ok":
        return BatchReply(BatchResult.OK, "The scale said Ok.")
    if text == b"Error":
        return BatchReply(BatchResult.ERROR, "The scale said Error.")
    return BatchReply(BatchResult.BAD, "The reply is neither Ok nor Error.")


def describe_reply(data: bytes) -> str:
    """Safe text of a reply, for logs and results.

    Only a reply in a known form is kept. Anything else may be something the scale sent
    by surprise, such as a settings record with password like values, so it is reduced
    to its length and is never stored.
    """
    _, text, valid = _split_frame(data)
    if valid and text in (b"bXXXOk", b"GPF0Ok", b"Ok", b"Error"):
        return data.decode("latin-1").encode("unicode_escape").decode("ascii")
    return f"<unrecognized reply, {len(data)} bytes>"


# ---------------------------------------------------------------- reads


class ReadReplyError(Exception):
    """A reply that cannot be a read block or an identity. Holds no reply bytes on purpose."""


@dataclass(frozen=True)
class ReadBlock:
    records: tuple[Item, ...]
    end: bool


def _parse_read_record(line: bytes) -> Item:
    # Latin-1 keeps every byte. No value checks here: a scale can hold odd records, and
    # the read has to report them as they are.
    values = line.decode("latin-1").split("\t")
    if len(values) != FIELD_COUNT or not values[0].isascii() or not values[0].isdigit():
        raise ReadReplyError("A record in the read block is not in the expected form.")
    return Item.from_fields(values)


def take_read_block(buffer: bytes) -> tuple[ReadBlock, bytes] | None:
    """Take one read block from the front of the buffer. None means keep reading.

    Counts records using the two digits, then walks the chain. Late chain pairs from the
    block before are skipped. Raises ReadReplyError if this cannot be a read block, for
    example a reply starting LG. The error text never holds what the scale sent.
    """
    starts = _leading_pairs(buffer)
    after = starts[-1] + 2 if starts else 0
    data = buffer[after:]
    if len(data) == 1 and data[0] in _CHECK_ALPHABET:
        return None  # a chain pair split between its character and CR. Wait for the rest.
    if len(data) < 4:
        if not b"LP".startswith(data[:2]):
            raise ReadReplyError("The reply is not a read block.")
        if len(data) >= 3 and not data[2:3].isdigit():
            raise ReadReplyError("The reply is not a read block.")
        return None
    if data[:2] != b"LP" or not data[2:4].isdigit():
        raise ReadReplyError("The reply is not a read block.")
    if len(data) > MAX_READ_BLOCK_BYTES:
        raise ReadReplyError("The read block is too long.")
    count = int(data[2:4])
    position = 4
    lines: list[bytes] = []
    for _ in range(max(count, 1)):
        at = data.find(_TERMINATOR, position)
        if at < 0:
            return None
        lines.append(data[position:at])
        position = at + len(_TERMINATOR)
    if len(data) < position + 2:
        return None
    end = _end_of_chain(data, position)
    if end == position:
        raise ReadReplyError("The read block does not end in a check character.")
    frame = data[:end]
    if not chain_ok(frame) and not (starts and chain_ok(buffer[starts[-1] : after + end])):
        raise ReadReplyError("A check character on the read block is wrong.")
    if count == 0:
        if lines[0] != b"End":
            raise ReadReplyError("The read block is not in the expected form.")
        return ReadBlock((), True), data[end:]
    return ReadBlock(tuple(_parse_read_record(line) for line in lines), False), data[end:]


def parse_identity(reply: bytes) -> str:
    """The serial from a CX reply. Raises ReadReplyError if the reply is not one."""
    _, text, valid = _split_frame(reply)
    if not valid or not text.startswith(b"CX00"):
        raise ReadReplyError("The reply is not an identity.")
    fields = text[4:].lstrip(b"\t").split(b"\t")
    serial = fields[0].decode("latin-1").strip()
    if not serial or not serial.isprintable():
        raise ReadReplyError("The identity holds no serial.")
    return serial
