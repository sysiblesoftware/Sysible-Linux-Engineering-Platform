"""Agent (pull) enrollment: SLEP downloads a one-time bundle from the Controller with
its machine API key and installs it on each VM over SSH, so the VM self-enrolls — the
fix for 'enrollment not working' (SSH-host registration needed a superuser token SLEP
never has). Unit-level: the bundle fetch and the SSH install-command builder."""
import backend.controller_import as ci
import backend.keydist as keydist


class _Resp:
    def __init__(self, status, content=b"", payload=None, text=""):
        self.status_code = status
        self.content = content
        self._p = payload
        self.text = text

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


def test_fetch_agent_bundle_ok(monkeypatch):
    calls = {}

    def fake_get(url, headers=None, verify=None, timeout=None):
        calls["url"] = url
        calls["key"] = (headers or {}).get("X-API-Key")
        return _Resp(200, content=b"PK\x03\x04zipbytes")

    monkeypatch.setattr(ci.requests, "get", fake_get)
    out = ci.fetch_agent_bundle("https://ctrl.example:9000", "SECRET-KEY")
    assert out == b"PK\x03\x04zipbytes"
    assert calls["url"].endswith("/remote/agent-bundle")
    assert calls["key"] == "SECRET-KEY"          # authenticates with the machine API key


def test_fetch_agent_bundle_error_raises(monkeypatch):
    monkeypatch.setattr(ci.requests, "get",
                        lambda *a, **k: _Resp(409, payload={"detail": "no controller address"}))
    try:
        ci.fetch_agent_bundle("https://ctrl.example", "k")
        assert False, "should have raised"
    except ci.ControllerImportError as e:
        assert "no controller address" in str(e)


def test_fetch_agent_bundle_404_is_actionable(monkeypatch):
    # A bare FastAPI 404 ("Not Found") means the route is missing — an old Controller or
    # a URL pointing at the wrong service. The message must name both causes, not echo
    # "Not Found", so the operator knows what to actually do.
    monkeypatch.setattr(ci.requests, "get",
                        lambda *a, **k: _Resp(404, payload={"detail": "Not Found"}))
    try:
        ci.fetch_agent_bundle("https://ctrl.example:9000", "k")
        assert False, "should have raised"
    except ci.ControllerImportError as e:
        m = str(e)
        assert "/remote/agent-bundle" in m and "404" in m
        assert "older build" in m or "wrong service" in m


def test_fetch_agent_bundle_200_html_rejected(monkeypatch):
    # A 200 that isn't a zip (e.g. the portal login page) must NOT be shipped to the host
    # as an "agent bundle" — it's a misdirected URL, and we say so.
    monkeypatch.setattr(ci.requests, "get",
                        lambda *a, **k: _Resp(200, content=b"<!doctype html><html>login"))
    try:
        ci.fetch_agent_bundle("https://ctrl.example", "k")
        assert False, "should have raised"
    except ci.ControllerImportError as e:
        assert "portal" in str(e) or "not an agent bundle" in str(e) or "instead of an" in str(e)


_SSL_ERR = ("could not reach the Controller at https://ctrl:9000/remote/agent-bundle: "
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate")


def test_bundle_fetch_tofu_pins_and_persists(monkeypatch):
    # A standalone Controller's self-signed cert changed (container → standalone), so the
    # stored/empty cert fails verification. The fetch must trust-on-first-use: pull the
    # current cert, PERSIST it, and retry — so enrollment self-heals with no manual PEM copy.
    import backend.app as app
    import backend.db as db
    seen = {"persist": None, "certs": []}

    def fake_bundle(base, key, cert_pem=""):
        seen["certs"].append(cert_pem)
        if not cert_pem:
            raise ci.ControllerImportError(_SSL_ERR)
        return b"PK\x03\x04zip"

    monkeypatch.setattr(ci, "fetch_agent_bundle", fake_bundle)
    monkeypatch.setattr(ci, "fetch_server_cert", lambda url: "-----FRESHPEM-----")
    monkeypatch.setattr(db, "set_controller_tls_cert", lambda cid, pem: seen.__setitem__("persist", (cid, pem)))

    ctrl = {"id": 7, "base_url": "https://ctrl:9000", "api_key": "K", "tls_cert": ""}
    out = app._fetch_agent_bundle_tofu(ctrl)
    assert out == b"PK\x03\x04zip"
    assert seen["persist"] == (7, "-----FRESHPEM-----")   # pinned cert saved for next time
    assert ctrl["tls_cert"] == "-----FRESHPEM-----"        # mutated so a multi-host enroll re-pins once
    assert seen["certs"] == ["", "-----FRESHPEM-----"]     # retried with the fetched cert


def test_bundle_fetch_tofu_non_cert_error_propagates(monkeypatch):
    # A 404 / key error is NOT a cert problem — don't fetch or pin anything, just raise.
    import backend.app as app
    import backend.db as db
    pinned = {"n": 0}
    monkeypatch.setattr(ci, "fetch_agent_bundle",
                        lambda *a, **k: (_ for _ in ()).throw(ci.ControllerImportError("HTTP 404 · Not Found")))
    monkeypatch.setattr(ci, "fetch_server_cert", lambda url: "X")
    monkeypatch.setattr(db, "set_controller_tls_cert", lambda cid, pem: pinned.__setitem__("n", pinned["n"] + 1))
    try:
        app._fetch_agent_bundle_tofu({"id": 1, "base_url": "https://c:9000", "api_key": "K", "tls_cert": ""})
        assert False, "should have raised"
    except ci.ControllerImportError as e:
        assert "404" in str(e)
    assert pinned["n"] == 0   # never persisted a cert for a non-cert failure


def test_agent_install_cmd_builds_ssh_argv():
    cmd = keydist.agent_install_cmd("root@jump", "admin@10.0.0.5", "/data/mk")
    assert cmd[0] == "ssh"
    assert "admin@10.0.0.5" in cmd and "/data/mk" in cmd
    assert any(a.startswith("-o") for a in cmd)
    assert any("ProxyCommand=" in a for a in cmd)      # jump hop
    assert "run_agent.sh" in cmd[-1] and "base64 -d" in cmd[-1]


def test_agent_install_cmd_no_bastion_and_no_key():
    direct = keydist.agent_install_cmd("", "admin@10.0.0.5", "/data/mk")
    assert not any("ProxyCommand" in a for a in direct)
    assert keydist.agent_install_cmd("", "admin@10.0.0.5", "") is None   # no key → no command
