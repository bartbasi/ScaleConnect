# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Fixtures for the web page tests."""

from __future__ import annotations

import pytest

from webenv import Web


@pytest.fixture
def make_web(make_env):
    def _make(flags=None, *, web_kw=None, **env_kw):
        env = make_env(flags or {"A": True}, **env_kw)
        return Web(env, **(web_kw or {}))

    return _make


@pytest.fixture
def web(make_web):
    return make_web({"A": True})


@pytest.fixture
def web3(make_web):
    return make_web({"A": True, "B": True, "C": True}, connect_attempts=1)
