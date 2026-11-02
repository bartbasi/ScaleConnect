"""One session with one scale. The only module that opens a socket to a scale.

Standard library only, plus this package's own standard library modules.

Public operations are named: identify, read_items and send_items. There is no way to
send arbitrary text. Two paths reach the socket, each with its own guard:

- The write path needs write_enabled. It sends only RI, bX01XX, GP batches and GPF0.
- The read path needs write_enabled or read_enabled. It sends only CX, LP and Ok.

"Not write enabled" does not mean "safe to read". A scale with neither flag is never
contacted. A write needs the serial to match first, and send_items checks that itself,
so no caller can skip it. Nothing here resends a record after sending has begun.

Read blocks never go in the reply list that reaches the change log.

A delete has no message of its own. It is a send_items call with a placeholder record,
because overwriting an id removes the old PLU. No E message exists here, and none can be
framed.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import protocol
from .config import MAX_CONNECT_WAIT_SECONDS, ScaleConfig, Settings
from .errors import NotReadEnabled, NotWriteEnabled
from .models import ITEM_NOT_SENT, MAX_ITEM_ID, STATUS_SEVERITY, Item, ScaleStatus

READ_AVAILABLE = True
DELETE_AVAILABLE = True

# A read has frozen real scales: no reply, then gone from the network until rebooted. See
# docs/PROTOCOL.md section 3. Any read or identity that gets no reply says so.
FROZEN_NOTE = "The scale may have frozen, and may need a reboot."

_READ_SIZE = 4096
_CLOSE_WAIT = 2.0
_READ_MESSAGES = {
    "CX": protocol.build_identity,
    "LP": protocol.build_read_start,
    "Ok": protocol.build_read_next,
}


@dataclass(frozen=True)
class ItemOutcome:
    item_id: int
    plu: str
    status: str  # a ScaleStatus value, or ITEM_NOT_SENT
    said_error: bool = False  # its batch got Error, so it may or may not be stored


@dataclass(frozen=True)
class SendReport:
    scale: str
    status: ScaleStatus
    message: str
    attempts: int
    items: tuple[ItemOutcome, ...]
    replies: tuple[str, ...]


@dataclass(frozen=True)
class IdentityCheck:
    ok: bool
    status: ScaleStatus | None
    message: str
    serial: str | None = None


@dataclass(frozen=True)
class ReadResult:
    ok: bool
    status: ScaleStatus | None
    message: str
    items: dict[int, Item]
    complete: bool = False  # reached the end of the table, not an early stop


class _NoSession(Exception):
    """The session did not open, and no item record went out."""


class _OpenRefused(Exception):
    """The scale said Error to the opening message."""


class _ReplyLost(Exception):
    """Timeout, closed connection or a broken reply while waiting."""

    def __init__(self, message: str, *, no_reply: bool = False) -> None:
        super().__init__(message)
        self.no_reply = no_reply  # dropped with not one byte back


class _NoReplyYet(Exception):
    """The scale took the connection, then dropped it with not one byte back. On a busy scale
    this is the same answer as a refused connect, so it is retried the same way."""


class _CouldNotConnect(Exception):
    def __init__(self, message: str, *, connected: bool = False) -> None:
        super().__init__(message)
        self.connected = connected  # the last try got a connection, and then a drop


class _ReadStopped(Exception):
    def __init__(self, message: str, *, before_first_block: bool = False, no_reply: bool = False, silent: bool = False) -> None:
        super().__init__(message)
        self.before_first_block = before_first_block
        self.no_reply = no_reply
        self.silent = silent  # the scale stopped answering, as opposed to answering badly


@dataclass
class _Batch:
    items: tuple[Item, ...]
    message: bytes
    # not_sent, pending (Ok here but the session has not finished), accepted, rejected, unknown
    state: str = "not_sent"


class ScaleClient:
    def __init__(
        self,
        scale: ScaleConfig,
        settings: Settings,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._scale = scale
        self._settings = settings
        self._sleep = sleep
        self._replies: list[str] = []
        self._identity_confirmed = False

    # ------------------------------------------------------------ guards

    def _require_write_enabled(self) -> None:
        if self._scale.write_enabled is not True:
            raise NotWriteEnabled(f"Writes are not enabled for scale {self._scale.name}.")

    def _require_read_allowed(self) -> None:
        if self._scale.write_enabled is not True and self._scale.read_enabled is not True:
            raise NotReadEnabled(f"Contact is not enabled for scale {self._scale.name}.")

    # ------------------------------------------------------------ sockets

    def _open_socket(self) -> socket.socket:
        return socket.create_connection(
            (self._scale.address, self._scale.port),
            timeout=self._settings.connect_timeout_seconds,
        )

    def _connect(self) -> socket.socket:
        """A connection for the write path."""
        self._require_write_enabled()
        return self._open_socket()

    def _connect_read(self) -> socket.socket:
        """A connection for the read path."""
        self._require_read_allowed()
        return self._open_socket()

    def _transmit(self, sock: socket.socket, message: bytes) -> None:
        """The write path's only way out."""
        self._require_write_enabled()
        sock.settimeout(self._settings.reply_timeout_seconds)
        sock.sendall(message)

    def _send_read_message(self, sock: socket.socket, name: str) -> None:
        """The read path's only way out. It takes a name, so it can send only these three."""
        self._require_read_allowed()
        sock.settimeout(self._settings.reply_timeout_seconds)
        sock.sendall(_READ_MESSAGES[name]())

    def _end_session(self, sock: socket.socket) -> None:
        """Hang up, and give the scale a moment to finish before any next session.

        After an early stop the scale sends the block again and LP00End, then closes. A new
        session that starts before then seems to queue behind the old one. What it sends
        here is thrown away unread, and never kept.
        """
        try:
            sock.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + min(_CLOSE_WAIT, self._settings.reply_timeout_seconds)
            while self._read_chunk(sock, deadline):
                pass
        except OSError:
            pass
        finally:
            sock.close()

    def _connect_with_retry(self, attempt: Callable[[], Any]) -> Any:
        """Tries up to connect_attempts times, and the wait grows each time.

        It retries a connect that fails, at once or by timing out, and a read message dropped
        with no reply. It never retries once a scale has said anything, or has stalled.
        The waits are real time: see config.connect_retry_span_seconds.
        """
        attempts = self._settings.connect_attempts
        wait = self._settings.connect_retry_wait_seconds
        last = "no attempt was made"
        connected = False
        for tried in range(1, attempts + 1):
            try:
                return attempt()
            except OSError as exc:
                last = _describe(exc)
                connected = False
            except _NoReplyYet as exc:
                last = str(exc)
                connected = True
            if tried < attempts:
                self._sleep(wait)
                wait = min(wait * 2, MAX_CONNECT_WAIT_SECONDS)
        raise _CouldNotConnect(last, connected=connected)

    def _read_chunk(self, sock: socket.socket, deadline: float) -> bytes:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("no reply in time")
        sock.settimeout(left)
        return sock.recv(_READ_SIZE)

    def _await_reply(self, sock: socket.socket, *, keep: bool = True) -> bytes:
        """Wait for one whole reply. The wait is a limit on the whole reply, not per chunk."""
        deadline = time.monotonic() + self._settings.reply_timeout_seconds
        buffer = b""
        try:
            while True:
                taken = protocol.take_reply(buffer)
                if taken is not None:
                    reply, extra = taken
                    if keep:
                        self._replies.append(protocol.describe_reply(reply))
                    if extra:
                        raise _ReplyLost("extra bytes after the reply")
                    return reply
                if len(buffer) > protocol.MAX_REPLY_BYTES:
                    raise _ReplyLost("the reply is too long")
                chunk = self._read_chunk(sock, deadline)
                if not chunk:
                    raise _ReplyLost("the scale closed the connection", no_reply=not buffer)
                buffer += chunk
        except OSError as exc:
            raise _ReplyLost(_describe(exc), no_reply=isinstance(exc, ConnectionError) and not buffer) from exc
        finally:
            if keep and buffer and protocol.take_reply(buffer) is None:
                self._replies.append(protocol.describe_reply(buffer))

    def _await_close(self, sock: socket.socket) -> None:
        """After RI the scale closes. Whatever it says first has no known meaning."""
        deadline = time.monotonic() + self._settings.reply_timeout_seconds
        try:
            while self._read_chunk(sock, deadline):
                pass
        except (ConnectionResetError, ConnectionAbortedError):
            return
        except OSError as exc:
            raise _NoSession(f"the scale did not close after RI: {_describe(exc)}") from exc

    # ------------------------------------------------------------ identity

    def identify(self) -> IdentityCheck:
        """Step 1. Send CX and compare the serial with the config. Nothing else goes out."""
        self._require_read_allowed()
        return self._identify_once(retry=True)

    def _ask_identity(self) -> bytes:
        sock = self._connect_read()
        try:
            self._send_read_message(sock, "CX")
            return self._await_reply(sock, keep=False)
        except ConnectionError as exc:
            raise _NoReplyYet(_describe(exc)) from exc
        except _ReplyLost as exc:
            if exc.no_reply:
                raise _NoReplyYet(str(exc)) from exc
            raise
        finally:
            self._end_session(sock)

    def _identify_once(self, *, retry: bool) -> IdentityCheck:
        expected = self._scale.serial
        name = self._scale.name
        if not expected:
            return IdentityCheck(
                False, ScaleStatus.WRONG_SCALE, f"{name}: the config has no serial for this scale, so nothing was sent."
            )
        unconfirmed = f"{name}: could not confirm which scale this is"
        try:
            reply = self._connect_with_retry(self._ask_identity) if retry else self._ask_identity()
            found = protocol.parse_identity(reply)
        except _CouldNotConnect as exc:
            if exc.connected:
                return IdentityCheck(False, ScaleStatus.READ_FAILED, f"{unconfirmed} ({exc}). Nothing was written. {FROZEN_NOTE}")
            return IdentityCheck(False, ScaleStatus.UNREACHABLE, f"{name}: could not connect ({exc}). Nothing was sent. {FROZEN_NOTE}")
        except OSError as exc:
            return IdentityCheck(False, ScaleStatus.UNREACHABLE, f"{name}: could not connect ({_describe(exc)}). Nothing was sent. {FROZEN_NOTE}")
        except (_NoReplyYet, _ReplyLost) as exc:
            detail = exc.args[0] if exc.args else exc.__class__.__name__
            return IdentityCheck(False, ScaleStatus.READ_FAILED, f"{unconfirmed} ({detail}). Nothing was written. {FROZEN_NOTE}")
        except protocol.ReadReplyError as exc:
            detail = exc.args[0] if exc.args else exc.__class__.__name__
            return IdentityCheck(False, ScaleStatus.READ_FAILED, f"{unconfirmed} ({detail}). Nothing was written.")
        if found != expected:
            return IdentityCheck(
                False,
                ScaleStatus.WRONG_SCALE,
                f"{name}: the scale at this address has a different serial from the config. Nothing was written.",
                serial=found,
            )
        self._identity_confirmed = True
        return IdentityCheck(True, None, f"{name}: the serial matches.", serial=found)

    # ------------------------------------------------------------ reads

    def read_items(self, up_to_id: int | None = None, *, patient: bool = False) -> ReadResult:
        """Read with LP. Stops early once up_to_id is reached. A stall stops the read.

        There is no way to seek, so a read always starts at id 1. patient is for the read
        back: the scale ignores connections for about 10 to 11 seconds after a write, so
        keep trying through a window of read_back_window_seconds.
        """
        self._require_read_allowed()
        if patient:
            return self._read_patiently(up_to_id)
        if not self._identity_confirmed:
            identity = self.identify()
            if not identity.ok:
                return ReadResult(False, identity.status, identity.message, {})

        def attempt() -> tuple[dict[int, Item], bool]:
            sock = self._connect_read()
            try:
                return self._read_table(sock, up_to_id)
            except _ReadStopped as exc:
                if exc.no_reply:
                    raise _NoReplyYet(str(exc)) from exc
                raise
            finally:
                self._end_session(sock)

        name = self._scale.name
        try:
            items, complete = self._connect_with_retry(attempt)
        except _CouldNotConnect as exc:
            if exc.connected:
                return ReadResult(False, ScaleStatus.READ_FAILED, f"{name}: the read failed ({exc}). {FROZEN_NOTE}", {})
            return ReadResult(False, ScaleStatus.UNREACHABLE, f"{name}: could not connect to read ({exc}). {FROZEN_NOTE}", {})
        except _ReadStopped as exc:
            return ReadResult(False, ScaleStatus.READ_FAILED, _stopped_message(name, exc), {})
        return ReadResult(True, None, f"{name}: read {len(items)} records.", items, complete)

    def _read_patiently(self, up_to_id: int | None) -> ReadResult:
        name = self._scale.name
        deadline = time.monotonic() + self._settings.read_back_window_seconds
        last = "no attempt was made"
        silent = True

        def out_of_time() -> ReadResult:
            text = f"{name}: could not read back in time ({last})."
            if silent and FROZEN_NOTE not in text:
                text = f"{text} {FROZEN_NOTE}"
            return ReadResult(False, ScaleStatus.READ_FAILED, text, {})

        # The window is real time, whichever way the scale says no: at once, or not at all.
        while True:
            if not self._identity_confirmed:
                identity = self._identify_once(retry=False)
                if identity.status is ScaleStatus.WRONG_SCALE:
                    return ReadResult(False, identity.status, identity.message, {})
                if not identity.ok:
                    last = identity.message
                    silent = True
                    if time.monotonic() >= deadline:
                        return out_of_time()
                    self._sleep(self._settings.read_back_retry_seconds)
                    continue
            try:
                sock = self._connect_read()
            except OSError as exc:
                last = _describe(exc)
                silent = True
            else:
                try:
                    items, complete = self._read_table(sock, up_to_id)
                    return ReadResult(True, None, f"{name}: read {len(items)} records.", items, complete)
                except _ReadStopped as exc:
                    last = str(exc)
                    silent = exc.silent
                    # Once blocks have come, a problem is a real stall. Stop.
                    if not exc.before_first_block:
                        return ReadResult(False, ScaleStatus.READ_FAILED, _stopped_message(name, exc), {})
                finally:
                    self._end_session(sock)
            if time.monotonic() >= deadline:
                return out_of_time()
            self._sleep(self._settings.read_back_retry_seconds)

    def _read_table(self, sock: socket.socket, up_to_id: int | None) -> tuple[dict[int, Item], bool]:
        items: dict[int, Item] = {}
        buffer = b""
        last_id = 0
        first = True
        while True:
            try:
                if first:
                    self._send_read_message(sock, "LP")
                else:
                    self._sleep(self._settings.read_pace_seconds)
                    self._send_read_message(sock, "Ok")
                block, buffer = self._await_block(sock, buffer)
            except _ReadStopped as exc:
                raise _ReadStopped(str(exc), before_first_block=first, no_reply=first and exc.no_reply, silent=exc.silent) from exc
            except OSError as exc:
                dropped = first and isinstance(exc, ConnectionError)
                raise _ReadStopped(_describe(exc), before_first_block=first, no_reply=dropped, silent=True) from exc
            first = False
            if block.end:
                return items, True
            ids = [record.id for record in block.records]
            if ids[0] <= last_id or ids != sorted(set(ids)):
                # We sent only Ok, so this is not a step forward. Do not loop on it.
                raise _ReadStopped("the scale sent blocks out of order")
            for record in block.records:
                items[record.id] = record
            last_id = ids[-1]
            if last_id > MAX_ITEM_ID:
                raise _ReadStopped("the scale listed an id above the limit")
            if up_to_id is not None and last_id >= up_to_id:
                return items, False

    def _await_block(self, sock: socket.socket, buffer: bytes) -> tuple[protocol.ReadBlock, bytes]:
        deadline = time.monotonic() + self._settings.reply_timeout_seconds
        while True:
            try:
                taken = protocol.take_read_block(buffer)
            except protocol.ReadReplyError as exc:
                raise _ReadStopped(str(exc)) from exc
            if taken is not None:
                return taken
            chunk = self._read_chunk(sock, deadline)
            if not chunk:
                raise _ReadStopped("the scale closed the connection", no_reply=not buffer, silent=True)
            buffer += chunk

    # ------------------------------------------------------------ the write session

    def _open_session(self) -> socket.socket:
        """RI, then a new connection and bX01XX. Only failed connects are tried again, never
        an opening message that went out.

        If the scale takes an opening message and then stalls, stop. It may still be
        working, and more sessions could queue behind that work.
        """
        try:
            first = self._connect_with_retry(self._connect)
        except _CouldNotConnect as exc:
            raise _NoSession(f"could not connect: {exc}") from exc
        try:
            try:
                self._transmit(first, protocol.build_ri())
            except OSError as exc:
                raise _NoSession(f"could not send RI: {_describe(exc)}") from exc
            self._await_close(first)
        finally:
            first.close()
        try:
            sock = self._connect_with_retry(self._connect)
        except _CouldNotConnect as exc:
            raise _NoSession(f"could not connect: {exc}") from exc
        try:
            self._transmit(sock, protocol.build_open())
            reply = self._await_reply(sock)
        except (OSError, _ReplyLost) as exc:
            self._end_session(sock)
            raise _NoSession(f"no opening reply: {exc}") from exc
        if protocol.is_open_error(reply):
            self._end_session(sock)
            raise _OpenRefused("the scale said Error to the opening message")
        if not protocol.is_open_ok(reply):
            self._end_session(sock)
            raise _NoSession("the opening reply was not bXXXOk")
        return sock

    def _run_session(self, batches: Sequence[_Batch]) -> str:
        """Sends the batches once, and returns a note. Updates batch states.

        A batch counts as accepted only with its own Ok and then a clean finish on
        the same connection. GPF0Ok alone never makes anything accepted.
        """
        sock = self._open_session()
        pending: list[_Batch] = []
        note = ""
        try:
            for batch in batches:
                try:
                    self._transmit(sock, batch.message)
                    reply = self._await_reply(sock)
                except (OSError, _ReplyLost) as exc:
                    # Some or all of it may have gone out, so its state is not known.
                    batch.state = "unknown"
                    for done in pending:
                        done.state = "unknown"
                    return f"Lost the scale while waiting for its reply: {exc}."
                parsed = protocol.parse_batch_reply(reply, batch.message)
                if parsed.result is protocol.BatchResult.OK:
                    batch.state = "pending"
                    pending.append(batch)
                    continue
                if parsed.result is protocol.BatchResult.ERROR:
                    batch.state = "rejected"
                    note = parsed.detail
                    break
                batch.state = "unknown"
                for done in pending:
                    done.state = "unknown"
                return parsed.detail
            # Finish even after a rejection, so batches already accepted are closed out.
            try:
                self._transmit(sock, protocol.build_finish())
                finish = self._await_reply(sock)
            except (OSError, _ReplyLost) as exc:
                for done in pending:
                    done.state = "unknown"
                return note or f"No reply to the end of the send: {exc}."
            if protocol.is_finish_ok(finish):
                for done in pending:
                    done.state = "accepted"
            else:
                for done in pending:
                    done.state = "unknown"
                note = note or "The reply to the end of the send was not GPF0Ok."
            return note
        finally:
            self._end_session(sock)

    def send_items(self, items: Sequence[Item]) -> SendReport:
        """Send records in batches, once. Each message waits for the last reply.

        Never resends. The next attempt starts again from the identity check, because a
        read shows whether the record landed.
        """
        self._require_write_enabled()
        if not items:
            raise ValueError("send_items needs at least one item")
        self._replies = []
        size = self._settings.batch_size
        batches = [
            _Batch(tuple(items[i : i + size]), protocol.build_item_batch(items[i : i + size]))
            for i in range(0, len(items), size)
        ]
        if not self._identity_confirmed:
            identity = self.identify()
            if not identity.ok:
                outcomes = tuple(ItemOutcome(i.id, i.plu, ITEM_NOT_SENT) for i in items)
                return SendReport(self._scale.name, identity.status, identity.message, 0, outcomes, ())
        try:
            note = self._run_session(batches)
        except _NoSession as exc:
            return self._report(batches, 0, "", str(exc), False)
        except _OpenRefused as exc:
            return self._report(batches, 0, "", str(exc), True)
        return self._report(batches, 1, note, "", False)

    # ------------------------------------------------------------ results

    def _report(self, batches: Sequence[_Batch], attempts: int, note: str, no_session: str, open_refused: bool) -> SendReport:
        name = self._scale.name
        outcomes: list[ItemOutcome] = []
        for batch in batches:
            said_error = batch.state == "rejected"
            if batch.state == "accepted":
                status = ScaleStatus.ACCEPTED_NOT_VERIFIED.value
            elif batch.state == "rejected":
                # With other records in the batch, nobody knows if the scale kept those.
                status = (ScaleStatus.REJECTED if len(batch.items) == 1 else ScaleStatus.UNKNOWN).value
            elif batch.state == "unknown":
                status = ScaleStatus.UNKNOWN.value
            elif no_session and not open_refused:
                status = ScaleStatus.UNREACHABLE.value
            else:
                status = ITEM_NOT_SENT
            outcomes.extend(ItemOutcome(i.id, i.plu, status, said_error) for i in batch.items)
        states = {o.status for o in outcomes}
        if open_refused:
            overall = ScaleStatus.REJECTED
        else:
            overall = next((s for s in STATUS_SEVERITY if s.value in states), ScaleStatus.UNKNOWN)
        # The flow should never reach this, but unsent records must not read as a success.
        if ITEM_NOT_SENT in states and overall is ScaleStatus.ACCEPTED_NOT_VERIFIED:
            overall = ScaleStatus.UNKNOWN
        if overall is ScaleStatus.ACCEPTED_NOT_VERIFIED:
            message = f"{name}: the scale said Ok to every record."
        elif overall is ScaleStatus.REJECTED and open_refused:
            message = f"{name}: the scale said Error to the opening message. No item was sent."
        elif overall is ScaleStatus.REJECTED:
            message = f"{name}: the scale said Error to a batch. Any records after it were not sent."
        elif overall is ScaleStatus.UNREACHABLE:
            message = f"{name}: no session could be opened, so no item was sent. Last problem: {no_session}."
        else:
            message = f"{name}: the result is unknown. {note}".strip()
        return SendReport(name, overall, message, attempts, tuple(outcomes), tuple(self._replies))


def _stopped_message(name: str, exc: _ReadStopped) -> str:
    text = f"{name}: the read failed ({exc})."
    return f"{text} {FROZEN_NOTE}" if exc.silent else text


def _describe(exc: OSError) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, ConnectionError):
        return "connection lost"
    return exc.__class__.__name__
