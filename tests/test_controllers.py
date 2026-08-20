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


def test_connect_rejects_bad_key(client, monkeypatch):
    import backend.controller_import as ci
    monkeypatch.setattr(ci.requests, "get", _fake_get({"/agents": Resp(401, None)}))
    assert client.post("/controllers", json={"base_url": "http://c:9000", "api_key": "BAD"}).status_code == 400
