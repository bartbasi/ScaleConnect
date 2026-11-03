# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Helpers for the web page tests. Fake scales on localhost only. Everything is made up."""

from __future__ import annotations

import dataclasses
import time

from fastapi.testclient import TestClient

from scaleconnect.web import create_app
PIN = "test-pin-4821"


class Clock:
    """A clock the test can move forward, for the lockout."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Web:
    """One page over one service, with helpers that behave like the page's own script."""

    def __init__(self, env, **kw):
        self.env = env
        self.clock = Clock()
        kw.setdefault("early_wait_seconds", 5.0)
        kw.setdefault("allowed_hosts", ["testserver"])
        self.config = dataclasses.replace(env.config, pin=PIN)
        self.app = create_app(env.svc, self.config, clock=self.clock, **kw)
        self.client = TestClient(self.app)
        self.csrf: str | None = None

    def login(self, pin: str = PIN):
        r = self.client.post("/api/login", json={"pin": pin})
        if r.status_code == 200:
            self.csrf = r.json()["csrf"]
        return r

    def post(self, path: str, body: dict, *, csrf: str | None | bool = True, headers: dict | None = None):
        h = dict(headers or {})
        if csrf is True:
            csrf = self.csrf
        if csrf:
            h["X-CSRF-Token"] = csrf
        return self.client.post(path, json=body, headers=h)

    def preview(self, changes, read=False, **kw):
        return self.post("/api/preview", {"changes": changes, "read": read}, **kw)

    def send(self, preview_id, *, warnings=False, drift=(), **kw):
        return self.post("/api/send", {
            "preview_id": preview_id, "acknowledge_warnings": warnings, "acknowledge_drift": list(drift),
        }, **kw)

    def wait_finished(self, preview_id, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            d = self.client.get(f"/api/send/{preview_id}").json()
            if d["state"] in ("finished", "error"):
                return d
            time.sleep(0.05)
        raise AssertionError("the send did not finish")

    def change_and_wait(self, changes, *, warnings=False, drift=(), read=False):
        p = self.preview(changes, read=read).json()
        assert p["ok"], p
        r = self.send(p["preview_id"], warnings=warnings, drift=drift)
        assert r.status_code == 200, r.text
        return p, self.wait_finished(p["preview_id"])


