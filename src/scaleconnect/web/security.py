"""PIN check, lockout and sessions for the web page. The PIN is never logged or returned."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

SESSION_COOKIE = "sc_session"
CSRF_HEADER = "x-csrf-token"
MIN_PIN_LENGTH = 4
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def listen_host_problem(host: str) -> str | None:
    """None if the host is fine. The page binds to one real IPv4 address of this PC.

    A name, a wildcard in any spelling, an IPv6 address, and the Tailscale range are refused.
    Loopback is fine, for testing on the same PC.
    """
    try:
        address = ipaddress.ip_address(host.strip()) if isinstance(host, str) else None
    except ValueError:
        address = None
    if address is None or address.version != 4:
        return (
            "The web host in the config must be one IPv4 address of this PC, written as four numbers "
            "such as 127.0.0.1. Names and IPv6 addresses are not allowed."
        )
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        return "The web host in the config must be a real address of this PC. A wildcard address is not allowed."
    if address in _TAILSCALE:
        return "The web host in the config is in the Tailscale range. Use the store network address of this PC."
    return None


def allowed_host_headers(host: str, port: int) -> set[str]:
    """The Host header values the page answers to: its own configured address and port."""
    names = {host.strip().lower()}
    if host.strip() in ("127.0.0.1", "::1", "localhost"):
        names |= {"127.0.0.1", "localhost"}
    out = set()
    for name in names:
        shown = f"[{name}]" if ":" in name else name
        out.add(f"{shown}:{port}")
        if port == 80:
            out.add(shown)
    return out


def config_mode_problem(path: Path | str, mode: int | None = None) -> str | None:
    """On Linux, the config holds the PIN, so group and others must not read it."""
    if mode is None:
        if os.name != "posix":
            return None
        try:
            mode = os.stat(path).st_mode
        except OSError:
            return None
    if mode & 0o077:
        return f"The config file can be read by other users. Run: chmod 600 {path}"
    return None


def same_origin(origin: str | None, host_header: str | None) -> bool:
    """True if the Origin header names the same host and port the request went to."""
    if origin is None:
        return True  # not a browser cross site request. The CSRF token still applies
    if not host_header:
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme != "http" or not parts.netloc:  # this page is plain HTTP
        return False
    return parts.netloc.lower() == host_header.strip().lower()

class Lockout:
    """Counts wrong tries. After max_failures in a row, that key is locked for lock_seconds."""

    def __init__(self, max_failures: int, lock_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, list[float]] = {}  # key -> [count, locked_until, last_failure]

    def seconds_left(self, key: str) -> int:
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return 0
            left = entry[1] - self._clock()
            return int(left) + 1 if left > 0 else 0

    def fail(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            entry = self._state.setdefault(key, [0, 0.0, now])
            if now - entry[2] > 600:  # old failures are forgotten
                entry[0] = 0
            entry[0] += 1
            entry[2] = now
            if entry[0] >= self.max_failures:
                entry[1] = now + self.lock_seconds
                entry[0] = 0
            if len(self._state) > 500:
                for stale in [k for k, v in self._state.items() if now - v[2] > 600 and v[1] < now]:
                    del self._state[stale]

    def clear(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


class Auth:
    """One shared PIN. Sessions live in memory, so a restart asks for the PIN again."""

    def __init__(
        self,
        pin: str,
        *,
        max_failures: int = 5,
        lock_seconds: float = 60.0,
        session_seconds: float = 12 * 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(pin, str) or not pin:
            raise ValueError("A PIN is required.")
        # Both sides are hashed first, so the compare takes the same time for any length.
        self._pin_digest = hashlib.sha256(pin.encode("utf-8")).digest()
        self._clock = clock
        self._session_seconds = session_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}  # token -> (csrf token, expires)
        self._per_client = Lockout(max_failures, lock_seconds, clock)
        self._overall = Lockout(max_failures * 4, lock_seconds, clock)

    @property
    def session_seconds(self) -> int:
        return int(self._session_seconds)

    def locked_for(self, client: str) -> int:
        return max(self._per_client.seconds_left(client), self._overall.seconds_left("*"))

    def pin_matches(self, given: str) -> bool:
        digest = hashlib.sha256(given.encode("utf-8", "replace")).digest()
        return hmac.compare_digest(digest, self._pin_digest)

    def login(self, client: str, given: str) -> tuple[str, str] | int:
        """(session token, csrf token) on success. Otherwise the seconds to wait, or 0 for a wrong PIN."""
        left = self.locked_for(client)
        if left:
            return left
        if not self.pin_matches(given):
            self._per_client.fail(client)
            self._overall.fail("*")
            return self.locked_for(client) or 0
        self._per_client.clear(client)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            for old in [t for t, (_, exp) in self._sessions.items() if exp < now]:
                del self._sessions[old]
            if len(self._sessions) >= 200:
                oldest = min(self._sessions, key=lambda t: self._sessions[t][1])
                del self._sessions[oldest]
            self._sessions[token] = (csrf, now + self._session_seconds)
        return token, csrf

    def csrf_for(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            if entry[1] < self._clock():
                del self._sessions[token]
                return None
            return entry[0]

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def csrf_ok(self, token: str | None, given: str | None) -> bool:
        expected = self.csrf_for(token)
        if expected is None or not given:
            return False
        return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8", "replace"))
