"""GET /runs takes a limit, and says which one it used.

db.list_runs has always capped at 100; the route accepted no limit at all. So a
caller that wanted more got 100 with nothing to say it had been truncated —
SLOP's Visualizer asks its sources for up to 500 events, slices the reply to
that, and reports anything odd in `notes`. For SLEP it silently received 100
and reported nothing, so on a busy instance a truncated feed looked complete.
"""
import pytest


def _make_runs(client, project, n):
    import backend.db as db
    ids = []
    for i in range(n):
        ids.append(db.create_run(project["id"], "ansible", f"play-{i}.yml", "all", "{}", "tester"))
    return ids


def test_the_default_is_unchanged(client, project):
    r = client.get("/runs")
    assert r.status_code == 200
    assert r.json()["limit"] == 100


def test_a_bigger_limit_returns_more_than_a_hundred(client, project):
    import backend.db as db
    before = len(db.list_runs(limit=10000))
    need = 120 - before
    if need > 0:
        _make_runs(client, project, need)
    body = client.get("/runs", params={"limit": 200}).json()
    assert body["limit"] == 200
    assert len(body["runs"]) > 100, (
        f"asked for 200, got {len(body['runs'])} — the cap is still 100")


def test_a_small_limit_is_honoured(client, project):
    body = client.get("/runs", params={"limit": 3}).json()
    assert body["limit"] == 3
    assert len(body["runs"]) <= 3


@pytest.mark.parametrize("asked,used", [(0, 1), (-5, 1), (99999, 500)])
def test_the_limit_is_bounded(client, asked, used):
    """A caller must not be able to ask for the whole table, or for nothing."""
    assert client.get("/runs", params={"limit": asked}).json()["limit"] == used


def test_the_limit_does_not_widen_org_scope(client, project):
    """The limit is a page size, not a way past the tenancy filter."""
    import backend.db as db
    acme = client.post("/organizations", json={"name": "Runs Scope Org"}).json()
    proj = client.post("/projects", json={"name": "runs-scope-proj",
                                          "org_id": acme["id"]}).json()
    db.create_run(proj["id"], "ansible", "secret.yml", "all", "{}", "tester")
    client.post("/users", json={"username": "runs-outsider",
                                "password": "runs-outsider-1", "role": "operator"})

    from fastapi.testclient import TestClient
    from backend.app import app
    out = TestClient(app)
    tok = out.post("/login", json={"username": "runs-outsider",
                                   "password": "runs-outsider-1"}).json()["token"]
    out.headers.update({"Authorization": f"Bearer {tok}"})
    body = out.get("/runs", params={"limit": 500}).json()
    assert all(r["project_id"] != proj["id"] for r in body["runs"])
