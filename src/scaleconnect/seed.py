# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Load a seed JSON file into a new catalog.

The seed file is private and lives outside the repo. It is built from
ScaleLink's real full send, so every field is exactly what ScaleLink sends.

File format: {"items": [ ... ]}. Each item is an object with the names used by
`Item.to_dict()` (id, code, plu, description, price, field6, fields_7_12,
timestamp, fields_14_20), or a list of the 20 text values. An object may also
carry "raw", the 20 values. If both are given they must agree.

Command line:

    python -m scaleconnect.seed SEED.json [--config PATH] [--db PATH] [--overwrite] [--believed-on FRONT]

Without --db the catalog goes where the config says, by the same rule the service uses.
--believed-on records what a scale should hold after a full send. The belief stays unknown
until a read shows it, and no write relies on it, because every write reads the scale first.
--overwrite takes the same lock as a change, so it refuses while one is running.

Without --overwrite an existing catalog file is never touched. With it, the
old file is renamed with a timestamp first, not deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import MAX_ID, MIN_ID, Catalog, CatalogError, apply_lock
from .config import catalog_path_for, default_config_path, load_config
from .errors import ScaleConnectError
from .models import Item


class CatalogExistsError(CatalogError):
    """The catalog file is already there and overwrite was not asked for."""

    code = "catalog_exists"


class SeedFileError(CatalogError):
    """The seed file is missing or not in the expected shape."""

    code = "seed_file_error"


_NAMES = ("id", "code", "plu", "description", "price", "field6",
          "fields_7_12", "timestamp", "fields_14_20")


def read_seed(seed_path: str | os.PathLike[str]) -> list[Item]:
    """Read and check a seed file. Returns the items in file order."""
    p = Path(seed_path)
    try:
        with p.open(encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise SeedFileError(f"Seed file not found: {p}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SeedFileError(f"Seed file is not valid JSON: {exc}") from None
    entries = data.get("items") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        raise SeedFileError("Seed file has no items")
    out: list[Item] = []
    for pos, entry in enumerate(entries, start=1):
        try:
            if isinstance(entry, list):
                item = Item.from_fields(entry)
            elif isinstance(entry, dict):
                if all(k in entry for k in _NAMES):
                    item = Item.from_dict({**entry, "id": int(entry["id"])})
                elif "raw" in entry:
                    item = Item.from_fields(entry["raw"])
                else:
                    raise ValueError("item has neither the named fields nor raw")
                if "raw" in entry and item.to_fields() != list(entry["raw"]):
                    raise ValueError("named fields and raw values disagree")
            else:
                raise ValueError("item is neither an object nor a list")
        except (KeyError, ValueError, TypeError) as exc:
            raise SeedFileError(f"Seed item {pos}: {exc}") from None
        out.append(item)
    return out


def seed_catalog(db_path: str | os.PathLike[str] | None, seed_path: str | os.PathLike[str], *,
                 overwrite: bool = False, believed_on: tuple[str, ...] = (),
                 config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Create a catalog from a seed file.

    Refuses if the catalog file exists, unless `overwrite` is True. Then the
    old file is renamed with a timestamp. `believed_on` names scales that took the
    full send, recorded as unknown until a read shows what they hold. The seed
    file is read and checked before anything on disk changes. If loading fails
    the new file is removed and any old catalog is put back.
    """
    path = Path(db_path).expanduser() if db_path is not None else catalog_path_for(config_path)
    items = read_seed(seed_path)
    with apply_lock(_lock_path(path, config_path)):
        return _seed_locked(path, items, seed_path, overwrite, believed_on)


def _lock_path(path: Path, config_path: str | os.PathLike[str] | None) -> Path:
    """The same lock the service holds, so a catalog is never replaced during a change."""
    try:
        chosen = config_path or os.environ.get("SCALECONNECT_CONFIG") or default_config_path()
        if Path(chosen).expanduser().exists():
            settings = load_config(chosen).settings
            if Path(settings.catalog_path) == path:
                return Path(settings.state_dir) / "apply.lock"
    except ScaleConnectError:
        pass
    return path.parent / "apply.lock"


def _seed_locked(path: Path, items: list[Item], seed_path: str | os.PathLike[str], overwrite: bool,
                 believed_on: tuple[str, ...]) -> dict[str, Any]:
    moved_to: Path | None = None
    if path.exists():
        if not overwrite:
            raise CatalogExistsError(
                f"A catalog already exists at {path}. Ask to overwrite it to replace it. "
                f"The old file would be renamed, not deleted.")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        moved_to = path.with_name(f"{path.name}.old-{stamp}")
        os.replace(path, moved_to)
        for ext in ("-wal", "-shm"):
            side = path.with_name(path.name + ext)
            if side.exists():
                os.replace(side, moved_to.with_name(moved_to.name + ext))
    seed_id = f"seed-{uuid.uuid4().hex[:8]}"
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with Catalog(path) as cat:
            count = cat.import_items(items)
            for scale in believed_on:
                cat.believe_scale_holds_listed_items(scale, "seed", seed_id, at)
            cat.append_log(seed_id, "seeded", {"items": count, "source": Path(seed_path).name,
                                               "believed_on": list(believed_on)}, at)
            free = len(cat.free_ids(MAX_ID))
    except BaseException:
        for ext in ("", "-wal", "-shm"):
            q = path.with_name(path.name + ext)
            if q.exists():
                q.unlink()
        if moved_to is not None:
            os.replace(moved_to, path)
            for ext in ("-wal", "-shm"):
                side = moved_to.with_name(moved_to.name + ext)
                if side.exists():
                    os.replace(side, path.with_name(path.name + ext))
        raise
    return {"path": str(path), "items": count, "free_ids": free,
            "previous_catalog": str(moved_to) if moved_to else None,
            "believed_on": list(believed_on)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scaleconnect.seed",
                                 description="Load a seed JSON file into a new catalog.")
    ap.add_argument("seed", help="path to the seed JSON file")
    ap.add_argument("--config", help="config file that names the catalog path")
    ap.add_argument("--db", help="catalog file to create (default: the path the config gives)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an existing catalog (the old one is renamed, not deleted)")
    ap.add_argument("--believed-on", action="append", default=[], metavar="SCALE",
                    help="record that this scale holds every seeded item. Repeat for more scales")
    args = ap.parse_args(argv)
    try:
        info = seed_catalog(args.db, args.seed, overwrite=args.overwrite,
                            believed_on=tuple(args.believed_on), config_path=args.config)
    except ScaleConnectError as exc:
        print(f"Not seeded: {exc.message}", file=sys.stderr)
        return 1
    print(f"Seeded {info['items']} items into {info['path']}. "
          f"Free ids: {info['free_ids']} of {MAX_ID - MIN_ID + 1}.")
    if info["previous_catalog"]:
        print(f"The old catalog was kept as {info['previous_catalog']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
