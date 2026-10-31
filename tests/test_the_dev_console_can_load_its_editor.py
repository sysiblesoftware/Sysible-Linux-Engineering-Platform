"""The documented no-build dev console must be able to load its editor.

docs/RUNNING.md's "Development (without Docker)" says no node toolchain is
needed, because "Monaco loads from a CDN". webgui/server.py serves
webgui/static/index.html whenever frontend/dist is absent — exactly that path —
and that file pulls Monaco from cdn.jsdelivr.net.

The CSP sent with it was `script-src 'self' 'unsafe-eval'`, which forbids
precisely that. So following the documented steps produced a console that loads
and whose editor never appears, with only a CSP violation in the browser console
to say why. Not reachable in the Docker image or any SLOP install, where dist
always exists — which is why it survived.

The fallback document now gets a policy that permits the one host it needs. The
BUILT console, which is the only thing that ships, keeps the airgap-friendly
policy unchanged — that is the half worth guarding, and it is what the first
test here is for.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC_INDEX = ROOT / "webgui" / "static" / "index.html"


def _server():
    """SLEP's webgui/server.py by path — see the note in
    test_the_login_throttle_cannot_be_dodged_by_a_header.py for why not by name."""
    spec = importlib.util.spec_from_file_location(
        "slep_webgui_server_csp", ROOT / "webgui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _directive(csp: str, name: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith(name + " ") or part == name:
            return part
    return ""


@pytest.fixture(scope="module")
def srv():
    return _server()


def test_the_built_console_allows_no_third_party_script(srv):
    """The policy that actually ships. Airgap-friendly means airgap-friendly."""
    script = _directive(srv._CSP, "script-src")
    assert script == "script-src 'self' 'unsafe-eval'", script
    assert "jsdelivr" not in srv._CSP and "http" not in srv._CSP


def test_the_fallback_console_allows_exactly_the_host_it_uses(srv):
    """And no more: whatever the static page loads from, that is what is allowed
    — not a wildcard, and not a second CDN nobody uses."""
    html = STATIC_INDEX.read_text()
    hosts = {f"{m.group(1)}://{m.group(2)}" for m in
             re.finditer(r"(https?)://([a-z0-9.-]+)", html, re.I)}
    external = {h for h in hosts if "jsdelivr" in h or "unpkg" in h or "cdn" in h}
    assert external, "the static console no longer loads anything external — drop _CSP_STATIC"
    for host in external:
        assert host in srv._CSP_STATIC, f"{host} is loaded but not allowed"
    # Nothing else was opened up.
    allowed = {m.group(0) for m in re.finditer(r"https?://[a-z0-9.:-]+", srv._CSP_STATIC, re.I)}
    assert allowed <= external | {srv._MONACO_CDN}, allowed
    assert "*" not in srv._CSP_STATIC


def test_monaco_needs_script_style_and_font(srv):
    """loader.js pulls further scripts, Monaco's own CSS, and the codicon font
    from the same CDN. Allowing only script-src leaves a broken-looking editor."""
    for name in ("script-src", "style-src", "font-src"):
        assert srv._MONACO_CDN in _directive(srv._CSP_STATIC, name), name


def test_the_two_policies_differ_only_in_that_host(srv):
    """A looser dev policy must not quietly loosen anything else."""
    strip = lambda c: c.replace(" " + srv._MONACO_CDN, "")  # noqa: E731
    assert strip(srv._CSP_STATIC) == srv._CSP


def test_the_policy_sent_is_chosen_by_the_document_served(srv):
    """Tied to what is actually being served, so a built install can never be
    handed the looser policy."""
    src = (ROOT / "webgui" / "server.py").read_text()
    i = src.index('resp.headers.setdefault("Content-Security-Policy"')
    decision = src[i:i + 260]
    assert "_CSP_STATIC" in decision and "DIST" in decision, decision


def test_the_dev_docs_still_describe_this_path():
    """If the CDN dependency is ever vendored (the TODO in static/index.html),
    this file and _CSP_STATIC should go with it."""
    doc = (ROOT / "docs" / "RUNNING.md").read_text()
    assert "Development (without Docker)" in doc
    assert "CDN" in doc
