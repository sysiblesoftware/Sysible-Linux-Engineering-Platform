"""The console port must be restrictable.

Behind SLOP, Caddy is meant to be the only way in. Publishing :8810 on every
interface let that be walked around — straight to :8810 and the gateway's
HSTS/CSP/frame headers are gone, and so is the platform's central login throttle.
(Not a way in: SLEP refuses a local login under SSO and fails closed without the
shared secret. Surface area, which is still worth removing.)

The SLOP installer sets this bind to the docker bridge gateway, the address Caddy
reaches SLEP through.
"""
import re
from pathlib import Path

COMPOSE = (Path(__file__).resolve().parents[1] / "deploy" / "docker-compose.yml").read_text(
    encoding="utf-8")


def _console_line():
    for ln in COMPOSE.splitlines():
        if re.match(r'^\s*-\s*"[^"]*8810:8810', ln):
            return ln.strip()
    raise AssertionError("no 8810 port mapping in deploy/docker-compose.yml")


def test_the_console_bind_is_a_variable():
    assert "SYSIBLE_SLEP_BIND" in _console_line(), "the console port is not restrictable"


def test_it_defaults_to_every_interface():
    """A STANDALONE SLEP has no gateway in front of it, so the default has to
    stay what it always was. Only the installer narrows it."""
    assert "${SYSIBLE_SLEP_BIND:-0.0.0.0}" in _console_line(), _console_line()
