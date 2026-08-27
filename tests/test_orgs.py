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


def test_cross_org_resource_isolation(client):
    """An operator in org B cannot reach org A's project files, runs, credentials,
    vault, or infra by guessing ids — the resource-route org guards."""
    acme = client.post("/organizations", json={"name": "Isolate-Acme"}).json()
    a_id = acme["id"]
    default_id = _default_org(client)
    # Victim resources live in Acme (created by the system admin).
    proj = client.post("/projects", json={"name": "acme-proj", "org_id": a_id}).json()
    pid = proj["id"]
    client.put(f"/projects/{pid}/file", json={"path": "secret.yml", "content": "top: secret"})
    cred = client.post("/credentials", json={"name": "acme-cred", "kind": "ssh_password",
                                             "username": "admin", "secret": "pw", "org_id": a_id}).json()
    client.post("/vault", json={"name": "acme_secret", "value": "v", "org_id": a_id})
    # Attacker is an operator ONLY in Default.
    client.post("/users", json={"username": "iso_op", "password": "iso-operator-pw", "role": "operator"})
    op = _login("iso_op", "iso-operator-pw")

    # Reads across the tenant boundary are refused.
    assert op.get(f"/projects/{pid}").status_code == 403
    assert op.get(f"/projects/{pid}/files").status_code == 403
    assert op.get(f"/projects/{pid}/file?path=secret.yml").status_code == 403
    # Writes/mutations are refused.
    assert op.put(f"/projects/{pid}/file", json={"path": "x", "content": "y"}).status_code == 403
    assert op.delete(f"/projects/{pid}").status_code == 403
    assert op.patch(f"/credentials/{cred['id']}", json={"become_password": "x"}).status_code == 403
    assert op.delete(f"/credentials/{cred['id']}").status_code == 403
    # A run in the attacker's OWN project may not borrow Acme's credential.
    myproj = op.post("/projects", json={"name": "mine", "org_id": default_id}).json()
    r = op.post("/runs", json={"project_id": myproj["id"], "kind": "ansible",
                               "target": "site.yml", "credential_id": cred["id"]})
    assert r.status_code == 403
    # The attacker's vault list never shows Acme's secret name.
    names = {s["name"] for s in op.get("/vault").json()["secrets"]}
    assert "acme_secret" not in names


def test_cross_org_pipeline_runs_schedules_hosts_isolation(client):
    """Regression for the audit findings: a pipeline step can't borrow another org's
    credential; ad-hoc pipelines, runs, schedules, hosts, inventories and controllers
    are all org-guarded (no read/tamper/cancel across the tenant boundary by id)."""
    import backend.db as db
    acme = client.post("/organizations", json={"name": "Iso2-Acme"}).json()
    a_id = acme["id"]
    default_id = _default_org(client)
    # Victim resources in Acme (system admin sets them up; db for the non-HTTP ones).
    vproj = client.post("/projects", json={"name": "iso2-acme-proj", "org_id": a_id}).json()
    vpid = vproj["id"]
    vcred = client.post("/credentials", json={"name": "iso2-cred", "kind": "ssh_password",
                                              "username": "admin", "secret": "pw", "org_id": a_id}).json()
    viid = db.create_inventory("iso2-inv", project_id=vpid, org_id=a_id)
    vhid = db.add_host(viid, "vhost", "10.9.9.9")
    vsid = db.create_schedule("iso2-sched", vpid, "ansible", "site.yml", "daily", "02:00")
    vrid = db.create_run(vpid, "ansible", "site.yml")          # queued
    vcid = db.create_controller("iso2-ctrl", "https://ctrl.example", "key", org_id=a_id)

    # Attacker is an operator ONLY in Default.
    client.post("/users", json={"username": "iso2_op", "password": "iso2-operator-pw", "role": "operator"})
    op = _login("iso2_op", "iso2-operator-pw")

    # Finding 2: ad-hoc pipeline aimed at a victim project (e.g. terraform destroy).
    assert op.post("/pipelines/run", json={"project_id": vpid,
                   "steps": [{"kind": "terraform", "target": "destroy"}]}).status_code == 403
    # Finding 1: a pipeline in the attacker's OWN project may not borrow Acme's credential.
    myproj = op.post("/projects", json={"name": "iso2-mine", "org_id": default_id}).json()
    assert op.post("/pipelines/run", json={"project_id": myproj["id"],
                   "steps": [{"kind": "ansible", "target": "site.yml", "credential_id": vcred["id"]}]}).status_code == 403
    # Finding 3: GET /runs never lists another tenant's run.
    assert vrid not in {r["id"] for r in op.get("/runs").json()["runs"]}
    # Finding 9: can't cancel another tenant's run.
    assert op.post(f"/runs/{vrid}/cancel").status_code == 403
    # Finding 4: GET /schedules never lists another tenant's schedule.
    assert vsid not in {s["id"] for s in op.get("/schedules").json()["schedules"]}
    # Finding 5: can't repoint or delete another tenant's schedule.
    assert op.patch(f"/schedules/{vsid}", json={"enabled": False}).status_code == 403
    assert op.delete(f"/schedules/{vsid}").status_code == 403
    # Finding 6: can't delete another tenant's host.
    assert op.delete(f"/hosts/{vhid}").status_code == 403
    # Finding 7: can't repoint another tenant's inventory (jump host / environment).
    assert op.patch(f"/inventories/{viid}", json={"environment": "x"}).status_code == 403
    # Finding 8: can't drive another tenant's Controller key to enumerate its fleet.
    assert op.get(f"/controllers/{vcid}/hosts").status_code == 403
