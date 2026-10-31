"""Under SLOP SSO, a signed-in user must land in a SLEP they can use.

SLEP honoured the gateway's identity — /me answered with the right username and
role — and then provisioned nothing. The identity had no row in `admins` and no
org membership, so:

  * _visible_org_ids → db.orgs_for_user(user) → [] → db.list_projects([]) → [].
    An operator or auditor opened /slep/ and found Projects, Inventories, Runs,
    Credentials and Templates all empty. Signed in, and nothing there.
  * there was no way to grant access. POST /organizations/{oid}/members 404s
    with "No such user." for an identity with no local row, and the console's
    member picker is filled from GET /users — which returned [] for the same
    reason, as a <select> with no free-text fallback and Add greyed out.
  * db.count_admins() stayed 0 for ever, so POST /setup's 409 guard never armed.
    Any signed-in SLOP user, auditor included, could plant a SLEP-local
    superuser that SLOP Administration can neither see nor revoke — inert while
    SSO holds, live the moment the trust flag or the shared secret is lost,
    which deploy/sysiblectl exists partly to recover from. In that same window
    the console served "Create the first administrator" to whoever opened it.

So: a trusted gateway identity is provisioned on first sight, /setup is refused
under SSO exactly as /login is, and /healthz says which mode it is in so the
console can offer a sign-in-at-SLOP screen instead of a form that cannot work.
"""
import pytest
from fastapi.testclient import TestClient

import backend.app as app_mod
import backend.db as db
from backend.app import app

SECRET = "shared-gateway-secret-abc123"


@pytest.fixture(autouse=True)
def _no_leftover_accounts(client):
    """Remove the accounts these tests provision.

    The `client` fixture is session-scoped and so is the database, and
    provisioning is the whole point of this file — including a gateway
    SUPERUSER, which adds a second superuser row. tests/test_rbac.py asserts
    that `admin` is the only one (that is what makes the last-superuser guard
    fire), so leaving one behind lets that test demote `admin` and every later
    test using `client` fails on a 401.
    """
    before = {u["username"] for u in db.list_admins()}
    yield
    for name in {u["username"] for u in db.list_admins()} - before:
        for oid in db.orgs_for_user(name):
            db.remove_org_member(oid, name)
        db.delete_admin(name)


@pytest.fixture()
def gw(client):
    """A bare client: identity can only come from gateway headers."""
    return TestClient(app)


@pytest.fixture()
def trust_on(monkeypatch):
    monkeypatch.setattr(app_mod, "_TRUST_GATEWAY_AUTH", True)
    monkeypatch.setattr(app_mod, "_SSO_SHARED_SECRET", SECRET)


def _hdrs(user, role="operator", auth=SECRET):
    h = {"X-Sysible-User": user, "X-Sysible-Role": role}
    if auth is not None:
        h["X-Sysible-Auth"] = auth
    return h


# ---------------------------------------------------------------------------
# provisioning
# ---------------------------------------------------------------------------
def test_an_sso_operator_is_provisioned_on_first_sight(gw, trust_on):
    assert db.get_admin("sso-op") is None
    r = gw.get("/me", headers=_hdrs("sso-op", "operator"))
    assert r.status_code == 200, r.text

    row = db.get_admin("sso-op")
    assert row is not None, "the gateway identity was honoured but never provisioned"
    assert row["role"] == "operator"
    assert db.orgs_for_user("sso-op") == [db.default_org_id()]
    assert r.json()["organizations"], "an SSO user should see the org they are in"


def test_the_provisioned_account_cannot_be_logged_into_locally(gw, trust_on):
    """The row exists so membership and the member picker work — not as a second
    way in. It gets an unusable password, so if SSO is ever turned off the
    account cannot be used to sign in."""
    gw.get("/me", headers=_hdrs("sso-op2", "operator"))
    row = db.get_admin("sso-op2")
    assert row["pw_hash"] and row["salt"]
    for guess in ("", "password", "changeme123", row["pw_hash"], row["salt"]):
        assert not app_mod._check_password(guess, row["pw_hash"], row["salt"]), guess


def test_an_sso_auditor_is_read_only(gw, trust_on):
    gw.get("/me", headers=_hdrs("sso-auditor", "auditor"))
    assert db.get_admin("sso-auditor")["role"] == "viewer"
    r = gw.post("/projects", json={"name": "auditor-should-not"},
                headers=_hdrs("sso-auditor", "auditor"))
    assert r.status_code == 403, r.text


def test_an_sso_operator_sees_and_creates_in_the_default_org(gw, trust_on):
    h = _hdrs("sso-op3", "operator")
    assert gw.get("/projects", headers=h).status_code == 200
    r = gw.post("/projects", json={"name": "sso-op3-project"}, headers=h)
    assert r.status_code == 200, r.text
    names = [p["name"] for p in gw.get("/projects", headers=h).json()["projects"]]
    assert "sso-op3-project" in names


# Under SSO the bearer path is deliberately off, so even the superuser acts
# through the gateway — a SLEP-issued token would be the second front door
# _sso_only() exists to close.
_SU = {"X-Sysible-User": "sso-super", "X-Sysible-Role": "superuser",
       "X-Sysible-Auth": SECRET}


def test_a_superuser_can_grant_an_sso_user_access_to_another_org(gw, trust_on):
    """The dead end: "No such user." for a real platform account."""
    gw.get("/me", headers=_hdrs("sso-op4", "operator"))
    org = gw.post("/organizations", json={"name": "SSO Grant Target"},
                  headers=_SU).json()
    r = gw.post(f"/organizations/{org['id']}/members",
                json={"username": "sso-op4", "role": "operator"}, headers=_SU)
    assert r.status_code == 200, r.text
    users = [u["username"] for u in gw.get("/users", headers=_SU).json()["users"]]
    assert "sso-op4" in users, "the member picker is filled from this list"
    assert org["id"] in db.orgs_for_user("sso-op4")


def test_provisioning_never_overwrites_what_an_operator_set(gw, trust_on):
    """It runs on every request, so it has to be a no-op after the first."""
    gw.get("/me", headers=_hdrs("sso-op5", "operator"))
    org = gw.post("/organizations", json={"name": "SSO No Clobber"},
                  headers=_SU).json()
    gw.post(f"/organizations/{org['id']}/members",
            json={"username": "sso-op5", "role": "admin"}, headers=_SU)
    gw.post(f"/organizations/{db.default_org_id()}/members",
            json={"username": "sso-op5", "role": "viewer"}, headers=_SU)

    for _ in range(3):
        gw.get("/me", headers=_hdrs("sso-op5", "operator"))

    assert db.effective_org_role("sso-op5", org["id"]) == "admin"
    assert db.effective_org_role("sso-op5", db.default_org_id()) == "viewer"


def test_an_existing_local_account_is_not_touched(gw, trust_on, client):
    """A SLEP set up standalone and later put behind SLOP: both default to the
    name `admin`. Provisioning must not rewrite that row's role or password."""
    before = db.get_admin("admin")
    assert before is not None, "the conftest superuser should exist"
    gw.get("/me", headers=_hdrs("admin", "auditor"))
    after = db.get_admin("admin")
    assert after["role"] == before["role"]
    assert after["pw_hash"] == before["pw_hash"]


def test_an_existing_row_is_not_written_to_at_all(gw, trust_on, monkeypatch):
    """Not just "the row survives" — nothing is attempted.

    Without this, the early return looks optional: removing it leaves the row
    intact anyway, because add_admin is an INSERT that fails on the primary key
    and the failure is swallowed so a request is never 500'd over provisioning.
    That is safe by accident, and it would stop being safe the day add_admin
    becomes an upsert or a role realignment is added. So assert the write is
    never even reached.
    """
    assert db.get_admin("admin") is not None

    # RECORD rather than raise: _ensure_sso_account swallows exceptions on
    # purpose (a request must never 500 over provisioning), so a raising stub
    # would be caught and the test would pass with the guard removed.
    wrote = []
    for name in ("add_admin", "set_org_member", "set_admin_role", "set_admin_password"):
        monkeypatch.setattr(db, name,
                            lambda *a, _n=name, **k: wrote.append(_n))
    assert gw.get("/me", headers=_hdrs("admin", "auditor")).status_code == 200
    assert wrote == [], f"provisioning wrote to an account it did not create: {wrote}"


def test_nothing_is_provisioned_without_the_shared_secret(gw, trust_on):
    gw.get("/me", headers=_hdrs("sso-forged", "superuser", auth="wrong"))
    assert db.get_admin("sso-forged") is None


def test_nothing_is_provisioned_when_trust_is_off(gw):
    gw.get("/me", headers=_hdrs("sso-off", "superuser"))
    assert db.get_admin("sso-off") is None


# ---------------------------------------------------------------------------
# /setup
# ---------------------------------------------------------------------------
def test_setup_is_refused_under_sso(gw, trust_on):
    r = gw.post("/setup", json={"username": "backdoor", "password": "Hunter2!!xyz"})
    assert r.status_code == 403, r.text
    assert "Operations Platform" in r.json()["detail"]
    assert db.get_admin("backdoor") is None


def test_setup_is_refused_under_sso_even_for_an_auditor(gw, trust_on):
    r = gw.post("/setup", json={"username": "backdoor2", "password": "Hunter2!!xyz"},
                headers=_hdrs("sso-auditor2", "auditor"))
    assert r.status_code == 403, r.text
    assert db.get_admin("backdoor2") is None


def test_setup_still_works_on_a_standalone_first_run(gw):
    """The refusal must be about SSO, not about setup."""
    assert gw.post("/setup", json={"username": "x", "password": "Hunter2!!xyz"}
                   ).status_code == 409   # this install already has an admin
    assert "Operations Platform" not in gw.post(
        "/setup", json={"username": "x", "password": "Hunter2!!xyz"}).json()["detail"]


# ---------------------------------------------------------------------------
# what the console is told
# ---------------------------------------------------------------------------
def test_health_says_which_identity_mode_this_is(gw, trust_on):
    """The console reads this before rendering anything. Without it, a 401 plus
    admins==0 looked like a fresh install and it offered a create-administrator
    wizard."""
    body = gw.get("/healthz").json()
    assert body["sso"] is True


def test_health_says_so_in_standalone_mode_too(gw):
    assert gw.get("/healthz").json()["sso"] is False


def test_the_console_asks_which_mode_before_offering_a_form():
    """The server half is useless if the SPA ignores it. Pinned as source, the
    way the repo pins the rest of the console contract."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "src"
           / "App.jsx").read_text()
    assert "h.sso" in src, "the bootstrap must read the identity mode from /api/health"
    assert "SsoSignIn" in src, "there must be a sign-in-at-SLOP screen to render"
    # The setup wizard must be the ELSE branch, never offered alongside SSO.
    i_sso, i_setup = src.index("if (h.sso)"), src.index("setNeedSetup(true)")
    assert i_sso < i_setup, "the SSO check has to come first"
    assert "else if (h.admins === 0)" in src


def test_the_built_console_contains_the_sso_screen():
    """dist is what actually ships. A source-only change here would be invisible
    in the Docker image, which serves the build."""
    from pathlib import Path
    dist = Path(__file__).resolve().parents[1] / "webgui" / "frontend" / "dist"
    if not dist.is_dir():
        pytest.skip("frontend/dist not built in this checkout")
    blobs = list(dist.rglob("*.js"))
    assert blobs, "no built JS found"
    assert any("Sign in at the Sysible Linux Operations Platform" in b.read_text(
        errors="ignore") for b in blobs), "rebuild the console (npm run build)"
