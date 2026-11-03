# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""Replay real captures against the fake. Skips if no captures are available.

Set SCALECONNECT_CAPTURES to a capture file or a folder of them. The
captures are Tor-rey traffic and are never part of the repo.

Only the messages the library can send are compared: RI, bX01XX, GP, GPF0, CX,
LP and the Ok that asks for the next block. Lab probes (E, S, D, other L
messages, junk) are not copied by the fake on purpose.

Known: the real scale never answered two LP requests in the captures (it had
left the network). The fake cannot know a scale is dead, so those show as
mismatches. They are listed in the warning, not hidden, and any other mismatch
fails the test.
"""

from __future__ import annotations

import os
import warnings

import pytest

from fake_scale import replay_check


def test_fake_replies_match_the_real_scale():
    path = os.environ.get("SCALECONNECT_CAPTURES")
    if not path:
        pytest.skip("SCALECONNECT_CAPTURES is not set")
    report = replay_check.run(path)
    if report.skipped:
        pytest.skip(report.skipped)
    library = [r for r in report.results if r.is_library]
    assert library, "the captures held no library messages to compare"
    unanswered = [r for r in report.mismatches if r.kind == "LP" and not r.real]
    bad = [r for r in report.mismatches if r not in unanswered]
    if unanswered:
        warnings.warn(
            f"{len(unanswered)} LP requests got no reply from the real scale (it was off the network); "
            "the fake answers them. Not compared."
        )
    assert not bad, "The fake differs from the real scale:\n" + "\n".join(
        f"{r.kind} {r.capture} stream {r.stream} message {r.index}: {r.note} {replay_check.describe(r)}"
        for r in bad
    )
