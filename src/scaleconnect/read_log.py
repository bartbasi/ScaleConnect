# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""A small log of read problems, so a failed read in a preview or compare is not lost.

The web page, MCP server and command line are separate processes and share this file. Each
line goes out as one short append. The file holds only the library's own words, scale names,
ids and counts, never what a record says.

Nothing here may break or slow a read, so every failure to write is ignored. Trimming swaps
the file, so a line another process appends in that moment can be lost. On Windows, where
this is not the home of the app, an append is a seek and then a write, so two processes
writing in the same instant can lose a line too. Both are rare, since problems are rare and
only one process talks to the scales at a time. A lock between processes would be one more
thing that could hold up a read.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

MAX_BYTES = 1_000_000
PATH_VARIABLE = "SCALECONNECT_READ_LOG"
_TEXT_LIMIT = 400
_LARGEST_ID = 99999  # a broken block can hold an id of any length
_NOT_PLAIN = re.compile(r"[^ -~]")
_THREADS = threading.Lock()


def default_path() -> Path:
    """The state folder deploy/*.sh uses."""
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "scaleconnect" / "read-problems.log"


def path_for(configured: Path | None) -> Path:
    """Path order: the read_problem_log setting, the SCALECONNECT_READ_LOG variable, the default."""
    if configured:
        return Path(configured)
    chosen = os.environ.get(PATH_VARIABLE)
    return Path(chosen).expanduser() if chosen else default_path()


def _plain(text: object) -> str:
    return _NOT_PLAIN.sub("?", str(text))[:_TEXT_LIMIT]


def format_line(*, scale: str, read: str, what: str, last_id: int, blocks: int, seconds: float, silent: bool, repeat: str) -> str:
    when = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        f"{when} scale={_plain(scale).replace(' ', '_')} read={read} last_id={min(last_id, _LARGEST_ID)} blocks={blocks} "
        f"seconds={seconds:.1f} silent={'yes' if silent else 'no'} repeat={repeat} what={_plain(what)}\n"
    )


def _trim(path: Path) -> None:
    """Keep the newer half, starting at a whole line."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - MAX_BYTES // 2))
        kept = handle.read()
    kept = kept[kept.find(b"\n") + 1 :]
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(kept)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # O_NONBLOCK keeps a stray pipe at this path from holding up a read. It changes nothing for a file.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    for extra in ("O_BINARY", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, extra, 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        os.write(descriptor, line.encode("ascii", "replace"))
        size = os.fstat(descriptor).st_size
    finally:
        os.close(descriptor)
    if size > MAX_BYTES:
        _trim(path)


def record(configured: Path | None, **fields: object) -> None:
    """Add one line. Never raises."""
    try:
        line = format_line(**fields)
        with _THREADS:
            _append(path_for(configured), line)
    except Exception:
        pass
