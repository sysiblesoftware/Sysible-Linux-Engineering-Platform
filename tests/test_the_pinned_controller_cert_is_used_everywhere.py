"""A pinned Controller certificate must be honoured on EVERY call, import included.

SLEP pins the Controller's certificate on connect (trust on first use) because
a stock Controller serves a self-signed cert — that is the whole reason the
pinning exists. Each public helper in controller_import gets the pinned PEM
through the @_accepts_cert decorator, which sets a contextvar for the duration
of the call so _verify() hands it to requests.

import_into_inventory was the one public helper without that decorator, and the
app.py route that calls it passed no cert_pem. So the one call that actually
writes hosts into an inventory fell back to the system CA store, on the default
install shape, and failed:

    Connect   → succeeds, pins the cert
    Test      → "reachable · N host(s)"
    Import →  → lists every host correctly
    Add N →   → 400 "Could not reach the Controller at https://…/agents:
                certificate verify failed: self-signed certificate"

Worse than a plain failure, because every neighbouring step works, so nothing
points at TLS.

Two tests, because one alone would not have caught it. The behavioural one runs
a real HTTPS server with a real self-signed certificate and imports through it.
The structural one asserts that every public helper in the module that issues a
request accepts the pin at all — that is the property that was violated, and it
holds for a helper added next year.
"""
import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from backend import controller_import  # noqa: E402

API_KEY = "test-api-key"
AGENTS = {"agents": [{"host_id": "h1", "hostname": "web-01", "ip": "10.0.0.11",
                      "platform": "linux", "environment": "prod"},
                     {"host_id": "h2", "hostname": "db-01", "ip": "10.0.0.12",
                      "platform": "linux", "environment": "prod"}]}
SSH_HOSTS = {"legacy-01": {"ip": "10.0.0.21", "user": "root", "port": 22,
                           "environment": "staging"}}


def _local_address():
    """A bindable NON-loopback address for this host.

    127.0.0.1 will not do: controller_import._guard_url is an SSRF guard that
    refuses loopback, link-local and reserved targets on purpose (so "import
    from Controller" cannot be aimed at cloud metadata or a local admin port),
    and pointing the test at loopback would test that guard instead of the
    certificate. test_the_ssrf_guard_still_refuses_loopback below pins it.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))       # TEST-NET-1; no packet is sent
        addr = s.getsockname()[0]
    except OSError:
        addr = "127.0.0.1"
    finally:
        s.close()
    if addr.startswith("127."):
        pytest.skip("no non-loopback local address to host the test Controller on")
    return addr


def _self_signed(tmp_path, addr):
    """A real self-signed cert — what a stock Controller serves."""
    key = tmp_path / "server.key"
    crt = tmp_path / "server.crt"
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(crt), "-days", "2",
         "-subj", f"/CN={addr}",
         "-addext", f"subjectAltName=IP:{addr}"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        pytest.skip(f"openssl could not make a test cert: {r.stderr[-300:]}")
    return str(key), str(crt)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("X-API-Key") != API_KEY:
            self.send_response(401); self.end_headers(); return
        body = {"/agents": AGENTS, "/remote/hosts": SSH_HOSTS}.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404); self.end_headers(); return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _FakeController:
    """A throwaway HTTPS Controller on a fixed port whose certificate can be
    rotated — which is what happens when the real one is rebuilt or moved."""

    def __init__(self, tmp_path, addr):
        self.tmp_path, self.addr = tmp_path, addr
        sock = socket.socket()
        sock.bind((addr, 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.srv = None
        self.pem = ""
        self.rotate(0)

    @property
    def base_url(self):
        return f"https://{self.addr}:{self.port}"

    def rotate(self, n=1):
        """Serve a brand-new self-signed certificate on the same address."""
        self.stop()
        d = self.tmp_path / f"cert{n}"
        d.mkdir(exist_ok=True)
        key, crt = _self_signed(d, self.addr)
        self.srv = http.server.ThreadingHTTPServer((self.addr, self.port), _Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.pem = open(crt).read()
        return self.pem

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


@pytest.fixture()
def controller(tmp_path, monkeypatch):
    addr = _local_address()
    # requests honours *_proxy from the environment, and a CI/dev box may have an
    # HTTPS proxy set — which would answer instead of the test server and reset
    # the connection. Exempt this one address.
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, (os.environ.get(var, "") + "," + addr).strip(","))
    c = _FakeController(tmp_path, addr)
    try:
        yield c
    finally:
        c.stop()


@pytest.fixture()
def self_signed_controller(controller):
    """(base_url, pem) — the shape the tests below were written against."""
    return controller.base_url, controller.pem


def test_the_ssrf_guard_still_refuses_loopback():
    """Why the fixture above does not use 127.0.0.1, pinned so the reason stays
    true: aiming 'import from Controller' at loopback or cloud metadata is
    refused before any request is made."""
    for url in ("https://127.0.0.1:8443", "http://localhost:9000",
                "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(controller_import.ControllerImportError) as ei:
            controller_import._guard_url(url)
        assert "blocked" in str(ei.value)


def test_without_the_pin_a_self_signed_controller_is_refused(self_signed_controller):
    """The control. If this passed, the test below would prove nothing — it
    would just mean the system CA store happened to accept the cert."""
    base, _pem = self_signed_controller
    with pytest.raises(controller_import.ControllerImportError) as ei:
        controller_import.fetch_hosts(base, API_KEY)
    assert controller_import._is_cert_error(ei.value) or "certificate" in str(ei.value).lower()


def test_fetch_hosts_honours_the_pin(self_signed_controller):
    base, pem = self_signed_controller
    out = controller_import.fetch_hosts(base, API_KEY, cert_pem=pem)
    assert {h["name"] for h in out["hosts"]} == {"web-01", "db-01", "legacy-01"}


def test_test_connection_honours_the_pin(self_signed_controller):
    base, pem = self_signed_controller
    assert controller_import.test_connection(base, API_KEY, cert_pem=pem) == {
        "ok": True, "agents": 2, "ssh": 1, "total": 3}


def test_the_import_that_actually_writes_hosts_honours_the_pin(client, self_signed_controller,
                                                               project):
    """The one that failed: Connect, Test and the host list all worked, and
    'Add N →' was the only step that could not reach the Controller."""
    base, pem = self_signed_controller
    inv = client.post("/inventories", json={"name": "pinned-import",
                                            "project_id": project["id"]}).json()
    summary = controller_import.import_into_inventory(inv["id"], base, API_KEY,
                                                      cert_pem=pem)
    assert summary["imported"] == 3, summary
    names = {h["name"] for h in client.get(f"/inventories/{inv['id']}/hosts").json()["hosts"]}
    assert names == {"web-01", "db-01", "legacy-01"}


def test_the_route_passes_the_saved_controllers_cert(client, self_signed_controller, project):
    """End to end through the HTTP route, which is where the cert was dropped —
    the helper could accept a pin and still never be given one. This is the
    exact sequence that failed: a Controller saved with its pinned cert, then
    'Add N →'."""
    import backend.db as db
    base, pem = self_signed_controller
    cid = db.create_controller("pinned-ctrl", base, API_KEY, tls_cert=pem)
    assert db.get_controller(cid, include_key=True)["tls_cert"] == pem
    inv = client.post("/inventories", json={"name": "via-route",
                                            "project_id": project["id"]}).json()
    r = client.post(f"/inventories/{inv['id']}/import-controller",
                    json={"controller_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3, r.text


def test_the_route_refuses_a_pin_that_is_not_this_controllers(client, controller,
                                                              tmp_path, project):
    """The control for the test above: the route must actually be VERIFYING, not
    merely passing a PEM around. A well-formed certificate that belongs to
    something else has to be refused — otherwise 'the pin was used' and 'nothing
    is checked' look identical.
    """
    import backend.db as db
    other = tmp_path / "other"
    other.mkdir()
    _key, crt = _self_signed(other, controller.addr)
    cid = db.create_controller("wrong-pin", controller.base_url, API_KEY,
                               tls_cert=open(crt).read())
    inv = client.post("/inventories", json={"name": "via-route-wrong-pin",
                                            "project_id": project["id"]}).json()
    r = client.post(f"/inventories/{inv['id']}/import-controller",
                    json={"controller_id": cid})
    assert r.status_code == 400, r.text
    assert "no longer matches" in r.json()["detail"], r.text


def test_every_requesting_helper_accepts_the_pin():
    """The structural rule. import_into_inventory was the only public helper in
    the module without @_accepts_cert, and that is exactly how it went unnoticed
    — it reads like all the others."""
    import inspect

    missing = []
    src = inspect.getsource(controller_import)
    for name, fn in vars(controller_import).items():
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        if getattr(fn, "__module__", "") != controller_import.__name__:
            continue
        body = inspect.getsource(inspect.unwrap(fn))
        issues_request = any(k in body for k in ("_get(", "_post(", "requests.",
                                                 "fetch_hosts(", "_normalize_base("))
        if not issues_request:
            continue
        # follow_wrapped=False: functools.wraps sets __wrapped__, and
        # inspect.signature follows it by default — so the decorated helpers
        # would report the UNDECORATED signature and every one of them would
        # look like it is missing the pin.
        params = inspect.signature(fn, follow_wrapped=False).parameters
        if "cert_pem" not in params and not any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            missing.append(name)
    assert missing == [], (
        f"these helpers reach the Controller but cannot be given the pinned cert: "
        f"{missing}. Decorate them with @_accepts_cert.")
    assert "fetch_server_cert" in src  # sanity: we are looking at the right module


def test_every_call_site_that_has_a_saved_controller_passes_its_cert():
    """The other half of the rule, and the half that actually broke.

    A helper can accept the pin and still never be handed one. Any call in
    app.py that reaches a Controller with a SAVED connection's key must pass
    that connection's pinned certificate in the same call.
    """
    import re
    src = (os.path.join(HERE, "backend", "app.py"))
    text = open(src).read()
    # Each `controller_import.<helper>(...)` call, balanced to its closing paren.
    offenders = []
    for m in re.finditer(r"controller_import\.(\w+)\(", text):
        name = m.group(1)
        if name.startswith("_") or name in ("fetch_server_cert",):
            continue
        i, depth = m.end() - 1, 0
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = text[m.start():i + 1]
        uses_saved = 'ctrl["api_key"]' in call or "ctrl['api_key']" in call
        if uses_saved and "cert_pem" not in call:
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{name} at app.py:{line}")
    assert offenders == [], (
        "these calls use a saved Controller's key but not its pinned certificate: "
        f"{offenders}")


# ---------------------------------------------------------------------------
# A rebuilt Controller presents a NEW self-signed cert
# ---------------------------------------------------------------------------
# This used to leave Test, the host listing, the environment picker and the
# import failing permanently with a raw "certificate verify failed: self-signed
# certificate" and nothing to do about it, while enrolment silently re-pinned
# and healed itself. Now every one of them says what the cure is, and none of
# them re-pins on its own.
@pytest.mark.parametrize("call", [
    ("GET", "/controllers/{cid}/hosts"),
    ("GET", "/controllers/{cid}/environments"),
    ("POST", "/controllers/{cid}/test"),
])
def test_a_rotated_cert_is_reported_with_the_cure(client, controller, call):
    import backend.db as db
    method, path = call
    cid = db.create_controller("rebuildable", controller.base_url, API_KEY,
                               tls_cert=controller.pem)
    url = path.format(cid=cid)
    assert client.request(method, url).status_code == 200, "pinned cert should work"

    controller.rotate()                       # the Controller is rebuilt
    r = client.request(method, url)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "no longer matches" in detail and "Disconnect" in detail, detail


def test_a_rotated_cert_does_not_silently_repin(client, controller, project):
    import backend.db as db
    old_pem = controller.pem
    cid = db.create_controller("rebuildable-2", controller.base_url, API_KEY,
                               tls_cert=old_pem)
    inv = client.post("/inventories", json={"name": "rotate-import",
                                            "project_id": project["id"]}).json()
    assert client.post(f"/inventories/{inv['id']}/import-controller",
                       json={"controller_id": cid}).status_code == 200

    controller.rotate()
    r = client.post(f"/inventories/{inv['id']}/import-controller",
                    json={"controller_id": cid})
    assert r.status_code == 400 and "no longer matches" in r.json()["detail"], r.text
    assert db.get_controller(cid, include_key=True)["tls_cert"] == old_pem, \
        "the stored pin was replaced without an operator saying so"


def test_reconnecting_is_what_trusts_the_new_cert(client, controller, project):
    """And the cure actually works — otherwise the message is a dead end."""
    import backend.db as db
    cid = db.create_controller("rebuildable-3", controller.base_url, API_KEY,
                               tls_cert=controller.pem)
    controller.rotate()
    inv = client.post("/inventories", json={"name": "after-reconnect",
                                            "project_id": project["id"]}).json()
    assert client.post(f"/inventories/{inv['id']}/import-controller",
                       json={"controller_id": cid}).status_code == 400

    # What Disconnect + Connect does: pin whatever the Controller presents now.
    db.set_controller_tls_cert(cid, controller.pem)
    r = client.post(f"/inventories/{inv['id']}/import-controller",
                    json={"controller_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3


def test_a_controller_with_no_pin_is_trusted_on_first_use(client, controller, project):
    """The other half, and the half that must NOT change: a Controller connected
    before pinning existed (or one that used to serve a publicly trusted cert)
    has nothing to compare against, so what it presents is pinned and the call
    retried. That is the same trust decision Connect makes, made later.

    Only enrolment did this before; now every route that reaches a saved
    Controller does, which is what fixes the pre-pinning upgrade path.
    """
    import backend.db as db
    cid = db.create_controller("never-pinned", controller.base_url, API_KEY)
    inv = client.post("/inventories", json={"name": "never-pinned-import",
                                            "project_id": project["id"]}).json()
    r = client.post(f"/inventories/{inv['id']}/import-controller",
                    json={"controller_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == 3
    assert db.get_controller(cid, include_key=True)["tls_cert"] == controller.pem, \
        "a first-use pin should be persisted, not re-fetched on every call"
