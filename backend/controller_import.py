"""Import inventory from a Sysible Controller.

The whole point of SLEP living next to Controller: the fleet you already manage
in Controller becomes the inventory you automate in SLEP, with one click. This
reads the Controller's agent list over its API and upserts each host into a SLEP
inventory (idempotent — re-importing refreshes addresses/groups, never duplicates).

Auth is the Controller's backend API key (X-API-Key), the same key its own BFF
uses. The Controller's TLS is self-signed and operator-directed here, so the
fetch is unverified (the operator typed the address + key) — mirroring how the
cross-controller handoff pulls a bundle.
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


def fetch_agents(controller_url: str, api_key: str, timeout: int = 20):
    """GET the Controller's /agents list. Returns the raw list of host dicts."""
    base = _normalize_base(controller_url)
    if not api_key:
        raise ControllerImportError("Controller API key is required.")
    try:
        resp = requests.get(
            f"{base}/agents",
            headers={"X-API-Key": api_key},
            verify=False,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        raise ControllerImportError(f"Could not reach the Controller at {base}: {e}")
    if resp.status_code in (401, 403):
        raise ControllerImportError("The Controller rejected that API key.")
    if resp.status_code != 200:
        raise ControllerImportError(
            f"The Controller returned HTTP {resp.status_code} for /agents."
        )
    try:
        data = resp.json()
    except ValueError:
        raise ControllerImportError("The Controller's /agents response was not JSON.")
    # Accept either a bare list or {"agents": [...]} / {"hosts": [...]}.
    if isinstance(data, dict):
        data = data.get("agents") or data.get("hosts") or []
    if not isinstance(data, list):
        raise ControllerImportError("Unexpected /agents payload shape.")
    return data


def _host_fields(agent: dict):
    """Map a Controller agent record to (name, address, groups) defensively —
    field names have varied across Controller versions."""
    name = (agent.get("hostname") or agent.get("name")
            or agent.get("host_id") or agent.get("id") or "").strip()
    address = (agent.get("address") or agent.get("ip") or agent.get("ansible_host")
               or name).strip()
    # Controller tags/environment become Ansible groups.
    groups = agent.get("groups") or agent.get("tags") or agent.get("environment") or ""
    if isinstance(groups, list):
        groups = ",".join(str(g) for g in groups)
    return name, address, str(groups)


def import_into_inventory(inventory_id: int, controller_url: str, api_key: str):
    """Pull the Controller's hosts into an existing SLEP inventory. Returns a
    summary dict {imported, skipped, total}."""
    inv = db.get_inventory(inventory_id)
    if not inv:
        raise ControllerImportError("Inventory not found.")
    agents = fetch_agents(controller_url, api_key)
    imported = skipped = 0
    for a in agents:
        name, address, groups = _host_fields(a)
        if not name or not address:
            skipped += 1
            continue
        db.upsert_host(
            inventory_id, name=name, address=address, groups=groups,
            variables={"sysible_host_id": a.get("host_id") or a.get("id") or name},
            source="controller",
        )
        imported += 1
    return {"imported": imported, "skipped": skipped, "total": len(agents)}
