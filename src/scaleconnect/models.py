"""Public data models. See docs/LIBRARY.md. Others import these and do not redefine them.

Prices are text, never floats. Item checks only shape. The rules on values live in service.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed

MAX_ITEM_ID = 500
FIELD_COUNT = 20
DEFAULT_FIELDS_7_12 = ("1", "1", "0", "0", "0", "0")
DEFAULT_FIELDS_14_20 = ("0", "0", "0", "0", "0", "0", "0")


class ScaleStatus(StrEnum):
    VERIFIED = "verified"
    ACCEPTED_NOT_VERIFIED = "accepted_not_verified"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"
    SKIPPED_NOT_WRITE_ENABLED = "skipped_not_write_enabled"
    NOT_AVAILABLE = "not_available"
    WRONG_SCALE = "wrong_scale"
    MISMATCH = "mismatch"
    DRIFT_REFUSED = "drift_refused"
    READ_FAILED = "read_failed"


# A change is a full success only if every configured scale is verified.
SUCCESS_STATUSES = frozenset({ScaleStatus.VERIFIED})
# The scale took the record, as far as anyone knows.
LANDED_STATUSES = frozenset({ScaleStatus.VERIFIED, ScaleStatus.ACCEPTED_NOT_VERIFIED})
# The record may or may not be on the scale, or is there in a different form.
UNSETTLED_STATUSES = frozenset({ScaleStatus.UNKNOWN, ScaleStatus.MISMATCH})

# Worst first. A scale's status is the first of these that any of its items has.
STATUS_SEVERITY = (
    ScaleStatus.WRONG_SCALE,
    ScaleStatus.UNKNOWN,
    ScaleStatus.MISMATCH,
    ScaleStatus.REJECTED,
    ScaleStatus.DRIFT_REFUSED,
    ScaleStatus.READ_FAILED,
    ScaleStatus.UNREACHABLE,
    ScaleStatus.NOT_AVAILABLE,
    ScaleStatus.SKIPPED_NOT_WRITE_ENABLED,
    ScaleStatus.ACCEPTED_NOT_VERIFIED,
    ScaleStatus.VERIFIED,
)

# Item level only: an earlier problem on that scale stopped this record from going out.
ITEM_NOT_SENT = "not_sent"


# A delete overwrites the record with a placeholder, so the old PLU stops working. Bart approved
# this on November 2. Each id has its own placeholder PLU, and ids run 1 to 500, so 9001 to 9500
# is held back. Real items use 1001 to 7262.
PLACEHOLDER_PLU_BASE = 9000
PLACEHOLDER_DESCRIPTION = "UNUSED"
PLACEHOLDER_PRICE = "0.01"


def _text_tuple(value: Any, count: int, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a list of {count} text values")
    values = tuple(value)
    if len(values) != count or not all(isinstance(v, str) for v in values):
        raise ValueError(f"{name} must be a list of {count} text values")
    return values


@dataclass(frozen=True)
class Item:
    """One scale record. Immutable. An edit makes a new item."""

    id: int
    code: str
    plu: str
    description: str
    price: str
    field6: str = ""
    fields_7_12: tuple[str, ...] = DEFAULT_FIELDS_7_12
    timestamp: str = ""
    fields_14_20: tuple[str, ...] = DEFAULT_FIELDS_14_20

    def __post_init__(self) -> None:
        if isinstance(self.price, (float, Decimal)) or not isinstance(self.price, str):
            raise TypeError("Item.price must be text such as '9.99', never a number")
        if isinstance(self.id, bool) or not isinstance(self.id, int):
            raise TypeError("Item.id must be a whole number")
        for name in ("code", "plu", "description", "field6", "timestamp"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"Item.{name} must be text")
        object.__setattr__(self, "fields_7_12", _text_tuple(self.fields_7_12, 6, "fields_7_12"))
        object.__setattr__(self, "fields_14_20", _text_tuple(self.fields_14_20, 7, "fields_14_20"))

    def to_fields(self) -> list[str]:
        """The 20 scale fields, in order."""
        return [
            str(self.id),
            self.code,
            self.plu,
            self.description,
            self.price,
            self.field6,
            *self.fields_7_12,
            self.timestamp,
            *self.fields_14_20,
        ]

    @classmethod
    def from_fields(cls, values: Sequence[str]) -> "Item":
        if len(values) != FIELD_COUNT:
            raise ValueError(f"a scale record has {FIELD_COUNT} fields, got {len(values)}")
        return cls(
            id=int(values[0]),
            code=values[1],
            plu=values[2],
            description=values[3],
            price=values[4],
            field6=values[5],
            fields_7_12=tuple(values[6:12]),
            timestamp=values[12],
            fields_14_20=tuple(values[13:20]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "plu": self.plu,
            "description": self.description,
            "price": self.price,
            "field6": self.field6,
            "fields_7_12": list(self.fields_7_12),
            "timestamp": self.timestamp,
            "fields_14_20": list(self.fields_14_20),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Item":
        return cls(
            id=data["id"],
            code=data["code"],
            plu=data["plu"],
            description=data["description"],
            price=data["price"],
            field6=data.get("field6", ""),
            fields_7_12=tuple(data.get("fields_7_12", DEFAULT_FIELDS_7_12)),
            timestamp=data.get("timestamp", ""),
            fields_14_20=tuple(data.get("fields_14_20", DEFAULT_FIELDS_14_20)),
        )

    def replace(self, **changes: Any) -> "Item":
        return replace(self, **changes)


def placeholder_record(item_id: int, timestamp: str) -> Item:
    """What an empty slot holds on a scale. Never a real item, and never in the catalog."""
    plu = str(PLACEHOLDER_PLU_BASE + item_id)
    return Item(id=item_id, code=plu, plu=plu, description=PLACEHOLDER_DESCRIPTION, price=PLACEHOLDER_PRICE, timestamp=timestamp)


def is_placeholder(item: Item) -> bool:
    """Every field must match, apart from the time. Anything else is a real record to report."""
    return item.to_fields() == placeholder_record(item.id, item.timestamp).to_fields()


def is_reserved_plu(plu: Any) -> bool:
    """True for a PLU only placeholders may use."""
    if not isinstance(plu, str) or not plu.isascii() or not plu.isdigit():
        return False
    return PLACEHOLDER_PLU_BASE + 1 <= int(plu) <= PLACEHOLDER_PLU_BASE + MAX_ITEM_ID


CHANGE_ACTIONS = ("add", "edit", "delete")
SOLD_BY_FIELD_7 = {"weight": "1", "piece": "0"}
_CHANGE_KEYS = frozenset(
    {"action", "target_plu", "target_id", "plu", "code", "description", "price", "sold_by", "replace_orphan_id"}
)


def _change_error(message: str, field_name: str | None = None) -> ValidationFailed:
    return ValidationFailed(
        message,
        errors=[PreviewError(code="invalid_change", message=message, index=None, field=field_name)],
    )


@dataclass(frozen=True)
class Change:
    """An add, edit or delete, as asked for. Checked by preview_changes."""

    action: str
    target_plu: str | None = None
    target_id: int | None = None
    plu: str | None = None
    code: str | None = None
    description: str | None = None
    price: str | Decimal | None = None
    sold_by: str | None = None
    replace_orphan_id: int | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Change":
        if not isinstance(data, Mapping):
            raise _change_error("Each change must be an object with an action.")
        unknown = sorted((k for k in data if k not in _CHANGE_KEYS), key=str)
        if unknown:
            raise _change_error(f"Unknown keys in change: {', '.join(str(k) for k in unknown)}.", str(unknown[0]))
        if "action" not in data:
            raise _change_error("A change needs an action: add, edit or delete.", "action")
        if isinstance(data.get("price"), (float, int)):
            raise _change_error("Price must be text such as \"9.99\", not a number.", "price")
        for name in ("target_plu", "plu", "code", "description", "sold_by"):
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                raise _change_error(f"{name} must be text.", name)
        for name in ("target_id", "replace_orphan_id"):
            value = data.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise _change_error(f"{name} must be a whole number.", name)
        return cls(
            action=data["action"],
            target_plu=data.get("target_plu"),
            target_id=data.get("target_id"),
            plu=data.get("plu"),
            code=data.get("code"),
            description=data.get("description"),
            price=data.get("price"),
            sold_by=data.get("sold_by"),
            replace_orphan_id=data.get("replace_orphan_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action}
        for f in fields(self):
            if f.name == "action":
                continue
            value = getattr(self, f.name)
            if value is None:
                continue
            out[f.name] = str(value) if isinstance(value, Decimal) else value
        return out


@dataclass(frozen=True)
class Capabilities:
    add: bool
    edit: bool
    delete: bool
    read: bool
    verify: bool
    notes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "add": self.add,
            "edit": self.edit,
            "delete": self.delete,
            "read": self.read,
            "verify": self.verify,
            "notes": dict(self.notes),
        }


@dataclass(frozen=True)
class ScaleInfo:
    name: str
    write_enabled: bool
    last_status: str | None = None
    last_change_id: str | None = None
    last_seen_at: str | None = None
    last_message: str | None = None
    read_enabled: bool = False
    interrupted_change_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["interrupted_change_ids"] = list(self.interrupted_change_ids)
        return out


@dataclass(frozen=True)
class ScaleItemState:
    scale: str
    status: str | None
    belief: str | None
    change_id: str | None
    at: str | None
    last_read: Item | None = None
    last_read_at: str | None = None
    last_read_matches: bool | None = None  # all 20 fields, against the catalog. None if nothing to compare
    last_read_found_nothing: bool = False

    def to_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["last_read"] = self.last_read.to_dict() if self.last_read else None
        return out


@dataclass(frozen=True)
class ItemView:
    item: Item
    scales: dict[str, ScaleItemState]
    source: str = "catalog"
    interrupted_change_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item.to_dict(),
            "scales": {name: state.to_dict() for name, state in self.scales.items()},
            "source": self.source,
            "interrupted_change_ids": list(self.interrupted_change_ids),
        }


@dataclass(frozen=True)
class FieldDiff:
    before: str | None
    after: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"before": self.before, "after": self.after}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FieldDiff":
        return cls(before=data.get("before"), after=data.get("after"))


@dataclass(frozen=True)
class Drift:
    """What one scale holds that differs from what was expected, by field.

    In a preview or a refusal, before is the catalog and after is the scale. In a
    mismatch after a write, before is what was sent and after is what the scale holds.
    """

    scale: str
    kind: str  # differs, different_product, missing, not_in_catalog, id_occupied, plu_on_other_id, mismatch or still_differs
    item_id: int | None
    plu: str | None
    fields: dict[str, FieldDiff]
    held: Item | None
    message: str
    ack_token: str | None = None  # None means no acknowledgement can allow it

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "kind": self.kind,
            "item_id": self.item_id,
            "plu": self.plu,
            "fields": {name: diff.to_dict() for name, diff in self.fields.items()},
            "held": self.held.to_dict() if self.held else None,
            "message": self.message,
            "ack_token": self.ack_token,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Drift":
        return cls(
            scale=data["scale"],
            kind=data["kind"],
            item_id=data.get("item_id"),
            plu=data.get("plu"),
            fields={k: FieldDiff.from_dict(v) for k, v in data.get("fields", {}).items()},
            held=Item.from_dict(data["held"]) if data.get("held") else None,
            message=data.get("message", ""),
            ack_token=data.get("ack_token"),
        )


@dataclass(frozen=True)
class ScaleComparison:
    scale: str
    status: str  # compared, not_contacted, wrong_scale, unreachable or read_failed
    message: str
    items_read: int = 0
    read_at: str | None = None
    differences: tuple[Drift, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "status": self.status,
            "message": self.message,
            "items_read": self.items_read,
            "read_at": self.read_at,
            "differences": [d.to_dict() for d in self.differences],
        }


@dataclass(frozen=True)
class OrphanMarks:
    """The result of marking, from a read, what a scale holds that the catalog does not list."""

    scale: str
    status: str  # read, not_contacted, wrong_scale, unreachable or read_failed
    message: str
    marked: tuple[int, ...] = ()
    not_marked: tuple[int, ...] = ()  # ids asked for that are empty there, or that the catalog lists
    held: tuple[Item, ...] = ()  # what the scale holds at the marked ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "status": self.status,
            "message": self.message,
            "marked": list(self.marked),
            "not_marked": list(self.not_marked),
            "held": [i.to_dict() for i in self.held],
        }


@dataclass(frozen=True)
class HeldRecord:
    """One record a scale holds right now, against the catalog's record for that id."""

    item: Item
    catalog: Item | None
    matches_catalog: bool
    difference: Drift | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item.to_dict(),
            "catalog": self.catalog.to_dict() if self.catalog else None,
            "matches_catalog": self.matches_catalog,
            "difference": self.difference.to_dict() if self.difference else None,
        }


@dataclass(frozen=True)
class ScaleRead:
    scale: str
    status: str  # read, not_contacted, wrong_scale, unreachable or read_failed
    message: str
    items_read: int = 0
    read_at: str | None = None
    complete: bool = False  # the read reached the end of the table
    records: tuple[HeldRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "status": self.status,
            "message": self.message,
            "items_read": self.items_read,
            "read_at": self.read_at,
            "complete": self.complete,
            "records": [r.to_dict() for r in self.records],
        }


@dataclass(frozen=True)
class Difference:
    index: int
    action: str
    item_id: int | None
    plu: str | None
    fields: dict[str, FieldDiff]
    before: Item | None
    after: Item | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "item_id": self.item_id,
            "plu": self.plu,
            "fields": {name: diff.to_dict() for name, diff in self.fields.items()},
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Difference":
        return cls(
            index=data["index"],
            action=data["action"],
            item_id=data.get("item_id"),
            plu=data.get("plu"),
            fields={k: FieldDiff.from_dict(v) for k, v in data.get("fields", {}).items()},
            before=Item.from_dict(data["before"]) if data.get("before") else None,
            after=Item.from_dict(data["after"]) if data.get("after") else None,
        )


@dataclass(frozen=True)
class PreviewWarning:
    code: str
    message: str
    requires_ack: bool = False
    index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "requires_ack": self.requires_ack,
            "index": self.index,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PreviewWarning":
        return cls(
            code=data["code"],
            message=data["message"],
            requires_ack=bool(data.get("requires_ack", False)),
            index=data.get("index"),
        )


@dataclass(frozen=True)
class PreviewError:
    code: str
    message: str
    index: int | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "index": self.index, "field": self.field}


@dataclass(frozen=True)
class Preview:
    ok: bool
    preview_id: str | None
    created_at: str
    expires_at: str | None
    requested_by: str
    scales: tuple[str, ...]
    differences: tuple[Difference, ...]
    warnings: tuple[PreviewWarning, ...]
    errors: tuple[PreviewError, ...]
    drift: tuple[Drift, ...] = ()
    scales_read: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "preview_id": self.preview_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "requested_by": self.requested_by,
            "scales": list(self.scales),
            "differences": [d.to_dict() for d in self.differences],
            "warnings": [w.to_dict() for w in self.warnings],
            "errors": [e.to_dict() for e in self.errors],
            "drift": [d.to_dict() for d in self.drift],
            "scales_read": list(self.scales_read),
        }


@dataclass(frozen=True)
class ItemResult:
    item_id: int | None
    plu: str | None
    action: str
    status: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ItemResult":
        return cls(
            item_id=data.get("item_id"),
            plu=data.get("plu"),
            action=data["action"],
            status=data["status"],
            message=data.get("message", ""),
        )


@dataclass(frozen=True)
class ScaleResult:
    scale: str
    status: str
    message: str
    attempts: int = 0
    items: tuple[ItemResult, ...] = ()
    replies: tuple[str, ...] = ()
    drift: tuple[Drift, ...] = ()
    mismatches: tuple[Drift, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "status": str(self.status),
            "message": self.message,
            "attempts": self.attempts,
            "items": [i.to_dict() for i in self.items],
            "replies": list(self.replies),
            "drift": [d.to_dict() for d in self.drift],
            "mismatches": [d.to_dict() for d in self.mismatches],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ScaleResult":
        return cls(
            scale=data["scale"],
            status=data["status"],
            message=data.get("message", ""),
            attempts=data.get("attempts", 0),
            items=tuple(ItemResult.from_dict(i) for i in data.get("items", [])),
            replies=tuple(data.get("replies", [])),
            drift=tuple(Drift.from_dict(d) for d in data.get("drift", [])),
            mismatches=tuple(Drift.from_dict(d) for d in data.get("mismatches", [])),
        )


@dataclass(frozen=True)
class ReloadResult:
    """The report of a reload: a full send to one scale, with no read before it."""

    scale: str
    status: str  # a ScaleStatus value
    ok: bool  # every record reads back as sent, and nothing else needs a person
    message: str
    change_id: str | None = None
    top_id: int = 0  # ids 1 to this were sent
    accepted: int = 0  # records the scale said Ok to
    placeholders: tuple[int, ...] = ()  # ids that got a placeholder
    not_accepted: tuple[int, ...] = ()
    differs_on_read: tuple[int, ...] = ()
    left_above: tuple[int, ...] = ()  # real records above top_id, which a reload does not touch
    duplicate_plus: tuple[str, ...] = ()  # PLUs both in the send and on an id above top_id

    def to_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        for name in ("placeholders", "not_accepted", "differs_on_read", "left_above", "duplicate_plus"):
            out[name] = list(out[name])
        return out


@dataclass(frozen=True)
class ChangeRecord:
    change_id: str
    preview_id: str
    requested_by: str
    acknowledged_warnings: bool
    started_at: str
    finished_at: str | None
    outcome: str
    success: bool
    changes: tuple[Difference, ...]
    scale_results: tuple[ScaleResult, ...]
    summary: str
    verified: bool = False
    display: str = ""
    not_touched: tuple[str, ...] = ()
    still_differ: tuple[Drift, ...] = ()
    running: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "preview_id": self.preview_id,
            "requested_by": self.requested_by,
            "acknowledged_warnings": self.acknowledged_warnings,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "success": self.success,
            "changes": [c.to_dict() for c in self.changes],
            "scale_results": [r.to_dict() for r in self.scale_results],
            "summary": self.summary,
            "verified": self.verified,
            "display": self.display,
            "not_touched": list(self.not_touched),
            "still_differ": [d.to_dict() for d in self.still_differ],
            "running": self.running,
        }
