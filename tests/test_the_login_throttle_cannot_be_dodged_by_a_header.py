"""The per-IP login throttle must key on an address the client cannot choose.

The backend derives it from the RIGHTMOST X-Forwarded-For hop, on the reasoning
that a trusted proxy APPENDS the real peer — so the last entry is the one our own
infrastructure wrote and everything to its left is client-appendable noise. That
reasoning is correct behind the SLOP gateway, where Caddy appends.

It was not correct standalone. The console BFF forwarded every request header
through to the backend untouched and never set or appended X-Forwarded-For, and
a standalone SLEP publishes :8810 on 0.0.0.0 by default — so the value the
backend keyed on was wholly attacker-chosen. One rotating header per attempt
gave every guess a fresh bucket, and the credential-spraying throttle (50
failures) never tripped. Proven before the fix: 60 wrong logins across 60
usernames with a rotating X-Forwarded-For produced 60 × 401 and no 429; the same
60 with one fixed value produced the 429.

The BFF is the only thing that talks to the backend (it listens on
127.0.0.1:9100 inside the container and is not published), so the BFF is the one
place that can decide this. It now OVERWRITES X-Forwarded-For with a single
value:

  * behind the gateway — the rightmost hop of the incoming header, which is the
    browser address Caddy appended and which a client cannot put anything to the
    right of;
  * standalone — the socket peer the BFF itself saw.

The backend then reads one trusted entry either way, and the per-username
throttle (10 failures) was never affected.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "shared-gateway-secret-abc123"


def _load_bff():
    """Load SLEP's webgui/server.py BY PATH.

    `import webgui.server` is not usable here: this suite runs under the shared
    virtualenv with the Controller's repo root on sys.path, and the Controller
    has a `webgui` PACKAGE while SLEP's webgui/ is a plain directory — so the
    name resolves to the Controller's BFF. By path, under its own module name,
    there is no ambiguity.
    """
    spec = importlib.util.spec_from_file_location(
        "slep_webgui_server_under_test", ROOT / "webgui" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bff(monkeypatch):
    """The console BFF with the backend hop captured instead of performed."""
    monkeypatch.setenv("SLEP_TRUST_GATEWAY_AUTH", "1")
    monkeypatch.setenv("SYSIBLE_SSO_SHARED_SECRET", SECRET)
    srv = _load_bff()

    from starlette.testclient import TestClient

    seen = {}

    class _Resp:
        status_code, content, headers = 200, b'{"ok":true}', {"content-type": "application/json"}

    def _capture(method, url, **kw):
        seen.clear()
        seen.update({k.lower(): v for k, v in (kw.get("headers") or {}).items()})
        return _Resp()

    monkeypatch.setattr(srv.requests, "request", _capture)
    return TestClient(srv.app), seen, srv


def _xff(seen):
    return seen.get("x-forwarded-for")


def test_a_client_supplied_header_is_discarded_standalone(bff):
    client, seen, srv = bff
    client.post("/api/login", json={"username": "a", "password": "b"},
                headers={"X-Forwarded-For": "9.9.9.9"})
    assert _xff(seen) != "9.9.9.9", "the client's own value reached the backend"
    assert _xff(seen) == "testclient", _xff(seen)


def test_a_chain_of_client_supplied_hops_is_discarded(bff):
    client, seen, srv = bff
    client.post("/api/login", json={"username": "a", "password": "b"},
                headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
    assert _xff(seen) == "testclient", _xff(seen)


def test_rotating_the_header_cannot_change_the_bucket(bff):
    """The bypass itself: every attempt must land in the SAME bucket."""
    client, seen, srv = bff
    buckets = set()
    for n in range(20):
        client.post("/api/login", json={"username": f"u{n}", "password": "b"},
                    headers={"X-Forwarded-For": f"10.0.0.{n}"})
        buckets.add(_xff(seen))
    assert buckets == {"testclient"}, buckets


def test_behind_the_gateway_the_real_client_is_preserved(bff):
    """Overwriting with the BFF's own peer would make every browser behind SLOP
    share one bucket — the gateway's address — so the gateway case takes the
    rightmost incoming hop, which is what Caddy appended."""
    client, seen, srv = bff
    client.post("/api/login", json={"username": "a", "password": "b"},
                headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7",
                         "X-Sysible-Auth": SECRET})
    assert _xff(seen) == "203.0.113.7", _xff(seen)


def test_a_forged_secret_does_not_buy_the_gateway_treatment(bff):
    client, seen, srv = bff
    client.post("/api/login", json={"username": "a", "password": "b"},
                headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7",
                         "X-Sysible-Auth": "wrong"})
    assert _xff(seen) == "testclient", _xff(seen)


def test_the_header_is_set_on_every_proxied_call_not_just_login(bff):
    client, seen, srv = bff
    client.get("/api/projects", headers={"X-Forwarded-For": "9.9.9.9"})
    assert _xff(seen) == "testclient", _xff(seen)


# ---------------------------------------------------------------------------
# the backend's half
# ---------------------------------------------------------------------------
def test_the_backend_reads_the_one_trusted_hop():
    from starlette.requests import Request

    import backend.app as app_mod

    def _req(xff=None, peer="192.0.2.50"):
        hdrs = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
        return Request({"type": "http", "method": "POST", "path": "/login",
                        "headers": hdrs, "client": (peer, 1234)})

    assert app_mod._login_source_ip(_req("203.0.113.7")) == "203.0.113.7"
    # No header at all (a direct call that somehow reached the backend): the peer.
    assert app_mod._login_source_ip(_req(None)) == "192.0.2.50"
    # A multi-hop value can only come from a misconfigured front end; the
    # rightmost is still the one our own hop wrote.
    assert app_mod._login_source_ip(_req("1.1.1.1, 203.0.113.7")) == "203.0.113.7"


def test_the_per_ip_throttle_trips_once_the_bucket_is_stable(client):
    """End to end at the backend, with the header the BFF would now send:
    spraying 60 usernames from one address trips the IP throttle."""
    import backend.db as db
    from starlette.testclient import TestClient

    from backend.app import app
    import backend.app as app_mod

    bare = TestClient(app)
    ip = "198.51.100.77"
    db.login_throttle_clear(f"ip:{ip}") if hasattr(db, "login_throttle_clear") else None
    codes = []
    for n in range(app_mod._LOGIN_IP_MAX_FAILURES + 10):
        r = bare.post("/login", json={"username": f"spray{n}", "password": "nope"},
                      headers={"X-Forwarded-For": ip})
        codes.append(r.status_code)
        if r.status_code == 429:
            break
    assert 429 in codes, (
        f"the IP throttle never tripped in {len(codes)} attempts "
        f"(limit {app_mod._LOGIN_IP_MAX_FAILURES})")
