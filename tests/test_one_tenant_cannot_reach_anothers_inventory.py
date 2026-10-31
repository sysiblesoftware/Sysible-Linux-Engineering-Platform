"""Every /inventories/{iid}/… route goes through the org guard.

_guard_inventory is described in app.py as "the single choke point that stops
one tenant reaching another's" — and nothing enforces that it is actually on
each route. Six of the eleven inventory routes skipped it:

    POST /inventories/{iid}/test-connection          Depends(current_user)
    GET  /inventories/{iid}/test-connection/log      Depends(current_user)
    GET  /inventories/{iid}/distribute-key/log       Depends(current_user)
    GET  /inventories/{iid}/prepare-bastion/log      Depends(current_user)
    POST /inventories/{iid}/distribute-key           Depends(require_superuser)
    POST /inventories/{iid}/prepare-bastion          Depends(require_superuser)

`current_user` is the lowest tier — any signed-in viewer, in any org. So a
viewer in one org could start an SSH probe against another org's hosts, choosing
any stored credential by id (keydist.start_test loads it with
include_secret=True and no org check of its own), and could read the logs of
another org's key distribution and bastion preparation: host names, addresses,
the jump host, the credential's label, and the per-host SSH outcome.

The two superuser POSTs were not a tenancy hole — a system admin is 'admin' in
every org by design — but they are on the guard now too, so the rule is "every
route", which is the only version of the rule a test can keep true.

The structural test below is the point: it walks the route table rather than
naming routes, so a seventh is covered the day it is added.
"""
import itertools
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "backend" / "app.py"


def _login(username, password):
    from backend.app import app
    c = TestClient(app)
    tok = c.post("/login", json={"username": username, "password": password}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def test_every_inventory_route_calls_the_guard():
    """Structural, and deliberately so. Several of these routes only START a
    background thread, so a behavioural test cannot see whether the guard ran
    before it — but it can see whether the call is there at all."""
    src = APP.read_text()
    blocks = re.split(r"\n(?=@app\.(?:get|post|put|patch|delete)\()", src)
    missing = []
    for b in blocks:
        m = re.match(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', b)
        if not m or "{iid}" not in m.group(2):
            continue
        if not re.search(r"_guard_inventory\(|_guard_object_org\(|_require_org\(", b):
            missing.append(f"{m.group(1).upper()} {m.group(2)}")
    assert missing == [], missing


_SEQ = itertools.count(1)


@pytest.fixture()
def two_orgs(client):
    """An inventory in another org, plus an operator with no access to it.

    Everything is uniquely named: the `client` fixture is session-scoped, so a
    second test reusing a name gets an error from the duplicate rather than the
    403 it was looking for.
    """
    n = next(_SEQ)
    who, pw = f"outsider{n}", f"outsider-pw-{n}0"
    acme = client.post("/organizations", json={"name": f"Acme Tenancy {n}"}).json()
    proj = client.post("/projects", json={"name": f"acme-proj-{n}",
                                          "org_id": acme["id"]}).json()
    inv = client.post("/inventories", json={"name": f"acme-inv-{n}",
                                            "project_id": proj["id"],
                                            "org_id": acme["id"]}).json()
    client.post(f"/inventories/{inv['id']}/hosts",
                json={"name": f"acme-db-{n}", "address": "10.9.9.9"})
    client.post("/users", json={"username": who, "password": pw, "role": "operator"})
    cred = client.post("/credentials", json={"name": f"acme-key-{n}", "kind": "ssh",
                                             "username": "root", "secret": "s3cret",
                                             "org_id": acme["id"]}).json()
    return {"org": acme, "inv": inv, "cred": cred, "who": who, "pw": pw,
            "outsider": _login(who, pw)}


def test_an_outsider_cannot_probe_another_orgs_hosts(two_orgs):
    iid = two_orgs["inv"]["id"]
    r = two_orgs["outsider"].post(f"/inventories/{iid}/test-connection",
                                  json={"credential_id": two_orgs["cred"]["id"]})
    assert r.status_code in (403, 404), r.text


@pytest.mark.parametrize("path", ["test-connection/log", "distribute-key/log",
                                  "prepare-bastion/log"])
def test_an_outsider_cannot_read_another_orgs_job_log(two_orgs, path):
    iid = two_orgs["inv"]["id"]
    r = two_orgs["outsider"].get(f"/inventories/{iid}/{path}")
    assert r.status_code in (403, 404), f"{path}: {r.status_code} {r.text[:200]}"


def test_a_member_of_that_org_still_can(client, two_orgs):
    """The guard has to let the tenant through, or it is just an outage."""
    iid, org = two_orgs["inv"]["id"], two_orgs["org"]["id"]
    client.post(f"/organizations/{org}/members",
                json={"username": two_orgs["who"], "role": "operator"})
    member = _login(two_orgs["who"], two_orgs["pw"])
    for path in ("test-connection/log", "distribute-key/log", "prepare-bastion/log"):
        assert member.get(f"/inventories/{iid}/{path}").status_code == 200, path


def test_a_viewer_may_read_a_log_but_not_start_a_probe(client, two_orgs):
    """Reading a log is a viewer action; opening SSH connections to the fleet is
    not, so the two must not share a role floor."""
    iid, org = two_orgs["inv"]["id"], two_orgs["org"]["id"]
    client.post(f"/organizations/{org}/members",
                json={"username": two_orgs["who"], "role": "viewer"})
    viewer = _login(two_orgs["who"], two_orgs["pw"])
    assert viewer.get(f"/inventories/{iid}/test-connection/log").status_code == 200
    assert viewer.post(f"/inventories/{iid}/test-connection", json={}).status_code == 403


def test_a_probe_cannot_borrow_a_credential_from_a_third_org(client, two_orgs):
    """The inventory guard is not enough on its own: credential_id is a separate
    id the caller chooses, and keydist loads it with include_secret=True."""
    iid, org = two_orgs["inv"]["id"], two_orgs["org"]["id"]
    third = client.post("/organizations",
                        json={"name": f"Third Party {two_orgs['org']['id']}"}).json()
    other_cred = client.post("/credentials", json={"name": f"third-key-{third['id']}",
                                                   "kind": "ssh",
                                                   "username": "root", "secret": "nope",
                                                   "org_id": third["id"]}).json()
    client.post(f"/organizations/{org}/members",
                json={"username": two_orgs["who"], "role": "operator"})
    member = _login(two_orgs["who"], two_orgs["pw"])
    r = member.post(f"/inventories/{iid}/test-connection",
                    json={"credential_id": other_cred["id"]})
    assert r.status_code in (403, 404), r.text
    # Its own org's credential is fine.
    assert member.post(f"/inventories/{iid}/test-connection",
                       json={"credential_id": two_orgs["cred"]["id"]}).status_code == 200
