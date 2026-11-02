"""The ScaleConnect web page. It calls ScaleConnectService and nothing below it.

Reading and searching need no PIN. Previews and sends do. Every POST must come from this
page: same origin, a session cookie and a CSRF token in a header.
"""

from __future__ import annotations

import logging
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
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

ADD_KEYS = {"plu", "description", "price", "sold_by", "replace_orphan_id"}
EDIT_KEYS = {"target_plu", "description", "price", "plu"}
DELETE_KEYS = {"target_plu", "target_id"}  # target_id only for repeating a delete the catalog no longer lists
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
            elif key in ("replace_orphan_id", "target_id"):
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

    @app.get("/api/item/{plu}")
    def item(plu: str):
        view = service.get_item(plu=plu)
        shown = view.item
        # From the catalog, with no scale contact. A scale behind a PLU change must be finished, never settled.
        behind = {(u.scale, u.item_id): u for u in service.list_unfinished() if u.kind == "plu_change"}
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
                olds = [o for o in late.old_plus if o.isdigit() and len(o) == 4]
                old_text = ", ".join(olds) if olds else "the old PLU"
                row.update(flag="unfinished", can_fix=False, repeat=dict(late.repeat), text=(
                    f"An earlier PLU change did not finish on this scale. The old PLU ({old_text}) may still work on it. "
                    "Repeat that PLU change. Do not settle it."))
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
        }

    # ------------------------------------------------------------ preview

    def remember(result: Any, data: dict[str, Any], settle: bool = False) -> dict[str, Any]:
        """Mark each drift as one a person may acknowledge or not, and remember the ones they may not."""
        blocked: set[str] = set()
        for warning in data.get("warnings", []):
            # A read with no reply may mean the scale froze. The library says so in may_be_frozen. The page says it plainly.
            if warning.get("code") == "gap_filled":
                warning["plain"] = words.gap_text(warning.get("message"))
        for drift in data.get("drift", []):
            drift["different_product"] = words.is_different_product(drift)
            # Settling is how a person resolves a different product, so it keeps its token. A change never does.
            drift["can_ack"] = bool(drift.get("ack_token")) and (settle or not drift["different_product"])
            drift["unfinished_move"] = words.is_unfinished_move(drift)
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
        if problems:
            return {
                "ok": False, "preview_id": None, "created_at": None, "expires_at": None, "requested_by": "web",
                "scales": [], "differences": [], "warnings": [], "errors": problems, "drift": [], "scales_read": [],
            }
        result = service.preview_changes(changes, requested_by="web", read_scales=body.read)
        data = result.to_dict()
        data["errors"] = words.plain_errors(data.get("errors", []))
        return remember(result, data)

    @app.post("/api/settle/preview")
    def settle_preview(body: SettleBody, request: Request):
        """Bart picks which side wins for one item on one scale. This reads the scale now."""
        problem = change_guard(request)
        if problem:
            return problem
        result = service.preview_settle_drift(scale=body.scale, take=body.take, plu=body.plu, requested_by="web")
        data = result.to_dict()
        behind = [d for d in data.get("drift", []) if words.is_unfinished_move(d)]
        unfinished = bool(behind) or any(e.get("code") == "plu_change_not_finished" for e in data.get("errors", []))
        data["errors"] = words.plain_errors(data.get("errors", []), unfinished)
        data["repeat"] = None
        if unfinished:
            # Settling cannot finish a PLU change. Hand the page the repeat that can.
            for u in service.list_unfinished():
                if u.kind == "plu_change" and u.scale == body.scale and (not behind or u.item_id == behind[0].get("item_id")):
                    data["repeat"] = {"change": dict(u.repeat), "plu": u.plu}
                    break
        return remember(result, data, settle=True)

    @app.get("/api/unfinished")
    def unfinished_work():
        """Deletes and PLU changes some scale has not confirmed. From the catalog, no scale contact."""
        return {"items": words.unfinished_cards(service.list_unfinished())}

    @app.post("/api/check")
    def check(body: CheckBody, request: Request):
        """Read this item from each scale now, so the item page shows what they really hold. Writes nothing."""
        problem = change_guard(request)
        if problem:
            return problem
        reads = service.read_scale_items(plus=[body.plu])
        # No raw messages: they can hold scale details. Staff get plain words, and a reboot flag.
        return {"scales": [words.read_row(r.scale, r.status, r.may_be_frozen) for r in reads]}

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
        # A scale that holds a different product is never forced from here, whatever the page sent.
        tokens = [t for t in body.acknowledge_drift if t not in known["blocked"]]
        try:
            job, started = jobs.start(
                body.preview_id, known["scales"],
                acknowledge_warnings=body.acknowledge_warnings is True,
                acknowledge_drift=tokens,
                requested_by="web",
            )
        except SendBusy:
            return _error(409, "busy", "Another change is being sent right now. Wait for it to finish, then try again.")
        if started:
            job.finished.wait(early_wait_seconds)  # quick refusals come back at once
        if job.state == "error":
            status = STATUS_FOR_CODE.get((job.error or {}).get("code", ""), 500)
            return JSONResponse({"error": job.error}, status_code=status)
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
        }
        if record is None and job is None:
            out.update(heading="This change has not been sent.", severity=words.WARN)
            return out
        if job is not None and job.state == "error" and record is None:
            out.update(state="error", error=job.error, severity=words.BAD)
            out["heading"] = (
                "The change was not sent. " + job.error["message"] if job.error.get("sent") is False
                else "Not sure what happened. " + job.error["message"]
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
        for name in order:
            if name in results:
                rows.append(words.scale_row(results[name]))
            elif finished or stalled:
                rows.append(words.missing_row(name))
            else:
                rows.append(words.pending_row(name, not first_pending_seen))
                first_pending_seen = True
        if finished:
            rows.extend(words.not_touched_row(n) for n in record.not_touched)
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
                statuses = [r.status for r in record.scale_results]
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