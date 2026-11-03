# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Shared fixtures. The fake scale lives in tests/fake_scale/."""

from __future__ import annotations

import atexit
import shutil
import sqlite3
import os
import sys
import tempfile
from pathlib import Path

import pytest

if os.name == "posix":
    os.umask(0o077)  # config files the tests write must be private, as a real one must be

# A failed read writes to the read problem log. No test may reach the real one, and this is set
# before any fixture runs because a server started by a test inherits it too.
_READ_LOG_FOLDER = tempfile.mkdtemp(prefix="scaleconnect-tests-")
atexit.register(shutil.rmtree, _READ_LOG_FOLDER, ignore_errors=True)
os.environ["SCALECONNECT_READ_LOG"] = str(Path(_READ_LOG_FOLDER) / "read-problems.log")

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(TESTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fake_scale import FakeScale  # noqa: E402


@pytest.fixture
def fake_scale():
    """A fake scale on localhost, with the post write gap off. The test fails if the fake saw traffic
    outside the known protocol, unless the test called fake.allow(code)."""
    scale = FakeScale(post_write_refusal_seconds=0.0).start()
    try:
        yield scale
    finally:
        scale.stop()
    scale.assert_clean()


@pytest.fixture
def make_fake():
    """Factory for tests that need a fake with options, or several fakes."""
    made: list[FakeScale] = []

    def _make(**kw) -> FakeScale:
        # The real 10.5 second gap after a write would make most tests slow.
        # Tests of that gap ask for it with post_write_refusal_seconds.
        kw.setdefault("post_write_refusal_seconds", 0.0)
        s = FakeScale(**kw).start()
        made.append(s)
        return s

    yield _make
    for s in made:
        s.stop()
    for s in made:
        s.assert_clean()


# ---------- service tests ----------

from service_env import Env, assert_wire_clean  # noqa: E402


@pytest.fixture(scope="session")
def template_root(tmp_path_factory):
    """Seeded catalogs are slow to make, so each seed is made once per run."""
    return tmp_path_factory.mktemp("catalog-templates")


@pytest.fixture
def make_env(tmp_path, template_root, make_fake):
    made = []

    def _make(flags, **kw):
        env = Env(tmp_path, template_root, make_fake, flags, **kw)
        made.append(env)
        return env

    yield _make
    for env in made:
        env.svc.close()
        for fake in env.fakes.values():
            assert_wire_clean(fake)


@pytest.fixture
def env(make_env):
    return make_env({"A": True})


@pytest.fixture
def env3(make_env):
    return make_env({"A": True, "B": True, "C": True})


@pytest.fixture(autouse=True)
def fast_sqlite(request, monkeypatch):
    """Each catalog write waits for the disk on this PC (about 60 ms). The service writes
    the catalog many times per change, so tests would spend most of their time there.
    These tests do not test durability, so they turn off the wait. Tests of the catalog
    itself and of the driver (tests/unit_catalog, tests/unit_driver) are left alone."""
    path = str(request.node.fspath)
    if "unit_catalog" in path or "unit_driver" in path:
        return
    real = sqlite3.connect

    def connect(*args, **kwargs):
        con = real(*args, **kwargs)
        con.execute("PRAGMA synchronous=OFF")
        return con

    monkeypatch.setattr(sqlite3, "connect", connect)
