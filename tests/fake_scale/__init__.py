# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""A fake scale for tests. See server.py."""

from .server import FakeScale, Fault, Message, Violation, make_record
from .wire import check_char, frame

__all__ = ["FakeScale", "Fault", "Message", "Violation", "make_record", "check_char", "frame"]
