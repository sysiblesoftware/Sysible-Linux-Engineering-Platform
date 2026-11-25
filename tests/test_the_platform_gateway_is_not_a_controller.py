"""Pointing SLEP at the platform's front door must say so, not fail on TLS.

SLEP and the Controller normally sit behind the SAME SLOP gateway, on one
address — the whole point of the gateway is that everything lives on one origin
addressed by path. So the address an operator browses the platform at is the
one they naturally type into "Controller URL", and it reaches the gateway:

  * the gateway routes /auth/* to the SLOP IdP, so SLEP's POST /auth/api-key
    never reaches the Controller; and
  * the gateway's certificate is minted by Caddy's internal CA (`tls internal`
    on the sysible.slop cert-holder site, served for every raw-IP request via
    default_sni), whose root SLEP has no reason to hold.

What the operator sees is:

    certificate verify failed: unable to get local issuer certificate

about two machines on the same LAN that are both working perfectly, with
nothing anywhere to suggest the address points at the wrong service. Reported
as "Controller isn't connecting to SLEP even though they are on the same IP".

Note the wording of that error. A SELF-SIGNED certificate reports "self-signed
certificate"; this one reports a missing ISSUER, because the gateway's leaf is
signed by Caddy's CA. Those are different faults and they were being treated as
one.

Worse, once pinning works the TLS error goes away and the request lands on the
IdP, which answers 404 — and SLEP's 404 branch reports that the Controller
"predates the feature". A confident, wrong answer that sends the operator to
go and look at the Controller.
"""
import http.server
import json
import os
import pathlib
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

# The name Caddy serves for every raw-IP request, written out rather than read
# from the code under test. test_the_gateway_name_matches_the_gateway pins it to
# the Caddyfile, so the two cannot drift apart unnoticed.
GATEWAY_CN = "sysible.slop"


def _local_address():
    """A bindable NON-loopback address: _guard_url refuses loopback on purpose."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        addr = s.getsockname()[0]
    except OSError:
        addr = "127.0.0.1"
    finally:
        s.close()
    if addr.startswith("127."):
        pytest.skip("no non-loopback local address to host the test server on")
    return addr


def _cert(tmp_path, cn, addr):
    key, crt = tmp_path / f"{cn}.key", tmp_path / f"{cn}.crt"
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(crt), "-subj", f"/CN={cn}",
         "-addext", f"subjectAltName=IP:{addr}"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        pytest.skip(f"openssl could not make a test cert: {r.stderr[-300:]}")
    return str(key), str(crt)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Enough of a Controller to get past connect, so a refusal can only come
    from the certificate check and not from the conversation failing anyway."""

    def log_message(self, *a):
        pass

    def _json(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path.split("?")[0] == "/auth/api-key":
            self._json({"api_key": API_KEY})
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        body = {"/agents": {"agents": []}, "/remote/hosts": {}}.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404); self.end_headers(); return
        self._json(body)


def _serve(tmp_path, cn):
    addr = _local_address()
    key, crt = _cert(tmp_path, cn, addr)
    sock = socket.socket()
    sock.bind((addr, 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = http.server.ThreadingHTTPServer((addr, port), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"https://{addr}:{port}"


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    """A server presenting the gateway's certificate, as Caddy does."""
    addr = _local_address()
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, (os.environ.get(var, "") + "," + addr).strip(","))
    srv, url = _serve(tmp_path, GATEWAY_CN)
    try:
        yield url
    finally:
        srv.shutdown(); srv.server_close()


@pytest.fixture()
def real_controller(tmp_path, monkeypatch):
    addr = _local_address()
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, (os.environ.get(var, "") + "," + addr).strip(","))
    srv, url = _serve(tmp_path, addr)
    try:
        yield url
    finally:
        srv.shutdown(); srv.server_close()


def test_connecting_to_the_gateway_says_which_address_to_use(client, gateway):
    r = client.post("/controllers", json={"name": "oops", "base_url": gateway,
                                          "username": "admin", "password": "pw"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "gateway" in detail.lower(), detail
    assert str(controller_import.CONTROLLER_DEFAULT_PORT) in detail, \
        f"the message does not say what address to use instead: {detail}"


def test_it_does_not_blame_the_controller_for_being_old(client, gateway):
    """The 404 branch's message. Reaching the IdP instead of the Controller
    produces a 404, and 'this Controller predates the feature' is a confident,
    wrong answer that sends the operator to the wrong machine."""
    r = client.post("/controllers", json={"name": "oops2", "base_url": gateway,
                                          "username": "admin", "password": "pw"})
    detail = r.json()["detail"].lower()
    assert "predates" not in detail, detail
    assert "update the controller" not in detail, detail


def test_nothing_is_saved_when_the_address_is_the_gateway(client, gateway):
    import backend.db as db
    before = {c["id"] for c in db.list_controllers()}
    client.post("/controllers", json={"name": "oops3", "base_url": gateway,
                                      "username": "admin", "password": "pw"})
    assert {c["id"] for c in db.list_controllers()} == before, \
        "a Controller row was written for an address that is not a Controller"


def test_a_real_controller_is_still_trusted_on_first_use(client, real_controller):
    """The control. A check that refuses the gateway and everything else would
    pass the test above and break the product."""
    r = client.post("/controllers", json={"name": "genuine", "base_url": real_controller,
                                          "username": "admin", "password": "pw"})
    assert r.status_code == 200, r.text
    assert r.json().get("pinned_cert") is True, r.text


def test_the_check_reads_the_certificate_not_the_url(real_controller):
    """It must key on what the server PRESENTS. An address-shaped guess (no
    port, port 443, a private IP) would refuse a perfectly good Controller that
    happens to be served on 443."""
    import ssl as _ssl
    from urllib.parse import urlparse
    u = urlparse(real_controller)
    pem = _ssl.get_server_certificate((u.hostname, u.port), timeout=10)
    assert controller_import.gateway_instead_of_controller(pem, real_controller) == ""


# ---------------------------------------------------------------------------
# A gateway with a certificate that actually verifies
# ---------------------------------------------------------------------------
# The check used to live inside the certificate-error branch, so it only ran
# when the TLS handshake failed first. That held for a stock platform, whose
# gateway serves a certificate from Caddy's internal CA — and stopped holding
# the moment the gateway could be given a real one, which Administration · TLS
# now makes a two-minute job. A trusted gateway produces no certificate error,
# so the guard went quiet in exactly the setup it exists for, and the operator
# got "this Controller predates the feature" about the platform's front door.

def _ca_signed_as(tmp_path, cn, addr):
    """(key, crt, ca) — a leaf for `cn` that also COVERS `addr`, so connecting
    by IP verifies cleanly once the CA is trusted."""
    ca_key, ca_crt = tmp_path / "ca.key", tmp_path / "ca.crt"
    key, csr, crt = tmp_path / "s.key", tmp_path / "s.csr", tmp_path / "s.crt"
    ext = tmp_path / "ext.cnf"
    ext.write_text(f"subjectAltName=DNS:{cn},IP:{addr}\n")
    for c in (
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(ca_key), "-out", str(ca_crt), "-subj", "/CN=Test Root"],
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={cn}"],
        ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey",
         str(ca_key), "-CAcreateserial", "-out", str(crt), "-days", "2",
         "-extfile", str(ext)],
    ):
        r = subprocess.run(c, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            pytest.skip(f"openssl could not build the test chain: {r.stderr[-300:]}")
    return str(key), str(crt), str(ca_crt)


@pytest.fixture()
def trusted_gateway(tmp_path, monkeypatch):
    """A gateway whose certificate VERIFIES — no TLS error anywhere."""
    addr = _local_address()
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, (os.environ.get(var, "") + "," + addr).strip(","))
    d = tmp_path / "trusted-pki"
    d.mkdir()
    key, crt, ca = _ca_signed_as(d, GATEWAY_CN, addr)
    # Trust it, the way a real platform with a real certificate would be trusted.
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.setenv(var, ca)
    sock = socket.socket()
    sock.bind((addr, 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = http.server.ThreadingHTTPServer((addr, port), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"https://{addr}:{port}"
    finally:
        srv.shutdown(); srv.server_close()


def test_a_trusted_gateway_is_still_recognised(client, trusted_gateway):
    """The hole. With the certificate verifying there is no TLS failure, so a
    check that waits for one never runs at all."""
    r = client.post("/controllers", json={"name": "trusted-gw", "base_url": trusted_gateway,
                                          "username": "admin", "password": "pw"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "gateway" in detail.lower(), detail
    assert str(controller_import.CONTROLLER_DEFAULT_PORT) in detail, detail


def test_the_probe_does_not_reject_an_address_it_cannot_reach(client):
    """The probe asks a question; it must not become a new way to fail. An
    address that answers nothing has to reach the ordinary attempt and produce
    the ordinary error, not a verdict from a handshake that never happened."""
    r = client.post("/controllers", json={"name": "nowhere",
                                          "base_url": "https://198.51.100.7:9",
                                          "username": "admin", "password": "pw"})
    assert r.status_code == 400
    assert "gateway" not in r.json()["detail"].lower(), r.json()["detail"]


class _Absent(_Handler):
    """Answers 404 to everything, as the gateway's IdP does to /auth/api-key."""

    def do_POST(self):
        self.send_response(404); self.end_headers()


@pytest.fixture()
def answers_nothing(tmp_path, monkeypatch):
    """Reachable, trusted, and nothing there — the 404 branch, by itself."""
    addr = _local_address()
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(var, (os.environ.get(var, "") + "," + addr).strip(","))
    d = tmp_path / "absent-pki"
    d.mkdir()
    # A name that is NOT the gateway's, so the gateway check stays out of this.
    key, crt, ca = _ca_signed_as(d, "controller.example", addr)
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.setenv(var, ca)
    sock = socket.socket()
    sock.bind((addr, 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = http.server.ThreadingHTTPServer((addr, port), _Absent)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"https://{addr}:{port}"
    finally:
        srv.shutdown(); srv.server_close()


def test_a_404_does_not_lead_with_the_controller_being_old(answers_nothing):
    """A 404 at /auth/api-key says only that nothing there answers it. Leading
    with "it predates the feature" is a confident, wrong answer that sends the
    operator to update a Controller that is fine — which is what happened."""
    with pytest.raises(controller_import.ControllerImportError) as ei:
        controller_import.exchange_credentials_for_key(answers_nothing, "admin", "pw")
    msg = str(ei.value)
    assert "gateway" in msg.lower(), msg
    assert str(controller_import.CONTROLLER_DEFAULT_PORT) in msg, msg
    # The old Controller may still be the cause, so it is named -- but last,
    # after the cause that is far likelier and costs nothing to check.
    head = msg.lower().split("failing that")[0]
    assert "predates" not in head, msg


def test_the_gateway_name_matches_the_gateway():
    """The check turns on the gateway's certificate carrying a known name. That
    name lives in the Caddyfile; renaming it there without touching the Python
    would leave the check matching a name nothing serves, and the operator back
    at "unable to get local issuer certificate" with no explanation."""
    caddyfile = None
    for up in range(1, 5):
        c = pathlib.Path(HERE).parents[up - 1] / "gateway" / "Caddyfile"
        if c.is_file():
            caddyfile = c
            break
    if caddyfile is None:
        pytest.skip("no gateway/Caddyfile here (app published standalone)")
    sni = [l.split()[1] for l in caddyfile.read_text().splitlines()
           if l.strip().startswith("default_sni ")]
    assert sni, f"no default_sni in {caddyfile}"
    assert sni[0] == GATEWAY_CN == controller_import.SLOP_GATEWAY_CN, (
        f"Caddy serves {sni[0]!r} but SLEP looks for "
        f"{controller_import.SLOP_GATEWAY_CN!r}")


def test_the_quiet_probe_still_refuses_the_addresses_the_guard_refuses():
    """The probe swallows exceptions, which is exactly how an SSRF guard goes
    missing: the refusal IS an exception. The guard runs before any connection,
    so "" must mean "no answer" and never "connected anyway"."""
    import unittest.mock as mock
    for addr in ("https://127.0.0.1:8800", "https://169.254.169.254",
                 "https://[::1]:8800", "https://0.0.0.0", "http://10.0.0.5:8800"):
        with mock.patch.object(ssl, "get_server_certificate",
                               side_effect=AssertionError("connected")) as g:
            assert controller_import.fetch_server_cert_quietly(addr) == "", addr
            assert not g.called, f"the probe opened a connection to {addr}"


def test_the_quiet_probe_returns_what_it_finds():
    """...and when the guard allows it, the probe does return the certificate,
    so the mock above is not passing because nothing ever works."""
    import unittest.mock as mock
    with mock.patch.object(ssl, "get_server_certificate", return_value="PEM") as g:
        assert controller_import.fetch_server_cert_quietly("https://198.51.100.7") == "PEM"
        assert g.called


def test_the_controllers_own_browser_url_is_recognised_too(client, gateway):
    """The gateway header documents the Controller as

        Controller    https://<server-ip>/controller/

    so that is a thoroughly reasonable thing to paste into "Controller URL".
    It cannot work for connect: /controller/* runs forward_auth, and a
    server-to-server POST carries no SLOP session cookie, so the gateway 302s
    to /login and SLEP gets an HTML login page where it expected JSON. Same
    certificate, so the same check must catch it -- and it must still point at
    the port, not at the path it just refused."""
    r = client.post("/controllers", json={"name": "by-path",
                                          "base_url": gateway.rstrip("/") + "/controller/",
                                          "username": "admin", "password": "pw"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "gateway" in detail.lower(), detail
    assert str(controller_import.CONTROLLER_DEFAULT_PORT) in detail, detail


def test_the_port_we_send_people_to_is_the_api_port():
    """The whole value of this check is the address it hands back, so that
    address has to be right. The Controller runs two listeners -- console 8800,
    API 9000 -- and POST /auth/api-key is on the API. Naming the console port
    sent the operator to something that 404s, i.e. straight back to "nothing
    answers like a Controller". Pin it to the unit that actually starts it."""
    root = None
    for up in range(1, 5):
        c = pathlib.Path(HERE).parents[up - 1] / "apps" / "controller"
        if c.is_dir():
            root = c
            break
    if root is None:
        pytest.skip("no apps/controller here (app published standalone)")

    unit = [l for l in (root / "install_sysible.sh").read_text().splitlines()
            if "uvicorn backend.app:app" in l]
    assert unit, "could not find the backend's uvicorn command line"
    line = unit[0]
    assert f"--port {controller_import.CONTROLLER_DEFAULT_PORT}" in line, line
    # ...and it must be TLS, since the advice says https://.
    assert "--ssl-certfile" in line, line

    # The endpoint SLEP posts to is served by that app, not by the console.
    assert '@app.post("/auth/api-key")' in (root / "backend" / "app.py").read_text()

    # And the UI's own placeholder must agree, or the field contradicts the error.
    ui = (root.parent / "slep" / "webgui" / "frontend" / "src" / "views"
          / "Controllers.jsx").read_text()
    assert f":{controller_import.CONTROLLER_DEFAULT_PORT}" in ui
