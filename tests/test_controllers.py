"""'Connect to Controller' — save a Controller connection, then import from it by
id (no key re-entry). Mocks the Controller's HTTP endpoints."""


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def _fake_get(routes):
    def get(url, headers=None, verify=None, timeout=None):
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        return Resp(404, None)
    return get


def test_connect_list_import_disconnect(client, monkeypatch):
    import backend.controller_import as ci
    monkeypatch.setattr(ci.requests, "get", _fake_get({
        "/agents": Resp(200, {"agents": [{"hostname": "web1", "ip": "10.0.0.11", "environment": "prod"}]}),
        "/remote/hosts": Resp(200, {"db1": {"ip": "10.0.0.21", "user": "deploy", "port": 2222}}),
    }))
    # connect (validates + saves)
    r = client.post("/controllers", json={"name": "Prod", "base_url": "http://ctrl:9000", "api_key": "K"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "connected" and body["total"] == 2
    cid = body["controller"]["id"]

    # list never leaks the key
    listed = client.get("/controllers").json()["controllers"]
    saved = [c for c in listed if c["id"] == cid][0]
    assert "api_key" not in saved and saved["base_url"] == "http://ctrl:9000"

    # re-test
    assert client.post(f"/controllers/{cid}/test").json()["total"] == 2

    # import by saved-controller id (no url/key in the body)
    iid = client.post("/inventories", json={"name": "fromctrl"}).json()["id"]
    d = client.post(f"/inventories/{iid}/import-controller", json={"controller_id": cid}).json()
    assert d["agents"] == 1 and d["ssh"] == 1
    names = {h["name"] for h in client.get(f"/inventories/{iid}/hosts").json()["hosts"]}
    assert names == {"web1", "db1"}

    # disconnect
    assert client.delete(f"/controllers/{cid}").json()["status"] == "disconnected"
    assert all(c["id"] != cid for c in client.get("/controllers").json()["controllers"])


def test_list_hosts_and_selective_import_to_different_inventories(client, monkeypatch):
    import backend.controller_import as ci
    monkeypatch.setattr(ci.requests, "get", _fake_get({
        "/agents": Resp(200, {"agents": [
            {"hostname": "web1", "ip": "10.0.0.11", "environment": "web"},
            {"hostname": "web2", "ip": "10.0.0.12", "environment": "web"}]}),
        "/remote/hosts": Resp(200, {"db1": {"ip": "10.0.0.21", "user": "deploy", "environment": "db"}}),
    }))
    cid = client.post("/controllers", json={"name": "Prod", "base_url": "http://ctrl:9000", "api_key": "K"}).json()["controller"]["id"]

    # List hosts WITHOUT importing.
    hosts = client.get(f"/controllers/{cid}/hosts").json()["hosts"]
    assert {h["name"] for h in hosts} == {"web1", "web2", "db1"}

    # Route different hosts to different inventories.
    web = client.post("/inventories", json={"name": "web-tier"}).json()["id"]
    dbi = client.post("/inventories", json={"name": "db-tier"}).json()["id"]
    r1 = client.post(f"/inventories/{web}/import-controller",
                     json={"controller_id": cid, "host_names": ["web1", "web2"]}).json()
    r2 = client.post(f"/inventories/{dbi}/import-controller",
                     json={"controller_id": cid, "host_names": ["db1"]}).json()
    assert r1["imported"] == 2 and r2["imported"] == 1

    assert {h["name"] for h in client.get(f"/inventories/{web}/hosts").json()["hosts"]} == {"web1", "web2"}
    assert {h["name"] for h in client.get(f"/inventories/{dbi}/hosts").json()["hosts"]} == {"db1"}


def test_connect_rejects_bad_key(client, monkeypatch):
    import backend.controller_import as ci
    monkeypatch.setattr(ci.requests, "get", _fake_get({"/agents": Resp(401, None)}))
    assert client.post("/controllers", json={"base_url": "http://c:9000", "api_key": "BAD"}).status_code == 400


def _fake_post(routes):
    def post(url, json=None, headers=None, verify=None, timeout=None):
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp(json) if callable(resp) else resp
        return Resp(404, None)
    return post


def test_connect_with_username_password(client, monkeypatch):
    """Superuser username/password is exchanged for the API key via /auth/api-key,
    then the key validates against /agents — no raw key entered by the operator."""
    import backend.controller_import as ci
    monkeypatch.setattr(ci.requests, "post", _fake_post({
        "/auth/api-key": Resp(200, {"status": "ok", "api_key": "SECRET"}),
    }))
    monkeypatch.setattr(ci.requests, "get", _fake_get({
        "/agents": Resp(200, {"agents": [{"hostname": "web1", "ip": "10.0.0.11"}]}),
    }))
    r = client.post("/controllers", json={
        "name": "Creds", "base_url": "http://ctrl:9000", "username": "root", "password": "pw"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "connected" and r.json()["total"] == 1
    # The saved connection carries the exchanged key (never surfaced to the client).
    listed = client.get("/controllers").json()["controllers"]
    assert any(c["name"] == "Creds" for c in listed)
    assert all("api_key" not in c for c in listed)


def test_connect_mfa_two_step(client, monkeypatch):
    """When the Controller superuser has MFA, the first call returns mfa_required;
    resubmitting with a TOTP code completes the exchange."""
    import backend.controller_import as ci
    state = {"seen_code": False}

    def api_key_route(payload):
        if (payload or {}).get("totp_code"):
            state["seen_code"] = True
            return Resp(200, {"status": "ok", "api_key": "SECRET"})
        return Resp(200, {"status": "mfa_required"})

    monkeypatch.setattr(ci.requests, "post", _fake_post({"/auth/api-key": api_key_route}))
    monkeypatch.setattr(ci.requests, "get", _fake_get({"/agents": Resp(200, {"agents": []})}))

    first = client.post("/controllers", json={
        "base_url": "http://ctrl:9000", "username": "root", "password": "pw"})
    assert first.status_code == 200 and first.json()["status"] == "mfa_required"

    second = client.post("/controllers", json={
        "base_url": "http://ctrl:9000", "username": "root", "password": "pw", "totp_code": "123456"})
    assert second.status_code == 200 and second.json()["status"] == "connected"
    assert state["seen_code"]


def test_connect_bad_creds_rejected(client, monkeypatch):
    import backend.controller_import as ci
    monkeypatch.setattr(ci.requests, "post", _fake_post({
        "/auth/api-key": Resp(401, {"detail": "Invalid username or password"}),
    }))
    r = client.post("/controllers", json={
        "base_url": "http://ctrl:9000", "username": "root", "password": "nope"})
    assert r.status_code == 400
    assert "Invalid username or password" in r.json()["detail"]


def test_connect_old_controller_no_endpoint(client, monkeypatch):
    """A Controller predating /auth/api-key (404) yields a clear 'use the key' error."""
    import backend.controller_import as ci
    monkeypatch.setattr(ci.requests, "post", _fake_post({"/auth/api-key": Resp(404, None)}))
    r = client.post("/controllers", json={
        "base_url": "http://ctrl:9000", "username": "root", "password": "pw"})
    assert r.status_code == 400
    assert "API key" in r.json()["detail"]
