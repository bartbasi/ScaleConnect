# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""A fake Tor-rey scale for tests. Standard library only.

It listens on localhost only and speaks the protocol in docs/PROTOCOL.md.
It never touches the network beyond 127.0.0.1.

What it copies from the real scale:
  RI                  no reply, the scale closes the connection
  CX                  the identity reply (serial, capacity, step ...)
  bX01XX              bXXXOk (framed)
  GP batch            <check char of what was received> CR <Ok|Error> framed
                      over all of it
  GPF0                GPF0Ok (framed), whether or not any item failed
  id above 500        Error
  id 0                Ok, but the item never shows up in a read
  LP                  the read: blocks of 5 records, each ending in a chain of
                      check characters. The client answers Ok for the next
                      block. LP00End at the end. Anything but Ok makes the scale
                      send the same block again with a longer chain. If the
                      client closes mid read the scale sends the block again,
                      then LP00End, then closes. slow_to_close changes that
  wrong check char   the scale closes the connection with no reply
  after GPF0          if the session had an accepted record, the scale does not
                      answer new connections for about 10.5 seconds
  long waits          on a write, one batch about every 5.6 seconds waits much
                      longer (1.75 to 3.29 s on the real scales)

What it deliberately does not do: it never validates values. A 1000.00
price, a 100 character name, a duplicate PLU are all accepted, because the
real scale accepts them. Validation is the library's job.

It keeps the items it accepts, replacing by id, and exposes them to tests.

It records a violation for anything outside the known protocol: an unknown
message, any E message, any L message but LP (LG above all), a malformed GP
batch, a wrong check character from the client, a message sent before the
previous reply, or a message out of sequence. The pytest fixture in
tests/conftest.py fails the test if any violation was recorded.

A read can freeze a real scale (PROTOCOL.md section 3). freeze_lp_at_id and
freeze_on_gap copy that: no reply to the read, the open connection stays silent,
and new connections are refused until reboot(). Nothing else changes on the table.

ASSUMPTIONS, because nobody has seen it on a real scale (PROTOCOL.md section 3
"Not seen yet"):
  * GAPS. The real scale table was always full (ids 1 to 500). With gaps, this
    fake reads the next 5 stored records in id order, skipping missing ids, so
    a block is always full except the last one. The count in the block is the
    real number of records. A last block with fewer than 5 records is followed
    by LP00End on the next Ok. A real scale may do it differently, for example
    a block per id range or an empty record for a missing id.
  * CHAIN LENGTH. A block starts with 2 check pairs. Each resend adds 2 pairs.
    Only the first resend (4 pairs) has been seen.
  * A write session that got a Hold from an Error batch does not start the
    10.5 second gap. Only an accepted record does.
"""

from __future__ import annotations

import select
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .wire import CR, check_char, frame, message_ok_check, split_clock, split_framed

MAX_ID = 500
FIELD_COUNT = 20
BLOCK_SIZE = 5
KNOWN_KINDS = ("RI", "bX01XX", "GP", "GPF0", "CX", "LP", "Ok")
CLOCK_KINDS = ("SetHORA", "SetFECHA")
KNOWN_PREFIXES = (b"RI", b"bX01XX", b"GP", b"CX", b"LP", b"Ok", b"SetHORA,", b"SetFECHA,")
FORBIDDEN_PREFIXES = (b"E", b"LG")

# Real timing, PROTOCOL.md section 10.
REAL_BX_WAIT = 1.04
REAL_GP_WAIT_FIRST = 0.26
REAL_GP_WAIT_EXTRA = 0.11
REAL_READ_WAIT = 0.085
BEAT_PERIOD = 5.6
BEAT_STALL_COUNTER = 3.25
BEAT_STALL_OTHER = 1.8
REAL_POST_WRITE_REFUSAL = 10.5

Record = tuple[str, ...]


@dataclass
class Message:
    """One message the fake received."""

    seq: int
    conn_id: int
    kind: str
    raw: bytes
    check_ok: bool
    records: list[Record] = field(default_factory=list)
    at: float = 0.0

    @property
    def ids(self) -> list[int]:
        return [int(r[0]) for r in self.records]


@dataclass
class Violation:
    code: str
    detail: str
    conn_id: int

    def __str__(self) -> str:
        return f"[{self.code}] connection {self.conn_id}: {self.detail}"


@dataclass
class Fault:
    """One planned misbehaviour. See the FakeScale methods that create it."""

    action: str
    kind: str | None = None  # message kind to match, None matches any
    times: int | None = 1  # None means every time
    seconds: float = 0.0
    apply: bool = True  # does the scale keep the items despite the fault
    raw: bytes = b""
    after_bytes: int = 0
    match: Callable[[Message], bool] | None = None
    fired: int = 0

    def matches(self, msg: Message) -> bool:
        if self.times is not None and self.fired >= self.times:
            return False
        if self.kind is not None and msg.kind != self.kind:
            return False
        if self.match is not None and not self.match(msg):
            return False
        return True


class _Conn:
    def __init__(self, sock: socket.socket, conn_id: int):
        self.sock = sock
        self.id = conn_id
        self.buf = b""
        # new -> ready (after bX01XX) -> done (after GPF0)
        # new -> reading (after LP) -> read_done (after LP00End)
        self.state = "new"
        self.closed = False
        self.accepted_any = False
        self.blocks: list[list[Record]] = []
        self.block_at = 0
        self.resends = 0


def _reset_close(sock: socket.socket) -> None:
    """Close with a TCP reset, like a scale that drops off the network."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def chain(body: bytes, pairs: int) -> bytes:
    """body, then `pairs` check characters each followed by CR. Each check
    covers everything before it, including the earlier pairs."""
    out = body
    for _ in range(pairs):
        out += check_char(out) + CR
    return out


def read_block_bytes(records: list[Record], resends: int = 0) -> bytes:
    """One LP block: LP, two digit count, the records each ending in TAB LF,
    then a chain of 2 pairs, plus 2 more for every resend."""
    body = f"LP{len(records):02d}".encode()
    for r in records:
        body += "\t".join(r).encode("latin-1") + b"\t\n"
    return chain(body, 2 + 2 * resends)


class FakeScale:
    def __init__(
        self,
        *,
        port: int = 0,
        serial: str = "FAKE-000001",
        max_id: int = MAX_ID,
        reply_delay: dict[str, float] | None = None,
        timing_factor: float = 0.0,
        beat_stall_seconds: float = 0.0,
        beat_period_seconds: float = BEAT_PERIOD,
        post_write_refusal_seconds: float = REAL_POST_WRITE_REFUSAL,
        post_write_mode: str = "refuse",
        read_delay: float = 0.0,
        queue_sessions: bool = False,
        allow_cx: bool = True,
        allow_clock: bool = False,
        allow_legacy: bool = False,
        allow_lp_params: bool = False,
        strict_sequence: bool = True,
        store_valid_in_errored_batch: bool = False,
        freeze_lp_at_id: int | None = None,
        freeze_on_gap: bool = False,
    ):
        """
        reply_delay: seconds to wait before replying, by kind, for example
            {"bX01XX": 1.0, "GP": 0.25}.
        timing_factor: 1.0 gives the real waits (bX01XX 1.04 s, GP of 5 records
            0.7 s, LP blocks 0.085 s). 0.1 gives a tenth of that. 0 gives none.
        beat_stall_seconds: the long wait that one GP batch gets about every
            beat_period_seconds. 0 turns it off. Real scales: 3.25 for the
            slowest (BEAT_STALL_COUNTER), 1.75 to 1.85 for the others.
            trigger_beat() makes the next GP batch stall once, at a known time.
        post_write_refusal_seconds: after GPF0, if the session had an accepted
            record, new connections are refused for this long. Real: 10.5.
            0 turns it off. post_write_mode "refuse" stops listening (a clean
            refusal). "silent" accepts connections and says nothing. "reset"
            accepts connections and resets them, the bare RST a real scale gives.
        read_delay: extra seconds before each LP block.
        queue_sessions: one connection at a time. A new connection is not
            served until the one before it has closed, as seen on the real
            scale.
        allow_cx: answer CX (default, the library uses it).
        allow_clock: also answer SetHORA and SetFECHA. The library never
            sends these, so by default they are a violation.
        allow_legacy: same as allow_clock.
        allow_lp_params: the real scale ignores anything after LP. The
            library must send exactly LP, so anything else is a violation
            unless this is set.
        strict_sequence: bX01XX must come before GP, and nothing after GPF0.
        store_valid_in_errored_batch: unknown on the real scale. If a GP
            batch has one bad id, are the good records kept? Default no.
        freeze_lp_at_id: a read hangs on the block holding this id, or the
            first block past it, and the scale drops off the network until
            reboot(). COUNTER did this asking for ids 41 to 45.
        freeze_on_gap: a read hangs the same way on the block that reaches a
            gap: an empty id with a record after it, or an empty start. A table
            with ids 1 to N all filled reads fine. Not proven on a real scale,
            only suspected. Without this option a gap reads as described under
            ASSUMPTIONS below.
        """
        self.host = "127.0.0.1"
        self._port = port
        self.serial = serial
        self.max_id = max_id
        self.reply_delay = dict(reply_delay or {})
        self.timing_factor = timing_factor
        self.beat_stall_seconds = beat_stall_seconds
        self.beat_period_seconds = beat_period_seconds
        self.post_write_refusal_seconds = post_write_refusal_seconds
        self.post_write_mode = post_write_mode
        self.read_delay = read_delay
        self.queue_sessions = queue_sessions
        self.allow_cx = allow_cx
        self.allow_clock = allow_clock or allow_legacy
        self.allow_lp_params = allow_lp_params
        self.strict_sequence = strict_sequence
        self.store_valid_in_errored_batch = store_valid_in_errored_batch
        self.freeze_lp_at_id = freeze_lp_at_id
        self.freeze_on_gap = freeze_on_gap
        self.frozen = False
        self.freezes = 0
        self.end_read_after_blocks: int | None = None
        self.end_read_every_time = True
        self._resend_at_id: int | None = None
        self._resends_left: int | None = 0
        self._hangup_wait = 0.0
        self._hangup_talks = True
        self._hangup_times: int | None = 0

        self._lock = threading.RLock()
        self._items: dict[int, Record] = {}
        self._hidden: dict[int, Record] = {}  # id 0: accepted, never read back
        self.messages: list[Message] = []
        self.violations: list[Violation] = []
        self.events: list[str] = []
        self._allowed_codes: set[str] = set()
        self.faults: list[Fault] = []
        self.fail_ids: set[int] = set()
        self.connection_count = 0
        self.max_concurrent = 0
        self.bytes_received = 0
        self.refusal_windows = 0
        self._open = 0
        self._reset_next = 0
        self._beat_next: float | None = None
        self._force_beats = 0
        self._refusal_gen = 0
        self._silent_until = 0.0
        self._reset_until = 0.0
        self._held: list[socket.socket] = []
        self._gate = threading.Lock()

        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._conns: list[_Conn] = []
        self._timers: list[threading.Timer] = []

    # ---------- lifecycle ----------

    @property
    def port(self) -> int:
        return self._port

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self._port)

    def start(self) -> "FakeScale":
        self._stop.clear()
        self.start_listening()
        return self

    def stop(self) -> None:
        self._stop.set()
        for t in self._timers:
            t.cancel()
        self.stop_listening()
        with self._lock:
            conns = list(self._conns)
            held, self._held = self._held, []
        for c in conns:
            try:
                c.sock.close()
            except OSError:
                pass
        for s in held:
            try:
                s.close()
            except OSError:
                pass
        for t in list(self._threads):
            t.join(timeout=2)

    def __enter__(self) -> "FakeScale":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def start_listening(self) -> None:
        with self._lock:
            if self._listener is not None:
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform != "win32":
                # On Windows SO_REUSEADDR lets two programs share a port. Skip it.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self._port))
            s.listen(16)
            s.settimeout(0.1)
            self._port = s.getsockname()[1]
            self._listener = s
        t = threading.Thread(target=self._accept_loop, args=(s,), daemon=True)
        self._accept_thread = t
        t.start()

    def stop_listening(self) -> None:
        """Nothing listens, so new connections are refused. Open ones stay."""
        with self._lock:
            s, self._listener = self._listener, None
        if s is not None:
            # On Linux a close alone leaves it listening until the accept thread's wait ends, so
            # connects in that moment succeed. Shutting down first stops it at once.
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Windows refuses this on a listening socket, and its close takes effect at once
            try:
                s.close()
            except OSError:
                pass
        th = self._accept_thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout=2)
        self._accept_thread = None

    # ---------- faults ----------

    def _add(self, fault: Fault) -> Fault:
        with self._lock:
            self.faults.append(fault)
        return fault

    def delay_reply(self, seconds: float, kind: str | None = "GP", times: int | None = 1,
                    apply: bool = True) -> Fault:
        """Wait before replying, then reply normally. 3.25 is what we saw."""
        return self._add(Fault("delay", kind, times, seconds=seconds, apply=apply))

    def stall(self, kind: str | None = "GP", times: int | None = 1, apply: bool = True) -> Fault:
        """Read the message, keep the connection open, never reply."""
        return self._add(Fault("stall", kind, times, apply=apply))

    def drop_before_reply(self, kind: str | None = "GP", times: int | None = 1,
                          apply: bool = True) -> Fault:
        """Read the whole message, then drop the connection with no reply.
        apply says whether the scale kept the items before dropping."""
        return self._add(Fault("drop_before_reply", kind, times, apply=apply))

    def drop_mid_send(self, kind: str = "GP", after_bytes: int = 20, times: int | None = 1) -> Fault:
        """Drop the connection once after_bytes of a matching message have arrived,
        before the message is handled. Nothing in it is kept."""
        return self._add(Fault("drop_mid_send", kind, times, after_bytes=after_bytes))

    def force_error(self, kind: str | None = "GP", times: int | None = 1) -> Fault:
        """Reply Error to the next matching batch. Nothing in it is kept."""
        return self._add(Fault("force_error", kind, times))

    def error_on_ids(self, *ids: int) -> None:
        """Any batch holding one of these ids gets Error, every time."""
        with self._lock:
            self.fail_ids.update(ids)

    def bad_reply_check(self, kind: str | None = "GP", times: int | None = 1,
                        where: str = "trailer") -> Fault:
        """Reply with a wrong check character. where is 'trailer' (the framed
        part) or 'echo' (the leading character on a GP reply)."""
        assert where in ("trailer", "echo")
        return self._add(Fault("bad_" + where, kind, times))

    def raw_reply(self, data: bytes, kind: str | None = "GP", times: int | None = 1) -> Fault:
        """Send these exact bytes instead of the normal reply."""
        return self._add(Fault("raw_reply", kind, times, raw=data))

    def stall_read(self, block: int | None = None, times: int | None = 1) -> Fault:
        """Never answer an LP request or an Ok (for block number `block`,
        counting from 0, if given). The connection stays open."""
        f = Fault("stall", None, times)
        f.match = lambda m: m.kind in ("LP", "Ok") and (block is None or self._block_of(m) == block)
        return self._add(f)

    def drop_read(self, block: int | None = None, times: int | None = 1) -> Fault:
        """Drop the connection instead of sending block number `block`."""
        f = Fault("drop_before_reply", None, times)
        f.match = lambda m: m.kind in ("LP", "Ok") and (block is None or self._block_of(m) == block)
        return self._add(f)

    def _block_of(self, m: Message) -> int:
        # LP asks for block 0. The nth Ok on a connection asks for block n.
        with self._lock:
            return sum(1 for x in self.messages if x.conn_id == m.conn_id and x.kind == "Ok" and x.seq < m.seq) + (
                1 if m.kind == "Ok" else 0
            )

    def refuse_for(self, seconds: float) -> None:
        """Nothing listens for this many seconds, then the fake listens again
        on the same port. Connects in the meantime are refused."""
        self._begin_refusal(seconds, "refuse")

    def _begin_refusal(self, seconds: float, mode: str) -> None:
        with self._lock:
            self._refusal_gen += 1
            gen = self._refusal_gen
            self.refusal_windows += 1
            if mode == "silent":
                self._silent_until = time.monotonic() + seconds
            if mode == "reset":
                self._reset_until = time.monotonic() + seconds
        if mode == "refuse":
            self.stop_listening()

        def reopen() -> None:
            if self._stop.is_set() or gen != self._refusal_gen:
                return
            if mode == "refuse" and not self.frozen:
                self.start_listening()
            with self._lock:
                held, self._held = self._held, []
            for s in held:
                try:
                    s.close()
                except OSError:
                    pass

        t = threading.Timer(seconds, reopen)
        t.daemon = True
        self._timers.append(t)
        t.start()

    def _freeze(self) -> None:
        """Off the network. The connection that froze it stays open and silent."""
        with self._lock:
            self.frozen = True
            self.freezes += 1
        self.stop_listening()

    def reboot(self) -> None:
        """Back on the network after a freeze, holding what it held. The table has not changed,
        so whatever froze it will freeze it again."""
        with self._lock:
            self.frozen = False
        self.start_listening()

    def _gap_at(self) -> int | None:
        """The lowest empty id with a record after it."""
        with self._lock:
            ids = sorted(i for i in self._items if i >= 1)
        return next((i for i in range(1, ids[-1]) if i not in ids), None) if ids else None

    def _read_freezes(self, conn: _Conn) -> bool:
        """Does the block this read is asking for now freeze the scale?"""
        if conn.block_at >= len(conn.blocks):
            return False
        marks = [self.freeze_lp_at_id, self._gap_at() if self.freeze_on_gap else None]
        marks = [m for m in marks if m is not None]
        return bool(marks) and int(conn.blocks[conn.block_at][-1][0]) >= min(marks)

    def end_read_early(self, after_blocks: int, every_time: bool = True) -> None:
        """Reads say LP00End after this many blocks, though the fake stores more."""
        with self._lock:
            self.end_read_after_blocks = after_blocks
            self.end_read_every_time = every_time

    def resend_block_at_id(self, item_id: int, times: int | None = 1) -> None:
        """The Ok after the block holding this id gets that same block again, with the longer
        chain, not the next one. Nobody has seen why a real scale does this, only that one
        seemed to. See PROTOCOL.md section 3. times counts across reads, None means every time."""
        with self._lock:
            self._resend_at_id = item_id
            self._resends_left = times

    def _resend_now(self, conn: _Conn) -> bool:
        with self._lock:
            if self._resend_at_id is None or self._resends_left == 0 or conn.block_at >= len(conn.blocks):
                return False
            if not any(int(record[0]) == self._resend_at_id for record in conn.blocks[conn.block_at]):
                return False
            if self._resends_left is not None:
                self._resends_left -= 1
            return True

    def slow_to_close(self, seconds: float = float("inf"), *, talks: bool = True, times: int | None = 1) -> None:
        """When the client hangs up, wait this long before closing. The default never closes,
        which is what a real freeze looks like: the FIN goes unanswered. With talks, the block
        and LP00End still go out first, so bytes alone do not mean it closed. times counts
        hang ups in the reading state, None means every time."""
        with self._lock:
            self._hangup_wait = seconds
            self._hangup_talks = talks
            self._hangup_times = times

    def _take_slow_close(self, conn: _Conn) -> tuple[float, bool]:
        with self._lock:
            if conn.state != "reading" or self._hangup_times == 0:
                return 0.0, True
            if self._hangup_times is not None:
                self._hangup_times -= 1
            return self._hangup_wait, self._hangup_talks

    def reset_next_connections(self, n: int = 1) -> None:
        """Accept the next n connections and reset them at once."""
        with self._lock:
            self._reset_next += n

    def trigger_beat(self, times: int = 1) -> None:
        """The next `times` GP batches get the long wait, as if the 5.6 second
        beat had just fallen. Needs beat_stall_seconds, or uses 1.0."""
        with self._lock:
            self._force_beats += times

    def clear_faults(self) -> None:
        with self._lock:
            self.faults.clear()
            self.fail_ids.clear()

    # ---------- state for assertions ----------

    @property
    def items(self) -> dict[int, Record]:
        """A copy of the items a read would show, by id. 20 text fields each.
        Id 0 is not here: the real scale says Ok to it and never shows it."""
        with self._lock:
            return dict(self._items)

    @property
    def hidden_items(self) -> dict[int, Record]:
        """Records the scale said Ok to but would never show in a read (id 0)."""
        with self._lock:
            return dict(self._hidden)

    def item(self, item_id: int) -> Record | None:
        with self._lock:
            return self._items.get(item_id)

    def preload(self, *records: Record) -> None:
        """Put items on the fake as if an earlier send had stored them."""
        with self._lock:
            for r in records:
                if len(r) != FIELD_COUNT:
                    raise ValueError(f"a record needs {FIELD_COUNT} fields, got {len(r)}")
                rec = tuple(str(x) for x in r)
                (self._hidden if int(rec[0]) == 0 else self._items)[int(rec[0])] = rec

    def set_table(self, records: Iterable[Record]) -> None:
        """Replace everything the fake holds with these records."""
        with self._lock:
            self._items = {}
            self._hidden = {}
        self.preload(*records)

    def remove(self, item_id: int) -> None:
        """Take an item off the fake, as a lab delete or a factory reset would."""
        with self._lock:
            self._items.pop(item_id, None)

    @property
    def nothing_received(self) -> bool:
        """True if no connection was made and no byte arrived."""
        with self._lock:
            return self.connection_count == 0 and self.bytes_received == 0

    def kinds(self) -> list[str]:
        with self._lock:
            return [m.kind for m in self.messages]

    def messages_of(self, kind: str) -> list[Message]:
        with self._lock:
            return [m for m in self.messages if m.kind == kind]

    def accepted_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._items)

    def wait_for_messages(self, count: int, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if len(self.messages) >= count:
                    return True
            time.sleep(0.01)
        return False

    def allow(self, *codes: str) -> None:
        """Tell the fake that a violation code is expected in this test."""
        self._allowed_codes.update(codes)

    def unexpected_violations(self) -> list[Violation]:
        with self._lock:
            return [v for v in self.violations if v.code not in self._allowed_codes]

    def assert_clean(self) -> None:
        bad = self.unexpected_violations()
        if bad:
            raise AssertionError(
                "The fake scale saw traffic outside the known protocol:\n  "
                + "\n  ".join(str(v) for v in bad)
            )

    def assert_one_at_a_time(self) -> None:
        """The library must use one connection at a time per scale."""
        if self.max_concurrent > 1:
            raise AssertionError(f"{self.max_concurrent} connections were open at once")

    # ---------- the server ----------

    def _violate(self, conn: _Conn, code: str, detail: str) -> None:
        with self._lock:
            self.violations.append(Violation(code, detail, conn.id))

    def _accept_loop(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.connection_count += 1
                conn_id = self.connection_count
                reset = self._reset_next > 0
                if reset:
                    self._reset_next -= 1
                reset = reset or time.monotonic() < self._reset_until
                silent = time.monotonic() < self._silent_until
                if silent:
                    self._held.append(sock)
            if reset:
                _reset_close(sock)
                continue
            if silent:
                continue
            conn = _Conn(sock, conn_id)
            with self._lock:
                self._conns.append(conn)
                self._open += 1
                self.max_concurrent = max(self.max_concurrent, self._open)
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.append(t)
            t.start()

    def _serve(self, conn: _Conn) -> None:
        sock = conn.sock
        sock.settimeout(0.1)
        gate = False
        try:
            if self.queue_sessions:
                while not self._gate.acquire(timeout=0.1):
                    if self._stop.is_set():
                        return
                gate = True
            while not self._stop.is_set() and not conn.closed:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    wait, talks = self._take_slow_close(conn)
                    if talks:
                        self._client_closed(conn)
                    self._stop.wait(wait if wait != float("inf") else None)
                    break
                with self._lock:
                    self.bytes_received += len(data)
                conn.buf += data
                if not self._drain(conn):
                    break
        finally:
            with self._lock:
                self._open -= 1
            try:
                sock.close()
            except OSError:
                pass
            if gate:
                self._gate.release()

    def _client_closed(self, conn: _Conn) -> None:
        """The client hung up. In the middle of a read the scale sends the
        block again and then LP00End. It goes nowhere, but it is recorded."""
        if conn.state == "reading":
            with self._lock:
                self.events.append(f"read_closed_mid conn {conn.id} block {conn.block_at}")
            try:
                if conn.resends == 0:
                    # Seen in a capture: with no resend yet, block again, then End.
                    # After a resend already went out (a CX during the read), only End.
                    conn.resends += 1
                    self._send(conn, self._block_reply(conn))
                self._send(conn, frame(b"LP00End"))
            except Exception:
                pass

    def _next_message(self, conn: _Conn) -> bytes | None:
        buf = conn.buf
        if buf.startswith((b"SetHORA,", b"SetFECHA,")):
            got = split_clock(buf)
        else:
            got = split_framed(buf)
        if got is None:
            return None
        msg, conn.buf = got
        return msg

    def _drain(self, conn: _Conn) -> bool:
        """Handle every complete message in the buffer. False means close."""
        # E messages and L messages other than LP are never allowed. LG holds
        # password like values and E may be a delete that took FRONT down.
        if conn.buf.startswith(FORBIDDEN_PREFIXES):
            with self._lock:
                self.messages.append(
                    Message(len(self.messages) + 1, conn.id, "FORBIDDEN", conn.buf, False,
                            at=time.monotonic())
                )
            self._violate(conn, "forbidden_message", repr(conn.buf[:12]))
            return False
        # Text that starts like nothing we know is an unknown message, framed or not.
        # The real scale closes the connection on it, with no reply.
        if conn.buf and not any(
            conn.buf.startswith(p) or p.startswith(conn.buf) for p in KNOWN_PREFIXES
        ):
            with self._lock:
                self.messages.append(
                    Message(len(self.messages) + 1, conn.id, "UNKNOWN", conn.buf, False,
                            at=time.monotonic())
                )
            self._violate(conn, "unknown_message", repr(conn.buf[:80]))
            return False
        # A fault can drop the connection while a batch is still arriving.
        fault = self._partial_drop(conn)
        if fault is not None:
            _reset_close(conn.sock)
            conn.closed = True
            return False
        while True:
            raw = self._next_message(conn)
            if raw is None:
                return True
            if conn.buf:
                self._violate(conn, "pipelined",
                              "bytes arrived before the previous reply was sent")
                return False
            if not self._handle(conn, raw):
                return False

    def _partial_drop(self, conn: _Conn) -> Fault | None:
        buf = conn.buf
        if not buf:
            return None
        with self._lock:
            for f in self.faults:
                if f.action != "drop_mid_send" or (f.times is not None and f.fired >= f.times):
                    continue
                if f.kind and not buf.startswith(f.kind.encode()):
                    continue
                if len(buf) >= f.after_bytes:
                    f.fired += 1
                    return f
        return None

    @staticmethod
    def _classify(raw: bytes) -> tuple[str, bytes]:
        if raw.startswith(b"SetHORA,"):
            return "SetHORA", raw
        if raw.startswith(b"SetFECHA,"):
            return "SetFECHA", raw
        text = raw[:-4]  # strip TAB LF check CR
        if text == b"RI":
            return "RI", text
        if text == b"bX01XX":
            return "bX01XX", text
        if text == b"GPF0":
            return "GPF0", text
        if text == b"CX":
            return "CX", text
        if text == b"Ok":
            return "Ok", text
        if text.startswith(b"LP"):
            return "LP", text
        if text.startswith(b"GP") and text[2:4].isdigit():
            return "GP", text
        return "UNKNOWN", text

    def _parse_gp(self, conn: _Conn, text: bytes) -> list[Record] | None:
        count = int(text[2:4])
        parts = text[4:].split(b"\t\n")
        records: list[Record] = []
        for p in parts:
            fields = tuple(f.decode("latin-1") for f in p.split(b"\t"))
            if len(fields) != FIELD_COUNT:
                self._violate(conn, "malformed_gp",
                              f"record has {len(fields)} fields, expected {FIELD_COUNT}")
                return None
            if not fields[0].isdigit():
                self._violate(conn, "malformed_gp", f"id {fields[0]!r} is not a whole number")
                return None
            records.append(fields)
        if count != len(records) or count == 0:
            self._violate(conn, "malformed_gp",
                          f"count says {count}, batch holds {len(records)} records")
            return None
        return records

    def _take_fault(self, msg: Message, actions: Iterable[str]) -> Fault | None:
        with self._lock:
            for f in self.faults:
                if f.action in actions and f.matches(msg):
                    f.fired += 1
                    return f
        return None

    # ----- timing -----

    def _beat_wait(self) -> float:
        with self._lock:
            if self._force_beats > 0:
                self._force_beats -= 1
                return self.beat_stall_seconds or 1.0
            if not self.beat_stall_seconds:
                return 0.0
            now = time.monotonic()
            if self._beat_next is None:
                self._beat_next = now + self.beat_period_seconds
                return 0.0
            if now >= self._beat_next:
                missed = int((now - self._beat_next) // self.beat_period_seconds) + 1
                self._beat_next += missed * self.beat_period_seconds
                return self.beat_stall_seconds
        return 0.0

    def _base_wait(self, kind: str, msg: Message) -> float:
        d = self.reply_delay.get(kind, 0.0)
        f = self.timing_factor
        if f:
            if kind == "bX01XX":
                d += REAL_BX_WAIT * f
            elif kind == "GP":
                d += (REAL_GP_WAIT_FIRST + REAL_GP_WAIT_EXTRA * (len(msg.records) - 1)) * f
            elif kind in ("LP", "Ok"):
                d += REAL_READ_WAIT * f
        if kind in ("LP", "Ok"):
            d += self.read_delay
        if kind == "GP":
            d += self._beat_wait()
        return d

    # ----- reading -----

    def _table_blocks(self) -> list[list[Record]]:
        with self._lock:
            ids = sorted(i for i in self._items if i >= 1)
            recs = [self._items[i] for i in ids]
            blocks = [recs[i : i + BLOCK_SIZE] for i in range(0, len(recs), BLOCK_SIZE)]
            if self.end_read_after_blocks is not None:
                # A read that says LP00End early, though the table goes on. Nobody has seen this on a
                # good table, and nobody has ruled it out, so nothing may rely on where a read ends.
                blocks = blocks[: self.end_read_after_blocks]
                self.end_read_after_blocks = self.end_read_after_blocks if self.end_read_every_time else None
        return blocks

    def _block_reply(self, conn: _Conn) -> bytes:
        if conn.block_at >= len(conn.blocks):
            return frame(b"LP00End")
        return read_block_bytes(conn.blocks[conn.block_at], conn.resends)

    # ----- the handler -----

    def _handle(self, conn: _Conn, raw: bytes) -> bool:
        kind, text = self._classify(raw)
        clock = kind in CLOCK_KINDS
        framed = not clock
        check_ok = message_ok_check(raw) if framed else True
        with self._lock:
            msg = Message(len(self.messages) + 1, conn.id, kind, raw, check_ok, at=time.monotonic())
            self.messages.append(msg)

        if kind == "UNKNOWN":
            self._violate(conn, "unknown_message", repr(raw[:80]))
            return False
        if clock and not self.allow_clock:
            self._violate(conn, "unknown_message", f"{kind} is not part of the library protocol")
            return False
        if kind == "CX" and not self.allow_cx:
            self._violate(conn, "unknown_message", "CX is not allowed on this fake")
            return False
        if framed and not check_ok:
            # The real scale closes the connection and says nothing.
            self._violate(conn, "bad_check", f"{kind} sent with a wrong check character")
            return False
        if kind == "LP" and text != b"LP" and not self.allow_lp_params:
            self._violate(conn, "lp_params", f"LP with extra text {text!r}: the library sends exactly LP")
            return False

        if self.strict_sequence and not clock:
            st = conn.state
            ok = (
                (kind == "RI" and st == "new")
                or (kind == "bX01XX" and st == "new")
                or (kind == "LP" and st == "new")
                or (kind == "CX" and st in ("new", "ready", "reading", "read_done"))
                or (kind == "Ok" and st == "reading")
                or (kind in ("GP", "GPF0") and st == "ready")
            )
            if not ok:
                self._violate(conn, "sequence", f"{kind} while connection is {st}")
                return False

        if kind == "RI":
            return False  # no reply, the scale closes

        reply = b""
        echo = b""
        result = b""
        msg_store: list[Record] = []
        if kind == "bX01XX":
            conn.state = "ready"
            reply = frame(b"bXXXOk")
        elif kind == "GPF0":
            conn.state = "done"
            reply = frame(b"GPF0Ok")
        elif kind == "CX":
            if conn.state == "reading":
                # Anything but Ok during a read: the same block again.
                conn.resends += 1
                reply = self._block_reply(conn)
            else:
                reply = frame(
                    b"CX00\t" + self.serial.encode() + b"\t40.00 lb\t0.010 lb\tEnglish\t\tlb\tW-LABEL\t2"
                )
        elif kind in CLOCK_KINDS:
            reply = b"OK\n"
        elif kind == "LP":
            conn.state = "reading"
            conn.blocks = self._table_blocks()
            conn.block_at = 0
            conn.resends = 0
            reply = self._block_reply(conn)
        elif kind == "Ok":
            if self._resend_now(conn):
                conn.resends += 1
            else:
                conn.block_at += 1
                conn.resends = 0
                if conn.block_at >= len(conn.blocks):
                    conn.state = "read_done"
            reply = self._block_reply(conn)
        elif kind == "GP":
            records = self._parse_gp(conn, text)
            if records is None:
                return False
            msg.records = records
            bad = any(int(r[0]) > self.max_id or int(r[0]) in self.fail_ids for r in records)
            forced = self._take_fault(msg, ("force_error",))
            if bad or forced:
                result = b"Error"
                msg_store = (
                    [r for r in records if int(r[0]) <= self.max_id and int(r[0]) not in self.fail_ids]
                    if (bad and self.store_valid_in_errored_batch and not forced)
                    else []
                )
            else:
                result = b"Ok"
                msg_store = records
            # echo = check character of what was received (the message body)
            echo = check_char(raw[:-2])
            reply = gp_reply(echo, result)
        else:  # pragma: no cover
            return False

        if kind in ("LP", "Ok") and self._read_freezes(conn):
            self._freeze()
            self._wait(conn, 3600.0)
            return False

        fault = self._take_fault(
            msg, ("delay", "stall", "drop_before_reply", "bad_trailer", "bad_echo", "raw_reply")
        )
        apply = fault.apply if fault is not None else True
        if kind == "GP" and apply:
            with self._lock:
                for r in msg_store:
                    rid = int(r[0])
                    if rid == 0:
                        self._hidden[0] = r
                    else:
                        self._items[rid] = r
            if result == b"Ok":
                conn.accepted_any = True

        if fault is None:
            d = self._base_wait(kind, msg)
            if d and self._wait(conn, d) == "closed":
                return False
            # The gap starts before the reply goes out. Otherwise a quick client can connect in
            # the moment between, which never happens the other way round on the real scale's timing.
            if kind == "GPF0" and conn.accepted_any and self.post_write_refusal_seconds > 0:
                self._begin_refusal(self.post_write_refusal_seconds, self.post_write_mode)
            sent = self._send(conn, reply)
            if kind == "LP" and conn.state == "reading" and not conn.blocks:
                conn.state = "read_done"
            return sent

        if fault.action == "delay":
            if self._wait(conn, fault.seconds) == "closed":
                return False
            return self._send(conn, reply)
        if fault.action == "stall":
            self._wait(conn, 3600.0)
            return False
        if fault.action == "drop_before_reply":
            _reset_close(conn.sock)
            conn.closed = True
            return False
        if fault.action == "bad_trailer":
            return self._send(conn, reply[:-2] + _wrong(reply[-2:-1]) + CR)
        if fault.action == "bad_echo":
            if kind == "GP":
                return self._send(conn, gp_reply(_wrong(echo), result))
            return self._send(conn, reply[:-2] + _wrong(reply[-2:-1]) + CR)
        if fault.action == "raw_reply":
            return self._send(conn, fault.raw)
        return False  # pragma: no cover

    def _wait(self, conn: _Conn, seconds: float) -> str:
        """Sleep while a reply is owed. Notice if the client hangs up or
        sends something it should not."""
        end = time.monotonic() + seconds
        while not self._stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return "done"
            try:
                r, _, _ = select.select([conn.sock], [], [], min(left, 0.05))
            except (OSError, ValueError):
                return "closed"
            if r:
                try:
                    data = conn.sock.recv(65536)
                except OSError:
                    return "closed"
                if not data:
                    return "closed"
                with self._lock:
                    self.bytes_received += len(data)
                self._violate(conn, "pipelined", "bytes arrived while a reply was owed")
                return "closed"
        return "closed"

    def _send(self, conn: _Conn, data: bytes) -> bool:
        try:
            conn.sock.sendall(data)
            return True
        except OSError:
            return False


def gp_reply(echo: bytes, result: bytes) -> bytes:
    """Reply to a GP batch: the echo character, CR, Ok or Error, TAB, LF,
    then a check character over all of it, then CR. The check covers the
    echo and the CR too, not just the word."""
    body = echo + CR + result + b"\t\n"
    return body + check_char(body) + CR


def _wrong(ch: bytes) -> bytes:
    """A different check character from the right one."""
    return bytes([0x30 + ((ch[0] - 0x30 + 1) % 11)])


def make_record(
    item_id: int,
    plu: str | int,
    description: str,
    price: str,
    *,
    code: str | int | None = None,
    field6: str = "",
    fields_7_12: tuple[str, ...] = ("1", "1", "0", "0", "0", "0"),
    timestamp: str = "2026-01-01 00:00:00",
    fields_14_20: tuple[str, ...] = ("0",) * 7,
) -> Record:
    """Build a 20 field record from made up values, for tests."""
    plu_s = str(plu)
    return (
        str(item_id),
        str(code if code is not None else plu_s),
        plu_s,
        description,
        price,
        field6,
        *fields_7_12,
        timestamp,
        *fields_14_20,
    )
