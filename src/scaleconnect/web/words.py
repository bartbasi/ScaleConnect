"""Plain words for the page. One place, so every screen says the same thing.

No em dashes and no double hyphens in any text here. Staff read it in a hurry.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from scaleconnect.models import ChangeRecord, Item, ScaleResult

OK, WARN, BAD, WORKING = "ok", "warn", "bad", "working"

# status -> (words after "FRONT: ", severity)
SCALE_WORDS: dict[str, tuple[str, str]] = {
    "verified": ("changed and checked", OK),
    "accepted_not_verified": ("accepted, but not checked. Look at the scale.", WARN),
    "rejected": ("the scale refused it (it said Error). Nothing should have changed. Look at the scale to be sure.", BAD),
    "unknown": ("no clear answer. It may or may not have changed. Look at the scale.", BAD),
    "mismatch": ("the scale took it, but now shows something different. Look at the scale.", BAD),
    "unreachable": ("could not connect. Nothing was sent.", BAD),
    "read_failed": ("could not read the scale first, so nothing was sent.", BAD),
    "wrong_scale": ("this is not the scale it should be. Nothing was sent.", BAD),
    "drift_refused": ("the scale holds something different from our list. Nothing was sent.", BAD),
    "skipped_not_write_enabled": ("not changed. Changes are turned off for this scale.", WARN),
    "not_available": ("not available yet. Nothing was sent.", BAD),
    "not_sent": ("not sent. An earlier problem on this scale stopped it.", BAD),
    "seed": ("loaded from the starting list. It has not been read from the scale yet.", WARN),
}

# outcome -> (heading, severity)
OUTCOME_WORDS: dict[str, tuple[str, str]] = {
    "success": ("Done. Every scale was changed and checked.", OK),
    "partial": ("Only partly done. Read each scale below.", WARN),
    "unknown": ("Not sure what happened. Look at the scales.", BAD),
    "failed": ("It did not work. Nothing was changed on any scale.", BAD),
    "catalog_out_of_sync": ("The scales were contacted, but our own list could not be fully updated. Tell Bart.", BAD),
    "interrupted": ("This change never finished. Look at the scales.", BAD),
    "in_progress": ("Working. Please wait.", WORKING),
}


def status_words(status: str) -> tuple[str, str]:
    return SCALE_WORDS.get(status, ("something unexpected happened. Look at the scale.", BAD))


def _held_text(item: Item | None) -> str | None:
    if item is None:
        return None
    return f"{item.description}, ${item.price}"


def scale_row(result: ScaleResult) -> dict[str, Any]:
    """One finished scale as a line for staff."""
    words, severity = status_words(result.status)
    now: str | None = None
    if result.status == "drift_refused" and any(is_different_product(d) for d in result.drift):
        words = "this scale holds a different product at this number. Nothing was sent. Call Bart."
        severity = BAD
    held = None
    for drift in (*result.mismatches, *result.drift):
        if drift.held is not None:
            held = drift.held
            break
    if result.status == "drift_refused" and any(is_different_product(d) for d in result.drift):
        now = "Do not try to force this. Call Bart."
    elif result.status in ("mismatch", "drift_refused") and held is not None:
        now = f"The scale shows: {_held_text(held)}."
    elif result.status in ("accepted_not_verified", "unknown"):
        now = "What is on the scale now is not known."
    elif result.status in ("unreachable", "read_failed", "wrong_scale", "skipped_not_write_enabled", "not_available"):
        now = "Nothing was sent, so the scale is as it was."
    return {
        "scale": result.scale,
        "status": result.status,
        "text": f"{result.scale}: {words}",
        "severity": severity,
        "state": "done",
        "now": now,
        "message": result.message,
        "drift_tokens": [d.ack_token for d in result.drift if d.ack_token],
    }


def pending_row(scale: str, working: bool) -> dict[str, Any]:
    text = f"{scale}: working now" if working else f"{scale}: waiting"
    return {
        "scale": scale, "status": "working" if working else "waiting", "text": text,
        "severity": WORKING, "state": "working" if working else "waiting",
        "now": None, "message": "", "drift_tokens": [],
    }


def missing_row(scale: str) -> dict[str, Any]:
    return {
        "scale": scale, "status": "no_result",
        "text": f"{scale}: no result was recorded. Look at the scale.",
        "severity": BAD, "state": "done", "now": "What is on the scale now is not known.",
        "message": "", "drift_tokens": [],
    }


def not_touched_row(scale: str) -> dict[str, Any]:
    return {
        "scale": scale, "status": "not_touched",
        "text": f"{scale}: not part of this change. It was not touched.",
        "severity": WARN, "state": "done", "now": None, "message": "", "drift_tokens": [],
    }


def heading_for(outcome: str) -> tuple[str, str]:
    return OUTCOME_WORDS.get(outcome, ("Not sure what happened. Look at the scales.", BAD))


BAD_STATUSES = frozenset({"rejected", "unknown", "mismatch", "unreachable", "read_failed", "wrong_scale",
                          "drift_refused", "not_available", "not_sent", "no_result"})
NOTHING_SENT = frozenset({"unreachable", "read_failed", "wrong_scale", "drift_refused",
                          "skipped_not_write_enabled", "not_available"})
FIELD_NAMES = {"price": "price", "description": "description", "plu": "PLU", "code": "code"}


def is_different_product(drift: Any) -> bool:
    """The scale holds another product at this id, or something no acknowledgement may override."""
    kind = getattr(drift, "kind", None) if not isinstance(drift, dict) else drift.get("kind")
    fields = getattr(drift, "fields", None) if not isinstance(drift, dict) else drift.get("fields")
    if kind in ("id_occupied", "plu_on_other_id", "different_product"):
        return True
    return kind == "differs" and bool({"plu", "code"} & set(fields or {}))


def _shown(name: str, value: Any) -> str:
    return f"${value}" if name == "price" else str(value)


def leftover_notes(record: ChangeRecord) -> list[str]:
    """Differences an acknowledged change did not remove. The scales still hold different values."""
    requested: dict[Any, set[str]] = {}
    for change in record.changes:
        requested.setdefault(change.item_id, set()).update(change.fields)
    notes: list[str] = []
    for result in record.scale_results:
        if result.status not in ("verified", "accepted_not_verified"):
            continue
        for drift in result.drift:
            if drift.kind != "differs":
                continue
            left = [f for f in drift.fields if f != "timestamp" and f not in requested.get(drift.item_id, set())]
            if not left:
                continue
            names = ", ".join(FIELD_NAMES.get(f, f) for f in left)
            have = ", ".join(_shown(f, drift.fields[f].after) for f in left)
            ours = ", ".join(_shown(f, drift.fields[f].before) for f in left)
            notes.append(
                f"{result.scale} still holds a different {names} for PLU {drift.plu}. "
                f"It has {have}. Our list has {ours}."
            )
    return notes


def result_heading(outcome: str, statuses: list[str], leftover: bool) -> tuple[str, str]:
    """The big line at the top of a result. Built from what each scale did, so it cannot say more than is known."""
    if leftover and not any(s in BAD_STATUSES for s in statuses) and outcome in ("success", "partial"):
        return ("Changed and checked, but the scales do not all hold the same values now. Read below.", WARN)
    if outcome == "success":
        return heading_for(outcome)
    if outcome == "failed":
        if statuses and all(s in NOTHING_SENT for s in statuses):
            return ("FAILED. Nothing was sent to any scale.", BAD)
        return ("FAILED. We could not confirm any change. Look at the scales to be sure.", BAD)
    if outcome == "partial":
        if any(s in BAD_STATUSES for s in statuses):
            return ("PARTLY DONE and some scales FAILED. Read each scale below.", BAD)
        if "accepted_not_verified" in statuses:
            return ("Changed, but NOT checked. Look at the scale.", WARN)
        return ("Only some scales were changed. Read each scale below.", WARN)
    return heading_for(outcome)


def summarize(record: ChangeRecord | None) -> str:
    return record.display if record else ""


_PRICE_TEXT = re.compile(r"^\$?\s*(\d{1,3})?(?:\.(\d{0,2}))?$")


def normalize_price(value: Any) -> tuple[str | None, str | None]:
    """Staff may type 9.5 or $9.5 or 10. Returns (price as 9.50, None) or (None, reason).

    This only fixes the shape. The library still checks the range and the rules.
    """
    if not isinstance(value, str):
        return None, "Type the price as a number such as 9.99."
    text = value.strip()
    match = _PRICE_TEXT.match(text)
    if not text or match is None or (match.group(1) is None and not match.group(2)):
        return None, "Type the price as a number such as 9.99."
    whole = match.group(1) or "0"
    cents = (match.group(2) or "").ljust(2, "0")
    try:
        price = Decimal(f"{int(whole)}.{cents}")
    except InvalidOperation:
        return None, "Type the price as a number such as 9.99."
    return f"{price:.2f}", None


def item_card(item: Item) -> dict[str, str | int]:
    return {"id": item.id, "plu": item.plu, "code": item.code, "description": item.description, "price": item.price}


_LABELS = {"id": "id", "code": "code", "plu": "PLU", "description": "description", "price": "price",
           "field6": "a setting", "fields_7_12": "settings", "timestamp": "last edit time", "fields_14_20": "settings"}


def differing_fields(read: Item, wanted: Item) -> list[str]:
    """Which of the 20 fields differ, comparing every one as exact text."""
    a, b = read.to_dict(), wanted.to_dict()
    return [name for name in b if a.get(name) != b.get(name)]


def field_list(names: list[str]) -> str:
    seen: list[str] = []
    for name in names:
        label = _LABELS.get(name, name)
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)
