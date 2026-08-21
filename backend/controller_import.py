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

The Controller's TLS is self-signed and operator-directed here (the operator
typed the address + key), so the fetch is unverified — mirroring the handoff pull.
"""
from __future__ import annotations

import requests

from . import db


class ControllerImportError(Exception):
    pass


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
    try:
        resp = requests.get(url, headers={"X-API-Key": api_key}, verify=False, timeout=20)
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
        resp = requests.post(url, json=payload, verify=False, timeout=20)
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


def import_into_inventory(inventory_id: int, controller_url: str, api_key: str):
    """Pull the Controller's agent + SSH hosts into an existing SLEP inventory.
    Returns {imported, agents, ssh, skipped, total, errors}."""
    inv = db.get_inventory(inventory_id)
    if not inv:
        raise ControllerImportError("Inventory not found.")
    if not api_key:
        raise ControllerImportError("Controller API key is required.")
    base = _normalize_base(controller_url)

    agents_n = ssh_n = skipped = 0
    errors: list[str] = []

    # --- agent-enrolled hosts (the primary listing) ---
    try:
        data = _get(base, "/agents", api_key)
        agents = data.get("agents", []) if isinstance(data, dict) else (data or [])
        for a in agents:
            name = str(a.get("hostname") or a.get("host_id") or "").strip()
            address = str(a.get("ip") or a.get("address") or name).strip()
            if not name or not address:
                skipped += 1
                continue
            db.upsert_host(
                inventory_id, name=name, address=address,
                groups=str(a.get("environment") or ""),
                variables={"sysible_source": "agent",
                           "sysible_host_id": a.get("host_id") or name,
                           "sysible_platform": a.get("platform") or ""},
                source="controller",
            )
            agents_n += 1
    except ControllerImportError as e:
        errors.append(f"agents: {e}")

    # --- SSH-managed hosts (carry connection user/port → runnable) ---
    try:
        hosts = _get(base, "/remote/hosts", api_key, allow_404=True)
        if isinstance(hosts, dict):
            for name, h in hosts.items():
                if not isinstance(h, dict):
                    continue
                nm = str(name).strip()
                address = str(h.get("ip") or nm).strip()
                if not nm or not address:
                    skipped += 1
                    continue
                variables = {"sysible_source": "ssh"}
                if h.get("user"):
                    variables["ansible_user"] = h["user"]
                if h.get("port"):
                    variables["ansible_port"] = h["port"]
                db.upsert_host(
                    inventory_id, name=nm, address=address,
                    groups=str(h.get("environment") or ""),
                    variables=variables, source="controller",
                )
                ssh_n += 1
    except ControllerImportError as e:
        errors.append(f"ssh hosts: {e}")

    total = agents_n + ssh_n
    if total == 0 and errors:
        # Nothing imported and something went wrong — surface it as a failure.
        raise ControllerImportError("; ".join(errors))
    return {"imported": total, "agents": agents_n, "ssh": ssh_n,
            "skipped": skipped, "total": total, "errors": errors}
