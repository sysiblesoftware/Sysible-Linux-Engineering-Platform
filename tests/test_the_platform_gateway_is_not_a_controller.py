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
    srv, url = _serve(tmp_path, controller_import.SLOP_GATEWAY_CN)
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
