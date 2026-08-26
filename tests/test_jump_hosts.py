"""Reusable jump hosts: CRUD, injection-safe validation, org scoping."""


def test_jump_host_crud_and_validation(client):
    # create
    j = client.post("/jump-hosts", json={"name": "hv1", "host": "192.168.8.212", "username": "admin"})
    assert j.status_code == 200, j.text
    jid = j.json()["id"]
    assert j.json()["bastion"] == "admin@192.168.8.212"
    # non-default port shows in the bastion
    j2 = client.post("/jump-hosts", json={"name": "hv2", "host": "10.0.0.1", "username": "ops", "port": 2222})
    assert j2.json()["bastion"] == "ops@10.0.0.1:2222"
    # list includes both
    names = {x["name"] for x in client.get("/jump-hosts").json()["jump_hosts"]}
    assert {"hv1", "hv2"} <= names
    # injection / bad input rejected
    assert client.post("/jump-hosts", json={"name": "x", "host": "$(id)@h"}).status_code == 400
    assert client.post("/jump-hosts", json={"name": "x", "host": "1.2.3.4", "username": "-oProxyCommand=x"}).status_code == 400
    assert client.post("/jump-hosts", json={"name": "x", "host": "1.2.3.4", "port": "notnum"}).status_code == 400
    assert client.post("/jump-hosts", json={"name": "", "host": "1.2.3.4"}).status_code == 400
    # delete
    assert client.delete(f"/jump-hosts/{jid}").status_code == 200
    names = {x["name"] for x in client.get("/jump-hosts").json()["jump_hosts"]}
    assert "hv1" not in names


def test_jump_host_org_scoped(client):
    from fastapi.testclient import TestClient
    from backend.app import app
    acme = client.post("/organizations", json={"name": "JH-Acme"}).json()
    client.post("/jump-hosts", json={"name": "acme-gw", "host": "10.9.9.9", "username": "root", "org_id": acme["id"]})
    client.post("/users", json={"username": "jh_op", "password": "jh-operator-pw", "role": "operator"})
    op = TestClient(app)
    tok = op.post("/login", json={"username": "jh_op", "password": "jh-operator-pw"}).json()["token"]
    op.headers.update({"Authorization": f"Bearer {tok}"})
    # operator only in Default can't see Acme's jump host, nor create one in Acme
    assert "acme-gw" not in {x["name"] for x in op.get("/jump-hosts").json()["jump_hosts"]}
    assert op.post("/jump-hosts", json={"name": "no", "host": "1.1.1.1", "org_id": acme["id"]}).status_code == 403
