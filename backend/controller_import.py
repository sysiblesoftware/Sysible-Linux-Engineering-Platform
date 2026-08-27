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
        resp = requests.get(url, headers={"X-API-Key": api_key}, verify=_tls_verify(), timeout=20)
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
    try:
        _guard_url(url)
        resp = requests.post(url, json=payload, verify=_tls_verify(), timeout=20)
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


def get_controller_key(controller_url: str, api_key: str) -> str:
    """Fetch a Controller's standing SSH public key (GET /remote/controller-key).
    Baked into a new VM's cloud-init so the Controller can SSH in after boot."""
    base = _normalize_base(controller_url)
    data = _get(base, "/remote/controller-key", api_key, allow_404=True)
    if isinstance(data, dict):
        return data.get("public_key", "") or ""
    return ""


def fetch_agent_bundle(controller_url: str, api_key: str) -> bytes:
    """Download a fresh one-time AGENT enrollment bundle (zip) from a Controller with the
    machine API key (GET /remote/agent-bundle). Each call mints a new single-use token,
    so fetch ONE bundle per host. This is the agent (pull) enrollment path — the target
    runs the bundle and self-enrolls outbound, so there's no inbound SSH-as-root and no
    human superuser token. Raises ControllerImportError on any failure."""
    base = _normalize_base(controller_url)
    url = base + "/remote/agent-bundle"
    try:
        resp = requests.get(url, headers={"X-API-Key": api_key}, verify=_tls_verify(), timeout=30)
    except requests.exceptions.RequestException as e:
        raise ControllerImportError(f"could not reach Controller: {e}")
    if resp.status_code != 200:
        detail = f"HTTP {resp.status_code}"
        try:
            detail = resp.json().get("detail") or detail
        except ValueError:
            detail = (resp.text or "").strip()[:200] or detail
        raise ControllerImportError(f"agent bundle download failed: {detail}")
    return resp.content


def register_ssh_host(controller_url: str, api_key: str, name: str, ip: str,
                      user: str = "root", environment: str = ""):
    """Register one SSH-managed host in a Controller (POST /remote/hosts). The
    Controller reaches it with its own key (get_controller_key). Returns (ok, detail)."""
    base = _normalize_base(controller_url)
    url = base + "/remote/hosts"
    try:
        resp = requests.post(url, headers={"X-API-Key": api_key},
                             json={"name": name, "ip": ip, "user": user, "environment": environment},
                             verify=_tls_verify(), timeout=20)
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
