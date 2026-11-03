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
    "not_available": ("this action is not available. Nothing was sent.", BAD),
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


# A change on this page reaches the scales only. Only a change Claude makes also reaches Square and the labels.
AFTER_SUCCESS_NOTE = "The scales are done. This page does not change Square or the shelf labels. Tell the manager so they can be updated."
AFTER_PARTIAL_NOTE = "Not every scale has this change yet. This page does not change Square or the shelf labels. Tell the manager this item changed."
AFTER_SETTLE_SALE_NOTE = "This changed what that scale sells. This page does not change Square or the shelf labels. Tell the manager so they can check them."
AFTER_SETTLE_MISS_NOTE = "This page does not change Square or the shelf labels. If they were waiting on this scale, tell the manager so they can be updated."
AFTER_TAKE_SCALE_NOTE = "Our list changed to match this scale. This page does not change Square or the shelf labels. Tell the manager so they can check them."
_SALE_ACTIONS = frozenset({"add", "edit", "delete"})
_LANDED = frozenset({"verified", "accepted_not_verified"})
_UNSETTLED = frozenset({"unknown", "mismatch"})
_SELLING_FIELDS = frozenset({"price", "plu", "code", "fields_7_12"})  # fields 7 to 12 hold sold by and tax


def after_success_note(actions: list[str], success: bool) -> str | None:
    """The line under a change every scale confirmed, if it changed what is sold. A settle does not."""
    return AFTER_SUCCESS_NOTE if success and _SALE_ACTIONS.intersection(actions) else None


def _statuses_of(record: ChangeRecord, change: Any) -> list[str]:
    return [i.status for r in record.scale_results for i in r.items if i.item_id == change.item_id and i.action == change.action]


def after_note(record: ChangeRecord, success: bool, ended_a_miss: bool = False) -> str | None:
    """The note about Square and the labels, on any finished change where the catalog moved, or a settle changed what a scale sells.

    A partial change moves the catalog too, and a settle can be what finishes it, so neither may go quiet.
    """
    actions = [c.action for c in record.changes]
    note = after_success_note(actions, success)
    if note:
        return note
    for change in record.changes:
        statuses = _statuses_of(record, change)
        landed = any(s in _LANDED for s in statuses)
        if change.action == "settle_accept" and landed:
            return AFTER_TAKE_SCALE_NOTE
        if change.action == "settle_push" and landed:
            if not change.fields or _SELLING_FIELDS.intersection(change.fields):
                return AFTER_SETTLE_SALE_NOTE
            return AFTER_SETTLE_MISS_NOTE if ended_a_miss else None
        if change.action in _SALE_ACTIONS and (landed or (change.action == "add" and any(s in _UNSETTLED for s in statuses))):
            return AFTER_PARTIAL_NOTE
    return None


def status_words(status: str) -> tuple[str, str]:
    return SCALE_WORDS.get(status, ("something unexpected happened. Look at the scale.", BAD))


def _held_text(item: Item | None) -> str | None:
    if item is None:
        return None
    return f"{item.description}, ${item.price}"


REBOOT_LINE = "The scale did not answer. It may need a reboot. Tell Bart."

# Shown while the config file says something this running page has not loaded. Nothing is sent until it is restarted.
CONFIG_NOTICE = ("Nothing was sent. The settings have changed since this page started. "
                 "Tell Bart. The web page and the MCP server need restarting together.")


def _get(drift: Any, name: str) -> Any:
    return drift.get(name) if isinstance(drift, dict) else getattr(drift, name, None)


def is_unfinished_move(drift: Any) -> bool:
    """The scale is still on the old PLU because an earlier PLU change did not finish there.

    This reads the library's own field. It never looks at message text, because a scale's record can hold any words.
    """
    return _get(drift, "unfinished_plu_change") is True


def is_plu_elsewhere(drift: Any) -> bool:
    """The scale holds a PLU this change needs under another item."""
    return _get(drift, "kind") == "plu_on_other_id"


_FOUR_DIGITS = re.compile(r"\d{4}")


def _plu_text(value: Any) -> str:
    """A PLU for a sentence, only if it looks like one. Anything else a scale holds is left out."""
    return f" {value}" if isinstance(value, str) and _FOUR_DIGITS.fullmatch(value) else ""


# Drifts about the whole table of a scale, not about one item. Nobody may override them.
TABLE_DRIFT_WORDS: dict[str, dict[str, str]] = {
    "read_ended_early": {
        "plain": "{scale} gave back fewer items than our list has.",
        "blocked": "This scale differs from our list, and a person must look at it.",
        "more": "Nothing can be sent to it from this page. Tell Bart. Bart can reload the scale from the command line.",
        "row": "this scale gave back fewer items than our list has. Nothing was sent. A person must look at it. Tell Bart.",
    },
    "fill_blocked": {
        "plain": "{scale} has gaps where our list has items.",
        "blocked": "This scale differs from our list, and a person must look at it.",
        "more": "Sending would put empty slots over items. Nothing can be sent to it from this page. Tell Bart.",
        "row": "this scale has gaps where our list has items. Nothing was sent. A person must look at it. Tell Bart.",
    },
    "fill_not_previewed": {
        "plain": "{scale} would need empty slots to leave no gap, and no review showed that.",
        "blocked": "This was not reviewed with the scales read, so it was not sent.",
        "more": "Go back and review it again. Wait until the scales say Checked.",
        "row": "this change would have added empty slots, and no review showed that. Nothing was sent. Review it again with the scales read.",
    },
}


def drift_words(drift: Any) -> dict[str, str] | None:
    """Plain words for a drift staff need told differently. None means the library's own message will do.

    plain: what the scale holds. blocked and more: shown when nobody may override it.
    tick and note: shown when a person may acknowledge it.
    """
    scale = _get(drift, "scale")
    kind = _get(drift, "kind")
    if is_unfinished_move(drift):
        held = _get(drift, "held")
        old = held.get("plu") if isinstance(held, dict) else getattr(held, "plu", None)
        return {
            "plain": f"{scale} is still on the old PLU{_plu_text(old)}. An earlier PLU change did not finish there, so the old PLU still works on it.",
            "blocked": "This scale is still on the old PLU from an earlier change.",
            "more": "Repeat the PLU change to finish it. It is on the home screen, and on this item.",
        }
    if is_plu_elsewhere(drift):
        return {
            "plain": f"{scale} already has PLU{_plu_text(_get(drift, 'plu'))} on a different item.",
            "blocked": "This scale already has that PLU on a different item. It cannot be sent from this page.",
            "more": "Call Bart.",
        }
    if kind in TABLE_DRIFT_WORDS:
        words = TABLE_DRIFT_WORDS[kind]
        return {"plain": words["plain"].format(scale=scale), "blocked": words["blocked"], "more": words["more"]}
    if kind == "delete_may_have_landed":
        return {
            "plain": f"{scale} may have already deleted this item. An earlier delete has no confirmed result.",
            "tick": f"Yes. Bring the item back on {scale}.",
            "note": "If it should stay deleted, go back and repeat the delete instead.",
        }
    return None


_GAP_WORDS = re.compile(r"^(?P<scale>.+?) holds nothing at id (?P<ids>[0-9 ,to-]+?)\. ")


def gap_text(message: Any) -> str:
    """The gap_filled warning in staff words. It needs a tick, because real ids would otherwise be filled."""
    found = _GAP_WORDS.match(str(message or ""))
    where = f"{found.group('scale')} has no item at id {found.group('ids')}. " if found else "A scale has gaps. "
    return where + "To leave no gap, empty slots named UNUSED will be put there in the same send."


def row_drift_words(result: ScaleResult) -> str | None:
    """The line for a scale that refused because of a table problem, if it has one."""
    for drift in result.drift:
        words = TABLE_DRIFT_WORDS.get(_get(drift, "kind"))
        if words:
            return words["row"]
    return None


def is_left_out(result: Any) -> bool:
    """A scale a review left out because it did not answer: not_available, and no record sent. Any other
    not_available result is an old one, from before the page could leave a scale out."""
    items = getattr(result, "items", ())
    return getattr(result, "status", None) == "not_available" and bool(items) and all(i.status == "not_sent" for i in items)


def _silence_text(names: list[str], how: dict[str, str] | None, when: str) -> str:
    """Why scales are left alone, in short sentences. how is frozen_how: unreachable means the connect failed,
    which can be the WiFi or a frozen scale. Anything else means it did not answer."""
    how = how or {}
    no_connect = [n for n in names if how.get(n) == "unreachable"]
    silent = [n for n in names if how.get(n) != "unreachable"]
    parts = []
    if silent:
        who = ("It" if len(silent) == 1 else "They") if not no_connect else _list_text(silent)
        parts.append(f"{who} did not answer {when}.")
    if no_connect:
        whom = ("it" if len(no_connect) == 1 else "them") if not silent else _list_text(no_connect)
        parts.append(f"We could not connect to {whom} {when}.")
    return " ".join(parts)


def left_out_text(names: list[str], how: dict[str, str] | None = None) -> str:
    """Said at the top of a review and of a result. The change goes to the other scales."""
    where = _list_text(names)
    verb = "is" if len(names) == 1 else "are"
    return f"{where} {verb} left out. {_silence_text(names, how, 'last time')} Tell Bart."


LEFT_OUT_ROW = "left out. It did not answer last time, so nothing was sent to it. Tell Bart."
LEFT_OUT_ROW_NO_CONNECT = "left out. We could not connect to it last time, so nothing was sent to it. Tell Bart."
LEFT_OUT_NOW = "It still has what it had. It stays on the Not finished list until Bart has looked at it."
REBOOT_NO_CONNECT = "Could not connect to the scale. It may be off the WiFi, or frozen. Tell Bart."


def left_out_row_text(name: str, how: str | None = None) -> str:
    """The line for one left out scale, on a review and on a result."""
    return f"{name}: {LEFT_OUT_ROW_NO_CONNECT if how == 'unreachable' else LEFT_OUT_ROW}"


def left_out_review_text(how: str | None = None) -> str:
    """The box for one left out scale on a review. The scale's name is the box's title."""
    why = "We could not connect to it last time." if how == "unreachable" else "It did not answer last time."
    return f"Left out. {why} Nothing is read or sent to it. Tell Bart."


def reboot_text(status: Any, how: str | None = None) -> str:
    """The line shown for a scale that may be frozen. A failed connect gets its own words."""
    return REBOOT_NO_CONNECT if status == "unreachable" or how == "unreachable" else REBOOT_LINE


def scale_row(result: ScaleResult, how: str | None = None) -> dict[str, Any]:
    """One finished scale as a line for staff. how is frozen_how for this scale, if known."""
    words, severity = status_words(result.status)
    now: str | None = None
    if is_left_out(result):
        return {
            "scale": result.scale, "status": "left_out", "text": left_out_row_text(result.scale, how), "severity": BAD, "state": "done",
            "now": LEFT_OUT_NOW, "message": "", "may_be_frozen": False, "drift_tokens": [],
        }
    refused = result.status == "drift_refused"
    refused_unfinished = refused and any(is_unfinished_move(d) for d in result.drift)
    refused_elsewhere = refused and any(is_plu_elsewhere(d) for d in result.drift)
    table_words = row_drift_words(result) if refused else None
    if refused_unfinished:
        words = "an earlier PLU change did not finish on this scale, so the old PLU still works on it. Nothing was sent. Repeat the PLU change to finish it."
        severity = BAD
    elif refused_elsewhere:
        words = "this scale already has that PLU on a different item. Nothing was sent. Call Bart."
        severity = BAD
    elif table_words:
        words = table_words
        severity = BAD
    elif refused and any(_get(d, "kind") == "delete_may_have_landed" for d in result.drift):
        words = ("an earlier delete of this item has no confirmed result, so this scale may already have deleted it. Nothing was sent. "
                 "To finish the delete, open the item and tap Delete this item. To bring the item back, tell Bart.")
        severity = BAD
    elif refused and any(is_different_product(d) for d in result.drift):
        words = "this scale holds a different product at this number. Nothing was sent. Call Bart."
        severity = BAD
    held = None
    for drift in (*result.mismatches, *result.drift):
        if drift.held is not None:
            held = drift.held
            break
    if refused_unfinished:
        now = "Repeat the PLU change from the home screen or from the item."
    elif refused and any(_get(d, "kind") == "delete_may_have_landed" for d in result.drift):
        now = "Do not settle this item to our list. That would bring it back."
    elif refused_elsewhere or (refused and any(is_different_product(d) for d in result.drift)):
        now = "Do not try to force this. Call Bart."
    elif table_words:
        now = "Do not try to force this. A person must look at the scale."
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
        "may_be_frozen": result.may_be_frozen is True,
        "reboot": reboot_text(result.status, how) if result.may_be_frozen is True else None,
        "drift_tokens": [d.ack_token for d in result.drift if d.ack_token],
    }


READ_WORDS = {
    "unreachable": "could not connect.",
    "read_failed": "could not be read.",
    "wrong_scale": "this is not the scale it should be.",
    "not_contacted": "was not read.",
}


def read_row(scale: str, status: str, may_be_frozen: bool) -> dict[str, Any]:
    """One scale after a read now. A good read needs no words here: the item page shows what was read."""
    if status == "read":
        return {"scale": scale, "status": status, "text": f"{scale}: read.", "severity": OK, "may_be_frozen": False}
    severity = WARN if status == "not_contacted" else BAD
    return {
        "scale": scale, "status": status, "severity": severity, "may_be_frozen": may_be_frozen is True,
        "text": f"{scale}: {READ_WORDS.get(status, 'could not be read.')}",
        "reboot": reboot_text(status) if may_be_frozen is True else None,
    }


def pending_row(scale: str, working: bool) -> dict[str, Any]:
    text = f"{scale}: working now" if working else f"{scale}: waiting"
    return {
        "scale": scale, "status": "working" if working else "waiting", "text": text,
        "severity": WORKING, "state": "working" if working else "waiting",
        "now": None, "message": "", "may_be_frozen": False, "drift_tokens": [],
    }


def missing_row(scale: str) -> dict[str, Any]:
    return {
        "scale": scale, "status": "no_result",
        "text": f"{scale}: no result was recorded. Look at the scale.",
        "severity": BAD, "state": "done", "now": "What is on the scale now is not known.",
        "message": "", "may_be_frozen": False, "drift_tokens": [],
    }


def not_touched_row(scale: str) -> dict[str, Any]:
    return {
        "scale": scale, "status": "not_touched",
        "text": f"{scale}: not part of this change. It was not touched.",
        "severity": WARN, "state": "done", "now": None, "message": "", "may_be_frozen": False, "drift_tokens": [],
    }


def heading_for(outcome: str) -> tuple[str, str]:
    return OUTCOME_WORDS.get(outcome, ("Not sure what happened. Look at the scales.", BAD))


BAD_STATUSES = frozenset({"rejected", "unknown", "mismatch", "unreachable", "read_failed", "wrong_scale",
                          "drift_refused", "not_available", "not_sent", "no_result"})
NOTHING_SENT = frozenset({"unreachable", "read_failed", "wrong_scale", "drift_refused",
                          "skipped_not_write_enabled", "not_available", "left_out"})
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


_PLU_IN_TEXT = re.compile(r"\bPLU (\d{4})\b")


def plain_error(error: dict[str, Any], unfinished: bool = False, how: dict[str, str] | None = None) -> dict[str, Any]:
    """Staff words for the refusals that come with a PLU. Other errors keep the library's own message.

    unfinished: the refusal came with a drift marked unfinished_plu_change. how: frozen_how, for a scale
    that may be frozen, so a failed connect is not called a scale that did not answer.
    """
    code = error.get("code")
    message = str(error.get("message") or "")
    field = error.get("field")
    found = _PLU_IN_TEXT.search(message)
    plu = found.group(1) if found else None
    text: str | None = None
    if code == "reserved_plu":
        if field in ("target_plu", "target_id"):
            text = "That number is set aside for an empty slot on the scales. It is not a real item, so it cannot be changed or deleted."
        else:
            text = "The numbers 9001 to 9500 are set aside for empty slots on the scales. Pick a different PLU."
    elif code == "code_plu_mismatch":
        text = "The PLU and the code must be the same number. Do not send this. Tell Bart."
    elif code == "duplicate_plu":
        which = f"PLU {plu}" if plu else "That PLU"
        if "not finished" in message:
            text = (f"{which} may still be in use on a scale, because an earlier PLU change did not finish. "
                    "Finish that change first, or pick a different PLU.")
        elif "was deleted" in message:
            text = f"{which} was deleted, but a scale may still sell it. Finish that delete first, or pick a different PLU."
        elif "names a live item" in message:
            text = f"{which} is also a deleted item that a scale may still sell. Finish that delete first. Tell Bart."
        else:
            text = f"{which} is already used by another item. Pick a different PLU."
    elif code == "config_changed":
        text = CONFIG_NOTICE
    elif code == "target_changed":
        text = "That number now belongs to a different item, so nothing was changed. The old result is out of date."
    elif code == "plu_on_other_id":
        text = "A scale already has that PLU on a different item. Call Bart."
    elif code == "plu_change_not_finished" or (code == "plu_edit_held" and unfinished):
        text = ("A PLU change did not finish on this scale, so it may still be on the old PLU. "
                "Settling would undo that change. Repeat the PLU change instead.")
    elif code == "plu_edit_held":
        text = "Settling cannot change a PLU on a scale. To change a PLU, use Change this item. Or ask Bart."
    elif code in TABLE_DRIFT_WORDS:
        text = f"{TABLE_DRIFT_WORDS[code]['blocked']} {TABLE_DRIFT_WORDS[code]['more']}"
    elif code == "reload_differences":
        text = ("A reload would change prices or items on the scale from what it last showed. "
                "A person at the command line must look at the list first.")
    elif code == "delete_not_available":
        text = "Deleting is not available right now. Ask Bart."
    elif code == "scale_may_be_frozen":
        # From the library, when a scale stopped answering between the page's own check and the apply lock.
        names = [n for n in (error.get("scales") or []) if isinstance(n, str)]
        text = frozen_send_refusal(names, how) if names else f"{DID_NOT_ANSWER} A scale did not answer earlier, so nothing was read or sent."
    if text is None:
        return error
    return {**error, "message": text}


def plain_errors(errors: list[dict[str, Any]], unfinished: bool = False, how: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return [plain_error(e, unfinished, how) for e in errors]


def _list_text(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def plu_roles(plu: str, old_plus: Any, repeat: dict[str, Any]) -> tuple[str, list[str]]:
    """What sending a repeat of a PLU change really does: (the PLU every scale gets, the PLUs that stop working).

    plu is the PLU the catalog holds, and old_plus the PLUs a scale may hold instead. The repeat says what is sent.
    Usually the catalog has already moved, so the repeat names the catalog's PLU and the old ones stop. But when a
    reply was lost the catalog has not moved: the repeat names the new PLU, and the catalog's PLU is what stops.
    """
    target = repeat.get("plu") if isinstance(repeat.get("plu"), str) else plu
    stopping = [p for p in [plu, *old_plus] if p != target]
    seen: list[str] = []
    for p in stopping:
        if p not in seen:
            seen.append(p)
    return target, seen


ROLL_UP_ABOVE = 5  # more missed changes than this on one scale get one card, with every row still in the data
DID_NOT_ANSWER = "Did not answer. Tell Bart."
COULD_NOT_CONNECT = "Could not connect. Tell Bart."
CHECK_THEN_CALL = "Check this scale, then call Bart."
LIST_FAILED_TEXT = "The list of work not finished could not be built."
LIST_FAILED_MORE = "Do not take this as all clear. Tell Bart."


def frozen_more(names: list[str]) -> str:
    where = _list_text(names)
    return f"Do not check or change {where} until someone has looked at it."


def frozen_refusal(names: list[str], how: dict[str, str] | None = None) -> dict[str, Any]:
    """The page refuses a review when every scale it would reach may be frozen, or a settle on such a scale.
    Bart clears it through Claude or the command line. how is frozen_how, so a failed connect says so."""
    how = how or {}
    where = _list_text(names)
    stop = COULD_NOT_CONNECT if names and all(how.get(n) == "unreachable" for n in names) else DID_NOT_ANSWER
    if any(how.get(n) == "unreachable" for n in names):
        why = f"{_silence_text(names, how, 'earlier')} So nothing was read or sent."
    else:
        why = f"{where} did not answer earlier, so nothing was read or sent."
    return {
        "code": "scale_may_be_frozen", "index": None, "field": None, "may_be_frozen": False, "scales": list(names),
        "message": f"{stop} {why} {frozen_more(names)}",
    }


def frozen_send_refusal(names: list[str], how: dict[str, str] | None = None) -> str:
    """A review that would reach a scale that stopped answering after it was made. A new review leaves that scale out."""
    where = _list_text(names)
    return f"{frozen_refusal(names, how)['message']} Go back and review it again. {where} will be left out."


def stop_words(how: str | None = None) -> str:
    """The first words for a scale that may be frozen. how is unreachable when the connect failed, which can be
    the WiFi or a frozen scale. Either way nothing reads it until someone has looked."""
    return COULD_NOT_CONNECT if how == "unreachable" else DID_NOT_ANSWER


def frozen_scale_card(scale: str, how: str | None = None) -> dict[str, Any]:
    """A scale that gave no reply, with nothing unfinished on it and no read since that answered. The reply may
    have been missed on a change, a check or a review."""
    if how == "unreachable":
        text = f"{scale} could not be reached earlier, and nothing has read it since. It may be off the WiFi, or it may be frozen."
    else:
        text = f"{scale} did not answer earlier, and nothing has read it since."
    return {"kind": "frozen_scale", "item_id": None, "plu": None, "scales": [scale], "old_plus": [], "repeat": {}, "target": None,
            "stopping": [], "rolled_up": False, "no_fix": True, "button": None, "how": how or "unknown",
            "text": text,
            "more": f"{stop_words(how)} {frozen_more([scale])} A change goes to the other scales only."}


def frozen_read_row(scale: str, how: str | None = None) -> dict[str, Any]:
    """A scale a check left out, because it did not answer earlier, or could not be reached."""
    return {"scale": scale, "status": "not_contacted", "severity": BAD, "may_be_frozen": False,
            "text": f"{scale}: {stop_words(how)} It was not read now. {frozen_more([scale])}"}


def _plu_phrase(plu: Any) -> str:
    return f"PLU {plu}" if _FOUR_DIGITS.fullmatch(str(plu)) else "an item"


def _missed_kind(u: Any) -> str:
    """Which words a missed change gets. Each of these needs its own card, because each asks for something different."""
    if getattr(u, "may_be_frozen", False) is True:
        return "frozen"
    if getattr(u, "catalog_took_it", True) is not True:
        return "not_taken"
    return "fixable"


def _missed_card(u: Any, how: str | None = None) -> dict[str, Any]:
    """One missed change on one scale."""
    plu_text = _plu_phrase(u.plu)
    card: dict[str, Any] = {
        "kind": u.kind, "item_id": u.item_id, "plu": u.plu, "scales": [u.scale], "old_plus": [], "repeat": dict(u.repeat),
        "target": u.plu, "stopping": [], "rolled_up": False, "no_fix": False, "button": None,
    }
    kind = _missed_kind(u)
    if kind == "frozen":
        card["no_fix"] = True
        card["text"] = f"The newest change to {plu_text} has not been confirmed on {u.scale}."
        card["more"] = f"{stop_words(how)} Do not check or change {u.scale} until someone has looked at it."
    elif kind == "not_taken":
        card["no_fix"] = True
        card["text"] = f"A change to {plu_text} did not finish, and our list did not take it."
        card["more"] = f"{u.scale} may hold the new value. {CHECK_THEN_CALL} Putting our list on {u.scale} would put the old value back."
    elif getattr(u, "differs_in", ()):
        card["text"] = f"{u.scale} may differ from our list on {plu_text}."
        card["more"] = f"The last record sent to {u.scale} did not bring it to our list. Nobody may have read it since. Look at the scale."
        card["button"] = f"Fix this on {u.scale}"
    else:
        card["text"] = f"The newest change to {plu_text} has not been confirmed on {u.scale}."
        card["more"] = f"Until it is, {u.scale} may have the old price or may not have the item. Look at the scale."
        card["button"] = f"Fix this on {u.scale}"
    return card


def _rolled_up_card(scale: str, rows: list[Any], kind: str, how: str | None = None) -> dict[str, Any]:
    """Many missed changes on one scale, such as a failed reload. One check of that scale clears any that match."""
    count = len(rows)
    card: dict[str, Any] = {
        "kind": "missed_change", "item_id": None, "plu": None, "scales": [scale], "old_plus": [], "repeat": {}, "target": None, "stopping": [],
        "rolled_up": True, "no_fix": True, "button": None, "count": count,
        "rows": [{"plu": u.plu, "item_id": u.item_id, "status": u.status, "change_id": u.change_id} for u in rows],
    }
    if kind == "frozen":
        card["text"] = f"{count} items have not been confirmed on {scale}."
        card["more"] = f"{stop_words(how)} Do not check or change {scale} until someone has looked at it."
    elif kind == "not_taken":
        card["text"] = f"{count} changes did not finish, and our list did not take them."
        card["more"] = f"{scale} may hold the new values. {CHECK_THEN_CALL} Putting our list on {scale} would put the old values back."
    else:
        card["text"] = f"{count} items have not been confirmed on {scale}."
        card["more"] = f"Tell Bart. One check of {scale} clears any that match our list, and a reload at the command line puts them all right."
    return card


def missed_settle_words(u: Any, read_since: bool, differs: list[str]) -> dict[str, Any]:
    """For the item page and the settle screen, when this scale has a missed change.

    Nobody knows what it holds unless a read came after that change, so it never says it holds something different until then.
    """
    scale = u.scale
    if getattr(u, "differs_in", ()):
        status_text = f"The last record sent to {scale} did not bring it to our list."
    else:
        status_text = f"Last change on {scale}: {status_words(u.status)[0]}"
    kind = _missed_kind(u)
    if kind == "frozen":
        return {"kind": kind, "status_text": status_text, "stop_text": f"{DID_NOT_ANSWER} Do not check or change {scale} until someone has looked at it.", "read_since": read_since, "text": ""}
    if kind == "not_taken":
        return {
            "kind": kind, "status_text": status_text, "read_since": read_since, "text": "",
            "stop_text": f"Our list did not take this change. {scale} may hold the new value. {CHECK_THEN_CALL} Putting our list on {scale} would put the old value back.",
        }
    if read_since and differs:
        text = f"When {scale} was last read, it differed from our list in: {field_list(differs)}."
    else:
        text = f"{scale} may differ from our list. Nobody has read it since this change."
    return {"kind": kind, "status_text": status_text, "read_since": read_since, "text": text, "stop_text": ""}


def unfinished_cards(work: list[Any], how: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Changes some scale has not confirmed, one card for each item, in staff words.

    This is from the catalog with no scale contact, so it says what may be so. The old PLU is only
    called gone once the scale has confirmed it. A repeat of a delete names the PLU, never the id, so
    it can only ever hit that item: if the id now holds something else, the library refuses it.
    A missed change is settled one scale at a time, so it gets one card for each scale, and many on one
    scale roll up into one card. One the catalog did not take, or from a scale that did not answer,
    has no fix button, because tapping it would undo the change or read that scale again.
    """
    groups: dict[tuple[str, int, Any, Any], dict[str, Any]] = {}
    missed: dict[str, dict[str, list[Any]]] = {}
    first: list[dict[str, Any]] = []
    for u in work:
        if u.kind == "list_failed":
            first.append({"kind": u.kind, "item_id": None, "plu": None, "scales": [], "old_plus": [], "repeat": {}, "target": None, "stopping": [],
                          "rolled_up": False, "no_fix": True, "button": None, "text": LIST_FAILED_TEXT, "more": LIST_FAILED_MORE})
            continue
        if u.kind == "missed_change":
            missed.setdefault(u.scale, {}).setdefault(_missed_kind(u), []).append(u)
            continue
        card = groups.setdefault((u.kind, u.item_id, u.repeat.get("plu"), u.repeat.get("scale")), {
            "kind": u.kind, "item_id": u.item_id, "plu": u.plu, "scales": [], "old_plus": [], "repeat": dict(u.repeat), "frozen": [],
        })
        card["scales"].append(u.scale)
        if getattr(u, "may_be_frozen", False) is True:
            card["frozen"].append(u.scale)
        for old in u.old_plus:
            if old not in card["old_plus"]:
                card["old_plus"].append(old)
    cards = first
    for card in groups.values():
        where = _list_text(card["scales"])
        if card["kind"] == "delete":
            card["repeat"] = {"action": "delete", "target_plu": card["plu"]}
            card["target"] = card["plu"]
            card["stopping"] = [card["plu"]]
            card["text"] = f"The delete of PLU {card['plu']} has not been confirmed on {where}."
            card["more"] = f"Until it is, assume PLU {card['plu']} still works on {where}. Look at the scale."
            card["button"] = f"Repeat the delete of PLU {card['plu']}"
        else:
            target, stopping = plu_roles(card["plu"], card["old_plus"], card["repeat"])
            card["target"], card["stopping"] = target, [p for p in stopping if _FOUR_DIGITS.fullmatch(str(p))]
            stop_text = _list_text([f"PLU {p}" for p in card["stopping"]]) if card["stopping"] else "the old PLU"
            if target == card["plu"]:
                card["text"] = f"The PLU change to {target} has not been confirmed on {where}."
                card["more"] = f"Until it is, assume {stop_text} still works on {where}. Look at the scale."
            else:
                card["text"] = f"The PLU change from {card['plu']} to {target} may have landed on {where}, and nobody has confirmed it."
                card["more"] = (f"Our list still says {card['plu']}. {where} may be on {target} or on {card['plu']}, "
                                "and both may work there until this is finished. Look at the scale.")
            card["button"] = f"Repeat the PLU change to {target}"
        frozen = card.pop("frozen")
        card["no_fix"] = bool(frozen)
        if frozen:
            # A repeat reads every scale it names, and one of them did not answer.
            card["button"] = None
            all_unreachable = all((how or {}).get(name) == "unreachable" for name in frozen)
            card["more"] = f"{stop_words('unreachable' if all_unreachable else None)} {frozen_more(frozen)}"
        cards.append(card)
    for scale, kinds in missed.items():
        for kind, rows in kinds.items():
            if len(rows) > ROLL_UP_ABOVE:
                cards.append(_rolled_up_card(scale, rows, kind, (how or {}).get(scale)))
            else:
                cards.extend(_missed_card(u, (how or {}).get(u.scale)) for u in rows)
    return cards

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

