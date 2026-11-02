"""Shared setup for tests of the service: fake scales, a seeded catalog, a service.

Everything is made up. The catalog is made by the real seed code, from a JSON
file written in the test folder, so a test starts from a catalog that is
seeded the way the real one is. Making a catalog costs 1.4 seconds on this
PC, so each distinct seed is made once per test run and copied.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from pathlib import Path

from scaleconnect.catalog import Catalog
from scaleconnect.config import Config, ScaleConfig, Settings
from scaleconnect.models import Item
from scaleconnect.seed import seed_catalog
from scaleconnect.service import ScaleConnectService

KNOWN_KINDS = {"RI", "bX01XX", "GP", "GPF0", "CX", "LP", "Ok"}
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
PRICE = re.compile(r"^(0|[1-9]\d{0,2})\.\d\d$")

SEED = [
    Item(1, "1001", "1001", "Test Cheddar", "10.00", field6=" ", timestamp="2026-01-01 00:00:00"),
    Item(2, "1002", "1002", "Test Brie", "20.00", field6="", timestamp="2026-01-01 00:00:00",
         fields_7_12=("0", "1", "0", "0", "0", "0")),
    Item(3, "1003", "1003", "Test Salami", "5.50", field6=" ", timestamp="2026-01-01 00:00:00",
         fields_7_12=("1", "1", "0", "0.139", "0", "0"),
         fields_14_20=("0", "0", "0", "0", "0", "0", "1")),
]

_TEMPLATES: dict[tuple, Path] = {}


def serial_for(name: str) -> str:
    return f"TEST-{name}"


def seeded_catalog_file(root: Path, seed: list[Item]) -> Path:
    """A seeded catalog file for this seed, made once and reused by copying."""
    key = tuple(i.to_fields().__repr__() for i in seed)
    if key not in _TEMPLATES:
        folder = root / f"seed-{len(_TEMPLATES)}"
        folder.mkdir(parents=True)
        seed_json = folder / "seed.json"
        seed_json.write_text(json.dumps({"items": [i.to_dict() for i in seed]}), encoding="utf-8")
        db = folder / "template.sqlite3"
        seed_catalog(db, seed_json)
        _TEMPLATES[key] = db
    return _TEMPLATES[key]


def scale_config(name: str, fake, write_enabled: bool) -> ScaleConfig:
    kw = dict(name=name, address="127.0.0.1", port=fake.port, write_enabled=write_enabled)
    names = {f.name for f in dataclasses.fields(ScaleConfig)}
    if "serial" in names:
        kw["serial"] = fake.serial
    return ScaleConfig(**kw)


def assert_wire_clean(fake) -> None:
    """Nothing invalid and nothing unknown ever reached this fake."""
    assert set(fake.kinds()) <= KNOWN_KINDS, fake.kinds()
    assert all(m.check_ok for m in fake.messages)
    assert fake.unexpected_violations() == []
    for m in fake.messages_of("GP"):
        for r in m.records:
            assert 1 <= int(r[0]) <= 500, r
            assert re.fullmatch(r"\d{4}", r[1]) and r[1] == r[2], r
            assert PRICE.match(r[4]) and 0 < float(r[4]) <= 999.99, r
            desc = r[3]
            assert desc.strip() and len(desc) <= 35, r
            assert all(0x20 <= ord(c) <= 0x7E for c in "".join(r)), r
            assert TIMESTAMP.match(r[12]), r


class Env:
    def __init__(self, tmp_path, template_root, make_fake, flags: dict[str, bool], *, seed=SEED,
                 preload=True, backup_hook=None, clock=None, fake_options=None, unseeded=False,
                 **settings_kw):
        opts = dict(fake_options or {})
        self.fakes = {name: make_fake(serial=serial_for(name), **opts) for name in flags}
        if preload:
            for fake in self.fakes.values():
                fake.preload(*[tuple(i.to_fields()) for i in seed])
        base = dict(
            reply_timeout_seconds=2.0,
            connect_timeout_seconds=2.0,
            connect_attempts=2,
            connect_retry_wait_seconds=0.0,
            state_dir=tmp_path / "state",
            catalog_path=tmp_path / "catalog.sqlite3",
            backup_dir=tmp_path / "backups",
            scalelink_db_copy="not_available_here",
        )
        base.update(settings_kw)
        self.settings = Settings(**base)
        scales = tuple(scale_config(n, self.fakes[n], flag) for n, flag in flags.items())
        self.config = Config(scales=scales, settings=self.settings)
        if not unseeded:
            shutil.copy(seeded_catalog_file(template_root, seed), self.settings.catalog_path)
        # With unseeded=True this is a catalog nobody seeded: a brand new, empty one.
        self.catalog = Catalog(self.settings.catalog_path)
        self._svc_kw = dict(backup_hook=backup_hook, sleep=lambda s: None)
        if clock is not None:
            self._svc_kw["clock"] = clock
        self.flags = dict(flags)
        self.svc = ScaleConnectService(self.config, self.catalog, **self._svc_kw)

    def set_write_enabled(self, name: str, flag: bool) -> None:
        """What editing the config file and restarting does: a new service on the
        same catalog, with one scale\x27s write flag changed."""
        self.flags[name] = flag
        scales = tuple(scale_config(n, self.fakes[n], f) for n, f in self.flags.items())
        self.config = Config(scales=scales, settings=self.settings)
        self.svc = ScaleConnectService(self.config, self.catalog, **self._svc_kw)

    def attempt(self, changes, *, ack=False, **kw):
        """Preview and apply, never raising ScaleConnectError. Returns
        (preview, record or None, error or None)."""
        from scaleconnect.errors import ScaleConnectError

        try:
            p = self.svc.preview_changes(changes, requested_by="test", **kw)
            if not p.ok:
                return p, None, None
            return p, self.svc.apply_change(p.preview_id, acknowledge_warnings=ack, requested_by="test"), None
        except ScaleConnectError as exc:
            return None, None, exc

    def preview(self, changes, **kw):
        p = self.svc.preview_changes(changes, requested_by="test", **kw)
        assert p.ok, [e.to_dict() for e in p.errors]
        return p

    def change(self, changes, *, ack=False, **kw):
        p = self.preview(changes, **kw)
        return self.svc.apply_change(p.preview_id, acknowledge_warnings=ack, requested_by="test")

    def statuses(self, record) -> dict[str, str]:
        return {r.scale: r.status for r in record.scale_results}

    def silent(self, *names) -> bool:
        return all(self.fakes[n].nothing_received for n in names)

    def writes(self, name: str) -> list:
        """GP messages the named fake received."""
        return self.fakes[name].messages_of("GP")
