"""Import inventory from a Sysible Controller.

The point of SLEP living next to Controller: the fleet you already manage there
becomes the inventory you automate here, in one click. A Controller manages TWO
kinds of host, and we pull both:

  * agent-enrolled hosts  — GET /agents           → {"agents": [ {hostname, ip, environment, …} ]}
  * SSH-managed hosts      — GET /remote/hosts      → { name: {ip, user, port, environment}, … }

Both are gated by the Controller's backend API key (X-API-Key) — the same key its
own console uses. SSH hosts carry their connection user/port, so those import as
runnable hosts (we set ansible_user / ansible_port host vars); agent hosts import
with their address + Controller environment as an Ansible group. The Controller
`environment` tag becomes the host's group. Idempotent: re-importing refreshes
addresses/groups/vars, never duplicates (upsert by name).

TLS is verified by default; a self-signed on-prem Controller can opt out with
SLEP_CONTROLLER_INSECURE=1. Target hosts are SSRF-guarded (loopback/link-local/
metadata blocked) so 'import from Controller' can't be aimed at internal services.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

import requests

from . import db


class ControllerImportError(Exception):
    pass


def _tls_verify():
    """How to verify the Controller's TLS certificate. Verify by default.

    For a standalone/on-prem Controller with a SELF-SIGNED cert (the usual cause of
    'certificate verify failed: self-signed certificate'), the SECURE fix is to TRUST
    that cert: point SLEP_CONTROLLER_CA at the Controller's certificate (or its CA) PEM
    on the SLEP host — requests then verifies against it, so enrollment/import work with
    no on-path window. Only as a last resort on a trusted LAN, SLEP_CONTROLLER_INSECURE=1
    skips verification entirely (never for production — it leaks the API key + creds to
    any on-path attacker). Returns True (default CA store), a CA-bundle path, or False."""
    if os.environ.get("SLEP_CONTROLLER_INSECURE", "").lower() in ("1", "true", "yes"):
        return False
    ca = os.environ.get("SLEP_CONTROLLER_CA", "").strip()
    if ca and os.path.isfile(ca):
        return ca      # requests' verify= accepts a path to a CA bundle / trusted cert
    return True


# The PEM SLEP has pinned for the Controller of the current operation (trust-on-first-use),
# threaded to the internal requests calls via a context var so we don't have to add a
# cert argument to every helper. Empty → fall back to _tls_verify() (env / public CA).
import contextlib
import contextvars

_PINNED_CERT: contextvars.ContextVar[str] = contextvars.ContextVar("slep_pinned_cert", default="")


def _first_pem_block(pem: str) -> str:
    """The first certificate in a PEM blob — a pin may hold a whole chain."""
    m = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                  pem or "", re.S)
    return m.group(0) if m else ""


def _fingerprint(pem: str) -> str:
    """The SHA-256 of a pinned certificate, in the form urllib3 wants."""
    import hashlib
    import ssl as _ssl
    block = _first_pem_block(pem)
    if not block:
        raise ControllerImportError("The pinned Controller certificate is not a PEM certificate.")
    return hashlib.sha256(_ssl.PEM_cert_to_DER_cert(block)).hexdigest()


# The name the SLOP gateway's own certificate carries. Caddy mints one cert under
# this fixed internal name and serves it for every raw-IP request (default_sni,
# gateway/Caddyfile), so a certificate with this subject means the address given
# is the PLATFORM FRONT DOOR and not a Controller at all.
SLOP_GATEWAY_CN = "sysible.slop"

# The Controller's own API listener, which is what SLEP has to talk to.
#
# NOT 8800. The Controller runs TWO listeners: the web console on 8800
# (webgui.server:app, whose only catch-all is a GET that serves the SPA) and the
# API on 9000 (backend.app:app), which is where POST /auth/api-key lives. 8800
# would 404 that POST, so naming it here sent the operator to an address that
# produces the very "nothing answers like a Controller" error this is meant to
# resolve. Both speak TLS; see install_sysible.sh, which starts 9000 with
# --ssl-keyfile/--ssl-certfile, and Controllers.jsx, whose field placeholder has
# always said https://controller-host:9000.
CONTROLLER_DEFAULT_PORT = 9000


def certificate_common_name(pem: str) -> str:
    """The subject CN of a PEM certificate, or "" if it cannot be read."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        block = _first_pem_block(pem)
        if not block:
            return ""
        cert = x509.load_pem_x509_certificate(block.encode())
        vals = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return str(vals[0].value) if vals else ""
    except Exception:  # noqa: BLE001 — an unreadable certificate is simply not a match
        return ""


def gateway_instead_of_controller(pem: str, url: str) -> str:
    """A sentence to show the operator if `url` is the platform gateway, else "".

    The reason this exists: SLEP and the Controller usually sit behind the SAME
    SLOP gateway on one address, so the obvious thing to type into "Controller
    URL" -- the address you browse the platform at -- reaches the gateway. The
    gateway routes /auth/* to the SLOP IdP, so SLEP's POST /auth/api-key does
    not reach the Controller at all, and the gateway's certificate is issued by
    Caddy's internal CA, whose root SLEP has no reason to hold. The operator
    therefore sees

        certificate verify failed: unable to get local issuer certificate

    about a machine on their own LAN that is working perfectly, with nothing to
    suggest the address is pointing at the wrong service. Reported as "Controller
    isn't connecting to SLEP even though they are on the same IP", which is
    exactly the sentence this produces an answer to.
    """
    if certificate_common_name(pem).strip().lower() != SLOP_GATEWAY_CN:
        return ""
    u = urlparse(url if "://" in url else "https://" + url)
    host = u.hostname or url
    return (f"{url} is the Sysible platform gateway, not a Controller — the "
            f"certificate it presents is the gateway's own ({SLOP_GATEWAY_CN}). "
            f"The gateway serves the Controller CONSOLE to a browser at "
            f"/controller/, but that path requires a sign-in and is not the "
            f"Controller's API, so SLEP cannot use it. Give SLEP the Controller's "
            f"own address instead — port {CONTROLLER_DEFAULT_PORT} by default: "
            f"https://{host}:{CONTROLLER_DEFAULT_PORT}")


class _PinnedCertAdapter(requests.adapters.HTTPAdapter):
    """Verify the Controller by the EXACT certificate SLEP pinned.

    Not by handing that certificate to OpenSSL as a CA bundle, which is what this
    used to do. A pinned leaf satisfies chain building ONLY when the Controller's
    certificate is self-signed, because then it is its own issuer. A Controller
    whose certificate is issued by an appliance's own CA fails with

        certificate verify failed: unable to get local issuer certificate

    and pinning the leaf changes nothing: the missing piece is the ISSUER, which
    the handshake never sent and which no amount of pinning the leaf supplies. So
    the retry failed exactly like the first attempt and the operator saw the same
    error as if trust-on-first-use had never run.

    The two cases are distinguishable in the text and were never distinguished:
    a self-signed certificate reports "self-signed certificate", a CA-issued one
    "unable to get local issuer certificate". Every test here used a self-signed
    certificate, so only the case that already worked was ever covered.

    A fingerprint says "exactly this certificate" and does not care how it is
    chained -- which is what trust-on-first-use meant in the first place. It is
    also strictly tighter than a CA bundle: any OTHER certificate is refused,
    including one that chains correctly to a public CA.
    """

    def __init__(self, fingerprint: str, **kw):
        self._fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        kw["assert_fingerprint"] = self._fingerprint
        kw["cert_reqs"] = "CERT_NONE"      # the fingerprint IS the check
        return super().init_poolmanager(connections, maxsize, block=block, **kw)

    def send(self, request, **kw):
        """Force verify=False for this adapter, whatever the session or the
        environment says.

        Setting it on the Session is not enough. requests'
        merge_environment_settings() turns an unset per-request verify into
        REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE when one is exported, and
        merge_setting() then PREFERS that over the session's False -- so chain
        verification comes back on, the pin is bypassed, and a pinned Controller
        fails with the very error the pin exists to fix. Exporting
        REQUESTS_CA_BUNDLE is ordinary on a host behind an inspecting proxy, so
        this is not a test-only concern; it is how the first version of this
        broke, on a machine that had it set."""
        kw["verify"] = False
        return super().send(request, **kw)


class _UnpinnedHttp:
    """The no-pin path: requests' module-level functions, exactly as before.

    A Session exists only to mount the pinning adapter, so the common path does
    not get one. That keeps its behaviour identical -- the env-driven verify from
    _tls_verify() included -- rather than changing the case that was never
    broken, and it does not build a Session per call for it."""

    @staticmethod
    def get(url, **kw):
        return requests.get(url, verify=_tls_verify(), **kw)

    @staticmethod
    def post(url, **kw):
        return requests.post(url, verify=_tls_verify(), **kw)


def _http():
    """How to reach the Controller, given whatever trust SLEP has for it: a
    pinned certificate verified by FINGERPRINT, else the env / public-CA default.

    urllib3 counts a fingerprint-checked connection as verified, so the pinned
    path raises no InsecureRequestWarning -- rightly, since it IS verified, and
    more narrowly than the CA store would."""
    pem = _PINNED_CERT.get()
    if not pem:
        return _UnpinnedHttp
    sess = requests.Session()
    sess.mount("https://", _PinnedCertAdapter(_fingerprint(pem)))
    sess.verify = False       # replaced by the adapter's fingerprint check
    return sess


@contextlib.contextmanager
def _pinned(cert_pem: str):
    token = _PINNED_CERT.set(cert_pem or "")
    try:
        yield
    finally:
        _PINNED_CERT.reset(token)


def _accepts_cert(fn):
    """Give a public helper a keyword-only `cert_pem` that pins the Controller's cert for
    the duration of the call (so its requests verify against it), without threading the
    argument through the body."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, cert_pem="", **kwargs):
        with _pinned(cert_pem):
            return fn(*args, **kwargs)
    return wrapper


def _is_cert_error(exc) -> bool:
    """True if an exception/message looks like a TLS certificate-verification failure
    (self-signed / unknown CA) — the signal to offer trust-on-first-use pinning."""
    s = str(exc).lower()
    return ("certificate verify failed" in s or "self-signed certificate" in s
            or "self signed certificate" in s or "sslcertverificationerror" in s
            or "certificate_verify_failed" in s
            # urllib3's wording when a PINNED certificate is not the one presented.
            # With fingerprint pinning this is what a rebuilt -- or intercepted --
            # Controller looks like, and it has to reach the same cure as the
            # chain-verification failures above, not a raw stack trace.
            or "fingerprints did not match" in s)


def fetch_server_cert(base_url: str) -> str:
    """Retrieve the Controller's leaf TLS certificate as PEM WITHOUT verifying it — for
    trust-on-first-use pinning of a self-signed/on-prem Controller. https only; SSRF-guarded
    (no loopback/metadata). Raises ControllerImportError if it can't be fetched."""
    import ssl
    u = urlparse(base_url if "://" in base_url else "https://" + base_url)
    if u.scheme != "https":
        raise ControllerImportError("A certificate can only be pinned for an https Controller.")
    host = u.hostname
    if not host:
        raise ControllerImportError("Invalid Controller URL.")
    _guard_url(base_url if "://" in base_url else "https://" + base_url)
    try:
        return ssl.get_server_certificate((host, u.port or 443), timeout=15)
    except Exception as e:  # noqa: BLE001
        raise ControllerImportError(f"could not fetch the Controller's certificate: {e}")


def fetch_server_cert_quietly(base_url: str) -> str:
    """The certificate an address presents, or "" if it cannot be had.

    Same as fetch_server_cert, but it never raises: this is used to ASK a
    question before any request is made (is this address a Controller, or the
    platform's own front door?), and a host that is simply unreachable, or
    plain http, must fall through to the ordinary attempt and its ordinary
    error rather than be rejected by a probe.
    """
    try:
        return fetch_server_cert(base_url)
    except Exception:  # noqa: BLE001 — a probe that cannot answer answers nothing
        return ""


def _guard_url(url: str) -> None:
    """SSRF guard: resolve the target host and refuse loopback, link-local (cloud
    metadata at 169.254.169.254 / fd00:ec2::), unspecified, multicast, and reserved
    addresses — so a caller can't point 'import from Controller' at internal metadata
    or localhost admin services. Private LAN ranges stay allowed (real on-prem
    Controllers). Resolving here also blocks DNS-rebinding to a metadata IP."""
    host = urlparse(url).hostname or ""
    if not host:
        raise ControllerImportError("Invalid Controller URL.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return  # let the actual request surface a clean DNS error
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ControllerImportError(
                f"Refusing to connect to {host} ({ip}): loopback/link-local/reserved addresses "
                "(including cloud metadata) are blocked.")


# A scheme typed by hand, with the separator mistyped. "https//host" (no colon),
# "https:/host" (one slash) and "https:host" (no slashes at all) are all things
# people type into a URL box, and none of them start with "http://" or
# "https://" -- so the old code took them for bare hostnames and prefixed a
# scheme, turning "https//192.168.8.139" into "https://https//192.168.8.139":
#
#   Could not reach the Controller at https://https//192.168.8.139/auth/api-key:
#   HTTPSConnectionPool(host='https', port=443) ... Failed to resolve 'https'
#   ([Errno -5] No address associated with hostname)
#
# The host it names is "https". Nothing in that mentions a missing colon, and the
# operator is left looking at the network between two machines that are fine.
#
# The group after the scheme must not be another slash, so a correct
# "https://host" matches and is rewritten to itself, while "httpsfoo.example"
# does not match at all and stays the hostname it is.
_MISTYPED_SCHEME = re.compile(r"^(https?)(?::/{0,2}|/{2})(?!/)(.*)$", re.I)


def _normalize_base(url: str) -> str:
    raw = (url or "").strip()
    url = raw.rstrip("/")
    if not url:
        raise ControllerImportError("Controller URL is required.")
    m = _MISTYPED_SCHEME.match(url)
    if m:
        url = f"{m.group(1).lower()}://{m.group(2)}"
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Say what is wrong with the URL here, where the URL is, rather than letting
    # it become a DNS failure for a host nobody typed.
    host = (urlparse(url).hostname or "").strip()
    if not host or host.lower() in ("http", "https"):
        raise ControllerImportError(
            f"\u201c{raw}\u201d is not a usable Controller URL \u2014 there is no host in it. "
            "Use the address of the Controller, e.g. https://192.168.8.139 "
            "or https://controller.example.com.")
    return url


def _get(base_url: str, path: str, api_key: str, allow_404: bool = False):
    """GET base_url+path with the Controller API key. Returns parsed JSON, or None
    on 404 when allow_404 (an older Controller may lack an endpoint). Raises
    ControllerImportError on auth/network/other failures."""
    url = base_url + path
    _guard_url(url)
    try:
        resp = _http().get(url, headers={"X-API-Key": api_key}, timeout=20)
    except requests.exceptions.RequestException as e:
        raise ControllerImportError(f"Could not reach the Controller at {url}: {e}")
    if resp.status_code in (401, 403):
        raise ControllerImportError("The Controller rejected that API key.")
    if resp.status_code == 404 and allow_404:
        return None
    if resp.status_code != 200:
        raise ControllerImportError(f"The Controller returned HTTP {resp.status_code} for {path}.")
    try:
        return resp.json()
    except ValueError:
        raise ControllerImportError(f"The Controller's {path} response was not JSON.")


class ControllerMFARequired(ControllerImportError):
    """Raised when the Controller superuser has MFA enrolled and no code was
    supplied — the caller should collect a TOTP code and retry."""


@_accepts_cert
def exchange_credentials_for_key(controller_url: str, username: str, password: str, totp_code: str = ""):
    """Trade a Controller superuser's console username+password (and TOTP code,
    if their account has MFA) for the Controller's backend API key, via the
    Controller's POST /auth/api-key. This is the friendly path: the operator
    signs in with the same credentials they use for the Controller console
    instead of hunting down /opt/sysible/api_key.txt.

    Returns the api_key string. Raises ControllerMFARequired when a second
    factor is needed, or ControllerImportError on bad creds / network / an
    older Controller that lacks the endpoint (404)."""
    if not username or not password:
        raise ControllerImportError("Controller username and password are required.")
    base = _normalize_base(controller_url)
    url = base + "/auth/api-key"
    payload = {"username": username, "password": password}
    if totp_code:
        payload["totp_code"] = totp_code
    # The Controller's /auth/api-key hands out the root backend key, so it refuses
    # off-gateway LAN callers — it accepts a request only from loopback OR one that
    # carries the SLOP shared secret. SLEP and the Controller run behind the same
    # SLOP gateway and share that secret, so present it as X-Sysible-Auth to prove
    # we're a trusted sibling (the same credential the gateway itself stamps).
    # Without this, a direct https://<host>:9000 connect is (correctly) rejected as
    # "reachable only through the local gateway/BFF."
    headers = {}
    _sso = os.environ.get("SYSIBLE_SSO_SHARED_SECRET", "").strip()
    if _sso:
        headers["X-Sysible-Auth"] = _sso
    try:
        _guard_url(url)
        resp = _http().post(url, json=payload, headers=headers or None, timeout=20)
    except requests.exceptions.RequestException as e:
        raise ControllerImportError(f"Could not reach the Controller at {url}: {e}")
    if resp.status_code == 404:
        # A 404 says only that nothing answers /auth/api-key there. It does NOT
        # say the Controller is old, and asserting that sends the operator to
        # update a Controller that is fine -- which is what happened when this
        # address turned out to be the platform gateway, whose /auth/* belongs
        # to the SLOP IdP. Name the likelier cause first and keep the other.
        raise ControllerImportError(
            f"Nothing at {url} answers like a Controller. The commonest cause is an "
            "address that is not the Controller's own: if SLEP and the Controller are "
            "behind the same platform gateway, the address you browse the platform at "
            f"is the gateway, and the Controller's own listener is port "
            f"{CONTROLLER_DEFAULT_PORT}. Failing that, a Controller too old for "
            "username/password connect would also answer this way — connect with its "
            "backend API key instead.")
    if resp.status_code in (401, 403, 429):
        detail = "The Controller rejected those credentials."
        try:
            detail = resp.json().get("detail") or detail
        except ValueError:
            pass
        raise ControllerImportError(detail)
    if resp.status_code != 200:
        raise ControllerImportError(f"The Controller returned HTTP {resp.status_code}.")
    try:
        data = resp.json()
    except ValueError:
        raise ControllerImportError("The Controller's response was not JSON.")
    if data.get("status") == "mfa_required":
        raise ControllerMFARequired("This account has multi-factor authentication — enter the current code.")
    key = data.get("api_key")
    if not key:
        raise ControllerImportError("The Controller did not return an API key.")
    return key


@_accepts_cert
def exchange_sso_for_key(controller_url: str, sso_user: str, sso_role: str):
    """SSO variant of exchange_credentials_for_key: when SLEP runs behind the SLOP
    gateway the operator is already signed in there, so there is NO separate
    Controller password to collect. Relay the gateway-asserted identity
    (X-Sysible-User / X-Sysible-Role) and prove we're a trusted sibling with the
    shared secret (X-Sysible-Auth); the Controller returns its API key without a
    password. Requires a superuser identity. Raises ControllerImportError otherwise
    (e.g. the target Controller isn't part of this SLOP / doesn't share the secret),
    so the caller can fall back to explicit credentials."""
    _sso = os.environ.get("SYSIBLE_SSO_SHARED_SECRET", "").strip()
    if not _sso:
        raise ControllerImportError("SSO connect needs the SLOP gateway (SYSIBLE_SSO_SHARED_SECRET is unset).")
    if not sso_user:
        raise ControllerImportError("No SLOP identity on this session — sign in through the SLOP gateway.")
    base = _normalize_base(controller_url)
    url = base + "/auth/api-key"
    headers = {"X-Sysible-Auth": _sso, "X-Sysible-User": sso_user, "X-Sysible-Role": sso_role or ""}
    # The model requires username/password fields; send them empty — the Controller
    # authenticates this request by the SSO headers, not the (nonexistent) password.
    payload = {"username": sso_user, "password": ""}
    try:
        _guard_url(url)
        resp = _http().post(url, json=payload, headers=headers, timeout=20)
    except requests.exceptions.RequestException as e:
        raise ControllerImportError(f"Could not reach the Controller at {url}: {e}")
    if resp.status_code == 404:
        raise ControllerImportError(
            "This Controller doesn't support SSO connect (update it), or connect with its backend API key.")
    if resp.status_code in (401, 403, 429):
        detail = "The Controller rejected the SLOP SSO identity."
        try:
            detail = resp.json().get("detail") or detail
        except ValueError:
            pass
        raise ControllerImportError(detail)
    if resp.status_code != 200:
        raise ControllerImportError(f"The Controller returned HTTP {resp.status_code}.")
    try:
        key = resp.json().get("api_key")
    except ValueError:
        raise ControllerImportError("The Controller's response was not JSON.")
    if not key:
        raise ControllerImportError("The Controller did not return an API key.")
    return key


@_accepts_cert
def get_controller_key(controller_url: str, api_key: str) -> str:
    """Fetch a Controller's standing SSH public key (GET /remote/controller-key).
    Baked into a new VM's cloud-init so the Controller can SSH in after boot."""
    base = _normalize_base(controller_url)
    data = _get(base, "/remote/controller-key", api_key, allow_404=True)
    if isinstance(data, dict):
        return data.get("public_key", "") or ""
    return ""


@_accepts_cert
def fetch_agent_bundle(controller_url: str, api_key: str, environment: str = "") -> bytes:
    """Download a fresh one-time AGENT enrollment bundle (zip) from a Controller with the
    machine API key (GET /remote/agent-bundle). Each call mints a new single-use token,
    so fetch ONE bundle per host. This is the agent (pull) enrollment path — the target
    runs the bundle and self-enrolls outbound, so there's no inbound SSH-as-root and no
    human superuser token. `environment` (optional) asks the Controller to drop the host
    straight into that environment on enroll (it's ignored server-side unless it names a
    real environment). Raises ControllerImportError on any failure."""
    base = _normalize_base(controller_url)
    url = base + "/remote/agent-bundle"
    params = {"environment": environment} if (environment or "").strip() else None
    try:
        resp = _http().get(url, headers={"X-API-Key": api_key}, params=params, timeout=30)
    except requests.exceptions.RequestException as e:
        raise ControllerImportError(f"could not reach the Controller at {url}: {e}")
    if resp.status_code == 200:
        # A minted bundle is a zip. If we somehow got HTML/JSON with a 200 (e.g. the URL
        # points at the portal web UI, which happily 200s its login page), it isn't a
        # bundle — surface that instead of shipping an HTML page to the host as an "agent".
        ctype = (getattr(resp, "headers", {}) or {}).get("Content-Type", "").lower()
        if "zip" in ctype or resp.content[:2] == b"PK":
            return resp.content
        raise ControllerImportError(
            f"the Controller at {url} returned {ctype or 'a non-zip response'} instead of an "
            f"agent bundle — {base} looks like the portal/web UI, not the Controller API. "
            f"Reconnect this Controller using its backend API address + machine API key.")
    # Non-200: decode the server's detail, then map the common cases to an actionable line.
    detail = f"HTTP {resp.status_code}"
    try:
        detail = resp.json().get("detail") or detail
    except ValueError:
        detail = (resp.text or "").strip()[:200] or detail
    if resp.status_code == 404:
        # FastAPI returns {"detail":"Not Found"} for an unknown route. The credentials and
        # host are fine — the route just isn't there — so point at the two real causes.
        raise ControllerImportError(
            f"the Controller at {url} has no agent-bundle route (HTTP 404 · {detail}). Either "
            f"this Controller is an older build without agent (pull) enrollment — update it — "
            f"or {base} points at the wrong service (e.g. the portal on its own port) instead "
            f"of the Controller backend API. Reconnect the Controller with its API address.")
    if resp.status_code in (401, 403):
        raise ControllerImportError(
            f"the Controller at {url} rejected SLEP's machine API key (HTTP {resp.status_code} · "
            f"{detail}). Reconnect this Controller with a current backend API key.")
    if resp.status_code == 409:
        # bundle_addresses() came back empty — the Controller has no configured address.
        raise ControllerImportError(f"the Controller can't build a bundle yet: {detail}")
    raise ControllerImportError(f"agent bundle download failed ({url}): {detail}")


@_accepts_cert
def list_environments(controller_url: str, api_key: str) -> list:
    """The Controller's defined environments (GET /environments), as a list of names.
    Used to populate SLEP's "enroll into environment" picker so an operator building
    VMs can drop them straight into an existing Controller environment. Returns [] on
    an older Controller without the route. Raises ControllerImportError on auth/network
    failure."""
    base = _normalize_base(controller_url)
    data = _get(base, "/environments", api_key, allow_404=True) or {}
    out = []
    for e in (data.get("environments") or []):
        name = e.get("name") if isinstance(e, dict) else e
        if name:
            out.append(str(name))
    return out


@_accepts_cert
def register_ssh_host(controller_url: str, api_key: str, name: str, ip: str,
                      user: str = "root", environment: str = ""):
    """Register one SSH-managed host in a Controller (POST /remote/hosts). The
    Controller reaches it with its own key (get_controller_key). Returns (ok, detail)."""
    base = _normalize_base(controller_url)
    url = base + "/remote/hosts"
    try:
        resp = _http().post(url, headers={"X-API-Key": api_key},
                            json={"name": name, "ip": ip, "user": user, "environment": environment},
                            timeout=20)
    except requests.exceptions.RequestException as e:
        return False, f"could not reach Controller: {e}"
    if resp.status_code == 200:
        return True, "enrolled"
    detail = f"HTTP {resp.status_code}"
    try:
        detail = resp.json().get("detail") or detail
    except ValueError:
        pass
    return False, detail


@_accepts_cert
def test_connection(controller_url: str, api_key: str):
    """Probe a Controller with the given key — used by 'Connect to Controller'.
    Returns {ok, agents, ssh, total} (host counts it can see). Raises
    ControllerImportError on auth/network failure so the UI can show why."""
    if not api_key:
        raise ControllerImportError("Controller API key is required.")
    base = _normalize_base(controller_url)
    data = _get(base, "/agents", api_key)          # auth-gated → validates the key
    agents = data.get("agents", []) if isinstance(data, dict) else (data or [])
    ssh = 0
    hosts = _get(base, "/remote/hosts", api_key, allow_404=True)
    if isinstance(hosts, dict):
        ssh = len(hosts)
    return {"ok": True, "agents": len(agents), "ssh": ssh, "total": len(agents) + ssh}


@_accepts_cert
def fetch_hosts(controller_url: str, api_key: str):
    """Return the Controller's hosts as a normalized, importable list WITHOUT
    writing anything — the console shows this so the operator can pick which hosts
    go into which inventory. Each entry:
      {name, address, groups, source: 'agent'|'ssh', variables}
    Raises ControllerImportError only if it can't reach/authenticate at all."""
    if not api_key:
        raise ControllerImportError("Controller API key is required.")
    base = _normalize_base(controller_url)
    out, errors = [], []

    # agent-enrolled hosts (the primary listing; also validates the key)
    data = _get(base, "/agents", api_key)
    agents = data.get("agents", []) if isinstance(data, dict) else (data or [])
    for a in agents:
        name = str(a.get("hostname") or a.get("host_id") or "").strip()
        address = str(a.get("ip") or a.get("address") or name).strip()
        if not name or not address:
            continue
        out.append({
            "name": name, "address": address,
            "groups": str(a.get("environment") or ""), "source": "agent",
            "variables": {"sysible_source": "agent",
                          "sysible_host_id": a.get("host_id") or name,
                          "sysible_platform": a.get("platform") or ""},
        })

    # SSH-managed hosts (carry connection user/port → runnable)
    try:
        hosts = _get(base, "/remote/hosts", api_key, allow_404=True)
        if isinstance(hosts, dict):
            for name, h in hosts.items():
                if not isinstance(h, dict):
                    continue
                nm = str(name).strip()
                address = str(h.get("ip") or nm).strip()
                if not nm or not address:
                    continue
                variables = {"sysible_source": "ssh"}
                if h.get("user"):
                    variables["ansible_user"] = h["user"]
                if h.get("port"):
                    variables["ansible_port"] = h["port"]
                out.append({"name": nm, "address": address,
                            "groups": str(h.get("environment") or ""),
                            "source": "ssh", "variables": variables})
    except ControllerImportError as e:
        errors.append(f"ssh hosts: {e}")

    return {"hosts": out, "errors": errors}


@_accepts_cert
def import_into_inventory(inventory_id: int, controller_url: str, api_key: str, only_names=None,
                          _cert_pem: str = ""):
    """Import the Controller's hosts into a SLEP inventory. `only_names` (a set/list
    of host names) restricts the import to that selection — so the operator can send
    different hosts to different inventories. Returns {imported, agents, ssh, total}.

    THE PIN HAS TO BE THREADED EXPLICITLY HERE. @_accepts_cert sets the pin for
    the duration of this call, but fetch_hosts is itself decorated, and that
    decorator's `cert_pem` defaults to "" — which _pinned() then SETS, clearing
    the pin this call established. So calling fetch_hosts bare would drop the
    certificate even with the decorator in place. _cert_pem is read back out of
    the contextvar and handed on.

    This helper was the only public one in the module without the decorator, and
    the route calling it passed no cert_pem. Since a stock Controller serves a
    self-signed cert — the entire reason the trust-on-first-use pinning exists —
    the one call that actually writes hosts into an inventory was also the only
    one that could not reach the Controller: Connect pinned the cert, Test said
    "reachable · N host(s)", the host list rendered, and "Add N →" answered 400
    "certificate verify failed: self-signed certificate".
    """
    inv = db.get_inventory(inventory_id)
    if not inv:
        raise ControllerImportError("Inventory not found.")
    fetched = fetch_hosts(controller_url, api_key, cert_pem=_cert_pem or _PINNED_CERT.get())
    wanted = set(only_names) if only_names is not None else None

    agents_n = ssh_n = 0
    for h in fetched["hosts"]:
        if wanted is not None and h["name"] not in wanted:
            continue
        db.upsert_host(inventory_id, name=h["name"], address=h["address"],
                       groups=h["groups"], variables=h["variables"], source="controller")
        if h["source"] == "agent":
            agents_n += 1
        else:
            ssh_n += 1

    total = agents_n + ssh_n
    if total == 0 and wanted is None and fetched["errors"]:
        raise ControllerImportError("; ".join(fetched["errors"]))
    return {"imported": total, "agents": agents_n, "ssh": ssh_n,
            "total": total, "skipped": 0, "errors": fetched["errors"]}
