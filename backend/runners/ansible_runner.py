"""Ansible runner — turn a SLEP run row into an actual `ansible-playbook`
invocation and stream its output to the run log.

Design goals (the "friendlier than AAP" part):
  * No execution environment, no container per run — just run `ansible-playbook`
    from the project's own directory so roles/relative paths/`ansible.cfg` all
    resolve the way the author expects.
  * The inventory is RENDERED from the SLEP database into a throwaway INI file,
    so what runs always matches what the console shows.
  * Credentials never touch the browser or the project dir: the SSH key/password
    is written to a 0600 file in a per-run temp dir and removed when the run ends.

`launch(run_id)` blocks until the run finishes; the API layer calls it on a
background thread and the console tails the log file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .. import db, keydist, projcfg, vault
from . import _common

# Transient per-run sudo passwords (never persisted): set just before launch(),
# consumed once inside it. Keyed by run id; same process, so a plain dict is fine.
_BECOME: dict[int, str] = {}


def stash_become(run_id: int, password: str) -> None:
    if password:
        _BECOME[run_id] = password


def pop_become(run_id: int) -> str:
    return _BECOME.pop(run_id, "")


# Transient per-run CLI options (--limit, --start-at-task), same pattern as above.
_RUNOPTS: dict[int, dict] = {}


def stash_opts(run_id: int, opts: dict) -> None:
    opts = {k: v for k, v in (opts or {}).items() if v}
    if opts:
        _RUNOPTS[run_id] = opts


def pop_opts(run_id: int) -> dict:
    return _RUNOPTS.pop(run_id, {})


def _ansible_group(name: str) -> str:
    """Ansible INI group names allow only letters, digits and underscores — a
    Controller environment like "Sysible Labs" (with a space) would otherwise
    write an invalid `[Sysible Labs]` section and the whole inventory fails to
    parse. Map every other character to '_'."""
    g = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if g and g[0].isdigit():
        g = "g_" + g            # groups can't start with a digit
    return g or "ungrouped"


def _render_inventory(hosts, credential, dest: Path, bastion: str = "", bastion_key: str = "") -> None:
    """Write an Ansible INI inventory from SLEP hosts. Hosts are grouped by their
    comma-separated `groups`; every host also lands in the implicit `all`. SSH
    connection vars come from the attached credential."""
    # group name -> list of "hostline"
    groups: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    conn_user = (credential or {}).get("username") or ""

    bhost = keydist.bastion_host(bastion) if bastion else ""
    # Hosts that ARE the jump host must connect directly — jumping through
    # themselves closes the connection ("Connection closed by UNKNOWN").
    direct_names: list[str] = []

    for h in hosts:
        parts = [h["name"], f"ansible_host={h['address']}"]
        if conn_user:
            parts.append(f"ansible_user={conn_user}")
        # ssh_password credentials pass the password as a host var (server-side
        # only — never rendered into the console). Key creds use --private-key.
        if credential and credential.get("kind") == "ssh_password" and credential.get("secret"):
            parts.append(f"ansible_password={credential['secret']}")
            parts.append(f"ansible_become_password={credential['secret']}")
        for k, v in (h.get("variables") or {}).items():
            parts.append(f"{k}={json.dumps(v) if not isinstance(v, str) else v}")
        line = " ".join(parts)
        if bhost and h["address"] == bhost:
            direct_names.append(h["name"])
        gs = [_ansible_group(g) for g in (h.get("groups") or "").split(",") if g.strip()]
        if gs:
            for g in gs:
                groups.setdefault(g, []).append(line)
        else:
            ungrouped.append(line)

    common = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    with dest.open("w") as f:
        for line in ungrouped:
            f.write(line + "\n")
        for g, lines in groups.items():
            f.write(f"\n[{g}]\n")
            for line in lines:
                f.write(line + "\n")
        # Optional SSH jump host (bastion): reach every host through it. Native
        # ProxyJump doesn't reliably pass StrictHostKeyChecking=no down to the jump
        # sub-connection ("Connection closed by UNKNOWN"/"Host key verification
        # failed"), so spell the jump out as an explicit ProxyCommand that carries
        # the host-key options and keys into the bastion with SLEP's managed key
        # (which 'Prepare jump host' / 'Distribute SSH key' installed there).
        if bastion:
            f.write("\n[all:vars]\n")
            if bastion_key:
                proxy = f"ssh {common} -o BatchMode=yes -i {bastion_key} -W %h:%p {bastion}"
                f.write(f'ansible_ssh_common_args=-o ProxyCommand="{proxy}" {common}\n')
            else:
                f.write(f"ansible_ssh_common_args=-o ProxyJump={bastion} {common}\n")
            # A target that IS the jump host connects directly: a dedicated group
            # whose vars override [all:vars] (more specific than the 'all' group).
            if direct_names:
                f.write("\n[slep_direct]\n")
                for n in direct_names:
                    f.write(n + "\n")
                f.write("\n[slep_direct:vars]\n")
                f.write(f"ansible_ssh_common_args={common}\n")


def _emit_unreachable_help(emit, has_bastion: bool, proxy_hop_closed: bool,
                           auth_denied: bool, timed_out: bool) -> None:
    """Translate an all-UNREACHABLE recap into plain next steps. Ansible's raw
    'Connection closed by UNKNOWN port 65535' is opaque: with a ProxyJump, UNKNOWN
    means SSH got PAST the jump host and the onward hop to the target closed."""
    emit("")
    emit("!! Hosts were UNREACHABLE — SSH couldn't connect. This is a connectivity/")
    emit("   credential issue on the target side, not a playbook error. Likely causes:")
    if auth_denied:
        emit("   • Auth was refused — the selected credential's user/key isn't accepted")
        emit("     on the target. Check the SSH credential and that its key is authorized.")
    if proxy_hop_closed:
        emit("   • 'Connection closed by UNKNOWN' = the jump host connected but the hop to")
        emit("     the target closed. The bastion can reach itself but not the target:")
        emit("       – WRONG JUMP HOST: for libvirt/cloud VMs on a private NAT network, the jump")
        emit("         host must be the HYPERVISOR that runs them — it's the only machine on their")
        emit("         network. A sibling box on the LAN can reach itself but not the VMs. Set the")
        emit("         inventory's jump host to the hypervisor (the user@host from its qemu+ssh URI);")
        emit("         SLEP now does this automatically for VMs it builds, or")
        emit("       – the target has no SSH server running (Sysible Linux ships SSH OFF by")
        emit("         default — enable sshd on the host, or manage it via its agent), or")
        emit("       – the bastion's sshd has 'AllowTcpForwarding no'.")
    if timed_out:
        emit("   • The connection TIMED OUT — SSH never reached the host's port 22. This is")
        emit("     NOT a wrong key/user (that would say 'Permission denied'). It means one of:")
        emit("       – the VMs were still booting when this ran. Freshly-applied cloud-init VMs")
        emit("         take ~30–90s before sshd answers — just re-run the Ansible step, or run")
        emit("         the whole cadence again; SLEP now waits for :22 before configuring.")
        emit("       – SLEP has no network route to the VMs' subnet. libvirt VMs sit on a NAT")
        emit("         network (e.g. 192.168.x) that a SLEP *container* can't reach unless it")
        emit("         shares the host network or a route is added. Verify from the SLEP host:")
        emit("           nc -vz <target-ip> 22     (or)   ssh <user>@<target-ip>")
        emit("         If that also hangs, it's routing/firewall — not SLEP or the playbook.")
    if not (auth_denied or proxy_hop_closed or timed_out):
        emit("   • The target isn't accepting SSH: sshd not running (Sysible Linux ships SSH")
        emit("     OFF by default), wrong port, or a firewall in the way.")
    if has_bastion:
        emit("   • Verify the jump host manually:")
        emit("       ssh -J <user>@<bastion> <user>@<target>")
    else:
        emit("   • Verify SSH manually from the SLEP host:  ssh <user>@<target>")
    emit("   Note: agent-enrolled hosts are managed by the Controller's agent (outbound")
    emit("   poll) — Ansible needs INBOUND SSH to them, which is a separate path.")


def _wait_for_ssh(hosts, emit, timeout: int = 120, bastion: str = "") -> None:
    """Give freshly-applied VMs a chance to finish booting before handing off to
    Ansible, so a still-booting host is a short wait instead of an instant
    UNREACHABLE. TCP-probes each host's port 22 directly; if all answer on the
    first pass there's no delay at all. Skipped when a jump host is configured —
    the real path then runs through the bastion, which this direct probe can't
    model. Never fails the run: after the deadline it just proceeds (Ansible then
    reports the real outcome, and the UNREACHABLE help explains a persistent one)."""
    if bastion:
        return
    pending = [(h["name"], h["address"]) for h in hosts if h.get("address")]
    if not pending:
        return
    deadline = time.time() + timeout
    waited = False
    while pending and time.time() < deadline:
        still = []
        for nm, addr in pending:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            try:
                s.connect((addr, 22))
            except OSError:
                still.append((nm, addr))
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        if not still:
            break
        pending = still
        waited = True
        emit(f"-- waiting for SSH (:22) on {len(pending)} host(s) to come up: "
             f"{', '.join(a for _, a in pending)} …")
        time.sleep(6)
    if waited and not pending:
        emit("-- all hosts now answer on :22 — proceeding.\n")
    elif pending:
        emit(f"-- {len(pending)} host(s) still silent on :22 after {timeout}s: "
             f"{', '.join(a for _, a in pending)}. Proceeding — Ansible will report the outcome.\n")


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
    inventory = db.get_inventory(run["inventory_id"]) if run.get("inventory_id") else None
    hosts = db.list_hosts(run["inventory_id"]) if run.get("inventory_id") else []
    bastion = (inventory or {}).get("bastion") or ""
    # Fall back to the project's hypervisor jump host when the inventory itself has
    # none — infra VMs live on the hypervisor's private network, unreachable
    # directly. Covers inventories built before the jump host was known.
    if not bastion and run.get("project_id"):
        try:
            from .. import app as _app
            bastion = _app._project_hypervisor_bastion(run["project_id"])
        except Exception:  # noqa: BLE001
            bastion = ""
    credential = (
        db.get_credential(run["credential_id"], include_secret=True)
        if run.get("credential_id") else None
    )
    try:
        extra_vars = json.loads(run.get("extra_vars") or "{}")
    except (TypeError, ValueError):
        extra_vars = {}

    tmp = Path(tempfile.mkdtemp(prefix=f"slep-run-{run_id}-"))
    inv_file = tmp / "inventory.ini"
    key_file = tmp / "id_key"

    db.set_run_status(run_id, "running", started=int(time.time()))

    with log_path.open("w", buffering=1) as log:
        def emit(msg: str):
            log.write(msg if msg.endswith("\n") else msg + "\n")

        try:
            if not hosts:
                emit("!! No hosts in the selected inventory — nothing to target.")
                raise RuntimeError("empty inventory")

            playbook = (workdir / run["target"]).resolve()
            # Refuse to escape the project dir (the target comes from the console).
            if not str(playbook).startswith(str(workdir.resolve())):
                emit(f"!! Playbook path escapes the project directory: {run['target']}")
                raise RuntimeError("invalid playbook path")
            if not playbook.is_file():
                emit(f"!! Playbook not found: {run['target']}")
                raise RuntimeError("playbook missing")

            # The jump hop authenticates with SLEP's managed key (installed on the
            # bastion by 'Prepare jump host' / 'Distribute SSH key').
            _render_inventory(hosts, credential, inv_file, bastion=bastion,
                              bastion_key=keydist.managed_key_path())

            # Freshly-applied VMs may still be booting — wait for :22 so the cadence
            # (apply → inventory → configure) doesn't race the boot. No-op delay when
            # hosts are already up, or when reached through a jump host.
            _wait_for_ssh(hosts, emit, bastion=bastion)

            cmd = ["ansible-playbook", "-i", str(inv_file), str(playbook)]
            if credential and credential.get("kind") == "ssh" and credential.get("secret"):
                _write_key(credential["secret"], key_file)
                cmd += ["--private-key", str(key_file)]
            # Targeted re-run: --limit narrows to a subset of hosts, --start-at-task
            # resumes at a named task (skipping the ones that already succeeded).
            opts = pop_opts(run_id)
            if opts.get("limit"):
                cmd += ["--limit", str(opts["limit"])]
                emit(f"-- limited to: {opts['limit']}")
            if opts.get("start_at_task"):
                cmd += ["--start-at-task", str(opts["start_at_task"])]
                emit(f"-- starting at task: {opts['start_at_task']}")
            for k, v in extra_vars.items():
                cmd += ["-e", f"{k}={v}"]

            # Inject the secrets vault as `vault.<name>` via a 0600 vars file
            # (-e @file keeps the values out of the process list / ps).
            secrets = db.all_secret_ciphertexts()
            if secrets:
                vfile = tmp / "vault.json"
                fd = os.open(str(vfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, json.dumps({"vault": {n: vault.decrypt(ct) for n, ct in secrets}}).encode())
                finally:
                    os.close(fd)
                cmd += ["-e", "@" + str(vfile)]

            # Sudo/become password: a per-run override (transient), else the one
            # stored (encrypted) on the credential. Passed via a 0600 vars file so
            # it never lands in the process list. Lets a key credential run `become`
            # tasks against password-sudo hosts (e.g. an admin account).
            # Per-run override (transient), else the credential's stored become
            # password (db returns it decrypted on the include_secret read).
            become_pw = pop_become(run_id) or (credential.get("become_secret") if credential else "") or ""
            if become_pw:
                bfile = tmp / "become.json"
                fd = os.open(str(bfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, json.dumps({"ansible_become_password": become_pw}).encode())
                finally:
                    os.close(fd)
                cmd += ["-e", "@" + str(bfile)]

            env = dict(os.environ)
            # The project's ansible.cfg (edited via the console's "Ansible Config"
            # action) is authoritative: point ansible at it explicitly and let it
            # own every setting it declares.
            if projcfg.exists(run["project_id"]):
                env["ANSIBLE_CONFIG"] = str(projcfg.config_path(run["project_id"]))
            # First-run friendliness: don't wedge on unknown host keys — UNLESS the
            # project's ansible.cfg sets host_key_checking itself (env vars override
            # ansible.cfg, so forcing it here would silently ignore the operator's
            # explicit choice).
            if not projcfg.defines(run["project_id"], "defaults", "host_key_checking"):
                env.setdefault("ANSIBLE_HOST_KEY_CHECKING", "False")
            env.setdefault("ANSIBLE_FORCE_COLOR", "1")

            # Secret -e values must not be echoed into the (viewer-readable) log.
            emit(f"== SLEP run #{run_id} · project '{project['name']}' ==")
            emit(f"$ {_common.shown_cmd(cmd, [str(v) for v in extra_vars.values() if str(v)])}")
            emit(f"-- inventory: {len(hosts)} host(s); credential: "
                 f"{credential['name'] if credential else 'none'}"
                 f"{'; jump host: ' + bastion if bastion else ''} --\n")
            log.flush()

            if _common.is_stopped(run_id):
                _common.clear_stop(run_id)
                emit("\n== canceled by operator ==")
                db.set_run_status(run_id, "canceled", exit_code=130, finished=int(time.time()))
                return
            proc = subprocess.Popen(
                cmd, cwd=str(workdir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True,
            )
            _common.register(run_id, proc)
            unreachable = proxy_hop_closed = auth_denied = timed_out = False
            try:
                for line in proc.stdout:      # live stream
                    log.write(line)
                    log.flush()
                    if "UNREACHABLE!" in line:
                        unreachable = True
                    if "Connection closed by UNKNOWN" in line:
                        proxy_hop_closed = True
                    if "Permission denied" in line or "Authentication failed" in line:
                        auth_denied = True
                    if "Connection timed out" in line or "Operation timed out" in line:
                        timed_out = True
                rc = proc.wait()
            finally:
                _common.unregister(run_id)

            if _common.is_stopped(run_id):
                _common.clear_stop(run_id)
                emit("\n== canceled by operator ==")
                db.set_run_status(run_id, "canceled", exit_code=rc or 130, finished=int(time.time()))
                return
            emit(f"\n== finished: exit code {rc} ==")
            if unreachable:
                _emit_unreachable_help(emit, bool(bastion), proxy_hop_closed, auth_denied, timed_out)
            db.set_run_status(
                run_id, "success" if rc == 0 else "failed",
                exit_code=rc, finished=int(time.time()),
            )
        except FileNotFoundError:
            emit("!! `ansible-playbook` is not installed on the SLEP host. "
                 "Install ansible (apt install ansible / pipx install ansible).")
            db.set_run_status(run_id, "failed", exit_code=127, finished=int(time.time()))
        except Exception as e:  # noqa: BLE001 — surface any failure into the log
            emit(f"\n!! run aborted: {e}")
            cur = db.get_run(run_id)
            if cur and cur.get("status") == "running":
                db.set_run_status(run_id, "failed", exit_code=1, finished=int(time.time()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
