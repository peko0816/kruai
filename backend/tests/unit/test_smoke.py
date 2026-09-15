"""Smoke test: proves the test runner is wired.

Delete this file the moment a real test in the same directory covers the
same guarantee.
"""

from __future__ import annotations

import app


def test_app_package_importable() -> None:
    assert app.__name__ == "app"
