# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""A bare socket client for testing the fake itself. Not the library."""

from __future__ import annotations

import socket
import time

from .wire import frame


def gp_batch(records: list[tuple[str, ...]]) -> bytes:
    """Frame a GP batch from 20 field records. Test helper only."""
    body = b"".join("\t".join(r).encode("latin-1") + b"\t\n" for r in records)
    # The last record's TAB LF is the framing TAB LF.
    text = f"GP{len(records):02d}".encode() + body[:-2]
    return frame(text)


class RawClient:
    def __init__(self, address: tuple[str, int], timeout: float = 2.0):
        self.sock = socket.create_connection(address, timeout=timeout)
        self.sock.settimeout(timeout)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv_reply(self, timeout: float = 2.0) -> bytes | None:
        """Read until the connection goes quiet. None means closed with no
        bytes. b'' never happens: no data and still open raises TimeoutError."""
        self.sock.settimeout(timeout)
        out = b""
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return out or None
                out += chunk
                # A full reply ends in CR. GP replies have two CRs.
                need = 2 if out[1:2] == b"\r" else 1
                if out.endswith(b"\r") and out.count(b"\r") >= need:
                    return out
        except (ConnectionResetError, ConnectionAbortedError):
            return out or None

    def recv_line(self, timeout: float = 2.0) -> bytes:
        """Read up to and including LF. For the clock messages."""
        self.sock.settimeout(timeout)
        out = b""
        while not out.endswith(b"\n"):
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            out += chunk
        return out

    def closed_by_peer(self, timeout: float = 2.0) -> bool:
        self.sock.settimeout(timeout)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.sock.recv(4096) == b"":
                    return True
            except (ConnectionResetError, ConnectionAbortedError):
                return True
            except socket.timeout:
                return False
        return False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
