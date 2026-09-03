"""SLOP SSO gateway trust mode. When SLEP runs behind the SLOP single-sign-on
gateway, an upstream Caddy authenticates the browser and injects identity headers
(X-Sysible-User / X-Sysible-Role) plus a shared-secret header (X-Sysible-Auth). SLEP
trusts the identity ONLY when trust mode is on AND X-Sysible-Auth matches the secret.
OFF by default → standalone SLEP ignores these headers entirely and is unchanged."""
import pytest
from fastapi.testclient import TestClient

import backend.app as app_mod
from backend.app import app

SECRET = "shared-gateway-secret-abc123"


@pytest.fixture()
def gw(client):
    # Depend on `client` for its side effects (startup → init_db + a seeded admin/
    # Default org). Hand back a BARE client with no Authorization header, so identity
    # can only come from gateway headers (or the request is unauthenticated).
    return TestClient(app)


@pytest.fixture()
def trust_on(monkeypatch):
    monkeypatch.setattr(app_mod, "_TRUST_GATEWAY_AUTH", True)
    monkeypatch.setattr(app_mod, "_SSO_SHARED_SECRET", SECRET)


def _hdrs(role="superuser", auth=SECRET, user="alice"):
    h = {"X-Sysible-User": user, "X-Sysible-Role": role}
    if auth is not None:
        h["X-Sysible-Auth"] = auth
    return h


# (a) Trust mode OFF (the default): the identity headers are meaningless.
def test_trust_off_ignores_identity_headers(gw):
    assert gw.get("/me", headers=_hdrs()).status_code == 401


# (b) Trust mode ON + correct secret: the gateway identity is honored.
def test_trust_on_correct_secret_honors_identity(gw, trust_on):
    r = gw.get("/me", headers=_hdrs(role="superuser", user="alice"))
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["role"] == "superuser"
    assert body["system_admin"] is True


# (c) Trust mode ON + wrong / missing secret: the identity is NOT honored.
def test_trust_on_wrong_secret_not_honored(gw, trust_on):
    assert gw.get("/me", headers=_hdrs(auth="nope")).status_code == 401


def test_trust_on_missing_secret_header_not_honored(gw, trust_on):
    assert gw.get("/me", headers=_hdrs(auth=None)).status_code == 401


# Fail closed: trust mode on but no configured secret → never trust the headers.
def test_fail_closed_when_secret_unset(gw, monkeypatch):
    monkeypatch.setattr(app_mod, "_TRUST_GATEWAY_AUTH", True)
    monkeypatch.setattr(app_mod, "_SSO_SHARED_SECRET", "")
    assert gw.get("/me", headers=_hdrs(auth="")).status_code == 401


# Role mapping: auditor → viewer (read-only), enforced by the viewer_read_only
# middleware; superuser maps through and may write.
def test_gateway_auditor_is_read_only(gw, trust_on):
    h = _hdrs(role="auditor", user="auditor1")
    assert gw.get("/me", headers=h).json()["role"] == "viewer"
    assert gw.get("/projects", headers=h).status_code == 200
    r = gw.post("/projects", headers=h, json={"name": "nope"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Your role is read-only (viewer)."


def test_gateway_unknown_role_defaults_to_viewer(gw, trust_on):
    assert gw.get("/me", headers=_hdrs(role="wizard", user="u")).json()["role"] == "viewer"


def test_gateway_operator_not_blocked_by_readonly_middleware(gw, trust_on):
    # An operator must pass the read-only middleware. (Org RBAC may still 403 a gateway
    # user who isn't an org member — but that's a different reason, never the read-only
    # message, which is what confirms the middleware honored the gateway role.)
    r = gw.post("/projects", headers=_hdrs(role="operator", user="op-gw"), json={"name": "x"})
    assert r.json().get("detail") != "Your role is read-only (viewer)."


def test_gateway_superuser_can_write(gw, trust_on):
    # A gateway superuser is a system admin → passes org guards and creates a project.
    r = gw.post("/projects", headers=_hdrs(role="superuser", user="su-gw"),
                json={"name": "gw superuser project"})
    assert r.status_code == 200


# ---- SLOP is the ONLY way in --------------------------------------------------
# SLEP's API is published on its own port, so anything it accepts locally is a
# second front door the gateway never sees: a SLEP-issued bearer token would keep
# working there after the user signed out of SLOP, and a SLEP-local account is one
# SLOP Administration does not manage.
def test_no_local_login_when_slop_owns_identity(gw, trust_on):
    r = gw.post("/login", json={"username": "admin", "password": "whatever"})
    assert r.status_code == 403
    assert "Sysible Linux Operations Platform" in r.json()["detail"]


def test_a_slep_token_stops_working_once_slop_owns_identity(client, monkeypatch):
    """The sign-out gap: a bearer minted before SSO was enabled (or by someone
    hitting the published port directly) must not survive."""
    # `client` carries a real SLEP bearer from the standalone login.
    assert client.get("/runs").status_code == 200

    monkeypatch.setattr(app_mod, "_TRUST_GATEWAY_AUTH", True)
    monkeypatch.setattr(app_mod, "_SSO_SHARED_SECRET", SECRET)
    # Same token, no gateway assertion — refused, because the gateway is now the
    # only identity source.
    assert client.get("/runs").status_code == 401


def test_the_gateway_identity_still_works(gw, trust_on):
    # Close the second door, not the first.
    assert gw.get("/runs", headers=_hdrs()).status_code == 200


def test_standalone_slep_is_unchanged(gw, client):
    assert app_mod._sso_only() is False
    # A bad password is still a 401 from the real login path, not a 403 lockout.
    assert gw.post("/login", json={"username": "admin", "password": "nope"}).status_code in (401, 429)
    assert client.get("/runs").status_code == 200
