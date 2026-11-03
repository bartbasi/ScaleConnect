# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Command line tool. Output is one JSON document on stdout. Exit codes are stable.

See docs/LIBRARY.md for the commands and exit codes. Holds no scale logic.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, Sequence

from .errors import ScaleConnectError, ValidationFailed
from .service import ScaleConnectService, compare_leaves_out_frozen

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_NOT_SUCCESS = 7


class _Parser(argparse.ArgumentParser):
    """Bad usage also gives JSON, with the stable exit code."""

    def error(self, message: str) -> Any:
        _emit({"ok": False, "error": {"code": "usage", "message": message}})
        raise SystemExit(EXIT_USAGE)


def _emit(document: Any) -> None:
    sys.stdout.write(json.dumps(document, indent=2) + "\n")
    sys.stdout.flush()


def _default_who() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli:unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="scaleconnect", description="Add, edit and delete items on the scales.")
    parser.add_argument("--config", help="path to config.toml")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    commands.add_parser("capabilities", help="what this version can do")
    commands.add_parser("scales", help="list scales and last known status. Contacts no scale")
    commands.add_parser("frozen", help="scales that may be frozen, and how each stopped answering. Contacts no scale")

    search = commands.add_parser("search", help="find items in the catalog")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)

    get = commands.add_parser("get", help="one item from the catalog")
    which = get.add_mutually_exclusive_group(required=True)
    which.add_argument("--plu")
    which.add_argument("--id", type=int)

    preview = commands.add_parser("preview", help="check changes. Changes nothing")
    source = preview.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", dest="json_text", help="JSON list of changes")
    source.add_argument("--file", help="file holding a JSON list of changes")
    source.add_argument("--stdin", action="store_true", help="read the JSON list from stdin")
    preview.add_argument("--scale", action="append", help="only this scale. Can repeat")
    preview.add_argument("--read", action="store_true", help="also read the scales now and report drift")
    preview.add_argument("--who", help="who is asking")

    apply = commands.add_parser("apply", help="apply a preview")
    apply.add_argument("preview_id")
    apply.add_argument("--acknowledge-warnings", action="store_true")
    apply.add_argument("--acknowledge-drift", action="append", default=[], metavar="TOKEN", help="a drift token from a preview or result. Can repeat")
    apply.add_argument("--who", help="who is asking")

    change = commands.add_parser("change", help="the result of an earlier change")
    change.add_argument("change_id")

    changes = commands.add_parser("changes", help="recent changes")
    changes.add_argument("--limit", type=int, default=50)

    compare = commands.add_parser("compare", help="read the enabled scales and list differences. Writes nothing")
    compare.add_argument("--scale", action="append", help="only this scale. Can repeat")

    read = commands.add_parser("read", help="what the enabled scales hold now. Writes nothing")
    read.add_argument("--plu", action="append", help="only this PLU. Can repeat")
    read.add_argument("--id", action="append", type=int, help="only this id. Can repeat")
    read.add_argument("--scale", action="append", help="only this scale. Can repeat")

    settle = commands.add_parser("settle", help="preview settling one item that differs on one scale")
    settle_item = settle.add_mutually_exclusive_group(required=True)
    settle_item.add_argument("--plu")
    settle_item.add_argument("--id", type=int)
    settle.add_argument("--scale", required=True)
    settle.add_argument("--take", required=True, choices=["scale", "catalog"], help="whose record wins")
    settle.add_argument("--who", help="who is asking")

    orphans = commands.add_parser("mark-orphans", help="read a scale, and record what it holds that the catalog does not list")
    orphans.add_argument("--scale", required=True)
    orphans.add_argument("--id", action="append", type=int, help="only this id. Can repeat")
    orphans.add_argument("--note", default="")
    orphans.add_argument("--who", help="who is asking")

    commands.add_parser("orphans", help="ids recorded as orphans. Contacts no scale")

    resolve = commands.add_parser("resolve", help="close a change that never finished, after someone has looked")
    resolve.add_argument("change_id")
    resolve.add_argument("--note", required=True)
    resolve.add_argument("--who", help="who is asking")

    reload_help = (
        "overwrite ids 1 to N on one scale with the catalog, where N is the highest id in the catalog. "
        "Whatever that scale holds at those ids is replaced. Deleted ids get placeholders. No read comes first. "
        "This is the repair for a frozen scale, a scale fresh from a reset, or a scale whose ids differ from the catalog"
    )
    reload = commands.add_parser("reload", help=reload_help, description=reload_help)
    reload.add_argument("--scale", required=True)
    reload.add_argument("--yes", action="store_true", help="yes, overwrite ids 1 to N on this scale. Refused without it")
    reload.add_argument(
        "--accept-differences",
        action="store_true",
        help="yes, send it although the catalog differs from this scale's last read, or nothing is known. Without it, those are listed and nothing is sent",
    )
    reload.add_argument("--who", help="who is asking")
    return parser


def _read_changes(args: argparse.Namespace) -> Any:
    if args.json_text is not None:
        text = args.json_text
    elif args.file is not None:
        try:
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ValidationFailed(f"Cannot read {args.file}: {exc.strerror}.") from None
    else:
        text = sys.stdin.read()
    try:
        # A float price is refused later, by the service, so it stays visible as an error.
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"That is not valid JSON: {exc.msg}.") from None


def _run(args: argparse.Namespace) -> int:
    if args.command == "reload" and not args.yes:
        message = "Refused, and nothing was sent. A reload overwrites ids 1 to N on that scale. Run it again with the yes flag if that is what you want."
        _emit({"ok": False, "error": {"code": "usage", "message": message}})
        return EXIT_USAGE
    service = ScaleConnectService.open(args.config)
    try:
        if args.command == "capabilities":
            _emit(service.capabilities().to_dict())
        elif args.command == "scales":
            _emit([info.to_dict() for info in service.list_scales()])
        elif args.command == "frozen":
            names = service.scales_may_be_frozen()
            how = service.frozen_how()
            _emit({"scales_may_be_frozen": names, "how": {name: how.get(name, "unknown") for name in names}})
        elif args.command == "search":
            _emit([item.to_dict() for item in service.search_items(args.query, limit=args.limit)])
        elif args.command == "get":
            _emit(service.get_item(plu=args.plu, id=args.id).to_dict())
        elif args.command == "preview":
            preview = service.preview_changes(
                _read_changes(args), requested_by=args.who or _default_who(), scales=args.scale, read_scales=args.read,
                leave_out_frozen=True,  # a scale that may be frozen is left out, named or not
            )
            _emit(preview.to_dict())
            return EXIT_OK if preview.ok else EXIT_VALIDATION
        elif args.command == "apply":
            record = service.apply_change(
                args.preview_id,
                args.acknowledge_warnings,
                acknowledge_drift=args.acknowledge_drift,
                requested_by=args.who or _default_who(),
                leave_out_frozen=True,  # checked again under the apply lock
            )
            _emit(record.to_dict())
            return EXIT_OK if record.success else EXIT_NOT_SUCCESS
        elif args.command == "change":
            _emit(service.get_change(args.change_id).to_dict())
        elif args.command == "changes":
            _emit([record.to_dict() for record in service.list_changes(limit=args.limit)])
        elif args.command == "compare":
            # Unless it is the only scale named, one that may be frozen is left out. Naming it alone is the deliberate check that clears it.
            results = service.compare_scales(args.scale, leave_out_frozen=compare_leaves_out_frozen(args.scale))
            _emit([result.to_dict() for result in results])
            clean = all(r.status == "compared" and not r.differences for r in results if r.status != "not_contacted" or r.may_be_frozen)
            return EXIT_OK if clean else EXIT_NOT_SUCCESS
        elif args.command == "read":
            results = service.read_scale_items(plus=args.plu, ids=args.id, scales=args.scale, avoid_frozen=True)
            _emit([result.to_dict() for result in results])
            clean = all(r.status == "read" for r in results if r.status != "not_contacted" or r.may_be_frozen)
            return EXIT_OK if clean else EXIT_NOT_SUCCESS
        elif args.command == "settle":
            preview = service.preview_settle_drift(
                scale=args.scale, take=args.take, plu=args.plu, id=args.id, requested_by=args.who or _default_who(), avoid_frozen=True
            )
            _emit(preview.to_dict())
            return EXIT_OK if preview.ok else EXIT_VALIDATION
        elif args.command == "mark-orphans":
            marks = service.mark_orphans_from_read(
                args.scale, ids=args.id, note=args.note, requested_by=args.who or _default_who(), avoid_frozen=True
            )
            _emit(marks.to_dict())
            return EXIT_OK if marks.status == "read" else EXIT_NOT_SUCCESS
        elif args.command == "orphans":
            _emit({str(item_id): names for item_id, names in service.list_orphans().items()})
        elif args.command == "resolve":
            _emit(service.resolve_interrupted_change(args.change_id, args.note, requested_by=args.who or _default_who()).to_dict())
        elif args.command == "reload":
            result = service.reload_scale(args.scale, accept_differences=args.accept_differences, requested_by=args.who or _default_who())
            _emit(result.to_dict())
            return EXIT_OK if result.ok else EXIT_NOT_SUCCESS
        return EXIT_OK
    finally:
        service.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except ScaleConnectError as exc:
        _emit({"ok": False, "error": exc.to_dict()})
        return exc.exit_code
    except Exception as exc:
        # The type only. Text from an exception could hold something from the config.
        _emit({"ok": False, "error": {"code": "unexpected", "message": f"Unexpected error: {exc.__class__.__name__}."}})
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
