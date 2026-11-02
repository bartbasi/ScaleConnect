"""ScaleConnect item list, kept in SQLite. Interface: docs/LIBRARY.md, Catalog section.

This is our own copy of the products, because we may not be able to read a
scale. It holds:

* items: the 20 record fields exactly as text, plus a deleted flag.
* what each scale is believed to hold (scale_state).
* ids we must not hand out: orphan ids and reserved ids.
* the change log: append only, enforced by database triggers.
* saved previews.

Rules this module enforces: no duplicate PLU among live items, ids from 1 to
500, and every field stored as text exactly as given, so field 6 (empty or one
space) is never trimmed. It does not check prices, PLU format or description
length. `service.py` does that before anything reaches a scale.

The catalog never contacts a scale. It is safe to use from several threads and
several processes: one connection per thread, WAL mode and a busy timeout.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .config import catalog_path_for
from .errors import ApplyBusy, CatalogNotSeeded, ScaleConnectError
from .models import Item

MIN_ID = 1
MAX_ID = 500
SCHEMA_VERSION = 4
_LOCK_TRIES = 4
_LOCK_GAP_SECONDS = 0.03
BELIEFS = ("present", "absent", "unknown")
_SUCCESS = ("verified", "accepted_not_verified")

_COLS = (
    "id", "code", "plu", "description", "price", "field6",
    "f7", "f8", "f9", "f10", "f11", "f12",
    "timestamp",
    "f14", "f15", "f16", "f17", "f18", "f19", "f20",
)


# ---------------------------------------------------------------- errors

class CatalogError(ScaleConnectError):
    code = "catalog_error"


class DuplicatePluError(CatalogError):
    code = "duplicate_plu"


class IdInUseError(CatalogError):
    code = "id_in_use"


class InvalidIdError(CatalogError):
    code = "invalid_id"


class NoFreeIdError(CatalogError):
    code = "no_free_id"


class InvalidItemError(CatalogError):
    code = "invalid_field"


# ---------------------------------------------------------------- the lock


@contextmanager
def apply_lock(path: Path) -> Iterator[None]:
    """One session with the scales at a time, for this state folder. Replacing the catalog takes
    it too. The system frees it if the process dies. It does not reach another folder or PC."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        # Brief tries, because apply_running holds this for an instant just to look.
        for tries_left in range(_LOCK_TRIES - 1, -1, -1):
            try:
                _lock_file(handle)
                break
            except OSError:
                if not tries_left:
                    raise ApplyBusy("Another change is being applied. Try again when it finishes.") from None
                time.sleep(_LOCK_GAP_SECONDS)
        try:
            yield
        finally:
            _unlock_file(handle)
    finally:
        handle.close()


if os.name == "nt":
    import msvcrt

    def _lock_file(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- helpers

def default_db_path() -> Path:
    """The one path rule, shared with the service. See config.catalog_path_for."""
    return catalog_path_for()


def db_path_from_config(cfg: Any) -> Path:
    """Catalog path from a config object or dict: `catalog_path`, or a
    `catalog` table with `path`. Falls back to `default_db_path()`."""
    value = None
    if isinstance(cfg, Mapping):
        value = cfg.get("catalog_path")
        if value is None and isinstance(cfg.get("catalog"), Mapping):
            value = cfg["catalog"].get("path")
    elif cfg is not None:
        value = getattr(cfg, "catalog_path", None)
        if value is None:
            settings = getattr(cfg, "settings", None)
            value = getattr(settings, "catalog_path", None)
    return Path(str(value)).expanduser() if value else default_db_path()


def _check_id(item_id: Any) -> int:
    if isinstance(item_id, bool) or not isinstance(item_id, int):
        raise InvalidIdError(f"id must be a whole number, got {item_id!r}")
    if not (MIN_ID <= item_id <= MAX_ID):
        raise InvalidIdError(f"id {item_id} is outside {MIN_ID} to {MAX_ID}")
    return item_id


def _item_values(item: Item) -> list[str]:
    """The 20 text values of an item, checked so they cannot break the framing."""
    if not isinstance(item, Item):
        raise InvalidItemError("expected an Item")
    _check_id(item.id)
    values = item.to_fields()
    for name, v in zip(_COLS, values):
        if not isinstance(v, str):
            raise InvalidItemError(f"{name} must be text")
        if "\t" in v or "\r" in v or "\n" in v:
            raise InvalidItemError(f"{name} contains a TAB, CR or LF")
    if values[2] == "":
        raise InvalidItemError("plu is empty")
    return values


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item.from_fields([str(row["id"])] + [row[c] for c in _COLS[1:]])


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY CHECK (id BETWEEN {MIN_ID} AND {MAX_ID}),
    code TEXT NOT NULL, plu TEXT NOT NULL, description TEXT NOT NULL,
    price TEXT NOT NULL, field6 TEXT NOT NULL,
    f7 TEXT NOT NULL, f8 TEXT NOT NULL, f9 TEXT NOT NULL,
    f10 TEXT NOT NULL, f11 TEXT NOT NULL, f12 TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    f14 TEXT NOT NULL, f15 TEXT NOT NULL, f16 TEXT NOT NULL, f17 TEXT NOT NULL,
    f18 TEXT NOT NULL, f19 TEXT NOT NULL, f20 TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    deleted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS items_live_plu ON items (plu) WHERE deleted = 0;

CREATE TABLE IF NOT EXISTS item_history (
    h_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    op TEXT NOT NULL,
    before TEXT,
    after TEXT
);
CREATE TRIGGER IF NOT EXISTS item_history_no_update BEFORE UPDATE ON item_history
BEGIN SELECT RAISE(ABORT, 'item_history is append only'); END;
CREATE TRIGGER IF NOT EXISTS item_history_no_delete BEFORE DELETE ON item_history
BEGIN SELECT RAISE(ABORT, 'item_history is append only'); END;

CREATE TABLE IF NOT EXISTS scale_state (
    scale TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    change_id TEXT NOT NULL,
    at TEXT NOT NULL,
    belief TEXT NOT NULL CHECK (belief IN ('present', 'absent', 'unknown')),
    record TEXT,
    PRIMARY KEY (scale, item_id)
);

CREATE TABLE IF NOT EXISTS scale_read (
    scale TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    record TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (scale, item_id)
);

CREATE TABLE IF NOT EXISTS plu_moves (
    scale TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    old_plu TEXT NOT NULL,
    record TEXT NOT NULL,
    noted_at TEXT NOT NULL,
    trying INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scale, item_id, old_plu)
);

CREATE TABLE IF NOT EXISTS orphans (
    scale TEXT NOT NULL,
    item_id INTEGER NOT NULL CHECK (item_id BETWEEN {MIN_ID} AND {MAX_ID}),
    note TEXT NOT NULL DEFAULT '',
    noted_at TEXT NOT NULL,
    PRIMARY KEY (scale, item_id)
);

CREATE TABLE IF NOT EXISTS reserved_ids (
    item_id INTEGER PRIMARY KEY CHECK (item_id BETWEEN {MIN_ID} AND {MAX_ID}),
    reason TEXT NOT NULL DEFAULT '',
    reserved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS previews (
    preview_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_by TEXT
);

CREATE TABLE IF NOT EXISTS change_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL,
    at TEXT NOT NULL,
    scale TEXT
);
CREATE INDEX IF NOT EXISTS change_log_change ON change_log (change_id);
CREATE INDEX IF NOT EXISTS change_log_scale ON change_log (event, scale);
CREATE TRIGGER IF NOT EXISTS change_log_no_update BEFORE UPDATE ON change_log
BEGIN SELECT RAISE(ABORT, 'change_log is append only'); END;
CREATE TRIGGER IF NOT EXISTS change_log_no_delete BEFORE DELETE ON change_log
BEGIN SELECT RAISE(ABORT, 'change_log is append only'); END;
"""

_SELECT = "SELECT " + ", ".join(_COLS) + ", deleted FROM items"


class Catalog:
    """One SQLite file holding items, scale beliefs, previews and the change log."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._conns_lock = threading.Lock()
        self._closed = False
        db = self._conn()
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.close()
            raise CatalogError(
                f"catalog file is version {version}, this program knows {SCHEMA_VERSION}")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(_SCHEMA)
        if "trying" not in {r["name"] for r in db.execute("PRAGMA table_info(plu_moves)")}:
            try:
                db.execute("ALTER TABLE plu_moves ADD COLUMN trying INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # another process opening this file got there first
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('epoch', ?)", (uuid.uuid4().hex[:8],))
        db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('items_version', '0')")
        db.execute("COMMIT")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @classmethod
    def open_existing(cls, path: str | os.PathLike[str]) -> "Catalog":
        """Open a catalog that seed.py made. Never creates one."""
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise CatalogNotSeeded(f"There is no catalog at {resolved}. Run the seed step first.")
        return cls(resolved)

    @classmethod
    def from_config(cls, cfg: Any) -> "Catalog":
        return cls(db_path_from_config(cfg))

    # ---------------------------------------------------------- plumbing

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise CatalogError("the catalog is closed")
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False,
                                 isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout = 10000")
            self._local.db = db
            with self._conns_lock:
                # A connection belongs to its thread. One whose thread has ended is never used again.
                ended = [c for t, c in self._conns if not t.is_alive()]
                self._conns = [(t, c) for t, c in self._conns if t.is_alive()]
                self._conns.append((threading.current_thread(), db))
            for old in ended:
                try:
                    old.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
        return db

    def close(self) -> None:
        self._closed = True
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for _, c in conns:
            try:
                c.close()
            except sqlite3.Error:  # pragma: no cover
                pass
        self._local = threading.local()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self._conn()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    @staticmethod
    def _bump(db: sqlite3.Connection) -> None:
        db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'items_version'")

    @staticmethod
    def _history(db: sqlite3.Connection, item_id: int, op: str,
                 before: list[str] | None, after: list[str] | None) -> None:
        db.execute("INSERT INTO item_history (at, item_id, op, before, after) VALUES (?,?,?,?,?)",
                   (_now(), item_id, op,
                    None if before is None else json.dumps(before, ensure_ascii=False),
                    None if after is None else json.dumps(after, ensure_ascii=False)))

    # ------------------------------------------------------------ items

    def is_seeded(self) -> bool:
        row = self._conn().execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
        return row is not None

    def mark_seeded(self, source: str = "") -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', ?)", (source or _now(),))

    def epoch(self) -> str:
        """Differs for each catalog file, so a backup marker can tell if the file was replaced."""
        return self._conn().execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()[0]

    def version(self) -> str:
        """Changes whenever any item changes. Not for scale state, previews or log writes."""
        db = self._conn()
        rows = dict(db.execute("SELECT key, value FROM meta").fetchall())
        return f"{rows['epoch']}-{rows['items_version']}"

    def list_items(self, *, include_deleted: bool = False) -> list[Item]:
        sql = _SELECT + ("" if include_deleted else " WHERE deleted = 0") + " ORDER BY id"
        return [_row_to_item(r) for r in self._conn().execute(sql)]

    def count(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM items WHERE deleted = 0").fetchone()[0])

    def get_by_id(self, item_id: int, *, include_deleted: bool = False) -> Item | None:
        sql = _SELECT + " WHERE id = ?" + ("" if include_deleted else " AND deleted = 0")
        row = self._conn().execute(sql, (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def is_deleted(self, item_id: int) -> bool:
        row = self._conn().execute("SELECT deleted FROM items WHERE id = ?", (item_id,)).fetchone()
        return bool(row and row["deleted"])

    def get_by_plu(self, plu: str) -> Item | None:
        row = self._conn().execute(_SELECT + " WHERE plu = ? AND deleted = 0", (plu,)).fetchone()
        return _row_to_item(row) if row else None

    def deleted_item_still_held(self, *, plu: str | None = None, item_id: int | None = None) -> Item | None:
        """A deleted item that some scale may still hold, so a delete can be tried again there."""
        column, value = ("plu", plu) if plu is not None else ("id", item_id)
        row = self._conn().execute(
            _SELECT + f" WHERE {column} = ? AND deleted = 1 AND id IN "
            "(SELECT item_id FROM scale_state WHERE belief IN ('present', 'unknown')) "
            "ORDER BY deleted_at DESC, id DESC LIMIT 1", (value,)).fetchone()
        return _row_to_item(row) if row else None

    def deleted_still_held(self) -> list[dict]:
        """Each deleted item and scale that may still hold it: {"plu", "item_id", "scale"}. Such a PLU is not free."""
        return [dict(r) for r in self._conn().execute(
            "SELECT i.plu AS plu, i.id AS item_id, s.scale AS scale FROM items i JOIN scale_state s ON s.item_id = i.id "
            "WHERE i.deleted = 1 AND s.belief IN ('present', 'unknown') ORDER BY i.deleted_at, i.id, s.scale")]

    def search(self, query: str, limit: int = 50) -> list[Item]:
        """Match the whole or start of a PLU, or any part of a description, ignoring case.

        An exact PLU comes first, then PLUs that start with the query, then
        description matches by name. An empty query lists everything.
        """
        q = query.strip()
        items = self.list_items()
        if q:
            cf = q.casefold()
            ranked: list[tuple[int, str, int, Item]] = []
            for it in items:
                if it.plu == q:
                    rank = 0
                elif it.plu.startswith(q):
                    rank = 1
                elif cf in it.description.casefold():
                    rank = 2
                else:
                    continue
                ranked.append((rank, it.description.casefold(), it.id, it))
            ranked.sort(key=lambda t: t[:3])
            items = [t[3] for t in ranked]
        return items[:limit] if limit is not None else items

    def _blocked(self, db: sqlite3.Connection) -> set[int]:
        """Ids that must not go to a new item."""
        blocked = {r[0] for r in db.execute("SELECT item_id FROM reserved_ids")}
        blocked |= {r[0] for r in db.execute("SELECT item_id FROM orphans")}
        blocked |= {r[0] for r in db.execute("SELECT id FROM items WHERE deleted = 0")}
        # A deleted item keeps its id while any scale may still hold it.
        blocked |= {r[0] for r in db.execute(
            "SELECT s.item_id FROM scale_state s JOIN items i ON i.id = s.item_id "
            "WHERE i.deleted = 1 AND s.belief IN ('present', 'unknown')")}
        return blocked

    def free_ids(self, count: int, max_id: int = MAX_ID) -> list[int]:
        """Lowest unused ids, ascending. Skips live, reserved and orphan ids.

        May return fewer than `count`, or none. `max_id` never goes above 500.
        """
        top = min(max_id, MAX_ID)
        blocked = self._blocked(self._conn())
        out: list[int] = []
        for n in range(MIN_ID, top + 1):
            if len(out) >= count:
                break
            if n not in blocked:
                out.append(n)
        return out

    def next_free_id(self) -> int:
        """The lowest unused id, or a clear error if every id is taken."""
        free = self.free_ids(1)
        if not free:
            raise NoFreeIdError(
                f"There are no free ids. All of {MIN_ID} to {MAX_ID} are in use, reserved, "
                f"or may hold an item on a scale that is not in the item list.")
        return free[0]

    def _write_item(self, db: sqlite3.Connection, item: Item, op: str, *, replacing_orphan: bool = False) -> None:
        values = _item_values(item)
        existing = db.execute(_SELECT + " WHERE id = ?", (item.id,)).fetchone()
        live = existing is not None and not existing["deleted"]
        clash = db.execute("SELECT id, description FROM items WHERE plu = ? AND deleted = 0 AND id != ?",
                           (item.plu, item.id)).fetchone()
        if clash:
            raise DuplicatePluError(
                f"PLU {item.plu} is already used by item {clash['id']} ({clash['description']})")
        if not live:
            # A new item. Its id must not be reserved or held by an orphan.
            blocked = self._blocked(db)
            if replacing_orphan:
                reserved = db.execute("SELECT 1 FROM reserved_ids WHERE item_id = ?", (item.id,)).fetchone()
                is_orphan = db.execute("SELECT 1 FROM orphans WHERE item_id = ?", (item.id,)).fetchone()
                if is_orphan and not reserved:
                    blocked = blocked - {item.id}
            if item.id in blocked:
                raise IdInUseError(f"id {item.id} is not free")
        before = None
        if existing is not None:
            before = [str(existing["id"])] + [existing[c] for c in _COLS[1:]]
        if existing is None:
            db.execute("INSERT INTO items (" + ", ".join(_COLS) + ") VALUES (" + ",".join("?" * 20) + ")",
                       (item.id, *values[1:]))
        else:
            sets = ", ".join(f"{c} = ?" for c in _COLS[1:])
            db.execute(f"UPDATE items SET {sets}, deleted = 0, deleted_at = NULL WHERE id = ?",
                       (*values[1:], item.id))
        self._history(db, item.id, op if live or existing is None else "revive", before, values)

    def upsert_item(self, item: Item, *, replacing_orphan: bool = False) -> None:
        """Add the item, or replace the live item with the same id.

        Raises DuplicatePluError if another live item has the PLU, and
        IdInUseError if a new id is reserved, orphaned, or held by a deleted
        item a scale may still have. replacing_orphan lets a new item take a recorded
        orphan id. Its notes stay, for any scale that has not taken the record.
        """
        with self._tx() as db:
            row = db.execute("SELECT deleted FROM items WHERE id = ?", (item.id,)).fetchone()
            self._write_item(db, item, "update" if row and not row["deleted"] else "add", replacing_orphan=replacing_orphan)
            self._bump(db)

    def remove_item(self, item_id: int) -> None:
        """Mark an item deleted. The row stays, so history and the id are kept.

        Does nothing if the item is missing or already deleted.
        """
        _check_id(item_id)
        with self._tx() as db:
            row = db.execute(_SELECT + " WHERE id = ? AND deleted = 0", (item_id,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE items SET deleted = 1, deleted_at = ? WHERE id = ?", (_now(), item_id))
            self._history(db, item_id, "delete", [str(row["id"])] + [row[c] for c in _COLS[1:]], None)
            self._bump(db)

    def import_items(self, items: Iterable[Item]) -> int:
        """Load many items at once, keeping ids and timestamps. All or nothing.

        For seeding an empty catalog. Refuses if the catalog already has any item row.
        """
        batch = list(items)
        with self._tx() as db:
            if db.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
                raise CatalogError("import needs an empty catalog")
            for it in batch:
                self._write_item(db, it, "import")
            self._bump(db)
            if batch:
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', ?)", (_now(),))
        return len(batch)

    # ------------------------------------------------ orphans and reserved

    def mark_orphan_id(self, item_id: int, scale: str, note: str = "") -> None:
        """Note that `scale` holds, or may hold, an item at this id that is not in our list."""
        n = _check_id(item_id)
        with self._tx() as db:
            db.execute("INSERT INTO orphans (scale, item_id, note, noted_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(scale, item_id) DO UPDATE SET note = excluded.note",
                       (scale, n, note, _now()))

    def clear_orphan_id(self, item_id: int, scale: str) -> bool:
        """Forget an orphan note, for example after the item was deleted or listed."""
        with self._tx() as db:
            return db.execute("DELETE FROM orphans WHERE scale = ? AND item_id = ?",
                              (scale, item_id)).rowcount > 0

    def orphan_ids(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for r in self._conn().execute("SELECT item_id, scale FROM orphans ORDER BY item_id, scale"):
            out.setdefault(r["item_id"], []).append(r["scale"])
        return out

    def orphan_notes(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn().execute(
            "SELECT item_id, scale, note, noted_at FROM orphans ORDER BY item_id, scale")]

    def reserve_id(self, item_id: int, reason: str = "") -> None:
        n = _check_id(item_id)
        with self._tx() as db:
            db.execute("INSERT INTO reserved_ids (item_id, reason, reserved_at) VALUES (?,?,?) "
                       "ON CONFLICT(item_id) DO UPDATE SET reason = excluded.reason", (n, reason, _now()))

    def unreserve_id(self, item_id: int) -> bool:
        with self._tx() as db:
            return db.execute("DELETE FROM reserved_ids WHERE item_id = ?", (item_id,)).rowcount > 0

    def reserved_ids(self) -> dict[int, str]:
        return {r["item_id"]: r["reason"] for r in self._conn().execute(
            "SELECT item_id, reason FROM reserved_ids ORDER BY item_id")}

    # ------------------------------------------------- what scales hold

    def record_scale_state(self, item_id: int, scale: str, status: str, change_id: str, at: str, *,
                           belief: str | None = None, record: Item | None = None) -> None:
        """Record the last result for an item on a scale.

        `belief` is what we think the scale now holds: present, absent or
        unknown. If you leave it out it is worked out: verified or
        accepted_not_verified gives present, unknown gives unknown, and any
        other status keeps the earlier belief (or unknown if there was none).
        Pass belief="absent" when a delete was accepted. `record` is the item
        believed to be on the scale.
        """
        with self._tx() as db:
            self._write_scale_state(db, item_id, scale, status, change_id, at, belief, record)

    def record_scale_states(self, scale: str, rows: Iterable[tuple[int, str, str | None, Item | None]],
                            change_id: str, at: str) -> None:
        """Many rows of (item id, status, belief, record) for one scale, in one transaction.
        A full send has up to 500, and each transaction waits for the disk."""
        with self._tx() as db:
            for item_id, status, belief, record in rows:
                self._write_scale_state(db, item_id, scale, status, change_id, at, belief, record)

    @staticmethod
    def _write_scale_state(db: sqlite3.Connection, item_id: int, scale: str, status: str, change_id: str, at: str,
                           belief: str | None, record: Item | None) -> None:
        n = _check_id(item_id)
        if belief is not None and belief not in BELIEFS:
            raise CatalogError(f"belief must be one of {BELIEFS}")
        if belief is None:
            if status in _SUCCESS:
                belief = "present"
            elif status == "unknown":
                belief = "unknown"
            else:
                prev = db.execute("SELECT belief FROM scale_state WHERE scale = ? AND item_id = ?",
                                  (scale, n)).fetchone()
                belief = prev["belief"] if prev else "unknown"
        rec = json.dumps(_item_values(record), ensure_ascii=False) if record is not None else None
        db.execute(
            "INSERT INTO scale_state (scale, item_id, status, change_id, at, belief, record) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(scale, item_id) DO UPDATE SET "
            "status = excluded.status, change_id = excluded.change_id, at = excluded.at, "
            "belief = excluded.belief, "
            "record = COALESCE(excluded.record, CASE WHEN excluded.belief = 'absent' THEN NULL "
            "ELSE scale_state.record END)",
            (scale, n, status, change_id, at, belief, rec))

    def scale_states(self, item_id: int) -> dict[str, dict]:
        """Scale name to {"status", "change_id", "at", "belief", "record"}."""
        out: dict[str, dict] = {}
        for r in self._conn().execute("SELECT * FROM scale_state WHERE item_id = ? ORDER BY scale",
                                      (item_id,)):
            out[r["scale"]] = {"status": r["status"], "change_id": r["change_id"], "at": r["at"],
                               "belief": r["belief"],
                               "record": json.loads(r["record"]) if r["record"] else None}
        return out

    def record_scale_reads(self, scale: str, items: Iterable[Item], at: str, *,
                           covered_up_to: int | None = None) -> None:
        """Keep the last record read from a scale, per id, and when. Exact text.

        covered_up_to says the read went through every id up to there, so an id at or
        below it that is not in items now holds nothing. That is recorded too. A placeholder
        is an empty slot, so the service leaves it out of items.

        A read that shows nothing at a deleted item's id also confirms that this scale no
        longer holds it, which is what frees the id for reuse. So covered_up_to must not go past
        what the read can vouch for. The item's PLU under another id of this scale means it is
        still on there, so that does not confirm anything.
        """
        held = {it.id: it for it in items}
        rows = [(scale, n, json.dumps(it.to_fields(), ensure_ascii=False), at) for n, it in held.items()]
        empty: list[int] = []
        if covered_up_to is not None:
            empty = [n for n in range(MIN_ID, min(covered_up_to, MAX_ID) + 1) if n not in held]
            rows.extend((scale, n, "null", at) for n in empty)
        with self._tx() as db:
            db.executemany(
                "INSERT INTO scale_read (scale, item_id, record, at) VALUES (?,?,?,?) "
                "ON CONFLICT(scale, item_id) DO UPDATE SET record = excluded.record, at = excluded.at", rows)
            plus_held = {it.plu for it in held.values()}
            deleted_plu = {r["id"]: r["plu"] for r in db.execute("SELECT id, plu FROM items WHERE deleted = 1")}
            gone = [n for n in empty if deleted_plu.get(n) not in plus_held]
            db.executemany(
                "UPDATE scale_state SET belief = 'absent', record = NULL WHERE scale = ? AND item_id = ? "
                "AND belief != 'absent' AND item_id IN (SELECT id FROM items WHERE deleted = 1)",
                [(scale, n) for n in gone])

    def scale_state_rows(self, scale: str) -> dict[int, dict]:
        """Every state row for one scale, by id, whatever the belief."""
        return {r["item_id"]: {"status": r["status"], "belief": r["belief"], "change_id": r["change_id"], "at": r["at"]}
                for r in self._conn().execute("SELECT * FROM scale_state WHERE scale = ?", (scale,))}

    def last_reads_for_scale(self, scale: str) -> dict[int, dict]:
        """Id to {"record": Item or None, "at"}, for every id a read of this scale has reached."""
        out: dict[int, dict] = {}
        for r in self._conn().execute("SELECT * FROM scale_read WHERE scale = ?", (scale,)):
            fields = json.loads(r["record"])
            out[r["item_id"]] = {"record": Item.from_fields(fields) if fields is not None else None, "at": r["at"]}
        return out

    def last_reads(self, item_id: int) -> dict[str, dict]:
        """Scale name to {"record": Item or None, "at"}. None means the read found nothing at this id."""
        out: dict[str, dict] = {}
        for r in self._conn().execute("SELECT * FROM scale_read WHERE item_id = ? ORDER BY scale", (item_id,)):
            fields = json.loads(r["record"])
            out[r["scale"]] = {"record": Item.from_fields(fields) if fields is not None else None, "at": r["at"]}
        return out

    def believed_on_scale(self, scale: str) -> dict[int, dict]:
        """Everything we believe `scale` holds or may hold: id to the scale state."""
        out: dict[int, dict] = {}
        for r in self._conn().execute(
                "SELECT * FROM scale_state WHERE scale = ? AND belief != 'absent' ORDER BY item_id", (scale,)):
            out[r["item_id"]] = {"status": r["status"], "belief": r["belief"], "at": r["at"],
                                 "change_id": r["change_id"],
                                 "record": json.loads(r["record"]) if r["record"] else None}
        return out

    def believe_scale_holds_listed_items(self, scale: str, status: str, change_id: str, at: str) -> int:
        """Record that `scale` should hold every live item as listed now, after a full send.

        Nobody has checked, so the belief is unknown until a read shows it. Returns how many items.
        """
        with self._tx() as db:
            rows = db.execute(_SELECT + " WHERE deleted = 0 ORDER BY id").fetchall()
            for r in rows:
                rec = json.dumps([str(r["id"])] + [r[c] for c in _COLS[1:]], ensure_ascii=False)
                db.execute(
                    "INSERT INTO scale_state (scale, item_id, status, change_id, at, belief, record) "
                    "VALUES (?,?,?,?,?,'unknown',?) ON CONFLICT(scale, item_id) DO UPDATE SET "
                    "status = excluded.status, change_id = excluded.change_id, at = excluded.at, "
                    "belief = 'unknown', record = excluded.record",
                    (scale, r["id"], status, change_id, at, rec))
        return len(rows)

    # ------------------------------------------- PLU changes not yet finished

    def note_plu_move(self, scale: str, old: Item, *, trying: bool = False) -> None:
        """The catalog's PLU changed, and this scale has not confirmed it, so it may still hold `old`.

        trying means the other way round: the catalog has not moved, because no scale is known to
        have taken the change, and this scale may hold `old` as the new record from that change.
        """
        with self._tx() as db:
            db.execute(
                "INSERT INTO plu_moves (scale, item_id, old_plu, record, noted_at, trying) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(scale, item_id, old_plu) DO UPDATE SET record = excluded.record, "
                "noted_at = excluded.noted_at, trying = excluded.trying",
                (scale, _check_id(old.id), old.plu, json.dumps(_item_values(old), ensure_ascii=False), _now(), int(trying)))

    def plu_moves(self, item_id: int, scale: str) -> dict[str, Item]:
        """Old PLU to old record, for what this scale may still hold at this id."""
        return {r["old_plu"]: Item.from_fields(json.loads(r["record"])) for r in self._conn().execute(
            "SELECT old_plu, record FROM plu_moves WHERE scale = ? AND item_id = ?", (scale, item_id))}

    def clear_plu_moves(self, item_id: int, scale: str) -> None:
        """This scale confirmed what the catalog holds at this id."""
        with self._tx() as db:
            db.execute("DELETE FROM plu_moves WHERE scale = ? AND item_id = ?", (scale, item_id))

    def all_plu_moves(self) -> list[dict]:
        """Every PLU change a scale has not confirmed: {"scale", "item_id", "old_plu", "trying"}."""
        return [dict(r) for r in self._conn().execute(
            "SELECT scale, item_id, old_plu, trying FROM plu_moves ORDER BY item_id, scale, noted_at")]

    def settle_plu_moves_from_read(self, scale: str, plus_held: Iterable[str]) -> None:
        """A read this scale can vouch for shows these PLUs and no others, so any other noted PLU is not on it."""
        held = set(plus_held)
        gone = [m for m in self.all_plu_moves() if m["scale"] == scale and m["old_plu"] not in held]
        if gone:
            with self._tx() as db:
                db.executemany("DELETE FROM plu_moves WHERE scale = ? AND item_id = ? AND old_plu = ?",
                               [(scale, m["item_id"], m["old_plu"]) for m in gone])

    def old_plus_in_use(self) -> dict[str, int]:
        """Old PLU to its id, for every PLU some scale may still hold from an unfinished change."""
        return {r["old_plu"]: r["item_id"] for r in self._conn().execute(
            "SELECT DISTINCT old_plu, item_id FROM plu_moves ORDER BY noted_at")}

    # ---------------------------------------------------------- previews

    def save_preview(self, preview_id: str, payload: dict, created_at: str, expires_at: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO previews (preview_id, payload, created_at, expires_at) VALUES (?,?,?,?)",
                       (preview_id, json.dumps(payload, ensure_ascii=False), created_at, expires_at))

    def load_preview(self, preview_id: str) -> dict | None:
        r = self._conn().execute("SELECT * FROM previews WHERE preview_id = ?", (preview_id,)).fetchone()
        if r is None:
            return None
        return {"preview_id": r["preview_id"], "payload": json.loads(r["payload"]),
                "created_at": r["created_at"], "expires_at": r["expires_at"],
                "consumed_by": r["consumed_by"]}

    def consume_preview(self, preview_id: str, change_id: str) -> bool:
        """True the first time only. False if already consumed or missing."""
        with self._tx() as db:
            return self._consume(db, preview_id, change_id)

    def start_change(self, preview_id: str, change_id: str, event: str, payload: dict, at: str) -> bool:
        """Consume the preview and log the start in one transaction, so no reader can find a preview
        that names a change with no log entry. False, with nothing written, if already consumed or missing."""
        with self._tx() as db:
            if not self._consume(db, preview_id, change_id):
                return False
            self._insert_log(db, change_id, event, payload, at)
            return True

    @staticmethod
    def _consume(db: sqlite3.Connection, preview_id: str, change_id: str) -> bool:
        return db.execute("UPDATE previews SET consumed_by = ? WHERE preview_id = ? "
                          "AND consumed_by IS NULL", (change_id, preview_id)).rowcount == 1

    # --------------------------------------------------------- change log

    def append_log(self, change_id: str, event: str, payload: dict, at: str) -> None:
        """Append one entry. The log has no update and no delete, enforced by triggers."""
        with self._tx() as db:
            self._insert_log(db, change_id, event, payload, at)

    @staticmethod
    def _insert_log(db: sqlite3.Connection, change_id: str, event: str, payload: dict, at: str) -> None:
        scale = payload.get("scale") if isinstance(payload, dict) else None
        db.execute("INSERT INTO change_log (change_id, event, payload, at, scale) VALUES (?,?,?,?,?)",
                   (change_id, event, json.dumps(payload, ensure_ascii=False),
                    at, scale if isinstance(scale, str) else None))

    def read_log(self, change_id: str) -> list[dict]:
        """Oldest first."""
        return [{"change_id": r["change_id"], "event": r["event"],
                 "payload": json.loads(r["payload"]), "at": r["at"]}
                for r in self._conn().execute(
                    "SELECT change_id, event, payload, at FROM change_log WHERE change_id = ? "
                    "ORDER BY log_id", (change_id,))]

    def list_change_ids(self, limit: int = 50) -> list[str]:
        """Newest first, by when each change first appears in the log."""
        return [r[0] for r in self._conn().execute(
            "SELECT change_id FROM change_log GROUP BY change_id ORDER BY MIN(log_id) DESC LIMIT ?",
            (limit,))]

    def open_change_ids(self) -> list[str]:
        """Changes that started and have no finish. Newest first."""
        return [r[0] for r in self._conn().execute(
            "SELECT change_id FROM change_log WHERE event = 'started' AND change_id NOT IN "
            "(SELECT change_id FROM change_log WHERE event = 'finished') ORDER BY log_id DESC")]

    def last_scale_event(self, scale: str) -> dict | None:
        r = self._conn().execute(
            "SELECT change_id, payload, at FROM change_log WHERE event = 'scale_result' AND scale = ? "
            "ORDER BY log_id DESC LIMIT 1", (scale,)).fetchone()
        if r is None:
            return None
        out = json.loads(r["payload"])
        out["change_id"] = r["change_id"]
        out["at"] = r["at"]
        return out
