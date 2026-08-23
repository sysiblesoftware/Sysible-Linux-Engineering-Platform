"""Project hierarchy — sub-projects nest under a parent via parent_id; moving is
cycle-guarded; deleting a parent promotes its children instead of orphaning them."""


def test_sub_project_nesting_and_org_inheritance(client):
    parent = client.post("/projects", json={"name": "Platform"}).json()
    child = client.post("/projects", json={"name": "Web Tier", "parent_id": parent["id"]}).json()
    assert child["parent_id"] == parent["id"]
    # A sub-project inherits its parent's organization.
    assert child["org_id"] == parent["org_id"]


def test_move_and_cycle_guard(client):
    a = client.post("/projects", json={"name": "A"}).json()
    b = client.post("/projects", json={"name": "B", "parent_id": a["id"]}).json()
    # Move B to top level.
    assert client.patch(f"/projects/{b['id']}", json={"parent_id": None}).json()["parent_id"] in (None,)
    # Re-nest B under A, then try to make A a child of B → cycle, refused.
    client.patch(f"/projects/{b['id']}", json={"parent_id": a["id"]})
    assert client.patch(f"/projects/{a['id']}", json={"parent_id": b["id"]}).status_code == 400


def test_delete_promotes_children(client):
    top = client.post("/projects", json={"name": "Top"}).json()
    mid = client.post("/projects", json={"name": "Mid", "parent_id": top["id"]}).json()
    leaf = client.post("/projects", json={"name": "Leaf", "parent_id": mid["id"]}).json()
    # Delete the middle project → its child is promoted to Top, not deleted.
    client.delete(f"/projects/{mid['id']}")
    got = next(p for p in client.get("/projects").json()["projects"] if p["id"] == leaf["id"])
    assert got["parent_id"] == top["id"]
