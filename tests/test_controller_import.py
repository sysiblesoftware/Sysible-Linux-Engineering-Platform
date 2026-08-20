"""Controller inventory import — mock the Controller's two host listings and
assert SLEP maps them correctly (agent hosts + runnable SSH hosts), is
idempotent, tolerates an older Controller with no /remote/hosts, and fails on
bad auth."""
import backend.db as db
import backend.controller_import as ci


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def _mock_get(monkeypatch, routes):
    """routes: dict of path-suffix -> FakeResp (default 404)."""
    def fake_get(url, headers=None, verify=None, timeout=None):
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        return FakeResp(404, None)
    monkeypatch.setattr(ci.requests, "get", fake_get)


def _inv():
    db.init_db()
    return db.create_inventory("imp-" + str(len(db.list_inventories()) + 1))


def test_imports_agents_and_ssh_hosts(monkeypatch):
    iid = _inv()
    _mock_get(monkeypatch, {
        "/agents": FakeResp(200, {"agents": [
            {"hostname": "web1", "ip": "10.0.0.11", "environment": "prod", "host_id": "h1", "platform": "linux"},
        ]}),
        "/remote/hosts": FakeResp(200, {
            "db1": {"ip": "10.0.0.21", "user": "deploy", "port": 2222, "environment": "prod"},
        }),
    })
    summary = ci.import_into_inventory(iid, "controller.local", "KEY")
    assert summary["agents"] == 1 and summary["ssh"] == 1 and summary["total"] == 2

    hosts = {h["name"]: h for h in db.list_hosts(iid)}
    assert hosts["web1"]["address"] == "10.0.0.11" and hosts["web1"]["groups"] == "prod"
    assert hosts["web1"]["variables"]["sysible_source"] == "agent"
    # SSH host is runnable: connection user/port land as ansible vars.
    assert hosts["db1"]["address"] == "10.0.0.21"
    assert hosts["db1"]["variables"]["ansible_user"] == "deploy"
    assert hosts["db1"]["variables"]["ansible_port"] == 2222


def test_reimport_is_idempotent(monkeypatch):
    iid = _inv()
    _mock_get(monkeypatch, {"/agents": FakeResp(200, {"agents": [
        {"hostname": "web1", "ip": "10.0.0.11", "environment": "prod"}]})})
    ci.import_into_inventory(iid, "c", "KEY")
    ci.import_into_inventory(iid, "c", "KEY")           # again
    assert len(db.list_hosts(iid)) == 1                  # no duplicate


def test_missing_ssh_endpoint_is_tolerated(monkeypatch):
    # Older Controller: /remote/hosts 404s. Agents still import; no error raised.
    iid = _inv()
    _mock_get(monkeypatch, {"/agents": FakeResp(200, {"agents": [
        {"hostname": "web1", "ip": "10.0.0.11"}]})})   # /remote/hosts -> default 404
    summary = ci.import_into_inventory(iid, "c", "KEY")
    assert summary["agents"] == 1 and summary["ssh"] == 0 and not summary["errors"]


def test_bad_api_key_raises(monkeypatch):
    iid = _inv()
    _mock_get(monkeypatch, {"/agents": FakeResp(401, None), "/remote/hosts": FakeResp(401, None)})
    import pytest
    with pytest.raises(ci.ControllerImportError):
        ci.import_into_inventory(iid, "c", "BAD")
