# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""One session with one scale. The only module that opens a socket to a scale.

Standard library only, plus this package's own standard library modules.

Public operations are named: identify, read_items and send_items. There is no way to
send arbitrary text. Two paths reach the socket, each with its own guard:

- The write path needs write_enabled. It sends only RI, bX01XX, GP batches and GPF0.
- The read path needs write_enabled or read_enabled. It sends only CX, LP and Ok.

"Not write enabled" does not mean "safe to read". A scale with neither flag is never
contacted. A write needs the serial to match first, and send_items checks that itself,
so no caller can skip it. Nothing here resends a record after sending has begun.

A read whose blocks came out of order can be read once more, if read_repeat_enabled is on.
See read_items. Any other bad answer never is, and a read the scale went silent on never
is. Writes never are.

Read blocks never go in the reply list that reaches the change log. Read problems go in
read_log, as ids and counts only.

A delete has no message of its own. It is a send_items call with a placeholder record,
because overwriting an id removes the old PLU. No E message exists here, and none can be
framed.
"""

from __future__ import annotations

import errno
import socket
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from . import protocol, read_log
from .config import MAX_CONNECT_WAIT_SECONDS, ScaleConfig, Settings
from .errors import NotReadEnabled, NotWriteEnabled
from .models import ITEM_NOT_SENT, MAX_ITEM_ID, STATUS_SEVERITY, Item, ScaleStatus

READ_AVAILABLE = True
DELETE_AVAILABLE = True

# A read has frozen real scales: no reply, then gone from the network until rebooted. See
# docs/PROTOCOL.md section 3. Any read or identity that gets no reply says so.
FROZEN_NOTE = "The scale may have frozen, and may need a reboot."
_NOT_CLOSED = "The scale did not close the connection after we hung up."

_READ_SIZE = 4096
_CLOSE_WAIT = 2.0
# After a bad answer the close decides whether the scale may be frozen, so it gets longer. A scale that
# has just sent a block again may be slow on the WiFi, and a false alarm costs a person a trip to look.
_BAD_ANSWER_CLOSE_WAIT = 8.0
_LARGEST_ID_SHOWN = 99999  # a broken block can hold an id of any length
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
    may_be_frozen: bool = False  # it gave no reply. Do not read it again until a person has checked it


@dataclass(frozen=True)
class ReadResult:
    ok: bool
    status: ScaleStatus | None
    message: str
    items: dict[int, Item]
    complete: bool = False  # reached the end of the table, not an early stop
    may_be_frozen: bool = False  # no reply, or it would not close after bad blocks. Do not read it again until a person has checked it
    repeated: bool = False  # the first read's blocks came out of order, so a second read was tried. A refused connect can mean no second LP went out
    first_problem: str | None = None  # the order problem that caused that, in library words and ids. None if no second read was tried


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
    def __init__(
        self,
        message: str,
        *,
        before_first_block: bool = False,
        no_reply: bool = False,
        silent: bool = False,
        order_problem: bool = False,
    ) -> None:
        super().__init__(message)
        self.before_first_block = before_first_block
        self.no_reply = no_reply
        self.silent = silent  # the scale stopped answering, as opposed to answering badly
        self.order_problem = order_problem  # blocks out of order after a good block, the one case a repeat can follow


@dataclass
class _ReadProgress:
    """How far one read got, for the read problem log."""

    last_id: int = 0
    blocks: int = 0
    lp_sent: int = 0  # counts a dropped LP that was tried again, so a repeat cannot come on top of those


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

    def _end_session(self, sock: socket.socket, *, after_bad_answer: bool = False) -> bool:
        """Hang up, and give the scale a moment to finish before any next session.

        After an early stop the scale sends the block again and LP00End, then closes. A new
        session that starts before then seems to queue behind the old one. What it sends
        here is thrown away unread, and never kept.

        Returns whether the scale closed its side inside the wait. Bytes alone do not count,
        only the close or a reset that ends them. In a real freeze our hang up was never
        answered. Only a read that the scale answered badly uses the answer, and only then
        is the wait longer. The reply timeout caps both waits.
        """
        longest = _BAD_ANSWER_CLOSE_WAIT if after_bad_answer else _CLOSE_WAIT
        try:
            sock.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + min(longest, self._settings.reply_timeout_seconds)
            while self._read_chunk(sock, deadline):
                pass
            return True
        except ConnectionResetError:
            return True
        except OSError as exc:
            return exc.errno == errno.ENOTCONN  # on Linux, a reset that came before we hung up
        finally:
            sock.close()

    def _connect_with_retry(self, attempt: Callable[[], Any], *, refused_only: bool = False) -> Any:
        """Tries up to connect_attempts times, and the wait grows each time.

        It retries a connect that fails, at once or by timing out, and a read message dropped
        with no reply. It never retries once a scale has said anything, or has stalled. The
        one repeat of a read is decided in read_items, not here.

        With refused_only, for that repeat, only a refused connect is tried again, since no
        message went out and the scale answered. So the repeat's LP goes out once.
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
                if refused_only and not isinstance(exc, ConnectionRefusedError):
                    break
            except _NoReplyYet as exc:
                last = str(exc)
                connected = True
                if refused_only:
                    break
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
        started = time.monotonic()
        check = self._check_identity(retry=retry)
        # A read back tries this many times while the scale settles, which is normal, and logs its own end.
        if retry and not check.ok:
            read_log.record(
                self._settings.read_problem_log,
                scale=self._scale.name,
                read="identity",
                what=check.message.removeprefix(f"{self._scale.name}: ").replace(FROZEN_NOTE, "").strip(),
                last_id=0,
                blocks=0,
                seconds=time.monotonic() - started,
                silent=check.may_be_frozen,
                repeat="no",
            )
        return check

    def _check_identity(self, *, retry: bool) -> IdentityCheck:
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
                return IdentityCheck(False, ScaleStatus.READ_FAILED, f"{unconfirmed} ({exc}). Nothing was written. {FROZEN_NOTE}", may_be_frozen=True)
            return IdentityCheck(False, ScaleStatus.UNREACHABLE, f"{name}: could not connect ({exc}). Nothing was sent. {FROZEN_NOTE}", may_be_frozen=True)
        except OSError as exc:
            return IdentityCheck(False, ScaleStatus.UNREACHABLE, f"{name}: could not connect ({_describe(exc)}). Nothing was sent. {FROZEN_NOTE}", may_be_frozen=True)
        except (_NoReplyYet, _ReplyLost) as exc:
            detail = exc.args[0] if exc.args else exc.__class__.__name__
            return IdentityCheck(False, ScaleStatus.READ_FAILED, f"{unconfirmed} ({detail}). Nothing was written. {FROZEN_NOTE}", may_be_frozen=True)
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

        With read_repeat_enabled on, one case is read once more from the top, after
        read_repeat_wait_seconds. All of these must hold: the block failed the order check, a
        good block came before it, its ids are ones a scale can hold, it came on this call's
        first LP, and the scale closed its side when we hung up. At most once per call, and
        that second LP goes out once. Every other bad answer is a plain failure, because some
        came right before a real freeze. See PROTOCOL.md section 3.

        If the scale answered badly in any way and did not close, it may be frozen, so the
        result says so, with the setting on or off. After a bad answer the wait for that close
        is up to 8 seconds, not 2. A scale that went silent is never read again here.
        """
        self._require_read_allowed()
        if patient:
            return self._read_patiently(up_to_id)
        started = time.monotonic()
        if not self._identity_confirmed:
            identity = self.identify()  # a failure here has gone in the log already
            if not identity.ok:
                return ReadResult(False, identity.status, identity.message, {}, may_be_frozen=identity.may_be_frozen)
        first, progress, problem = self._read_once(up_to_id)
        if first.ok:
            return first
        if problem is None:
            self._log_read(first, progress, started, "no")
            return first
        self._log_read(first, progress, started, "will_try")
        self._sleep(self._settings.read_repeat_wait_seconds)
        second, progress, _ = self._read_once(up_to_id, repeat=True)
        self._log_read(second, progress, started, "worked" if second.ok else "failed")
        if second.ok:
            return replace(second, repeated=True, first_problem=problem)
        return replace(second, message=_both_failed(self._scale.name, problem, second.message), repeated=True, first_problem=problem)

    def _read_once(self, up_to_id: int | None, *, repeat: bool = False) -> tuple[ReadResult, _ReadProgress, str | None]:
        """One read from the top. The last value is what went wrong, if a repeat is allowed."""
        progress = _ReadProgress()
        closed = False

        def attempt() -> tuple[dict[int, Item], bool]:
            nonlocal closed
            sock = self._connect_read()
            bad_answer = False
            try:
                return self._read_table(sock, up_to_id, progress)
            except _ReadStopped as exc:
                bad_answer = not exc.silent
                if exc.no_reply:
                    raise _NoReplyYet(str(exc)) from exc
                raise
            finally:
                closed = self._end_session(sock, after_bad_answer=bad_answer)

        name = self._scale.name
        try:
            items, complete = self._connect_with_retry(attempt, refused_only=repeat)
        except _CouldNotConnect as exc:
            if exc.connected:
                return ReadResult(False, ScaleStatus.READ_FAILED, f"{name}: the read failed ({exc}). {FROZEN_NOTE}", {}, may_be_frozen=True), progress, None
            return ReadResult(False, ScaleStatus.UNREACHABLE, f"{name}: could not connect to read ({exc}). {FROZEN_NOTE}", {}, may_be_frozen=True), progress, None
        except _ReadStopped as exc:
            result, problem = self._stopped_result(exc, closed, progress)
            return result, progress, problem
        return ReadResult(True, None, f"{name}: read {len(items)} records.", items, complete), progress, None

    def _stopped_result(self, exc: _ReadStopped, closed: bool, progress: _ReadProgress) -> tuple[ReadResult, str | None]:
        """The result of a read that stopped, and the reason to give if a repeat may follow."""
        name = self._scale.name
        if not exc.silent and not closed:
            # Any bad answer, then no close. Blank records came right before a real freeze.
            message = f"{name}: the read failed ({exc}). {_NOT_CLOSED} {FROZEN_NOTE}"
            return ReadResult(False, ScaleStatus.READ_FAILED, message, {}, may_be_frozen=True), None
        result = ReadResult(False, ScaleStatus.READ_FAILED, _stopped_message(name, exc), {}, may_be_frozen=exc.silent)
        may_repeat = exc.order_problem and progress.lp_sent == 1 and self._settings.read_repeat_enabled is True
        return result, str(exc) if may_repeat else None

    def _read_patiently(self, up_to_id: int | None) -> ReadResult:
        """The read back. The same one repeat as read_items, inside the same window.

        The window is there to wait out the busy gap and to limit how long a caller waits, so
        the repeat gets no time of its own. It starts only if its wait ends inside the window.
        Otherwise the first problem stands. As with any read, one under way is not cut off.
        While the repeat runs, a refused connect is still tried until the window ends, because
        a connect sends the table nothing and the scale answered it. A connect that gets no
        answer, and any silence on the read itself, stop at once.
        """
        name = self._scale.name
        started = time.monotonic()
        deadline = started + self._settings.read_back_window_seconds
        repeat_wait = self._settings.read_repeat_wait_seconds
        last = "no attempt was made"
        silent = True
        progress = _ReadProgress()
        first_problem: str | None = None

        def stop(result: ReadResult) -> ReadResult:
            if first_problem is None:
                self._log_read(result, progress, started, "no", patient=True)
                return result
            self._log_read(result, progress, started, "failed", patient=True)
            return replace(result, message=_both_failed(name, first_problem, result.message), repeated=True, first_problem=first_problem)

        def out_of_time() -> ReadResult:
            text = f"{name}: could not read back in time ({last})."
            if silent and FROZEN_NOTE not in text:
                text = f"{text} {FROZEN_NOTE}"
            return stop(ReadResult(False, ScaleStatus.READ_FAILED, text, {}, may_be_frozen=silent))

        # The window is real time, whichever way the scale says no: at once, or not at all.
        while True:
            if not self._identity_confirmed:
                identity = self._identify_once(retry=False)
                if identity.status is ScaleStatus.WRONG_SCALE:
                    return stop(ReadResult(False, identity.status, identity.message, {}))
                if not identity.ok:
                    last = identity.message
                    silent = identity.may_be_frozen
                    if time.monotonic() >= deadline:
                        return out_of_time()
                    self._sleep(self._settings.read_back_retry_seconds)
                    continue
            progress = _ReadProgress(lp_sent=progress.lp_sent)
            answered_badly: _ReadStopped | None = None
            closed = False
            try:
                sock = self._connect_read()
            except OSError as exc:
                last = _describe(exc)
                silent = True
                if first_problem is not None and not isinstance(exc, ConnectionRefusedError):
                    return stop(ReadResult(False, ScaleStatus.READ_FAILED, f"{name}: could not connect to read back ({last}). {FROZEN_NOTE}", {}, may_be_frozen=True))
            else:
                try:
                    items, complete = self._read_table(sock, up_to_id, progress)
                    result = ReadResult(True, None, f"{name}: read {len(items)} records.", items, complete)
                    if first_problem is None:
                        return result
                    self._log_read(result, progress, started, "worked", patient=True)
                    return replace(result, repeated=True, first_problem=first_problem)
                except _ReadStopped as exc:
                    last = str(exc)
                    silent = exc.silent
                    if not exc.silent:
                        answered_badly = exc
                    # Once blocks have come, silence is a real stall. Stop. The same goes for a
                    # repeat: the scale was past its busy gap, so this is no busy gap either.
                    elif not exc.before_first_block or first_problem is not None:
                        return stop(ReadResult(False, ScaleStatus.READ_FAILED, _stopped_message(name, exc), {}, may_be_frozen=True))
                finally:
                    closed = self._end_session(sock, after_bad_answer=answered_badly is not None)
            if answered_badly is not None:
                failed, problem = self._stopped_result(answered_badly, closed, progress)
                if first_problem is not None:
                    return stop(failed)
                if problem is None:
                    self._log_read(failed, progress, started, "no", patient=True)
                    return failed
                if time.monotonic() + repeat_wait >= deadline:
                    self._log_read(failed, progress, started, "no_time", patient=True)
                    return failed
                first_problem = problem
                self._log_read(failed, progress, started, "will_try", patient=True)
                self._sleep(repeat_wait)
                continue
            if time.monotonic() >= deadline:
                return out_of_time()
            self._sleep(self._settings.read_back_retry_seconds)

    def _log_read(self, result: ReadResult, progress: _ReadProgress, started: float, repeat: str, *, patient: bool = False) -> None:
        """One line in the read problem log. It never raises. Only the library's words go in."""
        name = self._scale.name
        what = "the repeat read worked" if result.ok else result.message.removeprefix(f"{name}: ").replace(FROZEN_NOTE, "").strip()
        read_log.record(
            self._settings.read_problem_log,
            scale=name,
            read="patient" if patient else "plain",
            what=what,
            last_id=progress.last_id,
            blocks=progress.blocks,
            seconds=time.monotonic() - started,
            silent=result.may_be_frozen,
            repeat=repeat,
        )

    def _read_table(self, sock: socket.socket, up_to_id: int | None, progress: _ReadProgress) -> tuple[dict[int, Item], bool]:
        items: dict[int, Item] = {}
        buffer = b""
        last_id = 0
        previous: tuple[Item, ...] | None = None
        first = True
        while True:
            try:
                if first:
                    progress.lp_sent += 1
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
            progress.blocks += 1
            ids = [record.id for record in block.records]
            if ids[0] <= last_id or ids != sorted(set(ids)):
                # We sent only Ok, so this is not a step forward. Do not loop on it.
                order_problem = previous is not None and min(ids) >= 1 and max(ids) <= MAX_ITEM_ID
                raise _ReadStopped(_out_of_order(last_id, ids, previous, block.records), order_problem=order_problem)
            for record in block.records:
                items[record.id] = record
            previous = block.records
            last_id = ids[-1]
            progress.last_id = last_id
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
            except ValueError as exc:  # an id too long to be a number. Its text is not repeated
                raise _ReadStopped("A record in the read block is not in the expected form.") from exc
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


def _both_failed(name: str, first_problem: str, second_message: str) -> str:
    return f"{name}: the read failed ({first_problem}). It was read once more, and that failed too. {second_message}"


def _id_word(value: int) -> str:
    return str(value) if value <= _LARGEST_ID_SHOWN else f"over {_LARGEST_ID_SHOWN}"


def _out_of_order(last_id: int, ids: Sequence[int], previous: tuple[Item, ...] | None, block: tuple[Item, ...]) -> str:
    """Parsed ids only, and never a word of what the records say."""
    if previous is None:
        compared = "with no block before it"
    elif block == previous:
        compared = "exactly the same as the block before"
    else:
        compared = "different from the block before"
    inside = "" if list(ids) == sorted(set(ids)) else ", with ids inside it out of order or repeated"
    return (
        f"the scale sent blocks out of order: last id seen {_id_word(last_id)}, "
        f"then a block from id {_id_word(ids[0])} to id {_id_word(ids[-1])}, {compared}{inside}"
    )


def _describe(exc: OSError) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, ConnectionError):
        return "connection lost"
    return exc.__class__.__name__
