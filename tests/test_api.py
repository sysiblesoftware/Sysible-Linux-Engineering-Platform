"""SLEP backend API tests — the release-readiness safety net.

Covers auth gating, project + IDE file CRUD (incl. the path-escape guard),
inventory/hosts, and run dispatch/validation. The run tests use paths that reach
a terminal state WITHOUT needing ansible/terraform installed or any real host
(empty inventory, missing .tf), so they're fast and hermetic in CI.
"""
import time


def _wait_terminal(client, run_id, timeout=8):
    for _ in range(timeout * 4):
        st = client.get(f"/runs/{run_id}").json()["status"]
        if st in ("success", "failed", "canceled"):
            return st
        time.sleep(0.25)
    return "timeout"


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_auth_required(client):
    # No bearer -> 401.
    r = client.get("/projects", headers={"Authorization": ""})
    assert r.status_code == 401


def test_project_file_crud_and_path_guard(client, project):
    pid = project["id"]
    # write + read
    assert client.put(f"/projects/{pid}/file", json={"path": "site.yml", "content": "- hosts: all\n"}).status_code == 200
    assert client.get(f"/projects/{pid}/file", params={"path": "site.yml"}).json()["content"] == "- hosts: all\n"
    # tree lists it
    files = client.get(f"/projects/{pid}/files").json()["files"]
    assert any(f["path"] == "site.yml" for f in files)
    # path escape is refused
    assert client.get(f"/projects/{pid}/file", params={"path": "../../../etc/passwd"}).status_code == 400
    assert client.put(f"/projects/{pid}/file", json={"path": "../evil", "content": "x"}).status_code == 400


def test_inventory_and_hosts(client, project):
    iid = client.post("/inventories", json={"name": "prod", "project_id": project["id"]}).json()["id"]
    assert client.post(f"/inventories/{iid}/hosts", json={"name": "web1", "address": "10.0.0.11", "groups": "web"}).status_code == 200
    hosts = client.get(f"/inventories/{iid}/hosts").json()["hosts"]
    assert len(hosts) == 1 and hosts[0]["address"] == "10.0.0.11"


def test_bastion_rejects_invalid_ip(client):
    # An octet > 255 (192.268.8.212) is caught up front, not at SSH time.
    r = client.post("/inventories", json={"name": "b1", "bastion": "admin@192.268.8.212"})
    assert r.status_code == 400 and "valid IP" in r.json()["detail"]
    # A valid jump host (with user + optional port) is accepted.
    ok = client.post("/inventories", json={"name": "b2", "bastion": "admin@192.168.8.212:22"})
    assert ok.status_code == 200
    # A hostname (not dotted-numeric) is left alone.
    assert client.post("/inventories", json={"name": "b3", "bastion": "bastion.example.com"}).status_code == 200
    # PATCH is guarded too.
    iid = ok.json()["id"]
    assert client.patch(f"/inventories/{iid}", json={"bastion": "999.1.1.1"}).status_code == 400


def test_collections_status_shape(client):
    d = client.get("/engines/collections").json()
    assert "common" in d and "installed" in d and "missing_common" in d
    assert "community.general" in d["common"]


def test_credentials_hide_secret(client):
    client.post("/credentials", json={"name": "k", "kind": "ssh", "username": "ansible", "secret": "PRIVATEKEY"})
    creds = client.get("/credentials").json()["credentials"]
    c = [x for x in creds if x["name"] == "k"][0]
    assert "secret" not in c            # never returned to the browser


def test_unknown_engine_rejected(client, project):
    r = client.post("/runs", json={"project_id": project["id"], "kind": "puppet", "target": "x"})
    assert r.status_code == 400


def test_run_missing_target_rejected(client, project):
    r = client.post("/runs", json={"project_id": project["id"], "kind": "ansible", "target": ""})
    assert r.status_code == 400


def test_ansible_run_empty_inventory_fails_fast(client, project):
    iid = client.post("/inventories", json={"name": "empty", "project_id": project["id"]}).json()["id"]
    rid = client.post("/runs", json={"project_id": project["id"], "kind": "ansible",
                                     "target": "site.yml", "inventory_id": iid}).json()["run_id"]
    assert _wait_terminal(client, rid) == "failed"
    log = client.get(f"/runs/{rid}/log").text
    assert "No hosts" in log


def test_terraform_run_without_tf_fails_fast(client, project):
    rid = client.post("/runs", json={"project_id": project["id"], "kind": "terraform",
                                     "target": "plan"}).json()["run_id"]
    assert _wait_terminal(client, rid) == "failed"
    assert "No .tf files" in client.get(f"/runs/{rid}/log").text
