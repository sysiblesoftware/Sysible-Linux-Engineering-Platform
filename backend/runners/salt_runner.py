"""Salt runner — apply Salt states over SSH with `salt-ssh` (agentless, so it
fits SLEP's SSH-native model; no minions to install).

Rendered per run from the SLEP database:
  * a ROSTER (which hosts, and how to reach them) from the selected inventory +
    SSH credential;
  * a minimal master CONFIG whose `file_roots` is the project dir, so the SLS
    states authored in the IDE are what salt applies.

The run's `target` is the state to apply (`state.apply <target>`); the literal
`highstate` runs `state.highstate`. Secrets (the SSH key) go to a 0600 temp file
that's removed when the run ends.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .. import db
from . import _common


def _render_roster(hosts, credential, key_path, dest: Path, bastion: str = "",
                   bastion_key: str = "") -> None:
    cred_user = (credential or {}).get("username") or ""
    kind = (credential or {}).get("kind") if credential else None
    # The jump hop authenticates with SLEP's managed key via an explicit ProxyCommand —
    # the SAME way the Ansible runner does it — because the bastion only trusts that key
    # ("Prepare jump host" installs it). A bare ProxyJump offers the bastion nothing
    # usable and the hop is refused (this was why Salt runs failed through a jump host).
    proxy = ""
    if bastion:
        if bastion_key:
            proxy = (f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                     f"-o BatchMode=yes -o ConnectTimeout=15 -i '{bastion_key}' -W %h:%p {bastion}")
        else:
            proxy = ""   # fall back to ProxyJump below if we have no key
    lines = []
    for h in hosts:
        # Target the inventory's ansible_user (the cloud-init login account) FIRST, so
        # Salt logs in as the same user Ansible does; then the credential username, then
        # root. Previously Salt used only credential.username/root and diverged from
        # Ansible whenever the credential had no username.
        huser = (h.get("variables") or {}).get("ansible_user") or cred_user or "root"
        lines.append(f"{h['name']}:")
        lines.append(f"  host: {h['address']}")
        lines.append(f"  user: {huser}")
        lines.append("  host_key_checking: False")
        if credential and kind == "ssh" and key_path:
            lines.append(f"  priv: {key_path}")
            lines.append("  sudo: True")   # key logins need sudo too (VMs grant NOPASSWD)
        elif credential and kind == "ssh_password" and credential.get("secret"):
            lines.append(f"  passwd: {credential['secret']}")
            lines.append("  sudo: True")
        # Optional SSH jump host (bastion): reach every host through it.
        if bastion:
            lines.append("  ssh_options:")
            if proxy:
                lines.append(f"    - ProxyCommand={proxy}")
            else:
                lines.append(f"    - ProxyJump={bastion}")
            lines.append("    - StrictHostKeyChecking=no")
    dest.write_text("\n".join(lines) + "\n")


def _write_key(secret: str, dest: Path) -> None:
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret.encode())
        if not secret.endswith("\n"):
            os.write(fd, b"\n")
    finally:
        os.close(fd)


def launch(run_id: int) -> None:
    run = db.get_run(run_id)
    if not run:
        return
    log_path = db.run_log_path(run_id)
    project = db.get_project(run["project_id"])
    workdir = db.project_dir(run["project_id"])
    hosts = db.list_hosts(run["inventory_id"]) if run.get("inventory_id") else []
    bastion = (db.get_inventory(run["inventory_id"]) or {}).get("bastion") or "" if run.get("inventory_id") else ""
    credential = (
        db.get_credential(run["credential_id"], include_secret=True)
        if run.get("credential_id") else None
    )
    state = (run["target"] or "").strip()
    try:
        extra_vars = json.loads(run.get("extra_vars") or "{}")
    except (TypeError, ValueError):
        extra_vars = {}

    tmp = Path(tempfile.mkdtemp(prefix=f"slep-salt-{run_id}-"))
    roster = tmp / "roster"
    key_file = tmp / "id_key"
    conf_dir = tmp / "conf"
    conf_dir.mkdir()

    db.set_run_status(run_id, "running", started=int(time.time()))
    with log_path.open("w", buffering=1) as log:
        def emit(m):
            log.write(m if m.endswith("\n") else m + "\n")

        emit(f"== SLEP run #{run_id} · project '{project['name']}' · salt-ssh {state or 'highstate'} ==")
        try:
            if not hosts:
                emit("!! No hosts in the selected inventory — nothing to target.")
                raise RuntimeError("empty inventory")

            key_path = None
            if credential and credential.get("kind") == "ssh" and credential.get("secret"):
                _write_key(credential["secret"], key_file)
                key_path = str(key_file)
            from .. import keydist
            _render_roster(hosts, credential, key_path, roster, bastion=bastion,
                           bastion_key=keydist.managed_key_path())

            # Minimal master config: states come from the project dir; keep all
            # of salt's scratch dirs inside the per-run temp so no root paths are
            # touched and nothing leaks between runs.
            (conf_dir / "master").write_text(
                "file_roots:\n  base:\n    - %s\n"
                "cachedir: %s\npki_dir: %s\nroster_file: %s\n"
                % (workdir, tmp / "cache", tmp / "pki", roster)
            )

            func = "state.highstate" if state.lower() == "highstate" or not state else "state.apply"
            cmd = ["salt-ssh", "-c", str(conf_dir), "-i", "--no-color", "'*'", func]
            if func == "state.apply":
                cmd.append(state)
            # extra_vars become salt kwargs (e.g. test=True for a dry run, or
            # pillar overrides key=value). Salt parses `k=v` trailing args itself.
            for k, v in extra_vars.items():
                cmd.append(f"{k}={v}")

            dry = str(extra_vars.get("test", "")).lower() in ("true", "1", "yes")
            emit(f"-- roster: {len(hosts)} host(s); credential: "
                 f"{credential['name'] if credential else 'none'}"
                 f"{'; test mode (dry-run)' if dry else ''} --")
            # Secret pillar/kwarg values must not be echoed into the viewer-readable log.
            rc = _common.stream(cmd, workdir, dict(os.environ), log,
                                redact=[str(v) for v in extra_vars.values() if str(v)], run_id=run_id)
            if _common.is_stopped(run_id):
                _common.clear_stop(run_id)
                emit("\n== canceled by operator ==")
                db.set_run_status(run_id, "canceled", exit_code=rc or 130, finished=int(time.time()))
                return
            emit(f"\n== finished: exit code {rc} ==")
            db.set_run_status(run_id, "success" if rc == 0 else "failed",
                              exit_code=rc, finished=int(time.time()))
        except Exception as e:  # noqa: BLE001
            emit(f"\n!! run aborted: {e}")
            cur = db.get_run(run_id)
            if cur and cur.get("status") == "running":
                db.set_run_status(run_id, "failed", exit_code=1, finished=int(time.time()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
