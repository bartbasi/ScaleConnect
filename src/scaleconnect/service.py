# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""The library the interfaces call. See docs/LIBRARY.md and docs/DESIGN.md.

All validation lives here, before anything reaches a scale, because the scale does
not check values. Prices stay text or Decimal. The PIN and token are never logged.

Each scale is the truth about itself. The catalog is what we want. Every write follows
seven steps per scale: check identity, read first, compare, build from what the scale
holds, write, read back, and never resend automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import scale as scale_module
from .catalog import Catalog, no_reply_how
from .catalog import apply_lock as _apply_lock
from .config import (
    SCALELINK_NOT_AVAILABLE,
    Config,
    ScaleConfig,
    Settings,
    load_config,
    require_backup_settings,
    restrict_unofficial,
)
from .errors import (
    ApplyBusy,
    AcknowledgementRequired,
    BackupFailed,
    CatalogNotSeeded,
    ConfigChanged,
    ChangeNotFound,
    InterruptedChange,
    ItemNotFound,
    LogFailed,
    NotReadEnabled,
    NotWriteEnabled,
    PreviewAlreadyApplied,
    PreviewExpired,
    PreviewNotFound,
    PreviewStale,
    ReloadDifferences,
    ProtocolError,
    ScaleConnectError,
    ScaleMayBeFrozen,
    ValidationFailed,
)
from .models import (
    CHANGE_ACTIONS,
    DEFAULT_FIELDS_7_12,
    ITEM_NOT_SENT,
    LANDED_STATUSES,
    MAX_ITEM_ID,
    SOLD_BY_FIELD_7,
    STATUS_SEVERITY,
    UNSETTLED_STATUSES,
    Capabilities,
    Change,
    ChangeRecord,
    Difference,
    Drift,
    FieldDiff,
    HeldRecord,
    Item,
    ItemResult,
    ItemView,
    Preview,
    PreviewError,
    PreviewWarning,
    OrphanMarks,
    ReloadDifference,
    ReloadResult,
    UnfinishedWork,
    ScaleComparison,
    ScaleInfo,
    ScaleItemState,
    ScaleRead,
    ScaleResult,
    ScaleStatus,
    is_placeholder,
    is_reserved_plu,
    placeholder_record,
)
from .scale import ScaleClient

_PRICE = re.compile(r"(0|[1-9][0-9]{0,2})\.[0-9]{2}")
# None of the 484 real items starts with 0 (1001 to 7262), and nobody knows what a scale does with one.
_PLU = re.compile(r"[1-9][0-9]{3}")
_PRICE_MAX = Decimal("999.99")
_OTHER_FIELD_MAX_LENGTH = 20
_BACKUP_MARKER_VERSION = 2

_DIFF_FIELDS = ("code", "plu", "description", "price", "field6", "timestamp")
_DIFF_GROUPS = ("fields_7_12", "fields_14_20")

_DELETE = "delete"
_WRITE_ACTIONS = ("add", "edit", "settle_push", _DELETE)
_SETTLE_ACCEPT = "settle_accept"
_SETTLE_PUSH = "settle_push"
_RELOAD = "reload"
_RELOAD_DUMP_NOTE = "not_taken_no_read_before_a_reload"
# A reload trusts a last read only this long. Prices change in ScaleLink with no word to us. It is
# long enough to run a comparison, read the list and run the reload, and it matches a preview's life.
RELOAD_READ_MAX_AGE = timedelta(minutes=15)
_FIELD_WORDS = {"field6": "field 6", "fields_7_12": "sold by, tax and other settings (fields 7 to 12)", "fields_14_20": "other settings (fields 14 to 20)"}

# What a change ended as on a scale, for what is unfinished. Something went out, so the scale may hold anything:
_MISSED_LANDED = frozenset({ScaleStatus.VERIFIED.value, ScaleStatus.ACCEPTED_NOT_VERIFIED.value})
# Results where something reached the scale, so its record may have changed. Only these end a row from a last read.
_REACHED_THE_SCALE = frozenset({
    ScaleStatus.VERIFIED.value, ScaleStatus.ACCEPTED_NOT_VERIFIED.value, ScaleStatus.UNKNOWN.value, ScaleStatus.MISMATCH.value,
})
_MISSED_UNSETTLED = frozenset({ScaleStatus.UNKNOWN.value, ScaleStatus.MISMATCH.value})
_MISSED_MAY_HOLD = frozenset({ScaleStatus.ACCEPTED_NOT_VERIFIED.value}) | _MISSED_UNSETTLED
# Nothing went out, so the scale is behind only if the catalog took the change. Writes off for a scale
# is a person's choice, and a change that left a scale out with scales is too, so neither is here.
_MISSED_NOT_SENT = frozenset({
    ScaleStatus.REJECTED.value,
    ScaleStatus.READ_FAILED.value,
    ScaleStatus.UNREACHABLE.value,
    ScaleStatus.WRONG_SCALE.value,
    ScaleStatus.DRIFT_REFUSED.value,
    ScaleStatus.NOT_AVAILABLE.value,
    ITEM_NOT_SENT,
})

_LIST_FAILED = "list_failed"  # a row that says the list of unfinished work could not be built

EVENT_STARTED = "started"
EVENT_SCALE_RESULT = "scale_result"
EVENT_FINISHED = "finished"
EVENT_BACKUP_FAILED = "backup_failed"
_CHANGE_EVENTS = (EVENT_STARTED, EVENT_BACKUP_FAILED)
log = logging.getLogger("scaleconnect.service")


def _printable(value: str) -> bool:
    return all(0x20 <= ord(ch) <= 0x7E for ch in value)


def _parsed_time(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _strictly_before(first: Any, second: Any) -> bool:
    """first is a time before second. A time that cannot be read, or one with no offset beside one with, is not."""
    a, b = _parsed_time(first), _parsed_time(second)
    try:
        return a is not None and b is not None and a < b
    except TypeError:
        return False


def _frozen_skip_words(name: str) -> str:
    """Starts with the scale's name, which is how a reader picks the scale out. Library words, no record text."""
    return f"{name}: it gave no reply earlier, and nothing has read it since, so it was not read now. Nothing was sent to it."


NAME_IT_ALONE = "To read it, name only this scale, after a person has looked at it."


def compare_leaves_out_frozen(scales: Sequence[str] | None) -> bool:
    """For the MCP server and the command line: a compare reads a scale that may be frozen only when it is
    the only scale named. With no scales, or with more than one, it is left out, so listing every scale to
    be thorough cannot read it. Naming it alone is the deliberate check, after a person has looked at it."""
    if scales is None or isinstance(scales, (str, bytes)):
        return True
    return len(set(scales)) != 1


def _note_repeat(read: Any, name: str, repeats: set[str] | None) -> None:
    """The scale needed a second read. Only that fact goes on: no words from the read, which can quote a record."""
    if repeats is not None and getattr(read, "repeated", False) is True:
        repeats.add(name)


def _real_records(table: Mapping[int, Item]) -> dict[int, Item]:
    """A scale's table without its placeholders. A placeholder is an empty slot, not a record."""
    return {item_id: record for item_id, record in table.items() if not is_placeholder(record)}


def _id_ranges(ids: Sequence[int]) -> str:
    """Ids in plain words: 3 to 8, 10."""
    parts: list[str] = []
    ordered = sorted(ids)
    start = previous = ordered[0] if ordered else 0
    for item_id in ordered[1:] + [None]:
        if item_id is not None and item_id == previous + 1:
            previous = item_id
            continue
        parts.append(str(start) if start == previous else f"{start} to {previous}")
        if item_id is not None:
            start = previous = item_id
    return ", ".join(parts) if ordered else ""


def _text_of(item: Item, name: str) -> str:
    value = getattr(item, name)
    return " ".join(value) if isinstance(value, tuple) else value


def _field_differences(before: Item, after: Item) -> dict[str, FieldDiff]:
    """Which fields differ, as exact text. Includes the timestamp."""
    out: dict[str, FieldDiff] = {}
    for name in (*_DIFF_FIELDS, *_DIFF_GROUPS):
        old, new = _text_of(before, name), _text_of(after, name)
        if getattr(before, name) != getattr(after, name):
            out[name] = FieldDiff(old, new)
    return out


def _is_other_product(wanted: Item, held: Item) -> bool:
    """A price must never land on a different product. The PLU and Code say which product it is."""
    return held.plu != wanted.plu or held.code != wanted.code


def _quoted(text: str) -> str:
    """Text from a scale record, shown as data. It cannot start a new line or pass for our own words."""
    return json.dumps(text)


def _other_product_message(name: str, item_id: int, wanted: Item, held: Item) -> str:
    return (
        f"{name} holds a different product at id {item_id}: PLU {_quoted(held.plu)}, Code {_quoted(held.code)}, "
        f"and the catalog has PLU {wanted.plu}, Code {wanted.code}. This cannot be acknowledged. Settle it first."
    )


def unfinished_plu_message(scale: str, plu: str, item_id: int | None) -> str:
    """What a Drift says when a scale is still on the old PLU. Interfaces compare a message with
    this exact text to recognize that case, because no other drift can read the same."""
    return (
        f"{scale} still holds PLU {_quoted(plu)} at id {item_id}, because a PLU change there has not finished. "
        "Repeat that edit to finish it."
    )


def _drift_token(scale: str, item_id: int, held: Item | None) -> str:
    """Names one scale, one id and exactly what it held. Another value needs a new look."""
    text = "\x1f".join(held.to_fields()) if held else "missing"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{scale}:{item_id}:{digest}"


def default_backup_hook(
    scale: str,
    items: Sequence[Item],
    backup_dir: Path,
    *,
    scalelink_db_path: Path | None = None,
    when: datetime | None = None,
) -> Path:
    """Snapshot the item list to a new timestamped folder. Runs before a first write."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = Path(backup_dir) / f"{stamp}-{scale}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True, exist_ok=False)
    with open(folder / "items.json", "w", encoding="utf-8") as handle:
        json.dump([item.to_dict() for item in items], handle, indent=1)
    if scalelink_db_path is not None:
        shutil.copy2(scalelink_db_path, folder / Path(scalelink_db_path).name)
    return folder


@dataclass(frozen=True)
class _Plan:
    """One change as planned at preview time. requested holds only what was asked for."""

    difference: Difference
    requested: dict[str, str]
    replace_orphan: bool = False

    @property
    def moves_plu(self) -> bool:
        """An edit that asks for a PLU. Only this may write a new PLU over what a scale holds."""
        return "plu" in self.requested

    def to_dict(self) -> dict[str, Any]:
        return {
            "difference": self.difference.to_dict(),
            "requested": dict(self.requested),
            "replace_orphan": self.replace_orphan,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_Plan":
        return cls(Difference.from_dict(data["difference"]), dict(data["requested"]), bool(data.get("replace_orphan")))


class ScaleConnectService:
    def __init__(
        self,
        config: Config,
        catalog: Catalog,
        *,
        backup_hook: Callable[..., Any] | None = None,
        scale_factory: Callable[..., Any] = ScaleClient,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not catalog.is_seeded():
            raise CatalogNotSeeded("This catalog was never seeded, so it cannot be trusted. Run the seed step first.")
        self._config = config
        self._settings: Settings = config.settings
        self._catalog = catalog
        self._backup_hook = backup_hook
        self._scale_factory = scale_factory
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._lock_path = Path(self._settings.state_dir) / "apply.lock"
        # No replies the log could not take: scale to (what read, at, status). See _note_no_reply.
        self._unnoted: dict[str, tuple[str, str, str | None]] = {}
        self._unnoted_lock = threading.Lock()
        # Held note files this process has dealt with: logged, or its scale answered since. Only a file that could
        # not be removed stays in this set and on disk. Such a file no longer counts, here. See _write_unnoted.
        self._held_done: set[str] = set()
        # Held note files already named in the server log, so a bad or stuck file is logged once per process.
        self._held_told: set[str] = set()

    @classmethod
    def open(cls, config_path: str | os.PathLike[str] | None = None) -> "ScaleConnectService":
        config = restrict_unofficial(load_config(config_path))
        require_backup_settings(config)
        catalog = Catalog.open_existing(config.settings.catalog_path)
        try:
            return cls(config, catalog)
        except CatalogNotSeeded:
            catalog.close()
            raise

    def close(self) -> None:
        self._catalog.close()

    def _now_text(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    # ------------------------------------------------------------ current flags

    def _current_scales(self) -> tuple[ScaleConfig, ...]:
        """The config as it is now. The file may only narrow what was loaded, and it wins at
        once. If the file cannot be read now, every scale counts as off."""
        loaded = self._config.scales
        if self._config.path is None:
            return loaded
        try:
            fresh = {s.name: s for s in restrict_unofficial(load_config(self._config.path)).scales}
        except ScaleConnectError:
            return tuple(replace(s, write_enabled=False, read_enabled=False) for s in loaded)
        current = []
        for scale in loaded:
            now = fresh.get(scale.name)
            same_place = now is not None and now.address == scale.address and now.port == scale.port
            current.append(
                replace(
                    scale,
                    write_enabled=scale.write_enabled is True and same_place and now.write_enabled is True,
                    read_enabled=scale.read_enabled is True and same_place and now.read_enabled is True,
                )
            )
        return tuple(current)

    def _current_scale(self, name: str) -> ScaleConfig | None:
        return next((s for s in self._current_scales() if s.name == name), None)

    def config_changed(self) -> str | None:
        """Plain words if the config file says something this running service has not loaded, or None.

        A scale added or turned on would otherwise get nothing, while the rest report success. What only
        narrows, such as a flag turned off or a scale taken out, is honored at once by _current_scales.
        """
        if self._config.path is None:
            return None
        try:
            fresh = restrict_unofficial(load_config(self._config.path))
        except ScaleConnectError:
            return None  # every scale counts as off then, so nothing can be written anyway
        loaded = {s.name: s for s in self._config.scales}
        changed = False
        for scale in fresh.scales:
            was = loaded.get(scale.name)
            if was is None or scale.serial != was.serial:
                changed = True
            elif (scale.write_enabled is True and was.write_enabled is not True) or (scale.read_enabled is True and was.read_enabled is not True):
                changed = True
        if replace(fresh, scales=(), warnings=()) != replace(self._config, scales=(), warnings=()):
            changed = True
        if not changed:
            return None
        return "The config file has changed since this service started, so nothing was sent. Restart the web page and the MCP server together, then try again."

    # ------------------------------------------------------------ reading

    def capabilities(self) -> Capabilities:
        delete = scale_module.DELETE_AVAILABLE
        read = scale_module.READ_AVAILABLE
        notes: dict[str, str] = {}
        if not delete:
            notes["delete"] = "Deletes are turned off in this version of the library."
        return Capabilities(add=True, edit=True, delete=delete, read=read, verify=read, notes=notes)

    def apply_running(self) -> bool:
        """True if a change or read holds the lock now, in this process or another."""
        try:
            with _apply_lock(self._lock_path):
                return False
        except ApplyBusy:
            return True

    def _interrupted(self, *, lock_held: bool = False) -> dict[str, set[int]]:
        """Changes with a start and no finish, and the ids they touched. While anything holds
        the lock, one of those may still be running, so none of them count as interrupted."""
        out: dict[str, set[int]] = {}
        if not self._catalog.open_change_ids():
            return out
        if not lock_held and self.apply_running():
            return out
        for change_id in self._catalog.open_change_ids():
            ids: set[int] = set()
            for event in self._catalog.read_log(change_id):
                if event["event"] == EVENT_STARTED:
                    ids.update(c.get("item_id") for c in event["payload"].get("changes", []))
            out[change_id] = {i for i in ids if i is not None}
        return out

    def list_scales(self) -> list[ScaleInfo]:
        """Never contacts a scale. Last known status comes from the change log."""
        infos = []
        interrupted = tuple(self._interrupted())
        for scale in self._current_scales():
            last = self._catalog.last_scale_event(scale.name) or {}
            infos.append(
                ScaleInfo(
                    name=scale.name,
                    write_enabled=scale.write_enabled is True,
                    read_enabled=scale.write_enabled is True or scale.read_enabled is True,
                    last_status=last.get("status"),
                    last_change_id=last.get("change_id"),
                    last_seen_at=last.get("at"),
                    last_message=last.get("message"),
                    interrupted_change_ids=interrupted,
                )
            )
        return infos

    def search_items(self, query: str, limit: int = 50) -> list[Item]:
        return self._catalog.search(query, limit=limit)

    def get_item(self, *, plu: str | None = None, id: int | None = None) -> ItemView:
        if (plu is None) == (id is None):
            raise ValidationFailed("Give exactly one of plu and id.")
        item = self._catalog.get_by_plu(plu) if plu is not None else self._catalog.get_by_id(id)
        if item is None:
            what = f"PLU {plu}" if plu is not None else f"id {id}"
            raise ItemNotFound(f"No item with {what}.")
        names = {s.name for s in self._config.scales}
        states = self._catalog.scale_states(item.id)
        reads = self._catalog.last_reads(item.id)
        view: dict[str, ScaleItemState] = {}
        for name in names & (set(states) | set(reads)):
            state = states.get(name, {})
            read = reads.get(name, {})
            view[name] = ScaleItemState(
                scale=name,
                status=state.get("status"),
                belief=state.get("belief"),
                change_id=state.get("change_id"),
                at=state.get("at"),
                last_read=read.get("record"),
                last_read_at=read.get("at"),
                last_read_matches=read["record"].to_fields() == item.to_fields() if read.get("record") else None,
                last_read_found_nothing=bool(read) and read.get("record") is None,
            )
        interrupted = tuple(c for c, ids in self._interrupted().items() if item.id in ids)
        return ItemView(item=item, scales=view, interrupted_change_ids=interrupted)

    # ------------------------------------------------------------ validation

    def _validate_price(self, price: Any, index: int | None) -> list[PreviewError]:
        def bad(message: str) -> list[PreviewError]:
            return [PreviewError("invalid_price", message, index, "price")]

        if not isinstance(price, str):
            return bad('Price must be text such as "9.99", not a number.')
        if not _PRICE.fullmatch(price):
            return bad("Price must look like 9.99, with two decimals and no extra zeros in front.")
        try:
            value = Decimal(price)
        except InvalidOperation:
            return bad("Price is not a number.")
        if value <= 0:
            return bad("Price must be more than 0.")
        if value > _PRICE_MAX:
            return bad("Price must be at most 999.99.")
        return []

    def _validate_new_description(self, description: Any, index: int | None) -> list[PreviewError]:
        """ScaleLink trims spaces, so a value someone asks for must not carry them."""
        if isinstance(description, str) and description != description.strip() and description.strip():
            return [PreviewError("invalid_description", "Description cannot start or end with a space.", index, "description")]
        return []

    def _validate_item(self, item: Item, index: int | None) -> list[PreviewError]:
        """Checks a whole record, new or edited, as it would go to the scale."""
        errors: list[PreviewError] = []
        if not 1 <= item.id <= MAX_ITEM_ID:
            errors.append(PreviewError("invalid_id", f"Id must be from 1 to {MAX_ITEM_ID}.", index, "id"))
        if not _PLU.fullmatch(item.plu):
            errors.append(PreviewError("invalid_plu", "PLU must be exactly 4 digits, and cannot start with 0.", index, "plu"))
        if not _PLU.fullmatch(item.code):
            errors.append(PreviewError("invalid_code", "Code must be exactly 4 digits, and cannot start with 0.", index, "code"))
        elif item.code != item.plu:
            errors.append(
                PreviewError(
                    "code_plu_mismatch", f"Code {item.code} and PLU {item.plu} must be the same.", index, "code"
                )
            )
        if is_reserved_plu(item.plu) or is_reserved_plu(item.code):
            errors.append(self._reserved_error(index, "plu"))
        errors.extend(self._validate_price(item.price, index))
        max_length = self._settings.description_max_length
        if not item.description.strip():
            errors.append(PreviewError("invalid_description", "Description cannot be empty.", index, "description"))
        elif not _printable(item.description):
            errors.append(
                PreviewError(
                    "invalid_description",
                    "Description can hold only plain letters, digits and common symbols.",
                    index,
                    "description",
                )
            )
        elif len(item.description) > max_length:
            errors.append(
                PreviewError(
                    "invalid_description", f"Description can be at most {max_length} characters.", index, "description"
                )
            )
        others = {"field6": [item.field6], "fields_7_12": item.fields_7_12, "fields_14_20": item.fields_14_20}
        for name, values in others.items():
            if not all(_printable(value) and len(value) <= _OTHER_FIELD_MAX_LENGTH for value in values):
                errors.append(PreviewError("invalid_field", f"{name} holds a value that cannot be sent.", index, name))
        return errors

    @staticmethod
    def _normal_price(price: Any) -> Any:
        return str(price) if isinstance(price, Decimal) else price

    @staticmethod
    def _reserved_error(index: int | None, field: str) -> PreviewError:
        return PreviewError(
            "reserved_plu", "PLUs 9001 to 9500 are held back for placeholders. They cannot be added, edited or deleted.", index, field
        )

    def _find_target(
        self, change: Change, index: int, items: dict[int, Item]
    ) -> tuple[Item | None, list[PreviewError]]:
        if (change.target_plu is None) == (change.target_id is None):
            message = "Give exactly one of target_plu and target_id."
            return None, [PreviewError("invalid_change", message, index, "target_plu")]
        if is_reserved_plu(change.target_plu):
            return None, [self._reserved_error(index, "target_plu")]
        if change.target_id is not None:
            if isinstance(change.target_id, bool) or not isinstance(change.target_id, int):
                return None, [PreviewError("invalid_change", "target_id must be a whole number.", index, "target_id")]
            found = items.get(change.target_id)
            what = f"id {change.target_id}"
        else:
            found = next((i for i in items.values() if i.plu == change.target_plu), None)
            what = f"PLU {change.target_plu}"
        if found is None:
            return None, [PreviewError("item_not_found", f"No item with {what}.", index, None)]
        if is_reserved_plu(found.plu):
            return None, [self._reserved_error(index, "target_id")]
        return found, []

    # ------------------------------------------------------------ preview

    def _target_scales(self, scales: Sequence[str] | None) -> tuple[tuple[ScaleConfig, ...], list[PreviewError]]:
        current = self._current_scales()
        if scales is None:
            chosen = current
            errors: list[PreviewError] = []
        else:
            wanted = list(scales)
            known = {s.name for s in current}
            errors = [PreviewError("unknown_scale", f"No scale named {n}.", None, "scales") for n in wanted if n not in known]
            chosen = tuple(s for s in current if s.name in wanted)
        if not chosen and not errors:
            errors.append(PreviewError("no_scales", "No scales to send to.", None, "scales"))
        return chosen, errors

    def preview_changes(
        self,
        changes: Sequence[Change | Mapping[str, Any]],
        *,
        requested_by: str = "unknown",
        scales: Sequence[str] | None = None,
        read_scales: bool = False,
        avoid_frozen: bool = False,
        leave_out_frozen: bool = False,
    ) -> Preview:
        """Checks changes and changes nothing. Bad input comes back in errors. It does not raise.

        It contacts no scale unless read_scales is True. Apply repeats the check either way.

        avoid_frozen leaves out every scale that may be frozen, as the web page asks. Each one left out
        gets a scale_left_out warning, is named in left_out, and stays on the unfinished list as missing
        this change once it is applied. If no scale with writes on is left, it refuses with scale_may_be_frozen.
        leave_out_frozen does the same for the MCP server and the command line, whether scales names the
        scale or not. Only a compare that names that scale alone, or a reload, clears the flag.
        """
        now = self._clock()
        created_at = now.isoformat(timespec="seconds")
        targets, errors = self._target_scales(scales)
        left_out: list[str] = []
        if (avoid_frozen or leave_out_frozen) and not errors:
            frozen = set(self.scales_may_be_frozen())
            left_out = [s.name for s in targets if s.name in frozen and (s.write_enabled is True or s.read_enabled is True)]
            targets = tuple(s for s in targets if s.name not in left_out)
            if scales is not None:
                # A flagged scale outside scales misses this change too, as below.
                chosen = {s.name for s in targets} | set(left_out)
                left_out += [s.name for s in self._current_scales() if s.name not in chosen and s.name in frozen and s.write_enabled is True]
                left_out = self._config_order(left_out)
            if left_out and not any(s.write_enabled is True for s in targets):
                errors.append(PreviewError(
                    "scale_may_be_frozen",
                    f"{', '.join(left_out)} gave no reply earlier, and no other scale with writes on is left, so nothing was read or sent.",
                    None, "scales",
                ))
        elif scales is not None and not errors:
            # Claude is told to leave a scale that may be frozen out with scales. That scale misses this change
            # just as one the page leaves out does, so it is left out the same way and stays on the unfinished
            # list. Only a scale with writes on: one with writes off would not have been written anyway.
            chosen = {s.name for s in targets}
            frozen = set(self.scales_may_be_frozen())
            left_out = [s.name for s in self._current_scales() if s.name not in chosen and s.name in frozen and s.write_enabled is True]
        restart = self.config_changed()
        if restart:
            errors.append(PreviewError("config_changed", restart, None, None))
        if isinstance(changes, (str, bytes, Mapping)) or not isinstance(changes, Sequence):
            errors.append(PreviewError("invalid_change", "Changes must be a list.", None, None))
            changes = []
        if not changes and not errors:
            errors.append(PreviewError("no_changes", "There are no changes to preview.", None, None))

        catalog_version = self._catalog.version()
        catalog_items = self._catalog.list_items()
        before_items = {i.id: i for i in catalog_items}
        final = dict(before_items)
        free_ids = self._catalog.free_ids(MAX_ITEM_ID)
        touched: set[int] = set()
        plans: list[_Plan] = []
        warnings: list[PreviewWarning] = []

        for index, raw in enumerate(changes):
            try:
                change = raw if isinstance(raw, Change) else Change.from_dict(raw)
            except ValidationFailed as exc:
                errors.append(PreviewError("invalid_change", exc.message, index, None))
                continue
            except TypeError:
                errors.append(PreviewError("invalid_change", "This change is not in the expected form.", index, None))
                continue
            if change.action not in CHANGE_ACTIONS:
                errors.append(PreviewError("invalid_action", "Action must be add, edit or delete.", index, "action"))
                continue
            handler = {"add": self._plan_add, "edit": self._plan_edit, "delete": self._plan_delete}[change.action]
            try:
                handler(change, index, before_items, final, free_ids, touched, plans, warnings, errors)
            except (TypeError, ValueError):
                errors.append(PreviewError("invalid_change", "A value in this change has the wrong type.", index, None))

        differences = [p.difference for p in plans]
        errors.extend(self._duplicate_plus(final, before_items, differences, self._catalog.old_plus_in_use(), self._deleted_held()))
        drift: list[Drift] = []
        scales_read: list[str] = []
        fills: dict[str, list[int]] = {}
        repeats: set[str] = set()

        if not errors:
            configured = {s.name for s in self._config.scales}
            untouched = sorted(configured - {s.name for s in targets} - set(left_out))
            if left_out:
                warnings.append(
                    PreviewWarning(
                        "scale_left_out",
                        f"{', '.join(left_out)} gave no reply earlier, so this change leaves {'it' if len(left_out) == 1 else 'them'} out. "
                        "Nothing is read or sent there. Unless its writes are off, each stays on the unfinished list as missing this "
                        "change until a person has checked it.",
                        True,
                    )
                )
            if untouched:
                warnings.append(
                    PreviewWarning(
                        "scale_not_targeted",
                        f"This change will not touch {', '.join(untouched)}. Those scales keep what they hold.",
                        True,
                    )
                )
            for scale in targets:
                if scale.write_enabled is not True:
                    warnings.append(
                        PreviewWarning(
                            "scale_not_write_enabled",
                            f"Writes are off for {scale.name}. It will report skipped_not_write_enabled.",
                        )
                    )
            interrupted = self._interrupted()
            for change_id, ids in interrupted.items():
                if ids & touched:
                    warnings.append(
                        PreviewWarning(
                            "interrupted_change",
                            f"Change {change_id} touched these items and never finished. Apply will refuse until it is resolved.",
                        )
                    )
            if read_scales:
                drift, scales_read, fills = self._read_for_preview(targets, plans, warnings, repeats, avoid_frozen=avoid_frozen or leave_out_frozen)

        scale_names = tuple(s.name for s in targets)
        if errors:
            return Preview(
                ok=False,
                preview_id=None,
                created_at=created_at,
                expires_at=None,
                requested_by=requested_by,
                scales=scale_names,
                differences=tuple(differences),
                warnings=tuple(warnings),
                errors=tuple(errors),
                left_out=tuple(left_out),
            )

        preview_id = uuid.uuid4().hex
        expires = now + timedelta(seconds=self._settings.preview_ttl_seconds)
        expires_at = expires.isoformat(timespec="seconds")
        payload = {
            "preview_id": preview_id,
            "requested_by": requested_by,
            "scales": list(scale_names),
            "write_flags": {s.name: s.write_enabled is True for s in targets},
            "catalog_version": catalog_version,
            "plans": [p.to_dict() for p in plans],
            "warnings": [w.to_dict() for w in warnings],
            "fills": fills,
            "left_out": list(left_out),
            # A scale left out with writes off would not have been written anyway, so it is not a miss.
            "left_out_writes": {s.name: s.write_enabled is True for s in self._current_scales() if s.name in left_out},
        }
        self._catalog.save_preview(preview_id, payload, created_at, expires_at)
        return Preview(
            ok=True,
            preview_id=preview_id,
            created_at=created_at,
            expires_at=expires_at,
            requested_by=requested_by,
            scales=scale_names,
            differences=tuple(differences),
            warnings=tuple(warnings),
            errors=(),
            drift=tuple(drift),
            scales_read=tuple(scales_read),
            scales_read_repeated=tuple(name for name in scale_names if name in repeats),
            left_out=tuple(left_out),
        )

    def _deleted_held(self) -> list[dict]:
        """Deleted items a scale in this config may still hold. A scale taken out of the config cannot be
        finished, and no write reaches it, so it must not hold a PLU forever. It is read if it comes back."""
        names = {s.name for s in self._config.scales}
        return [r for r in self._catalog.deleted_still_held() if r["scale"] in names]

    def _plan_add(self, change, index, before_items, final, free_ids, touched, plans, warnings, errors) -> None:
        if change.expected_plu is not None:
            errors.append(PreviewError("invalid_change", "Only a delete takes expected_plu.", index, "expected_plu"))
            return
        if change.target_plu is not None or change.target_id is not None:
            errors.append(PreviewError("invalid_change", "An add has no target. It makes a new item.", index, "target_plu"))
            return
        missing = [n for n in ("plu", "description", "price") if getattr(change, n) is None]
        if missing:
            errors.append(PreviewError("invalid_change", f"An add needs {', '.join(missing)}.", index, missing[0]))
            return
        sold_by = change.sold_by if change.sold_by is not None else "weight"
        if sold_by not in SOLD_BY_FIELD_7:
            errors.append(PreviewError("invalid_field", "sold_by must be weight or piece.", index, "sold_by"))
            return
        replace_orphan = change.replace_orphan_id is not None
        if replace_orphan:
            item_id = change.replace_orphan_id
            if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id not in self._catalog.orphan_ids():
                errors.append(PreviewError("invalid_id", "That id is not a recorded orphan.", index, "replace_orphan_id"))
                return
            if item_id in before_items or item_id in touched:
                errors.append(PreviewError("invalid_id", "That id is already in use.", index, "replace_orphan_id"))
                return
        else:
            if not free_ids:
                recorded = sorted(self._catalog.orphan_ids())
                choice = f" Give replace_orphan_id with a recorded orphan, such as {recorded[0]}." if recorded else ""
                errors.append(PreviewError("no_free_id", f"No free ids are left from 1 to {MAX_ITEM_ID}.{choice}", index, "id"))
                return
            item_id = free_ids[0]
        price = self._normal_price(change.price)
        price_errors = self._validate_price(price, index) + self._validate_new_description(change.description, index)
        if price_errors:
            errors.extend(price_errors)
            return
        after = Item(
            id=item_id,
            code=change.code if change.code is not None else change.plu,
            plu=change.plu,
            description=change.description,
            price=price,
            fields_7_12=(SOLD_BY_FIELD_7[sold_by], *DEFAULT_FIELDS_7_12[1:]),
        )
        item_errors = self._validate_item(after, index)
        if item_errors:
            errors.extend(item_errors)
            return
        if replace_orphan:
            warnings.append(
                PreviewWarning(
                    "orphan_replaced", f"This add replaces what the scales hold at id {item_id}.", True, index
                )
            )
        else:
            free_ids.pop(0)
        final[item_id] = after
        touched.add(item_id)
        plans.append(_Plan(self._difference(index, "add", None, after), {}, replace_orphan))

    def _plan_edit(self, change, index, before_items, final, free_ids, touched, plans, warnings, errors) -> None:
        if change.expected_plu is not None:
            errors.append(PreviewError("invalid_change", "Only a delete takes expected_plu.", index, "expected_plu"))
            return
        before, problems = self._find_target(change, index, before_items)
        old_plus = self._catalog.old_plus_in_use()
        if problems and problems[0].code == "item_not_found" and change.target_plu in old_plus:
            # The same edit again, after it landed on one scale and not another. The catalog has moved.
            moved = before_items.get(old_plus[change.target_plu])
            if moved is not None and moved.plu == change.plu:
                before, problems = moved, []
        if problems:
            errors.extend(problems)
            return
        if before.id in touched:
            errors.append(PreviewError("duplicate_target", f"Id {before.id} is targeted twice in this preview.", index, None))
            return
        touched.add(before.id)
        if change.sold_by is not None or change.replace_orphan_id is not None:
            errors.append(PreviewError("invalid_change", "An edit can change only price, description, PLU and Code.", index, None))
            return
        requested: dict[str, str] = {}
        resulting_plu = change.plu if change.plu is not None else before.plu
        if is_reserved_plu(change.plu) or is_reserved_plu(change.code):
            errors.append(self._reserved_error(index, "plu"))
            return
        changing = (change.plu is not None and change.plu != before.plu) or (change.code is not None and change.code != before.code)
        # Asking for the PLU the catalog already has finishes a change a scale has not confirmed.
        finishing = change.plu == before.plu and before.id in old_plus.values()
        if changing or finishing:
            if change.code is not None and change.code != resulting_plu:
                errors.append(
                    PreviewError("code_plu_mismatch", f"Code {change.code} must equal the PLU {resulting_plu}.", index, "code")
                )
                return
            requested["plu"] = resulting_plu
            requested["code"] = resulting_plu
        if change.description is not None:
            description_errors = self._validate_new_description(change.description, index)
            if description_errors:
                errors.extend(description_errors)
                return
            requested["description"] = change.description
        if change.price is not None:
            price = self._normal_price(change.price)
            price_errors = self._validate_price(price, index)
            if price_errors:
                errors.extend(price_errors)
                return
            requested["price"] = price
        if not requested and change.plu is None and change.code is None:
            errors.append(PreviewError("invalid_change", "An edit needs at least one field to change.", index, None))
            return
        after = before.replace(**requested)
        item_errors = self._validate_item(after, index)
        if item_errors:
            errors.extend(item_errors)
            return
        final[before.id] = after
        difference = self._difference(index, "edit", before, after)
        plans.append(_Plan(difference, requested))
        if finishing:
            # It changes what a PLU does on a scale, however long ago someone agreed to it, so it needs a yes now.
            noted = [m for m in self._catalog.all_plu_moves() if m["item_id"] == before.id and m["old_plu"] != after.plu]
            where = "; ".join(f"PLU {m['old_plu']} on {m['scale']}" for m in noted)
            warnings.append(
                PreviewWarning(
                    "plu_change",
                    f"This finishes a PLU change that did not finish everywhere. These may still be working: {where}. "
                    f"Once this runs they stop working, and id {before.id} holds PLU {after.plu} on each scale it reaches.",
                    True,
                    index,
                )
            )
        elif before.plu != after.plu:
            warnings.append(
                PreviewWarning(
                    "plu_change",
                    f"PLU {before.plu} will stop working on the scales once this runs. "
                    f"Id {before.id} takes PLU {after.plu} instead, and Code follows it.",
                    True,
                    index,
                )
            )
        elif not difference.fields:
            warnings.append(
                PreviewWarning(
                    "no_change", f"The catalog already has these values for PLU {after.plu}.", False, index
                )
            )
        warning = self._price_warning(before, after, index)
        if warning is not None:
            warnings.append(warning)

    def _plan_delete(self, change, index, before_items, final, free_ids, touched, plans, warnings, errors) -> None:
        extra = [n for n in ("plu", "code", "description", "price", "sold_by", "replace_orphan_id") if getattr(change, n) is not None]
        if extra:
            errors.append(PreviewError("invalid_change", "A delete takes only a target.", index, extra[0]))
            return
        if not scale_module.DELETE_AVAILABLE:
            errors.append(PreviewError("delete_not_available", "Deletes are turned off in this version.", index, "action"))
            return
        before, problems = self._find_target(change, index, before_items)
        if problems and problems[0].code == "item_not_found":
            # Already gone from the list, but a scale may still hold it, so this is the next try there.
            before = self._catalog.deleted_item_still_held(plu=change.target_plu, item_id=change.target_id)
            problems = [] if before is not None else problems
        elif change.target_plu is not None and any(r["plu"] == change.target_plu for r in self._deleted_held()):
            # A PLU a scale may still hold as a deleted item can also be on a live one, in a catalog from before
            # that was refused. Asking by PLU could then hit the wrong product. The id says which.
            message = f"PLU {change.target_plu} names a live item, and a deleted one a scale may still hold. Give target_id to say which."
            problems = [PreviewError("duplicate_plu", message, index, "target_plu")]
        if problems:
            errors.extend(problems)
            return
        if change.expected_plu is not None and change.expected_plu != before.plu:
            # An old result can name an id that a new item has since taken.
            message = f"Id {before.id} now holds PLU {before.plu}, not PLU {change.expected_plu}. Nothing was changed."
            errors.append(PreviewError("target_changed", message, index, "expected_plu"))
            return
        if before.id in touched:
            errors.append(PreviewError("duplicate_target", f"Id {before.id} is targeted twice in this preview.", index, None))
            return
        touched.add(before.id)
        final.pop(before.id, None)
        placeholder = placeholder_record(before.id, "")
        warnings.append(
            PreviewWarning(
                "delete_placeholder",
                f"PLU {before.plu} will stop working on each scale. Id {before.id} gets a placeholder record, "
                f"named {placeholder.description}, with PLU {placeholder.plu}. The id can be used again once every scale has confirmed it.",
                True,
                index,
            )
        )
        plans.append(_Plan(self._difference(index, _DELETE, before, None), {}))

    @staticmethod
    def _duplicate_plus(
        final: dict[int, Item],
        before_items: dict[int, Item],
        differences: Sequence[Difference],
        old_plus: Mapping[str, int],
        deleted_held: Sequence[Mapping[str, Any]],
    ) -> list[PreviewError]:
        """No two items share a PLU, before or after the preview. Each scale is checked at apply too.

        A PLU a scale may still hold from an unfinished change is not free either, except for the item
        that had it. Nor is a deleted item's, until every scale that may hold it has confirmed.
        """
        errors = []
        for difference in differences:
            if difference.action == "delete" or difference.after is None:
                continue
            item = difference.after
            others = {i.id for i in final.values() if i.plu == item.plu and i.id != item.id}
            others |= {i.id for i in before_items.values() if i.plu == item.plu and i.id != item.id}
            if others:
                listed = ", ".join(str(i) for i in sorted(others))
                errors.append(PreviewError("duplicate_plu", f"PLU {item.plu} is already on id {listed}.", difference.index, "plu"))
            elif old_plus.get(item.plu, item.id) != item.id:
                message = f"PLU {item.plu} may still be on id {old_plus[item.plu]} of a scale, because a PLU change there has not finished."
                errors.append(PreviewError("duplicate_plu", message, difference.index, "plu"))
            else:
                holders = sorted(r["scale"] for r in deleted_held if r["plu"] == item.plu)
                if holders:
                    message = f"PLU {item.plu} was deleted, but {', '.join(holders)} may still sell it. Finish that delete first."
                    errors.append(PreviewError("duplicate_plu", message, difference.index, "plu"))
        return errors

    @staticmethod
    def _difference(index: int, action: str, before: Item | None, after: Item | None) -> Difference:
        fields: dict[str, FieldDiff] = {}
        names = (*[n for n in _DIFF_FIELDS if n != "timestamp"], *_DIFF_GROUPS)
        for name in names:
            old = _text_of(before, name) if before else None
            new = _text_of(after, name) if after else None
            if old != new:
                fields[name] = FieldDiff(old, new)
        shown = after or before
        return Difference(
            index=index,
            action=action,
            item_id=shown.id if shown else None,
            plu=shown.plu if shown else None,
            fields=fields,
            before=before,
            after=after,
        )

    @staticmethod
    def _price_warning(before: Item, after: Item, index: int) -> PreviewWarning | None:
        if before.price == after.price:
            return None
        message = f"PLU {after.plu}: price goes from {before.price} to {after.price}, more than 50 percent."
        try:
            old = Decimal(before.price)
            new = Decimal(after.price)
        except InvalidOperation:
            return PreviewWarning("price_change_large", message, True, index)
        if old <= 0 or abs(new - old) * 2 > old:
            return PreviewWarning("price_change_large", message, True, index)
        return None

    def _read_for_preview(
        self,
        targets: Sequence[ScaleConfig],
        plans: Sequence[_Plan],
        warnings: list[PreviewWarning],
        repeats: set[str] | None = None,
        *,
        avoid_frozen: bool = False,
    ) -> tuple[list[Drift], list[str], dict[str, list[int]]]:
        drift: list[Drift] = []
        read_names: list[str] = []
        fills: dict[str, list[int]] = {}
        writes = [p for p in plans if p.difference.action in _WRITE_ACTIONS]
        if not writes:
            return drift, read_names, fills
        with _apply_lock(self._lock_path):
            # Under the lock, so a freeze another process logged a moment ago is seen.
            frozen = set(self.scales_may_be_frozen()) if avoid_frozen else set()
            for scale in targets:
                if scale.write_enabled is not True and scale.read_enabled is not True:
                    continue
                if scale.name in frozen:
                    warnings.append(PreviewWarning("scale_may_be_frozen", _frozen_skip_words(scale.name), may_be_frozen=True))
                    continue
                client = self._scale_factory(scale, self._settings, sleep=self._sleep)
                outcome = self._identify_and_read(scale, client, writes, repeats, what="preview")
                if isinstance(outcome, ScaleResult):
                    warnings.append(PreviewWarning("scale_read_failed", outcome.message, may_be_frozen=outcome.may_be_frozen))
                    continue
                read_names.append(scale.name)
                held = _real_records(outcome)
                drift.extend(self._compare(scale.name, writes, held))
                drift.extend(self._lost_delete_drift(scale.name, writes, outcome))
                fill, problems = self._table_problems(scale.name, outcome, held, writes)
                drift.extend(problems)
                if fill and not problems:
                    fills[scale.name] = fill
                    warnings.append(self._fill_warning(scale.name, fill))
                for plan in writes:
                    difference = plan.difference
                    if difference.action == "edit" and difference.item_id not in held and not problems:
                        warnings.append(
                            PreviewWarning(
                                "missing_on_scale",
                                f"{scale.name} holds nothing at id {difference.item_id}, so this goes there as a new item.",
                                False,
                                difference.index,
                            )
                        )
                    current = held.get(difference.item_id)
                    if plan.moves_plu and current is not None and current.plu != difference.plu:
                        if current.plu in self._catalog.plu_moves(difference.item_id, scale.name):
                            warnings.append(
                                PreviewWarning(
                                    "plu_change_lagging",
                                    f"{scale.name} still holds PLU {_quoted(current.plu)} at id {difference.item_id}, read just now. This edit finishes the change there.",
                                    False,
                                    difference.index,
                                )
                            )
        return drift, read_names, fills

    # ------------------------------------------------------------ the check before a write

    @staticmethod
    def _ids_to_fill(table: Mapping[int, Item], writes: Sequence[_Plan]) -> list[int]:
        """A gap in the ids may be what freezes a read, so no write may leave one. An id above the last
        filled one plus one needs the ids between filled in the same send."""
        planned = {p.difference.item_id for p in writes}
        return [i for i in range(max(table, default=0) + 1, max(planned)) if i not in planned]

    def _logged_action(self, change_id: str, item_id: int) -> str | None:
        """What the change that last touched this id did to it."""
        for event in self._catalog.read_log(change_id):
            if event["event"] == EVENT_STARTED:
                for change in event["payload"].get("changes", []):
                    if change.get("item_id") == item_id:
                        return change.get("action")
        return None

    def _expected_ids(self, name: str) -> set[int]:
        """Ids a read of this scale should reach: what the catalog lists, and deleted items it may still hold.

        An id counts unless the catalog has positive evidence it never went to this scale. A read ending
        below an id that counts cannot be trusted. A read ending below one that does not is just a scale
        that missed an add. The evidence is one of these:

        - The belief is absent: an add that was not delivered here, or that left this scale out.
        - The last change here was an add, it was not accepted, and no orphan is noted here. A lost reply may simply not have landed.
        - No row here, and the item came through an add, not the seed. Every send leaves a row for each
          scale configured then, so this scale joined the config later.

        A seeded item, or anything a scale has been sent or shown holding, always counts.
        """
        rows = self._catalog.scale_state_rows(name)
        live = {i.id for i in self._catalog.list_items()}
        added = self._catalog.ids_added_since_seed()
        orphans = self._catalog.orphan_ids()
        expected: set[int] = set()
        for item in self._catalog.list_items(include_deleted=True):
            row = rows.get(item.id)
            if item.id not in live:
                if row is not None and row["belief"] in ("present", "unknown"):
                    expected.add(item.id)
            elif row is None:
                if item.id not in added:
                    expected.add(item.id)
            elif row["belief"] == "present":
                expected.add(item.id)
            elif row["belief"] == "unknown":
                accepted = row["status"] in (ScaleStatus.VERIFIED.value, ScaleStatus.ACCEPTED_NOT_VERIFIED.value)
                add_missed = not accepted and name not in orphans.get(item.id, []) and self._logged_action(row["change_id"], item.id) == "add"
                if not add_missed:
                    expected.add(item.id)
        return expected

    def _read_ended_early(self, name: str, table: Mapping[int, Item]) -> list[int]:
        """Ids the scale should hold past where a whole read ended. If any, that read says nothing about the scale's end."""
        last = max(table, default=0)
        return sorted(i for i in self._expected_ids(name) if i > last)

    def _table_problems(
        self, name: str, table: Mapping[int, Item], held: Mapping[int, Item], writes: Sequence[_Plan]
    ) -> tuple[list[int], list[Drift]]:
        """The ids this write would have to fill on this scale, and any reason it must not go at all.

        Filling trusts the end of the read, and a read that ended early would turn real items into
        placeholders. So a read ending below an id the scale should hold refuses the scale, and a fill
        never goes over an id the catalog lists or a deleted item the scale may hold. No token passes these.
        """
        last = max(table, default=0)
        beyond = self._read_ended_early(name, table)
        if beyond:
            words = (
                f"{name} differs from the list. Its read ended at id {last}, and the catalog expects records up to id {beyond[-1]}. "
                "Nothing was sent. Reload it from the command line, or have a person look."
            )
            return [], [Drift(name, "read_ended_early", None, None, {}, None, words)]
        fill = self._ids_to_fill(table, writes)
        listed = {i.id for i in self._catalog.list_items()} | {r["item_id"] for r in self._catalog.deleted_still_held()}
        blocked = [i for i in fill if i in listed]
        if blocked:
            words = (
                f"{name} differs from the list. This would put placeholders at id {_id_ranges(blocked)}, and the catalog lists "
                "those ids or a scale may still hold them. Nothing was sent. Settle those items to this scale in id order, reload it from the command line, or have a person look."
            )
            return [], [Drift(name, "fill_blocked", None, None, {}, None, words)]
        return fill, self._placeholder_clashes(name, fill, held)

    @staticmethod
    def _fill_warning(name: str, fill: Sequence[int]) -> PreviewWarning:
        words = f"{name} holds nothing at id {_id_ranges(fill)}. To leave no gap, those ids get placeholders in the same send."
        return PreviewWarning("gap_filled", words, True)

    def _lost_delete_drift(self, name: str, writes: Sequence[_Plan], table: Mapping[int, Item]) -> list[Drift]:
        """A placeholder where an earlier delete has no confirmed result: it may have landed, and writing brings the item back."""
        found: list[Drift] = []
        rows = self._catalog.scale_state_rows(name)
        for plan in writes:
            difference = plan.difference
            record = table.get(difference.item_id)
            row = rows.get(difference.item_id)
            if difference.action in (_DELETE, "add") or record is None or row is None or not is_placeholder(record):
                continue
            if row["status"] in (ScaleStatus.UNKNOWN.value, ScaleStatus.MISMATCH.value) and self._logged_action(row["change_id"], difference.item_id) == _DELETE:
                words = (
                    f"{name} holds a placeholder at id {difference.item_id}. An earlier delete of this item has no confirmed result, so "
                    "it may have landed. This write would bring the item back. Acknowledge that only if it should come back. Otherwise repeat the delete."
                )
                found.append(Drift(name, "delete_may_have_landed", difference.item_id, difference.plu, {}, record, words, _drift_token(name, difference.item_id, record)))
        return found

    def _identify_and_read(self, scale: ScaleConfig, client: Any, writes: Sequence[_Plan], repeats: set[str] | None = None, what: str = "apply") -> Any:
        """Steps 1 and 2. Returns what the scale holds, by id, or a ScaleResult saying why not.

        It reads the whole table once, because any write must find its PLU under any other id, and
        must know where the table ends. The table comes back with placeholders in it. Use
        _real_records to judge what is on it. A read that ends may_be_frozen is noted, see _note_no_reply.
        """
        name = scale.name
        differences = [p.difference for p in writes]
        identity = client.identify()
        if not identity.ok:
            result = self._uniform_result(name, identity.status, identity.message, differences)
            self._note_no_reply(name, what, identity)
            return replace(result, may_be_frozen=identity.may_be_frozen)
        read = client.read_items(None)
        _note_repeat(read, name, repeats)
        if not read.ok:
            status = read.status or ScaleStatus.READ_FAILED
            message = f"{read.message} Nothing was written to this scale."
            self._note_no_reply(name, what, read)
            return replace(self._uniform_result(name, status, message, differences), may_be_frozen=read.may_be_frozen)
        self._save_reads(name, read)
        return read.items

    def _save_reads(self, name: str, read: Any) -> None:
        """Includes ids the read went past that hold nothing, so an old last read does not linger.

        A whole read vouches for the ids past its end only if it reached every id the scale should
        hold. Otherwise it vouches only for what it went past, so it frees no deleted id above that.
        """
        # The scale answered, even if the table is empty and nothing below is kept. That ends a freeze flag.
        self._forget_unnoted(name)
        try:
            self._catalog.note_read_answered(name, self._now_text())
        except Exception:
            pass  # the flag stays, which is the safe way to be wrong
        try:
            last = max(read.items, default=0)
            trusted = read.complete and not self._read_ended_early(name, read.items)
            covered = MAX_ITEM_ID if trusted else last
            self._catalog.record_scale_reads(name, list(_real_records(read.items).values()), self._now_text(), covered_up_to=covered)
            if trusted:
                self._catalog.settle_plu_moves_from_read(name, {r.plu for r in _real_records(read.items).values()})
        except Exception:
            # A note of what was read must not stop the check it was read for.
            pass

    def _compare(self, name: str, writes: Sequence[_Plan], held: Mapping[int, Item]) -> list[Drift]:
        """Step 3. What this scale holds against the catalog, for each planned record.

        Only a field difference on the same product carries an ack_token. A different product
        at the id, or a PLU the scale holds under another id, refuses this scale outright.
        held comes without placeholders, so one is the same as an empty slot here.
        """
        drift: list[Drift] = []
        orphans = self._catalog.orphan_ids()
        for plan in writes:
            difference = plan.difference
            item_id = difference.item_id
            current = held.get(item_id)
            is_orphan = plan.replace_orphan and name in orphans.get(item_id, [])
            same_plu = sorted(i.id for i in held.values() if i.plu == difference.plu and i.id != item_id)
            taken = Drift(
                name,
                "plu_on_other_id",
                item_id,
                difference.plu,
                {},
                held[same_plu[0]] if same_plu else None,
                f"{name} already holds PLU {difference.plu} at id {same_plu[0] if same_plu else ''}. This cannot be acknowledged.",
            )
            if difference.action == "add":
                if current is not None and not is_orphan:
                    drift.append(
                        Drift(
                            name,
                            "id_occupied",
                            item_id,
                            difference.plu,
                            {},
                            current,
                            f"{name} already holds PLU {_quoted(current.plu)} at id {item_id}, and it is not a recorded orphan. "
                            "If it should go, record it with mark_orphans_from_read, then add with replace_orphan_id.",
                        )
                    )
                if same_plu:
                    drift.append(taken)
                continue
            before = difference.before
            if difference.action == _DELETE:
                drift.extend(self._placeholder_clashes(name, [item_id], held))
            # Rule 7: the PLU on another id of this scale refuses any write. The other id would keep
            # selling it at its own price, and a delete or PLU change could never be verified.
            if same_plu:
                drift.append(taken)
            if plan.moves_plu or difference.action == _DELETE:
                for old in sorted({before.plu, current.plu if current else before.plu} - {difference.plu}):
                    elsewhere = sorted(i.id for i in held.values() if i.plu == old and i.id != item_id)
                    if elsewhere:
                        words = f"{name} still holds the old PLU {_quoted(old)} at id {elsewhere[0]}. This cannot be acknowledged."
                        drift.append(Drift(name, "plu_on_other_id", item_id, old, {}, held[elsewhere[0]], words))
            if current is None or is_orphan:
                continue
            expected = self._expected_record(name, plan, current)
            if expected is None:
                found = Drift(
                    name,
                    "different_product",
                    item_id,
                    before.plu,
                    _field_differences(before, current),
                    current,
                    _other_product_message(name, item_id, before, current),
                )
                drift.append(self._with_move_hint(found))
                continue
            if current.to_fields() == expected.to_fields():
                continue
            fields = _field_differences(expected, current)
            listed = ", ".join(f"{k} is {v.after!r} there and {v.before!r} in the catalog" for k, v in fields.items())
            drift.append(
                Drift(
                    name,
                    "differs",
                    item_id,
                    difference.plu,
                    fields,
                    current,
                    f"{name} differs at id {item_id}: {listed}.",
                    _drift_token(name, item_id, current),
                )
            )
        return drift

    def _expected_record(self, name: str, plan: _Plan, current: Item) -> Item | None:
        """What this scale's record at the id should look like for this plan. None means it is another product.

        A PLU change can land on one scale and not another, so a retry can meet either end of it. The
        new PLU means the record already landed, and the time it was sent there is not known here. A
        PLU this scale was recorded as not having moved off yet is the record from before the change.
        Only an edit that asks for the PLU, or a delete, gets this. A price must not land on either.
        """
        difference = plan.difference
        if not _is_other_product(difference.before, current):
            return difference.before
        if plan.moves_plu and not _is_other_product(difference.after, current):
            # The new PLU with another name is not this edit having landed. It may be another product.
            if current.description in (difference.before.description, difference.after.description):
                return difference.after.replace(timestamp=current.timestamp)
            return None
        if plan.moves_plu or difference.action == _DELETE:
            old = self._catalog.plu_moves(difference.item_id, name).get(current.plu)
            if old is not None and old.code == current.code:
                return old
        return None

    def _with_move_hint(self, drift: Drift) -> Drift:
        """Settling cannot finish a PLU change, so say what can."""
        if drift.kind != "different_product" or drift.held is None or drift.item_id is None:
            return drift
        old = self._catalog.plu_moves(drift.item_id, drift.scale).get(drift.held.plu)
        if old is None or old.code != drift.held.code:
            return drift
        return replace(drift, message=unfinished_plu_message(drift.scale, drift.held.plu, drift.item_id), unfinished_plu_change=True)

    @staticmethod
    def _placeholder_clashes(name: str, ids: Sequence[int], held: Mapping[int, Item]) -> list[Drift]:
        """Rule 7 covers placeholders too: an old record may use the PLU one of these would take."""
        found: list[Drift] = []
        by_plu = {record.plu: record for record in held.values()}
        for item_id in ids:
            plu = placeholder_record(item_id, "").plu
            other = by_plu.get(plu)
            if other is not None and other.id != item_id:
                message = f"{name} holds PLU {plu} at id {other.id}, and that PLU is the placeholder for id {item_id}. This cannot be acknowledged."
                found.append(Drift(name, "plu_on_other_id", item_id, plu, {}, other, message))
        return found

    def _build_record(self, plan: _Plan, held: Mapping[int, Item], stamp: str) -> Item:
        """Step 4. The scale's own record, with only the asked for fields and a new time."""
        difference = plan.difference
        if difference.action == _DELETE:
            return placeholder_record(difference.item_id, stamp)
        if difference.action == "add":
            return difference.after.replace(timestamp=stamp)
        if difference.action == _SETTLE_PUSH:
            # The catalog's record exactly, with its own time. A new time would make this scale
            # differ from the catalog, and from every other scale, by the timestamp alone.
            return difference.before
        base = held.get(difference.item_id) or difference.before
        # after keeps the catalog's own time when this only finishes a PLU change. See _stamped.
        return base.replace(**plan.requested, timestamp=difference.after.timestamp or stamp)

    # ------------------------------------------------------------ apply

    def apply_change(
        self,
        preview_id: str,
        acknowledge_warnings: bool = False,
        *,
        acknowledge_drift: Sequence[str] = (),
        requested_by: str | None = None,
        avoid_frozen: bool = False,
        leave_out_frozen: bool = False,
    ) -> ChangeRecord:
        """avoid_frozen refuses with ScaleMayBeFrozen, under the apply lock and before anything is sent,
        if any scale this preview names may be frozen. The web page asks for it.

        leave_out_frozen, for the MCP server and the command line, checks the same list under the lock,
        and leaves out any scale that may be frozen now. Nothing is read or sent there, and it gets the
        result a preview's left out scale gets. It refuses only if no scale it would contact is left."""
        with _apply_lock(self._lock_path):
            tokens = frozenset(t for t in acknowledge_drift if isinstance(t, str))
            return self._apply_locked(
                preview_id, acknowledge_warnings is True, tokens, requested_by, avoid_frozen is True, leave_out_frozen is True
            )

    def _apply_locked(
        self,
        preview_id: str,
        acknowledged: bool,
        tokens: frozenset[str],
        requested_by: str | None,
        avoid_frozen: bool = False,
        leave_out_frozen: bool = False,
    ) -> ChangeRecord:
        restart = self.config_changed()
        if restart:
            raise ConfigChanged(restart)
        stored = self._catalog.load_preview(preview_id)
        if stored is None:
            raise PreviewNotFound(f"No preview with id {preview_id}.")
        if stored["consumed_by"] is not None:
            raise PreviewAlreadyApplied("This preview was already applied. Make a new one.")
        if self._clock() >= datetime.fromisoformat(stored["expires_at"]):
            raise PreviewExpired("This preview has expired. Make a new one.")
        payload = stored["payload"]
        if self._catalog.version() != payload["catalog_version"]:
            raise PreviewStale("The item list has changed since this preview. Make a new one.")

        plans = [_Plan.from_dict(p) for p in payload["plans"]]
        by_name = {s.name: s for s in self._current_scales()}
        names = payload["scales"]
        missing = [n for n in names if n not in by_name]
        if missing:
            raise ValidationFailed(f"This preview names scales no longer in the config: {', '.join(missing)}.")
        flags_then = payload.get("write_flags", {})
        changed = [n for n in names if flags_then.get(n) is not (by_name[n].write_enabled is True)]
        if changed:
            raise PreviewStale(f"Write settings changed for {', '.join(changed)} since this preview. Make a new one.")

        warnings = [PreviewWarning.from_dict(w) for w in payload["warnings"]]
        needing_ack = [w for w in warnings if w.requires_ack]
        if needing_ack and not acknowledged:
            raise AcknowledgementRequired(
                "This change has warnings to acknowledge: " + " ".join(w.message for w in needing_ack)
            )
        self._recheck(plans)

        ids = {p.difference.item_id for p in plans}
        for change_id, touched in self._interrupted(lock_held=True).items():
            if touched & ids:
                raise InterruptedChange(
                    f"Change {change_id} touched these items and never finished. Run compare_scales, then resolve it."
                )
        late: list[str] = []
        if avoid_frozen or leave_out_frozen:
            # Under the lock, so a freeze another process logged since the preview is seen before anything goes out.
            # Only a scale this apply would contact counts: writes on, or reads on for a settle that takes the
            # scale's record. One with both off gets skipped_not_write_enabled and is never contacted, so it
            # must not stop the others. Turning a flagged scale's reads and writes off is the way out.
            takes_scale = any(p.difference.action == _SETTLE_ACCEPT for p in plans)
            contacted = {
                n for n in names
                if by_name[n].write_enabled is True or (takes_scale and by_name[n].read_enabled is True)
            }
            frozen_now = [n for n in self.scales_may_be_frozen() if n in contacted]
            if frozen_now and (avoid_frozen or not contacted - set(frozen_now)):
                raise ScaleMayBeFrozen(
                    f"{', '.join(frozen_now)} gave no reply after this preview was made, so nothing was read or sent. Make a new preview.",
                    frozen_now,
                )
            if frozen_now:
                # The MCP server and the command line leave it out, as a preview does, and the change goes to the others.
                late = frozen_now
                names = [n for n in names if n not in late]

        targets = [by_name[n] for n in self._config_order(names)]
        actor = (requested_by or payload.get("requested_by") or "unknown").strip() or "unknown"
        writes = [p for p in plans if p.difference.action in _WRITE_ACTIONS]
        # Scales the preview left out because they may be frozen, and any found so under the lock just now. Nothing
        # goes to them. Each gets a result that says so, which leaves it on the unfinished list as missing this change.
        left_out = [n for n in self._config_order([*(payload.get("left_out") or []), *late]) if n not in names]

        backup_notes = self._backup_first_writes(targets, writes, preview_id, actor)

        change_id = uuid.uuid4().hex

        # One timestamp per apply, so every scale gets the same time.
        stamp = self._clock().strftime("%Y-%m-%d %H:%M:%S")
        stamped = [self._stamped(p, stamp) for p in plans]
        not_touched = tuple(s.name for s in self._config.scales if s.name not in names and s.name not in left_out)
        started_at = self._now_text()
        try:
            # One transaction, so a client looking up this preview finds either nothing sent yet or a running change.
            started = self._catalog.start_change(
                preview_id,
                change_id,
                EVENT_STARTED,
                {
                    "preview_id": preview_id,
                    "requested_by": actor,
                    "acknowledged_warnings": acknowledged,
                    "acknowledged_drift": sorted(tokens),
                    "started_at": started_at,
                    "scales": list(names),
                    "not_touched": list(not_touched),
                    "left_out": list(left_out),
                    "config_path": str(self._config.path) if self._config.path else None,
                    "backups": backup_notes,
                    "changes": [p.difference.to_dict() for p in stamped],
                },
                started_at,
            )
        except Exception as exc:
            raise LogFailed(f"The change log could not be written ({exc.__class__.__name__}), so nothing was sent.") from exc
        if not started:
            raise PreviewAlreadyApplied("This preview was already applied. Make a new one.")

        problems: list[str] = []
        results: list[ScaleResult] = []
        sent_records: dict[str, dict[int, Item]] = {}
        writes_then = payload.get("left_out_writes") or {}
        for name in left_out:
            # Logged first, so a stop part way through cannot lose that this scale missed the change.
            if writes_then.get(name, True) is False:
                words = f"{name}: writes are off, and it gave no reply earlier, so it was not contacted. Nothing was sent."
                result = self._uniform_result(name, ScaleStatus.SKIPPED_NOT_WRITE_ENABLED, words, [p.difference for p in stamped])
            else:
                result = self._left_out_result(name, stamped)
            results.append(result)
            sent_records[name] = {}
            try:
                self._catalog.append_log(change_id, EVENT_SCALE_RESULT, result.to_dict(), self._now_text())
            except Exception as exc:
                problems.append(f"The result for {name} could not be logged ({exc.__class__.__name__}).")
        for scale in targets:
            repeats: set[str] = set()
            result, sent = self._apply_to_scale(scale, stamped, stamp, tokens, payload.get("fills", {}).get(scale.name, ()), repeats)
            if scale.name in repeats:
                result = replace(result, read_repeated=True)
            results.append(result)
            sent_records[scale.name] = sent
            # Logged at once, so a crash on a later scale cannot lose this reply.
            try:
                self._catalog.append_log(change_id, EVENT_SCALE_RESULT, result.to_dict(), self._now_text())
            except Exception as exc:
                problems.append(f"The result for {scale.name} could not be logged ({exc.__class__.__name__}).")
        order = [s.name for s in self._config.scales]
        results.sort(key=lambda r: order.index(r.scale) if r.scale in order else len(order))

        problems.extend(self._commit(stamped, results, sent_records, change_id))
        still_differ = self._still_differ(stamped, results, sent_records)
        outcome, summary, display = self._outcome(results, not_touched, problems, still_differ)
        finished_at = self._now_text()
        verified = bool(results) and all(r.status == ScaleStatus.VERIFIED.value for r in results) and all(
            i.status == ScaleStatus.VERIFIED.value for r in results for i in r.items
        )
        try:
            self._catalog.append_log(
                change_id,
                EVENT_FINISHED,
                {
                    "finished_at": finished_at,
                    "outcome": outcome,
                    "summary": summary,
                    "display": display,
                    "verified": verified,
                    "still_differ": [d.to_dict() for d in still_differ],
                },
                finished_at,
            )
        except Exception as exc:
            outcome = "catalog_out_of_sync"
            summary = f"{summary} The end of this change could not be logged ({exc.__class__.__name__})."
            display = summary
        return ChangeRecord(
            change_id=change_id,
            preview_id=preview_id,
            requested_by=actor,
            acknowledged_warnings=acknowledged,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            success=outcome == "success",
            changes=tuple(p.difference for p in stamped),
            scale_results=tuple(results),
            summary=summary,
            verified=verified,
            display=display,
            not_touched=not_touched,
            still_differ=still_differ,
        )

    def _config_order(self, names: Sequence[str]) -> list[str]:
        return [s.name for s in self._config.scales if s.name in names]

    def _recheck(self, plans: Sequence[_Plan]) -> None:
        """Every record and id is checked again right before anything can go out."""
        errors: list[PreviewError] = []
        seen: dict[str, int] = {}
        free = set(self._catalog.free_ids(MAX_ITEM_ID))
        orphans = self._catalog.orphan_ids()
        for plan in plans:
            difference = plan.difference
            if difference.action == _DELETE:
                continue
            after = difference.after
            if after is None:
                errors.append(PreviewError("invalid_change", "A record is missing.", difference.index, None))
                continue
            errors.extend(self._validate_item(after, difference.index))
            if after.plu in seen and seen[after.plu] != after.id:
                errors.append(PreviewError("duplicate_plu", f"PLU {after.plu} appears twice.", difference.index, "plu"))
            seen[after.plu] = after.id
            if difference.action == "add":
                allowed = after.id in orphans if plan.replace_orphan else after.id in free
                if not allowed:
                    raise PreviewStale(f"Id {after.id} is no longer free for this add. Make a new preview.")
        if errors:
            raise ValidationFailed("A record in this preview is no longer valid.", errors=errors)

    @staticmethod
    def _stamped(plan: _Plan, stamp: str) -> _Plan:
        difference = plan.difference
        if difference.after is None or difference.action in (_SETTLE_ACCEPT, _SETTLE_PUSH):
            return plan
        if plan.moves_plu and not difference.fields:
            # This only finishes a PLU change. The catalog's own time goes with it, so a scale this
            # send leaves out does not then differ from the catalog by the timestamp alone.
            return plan
        return replace(plan, difference=replace(difference, after=difference.after.replace(timestamp=stamp)))

    # ------------------------------------------------------------ backup

    def _marker(self, kind: str, scale_name: str) -> Path:
        return Path(self._settings.backup_dir) / f"first-write-{kind}-{scale_name}.marker"

    def _read_marker(self, kind: str, scale_name: str) -> dict | None:
        """A marker counts only if it is this version, for this catalog, and what it names is there."""
        path = self._marker(kind, scale_name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != _BACKUP_MARKER_VERSION:
            return None
        if data.get("catalog_epoch") != self._catalog.epoch():
            return None
        where = data.get("where")
        if where is not None and not Path(where).exists():
            return None
        return data

    def _write_marker(self, kind: str, scale_name: str, where: Any, note: str) -> None:
        Path(self._settings.backup_dir).mkdir(parents=True, exist_ok=True)
        data = {
            "version": _BACKUP_MARKER_VERSION,
            "catalog_epoch": self._catalog.epoch(),
            "at": self._now_text(),
            "where": str(where) if isinstance(where, (str, os.PathLike)) else None,
            "note": note,
        }
        self._marker(kind, scale_name).write_text(json.dumps(data), encoding="utf-8")

    def _scalelink_note(self) -> str:
        settings = self._settings
        if settings.scalelink_db_path is not None:
            return "scalelink_db_copied"
        if settings.scalelink_db_copy == SCALELINK_NOT_AVAILABLE:
            return SCALELINK_NOT_AVAILABLE
        raise OSError("Set scalelink_db_path, or set scalelink_db_copy to not_available_here.")

    def _backup_first_writes(
        self, targets: Sequence[ScaleConfig], writes: Sequence[Any], preview_id: str, actor: str
    ) -> dict[str, str]:
        """Rule 5, the item list. Runs before anything is sent. A failure stops the whole apply.

        A delete counts as a write. The dump of each scale comes later, in _save_scale_dump,
        once its identity is confirmed.
        """
        notes: dict[str, str] = {}
        if not writes:
            return notes
        backup_dir = Path(self._settings.backup_dir)
        for scale in targets:
            if scale.write_enabled is not True:
                continue
            existing = self._read_marker("items", scale.name)
            if existing is not None:
                notes[scale.name] = existing.get("note", "")
                continue
            try:
                note = "custom_hook" if self._backup_hook is not None else self._scalelink_note()
                items = self._catalog.list_items()
                if self._backup_hook is not None:
                    where = self._backup_hook(scale.name, items, backup_dir)
                else:
                    where = default_backup_hook(
                        scale.name, items, backup_dir, scalelink_db_path=self._settings.scalelink_db_path, when=self._clock()
                    )
                self._write_marker("items", scale.name, where, note)
                notes[scale.name] = note
            except Exception as exc:
                detail = exc.args[0] if isinstance(exc, OSError) and len(exc.args) == 1 else exc.__class__.__name__
                message = f"The backup for {scale.name} failed ({detail}), so nothing was sent."
                try:
                    self._catalog.append_log(
                        uuid.uuid4().hex,
                        EVENT_BACKUP_FAILED,
                        {"preview_id": preview_id, "requested_by": actor, "scale": scale.name, "message": message},
                        self._now_text(),
                    )
                except Exception:
                    pass
                raise BackupFailed(message) from exc
        return notes

    def _save_scale_dump(self, name: str, held: Mapping[int, Item]) -> None:
        """Rule 5, what the scale holds. LP records only. Saved once, before the first write."""
        folder = Path(self._settings.backup_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = self._clock().strftime("%Y%m%d-%H%M%S")
        path = folder / f"{stamp}-{name}-{uuid.uuid4().hex[:6]}-scale-dump.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([held[i].to_dict() for i in sorted(held)], handle, indent=1)
        self._write_marker("dump", name, path, "lp_records")

    # ------------------------------------------------------------ sending

    def _apply_to_scale(
        self, scale_then: ScaleConfig, plans: Sequence[_Plan], stamp: str, tokens: frozenset[str], seen_fills: Sequence[int] = (), repeats: set[str] | None = None
    ) -> tuple[ScaleResult, dict[int, Item]]:
        """The seven steps for one scale. A problem here never stops the next scale."""
        name = scale_then.name
        differences = [p.difference for p in plans]
        scale = self._current_scale(name) or replace(scale_then, write_enabled=False, read_enabled=False)
        if any(p.difference.action == _SETTLE_ACCEPT for p in plans):
            try:
                return self._accept_from_scale(scale, plans[0], tokens, repeats)
            except (NotWriteEnabled, NotReadEnabled):
                message = f"{name}: contact is not enabled for this scale. Nothing changed."
                return self._uniform_result(name, ScaleStatus.SKIPPED_NOT_WRITE_ENABLED, message, differences), {}
            except Exception as exc:
                message = f"{name}: an unexpected problem ({exc.__class__.__name__}). Nothing changed."
                return self._uniform_result(name, ScaleStatus.READ_FAILED, message, differences), {}
        if scale.write_enabled is not True:
            return (
                self._uniform_result(
                    name,
                    ScaleStatus.SKIPPED_NOT_WRITE_ENABLED,
                    f"{name}: writes are off for this scale. Nothing was sent.",
                    differences,
                ),
                {},
            )
        try:
            return self._apply_steps(scale, plans, stamp, tokens, seen_fills, repeats)
        except (NotWriteEnabled, NotReadEnabled):
            return (
                self._uniform_result(
                    name,
                    ScaleStatus.SKIPPED_NOT_WRITE_ENABLED,
                    f"{name}: writes are off for this scale. Nothing was sent.",
                    differences,
                ),
                {},
            )
        except ProtocolError as exc:
            # Raised while building, before any socket for the write opens.
            return (
                self._uniform_result(
                    name, ScaleStatus.DRIFT_REFUSED, f"{name}: refused here, before sending. {exc.message}", differences
                ),
                {},
            )
        except Exception as exc:
            return (
                self._uniform_result(
                    name,
                    ScaleStatus.UNKNOWN,
                    f"{name}: an unexpected problem ({exc.__class__.__name__}). The result is unknown. Check the scale.",
                    differences,
                ),
                {},
            )

    def _accept_from_scale(self, scale: ScaleConfig, plan: _Plan, tokens: frozenset[str], repeats: set[str] | None = None) -> tuple[ScaleResult, dict[int, Item]]:
        """Settling toward the scale. Identity and read as usual, and nothing is written to it."""
        name = scale.name
        difference = plan.difference
        client = self._scale_factory(scale, self._settings, sleep=self._sleep)
        held = self._identify_and_read(scale, client, [plan], repeats)
        if isinstance(held, ScaleResult):
            return held, {}
        current = _real_records(held).get(difference.item_id)
        wanted = difference.after
        final: dict[int, tuple[str, str]] = {}
        if current is None or current.to_fields() != wanted.to_fields():
            message = f"{name}: this scale's record changed since the preview, so the catalog was not changed."
            found = (self._settle_finding(name, difference.before, current),)
            final[difference.item_id] = (ScaleStatus.DRIFT_REFUSED.value, message)
            return self._result(name, [plan], final, message, 0, (), found, ()), {}
        drift = (self._settle_finding(name, difference.before, current),)
        token = _drift_token(name, difference.item_id, current)
        if token not in tokens:
            message = f"{name}: taking this scale's record was not acknowledged, so the catalog was not changed."
            final[difference.item_id] = (ScaleStatus.DRIFT_REFUSED.value, message)
            return self._result(name, [plan], final, message, 0, (), drift, ()), {}
        message = f"{name}: read just now. The catalog takes this scale's record. Nothing was written to the scale."
        final[difference.item_id] = (ScaleStatus.VERIFIED.value, message)
        return self._result(name, [plan], final, message, 0, (), drift, ()), {difference.item_id: current}

    @staticmethod
    def _settle_finding(name: str, wanted: Item, current: Item | None) -> Drift:
        """What settling works on. Unlike a write's compare, this always names a token, because a
        person settling is the one thing that may resolve a different product."""
        item_id = wanted.id
        token = _drift_token(name, item_id, current)
        if current is None:
            message = f"{name} holds nothing at id {item_id}, and the catalog lists PLU {wanted.plu} there."
            return Drift(name, "missing", item_id, wanted.plu, {}, None, message, token)
        fields = _field_differences(wanted, current)
        if _is_other_product(wanted, current):
            message = _other_product_message(name, item_id, wanted, current).replace(" This cannot be acknowledged. Settle it first.", "")
            return Drift(name, "different_product", item_id, wanted.plu, fields, current, message, token)
        listed = ", ".join(f"{k} is {v.after!r} there and {v.before!r} in the catalog" for k, v in fields.items())
        return Drift(name, "differs", item_id, wanted.plu, fields, current, f"{name} differs at id {item_id}: {listed}.", token)

    def _apply_steps(
        self, scale: ScaleConfig, plans: Sequence[_Plan], stamp: str, tokens: frozenset[str], seen_fills: Sequence[int] = (), repeats: set[str] | None = None
    ) -> tuple[ScaleResult, dict[int, Item]]:
        name = scale.name
        writes = [p for p in plans if p.difference.action in _WRITE_ACTIONS]
        client = self._scale_factory(scale, self._settings, sleep=self._sleep)
        final: dict[int, tuple[str, str]] = {}

        # Steps 1 and 2. The first write also needs the whole table, for rule 5.
        need_dump = self._read_marker("dump", name) is None
        table = self._identify_and_read(scale, client, writes, repeats)
        if isinstance(table, ScaleResult):
            return table, {}
        if need_dump:
            try:
                self._save_scale_dump(name, table)
            except Exception as exc:
                message = f"{name}: the backup of this scale failed ({exc.__class__.__name__}). Nothing was written."
                return self._uniform_result(name, ScaleStatus.READ_FAILED, message, [p.difference for p in plans]), {}
        held = _real_records(table)

        last_filled = max(table, default=0)
        planned = {p.difference.item_id for p in writes}
        fill, table_problems = self._table_problems(name, table, held, writes)
        unseen = [i for i in fill if i not in seen_fills]
        if unseen and not table_problems:
            # No fill goes out that nobody saw. The acknowledgement of what a preview warned of is checked before this.
            words = (
                f"{name}: this write would put placeholders at id {_id_ranges(unseen)}, and no preview showed that. "
                "Nothing was sent. Make a new preview that reads the scales, and acknowledge it."
            )
            table_problems.append(Drift(name, "fill_not_previewed", None, None, {}, None, words))

        # Step 3. Refuse the whole scale on any drift nobody acknowledged.
        drift = self._compare(name, writes, held)
        drift.extend(self._lost_delete_drift(name, writes, table))
        drift.extend(table_problems)
        refused = [d for d in drift if d.ack_token is None or d.ack_token not in tokens]
        if refused:
            refused_ids = {d.item_id for d in refused}
            # A problem with the table, or a clash on a filler, names no planned id, and it still stops every record.
            everyone = bool(refused_ids - planned)
            for plan in writes:
                item_id = plan.difference.item_id
                if everyone or item_id in refused_ids:
                    words = " ".join(d.message for d in refused if everyone or d.item_id == item_id)
                    final[item_id] = (ScaleStatus.DRIFT_REFUSED.value, words)
                else:
                    final[item_id] = (ITEM_NOT_SENT, "Another record on this scale was refused, so nothing was written.")
            message = f"{name}: this scale's records differ, and that was not acknowledged. Nothing was written."
            if everyone:
                message = " ".join(d.message for d in refused if d.item_id not in planned)
            return self._result(name, plans, final, message, 0, (), tuple(drift), ()), {}

        # Step 4. A placeholder is ours and needs no check. The reserved PLU rule would refuse it.
        records = {p.difference.item_id: self._build_record(p, held, stamp) for p in writes}
        for plan in writes:
            if plan.difference.action == _DELETE:
                continue
            item_id = plan.difference.item_id
            problems = self._validate_item(records[item_id], plan.difference.index)
            if problems:
                for other in writes:
                    final[other.difference.item_id] = (ITEM_NOT_SENT, "Another record on this scale was refused, so nothing was written.")
                final[item_id] = (ScaleStatus.DRIFT_REFUSED.value, f"The record this scale holds cannot be sent back: {problems[0].message}")
                message = f"{name}: this scale's record holds values that cannot be sent. Nothing was written."
                return self._result(name, plans, final, message, 0, (), tuple(drift), ()), {}

        # The flag may have been turned off while reading. A flag set to off wins at once.
        current = self._current_scale(name)
        if current is None or current.write_enabled is not True:
            return (
                self._uniform_result(
                    name,
                    ScaleStatus.SKIPPED_NOT_WRITE_ENABLED,
                    f"{name}: writes were turned off. Nothing was sent.",
                    [p.difference for p in plans],
                ),
                {},
            )

        # Step 5. Sent once. Step 7: nothing here ever resends. Anything past the end of the table
        # goes in id order, so a send that stops early leaves no gap behind it.
        fillers = {i: placeholder_record(i, stamp) for i in fill}
        everything = {**records, **fillers}
        if max(planned) > last_filled:
            ordered = [everything[i] for i in sorted(everything)]
        else:
            ordered = [records[p.difference.item_id] for p in writes]
        report = client.send_items(ordered)
        by_id = {o.item_id: o for o in report.items}
        if report.attempts == 0:
            for plan in writes:
                item_id = plan.difference.item_id
                final[item_id] = (by_id[item_id].status, report.message)
            result = self._result(name, plans, final, report.message, 0, report.replies, tuple(drift), ())
            # No record went out, so the scale's status comes from the opening step.
            # It answered the read a moment ago, so a scale that now takes no connection may have frozen.
            return replace(result, status=report.status.value, may_be_frozen=report.status is ScaleStatus.UNREACHABLE), {}

        # Step 6. After a stall or a lost connection, do not read now. The scale may still be
        # working, and a new session could queue behind that work. It stays unknown, and the
        # next attempt starts from step 1, whose read shows whether the record landed.
        sent_ids = [i for i in everything if by_id[i].status != ITEM_NOT_SENT]
        lost = any(o.status == ScaleStatus.UNKNOWN.value and not o.said_error for o in report.items)
        # A delete or a PLU change is verified only with the old PLU on no other id, so it reads the whole table.
        checks_old_plu = [p for p in writes if p.moves_plu or p.difference.action == _DELETE]
        limit = None if checks_old_plu else max(sent_ids, default=0)
        readback = client.read_items(limit, patient=True) if sent_ids and not lost else None
        _note_repeat(readback, name, repeats)
        if readback is not None and readback.ok:
            self._save_reads(name, readback)
        mismatches: list[Drift] = []
        for plan in writes:
            item_id = plan.difference.item_id
            prior = table.get(item_id)
            final[item_id] = self._settle(name, item_id, by_id[item_id], records[item_id], prior, readback, mismatches, lost)
            if plan in checks_old_plu and final[item_id][0] == ScaleStatus.VERIFIED.value:
                was_there = held.get(item_id)
                plus = {plan.difference.before.plu, records[item_id].plu, *([was_there.plu] if was_there else [])}
                final[item_id] = self._old_plu_gone(name, item_id, plus, readback, mismatches) or final[item_id]
        fill_words, fill_problems = self._check_fillers(name, fillers, by_id, readback)
        mismatches.extend(fill_problems)
        result = self._result(name, plans, final, "", report.attempts, report.replies, tuple(drift), tuple(mismatches))
        if fill_problems and ScaleStatus(result.status) in LANDED_STATUSES:
            result = replace(result, status=ScaleStatus.MISMATCH.value)
        detail = readback.message if readback is not None and not readback.ok else ""
        message = self._final_message(name, result.status, report.message, lost, readback, detail)
        absent = [p.difference.item_id for p in writes if p.difference.item_id not in held and p.difference.action not in ("add", _DELETE)]
        if absent:
            ids = ", ".join(str(i) for i in absent)
            message = f"{message} This scale held nothing at id {ids}, so the record went there as a new item."
        if fill_words:
            message = f"{message} {fill_words}"
        gaps = [i for i in range(1, last_filled) if i not in table and i not in everything]
        if gaps:
            message = (
                f"{message} This scale still has nothing at id {_id_ranges(gaps)}, with records after them. "
                "A gap may be what freezes a read. A reload from the command line closes it."
            )
        landed_fillers = tuple(i for i in fillers if by_id[i].status == ScaleStatus.ACCEPTED_NOT_VERIFIED.value)
        frozen = readback is not None and not readback.ok and readback.may_be_frozen
        return replace(result, message=message, fillers_landed=landed_fillers, may_be_frozen=frozen), records

    @staticmethod
    def _old_plu_gone(name: str, item_id: int, plus: set[str], readback: Any, mismatches: list[Drift]) -> tuple[str, str] | None:
        """The record at the id can match and an old PLU still sell, from under another id. None if none does."""
        elsewhere = sorted(i for i, record in readback.items.items() if record.plu in plus and i != item_id)
        if not elsewhere:
            return None
        plu = readback.items[elsewhere[0]].plu
        words = f"{name} holds the new record at id {item_id}, but PLU {_quoted(plu)} is still on id {_id_ranges(elsewhere)}."
        mismatches.append(Drift(name, "mismatch", item_id, plu, {}, readback.items[elsewhere[0]], words))
        return ScaleStatus.MISMATCH.value, words

    @staticmethod
    def _check_fillers(name: str, fillers: Mapping[int, Item], by_id: Mapping[int, Any], readback: Any) -> tuple[str, list[Drift]]:
        """What became of the placeholders sent to leave no gap. They are not in any plan, so they get their own words."""
        if not fillers:
            return "", []
        said = f"To leave no gap, ids {_id_ranges(list(fillers))} got placeholders in the same send."
        unsent = [i for i in fillers if by_id[i].status == ITEM_NOT_SENT]
        if unsent:
            said = f"{said} An earlier problem stopped the ones at id {_id_ranges(unsent)}, so a gap may remain. A reload from the command line closes it."
        if readback is None or not readback.ok:
            return f"{said} They were not read back.", []
        problems: list[Drift] = []
        for item_id, record in fillers.items():
            held = readback.items.get(item_id)
            if item_id in unsent or (held is not None and held.to_fields() == record.to_fields()):
                continue
            problems.append(
                Drift(name, "mismatch", item_id, record.plu, {}, held, f"{name} said Ok to a placeholder at id {item_id}, but holds something different there.")
            )
        if problems:
            return f"{said} Reading it back shows something else at id {_id_ranges([d.item_id for d in problems])}.", problems
        return f"{said} Reading it back confirmed them.", []

    @staticmethod
    def _final_message(name: str, status: str, sent_message: str, lost: bool, readback: Any, detail: str) -> str:
        """Written after each record is settled, so it says the same as the final status."""
        if lost:
            return f"{sent_message} Not read back yet, because the scale may still be working. Run a comparison later."
        was_read = readback is not None and readback.ok
        if status == ScaleStatus.VERIFIED.value:
            return f"{name}: confirmed by reading it back."
        if status == ScaleStatus.ACCEPTED_NOT_VERIFIED.value:
            return f"{name}: the scale said Ok to every record. Could not read it back, so nobody has confirmed it. {detail}".strip()
        if status == ScaleStatus.REJECTED.value:
            if was_read:
                return f"{name}: the scale said Error, and reading it back shows those records did not change."
            return f"{name}: the scale said Error. Could not read it back. {detail}".strip()
        if status == ScaleStatus.MISMATCH.value:
            return f"{name}: the scale said Ok, but reading it back shows something different. See mismatches."
        if status == ScaleStatus.UNKNOWN.value:
            if was_read:
                return f"{name}: the result is unknown. The exchange failed, and reading it back did not settle it. See each item."
            return f"{name}: the result is unknown. Could not read it back. {detail}".strip()
        return sent_message

    @staticmethod
    def _settle(
        name: str, item_id: int, sent: Any, record: Item, prior: Item | None, readback: Any, mismatches: list[Drift], lost: bool = False
    ) -> tuple[str, str]:
        """What the read back says about one record. Exact text, every field.

        Only an Ok plus a matching read is verified. If the scale said Error, then only a read
        can say it did not change. Without one, nobody knows.
        """
        if sent.status == ITEM_NOT_SENT:
            return ITEM_NOT_SENT, "An earlier problem on this scale stopped this record."
        if lost:
            status = ScaleStatus.UNKNOWN.value if sent.said_error else sent.status
            return status, "Not read back yet, because the scale may still be working."
        if readback is None or not readback.ok:
            if sent.said_error:
                return ScaleStatus.UNKNOWN.value, "The scale said Error, and could not be read back, so nobody knows if it kept this."
            return sent.status, "Could not read it back."
        held = readback.items.get(item_id)
        if held is not None and held.to_fields() == record.to_fields():
            if sent.status == ScaleStatus.ACCEPTED_NOT_VERIFIED.value:
                return ScaleStatus.VERIFIED.value, "Confirmed by reading it back."
            if sent.said_error:
                return ScaleStatus.UNKNOWN.value, "The scale said Error, but reading it back shows the record is there. Check the scale."
            return ScaleStatus.UNKNOWN.value, "The exchange failed, but reading it back shows the record is there. Check the scale."
        same_as_before = (held is None and prior is None) or (
            held is not None and prior is not None and held.to_fields() == prior.to_fields()
        )
        if sent.said_error and same_as_before:
            return ScaleStatus.REJECTED.value, "The scale said Error, and reading it back shows the record did not change."
        if sent.status != ScaleStatus.ACCEPTED_NOT_VERIFIED.value:
            note = "still holds the old record, and may still be working" if same_as_before else "holds something else"
            return ScaleStatus.UNKNOWN.value, f"The exchange failed. Reading it back shows the scale {note}. Check again later."
        fields = _field_differences(record, held) if held is not None else {}
        mismatches.append(
            Drift(
                name,
                "mismatch",
                item_id,
                record.plu,
                fields,
                held,
                f"{name} said Ok, but holds something different at id {item_id}.",
            )
        )
        return ScaleStatus.MISMATCH.value, "The scale said Ok, but reading it back shows something different."

    @staticmethod
    def _result(
        name: str,
        plans: Sequence[_Plan],
        final: Mapping[int, tuple[str, str]],
        message: str,
        attempts: int,
        replies: tuple[str, ...],
        drift: tuple[Drift, ...],
        mismatches: tuple[Drift, ...],
    ) -> ScaleResult:
        items = []
        for plan in plans:
            difference = plan.difference
            status, note = final.get(difference.item_id, (ScaleStatus.UNKNOWN.value, ""))
            items.append(ItemResult(difference.item_id, difference.plu, difference.action, status, note))
        states = {i.status for i in items}
        overall = next((s for s in STATUS_SEVERITY if s.value in states), ScaleStatus.UNKNOWN)
        # A record never sent is never a success, whatever happened to the others.
        if ITEM_NOT_SENT in states and overall in LANDED_STATUSES:
            overall = ScaleStatus.UNKNOWN
        return ScaleResult(
            scale=name,
            status=overall.value,
            message=message,
            attempts=attempts,
            items=tuple(items),
            replies=replies,
            drift=drift,
            mismatches=mismatches,
        )

    @staticmethod
    def _left_out_result(name: str, plans: Sequence[_Plan]) -> ScaleResult:
        """A scale the preview left out because it may be frozen. Not contacted. Each record is not_sent,
        which the unfinished list reads as a miss once the catalog takes the change."""
        message = (
            f"{name}: left out. It gave no reply earlier, so nothing was read or sent to it. "
            "It is on the unfinished list as missing this change until a person has checked it."
        )
        items = tuple(ItemResult(p.difference.item_id, p.difference.plu, p.difference.action, ITEM_NOT_SENT, "") for p in plans)
        return ScaleResult(scale=name, status=ScaleStatus.NOT_AVAILABLE.value, message=message, attempts=0, items=items)

    @staticmethod
    def _uniform_result(name: str, status: ScaleStatus, message: str, differences: Sequence[Difference]) -> ScaleResult:
        return ScaleResult(
            scale=name,
            status=status.value,
            message=message,
            attempts=0,
            items=tuple(ItemResult(d.item_id, d.plu, d.action, status.value, "") for d in differences),
        )

    @staticmethod
    def _outcome(
        results: Sequence[ScaleResult], not_touched: Sequence[str], problems: Sequence[str], still_differ: Sequence[Drift] = ()
    ) -> tuple[str, str, str]:
        """Written from the item results, so it never says more than is known. Success needs
        proof for every record. Saying nothing changed needs proof too, which a failure has
        only if no record went out, or a read showed it did not change."""
        parts = "; ".join(f"{r.scale}: {r.status}" for r in results)
        statuses = {i.status for r in results for i in r.items}
        # A placeholder sent to leave no gap is a change on the scale too, whatever became of the records after it.
        landed = any(s.value in statuses for s in LANDED_STATUSES) or any(r.fillers_landed for r in results)
        unsettled = any(s.value in statuses for s in UNSETTLED_STATUSES)
        all_verified = statuses == {ScaleStatus.VERIFIED.value} and all(r.status == ScaleStatus.VERIFIED.value for r in results)
        tail = f" Not touched: {', '.join(not_touched)}." if not_touched else ""
        if problems:
            outcome = "catalog_out_of_sync"
            display = "The scales were contacted, but the item list could not be fully updated. Run a comparison."
        elif all_verified and not not_touched and not still_differ:
            outcome = "success"
            display = "Done. Every scale confirmed it by reading it back."
        elif all_verified and still_differ:
            outcome = "partial"
            listed = "; ".join(
                f"{d.scale} id {d.item_id} has {name} {diff.after}, and the catalog has {diff.before}"
                for d in still_differ
                for name, diff in d.fields.items()
            )
            display = f"This change is confirmed where it went, but the scales still differ: {listed}."
        elif landed and ITEM_NOT_SENT in statuses:
            outcome = "partial"
            display = "Partly done. Some records were not sent."
        elif landed:
            outcome = "partial"
            display = "Partly done. Some scales have not confirmed this change."
        elif unsettled:
            outcome = "unknown"
            display = "The result is not known. Check the scale, or run a comparison."
        else:
            outcome = "failed"
            display = "Not done. No scale took this change."
        if not all_verified:
            for result in results:
                if result.fillers_landed:
                    display = f"{display} {result.scale} took placeholders at id {_id_ranges(result.fillers_landed)}."
        if still_differ and not all_verified:
            scales = ", ".join(sorted({d.scale for d in still_differ}))
            display = f"{display} {scales} also still differ from the catalog. See still_differ."
        summary = f"{display} {parts}.{tail}"
        if problems:
            summary = f"{summary} {' '.join(problems)}"
        return outcome, summary, display

    @staticmethod
    def _still_differ(plans: Sequence[_Plan], results: Sequence[ScaleResult], sent_records: Mapping[str, Mapping[int, Item]]) -> tuple[Drift, ...]:
        """Where a scale took a change and still holds other values than the catalog: the fields of
        acknowledged drift, which were kept on purpose. The timestamp does not count."""
        found: list[Drift] = []
        for result in results:
            sent = sent_records.get(result.scale, {})
            landed = {i.item_id for i in result.items if ScaleStatus.__members__.get(i.status.upper()) in LANDED_STATUSES}
            for plan in plans:
                difference = plan.difference
                record = sent.get(difference.item_id)
                if record is None or difference.after is None or difference.item_id not in landed:
                    continue
                fields = _field_differences(difference.after, record)
                fields.pop("timestamp", None)
                if fields:
                    found.append(
                        Drift(
                            result.scale,
                            "still_differs",
                            difference.item_id,
                            difference.plu,
                            fields,
                            record,
                            f"{result.scale} still differs from the catalog at id {difference.item_id} in {', '.join(fields)}.",
                        )
                    )
        return tuple(found)

    # ------------------------------------------------------------ after sending

    def _commit(
        self,
        plans: Sequence[_Plan],
        results: Sequence[ScaleResult],
        sent_records: Mapping[str, Mapping[int, Item]],
        change_id: str,
    ) -> list[str]:
        """Update the item list and each scale's state. Never raises: the replies are logged."""
        problems: list[str] = []
        at = self._now_text()
        for plan in plans:
            difference = plan.difference
            statuses: dict[str, str] = {}
            for result in results:
                for item in result.items:
                    if item.item_id == difference.item_id and item.action == difference.action:
                        statuses[result.scale] = item.status
            landed = any(ScaleStatus(s) in LANDED_STATUSES for s in statuses.values() if s != ITEM_NOT_SENT)
            unsettled = any(ScaleStatus(s) in UNSETTLED_STATUSES for s in statuses.values() if s != ITEM_NOT_SENT)
            try:
                self._commit_item(plan, landed, unsettled, statuses)
            except Exception as exc:
                problems.append(f"PLU {difference.plu}: {exc.__class__.__name__}.")
            for scale_name, status in statuses.items():
                try:
                    record = sent_records.get(scale_name, {}).get(difference.item_id)
                    self._record_state(scale_name, plan, status, record, change_id, at)
                except Exception as exc:
                    problems.append(f"State for {scale_name}, PLU {difference.plu}: {exc.__class__.__name__}.")
            if difference.action == "add" and (landed or unsettled):
                # A scale this change left out never saw the item, so its read need not reach this id.
                for scale in self._config.scales:
                    if scale.name not in statuses:
                        try:
                            self._catalog.record_scale_state(difference.item_id, scale.name, ITEM_NOT_SENT, change_id, at, belief="absent")
                        except Exception as exc:
                            problems.append(f"State for {scale.name}, PLU {difference.plu}: {exc.__class__.__name__}.")
            if difference.action == _DELETE and landed:
                # A scale this change left out still holds the item, so its id must wait for that scale too.
                for scale in self._config.scales:
                    if scale.name not in statuses:
                        try:
                            self._catalog.record_scale_state(difference.item_id, scale.name, ITEM_NOT_SENT, change_id, at)
                        except Exception as exc:
                            problems.append(f"State for {scale.name}, PLU {difference.plu}: {exc.__class__.__name__}.")
            if plan.moves_plu and landed and difference.before.plu != difference.plu:
                # The catalog has moved. Any scale that has not confirmed it may still hold the old
                # record, including one this change left out, and a retry must know that record.
                for scale in self._config.scales:
                    if statuses.get(scale.name) != ScaleStatus.VERIFIED.value:
                        try:
                            self._catalog.note_plu_move(scale.name, difference.before)
                        except Exception as exc:
                            problems.append(f"PLU change on {scale.name}, PLU {difference.plu}: {exc.__class__.__name__}.")
            elif plan.moves_plu and unsettled and difference.before.plu != difference.plu:
                # No scale is known to have taken it, so the catalog stays. But a scale with a lost reply
                # may be selling the new PLU, so that must not look free until a read or a repeat settles it.
                for scale_name, status in statuses.items():
                    if status in (ScaleStatus.UNKNOWN.value, ScaleStatus.MISMATCH.value):
                        try:
                            self._catalog.note_plu_move(scale_name, difference.after, trying=True)
                        except Exception as exc:
                            problems.append(f"PLU change on {scale_name}, PLU {difference.plu}: {exc.__class__.__name__}.")
        return problems

    def _commit_item(self, plan: _Plan, landed: bool, unsettled: bool, statuses: Mapping[str, str]) -> None:
        difference = plan.difference
        if plan.replace_orphan:
            # Only for a scale that took the record. Any other still holds the orphan.
            took = [name for name, status in statuses.items() if status != ITEM_NOT_SENT and ScaleStatus(status) in LANDED_STATUSES]
            for name in took:
                self._catalog.clear_orphan_id(difference.item_id, name)
        if difference.action == "add" and (landed or unsettled):
            # On unknown the record may be on the scale, so it must keep this id.
            if plan.replace_orphan:
                self._catalog.upsert_item(difference.after, replacing_orphan=True)
            else:
                self._catalog.upsert_item(difference.after)
        elif difference.action in ("edit", _SETTLE_ACCEPT) and landed:
            self._catalog.upsert_item(difference.after)
        elif difference.action == _DELETE and landed:
            self._catalog.remove_item(difference.item_id)

    def _record_state(self, scale_name: str, plan: _Plan, status: str, record: Item | None, change_id: str, at: str) -> None:
        item_id = plan.difference.item_id
        deleting = plan.difference.action == _DELETE
        if status == ScaleStatus.VERIFIED.value:
            # A delete confirmed by a read is the one thing that says this scale no longer holds it.
            belief = "absent" if deleting else "present"
            self._catalog.record_scale_state(item_id, scale_name, status, change_id, at, belief=belief, record=None if deleting else record)
            self._catalog.clear_plu_moves(item_id, scale_name)
            return
        if status == ScaleStatus.ACCEPTED_NOT_VERIFIED.value:
            # A bare Ok does not show that the record is there.
            self._catalog.record_scale_state(item_id, scale_name, status, change_id, at, belief="unknown", record=None if deleting else record)
            return
        if status in (ScaleStatus.UNKNOWN.value, ScaleStatus.MISMATCH.value):
            self._catalog.record_scale_state(item_id, scale_name, status, change_id, at, belief="unknown")
            return
        # Not delivered, so an existing belief stands. The catalog keeps it when belief is None.
        # A lost reply on an add or a delete stays on record too. Later checks need to know the
        # change it came from, and a refusal that sent nothing says nothing new about the scale.
        earlier = self._catalog.scale_states(item_id).get(scale_name)
        if earlier is not None and earlier["status"] in (ScaleStatus.UNKNOWN.value, ScaleStatus.MISMATCH.value):
            if self._logged_action(earlier["change_id"], item_id) in ("add", _DELETE):
                return
        # An add over an orphan noted on this scale is different. The orphan is still there.
        orphan_here = plan.replace_orphan and scale_name in self._catalog.orphan_ids().get(item_id, [])
        new_and_absent = plan.difference.action == "add" and not orphan_here
        self._catalog.record_scale_state(item_id, scale_name, status, change_id, at, belief="absent" if new_and_absent else None)

    # ------------------------------------------------------------ comparing

    def compare_scales(self, scales: Sequence[str] | None = None, *, leave_out_frozen: bool = False) -> list[ScaleComparison]:
        """Read each enabled scale and list where it differs from the catalog. Writes nothing.

        A scale with neither flag on is not contacted. This takes the apply lock, and a
        full read at the default pace takes about 30 seconds per scale.

        leave_out_frozen leaves out, under the apply lock, any scale that may be frozen: it comes back
        not_contacted with may_be_frozen true, and is not read. The MCP server and the command line ask
        for it unless exactly one scale is named, see compare_leaves_out_frozen, so only a compare that
        names that scale alone can clear its flag.
        """
        chosen = self._named_scales(scales)
        out: list[ScaleComparison] = []
        with _apply_lock(self._lock_path):
            # Under the lock, so a freeze another process logged a moment ago is seen.
            frozen = set(self.scales_may_be_frozen()) if leave_out_frozen else set()
            for scale in chosen:
                if scale.name in frozen and (scale.write_enabled is True or scale.read_enabled is True):
                    words = _frozen_skip_words(scale.name) + (f" {NAME_IT_ALONE}" if scales is not None else "")
                    out.append(ScaleComparison(scale.name, "not_contacted", words, may_be_frozen=True))
                    continue
                out.append(self._compare_one(scale))
        return out

    def _named_scales(self, scales: Sequence[str] | None) -> tuple[ScaleConfig, ...]:
        """The scales asked for, in config order. A name nobody knows is refused, not skipped."""
        current = self._current_scales()
        if scales is None:
            return current
        if isinstance(scales, (str, bytes)):
            raise ValidationFailed("Scales must be a list of names.")
        names = list(scales)
        if not names:
            raise ValidationFailed(
                "The list of scales is empty. Leave it out to mean every scale.",
                errors=[PreviewError("no_scales", "The list of scales is empty.", None, "scales")],
            )
        known = {s.name for s in current}
        bad = [str(n) for n in names if n not in known]
        if bad:
            message = f"No scale named {', '.join(bad)}."
            raise ValidationFailed(message, errors=[PreviewError("unknown_scale", message, None, "scales")])
        return tuple(s for s in current if s.name in names)

    def read_scale_items(
        self,
        *,
        plus: Sequence[str] | None = None,
        ids: Sequence[int] | None = None,
        scales: Sequence[str] | None = None,
        avoid_frozen: bool = False,
    ) -> list[ScaleRead]:
        """What each enabled scale holds right now. Writes nothing.

        Give PLUs, ids or both to get only those records. Give neither for every record.
        A PLU can be under any id, so PLUs or neither read the whole table. Ids alone stop
        as soon as the highest one is reached. avoid_frozen leaves out, under the apply lock, any
        scale that may be frozen: it comes back not_contacted with may_be_frozen true, and is not read.
        """
        wanted_plus = list(plus) if plus is not None else []
        wanted_ids = list(ids) if ids is not None else []
        if not all(isinstance(p, str) for p in wanted_plus):
            raise ValidationFailed("Each PLU must be text.")
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in wanted_ids):
            raise ValidationFailed("Each id must be a whole number.")
        chosen = self._named_scales(scales)
        up_to = max(wanted_ids) if wanted_ids and not wanted_plus and plus is None else None
        out: list[ScaleRead] = []
        with _apply_lock(self._lock_path):
            # Under the lock, so a freeze another process logged a moment ago is seen.
            frozen = set(self.scales_may_be_frozen()) if avoid_frozen else set()
            for scale in chosen:
                # A scale with reads and writes off is not read either way, and gets the usual words for that.
                if scale.name in frozen and (scale.write_enabled is True or scale.read_enabled is True):
                    out.append(ScaleRead(scale.name, "not_contacted", _frozen_skip_words(scale.name), may_be_frozen=True))
                    continue
                out.append(self._read_one(scale, up_to, wanted_plus, wanted_ids))
        return out

    def _read_one(self, scale: ScaleConfig, up_to: int | None, wanted_plus: list[str], wanted_ids: list[int]) -> ScaleRead:
        name = scale.name
        if scale.write_enabled is not True and scale.read_enabled is not True:
            return ScaleRead(name, "not_contacted", f"{name}: contact is not enabled for this scale.")
        client = self._scale_factory(scale, self._settings, sleep=self._sleep)
        try:
            identity = client.identify()
            if not identity.ok:
                self._note_no_reply(name, "read", identity)
                return ScaleRead(name, identity.status.value, identity.message, may_be_frozen=identity.may_be_frozen)
            read = client.read_items(up_to)
        except Exception as exc:
            return ScaleRead(name, "read_failed", f"{name}: the read failed ({exc.__class__.__name__}).")
        second_try = getattr(read, "repeated", False) is True
        if not read.ok:
            self._note_no_reply(name, "read", read)
            return ScaleRead(name, (read.status or ScaleStatus.READ_FAILED).value, read.message, may_be_frozen=read.may_be_frozen, read_repeated=second_try)
        self._save_reads(name, read)
        catalog = {i.id: i for i in self._catalog.list_items()}
        everything = not wanted_plus and not wanted_ids
        records = []
        real = _real_records(read.items)
        for item_id in sorted(real):
            held = real[item_id]
            if not (everything or held.plu in wanted_plus or item_id in wanted_ids):
                continue
            listed = catalog.get(item_id)
            difference = self._held_difference(name, item_id, listed, held)
            if difference is not None:
                difference = self._with_move_hint(difference)
            records.append(HeldRecord(held, listed, difference is None, difference))
        return ScaleRead(
            name,
            "read",
            f"{name}: read {len(read.items)} records.{self._placeholder_words(read.items, real)}",
            items_read=len(read.items),
            read_at=self._now_text(),
            complete=read.complete,
            records=tuple(records),
            read_repeated=second_try,
        )

    @staticmethod
    def _placeholder_words(table: Mapping[int, Item], real: Mapping[int, Item]) -> str:
        count = len(table) - len(real)
        return f" {count} of them are placeholders, which count as empty." if count else ""

    @staticmethod
    def _held_difference(name: str, item_id: int, wanted: Item | None, held: Item) -> Drift | None:
        if wanted is None:
            return Drift(name, "not_in_catalog", item_id, held.plu, {}, held, f"{name} holds PLU {_quoted(held.plu)} at id {item_id}, and the catalog does not list it.")
        if held.to_fields() == wanted.to_fields():
            return None
        fields = _field_differences(wanted, held)
        if _is_other_product(wanted, held):
            return Drift(name, "different_product", item_id, wanted.plu, fields, held, _other_product_message(name, item_id, wanted, held))
        return Drift(name, "differs", item_id, wanted.plu, fields, held, f"{name} differs at id {item_id} in {', '.join(fields)}.", _drift_token(name, item_id, held))

    # ------------------------------------------------------------ settling drift on one item

    def preview_settle_drift(
        self,
        *,
        scale: str,
        take: str,
        plu: str | None = None,
        id: int | None = None,
        requested_by: str = "unknown",
        avoid_frozen: bool = False,
    ) -> Preview:
        """Settle one item that differs on one scale, in the direction a person chose.

        take="scale" copies what the scale holds into the catalog, and writes nothing to the
        scale. take="catalog" writes the catalog's record to the scale through the normal
        seven steps. This reads the scale now. Apply with apply_change, naming the drift token.
        avoid_frozen refuses with scale_may_be_frozen, under the apply lock, if that scale may be frozen.
        """
        now = self._clock()
        created_at = now.isoformat(timespec="seconds")
        repeats: set[str] = set()

        def refused(
            code: str, message: str, field: str | None = None, diffs: tuple[Difference, ...] = (), drift: tuple[Drift, ...] = (), frozen: bool = False
        ) -> Preview:
            return Preview(
                ok=False,
                preview_id=None,
                created_at=created_at,
                expires_at=None,
                requested_by=requested_by,
                scales=(scale,) if isinstance(scale, str) else (),
                differences=diffs,
                warnings=(),
                errors=(PreviewError(code, message, 0, field, frozen),),
                drift=drift,
                scales_read_repeated=tuple(sorted(repeats)),
                # Not read because it may be frozen, so it is named as left out.
                left_out=(scale,) if code == "scale_may_be_frozen" and isinstance(scale, str) else (),
            )

        if take not in ("scale", "catalog"):
            return refused("invalid_change", "take must be scale or catalog.", "take")
        if (plu is None) == (id is None):
            return refused("invalid_change", "Give exactly one of plu and id.", "plu")
        target = next((s for s in self._current_scales() if s.name == scale), None)
        if target is None:
            return refused("unknown_scale", f"No scale named {scale}.", "scale")
        restart = self.config_changed()
        if restart:
            return refused("config_changed", restart)
        if target.write_enabled is not True and (take == "catalog" or target.read_enabled is not True):
            return refused("scale_not_write_enabled", f"{scale}: this scale cannot be contacted for this.", "scale")
        version = self._catalog.version()
        item = self._catalog.get_by_plu(plu) if plu is not None else self._catalog.get_by_id(id)
        if item is None:
            return refused("item_not_found", f"No item with {'PLU ' + str(plu) if plu is not None else 'id ' + str(id)}.", "plu")

        action = _SETTLE_ACCEPT if take == "scale" else _SETTLE_PUSH
        probe = _Plan(Difference(0, action, item.id, item.plu, {}, item, item), {})
        client = self._scale_factory(target, self._settings, sleep=self._sleep)
        with _apply_lock(self._lock_path):
            # Under the lock, so a freeze another process logged a moment ago is seen.
            if avoid_frozen and scale in self.scales_may_be_frozen():
                return refused("scale_may_be_frozen", _frozen_skip_words(scale), "scale")
            table = self._identify_and_read(target, client, [probe], repeats, what="settle_preview")
        if isinstance(table, ScaleResult):
            return refused("settle_read_failed", table.message, "scale", frozen=table.may_be_frozen)
        held = _real_records(table)
        current = held.get(item.id)
        if current is not None and current.to_fields() == item.to_fields():
            return refused("no_drift", f"{scale} already matches the catalog for PLU {item.plu}. There is nothing to settle.")
        drift = (self._settle_finding(scale, item, current),)
        warnings: list[PreviewWarning] = []
        replace_orphan = False
        fills: dict[str, list[int]] = {}
        lagging = self._with_move_hint(drift[0]) if current is not None else drift[0]

        if lagging.unfinished_plu_change:
            # The scale is behind a PLU change. Taking its record would undo the change in the catalog
            # while other scales hold the new PLU. Writing the catalog's would change a PLU by settling.
            code = "plu_change_not_finished" if take == "scale" else "plu_edit_held"
            return refused(code, lagging.message, "plu", drift=(replace(lagging, ack_token=None),))
        if take == "scale":
            if current is None:
                return refused("settle_not_possible", f"{scale} holds nothing at id {item.id}, so there is nothing to take.", drift=drift)
            problems = self._validate_item(current, 0)
            if problems:
                first = problems[0]
                return refused(first.code, f"What {scale} holds cannot go in the catalog. {first.message}", first.field, drift=drift)
            clash = next((i for i in self._catalog.list_items() if i.plu == current.plu and i.id != item.id), None)
            if clash is not None:
                return refused("duplicate_plu", f"PLU {current.plu} is already on id {clash.id} in the catalog.", "plu", drift=drift)
            still_held = sorted(r["scale"] for r in self._deleted_held() if r["plu"] == current.plu)
            moved_from = self._catalog.old_plus_in_use().get(current.plu, item.id)
            if still_held or moved_from != item.id:
                message = f"PLU {current.plu} is not free, because a delete or a PLU change on it has not finished. Finish that first."
                return refused("duplicate_plu", message, "plu", drift=drift)
            difference = Difference(0, _SETTLE_ACCEPT, item.id, current.plu, _field_differences(item, current), item, current)
            warning = self._price_warning(item, current, 0)
        else:
            replace_orphan = scale in self._catalog.orphan_ids().get(item.id, [])
            other_product = current is not None and _is_other_product(item, current)
            if other_product and not replace_orphan:
                # Stays held on purpose. Settling could overwrite what a scale holds with nobody having
                # asked for a PLU change. An edit that names the PLU is the way to change one.
                message = "Writing this would change the PLU or Code on the scale, and settling never does that. To change a PLU, make an edit that asks for it."
                return refused("plu_edit_held", message, "plu", drift=drift)
            elsewhere = sorted(i.id for i in held.values() if i.plu == item.plu and i.id != item.id)
            if elsewhere:
                message = f"{scale} already holds PLU {item.plu} at id {elsewhere[0]}, so this would make a duplicate."
                return refused("duplicate_plu", message, "plu", drift=drift)
            problems = self._validate_item(item, 0)
            if problems:
                first = problems[0]
                return refused(first.code, f"The catalog's record cannot be sent. {first.message}", first.field, drift=drift)
            fill, table_problems = self._table_problems(scale, table, held, [probe])
            if table_problems:
                first = table_problems[0]
                return refused(first.kind, first.message, "scale", drift=(*drift, *table_problems))
            if fill:
                fills[scale] = fill
                warnings.append(self._fill_warning(scale, fill))
            fields = _field_differences(current, item) if current is not None else {}
            difference = Difference(0, _SETTLE_PUSH, item.id, item.plu, fields, item, item)
            warning = self._price_warning(current, item, 0) if current is not None and not other_product else None
            if replace_orphan:
                warnings.append(PreviewWarning("orphan_replaced", f"This replaces what {scale} holds at id {item.id}.", True, 0))
        if warning is not None:
            warnings.append(warning)

        others = sorted(s.name for s in self._config.scales if s.name != scale)
        if others:
            warnings.append(
                PreviewWarning("scale_not_targeted", f"This change will not touch {', '.join(others)}. Those scales keep what they hold.", True)
            )
            if take == "scale":
                warnings.append(
                    PreviewWarning(
                        "other_scales_may_differ",
                        f"The catalog will change, so {', '.join(others)} may then show drift for this item until settled too.",
                    )
                )
        for change_id, ids in self._interrupted().items():
            if item.id in ids:
                warnings.append(
                    PreviewWarning(
                        "interrupted_change",
                        f"Change {change_id} touched this item and never finished. Apply will refuse until it is resolved.",
                    )
                )

        preview_id = uuid.uuid4().hex
        expires = now + timedelta(seconds=self._settings.preview_ttl_seconds)
        expires_at = expires.isoformat(timespec="seconds")
        payload = {
            "preview_id": preview_id,
            "requested_by": requested_by,
            "scales": [scale],
            "write_flags": {scale: target.write_enabled is True},
            "catalog_version": version,
            "plans": [_Plan(difference, {}, replace_orphan).to_dict()],
            "warnings": [w.to_dict() for w in warnings],
            "fills": fills,
        }
        self._catalog.save_preview(preview_id, payload, created_at, expires_at)
        return Preview(
            ok=True,
            preview_id=preview_id,
            created_at=created_at,
            expires_at=expires_at,
            requested_by=requested_by,
            scales=(scale,),
            differences=(difference,),
            warnings=tuple(warnings),
            errors=(),
            drift=drift,
            scales_read=(scale,),
            scales_read_repeated=tuple(sorted(repeats)),
        )

    def _compare_one(self, scale: ScaleConfig) -> ScaleComparison:
        name = scale.name
        if scale.write_enabled is not True and scale.read_enabled is not True:
            return ScaleComparison(name, "not_contacted", f"{name}: contact is not enabled for this scale.")
        client = self._scale_factory(scale, self._settings, sleep=self._sleep)
        try:
            identity = client.identify()
            if not identity.ok:
                self._note_no_reply(name, "compare", identity)
                return ScaleComparison(name, identity.status.value, identity.message, may_be_frozen=identity.may_be_frozen)
            read = client.read_items(None)
        except Exception as exc:
            return ScaleComparison(name, "read_failed", f"{name}: the read failed ({exc.__class__.__name__}).")
        second_try = getattr(read, "repeated", False) is True
        if not read.ok:
            self._note_no_reply(name, "compare", read)
            return ScaleComparison(name, (read.status or ScaleStatus.READ_FAILED).value, read.message, may_be_frozen=read.may_be_frozen, read_repeated=second_try)
        self._save_reads(name, read)
        catalog = {i.id: i for i in self._catalog.list_items()}
        differences: list[Drift] = []
        real = _real_records(read.items)
        for item_id in sorted(set(catalog) | set(real)):
            wanted = catalog.get(item_id)
            held = real.get(item_id)
            if wanted is None:
                differences.append(
                    self._held_difference(name, item_id, None, held)
                )
            elif held is None:
                differences.append(
                    Drift(name, "missing", item_id, wanted.plu, {}, None, f"{name} holds nothing at id {item_id}, and the catalog lists PLU {wanted.plu} there.")
                )
            else:
                found = self._held_difference(name, item_id, wanted, held)
                if found is not None:
                    differences.append(self._with_move_hint(found))
        return ScaleComparison(
            name,
            "compared",
            f"{name}: read {len(read.items)} records, and {len(differences)} differ from the catalog.{self._placeholder_words(read.items, real)}",
            items_read=len(read.items),
            read_at=self._now_text(),
            differences=tuple(differences),
            read_repeated=second_try,
        )

    def mark_orphans_from_read(
        self, scale: str, *, ids: Sequence[int] | None = None, note: str = "", requested_by: str = "unknown", avoid_frozen: bool = False
    ) -> OrphanMarks:
        """Read one scale, and record what it holds that the catalog does not list as orphans there.

        Real tables can be full, so an add has nowhere to land until this is done. It changes
        nothing on the scale. Give ids to mark only those. An add can then choose one with
        replace_orphan_id, and a preview says so before anything is replaced. avoid_frozen leaves
        the scale alone, under the apply lock, if it may be frozen: not_contacted with may_be_frozen true.
        """
        chosen = self._named_scales([scale])[0]
        if ids is not None and not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise ValidationFailed("Each id must be a whole number.")
        name = chosen.name
        if chosen.write_enabled is not True and chosen.read_enabled is not True:
            return OrphanMarks(name, "not_contacted", f"{name}: contact is not enabled for this scale.")
        client = self._scale_factory(chosen, self._settings, sleep=self._sleep)
        with _apply_lock(self._lock_path):
            if avoid_frozen and name in self.scales_may_be_frozen():
                return OrphanMarks(name, "not_contacted", _frozen_skip_words(name), may_be_frozen=True)
            try:
                identity = client.identify()
                if not identity.ok:
                    self._note_no_reply(name, "orphans", identity)
                    return OrphanMarks(name, identity.status.value, identity.message, may_be_frozen=identity.may_be_frozen)
                read = client.read_items(None)
            except Exception as exc:
                return OrphanMarks(name, "read_failed", f"{name}: the read failed ({exc.__class__.__name__}).")
            if not read.ok:
                self._note_no_reply(name, "orphans", read)
                return OrphanMarks(name, (read.status or ScaleStatus.READ_FAILED).value, read.message, may_be_frozen=read.may_be_frozen)
            self._save_reads(name, read)
            listed = {i.id for i in self._catalog.list_items()}
            eligible = {i for i in _real_records(read.items) if i not in listed}
            wanted = eligible if ids is None else set(ids)
            marked = sorted(wanted & eligible)
            not_marked = sorted(wanted - eligible)
            for item_id in marked:
                self._catalog.mark_orphan_id(item_id, name, note)
            at = self._now_text()
            self._catalog.append_log(
                uuid.uuid4().hex,
                "orphans_marked",
                {"scale": name, "requested_by": requested_by, "ids": marked, "note": note, "at": at},
                at,
            )
        return OrphanMarks(
            name,
            "read",
            f"{name}: marked {len(marked)} ids as orphans. Nothing on the scale changed.",
            marked=tuple(marked),
            not_marked=tuple(not_marked),
            held=tuple(read.items[i] for i in marked),
        )

    def list_unfinished(self) -> list[UnfinishedWork]:
        """Changes some scale has not confirmed, with what to preview to finish each.

        Deletes, PLU changes, and any other change a scale may have missed, or a last record sent that
        differs from the catalog. From the catalog and its log, with no scale contact, so it says what
        may be so. A read shows what is. If part of it cannot be built, the list says so in a row of
        kind list_failed, because an empty list must never stand in for one nobody could make.
        """
        names = [s.name for s in self._config.scales]
        out: list[UnfinishedWork] = []
        frozen: set[tuple[str, int]] = set()
        failed: str | None = None
        try:
            for row in self._catalog.deleted_still_held():
                if row["scale"] in names:
                    plu = row["plu"]
                    words = f"{row['scale']} may still sell PLU {plu}, because a delete of it has not been confirmed there. Repeat the delete to finish it."
                    repeat = {"action": "delete", "target_id": row["item_id"]}
                    out.append(UnfinishedWork(row["scale"], "delete", row["item_id"], plu, (plu,), repeat, words))
            moves: dict[tuple[str, int], list[dict]] = {}
            for row in self._catalog.all_plu_moves():
                moves.setdefault((row["scale"], row["item_id"]), []).append(row)
            for (scale, item_id), rows in moves.items():
                item = self._catalog.get_by_id(item_id)
                old_plus = tuple(sorted(r["old_plu"] for r in rows if item is not None and r["old_plu"] != item.plu))
                if scale in names and old_plus:
                    listed = " or ".join(old_plus)
                    tried = [r["old_plu"] for r in rows if r["trying"] and r["old_plu"] != item.plu]
                    if tried:
                        # The catalog has not moved, so finishing it means asking for the new PLU again.
                        words = f"{scale} may have taken PLU {listed} at id {item_id}, and the catalog still has PLU {item.plu}, because a reply was lost. A read shows which. Repeat that edit to finish it."
                        repeat = {"action": "edit", "target_plu": item.plu, "plu": tried[-1]}
                    else:
                        words = f"{scale} may still be on PLU {listed} at id {item_id}, and the catalog has PLU {item.plu}. Repeat that edit to finish it."
                        repeat = {"action": "edit", "target_plu": item.plu, "plu": item.plu}
                    out.append(UnfinishedWork(scale, "plu_change", item_id, item.plu, old_plus, repeat, words))
        except Exception as exc:
            failed = exc.__class__.__name__
        try:
            missed, frozen = self._missed_changes(names, {(u.scale, u.item_id) for u in out})
            out.extend(missed)
        except Exception as exc:
            failed = exc.__class__.__name__
        try:
            out.extend(self._records_that_differ(names, {(u.scale, u.item_id) for u in out}))
        except Exception as exc:
            failed = exc.__class__.__name__
        try:
            out.extend(self._behind_a_settle(names, {(u.scale, u.item_id) for u in out}))
        except Exception as exc:
            failed = exc.__class__.__name__
        # One row of a scale that gave no reply is enough. Going back for another row reads that same scale.
        stuck = {u.scale for u in out if (u.scale, u.item_id) in frozen}
        how: dict[str, str] = {}
        try:
            # So is a scale that gave no reply to anything else, such as a check or a review, with no read since.
            how = self._frozen_events()
            stuck |= set(how)
        except Exception as exc:
            failed = exc.__class__.__name__
        out = [self._as_frozen(u, how.get(u.scale)) if u.scale in stuck else u for u in out]
        if failed is not None:
            words = f"The list of unfinished work could not be built ({failed}), so it may leave things out. Do not read it as all clear."
            out.append(UnfinishedWork("", _LIST_FAILED, 0, "", (), {}, words))
        return out

    @staticmethod
    def _as_frozen(work: UnfinishedWork, how: str | None = None) -> UnfinishedWork:
        """The row's own words would say to settle or repeat, and either one reads that scale.
        how says if the newest no reply was a connect that failed. Both count as may be frozen."""
        plu_words = f"PLU {work.plu}" if _PLU.fullmatch(work.plu) else "the item"
        if how == "unreachable":
            silence = "Last time it could not be reached, which can be the network or a frozen scale, and it has not answered a read since, so it may have frozen."
        elif how == "no_reply":
            silence = "It connected, then gave no reply, and has not answered a read since, so it may have frozen."
        else:
            silence = "It gave no reply, and has not answered a read since, so it may have frozen."
        words = (
            f"{work.scale} has not confirmed a change to {plu_words} at id {work.item_id}. {silence} "
            "Do not read it until a person says it has been checked."
        )
        return replace(work, may_be_frozen=True, message=words)

    def _missed_changes(self, names: Sequence[str], listed: set[tuple[str, int]]) -> tuple[list[UnfinishedWork], set[tuple[str, int]]]:
        """Live items a scale may not hold as the catalog lists them, because a change did not end verified there.
        Also which rows, of these or of those already listed, came from a scale that gave no reply.

        A row in scale_state holds only the last result, and a refusal on a later change that moved
        nothing overwrites a real miss. So the row says where to look, and the log says what was missed.
        A read of the scale made after that change, showing what the catalog lists, ends it.
        """
        rows = [
            r for r in self._catalog.live_state_rows_with_status(_MISSED_MAY_HOLD | _MISSED_NOT_SENT)
            if r["scale"] in names and (r["scale"], r["item_id"]) not in listed
        ]
        if not rows and not listed:
            return [], set()
        pending, frozen = self._pending_misses(rows, listed)
        out: list[UnfinishedWork] = []
        for row in rows:
            scale, item_id = row["scale"], row["item_id"]
            miss = pending.get((scale, item_id))
            item = self._catalog.get_by_id(item_id)
            if miss is None or item is None or self._read_since_matches(scale, item, miss["at"]):
                continue
            plu_words = f"PLU {item.plu}" if _PLU.fullmatch(item.plu) else "the item"
            if miss["action"] == _DELETE:
                # Nothing landed for certain, or the item would have left the catalog. It is still listed there.
                words = f"{scale} may or may not have deleted {plu_words} at id {item_id}, because a delete of it has not been confirmed there. Repeat the delete to finish it."
                out.append(UnfinishedWork(scale, _DELETE, item_id, item.plu, (item.plu,), {"action": _DELETE, "target_id": item_id}, words, miss["status"], miss["change_id"], at=miss["at"] or ""))
                continue
            if miss["took"]:
                words = (
                    f"{scale} may not hold the newest change to {plu_words} at id {item_id}. That change ended as {miss['status']} there. "
                    "Settle this scale to the catalog to finish it. A read also shows if it already matches."
                )
                repeat = {"tool": "scale_preview_settle_drift", "scale": scale, "take": "catalog", "plu": item.plu}
            else:
                # A settle would put back what the person asked to change, so no repeat is offered for it.
                words = (
                    f"A change to {plu_words} at id {item_id} ended as {miss['status']} on {scale}. The catalog did not take this change. {scale} may hold the new value. "
                    "A settle to the catalog puts the old value back. To finish the change, make a new preview of it."
                )
                repeat = {}
            out.append(UnfinishedWork(scale, "missed_change", item_id, item.plu, (), repeat, words, miss["status"], miss["change_id"], catalog_took_it=miss["took"], at=miss["at"] or ""))
        return out, frozen

    def _records_that_differ(self, names: Sequence[str], listed: set[tuple[str, int]]) -> list[UnfinishedWork]:
        """Rows whose last record sent differs from the catalog in anything but the time, whatever the status.

        A verified result says the scale holds what was sent. For an edit that is the scale's own record
        with only the asked for fields changed, so it can be verified and still differ from the catalog.
        A scale with writes off is a person's choice and stays out. A read after that record went, showing
        what the catalog lists but for the time, ends it. No record stored means nothing to compare, so
        that row says nothing here. Neither does a placeholder, which is an empty slot and not a record.
        """
        writes_on = {s.name for s in self._current_scales() if s.write_enabled is True}
        items = {i.id: i for i in self._catalog.list_items()}
        out: list[UnfinishedWork] = []
        for row in self._catalog.live_state_rows_with_record():
            scale, item_id = row["scale"], row["item_id"]
            item = items.get(item_id)
            if scale not in names or scale not in writes_on or (scale, item_id) in listed or item is None:
                continue
            try:
                record = Item.from_fields(row["record"])
            except (TypeError, ValueError):
                continue
            if is_placeholder(record):
                continue
            differs = tuple(name for name in _field_differences(item, record) if name != "timestamp")
            if not differs or self._read_since_matches(scale, item, row["at"], but_time=True):
                continue
            plu_words = f"PLU {item.plu}" if _PLU.fullmatch(item.plu) else "the item"
            fields = ", ".join(_FIELD_WORDS.get(name, name) for name in differs)
            words = (
                f"{scale} may differ from the catalog for {plu_words} at id {item_id}, in {fields}. The last record sent there did not bring it to the catalog's record. "
                "Settle this scale to the catalog to end it. A read also shows if it already matches."
            )
            repeat = {"tool": "scale_preview_settle_drift", "scale": scale, "take": "catalog", "plu": item.plu}
            out.append(UnfinishedWork(scale, "missed_change", item_id, item.plu, (), repeat, words, row["status"], row["change_id"], differs_in=differs, at=row["at"]))
        return out

    def _behind_a_settle(self, names: Sequence[str], listed: set[tuple[str, int]]) -> list[UnfinishedWork]:
        """Scales a settle that took another scale's record may have left behind. Round 6 N8.

        That settle names one scale and moves the catalog, so nothing goes to the others. A scale with no
        record stored for the item has no state row to compare, so only its last read can say it differs.
        A row is listed when the scale has writes on, its last read shows a real record that differs from
        the catalog in more than the time, and it has no result for the item at or after that settle. A
        scale never read says nothing here. A read that shows the catalog's record, or a later change or
        settle that reaches the scale, ends it. Plain drift with no such settle stays out, as before.
        """
        taken: dict[int, dict[str, Any]] = {}
        for row in self._catalog.settles_that_took_a_scale():
            taken[row["item_id"]] = row  # oldest first, so the newest settle of each item is kept
        if not taken:
            return []
        writes_on = {s.name for s in self._current_scales() if s.write_enabled is True}
        items = {i.id: i for i in self._catalog.list_items()}
        out: list[UnfinishedWork] = []
        for item_id, settle in sorted(taken.items()):
            item = items.get(item_id)
            if item is None:
                continue
            reads = self._catalog.last_reads(item_id)
            states = self._catalog.scale_states(item_id)
            for scale in names:
                if scale == settle["scale"] or scale not in writes_on or (scale, item_id) in listed:
                    continue
                state = states.get(scale)
                if state is not None and state.get("status") in _REACHED_THE_SCALE and not _strictly_before(state.get("at"), settle["at"]):
                    continue  # a result that reached the scale at or after the settle, or with a time that cannot be read, decides instead
                # A result that sent nothing, such as a failed read or a scale left out, does not end the row. Round 11 L1.
                read = reads.get(scale)
                if read is None or read["record"] is None or is_placeholder(read["record"]):
                    continue
                differs = tuple(name for name in _field_differences(item, read["record"]) if name != "timestamp")
                if not differs:
                    continue
                plu_words = f"PLU {item.plu}" if _PLU.fullmatch(item.plu) else "the item"
                fields = ", ".join(_FIELD_WORDS.get(name, name) for name in differs)
                other = settle["scale"] if settle["scale"] in names else "another scale"
                words = (
                    f"{scale} may differ from the catalog for {plu_words} at id {item_id}, in {fields}. A settle took {other}'s record into the "
                    f"catalog, and nothing was sent to {scale}. Its last read shows another record. "
                    "Settle this scale to the catalog to end it. A read also shows if it already matches."
                )
                repeat = {"tool": "scale_preview_settle_drift", "scale": scale, "take": "catalog", "plu": item.plu}
                out.append(UnfinishedWork(
                    scale, "missed_change", item_id, item.plu, (), repeat, words, ITEM_NOT_SENT, settle["change_id"] or "",
                    differs_in=differs, at=settle["at"] or "", from_last_read=True,
                ))
        return out

    @staticmethod
    def _changes_in_log(events: Sequence[Mapping[str, Any]], wanted: set[int]) -> dict[str, dict[int, dict[str, Any]]]:
        """Change id, in log order, to item id, to what that change did to the item on each scale."""
        changes: dict[str, dict[int, dict[str, Any]]] = {}
        for event in events:
            for item in event["items"]:
                if item.get("item_id") in wanted:
                    entry = changes.setdefault(event["change_id"], {}).setdefault(item["item_id"], {"action": item.get("action"), "statuses": {}, "ats": {}, "log_ids": {}, "frozen": {}})
                    entry["statuses"][event["scale"]] = item.get("status")
                    # Scales go one after another, so each has its own time. The read before a write is older than this.
                    entry["ats"][event["scale"]] = event["at"]
                    entry["log_ids"][event["scale"]] = event.get("log_id")
                    entry["frozen"][event["scale"]] = event.get("may_be_frozen") is True
        return changes

    @staticmethod
    def _catalog_moved(entry: Mapping[str, Any]) -> bool:
        """Did the catalog take this change? A scale that was not sent it is only behind if so.
        A settle that takes the scale's record is left out on purpose. It names one scale, so it cannot have
        landed and also not been sent there, and that scale's own status says all there is."""
        statuses = entry["statuses"].values()
        landed = any(s in _MISSED_LANDED for s in statuses)
        unsettled = any(s in _MISSED_UNSETTLED for s in statuses)
        return (entry["action"] == "add" and (landed or unsettled)) or (entry["action"] == "edit" and landed)

    def _pending_misses(
        self, rows: Sequence[Mapping[str, Any]], also: set[tuple[str, int]] = frozenset()
    ) -> tuple[dict[tuple[str, int], dict[str, Any]], set[tuple[str, int]]]:
        """Which of these rows are still a miss, going through the log's scale results in order.
        Also which of these rows and of also have a result with no reply that nothing has ended since."""
        scales_of: dict[int, set[str]] = {}
        for r in rows:
            scales_of.setdefault(r["item_id"], set()).add(r["scale"])
        for scale, item_id in also:
            scales_of.setdefault(item_id, set()).add(scale)
        changes = self._changes_in_log(self._catalog.scale_results_in_log(), set(scales_of))
        pending: dict[tuple[str, int], dict[str, Any]] = {}
        frozen_at: dict[tuple[str, int], tuple[str, int | None]] = {}
        logged: set[tuple[str, int]] = set()
        for change_id, items in changes.items():
            for item_id, entry in items.items():
                logged.add((change_id, item_id))
                action = entry["action"]
                moved = self._catalog_moved(entry)
                for scale in scales_of[item_id]:
                    key = (scale, item_id)
                    own = entry["statuses"].get(scale)
                    if action == "add" and moved:
                        pending.pop(key, None)  # a new item on this id, so older misses are about something else
                        frozen_at.pop(key, None)
                    if own == ScaleStatus.VERIFIED.value:
                        pending.pop(key, None)
                        frozen_at.pop(key, None)
                    elif own in _MISSED_MAY_HOLD or (own in _MISSED_NOT_SENT and moved):
                        # Only an edit can leave a scale ahead of the catalog. A settle or a reload is the catalog's own record.
                        pending[key] = {"action": action, "status": own, "change_id": change_id, "at": entry["ats"][scale], "took": action != "edit" or moved}
                    if entry["frozen"].get(scale):
                        frozen_at[key] = (entry["ats"][scale], entry["log_ids"].get(scale))
        for r in rows:
            # The row's change is not in the log, so the row is all there is. It says this scale was not confirmed.
            if (r["change_id"], r["item_id"]) not in logged:
                pending.setdefault((r["scale"], r["item_id"]), {"action": None, "status": r["status"], "change_id": r["change_id"], "at": r["at"], "took": True})
        # A read of that scale saved after the silence means it answered since, so the silence is old news.
        # The miss itself stays until a read shows the catalog's record. Only the freeze words go.
        newest: dict[str, Any] = {}
        frozen = {key for key, (at, log_id) in frozen_at.items() if not self._read_of_scale_since(key[0], at, newest, log_id=log_id)}
        return pending, frozen

    def _trusted_time(self, text: Any, now: datetime) -> datetime | None:
        """A time from the log or a saved read that can be compared with now, and is not after it.
        A damaged time, one with no offset, or one in the future, such as from a clock that was wrong
        and then fixed, is None. The clock can say nothing safe about those."""
        when = _parsed_time(text)
        if when is None:
            return None
        try:
            return when if when <= now else None
        except TypeError:
            return None

    def _read_of_scale_since(self, scale: str, since: str | None, cache: dict[str, Any] | None = None, *, log_id: int | None = None) -> bool:
        """A read of this scale answered after that no reply: a saved read, or a whole read that found
        nothing to keep, such as an empty table. Reads no scale. A catalog that cannot say counts as no.

        The place in the log decides, not the clock. Each read that answers keeps the newest log id at
        that moment, so a no reply logged at or before it is over, whatever its time says. Only when no
        such place is kept yet, from before this was added, does the clock decide. Then only a time that
        can be trusted counts, see _trusted_time, and a read in the same second does not count."""
        cache = {} if cache is None else cache
        if scale not in cache:
            try:
                mark = self._catalog.read_answered_log_id(scale)
            except Exception:
                mark = None
            try:
                now = self._clock()
                times = [r["at"] for r in self._catalog.last_reads_for_scale(scale).values()]
                answered = self._catalog.read_answered_at(scale)
                if answered:
                    times.append(answered)
                trusted = [t for t in (self._trusted_time(x, now) for x in times) if t is not None]
                newest = max(trusted, default=None)
            except Exception:
                now, newest = None, None
            cache[scale] = (mark, newest, now)
        mark, newest, now = cache[scale]
        if log_id is not None and mark is not None:
            return log_id <= mark
        then = self._trusted_time(since, now) if now is not None else None
        try:
            return newest is not None and then is not None and newest > then
        except TypeError:
            return False

    def _note_no_reply(self, name: str, what: str, outcome: Any) -> None:
        """Keep a read that ended may_be_frozen, so the page, the unfinished list and Claude all see it,
        from any process, until a read of that scale answers. A logged change keeps its own. Never raises.

        If the log cannot take the note, it is held in a small file in the state folder, one file per
        note, and in this process too. Every process that looks at the flags reads those files, so the
        web page and the MCP server both count the scale as may be frozen at once, and whichever looks
        first writes the note to the log once it works. A read of that scale that answers, in any process,
        ends it. If the file cannot be written either, only this process holds it, and the log says so."""
        if getattr(outcome, "may_be_frozen", False) is not True:
            return
        at = self._now_text()
        status = getattr(getattr(outcome, "status", None), "value", getattr(outcome, "status", None))
        status = status if isinstance(status, str) else None
        try:
            self._catalog.note_read_no_reply(name, what, at, status)
        except Exception:
            if self._hold_in_file(name, what, at, status):
                # Not kept here as well, so a read of that scale that answers in another process ends it here too.
                log.error(
                    "A read of %s got no reply, and that could not be noted in the log. It is held in the state folder, "
                    "so every process counts %s as may be frozen, and it goes in the log when the log works.", name, name,
                )
            else:
                with self._unnoted_lock:
                    self._unnoted[name] = (what, at, status)
                log.error(
                    "A read of %s got no reply, and that could not be noted in the log or in the state folder. Only this process "
                    "counts %s as may be frozen until the log works. A restart before then loses it, so look at %s.", name, name, name,
                )

    def _held_dir(self) -> Path:
        return Path(self._settings.state_dir) / "held_no_replies"

    def _hold_in_file(self, name: str, what: str, at: str, status: str | None) -> bool:
        """One file per note, written whole, flushed to disk and then renamed, so no reader sees half of one.
        The scale's name starts the file name, so even a file that is damaged later still counts against
        that scale. Config names are letters, digits, _ and -, so they are safe in a file name. Never raises."""
        temp: Path | None = None
        try:
            folder = self._held_dir()
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            final = folder / f"{name}.{uuid.uuid4().hex}.json"
            temp = folder / f"{final.stem}.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"scale": name, "what": what, "at": at, "status": status}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, final)
        except Exception:
            if temp is not None:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    self._tell_held_once(temp, "A part written held no reply note could not be removed from the state folder: %s")
            return False
        # The newest note per scale is enough, so the folder does not grow. Notes run under the apply lock, so no
        # other note is being made now. Another process may be logging an older one: it then finds it gone, which is harmless.
        for path, scale, _, _, _ in self._held_files():
            if scale == name and path != final:
                self._remove_held(path)
        return True

    def _tell_held_once(self, path: Path, words: str) -> None:
        """Log a problem with a held file once per process, not on every look at the flags."""
        key = f"{path}\n{words}"  # one line per file and problem, so a later, different problem is still told. Round 11 L2.
        if key not in self._held_told:
            self._held_told.add(key)
            log.error(words, path)

    def _remove_held(self, path: Path) -> bool:
        """Remove a held file. True if it is gone. A file that cannot be removed is logged with its path."""
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except Exception:
            self._tell_held_once(path, "A held no reply note could not be removed from the state folder: %s. Remove it by hand.")
            return False
        return True

    def _held_files(self) -> list[tuple[Path, str, str, str, str | None]]:
        """Notes held in the state folder, by any process: (file, scale, what, at, status), oldest first.

        A file named for its scale that cannot be read still counts against that scale, as a damaged row in
        the log does. Its time is the file's own, and what read and how are unknown. A damaged file from
        before the name held the scale names no scale, so it is logged and left out."""
        try:
            paths = list(self._held_dir().glob("*.json"))
        except Exception:
            return []
        names = {s.name for s in self._config.scales}
        out = []
        for path in paths:
            named = path.name.split(".")[0] if path.name.count(".") == 2 else None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                scale, what, at, status = data["scale"], data["what"], data["at"], data.get("status")
                if not (isinstance(scale, str) and isinstance(what, str) and isinstance(at, str)):
                    raise ValueError("not a held note")
            except FileNotFoundError:
                continue  # another process wrote it to the log a moment ago
            except Exception:
                if named is None or named not in names:
                    self._tell_held_once(path, "A held no reply note in the state folder could not be read, and names no scale: %s")
                    continue
                try:
                    stamp = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                except FileNotFoundError:
                    continue
                except Exception:
                    stamp = self._now_text()
                self._tell_held_once(path, "A held no reply note in the state folder could not be read. It still counts against its scale: %s")
                scale, what, at, status = named, "unknown", stamp, None
            if scale in names:
                out.append((path, scale, what, at, status if isinstance(status, str) else None))
        out.sort(key=lambda note: note[3])
        return out

    def _forget_unnoted(self, name: str) -> None:
        """A read of this scale answered, under the apply lock, so any note still held for it, here or in the
        state folder, is old news. Every read and every note runs under that lock, so each note held was made
        before this read. A file that cannot be removed is logged, and _write_unnoted no longer counts it."""
        with self._unnoted_lock:
            self._unnoted.pop(name, None)
        for path, scale, _, _, _ in self._held_files():
            if scale == name and not self._remove_held(path):
                self._held_done.add(str(path))

    def _write_unnoted(self) -> dict[str, str]:
        """Try again to log each no reply the log could not take. Returns those still held, scale to how.
        A note whose scale has answered a read since, by a time that can be trusted, is dropped.

        A file whose note went to the log, or whose scale has answered since, no longer counts, even if it
        cannot be removed. The log then holds the flag, and a read that answers ends it as usual. So a file
        that cannot be removed does not keep its scale flagged for good, and its note is logged once here."""
        for path, name, what, at, status in self._held_files():
            if str(path) in self._held_done:
                self._remove_held(path)  # dealt with already. Only the removal is tried again
                continue
            try:
                if not self._read_of_scale_since(name, at):
                    self._catalog.note_read_no_reply(name, what, at, status)
            except Exception:
                continue  # the log still refuses, so the file goes on counting
            self._held_done.add(str(path))
            if self._remove_held(path):
                self._held_done.discard(str(path))
            with self._unnoted_lock:
                if self._unnoted.get(name) == (what, at, status):
                    self._unnoted.pop(name, None)
        with self._unnoted_lock:
            held = dict(self._unnoted)
        for name, (what, at, status) in held.items():
            try:
                if not self._read_of_scale_since(name, at):
                    self._catalog.note_read_no_reply(name, what, at, status)
            except Exception:
                continue
            with self._unnoted_lock:
                if self._unnoted.get(name) == (what, at, status):
                    self._unnoted.pop(name, None)
        with self._unnoted_lock:
            out = {name: no_reply_how(status) for name, (_, _, status) in self._unnoted.items()}
        for path, name, _, at, status in self._held_files():
            if str(path) in self._held_done or self._read_of_scale_since(name, at):
                continue  # in the log now, or its scale answered since
            out[name] = no_reply_how(status)  # oldest first, so the newest held note decides
        return out

    def _frozen_events(self) -> dict[str, str]:
        """Scales with a no reply, on a logged change, a reload or any other read, and no read of them that
        answered since. Scale to how the newest such no reply came about, see catalog.no_reply_how: unreachable,
        no_reply or unknown. Raises if the log cannot
        be read, so a caller can refuse rather than allow. Includes notes the log could not take."""
        held = self._write_unnoted()
        events = [*self._catalog.frozen_scale_results(detail=True), *self._catalog.read_no_reply_events(detail=True)]
        events.sort(key=lambda e: e["log_id"])
        cache: dict[str, Any] = {}
        out: dict[str, str] = {}
        newest: dict[str, dict] = {}
        for event in events:
            newest[event["scale"]] = event  # in log order, so the last one is the newest
        marks: dict[str, int | None] = {}
        for scale in newest:
            try:
                marks[scale] = self._catalog.read_answered_log_id(scale)
            except Exception:
                marks[scale] = None
        for event in events:
            scale = event["scale"]
            mark = marks.get(scale)
            if mark is not None and event is not newest[scale]:
                continue  # with a place in the log kept, the newest one decides for all
            # With no place kept yet, every one is checked, so an old one with a time that cannot be trusted still counts.
            if not self._read_of_scale_since(scale, event["at"], cache, log_id=event["log_id"]):
                out[scale] = event["how"]
        for name, how in held.items():
            out[name] = how
        return out

    def _frozen_by_events(self) -> set[str]:
        """The scales of _frozen_events. Raises if the log cannot be read."""
        return set(self._frozen_events())

    def scales_may_be_frozen(self, unfinished: Sequence[UnfinishedWork] | None = None) -> list[str]:
        """Scales that gave no reply, with no read of them that answered since. Reads no scale.

        No reply means on a logged change or reload, or on any other read: a check, a preview, a settle
        preview, a compare or an orphan read. This covers more than the frozen rows in list_unfinished,
        since a one scale change that moved nothing, a reload and a read leave no row. If the log cannot
        be read, every configured scale is named, so nothing is read or sent on a guess.
        """
        work = self.list_unfinished() if unfinished is None else unfinished
        names = {u.scale for u in work if u.may_be_frozen}
        order = [s.name for s in self._config.scales]
        try:
            names |= self._frozen_by_events()
        except Exception:
            names |= set(order)
        return [name for name in order if name in names]

    def frozen_how(self) -> dict[str, str]:
        """For each scale that may be frozen by a no reply, how: unreachable (could not connect), no_reply (it
        connected, then stopped answering) or unknown (not kept, as in older rows). All count as may be frozen.
        Reads no scale. Empty if the log cannot be read. scales_may_be_frozen still names every scale then."""
        try:
            return self._frozen_events()
        except Exception:
            return {}

    def settle_ended_a_miss(self, record: ChangeRecord) -> bool:
        """A settle that put the catalog's record on a scale, where an earlier change had moved the catalog
        and left that scale behind. Square and the labels may have been waiting on it. Reads no scale.
        A log that cannot be read says True: the words that follow only ask the person."""
        pushed = {
            (result.scale, item.item_id)
            for result in record.scale_results
            for item in result.items
            if item.action == _SETTLE_PUSH and item.status == ScaleStatus.VERIFIED.value
        }
        if not pushed:
            return False
        try:
            changes = self._changes_in_log(self._catalog.scale_results_in_log(), {item_id for _, item_id in pushed})
        except Exception:
            return True
        waiting: set[tuple[str, int]] = set()
        for change_id, items in changes.items():
            if change_id == record.change_id:
                break
            for item_id, entry in items.items():
                moved = self._catalog_moved(entry)
                for scale, wanted_id in pushed:
                    if wanted_id != item_id:
                        continue
                    own = entry["statuses"].get(scale)
                    if entry["action"] in (_SETTLE_PUSH, _RELOAD) and own == ScaleStatus.VERIFIED.value:
                        waiting.discard((scale, item_id))
                    elif moved and own != ScaleStatus.VERIFIED.value:
                        waiting.add((scale, item_id))
        return bool(waiting & pushed)
    def _read_since_matches(self, scale: str, item: Item, since: str | None, *, but_time: bool = False) -> bool:
        """The last read of this scale came after that change, and shows what the catalog lists at this id.
        but_time leaves the time out of what must match.

        Times are to the second, so a read in the same second does not count. That is the read back of
        the change itself. A real read of a scale takes far longer than a second.
        """
        read = self._catalog.last_reads(item.id).get(scale)
        if read is None or read["record"] is None:
            return False
        shown = read["record"].replace(timestamp=item.timestamp) if but_time else read["record"]
        if shown.to_fields() != item.to_fields():
            return False
        try:
            return datetime.fromisoformat(read["at"]) > datetime.fromisoformat(since)
        except (TypeError, ValueError):
            return False

    def list_orphans(self) -> dict[int, list[str]]:
        """Ids recorded as orphans, with the scales that hold them."""
        return self._catalog.orphan_ids()

    def mark_orphan(self, item_id: int, scale: str, note: str = "") -> None:
        """Record that a scale holds something at this id that the catalog does not list."""
        self._catalog.mark_orphan_id(item_id, scale, note)

    # ------------------------------------------------------------ reloading one scale

    def reload_scale(self, scale: str, *, accept_differences: bool = False, requested_by: str = "unknown") -> ReloadResult:
        """Overwrite ids 1 to the highest catalog id on one scale, for a person at the command line.

        This is writes only, because a read can freeze a scale, and a scale fresh from a product
        reset has nothing safe to read. Identity is checked with CX alone. Live items go exactly
        as the catalog holds them, and every other id gets a placeholder, so no gap is left. It
        also puts a scale whose ids differ from the catalog back in step, so it does not compare
        anything first. One read follows, only if every record was accepted.

        Rule 5: the item list is backed up as for any write. What the scale holds is not, since
        that needs the read this exists to avoid. The log says so.

        The catalog can be out of date, so this would put old prices on a scale. Before anything
        goes out, it lists where the catalog differs from this scale's last known read, with no
        scale contact. If it differs, or nothing is known, it raises ReloadDifferences unless a
        person has seen that and passes accept_differences=True.
        """
        restart = self.config_changed()
        if restart:
            raise ConfigChanged(restart)
        chosen = self._named_scales([scale])[0]
        if chosen.write_enabled is not True:
            message = f"{chosen.name}: writes are off for this scale, so a reload is refused. Nothing was sent."
            raise ValidationFailed(message, errors=[PreviewError("scale_not_write_enabled", message, None, "scale")])
        with _apply_lock(self._lock_path):
            return self._reload_locked(chosen, (requested_by or "unknown").strip() or "unknown", accept_differences is True)

    def _read_is_stale(self, at: str) -> bool:
        try:
            return self._clock() - datetime.fromisoformat(at) > RELOAD_READ_MAX_AGE
        except (TypeError, ValueError):
            return True

    def _reload_differences(self, name: str, records: Sequence[Item]) -> tuple[list[ReloadDifference], tuple[int, ...], str | None]:
        """Where a reload would change what this scale last showed, the ids nothing recent is known about, and
        the time of the newest read. An old read still says what the scale showed, so it is listed too."""
        reads = self._catalog.last_reads_for_scale(name)
        differences: list[ReloadDifference] = []
        not_known: list[int] = []
        last_read_at = max((r["at"] for r in reads.values()), default=None)
        for record in records:
            read = reads.get(record.id)
            if read is None or self._read_is_stale(read["at"]):
                not_known.append(record.id)
            if read is None:
                continue
            held = read["record"]
            going = is_placeholder(record)
            if held is None:
                if not going:
                    words = f"Id {record.id}: nothing there at the last read, and this sends PLU {record.plu} at {record.price}."
                    fields = {"plu": FieldDiff(None, record.plu), "description": FieldDiff(None, record.description), "price": FieldDiff(None, record.price)}
                    differences.append(ReloadDifference(record.id, None, fields, read["at"], words))
                continue
            if going:
                words = f"Id {record.id}: PLU {_quoted(held.plu)} at {_quoted(held.price)} goes, and a placeholder takes its place."
                differences.append(ReloadDifference(record.id, held.plu, {"plu": FieldDiff(held.plu, record.plu)}, read["at"], words))
                continue
            fields = _field_differences(held, record)
            fields.pop("timestamp", None)
            if fields:
                listed = ", ".join(f"{_FIELD_WORDS.get(n, n)} {_quoted(d.before)} goes to {_quoted(d.after)}" for n, d in fields.items())
                differences.append(ReloadDifference(record.id, held.plu, fields, read["at"], f"Id {record.id}, PLU {_quoted(held.plu)}: {listed}."))
        return differences, tuple(not_known), last_read_at

    @staticmethod
    def _reload_refusal(name: str, differences: Sequence[ReloadDifference], not_known: Sequence[int], total: int, last_read_at: str | None) -> str:
        parts = [f"{name}: nothing was sent, and this scale was not contacted."]
        limit = int(RELOAD_READ_MAX_AGE.total_seconds() // 60)
        if not_known:
            which = "what it holds" if len(not_known) == total else f"id {_id_ranges(not_known)}"
            parts.append(f"Nothing is known about {which} now, because no read has reached it, or the last read is more than {limit} minutes old.")
        parts.append(f"Its last read was at {last_read_at}." if last_read_at else "It has never been read.")
        if differences:
            oldest = min(d.read_at for d in differences)
            parts.append(f"A reload would change {len(differences)} ids from what it last showed, in a read as old as {oldest}.")
            parts.extend(d.message for d in differences)
        parts.append("The catalog may be out of date, and this puts its prices on the scale. If that is what you want, run it again and accept the differences.")
        return " ".join(parts)

    def _reload_locked(self, scale: ScaleConfig, actor: str, accept_differences: bool = False) -> ReloadResult:
        name = scale.name
        for change_id in self._interrupted(lock_held=True):
            raise InterruptedChange(f"Change {change_id} never finished. Resolve it before a reload.")
        rows = self._catalog.list_items(include_deleted=True)
        if not rows:
            raise ValidationFailed("The catalog holds no items, so there is nothing to send.")
        live = {i.id: i for i in self._catalog.list_items()}
        known = {i.id for i in rows}
        top = max(known)
        errors = [e for item in live.values() for e in self._validate_item(item, item.id)]
        if errors:
            raise ValidationFailed(f"The catalog holds a record that cannot be sent, at id {errors[0].index}: {errors[0].message}", errors=errors)
        stamp = self._clock().strftime("%Y-%m-%d %H:%M:%S")
        records = [live.get(i) or placeholder_record(i, stamp) for i in range(1, top + 1)]
        differences, not_known, last_read_at = self._reload_differences(name, records)
        if (differences or not_known) and not accept_differences:
            raise ReloadDifferences(self._reload_refusal(name, differences, not_known, len(records), last_read_at), differences, not_known, last_read_at)

        backup_notes = self._backup_first_writes([scale], records, _RELOAD, actor)
        change_id = uuid.uuid4().hex
        not_touched = tuple(s.name for s in self._config.scales if s.name != name)
        started_at = self._now_text()
        try:
            self._catalog.append_log(
                change_id,
                EVENT_STARTED,
                {
                    "preview_id": "",
                    "requested_by": actor,
                    "acknowledged_warnings": True,
                    "started_at": started_at,
                    "scales": [name],
                    "not_touched": list(not_touched),
                    "config_path": str(self._config.path) if self._config.path else None,
                    "backups": backup_notes,
                    "scale_dump": _RELOAD_DUMP_NOTE,
                    "differences": [d.to_dict() for d in differences],
                    "not_known": list(not_known),
                    "changes": [Difference(r.id - 1, _RELOAD, r.id, r.plu, {}, None, r).to_dict() for r in records],
                },
                started_at,
            )
        except Exception as exc:
            raise LogFailed(f"The change log could not be written ({exc.__class__.__name__}), so nothing was sent.") from exc

        attempts = 0
        replies: tuple[str, ...] = ()
        statuses: dict[int, str] = {}
        try:
            result, statuses, attempts, replies = self._reload_send(scale, records, top, differences, not_known, last_read_at)
        except (NotWriteEnabled, NotReadEnabled):
            result = ReloadResult(name, ScaleStatus.SKIPPED_NOT_WRITE_ENABLED.value, False, f"{name}: writes are off for this scale. Nothing was sent.", top_id=top, differences=tuple(differences), not_known=not_known, last_read_at=last_read_at)
        except Exception as exc:
            result = ReloadResult(name, ScaleStatus.UNKNOWN.value, False, f"{name}: an unexpected problem ({exc.__class__.__name__}). The result is unknown. Check the scale.", top_id=top, differences=tuple(differences), not_known=not_known, last_read_at=last_read_at)

        problems = self._reload_commit(name, records, live, known, statuses, change_id)
        scale_result = ScaleResult(
            scale=name,
            status=result.status,
            message=result.message,
            attempts=attempts,
            items=tuple(ItemResult(r.id, r.plu, _RELOAD, statuses.get(r.id, ITEM_NOT_SENT)) for r in records),
            replies=replies,
            may_be_frozen=result.may_be_frozen is True,
        )
        finished_at = self._now_text()
        outcome, summary, _ = self._outcome([scale_result], not_touched, problems)
        try:
            self._catalog.append_log(change_id, EVENT_SCALE_RESULT, scale_result.to_dict(), finished_at)
            self._catalog.append_log(
                change_id,
                EVENT_FINISHED,
                {
                    "finished_at": finished_at,
                    "outcome": outcome,
                    "summary": f"{result.message} {summary}",
                    "display": result.message,
                    "verified": result.status == ScaleStatus.VERIFIED.value,
                },
                finished_at,
            )
        except Exception as exc:
            problems.append(f"The end of this reload could not be logged ({exc.__class__.__name__}).")
        if problems:
            words = " ".join(problems)
            result = replace(result, ok=False, message=f"{result.message} The item list could not be fully updated: {words}")
        return replace(result, change_id=change_id)

    @staticmethod
    def _reload_price_words(differences: Sequence[ReloadDifference], not_known: Sequence[int], last_read_at: str | None, *, changed: bool) -> str:
        """Names the prices, from the last read, so a person can see what a reload did or may have done."""
        prices = [(d.plu, d.fields["price"]) for d in differences if d.plu is not None and "price" in d.fields]
        said = "changed" if changed else "may have changed"
        others = sorted(d.item_id for d in differences if not set(d.fields) <= {"price"} and d.plu is not None)
        parts = [f"The last read of this scale before this was at {last_read_at}." if last_read_at else "This scale had never been read."]
        if prices:
            listed = "; ".join(f"PLU {_quoted(plu)} from {_quoted(diff.before)} to {_quoted(diff.after)}" for plu, diff in prices)
            parts.append(f"Prices that {said} from what this scale last showed: {listed}.")
        elif not not_known:
            parts.append("No price differs from what this scale last showed.")
        if others:
            parts.append(f"Other fields {said} at id {_id_ranges(others)}. See differences.")
        if not_known:
            parts.append(f"Nobody knew what it held now at id {_id_ranges(not_known)}, so a price there may have changed with no name here.")
        return " ".join(parts)

    def _reload_send(
        self,
        scale: ScaleConfig,
        records: Sequence[Item],
        top: int,
        differences: Sequence[ReloadDifference] = (),
        not_known: tuple[int, ...] = (),
        last_read_at: str | None = None,
    ) -> tuple[ReloadResult, dict[int, str], int, tuple[str, ...]]:
        """Identity, the full send, then one read if every record was accepted. Each id's status comes back too."""
        name = scale.name
        known = dict(differences=tuple(differences), not_known=not_known, last_read_at=last_read_at)
        placeholders = tuple(r.id for r in records if is_placeholder(r))
        client = self._scale_factory(scale, self._settings, sleep=self._sleep)
        identity = client.identify()
        if not identity.ok:
            return ReloadResult(name, identity.status.value, False, identity.message, top_id=top, may_be_frozen=identity.may_be_frozen, **known), {}, 0, ()
        current = self._current_scale(name)
        if current is None or current.write_enabled is not True:
            message = f"{name}: writes were turned off. Nothing was sent."
            return ReloadResult(name, ScaleStatus.SKIPPED_NOT_WRITE_ENABLED.value, False, message, top_id=top, **known), {}, 0, ()

        report = client.send_items(records)
        # No read follows a failed send, so an Error here has no proof of what the scale kept.
        statuses = {o.item_id: ScaleStatus.UNKNOWN.value if o.said_error else o.status for o in report.items}
        accepted_word = ScaleStatus.ACCEPTED_NOT_VERIFIED.value
        not_accepted = tuple(r.id for r in records if statuses[r.id] != accepted_word)
        accepted = len(records) - len(not_accepted)
        if report.attempts == 0:
            frozen = report.status is ScaleStatus.UNREACHABLE
            result = ReloadResult(name, report.status.value, False, report.message, top_id=top, not_accepted=not_accepted, may_be_frozen=frozen, **known)
            return result, statuses, 0, report.replies
        base = dict(top_id=top, accepted=accepted, placeholders=placeholders, not_accepted=not_accepted, **known)
        may_have = self._reload_price_words(differences, not_known, last_read_at, changed=False)
        if not_accepted:
            message = (
                f"{name}: the scale accepted {accepted} of {len(records)} records, and did not take id {_id_ranges(not_accepted)}. "
                f"The result for those is unknown, so this was not read. {report.message} {may_have} Wait, check the scale, then run the reload again."
            )
            return ReloadResult(name, ScaleStatus.UNKNOWN.value, False, message, **base), statuses, report.attempts, report.replies

        read = client.read_items(None, patient=True)
        if not read.ok:
            detail = read.message.replace(scale_module.FROZEN_NOTE, "").strip()
            # Only the library's own marker says frozen. A bad answer on a scale that then closed is a plain failure.
            frozen = read.may_be_frozen is True
            why = (
                "The scale may be frozen and need a reboot. It stopped trying once the read back window ended. Do not read it again until someone has checked it."
                if frozen else "The scale answered badly, so nothing is confirmed. Check the scale before reading it again."
            )
            message = f"{name}: the scale accepted all {len(records)} records, but the read to check them failed. {why} {detail} {may_have}"
            return ReloadResult(name, accepted_word, False, message, may_be_frozen=frozen, **base), statuses, report.attempts, report.replies
        self._save_reads(name, read)

        differs = tuple(r.id for r in records if (held := read.items.get(r.id)) is None or held.to_fields() != r.to_fields())
        for record in records:
            statuses[record.id] = ScaleStatus.MISMATCH.value if record.id in differs else ScaleStatus.VERIFIED.value
        real = _real_records(read.items)
        left_above = tuple(sorted(i for i in real if i > top))
        sent_plus = {r.plu for r in records}
        duplicates = tuple(sorted({real[i].plu for i in left_above if real[i].plu in sent_plus}))
        if differs:
            status = ScaleStatus.MISMATCH.value
            message = (
                f"{name}: the scale accepted all {len(records)} records, but reading them back shows something different "
                f"at id {_id_ranges(differs)}."
            )
        else:
            status = ScaleStatus.VERIFIED.value
            message = f"{name}: the scale accepted all {len(records)} records, and reading them back matched every field. Ids 1 to {top} now match the catalog."
        message = f"{message} {self._reload_price_words(differences, not_known, last_read_at, changed=not differs)}"
        if placeholders:
            message = f"{message} Id {_id_ranges(placeholders)} got placeholders."
        if left_above:
            message = f"{message} A reload does not touch ids above {top}, and this scale holds records at id {_id_ranges(left_above)}."
        if duplicates:
            # Every record read back as sent, but rule 7 says a PLU is on one id. So this is not verified.
            status = ScaleStatus.MISMATCH.value
            message = f"{message} PLU {', '.join(duplicates)} is now on two ids, so check the scale."
        result = ReloadResult(
            name, status, not differs and not duplicates, message, differs_on_read=differs, left_above=left_above, duplicate_plus=duplicates, **base
        )
        return result, statuses, report.attempts, report.replies

    def _reload_commit(
        self,
        name: str,
        records: Sequence[Item],
        live: Mapping[int, Item],
        known: set[int],
        statuses: Mapping[int, str],
        change_id: str,
    ) -> list[str]:
        """What the catalog believes this scale holds now. A reload changes no item in the catalog."""
        rows: list[tuple[int, str, str | None, Item | None]] = []
        taken: set[int] = set()
        for record in records:
            status = statuses.get(record.id)
            if status is None or record.id not in known:
                continue
            listed = live.get(record.id)
            if status == ScaleStatus.VERIFIED.value:
                rows.append((record.id, status, "present" if listed else "absent", listed))
                taken.add(record.id)
            elif status == ScaleStatus.ACCEPTED_NOT_VERIFIED.value:
                rows.append((record.id, status, "unknown", listed))
            elif status in (ScaleStatus.UNKNOWN.value, ScaleStatus.MISMATCH.value):
                rows.append((record.id, status, "unknown", None))
            else:
                rows.append((record.id, status, None, None))
        problems: list[str] = []
        try:
            self._catalog.record_scale_states(name, rows, change_id, self._now_text())
            verified = {r.id for r in records if statuses.get(r.id) == ScaleStatus.VERIFIED.value}
            for item_id, holders in self._catalog.orphan_ids().items():
                if item_id in verified and name in holders:
                    self._catalog.clear_orphan_id(item_id, name)
            for item_id in set(self._catalog.old_plus_in_use().values()) & verified:
                self._catalog.clear_plu_moves(item_id, name)
        except Exception as exc:
            problems.append(exc.__class__.__name__)
        return problems

    # ------------------------------------------------------------ the record

    def get_change(self, change_id: str) -> ChangeRecord:
        events = self._catalog.read_log(str(change_id))
        if not any(e["event"] in _CHANGE_EVENTS for e in events):
            raise ChangeNotFound(f"No change with id {change_id}.")
        return self._with_running(self._record_from_log(str(change_id), events))

    def _with_running(self, record: ChangeRecord) -> ChangeRecord:
        """No finish means running, or stopped for good. The lock tells which."""
        if record.outcome != "in_progress":
            return record
        if self.apply_running():
            words = "This change has no finish yet, and something is running now. Check again soon."
            return replace(record, running=True, display=words, summary=words)
        words = "This change stopped without finishing. Nobody knows what reached the scales. Run a comparison, then resolve it."
        return replace(record, display=words, summary=words)

    def get_change_for_preview(self, preview_id: str) -> ChangeRecord | None:
        """The change a preview became, so a client that lost its connection can find it.

        None if that preview has not been applied. Raises PreviewNotFound if there is no such preview.
        """
        stored = self._catalog.load_preview(preview_id)
        if stored is None:
            raise PreviewNotFound(f"No preview with id {preview_id}.")
        change_id = stored["consumed_by"]
        if change_id is None:
            return None
        return self.get_change(change_id)

    def list_changes(self, limit: int = 50) -> list[ChangeRecord]:
        records: list[ChangeRecord] = []
        # Log entries that are not changes, such as the seeding note, are left out.
        for change_id in self._catalog.list_change_ids(limit * 2 + 10):
            events = self._catalog.read_log(change_id)
            if any(e["event"] in _CHANGE_EVENTS for e in events):
                records.append(self._with_running(self._record_from_log(change_id, events)))
            if len(records) >= limit:
                break
        return records

    def resolve_interrupted_change(self, change_id: str, note: str, *, requested_by: str = "unknown") -> ChangeRecord:
        """Close a change that never finished, after someone has looked. Adds to the log only.

        The next write to those items reads the scale first, so what landed will show as drift.
        """
        with _apply_lock(self._lock_path):
            if change_id not in self._catalog.open_change_ids():
                raise ChangeNotFound(f"Change {change_id} is not an interrupted change.")
            at = self._now_text()
            text = f"This change never finished. Resolved by {requested_by}: {note}".strip()
            self._catalog.append_log(
                change_id,
                EVENT_FINISHED,
                {"finished_at": at, "outcome": "interrupted", "summary": text, "display": text, "verified": False},
                at,
            )
        return self.get_change(change_id)

    @staticmethod
    def _record_from_log(change_id: str, events: Sequence[dict]) -> ChangeRecord:
        start: dict[str, Any] = {}
        finish: dict[str, Any] = {}
        results: list[ScaleResult] = []
        for event in events:
            payload = event["payload"]
            if event["event"] == EVENT_STARTED:
                start = payload
            elif event["event"] == EVENT_SCALE_RESULT:
                results.append(ScaleResult.from_dict(payload))
            elif event["event"] == EVENT_FINISHED:
                finish = payload
            elif event["event"] == EVENT_BACKUP_FAILED:
                # Nothing was sent. The preview can still be used.
                start = {**payload, "started_at": event["at"]}
                message = payload.get("message", "")
                finish = {"finished_at": event["at"], "outcome": "failed", "summary": message, "display": message}
        outcome = finish.get("outcome", "in_progress")
        waiting = "No finish was logged. It is still running or the process died. Treat the result as unknown."
        return ChangeRecord(
            change_id=change_id,
            preview_id=start.get("preview_id", ""),
            requested_by=start.get("requested_by", ""),
            acknowledged_warnings=bool(start.get("acknowledged_warnings", False)),
            started_at=start.get("started_at", ""),
            finished_at=finish.get("finished_at"),
            outcome=outcome,
            success=outcome == "success",
            changes=tuple(Difference.from_dict(d) for d in start.get("changes", [])),
            scale_results=tuple(results),
            summary=finish.get("summary", waiting),
            verified=bool(finish.get("verified", False)),
            display=finish.get("display", waiting),
            not_touched=tuple(start.get("not_touched", [])),
            still_differ=tuple(Drift.from_dict(d) for d in finish.get("still_differ", [])),
        )


__all__ = ["ScaleConnectService", "default_backup_hook"]
