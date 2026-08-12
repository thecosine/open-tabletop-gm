"""Focused tests for the wrapper's display URL resolution."""

from __future__ import annotations

import pathlib
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
sys.path.insert(0, str(DISPLAY))

from wrapper import _resolve_display_url


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_wrapper_uses_configured_display_scheme(tmp_path, scheme):
    scheme_file = tmp_path / ".scheme"
    scheme_file.write_text(f"{scheme}\n", encoding="utf-8")

    assert _resolve_display_url(scheme_file, {"GM_DISPLAY_PORT": "5002"}) == (
        f"{scheme}://127.0.0.1:5002"
    )


def test_wrapper_defaults_to_http_when_scheme_file_is_missing(tmp_path):
    assert _resolve_display_url(tmp_path / ".scheme", {"GM_DISPLAY_PORT": "5002"}) == (
        "http://127.0.0.1:5002"
    )


def test_wrapper_rejects_invalid_display_scheme(tmp_path):
    scheme_file = tmp_path / ".scheme"
    scheme_file.write_text("file\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid display scheme 'file'"):
        _resolve_display_url(scheme_file, {"GM_DISPLAY_PORT": "5002"})
