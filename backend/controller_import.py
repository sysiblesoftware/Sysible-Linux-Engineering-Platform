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
_CERT_CACHE: dict[str, str] = {}


def _cert_file(pem: str) -> str:
    """A cached temp-file path for a PEM (requests' verify= needs a path, not a string)."""
    import hashlib
    import tempfile
    key = hashlib.sha256(pem.encode()).hexdigest()
    path = _CERT_CACHE.get(key)
    if path and os.path.isfile(path):
        return path
    fd, path = tempfile.mkstemp(prefix="slep-ctrl-ca-", suffix=".pem")
    try:
        os.write(fd, pem.encode())
    finally:
        os.close(fd)
    _CERT_CACHE[key] = path
    return path


def _verify():
    """What to pass to requests' verify=: a pinned Controller cert if one is set for this
    operation, else the env/public-CA default."""
    pem = _PINNED_CERT.get()
    return _cert_file(pem) if pem else _tls_verify()


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
            or "certificate_verify_failed" in s)


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


def _normalize_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ControllerImportError("Controller URL is required.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _get(base_url: str, path: str, api_key: str, allow_404: bool = False):
    """GET base_url+path with the Controller API key. Returns parsed JSON, or None
    on 404 when allow_404 (an older Controller may lack an endpoint). Raises
    ControllerImportError on auth/network/other failures."""
    url = base_url + path
    _guard_url(url)
    try:
        resp = requests.get(url, headers={"X-API-Key": api_key}, verify=_verify(), timeout=20)
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
        resp = requests.post(url, json=payload, headers=headers or None,
                             verify=_verify(), timeout=20)
    except requests.exceptions.RequestException as e:
        raise ControllerImportError(f"Could not reach the Controller at {url}: {e}")
    if resp.status_code == 404:
        raise ControllerImportError(
            "This Controller doesn't support username/password connect (it predates the "
            "feature). Update the Controller, or connect with its backend API key instead.")
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
        resp = requests.post(url, json=payload, headers=headers, verify=_verify(), timeout=20)
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
        resp = requests.get(url, headers={"X-API-Key": api_key}, params=params,
                            verify=_verify(), timeout=30)
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
        resp = requests.post(url, headers={"X-API-Key": api_key},
                             json={"name": name, "ip": ip, "user": user, "environment": environment},
                             verify=_verify(), timeout=20)
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


def import_into_inventory(inventory_id: int, controller_url: str, api_key: str, only_names=None):
    """Import the Controller's hosts into a SLEP inventory. `only_names` (a set/list
    of host names) restricts the import to that selection — so the operator can send
    different hosts to different inventories. Returns {imported, agents, ssh, total}."""
    inv = db.get_inventory(inventory_id)
    if not inv:
        raise ControllerImportError("Inventory not found.")
    fetched = fetch_hosts(controller_url, api_key)
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
