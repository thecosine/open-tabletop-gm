"""Display port configuration tests."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = REPO / "display"
sys.path.insert(0, str(DISPLAY))

from display_config import DEFAULT_DISPLAY_PORT, resolve_display_port


@pytest.mark.parametrize("environ", [{}, {"GM_DISPLAY_PORT": ""}, {"GM_DISPLAY_PORT": "  "}])
def test_unset_or_empty_port_uses_live_default(environ):
    assert DEFAULT_DISPLAY_PORT == 5001
    assert resolve_display_port(environ) == 5001


@pytest.mark.parametrize("value", ["1", "5002", "65535", " 5002 "])
def test_valid_port_is_resolved(value):
    assert resolve_display_port({"GM_DISPLAY_PORT": value}) == int(value)


@pytest.mark.parametrize("value", ["not-a-port", "1.5", "0", "-1", "65536"])
def test_invalid_port_is_rejected(value):
    with pytest.raises(ValueError, match="GM_DISPLAY_PORT must be an integer from 1 to 65535"):
        resolve_display_port({"GM_DISPLAY_PORT": value})


def test_invalid_cli_configuration_fails_cleanly():
    result = subprocess.run(
        [sys.executable, str(DISPLAY / "display_config.py")],
        env={**os.environ, "GM_DISPLAY_PORT": "invalid"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "GM Display configuration error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_server_and_launcher_use_the_resolved_port():
    app_source = (DISPLAY / "gm-display-app.py").read_text(encoding="utf-8")
    launcher_source = (DISPLAY / "start-display.sh").read_text(encoding="utf-8")
    assert "port = _resolve_display_port()" in app_source
    assert "app.run(host=host, port=port" in app_source
    assert "localhost:{port}" in app_source
    assert "display_config.py" in launcher_source
    assert "localhost:${DISPLAY_PORT}" in launcher_source
