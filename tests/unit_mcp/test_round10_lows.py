# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Round 10 recheck, the low findings L1, L2 and L3.

L1: a held no reply file that cannot be removed no longer keeps its scale flagged for good. Its note goes to
the log once, a read that answers ends the flag as usual, and the failed removal is logged with the path.
L2: a held file carries its scale in its name, so a damaged one still counts against that scale. It is
logged once per process. A write that fails leaves no temp file, and the file is flushed before the rename.
L3: a compare reads a flagged scale only when it is the only scale named. Naming every scale leaves it out.

Fake scales on localhost, made up items.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import scaleconnect.mcp_server as server_module
from scaleconnect.service import ScaleConnectService, compare_leaves_out_frozen

from test_mcp_three_part_rule import Session, ticking_clock

THREE = {"A": True, "B": True, "C": True}


def three(make_env, **kw):
    kw.setdefault("reply_timeout_seconds", 0.3)
    kw.setdefault("connect_attempts", 1)
    return make_env(THREE, clock=ticking_clock(), **kw)


def b_flagged(env):
    env.fakes["B"].stall_read(times=None)
    assert env.svc.compare_scales(["B"])[0].may_be_frozen
    env.fakes["B"].clear_faults()
    assert env.svc.scales_may_be_frozen() == ["B"]


def log_refuses(env):
    real = env.catalog.note_read_no_reply

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    env.catalog.note_read_no_reply = locked
    return real


def held(env):
    return sorted(p.name for p in env.svc._held_dir().iterdir())


def stuck_files(monkeypatch):
    """Held note files cannot be removed, as when the folder belongs to another user."""
    real = Path.unlink

    def unlink(self, *a, **k):
        if self.parent.name == "held_no_replies":
            raise PermissionError("not allowed")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)


# ---------------------------------------------------------------- L1


def test_a_held_file_that_cannot_be_removed_is_logged_once_and_a_compare_still_clears_it(make_env, monkeypatch, caplog):
    env = three(make_env)
    real = log_refuses(env)
    b_flagged(env)
    assert len(held(env)) == 1
    env.catalog.note_read_no_reply = real
    stuck_files(monkeypatch)
    caplog.set_level(logging.ERROR)
    for _ in range(4):
        assert env.svc.scales_may_be_frozen() == ["B"]
    assert len(env.catalog.read_no_reply_events()) == 1, "the note goes to the log once, not on every look"
    stuck = [r.getMessage() for r in caplog.records if "could not be removed" in r.getMessage()]
    assert len(stuck) == 1 and "held_no_replies" in stuck[0] and held(env)[0] in stuck[0]
    assert env.svc.compare_scales(["B"])[0].status == "compared"
    assert env.svc.scales_may_be_frozen() == [], "the documented way out works with the file still there"
    assert len(held(env)) == 1
    other = ScaleConnectService(env.config, env.catalog, sleep=lambda s: None, clock=env.svc._clock)  # the same wall clock
    other_events = len(env.catalog.read_no_reply_events())
    assert other.scales_may_be_frozen() == [], "another process sees the scale answered since the note"
    assert len(env.catalog.read_no_reply_events()) == other_events


def test_a_stuck_file_whose_scale_answered_since_does_not_count(make_env, monkeypatch):
    env = three(make_env)
    log_refuses(env)
    b_flagged(env)
    stuck_files(monkeypatch)
    assert env.svc.compare_scales(["B"])[0].status == "compared"  # the log still refuses here
    assert len(held(env)) == 1
    assert env.svc.scales_may_be_frozen() == []
    assert env.catalog.read_no_reply_events() == []


def test_the_held_folder_is_made_private(make_env, monkeypatch):
    env = three(make_env)
    made = []
    real = Path.mkdir

    def mkdir(self, mode=0o777, *a, **k):
        made.append((self.name, mode))
        return real(self, mode, *a, **k)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    assert env.svc._hold_in_file("B", "check", "2026-11-02T10:00:00+00:00", "read_failed")
    assert ("held_no_replies", 0o700) in made


# ---------------------------------------------------------------- L2


def test_a_held_file_is_named_for_its_scale(make_env):
    env = three(make_env)
    log_refuses(env)
    b_flagged(env)
    [name] = held(env)
    assert name.startswith("B.") and name.endswith(".json") and name.count(".") == 2


def test_a_damaged_held_file_still_counts_against_its_scale_and_is_logged_once(make_env, caplog):
    env = three(make_env)
    real = log_refuses(env)
    folder = env.svc._held_dir()
    folder.mkdir(parents=True)
    (folder / "C.0123456789abcdef0123456789abcdef.json").write_text('{"scale": "C", "wh', encoding="utf-8")
    (folder / "B.fedcba9876543210fedcba9876543210.json").write_text("", encoding="utf-8")
    caplog.set_level(logging.ERROR)
    for _ in range(3):
        assert env.svc.scales_may_be_frozen() == ["B", "C"]
    assert env.svc.frozen_how() == {"B": "unknown", "C": "unknown"}
    bad = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(bad) == 2, "one line per file per process, not one per look"
    env.catalog.note_read_no_reply = real
    assert env.svc.scales_may_be_frozen() == ["B", "C"]
    assert held(env) == [] and len(env.catalog.read_no_reply_events()) == 2, "moved to the log"
    assert env.svc.compare_scales(["C"])[0].status == "compared"
    assert env.svc.scales_may_be_frozen() == ["B"]


def test_a_damaged_file_that_cannot_be_removed_is_told_both_ways(make_env, monkeypatch, caplog):
    """Round 11 L2. One line per file and problem, so the person also learns to remove it by hand."""
    env = three(make_env)
    real = log_refuses(env)
    folder = env.svc._held_dir()
    folder.mkdir(parents=True)
    (folder / "B.fedcba9876543210fedcba9876543210.json").write_text("", encoding="utf-8")
    caplog.set_level(logging.ERROR)
    assert env.svc.scales_may_be_frozen() == ["B"]
    env.catalog.note_read_no_reply = real
    stuck_files(monkeypatch)
    for _ in range(3):
        assert env.svc.scales_may_be_frozen() == ["B"]
    said = [r.getMessage() for r in caplog.records]
    assert len([m for m in said if "could not be read" in m]) == 1
    removed = [m for m in said if "could not be removed" in m]
    assert len(removed) == 1 and "Remove it by hand" in removed[0]


def test_a_damaged_old_style_file_names_no_scale_and_is_left_out(make_env, caplog):
    env = three(make_env)
    folder = env.svc._held_dir()
    folder.mkdir(parents=True)
    (folder / "0123456789abcdef0123456789abcdef.json").write_text("{", encoding="utf-8")
    caplog.set_level(logging.ERROR)
    assert env.svc.scales_may_be_frozen() == [] and env.svc.scales_may_be_frozen() == []
    assert len([r for r in caplog.records if "names no scale" in r.getMessage()]) == 1


def test_an_old_style_good_file_still_counts(make_env):
    env = three(make_env)
    log_refuses(env)
    folder = env.svc._held_dir()
    folder.mkdir(parents=True)
    (folder / "0123456789abcdef0123456789abcdef.json").write_text(
        '{"scale": "A", "what": "check", "at": "2026-11-02T09:00:00+00:00", "status": "unreachable"}', encoding="utf-8")
    assert env.svc.scales_may_be_frozen() == ["A"] and env.svc.frozen_how() == {"A": "unreachable"}


def test_a_held_write_is_flushed_and_a_failed_one_leaves_no_temp_file(make_env, monkeypatch):
    env = three(make_env)
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    assert env.svc._hold_in_file("B", "check", "2026-11-02T10:00:00+00:00", "read_failed")
    assert len(synced) == 1
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    for _ in range(3):
        assert env.svc._hold_in_file("C", "check", "2026-11-02T10:00:01+00:00", "read_failed") is False
    names = held(env)
    assert len(names) == 1 and names[0].startswith("B.") and not any(n.endswith(".tmp") for n in names)


# ---------------------------------------------------------------- L3


def test_the_rule_for_which_compare_reads_a_flagged_scale():
    assert compare_leaves_out_frozen(None) is True
    assert compare_leaves_out_frozen(["A", "B", "C"]) is True
    assert compare_leaves_out_frozen(["A", "B"]) is True
    assert compare_leaves_out_frozen(["B"]) is False
    assert compare_leaves_out_frozen(["B", "B"]) is False


def test_a_compare_that_names_every_scale_leaves_the_flagged_one_out(make_env):
    env = three(make_env)
    b_flagged(env)
    session = Session(env)
    before = len(env.fakes["B"].messages)
    data = session.data("scale_compare_scales", scales=["A", "B", "C"])
    by_scale = {c["scale"]: c for c in data["scales"]}
    assert by_scale["B"]["status"] == "not_contacted" and by_scale["B"]["may_be_frozen"] is True
    assert by_scale["A"]["status"] == "compared" and by_scale["C"]["status"] == "compared"
    assert "B: left out, so it was not read. It is read only when it is the only scale named." in data["summary"]
    assert "name only this scale" in by_scale["B"]["message"]
    assert len(env.fakes["B"].messages) == before and env.svc.scales_may_be_frozen() == ["B"]
    alone = session.data("scale_compare_scales", scales=["B"])
    assert alone["scales"][0]["status"] == "compared" and env.svc.scales_may_be_frozen() == []


def test_the_compare_tool_and_instructions_say_name_it_alone():
    assert "names it alone" in server_module.SERVER_INSTRUCTIONS[:2000]
    assert "names it alone" in server_module._FROZEN_NEXT_STEP
    assert len(server_module.SERVER_INSTRUCTIONS) <= 4096


def test_the_command_line_compare_reads_a_flagged_scale_only_when_named_alone(tmp_path, make_fake, capsys):
    from scaleconnect.catalog import Catalog
    from scaleconnect.config import load_config

    from test_delete_and_reload import run_cli, write_config

    a, b = make_fake(), make_fake(serial="TEST-B")
    path = write_config(tmp_path, a)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f'\n[[scales]]\nname = "B"\naddress = "127.0.0.1"\nport = {b.port}\nserial = "{b.serial}"\nwrite_enabled = true\n')
    with Catalog(load_config(path).settings.catalog_path) as catalog:
        catalog.note_read_no_reply("B", "check", "2026-11-02T10:00:00+00:00", "read_failed")
    code, out = run_cli(capsys, "--config", path, "compare", "--scale", "A", "--scale", "B")
    by_scale = {r["scale"]: r for r in out}
    assert code == 7 and by_scale["B"]["status"] == "not_contacted" and by_scale["B"]["may_be_frozen"] is True
    assert by_scale["A"]["status"] == "compared" and b.nothing_received
    code, out = run_cli(capsys, "--config", path, "compare", "--scale", "B")
    assert out[0]["status"] == "compared"
    code, out = run_cli(capsys, "--config", path, "frozen")
    assert out["scales_may_be_frozen"] == []
