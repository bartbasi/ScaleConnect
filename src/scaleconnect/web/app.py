"""The ScaleConnect web page. It calls ScaleConnectService and nothing below it.

Reading and searching need no PIN. Previews and sends do. Every POST must come from this
page: same origin, a session cookie and a CSRF token in a header.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, StrictBool

from scaleconnect.config import Config
from scaleconnect.errors import ConfigError, ScaleConnectError

from . import words
from .jobs import SendBusy, SendJobs
from .security import CSRF_HEADER, MIN_PIN_LENGTH, SESSION_COOKIE, Auth, allowed_host_headers, same_origin

log = logging.getLogger("scaleconnect.web")
STATIC = Path(__file__).resolve().parent / "static"

STATUS_FOR_CODE = {
    "item_not_found": 404,
    "change_not_found": 404,
    "preview_not_found": 404,
    "validation_failed": 400,
    "acknowledgement_required": 400,
    "preview_expired": 409,
    "preview_stale": 409,
    "preview_already_applied": 409,
    "interrupted_change": 409,
    "apply_busy": 409,
    "config_changed": 409,
    "target_changed": 409,
    "scale_may_be_frozen": 409,
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

ADD_KEYS = {"plu", "description", "price", "sold_by", "replace_orphan_id"}
EDIT_KEYS = {"target_plu", "description", "price", "plu"}
DELETE_KEYS = {"target_plu"}  # a repeat of a delete names the PLU, never the id, so it cannot hit what the id holds now
ALLOWED_KEYS = {"add": ADD_KEYS, "edit": EDIT_KEYS, "delete": DELETE_KEYS}


class LoginBody(BaseModel):
    pin: str = Field(max_length=200)


class PreviewBody(BaseModel):
    changes: list[dict[str, Any]] = Field(max_length=25)
    read: bool = False


class SendBody(BaseModel):
    preview_id: str = Field(max_length=100)
    acknowledge_warnings: StrictBool = False  # only a real true counts, never "yes" or 1
    acknowledge_drift: list[str] = Field(default_factory=list, max_length=50)


class SettleBody(BaseModel):
    scale: str = Field(max_length=40)
    take: str = Field(max_length=10)
    plu: str = Field(max_length=10)


class CheckBody(BaseModel):
    plu: str = Field(max_length=10)


class OrphanBody(BaseModel):
    scale: str = Field(max_length=40)
    id: int = Field(ge=1, le=500)
    note: str = Field(default="", max_length=100)


class ResolveBody(BaseModel):
    change_id: str = Field(max_length=100)
    note: str = Field(min_length=3, max_length=200)

def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


def _clean_changes(raw: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Keep only what this page may send, tidy the text, and say plainly what is wrong."""
    clean: list[dict[str, str]] = []
    problems: list[dict[str, Any]] = []
    if not raw:
        problems.append({"code": "no_changes", "message": "There is nothing to change.", "index": None, "field": None})
    for index, entry in enumerate(raw):
        action = entry.get("action")
        allowed = ALLOWED_KEYS.get(action) if isinstance(action, str) else None
        if allowed is None:
            problems.append({"code": "invalid_action", "message": "This kind of change is not allowed here.", "index": index, "field": "action"})
            continue
        out: dict[str, Any] = {"action": action}
        bad = False
        for key, value in entry.items():
            if key == "action":
                continue
            if key not in allowed:
                problems.append({"code": "invalid_field", "message": f"The field {key} cannot be sent from this page.", "index": index, "field": key})
                bad = True
            elif key == "replace_orphan_id":
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
                    problems.append({"code": "invalid_id", "message": "The scale id must be a whole number from 1 to 500.", "index": index, "field": key})
                    bad = True
                else:
                    out[key] = value
            elif not isinstance(value, str):
                label = "price" if key == "price" else key
                problems.append({"code": f"invalid_{label}" if key in ("price", "plu") else "invalid_field", "message": f"The {label} must be typed as text.", "index": index, "field": key})
                bad = True
            elif key == "price":
                price, why = words.normalize_price(value)
                if price is None:
                    problems.append({"code": "invalid_price", "message": why, "index": index, "field": "price"})
                    bad = True
                else:
                    out["price"] = price
            else:
                out[key] = value.strip()
        if not bad:
            clean.append(out)
    return clean, problems


def create_app(
    service: Any,
    config: Config,
    *,
    plu_edit_enabled: bool = False,
    early_wait_seconds: float = 2.0,
    max_pin_failures: int = 5,
    lock_seconds: float = 60.0,
    clock: Any = None,
    allowed_hosts: Any = None,
    stalled_after_seconds: float = 600.0,
) -> FastAPI:
    """Build the page. Refuses to build without a PIN.

    plu_edit_enabled: when True the edit screen lets staff change the PLU. Code always follows
    the PLU, so the page never shows Code as a field. The library still has the last word.
    allowed_hosts: the Host header values to answer. Default: the configured web host and port.
    stalled_after_seconds: a change with no finish, that this program is not running, older
    than this is shown as stopped, not as working.
    """
    if not config.pin:
        raise ConfigError("No PIN is set in the config, so the web page will not start. Set [auth] pin.")
    if len(config.pin.strip()) < MIN_PIN_LENGTH:
        raise ConfigError(f"The PIN is too short. Use at least {MIN_PIN_LENGTH} characters.")
    auth_kw: dict[str, Any] = {"max_failures": max_pin_failures, "lock_seconds": lock_seconds}
    if clock is not None:
        auth_kw["clock"] = clock
    auth = Auth(config.pin, **auth_kw)
    jobs = SendJobs(service)
    # preview id -> what the page needs to remember about it
    previews: OrderedDict[str, dict[str, Any]] = OrderedDict()
    description_max = config.settings.description_max_length
    hosts = {h.lower() for h in (allowed_hosts if allowed_hosts is not None
                                  else allowed_host_headers(config.web.host, config.web.port))}

    app = FastAPI(title="ScaleConnect", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.jobs = jobs

    # ------------------------------------------------------------ plumbing

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.headers.get("host", "").strip().lower() not in hosts:
            # Stops DNS rebinding: only the page's own address and port are answered.
            refused = _error(421, "wrong_host", "This is not the address of the scale page.")
            refused.headers["Cache-Control"] = "no-store"
            return refused
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ScaleConnectError)
    async def library_error(request: Request, exc: ScaleConnectError):
        status = STATUS_FOR_CODE.get(exc.code, 500)
        body = exc.to_dict()
        return JSONResponse({"error": body}, status_code=status)

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def origin_problem(request: Request) -> JSONResponse | None:
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return _error(403, "cross_site", "This request did not come from the ScaleConnect page.")
        if not same_origin(request.headers.get("origin"), request.headers.get("host")):
            return _error(403, "cross_site", "This request did not come from the ScaleConnect page.")
        return None

    def change_guard(request: Request) -> JSONResponse | None:
        """For anything that changes things: same origin, a PIN session, and the CSRF token."""
        problem = origin_problem(request)
        if problem:
            return problem
        token = request.cookies.get(SESSION_COOKIE)
        if auth.csrf_for(token) is None:
            return _error(401, "pin_required", "Enter the PIN to make changes.")
        if not auth.csrf_ok(token, request.headers.get(CSRF_HEADER)):
            return _error(403, "bad_csrf", "The page is out of date. Reload it and try again.")
        return None

    # ------------------------------------------------------------ pages

    def static_file(name: str, kind: str) -> Response:
        return FileResponse(STATIC / name, media_type=kind)

    @app.get("/", include_in_schema=False)
    def index():
        return static_file("index.html", "text/html; charset=utf-8")

    @app.get("/static/app.js", include_in_schema=False)
    def script():
        return static_file("app.js", "text/javascript; charset=utf-8")

    @app.get("/static/style.css", include_in_schema=False)
    def style():
        return static_file("style.css", "text/css; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    # ------------------------------------------------------------ session

    def config_notice() -> bool:
        """True if the config file now says something this running page did not load. Reading it changes nothing."""
        try:
            return service.config_changed() is not None
        except Exception:  # noqa: BLE001 - a notice must never stop the page from loading
            return False

    @app.get("/api/state")
    def state(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        csrf = auth.csrf_for(token)
        caps = service.capabilities().to_dict()
        # Only what the page needs. No messages from the scales, which can hold serial numbers.
        scales = [
            {"name": s.name, "write_enabled": s.write_enabled, "read_enabled": s.read_enabled,
             "last_status": s.last_status, "interrupted_change_ids": list(s.interrupted_change_ids)}
            for s in service.list_scales()
        ]
        return {
            "signed_in": csrf is not None,
            "csrf": csrf,
            "capabilities": caps,
            "scales": scales,
            "busy": jobs.busy(),
            "description_max_length": description_max,
            "plu_edit_enabled": plu_edit_enabled,
            "delete_note": "" if caps.get("delete") else "Deleting is not available right now. Ask Bart.",
            "config_notice": words.CONFIG_NOTICE if config_notice() else None,
        }
    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        problem = origin_problem(request)
        if problem:
            return problem
        result = auth.login(client_of(request), body.pin)
        if isinstance(result, int):
            if result > 0:
                return _error(429, "locked", f"Too many wrong tries. Wait {result} seconds, then try again.", retry_after=result)
            return _error(401, "wrong_pin", "That PIN is not right.")
        token, csrf = result
        response = JSONResponse({"signed_in": True, "csrf": csrf})
        response.set_cookie(
            SESSION_COOKIE, token, max_age=auth.session_seconds, httponly=True, samesite="strict", path="/"
        )
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        problem = origin_problem(request)
        if problem:
            return problem
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"signed_in": False})
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    # ------------------------------------------------------------ reading

    @app.get("/api/search")
    def search(q: str = "", limit: int = 40):
        limit = max(1, min(limit, 100))
        return {"items": [words.item_card(i) for i in service.search_items(q.strip()[:100], limit=limit)]}

    def frozen_scales(unfinished: Any = None, among: Any = None) -> list[str]:
        """Scales this page would reach that may be frozen, from the catalog and its log, with no scale contact.
        That is a logged change or reload, or any read, that got no reply, with no read since that answered.
        The page reads and sends nothing to them. A review leaves them out, and the change goes to the others.
        Bart clears it through Claude or the command line: a compare of that scale that answers, or a reload."""
        reached = [s.name for s in service.list_scales() if s.read_enabled]
        names = set(service.scales_may_be_frozen(unfinished))
        wanted = set(among) if among else set(reached)
        return [n for n in reached if n in names and n in wanted]

    def frozen_in(data: dict[str, Any]) -> bool:
        """A review or settle that met a scale that may be frozen, in its warnings or errors."""
        return any(x.get("may_be_frozen") or x.get("code") == "scale_may_be_frozen" for x in [*data.get("warnings", []), *data.get("errors", [])])

    def add_reboot_words(data: dict[str, Any], how: dict[str, str]) -> None:
        """The words shown for a warning or error that may mean a frozen scale. A failed connect gets its own words.
        The scale is picked out by its name at the start of the library's message, which only holds config names."""
        names = scale_names()
        for x in [*data.get("warnings", []), *data.get("errors", [])]:
            if x.get("may_be_frozen"):
                said = str(x.get("message") or "")
                scale = next((n for n in names if said.startswith(f"{n}:")), None)
                x["reboot"] = words.reboot_text(None, how.get(scale) if scale else None)

    def error_how(error: Any) -> dict[str, str]:
        return service.frozen_how() if isinstance(error, dict) and error.get("code") == "scale_may_be_frozen" else {}

    @app.get("/api/item/{plu}")
    def item(plu: str):
        view = service.get_item(plu=plu)
        shown = view.item
        # From the catalog, with no scale contact. A scale behind a PLU change must be finished, never settled.
        unfinished_now = service.list_unfinished()
        frozen_now = frozen_scales(unfinished_now)
        how = service.frozen_how() if frozen_now else {}
        behind = {(u.scale, u.item_id): u for u in unfinished_now if u.kind == "plu_change"}
        missed = {(u.scale, u.item_id): u for u in unfinished_now if u.kind == "missed_change"}
        rows = []
        for info in service.list_scales():
            state_ = view.scales.get(info.name)
            row: dict[str, Any] = {
                "scale": info.name,
                "can_change": info.write_enabled,
                "can_read": info.read_enabled,
                "flag": "none",
                "text": "Nothing is recorded for this scale yet.",
                "differs": [],
                "last_read": None,
                "last_read_at": None,
                "can_fix": False,
                "repeat": None,
                "repeat_meta": None,
                "missed": None,
            }
            if state_ is not None:
                read = state_.last_read
                if read is not None:
                    differs = words.differing_fields(read, shown)
                    row.update(last_read=words.item_card(read), last_read_at=state_.last_read_at, differs=differs)
                    if not differs:
                        row.update(flag="same", text="Matches our list when last read.")
                    elif "plu" in differs or "code" in differs:
                        row.update(flag="different_product", can_fix=info.write_enabled, text=(
                            f"This scale holds a DIFFERENT PRODUCT at this number (PLU {read.plu}). "
                            "Do not change it from here. Call Bart."))
                    else:
                        row.update(flag="different", can_fix=info.read_enabled, text=(
                            "This scale holds something different: " + words.field_list(differs) + "."))
                elif state_.last_read_at:
                    row.update(flag="missing", last_read_at=state_.last_read_at, can_fix=info.write_enabled,
                               text="The last read found nothing at this number on this scale. Call Bart.")
                elif state_.status:
                    words_, _ = words.status_words(state_.status)
                    row.update(flag="unsure", text=f"Last change: {words_}")
                if state_.belief == "unknown" and row["flag"] not in ("different", "different_product", "missing"):
                    row.update(flag="unsure", text="Not sure what is on this scale. " + (row["text"] if row["flag"] == "unsure" else ""))
            late = behind.get((info.name, shown.id))
            if late is not None:
                target, stopping = words.plu_roles(late.plu, late.old_plus, late.repeat)
                stopping = [p for p in stopping if p.isdigit() and len(p) == 4]
                if target == late.plu:
                    old_text = ", ".join(stopping) if stopping else "the old PLU"
                    said = (f"An earlier PLU change did not finish on this scale. The old PLU ({old_text}) may still work on it. "
                            "Repeat the PLU change. Do not settle it.")
                else:
                    said = (f"An earlier PLU change to {target} may have landed on this scale, and nobody has confirmed it. "
                            f"It may be on {target} or still on {late.plu}. Repeat the PLU change. Do not settle it.")
                row.update(flag="unfinished", can_fix=False, repeat=dict(late.repeat), text=said,
                           repeat_meta={"plu": target, "old": stopping})
            elif (info.name, shown.id) in missed:
                # An old read that matched, or differed, says nothing about a change made since. Settling is how this one ends.
                miss = missed[(info.name, shown.id)]
                read_since = read_after(row["last_read_at"], miss.at)
                said = words.missed_settle_words(miss, read_since, row["differs"])
                row["missed"] = said
                if said["kind"] != "fixable":
                    row.update(flag="unsure", can_fix=False, text=said["stop_text"])
                else:
                    if row["flag"] in ("none", "same", "unsure") or not read_since:
                        row.update(flag="unsure", text=f"{said['text']} Look at the scale.")
                    row.update(can_fix=row["can_fix"] or info.write_enabled)
            if info.name in frozen_now:
                # Every button on this row would read that scale, so none is offered, of any kind.
                stop = f"{words.stop_words(how.get(info.name))} {words.frozen_more([info.name])}"
                row.update(flag="frozen", can_fix=False, repeat=None, repeat_meta=None, text=stop, frozen_how=how.get(info.name, "unknown"))
                if row["missed"] is not None:
                    row["missed"] = {**row["missed"], "kind": "frozen", "stop_text": stop, "text": ""}
            rows.append(row)
        interrupted = []
        for change_id in view.interrupted_change_ids:
            try:
                record = service.get_change(change_id)
            except ScaleConnectError:
                continue
            interrupted.append({"change_id": change_id, "preview_id": record.preview_id, "started_at": record.started_at})
        return {
            "item": words.item_card(shown),
            "scales": rows,
            "interrupted": interrupted,
            "sending_now": jobs.busy(),
            "frozen_scales": frozen_now,
            "frozen_how": {name: how.get(name, "unknown") for name in frozen_now},
        }

    # ------------------------------------------------------------ preview

    def read_after(read_at: str | None, change_at: str | None) -> bool:
        """The read came after the change. Times are to the second, so the same second does not count."""
        try:
            return datetime.fromisoformat(read_at or "") > datetime.fromisoformat(change_at or "")
        except (TypeError, ValueError):
            return False

    def scale_names() -> list[str]:
        return [s.name for s in service.list_scales()]

    def remember(result: Any, data: dict[str, Any], settle: bool = False) -> dict[str, Any]:
        """Mark each drift as one a person may acknowledge or not, and remember the ones they may not."""
        blocked: set[str] = set()
        for warning in data.get("warnings", []):
            # A read with no reply may mean the scale froze. The library says so in may_be_frozen. The page says it plainly.
            if warning.get("code") == "gap_filled":
                warning["plain"] = words.gap_text(warning.get("message"))
            # Which scales this note is about, so the review does not say "Nothing is different" beside it.
            warning["scales_named"] = [n for n in scale_names() if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", str(warning.get("message") or ""))]
        for drift in data.get("drift", []):
            drift["different_product"] = words.is_different_product(drift)
            # Settling is how a person resolves a different product, so it keeps its token. A change never does.
            drift["can_ack"] = bool(drift.get("ack_token")) and (settle or not drift["different_product"])
            drift["unfinished_move"] = words.is_unfinished_move(drift)
            if settle and drift.get("kind") == "missing":
                # Pushing the list onto a scale that holds nothing there also brings back an item whose delete did not finish.
                drift["plain_note"] = "If a delete of this item did not finish, do not settle. Open the item and tap Delete this item instead."
            drift["plain"] = words.drift_words(drift)  # staff words for a drift nobody may override, or None
            if drift.get("ack_token") and not drift["can_ack"]:
                blocked.add(drift["ack_token"])
        if result.ok and result.preview_id:
            previews[result.preview_id] = {"scales": tuple(result.scales), "blocked": blocked}
            while len(previews) > 200:
                previews.popitem(last=False)
        return data

    @app.post("/api/preview")
    def preview(body: PreviewBody, request: Request):
        problem = change_guard(request)
        if problem:
            return problem
        changes, problems = _clean_changes(body.changes)
        frozen = frozen_scales()
        if frozen and not problems and not any(s.write_enabled for s in service.list_scales() if s.name not in frozen):
            # Every scale this change would reach did not answer, so nothing is read or sent from here.
            problems = [words.frozen_refusal(frozen, service.frozen_how())]
        if problems:
            return {
                "ok": False, "preview_id": None, "created_at": None, "expires_at": None, "requested_by": "web",
                "scales": [], "differences": [], "warnings": [], "errors": problems, "drift": [], "scales_read": [],
                "left_out": [], "left_out_text": None,
            }
        # A scale that did not answer is left out, under the apply lock too, and the change goes to the others.
        result = service.preview_changes(changes, requested_by="web", read_scales=body.read, avoid_frozen=True)
        data = result.to_dict()
        how = service.frozen_how() if result.left_out or frozen_in(data) else {}
        data["errors"] = words.plain_errors(data.get("errors", []), how=how)
        data["left_out_text"] = words.left_out_text(list(result.left_out), how) if result.left_out else None
        # One line per left out scale on the review, in words that say how it stopped answering.
        data["left_out_rows"] = {name: words.left_out_review_text(how.get(name)) for name in result.left_out}
        add_reboot_words(data, how)
        return remember(result, data)

    @app.post("/api/settle/preview")
    def settle_preview(body: SettleBody, request: Request):
        """Bart picks which side wins for one item on one scale. This reads the scale now."""
        problem = change_guard(request)
        if problem:
            return problem
        frozen = frozen_scales(among=[body.scale])
        if frozen:
            return {
                "ok": False, "preview_id": None, "created_at": None, "expires_at": None, "requested_by": "web",
                "scales": [], "differences": [], "warnings": [], "errors": [words.frozen_refusal(frozen, service.frozen_how())], "drift": [],
                "scales_read": [], "repeat": None,
            }
        result = service.preview_settle_drift(scale=body.scale, take=body.take, plu=body.plu, requested_by="web", avoid_frozen=True)
        data = result.to_dict()
        behind = [d for d in data.get("drift", []) if words.is_unfinished_move(d)]
        unfinished = bool(behind) or any(e.get("code") == "plu_change_not_finished" for e in data.get("errors", []))
        how = service.frozen_how() if frozen_in(data) else {}
        data["errors"] = words.plain_errors(data.get("errors", []), unfinished, how)
        add_reboot_words(data, how)
        data["repeat"] = None
        if unfinished:
            # Settling cannot finish a PLU change. Hand the page the repeat that can.
            for u in service.list_unfinished():
                if u.kind == "plu_change" and u.scale == body.scale and (not behind or u.item_id == behind[0].get("item_id")):
                    target, stopping = words.plu_roles(u.plu, u.old_plus, u.repeat)
                    data["repeat"] = {"change": dict(u.repeat), "plu": target, "old": stopping}
                    break
        return remember(result, data, settle=True)

    @app.get("/api/unfinished")
    def unfinished_work():
        """Changes some scale has not confirmed. From the catalog and its log, no scale contact.
        A scale that may be frozen with no row of its own still gets a card, so staff hear it before they try a change."""
        work = service.list_unfinished()
        how = service.frozen_how()  # could not connect, or connected and gave no reply. Both are treated the same.
        cards = words.unfinished_cards(work, how)
        in_rows = {u.scale for u in work if u.may_be_frozen}
        cards.extend(words.frozen_scale_card(name, how.get(name)) for name in frozen_scales(work) if name not in in_rows)
        return {"items": cards}

    @app.post("/api/check")
    def check(body: CheckBody, request: Request):
        """Read this item from each scale now, so the item page shows what they really hold. Writes nothing."""
        problem = change_guard(request)
        if problem:
            return problem
        frozen = frozen_scales()
        # A scale that did not answer earlier is left out, and only the others are read.
        others = [s.name for s in service.list_scales() if s.name not in frozen]
        reads = service.read_scale_items(plus=[body.plu], scales=others, avoid_frozen=True) if others else []
        # No raw messages: they can hold scale details. Staff get plain words, and a reboot flag.
        rows = {r.scale: words.read_row(r.scale, r.status, r.may_be_frozen) for r in reads}
        # The service left out, under the lock, any scale that stopped answering since the line above.
        skipped = [r.scale for r in reads if r.status == "not_contacted" and r.may_be_frozen]
        how = service.frozen_how() if frozen or skipped else {}
        rows.update({name: words.frozen_read_row(name, how.get(name)) for name in [*frozen, *skipped]})
        return {"scales": [rows[s.name] for s in service.list_scales() if s.name in rows]}

    @app.post("/api/orphan")
    def mark_orphan(body: OrphanBody, request: Request):
        """Record that a scale holds something at an id that the item list does not know."""
        problem = change_guard(request)
        if problem:
            return problem
        if body.scale not in {s.name for s in service.list_scales()}:
            return _error(400, "unknown_scale", f"No scale named {body.scale}.")
        note = "".join(ch for ch in body.note if 0x20 <= ord(ch) <= 0x7E).strip() or "marked from the web page"
        service.mark_orphan(body.id, body.scale, note)
        return {"ok": True, "message": f"Id {body.id} on {body.scale} is now marked as holding something we do not list."}

    @app.post("/api/resolve")
    def resolve(body: ResolveBody, request: Request):
        """Close a change that never finished, after a person has looked at the scales."""
        problem = change_guard(request)
        if problem:
            return problem
        record = service.resolve_interrupted_change(body.change_id, body.note.strip(), requested_by="web")
        return {"ok": True, "change_id": record.change_id, "outcome": record.outcome}

    # ------------------------------------------------------------ send

    @app.post("/api/send")
    def send(body: SendBody, request: Request):
        problem = change_guard(request)
        if problem:
            return problem
        known = previews.get(body.preview_id) or {"scales": (), "blocked": set()}
        frozen = frozen_scales(among=known["scales"])
        if frozen and jobs.get(body.preview_id) is None:
            # A review made before that scale stopped answering. Sending would read it before writing.
            return _error(409, "scale_may_be_frozen", words.frozen_send_refusal(frozen, service.frozen_how()), sent=False, scales=frozen)
        # A scale that holds a different product is never forced from here, whatever the page sent.
        tokens = [t for t in body.acknowledge_drift if t not in known["blocked"]]
        try:
            job, started = jobs.start(
                body.preview_id, known["scales"],
                acknowledge_warnings=body.acknowledge_warnings is True,
                acknowledge_drift=tokens,
                requested_by="web",
                avoid_frozen=True,  # checked again under the apply lock, before anything goes out
            )
        except SendBusy:
            return _error(409, "busy", "Another change is being sent right now. Wait for it to finish, then try again.")
        if started:
            job.finished.wait(early_wait_seconds)  # quick refusals come back at once
        if job.state == "error":
            status = STATUS_FOR_CODE.get((job.error or {}).get("code", ""), 500)
            return JSONResponse({"error": words.plain_error(job.error or {}, how=error_how(job.error))}, status_code=status)
        return {"preview_id": body.preview_id, "started": started, "state": job.state}

    def age_seconds(text: str | None) -> float | None:
        try:
            moment = datetime.fromisoformat(text or "")
        except ValueError:
            return None
        return (datetime.now(moment.tzinfo) - moment).total_seconds()

    def is_stalled(record: Any, job: Any) -> bool:
        """No finish was logged, and nothing is sending it. Then it stopped, and must not say Working.

        The library says whether something holds the apply lock now (`running`). If it does
        not say, a change with no finish that is older than a few minutes counts as stopped.
        """
        if record.finished_at is not None and record.outcome != "in_progress":
            return False
        if job is not None:
            return False
        running = getattr(record, "running", None)
        if running is not None:
            return not running
        age = age_seconds(record.started_at)
        return age is not None and age > stalled_after_seconds

    def build_status(preview_id: str) -> dict[str, Any]:
        job = jobs.get(preview_id)
        record = service.get_change_for_preview(preview_id)  # PreviewNotFound becomes a 404
        remembered = previews.get(preview_id)
        expected = list(job.scales if job and job.scales else (remembered["scales"] if remembered else ()))
        out: dict[str, Any] = {
            "preview_id": preview_id, "state": "not_sent", "heading": "", "severity": words.WORKING, "display": "",
            "summary": "", "rows": [], "changes": [], "outcome": None, "success": False, "notes": [],
            "change_id": None, "error": None, "may_have_stopped": False, "requested_by": None, "started_at": None,
            "left_out": [], "left_out_text": None,
        }
        if record is None and job is None:
            out.update(heading="This change has not been sent.", severity=words.WARN)
            return out
        if job is not None and job.state == "error" and record is None:
            job_error = words.plain_error(job.error, how=error_how(job.error))
            out.update(state="error", error=job_error, severity=words.BAD)
            out["heading"] = (
                "The change was not sent. " + job_error["message"] if job_error.get("sent") is False
                else "Not sure what happened. " + job_error["message"]
            )
            return out
        if record is None:
            order = expected or []
            out.update(state="running", heading=words.OUTCOME_WORDS["in_progress"][0], severity=words.WORKING)
            out["rows"] = [words.pending_row(s, i == 0) for i, s in enumerate(order)]
            return out
        if job is not None and job.state == "running" and record.finished_at is not None:
            # The library logs the finish, then frees the lock, and only then does the job end. Until
            # it ends this send still counts as busy, so do not say finished yet.
            record = replace(record, finished_at=None, outcome="in_progress", success=False, verified=False, display="", summary="")

        results = {r.scale: r for r in record.scale_results}
        order = expected or [r.scale for r in record.scale_results]
        for name in results:
            if name not in order:
                order.append(name)
        finished = record.finished_at is not None and record.outcome != "in_progress"
        # Nothing is sending it: it stopped. Do not say Working.
        stalled = is_stalled(record, job)
        if stalled and not order:
            order = [s.name for s in service.list_scales()]  # which scales it reached is not recorded
        rows: list[dict[str, Any]] = []
        first_pending_seen = False
        # How each scale that may be frozen stopped answering, so a failed connect is not called a scale that did not answer.
        how = service.frozen_how() if any(words.is_left_out(r) or r.may_be_frozen for r in record.scale_results) else {}
        for name in order:
            if name in results:
                rows.append(words.scale_row(results[name], how.get(name)))
            elif finished or stalled:
                rows.append(words.missing_row(name))
            else:
                rows.append(words.pending_row(name, not first_pending_seen))
                first_pending_seen = True
        if finished:
            rows.extend(words.not_touched_row(n) for n in record.not_touched)
        # A scale the review left out because it did not answer. Said at the top as well as on its line.
        left = [r.scale for r in record.scale_results if words.is_left_out(r)]
        out["left_out"] = left
        out["left_out_text"] = words.left_out_text(left, how) if left else None
        still = tuple(getattr(record, "still_differ", ()) or ())
        notes = (words.leftover_notes(record) or [d.message for d in still]) if finished else []
        if finished and still and not notes:
            notes = [d.message for d in still]
        if finished:
            statuses = [r["status"] for r in rows if r["status"] != "not_touched"]
            heading, severity = words.result_heading(record.outcome, statuses, bool(notes))
        elif stalled:
            heading, severity = ("STOPPED. This change never finished. Look at every scale. "
                                 "A scale may or may not hold the change."), words.BAD
        else:
            heading, severity = words.heading_for(record.outcome)
        out.update(
            state="finished" if finished else ("stalled" if stalled else "running"),
            heading=heading, severity=severity, display=record.display, summary=record.summary,
            rows=rows, changes=[d.to_dict() for d in record.changes], outcome=record.outcome,
            success=bool(record.success and record.outcome == "success" and not notes),
            running_elsewhere=bool(getattr(record, "running", False)) and job is None and not finished,
            notes=notes,
            change_id=record.change_id, requested_by=record.requested_by, started_at=record.started_at,
            may_have_stopped=(not finished and not stalled and job is None),
        )
        if finished:
            out["after_note"] = words.after_note(record, out["success"], service.settle_ended_a_miss(record))
        return out

    @app.get("/api/send/{preview_id}")
    def send_status(preview_id: str):
        return build_status(preview_id[:100])

    @app.get("/api/changes")
    def recent(limit: int = 20):
        limit = max(1, min(limit, 50))
        out = []
        for record in service.list_changes(limit):
            finished = record.finished_at is not None and record.outcome != "in_progress"
            if finished:
                statuses = ["left_out" if words.is_left_out(r) else r.status for r in record.scale_results]
                left = bool(words.leftover_notes(record) or getattr(record, "still_differ", ()))
                heading, severity = words.result_heading(record.outcome, statuses, left)
            elif is_stalled(record, jobs.get(record.preview_id)):
                heading, severity = "STOPPED. This change never finished. Look at the scales.", words.BAD
            else:
                heading, severity = words.heading_for(record.outcome)
            out.append({
                "preview_id": record.preview_id, "change_id": record.change_id, "started_at": record.started_at,
                "finished_at": record.finished_at, "outcome": record.outcome, "success": record.success,
                "heading": heading, "severity": severity, "display": record.display,
                "what": [{"plu": d.plu, "action": d.action} for d in record.changes],
            })
        return {"changes": out}

    return app