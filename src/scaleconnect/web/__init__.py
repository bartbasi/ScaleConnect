# ScaleConnect. Copyright 2026 Cheese & Crackers, Inc.
# Free software under the GNU GPL v3 with additional terms: see LICENSE and NOTICE.
"""The ScaleConnect web page. Run it with: python -m scaleconnect.web"""

from .app import create_app

__all__ = ["create_app"]
