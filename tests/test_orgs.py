"""Organizations + Teams + per-org RBAC. The `client` fixture is the system
superuser (system admin: sees every org, manages orgs/teams/users)."""
from fastapi.testclient import TestClient


def _login(username, password):
    from backend.app import app
    c = TestClient(app)
    tok = c.post("/login", json={"username": username, "password": password}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _default_org(client):
    orgs = client.get("/organizations").json()["organizations"]
    return next(o for o in orgs if o["slug"] == "default")["id"]


def test_default_org_seeded_and_superuser_sees_all(client):
    orgs = client.get("/organizations").json()["organizations"]
    assert any(o["slug"] == "default" for o in orgs)
    # /me reports system_admin + org membership
    me = client.get("/me").json()
    assert me["system_admin"] is True
    assert any(o["slug"] == "default" for o in me["organizations"])


def test_org_scoping_and_membership(client):
    default_id = _default_org(client)
    # Only a system admin can create an org.
    acme = client.post("/organizations", json={"name": "Acme"}).json()
    assert acme["id"]

    # A fresh operator is auto-enrolled in Default, NOT in Acme.
    client.post("/users", json={"username": "op2", "password": "operator-pw2", "role": "operator"})
    op = _login("op2", "operator-pw2")

    seen = {o["id"] for o in op.get("/organizations").json()["organizations"]}
    assert default_id in seen and acme["id"] not in seen

    # Can create in Default (member/operator) …
    assert op.post("/projects", json={"name": "in-default", "org_id": default_id}).status_code == 200
    # … but not in Acme (no membership there).
    assert op.post("/projects", json={"name": "in-acme", "org_id": acme["id"]}).status_code == 403

    # A project created in Acme by the superuser is invisible to op.
    client.post("/projects", json={"name": "acme-secret", "org_id": acme["id"]})
    assert "acme-secret" not in [p["name"] for p in op.get("/projects").json()["projects"]]

    # Grant op viewer in Acme → can now see it, but still can't create (viewer).
    client.post(f"/organizations/{acme['id']}/members", json={"username": "op2", "role": "viewer"})
    assert acme["id"] in {o["id"] for o in op.get("/organizations").json()["organizations"]}
    assert "acme-secret" in [p["name"] for p in op.get("/projects").json()["projects"]]
    assert op.post("/projects", json={"name": "still-no", "org_id": acme["id"]}).status_code == 403

    # Promote to operator in Acme → can create now.
    client.post(f"/organizations/{acme['id']}/members", json={"username": "op2", "role": "operator"})
    assert op.post("/projects", json={"name": "now-yes", "org_id": acme["id"]}).status_code == 200


def test_team_confers_org_role(client):
    org = client.post("/organizations", json={"name": "Beta"}).json()
    client.post("/users", json={"username": "vw2", "password": "viewer-pw-2x", "role": "operator"})
    # vw2 has no access to Beta yet.
    vw = _login("vw2", "viewer-pw-2x")
    assert org["id"] not in {o["id"] for o in vw.get("/organizations").json()["organizations"]}

    # A team that confers 'operator', with vw2 as a member, grants access.
    team = client.post(f"/organizations/{org['id']}/teams",
                       json={"name": "Engineers", "org_role": "operator"}).json()
    client.post(f"/teams/{team['id']}/members", json={"username": "vw2"})
    assert org["id"] in {o["id"] for o in vw.get("/organizations").json()["organizations"]}
    assert vw.post("/projects", json={"name": "beta-proj", "org_id": org["id"]}).status_code == 200


def test_only_system_admin_manages_orgs(client):
    client.post("/users", json={"username": "op3", "password": "operator-pw3", "role": "operator"})
    op = _login("op3", "operator-pw3")
    # A non-system-admin can't create or delete organizations.
    assert op.post("/organizations", json={"name": "Nope"}).status_code == 403
    default_id = _default_org(client)
    assert op.delete(f"/organizations/{default_id}").status_code in (400, 403)
