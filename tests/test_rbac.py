"""RBAC — viewer (read-only), operator (author + run, no user admin), superuser
(everything incl. user management). The `client` fixture is the superuser."""
from fastapi.testclient import TestClient


def _login(username, password):
    from backend.app import app
    c = TestClient(app)
    tok = c.post("/login", json={"username": username, "password": password}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def test_role_matrix(client):
    # superuser creates an operator and a viewer
    assert client.post("/users", json={"username": "op1", "password": "operator-pw1", "role": "operator"}).status_code == 200
    assert client.post("/users", json={"username": "vw1", "password": "viewer-pw123", "role": "viewer"}).status_code == 200
    op = _login("op1", "operator-pw1")
    vw = _login("vw1", "viewer-pw123")

    # viewer: reads ok, writes blocked, vault hidden
    assert vw.get("/projects").status_code == 200
    assert vw.post("/projects", json={"name": "nope"}).status_code == 403
    assert vw.get("/vault").status_code == 403

    # operator: can author + run + use vault, but not manage users or connect controllers
    assert op.post("/projects", json={"name": "op-proj"}).status_code == 200
    assert op.post("/vault", json={"name": "opsecret", "value": "v"}).status_code == 200
    assert op.get("/users").status_code == 403
    assert op.post("/controllers", json={"base_url": "http://c:9000", "api_key": "k"}).status_code == 403

    # superuser: full access
    assert client.get("/users").status_code == 200
    assert any(u["username"] == "op1" and u["role"] == "operator" for u in client.get("/users").json()["users"])


def test_me_reports_role(client):
    assert client.get("/me").json()["role"] == "superuser"


def test_last_superuser_guards(client):
    # only `admin` is a superuser → can't demote or delete it
    assert client.patch("/users/admin", json={"role": "operator"}).status_code == 400
    assert client.delete("/users/admin").status_code == 400   # also self


def test_change_role_and_delete(client):
    client.post("/users", json={"username": "temp1", "password": "temp-pw-123", "role": "viewer"})
    assert client.patch("/users/temp1", json={"role": "operator"}).json()["role"] == "operator"
    assert client.delete("/users/temp1").json()["status"] == "deleted"
    assert "temp1" not in [u["username"] for u in client.get("/users").json()["users"]]
